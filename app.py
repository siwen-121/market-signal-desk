"""Start Market Signal Desk and create a desktop launcher."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
import webbrowser

from monitor import Desk, MAX_BYTES, MonitorError, NAME, data_dir

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": ("index.html", "text/html"), "/desk.css": ("desk.css", "text/css"),
          "/desk.js": ("desk.js", "text/javascript")}
APP_ID = "market-signal-desk-v1"
POLL_SECONDS = 90 * 60


def desktop_launcher():
    if sys.platform == "win32":
        env = {**os.environ, "DESK_SCRIPT": str(ROOT / "app.py"), "DESK_PYTHON": sys.executable,
               "DESK_ROOT": str(ROOT)}
        script = r"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
if (-not $desktop) { throw 'Desktop folder unavailable' }
$path = Join-Path $desktop 'Market Signal Desk.lnk'
if (Test-Path -LiteralPath $path) {
  $existing = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
  if ($existing.Arguments -ne ('"' + $env:DESK_SCRIPT + '" serve')) {
    throw 'A shortcut with the same name already exists'
  }
}
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$pythonw = Join-Path (Split-Path $env:DESK_PYTHON) 'pythonw.exe'
$shortcut.TargetPath = if (Test-Path -LiteralPath $pythonw) { $pythonw } else { $env:DESK_PYTHON }
$shortcut.Arguments = '"' + $env:DESK_SCRIPT + '" serve'
$shortcut.WorkingDirectory = $env:DESK_ROOT
$shortcut.Description = 'Open Market Signal Desk'
$shortcut.Save()
"""
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, timeout=15)
        return
    desktop = Path.home() / "Desktop"
    if sys.platform != "darwin" and shutil.which("xdg-user-dir"):
        result = subprocess.check_output(["xdg-user-dir", "DESKTOP"], text=True, timeout=5).strip()
        if result and Path(result) != Path.home():
            desktop = Path(result)
    if not desktop.is_dir():
        raise MonitorError("没有找到可写的桌面目录，请用启动脚本打开面板。")
    name = f"{NAME}.command" if sys.platform == "darwin" else f"{NAME}.sh"
    path = desktop / name
    command = f"#!/bin/sh\nexec {shlex.join([sys.executable, str(ROOT / 'app.py'), 'serve'])}\n"
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(command)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != command:
            raise MonitorError("桌面已有同名入口，请先移走旧入口后重试。") from None
    path.chmod(0o700)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, desk):
        self.desk = desk
        self.csrf = secrets.token_urlsafe(32)
        self.address = f"127.0.0.1:{port}"
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, *_args):
        return

    def reply(self, status, body, mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, payload):
        self.reply(status, json.dumps(payload, ensure_ascii=False).encode())

    def valid_host(self):
        return self.headers.get_all("Host") == [self.server.address]

    def do_GET(self):
        if not self.valid_host():
            return self.json(403, {"error": "请从本机启动入口打开面板。"})
        if self.path in ASSETS:
            name, mime = ASSETS[self.path]
            return self.reply(200, (ROOT / "web" / name).read_bytes(), mime)
        if self.path == "/api/health":
            return self.json(200, {"app": APP_ID})
        if self.path == "/api/state":
            return self.json(200, {"csrf": self.server.csrf, **self.server.desk.snapshot()})
        return self.json(404, {"error": "此入口不存在。"})

    def do_POST(self):
        valid = (self.valid_host() and
                 self.headers.get("Origin") == f"http://{self.server.address}" and
                 secrets.compare_digest(self.headers.get("X-Desk-Token", "").encode(),
                                        self.server.csrf.encode()))
        if not valid:
            return self.json(403, {"error": "会话已失效，请从本机面板刷新后重试。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BYTES or self.headers.get_content_type() != "application/json":
                return self.json(413, {"error": "只接受不超过 1 MiB 的 JSON 请求。"})
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise MonitorError("请求格式无效。")
            desk = self.server.desk
            result = {"ok": True}
            if self.path == "/api/save":
                desk.save(value)
            elif self.path == "/api/run":
                result["errors"] = desk.run()
            elif self.path == "/api/csv":
                if not isinstance(value.get("text"), str):
                    raise MonitorError("请选择 CSV 文件。")
                result["rows"] = desk.import_csv(value["text"])
            elif self.path == "/api/bark-test":
                desk.test_push()
            elif self.path == "/api/desktop":
                desktop_launcher()
            elif self.path == "/api/stop":
                stop = threading.Timer(0.2, self.server.shutdown)
                stop.daemon = True
                stop.start()
            else:
                return self.json(404, {"error": "此入口不存在。"})
            self.json(200, result)
        except MonitorError as exc:
            self.json(422, {"error": str(exc)})
        except (ValueError, UnicodeError, TimeoutError):
            self.json(400, {"error": "请求格式无效或等待超时。"})
        except (OSError, subprocess.SubprocessError):
            self.json(500, {"error": "本机操作失败，请检查文件权限或使用原启动脚本。"})


def scheduler(desk, stop):
    while not stop.wait(POLL_SECONDS):
        try:
            desk.run(scheduled=True)
        except MonitorError:
            continue


def running(url):
    try:
        with urllib.request.urlopen(url + "api/health", timeout=1) as response:
            return json.loads(response.read(512)).get("app") == APP_ID
    except (ValueError, OSError, AttributeError):
        return False


def main():
    if sys.version_info < (3, 10):
        print("Python 3.10+ required.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("setup", "serve"), nargs="?", default="setup")
    parser.add_argument("--data-dir", type=Path, default=data_dir())
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port from 1024 to 65535.")
    url = f"http://127.0.0.1:{args.port}/"
    try:
        if args.mode == "setup" and not args.no_browser:
            try:
                desktop_launcher()
            except (MonitorError, OSError, subprocess.SubprocessError) as exc:
                if sys.stderr:
                    print(f"桌面入口未创建。面板仍可启动。{exc}", file=sys.stderr)
        if running(url):
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        desk = Desk(args.data_dir)
        desk.seed()
        server = LocalServer(args.port, desk)
    except (OSError, MonitorError) as exc:
        if sys.stderr:
            print(f"启动失败。检查本地端口和数据目录权限。{exc}", file=sys.stderr)
        return 1
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(desk, stop), daemon=True).start()
    if sys.stdout:
        print(f"{NAME}  {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
