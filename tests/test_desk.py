import json
from datetime import timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from app import LocalServer
from monitor import Desk, MonitorError, alpha_snapshot, bark_push, csv_rows, now


class DeskTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.desk = Desk(Path(folder.name))

    def test_demo_blocks_stale_and_deduplicates_recovery(self):
        self.desk.seed()
        for _ in range(3):
            self.desk.run()
        state = self.desk.snapshot()
        self.assertEqual(state["counts"], {"alert": 1, "blocked": 1})
        self.assertEqual(len(state["samples"]), 6)
        self.assertEqual(state["events"][0]["kind"], "alert")

    def test_save_redacts_and_clears_key(self):
        conf = {"source": "demo", "threshold": 2, "bark_key": "example-test-device", "push": True}
        self.desk.save(conf)
        state = self.desk.snapshot()
        self.assertTrue(state["config"]["has_bark_key"])
        self.assertNotIn("example-test-device", json.dumps(state))
        self.desk.save({**conf, "bark_key": "", "clear_bark_key": True})
        self.assertFalse(self.desk.snapshot()["config"]["push"])

    def test_csv_import_is_local_and_validates_timestamp(self):
        self.desk.save({"source": "csv", "threshold": 2})
        content = "timestamp,symbol,value,change_pct\n" + now().isoformat() + ",TEST,103,3\n"
        self.assertEqual(self.desk.import_csv(content), 1)
        self.desk.import_csv(content)
        self.assertEqual(self.desk.snapshot()["counts"], {"alert": 1})
        with self.assertRaises(MonitorError):
            csv_rows("timestamp,symbol,value,change_pct\n2026-01-15T09:30:00,TEST,103,3\n")

    def test_reading_becomes_stale_even_without_new_run(self):
        start = now()
        self.desk.save({"source": "csv", "threshold": 2})
        with patch("monitor.now", return_value=start):
            self.desk.import_csv("timestamp,symbol,value,change_pct\n" + start.isoformat() + ",TEST,103,3\n")
        with patch("monitor.now", return_value=start + timedelta(minutes=21)):
            self.assertEqual(self.desk.snapshot()["samples"][0]["state"], "stale")

    def test_alpha_rejects_provider_errors_without_returning_credentials(self):
        with patch("monitor.fetch", return_value=b'{"Information":"synthetic quota response"}'):
            with self.assertRaisesRegex(MonitorError, "有效日线"):
                alpha_snapshot("TEST", "example-test-key")

    def test_alert_sends_only_once_with_opt_in(self):
        self.desk.save({"source": "alpha", "threshold": 2, "alpha_key": "example-test-key", "symbol": "TEST",
                        "bark_key": "example-test-device", "push": True})
        with patch("monitor.alpha_snapshot", return_value=(now(), "TEST", 103, 3)), patch("monitor.bark_push") as push:
            self.desk.run()
            self.desk.run()
        push.assert_called_once()
        self.assertEqual(self.desk.snapshot()["events"][0]["delivery"], "sent")

    def test_bark_uses_json_post_to_fixed_host(self):
        with patch("monitor.fetch", return_value=b'{"code":200}') as fetch:
            bark_push("example-test-device", "Test", "Synthetic test")
        endpoint, payload = fetch.call_args.args
        self.assertEqual(endpoint, "https://api.day.app/push")
        self.assertEqual(payload["device_key"], "example-test-device")


class ServerTests(unittest.TestCase):
    def test_loopback_and_csrf(self):
        with tempfile.TemporaryDirectory() as folder:
            desk = Desk(folder)
            server = LocalServer(0, desk)
            port = server.server_address[1]
            server.address = f"127.0.0.1:{port}"
            url = f"http://{server.address}"
            self.assertEqual(server.server_address[0], "127.0.0.1")
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with urllib.request.urlopen(url + "/api/state", timeout=3) as response:
                    state = json.load(response)
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                with self.assertRaises(urllib.error.HTTPError) as bad_host:
                    urllib.request.urlopen(urllib.request.Request(url + "/api/health", headers={"Host": "wrong.test"}), timeout=3)
                self.assertEqual(bad_host.exception.code, 403)
                headers = {"Content-Type": "application/json", "Origin": "https://wrong.test", "X-Desk-Token": state["csrf"]}
                request = urllib.request.Request(url + "/api/run", data=b"{}", headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as bad_origin:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(bad_origin.exception.code, 403)
                headers["Origin"] = url
                request = urllib.request.Request(url + "/api/run", data=b"{}", headers=headers)
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertTrue(json.load(response)["ok"])
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()
