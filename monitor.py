"""Local market data checks, event history and optional notifications."""
import csv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NAME = "Market Signal Desk"
FEED = "https://www.federalreserve.gov/feeds/press_all.xml"
ALPHA = "https://www.alphavantage.co/query"
BARK = "https://api.day.app/push"
MAX_BYTES = 1_048_576
DEFAULTS = {"source": "demo", "symbol": "", "alpha_key": "", "bark_key": "",
            "fed": False, "scheduled": False, "push": False, "threshold": 2.0}
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,19}")
KEY = re.compile(r"[A-Za-z0-9_-]{6,200}")
DEMO_MOVES = ((100, 0, False), (103, 3, True), (104, 4, False),
              (104, 4, False), (100.5, 0.5, False), (97, -3, False))


class MonitorError(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise MonitorError("时间戳须包含时区，例如 2026-01-15T09:30:00+08:00。") from exc


def data_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / NAME
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support" / NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "market-signal-desk"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, payload=None):
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                                     headers={"User-Agent": "MarketSignalDesk/0.1",
                                              "Content-Type": "application/json"} if payload else
                                             {"User-Agent": "MarketSignalDesk/0.1"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=9) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise MonitorError(f"服务返回 HTTP {exc.code}，请检查服务状态或账户配置。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MonitorError("服务请求失败或超时，请检查网络后重试。") from None
    if len(raw) > MAX_BYTES:
        raise MonitorError("数据源返回的内容超过 1 MiB。")
    return raw


def alpha_snapshot(symbol, key):
    query = urllib.parse.urlencode({"function": "TIME_SERIES_DAILY", "symbol": symbol,
                                    "outputsize": "compact", "apikey": key})
    raw = fetch(f"{ALPHA}?{query}")
    try:
        series = json.loads(raw).get("Time Series (Daily)")
        if not isinstance(series, dict) or len(series) < 2:
            raise ValueError
        dates = sorted(series, reverse=True)[:2]
        current, previous = (float(series[date]["4. close"]) for date in dates)
        dt = datetime.fromisoformat(dates[0]).replace(tzinfo=timezone.utc)
        if not math.isfinite(current) or not math.isfinite(previous) or min(current, previous) <= 0:
            raise ValueError
        change = (current / previous - 1) * 100
        if not math.isfinite(change):
            raise ValueError
    except (ValueError, AttributeError, TypeError, KeyError):
        raise MonitorError("行情源未返回有效日线。请检查 API Key、额度和交易代码。") from None
    return (dt, symbol, current, round(change, 3))


def fresh(source, dt):
    age = now() - dt
    limit = timedelta(days=4) if source == "alpha" else timedelta(minutes=20)
    return -timedelta(minutes=2) <= age <= limit


def csv_rows(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    fields = ("timestamp", "symbol", "value", "change_pct")
    if not reader.fieldnames or any(k not in reader.fieldnames for k in fields):
        raise MonitorError("CSV 需要 timestamp,symbol,value,change_pct 四列。")
    rows = []
    try:
        for row in reader:
            if len(rows) >= 500:
                raise MonitorError("每次最多导入 500 行。")
            symbol = row["symbol"].strip().upper()
            price, change = float(row["value"]), float(row["change_pct"])
            if not SYMBOL.fullmatch(symbol) or not math.isfinite(price) or price <= 0 or not math.isfinite(change):
                raise ValueError
            rows.append((timestamp(row["timestamp"]), symbol, price, change))
    except (ValueError, AttributeError, TypeError, csv.Error):
        raise MonitorError("CSV 存在无效代码、价格或变动值。") from None
    if not rows:
        raise MonitorError("CSV 没有可导入的数据。")
    return sorted(rows)


def bark_push(key, title, message):
    raw = fetch(BARK, {"device_key": key, "title": title, "body": message, "group": NAME})
    try:
        if json.loads(raw).get("code") == 200:
            return
    except (ValueError, AttributeError):
        pass
    raise MonitorError("Bark 没有确认接收，请检查设备 Key 和 iPhone 通知设置。")


def fed_events():
    raw = fetch(FEED)
    if b"<!DOCTYPE" in raw.upper():
        raise MonitorError("公告源包含不支持的 XML 定义。")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise MonitorError("公告源返回的 RSS 无法读取。") from None
    result = []
    for item in root.findall("./channel/item")[:25]:
        title, link = item.findtext("title", "").strip(), item.findtext("link", "").strip()
        try:
            date = parsedate_to_datetime(item.findtext("pubDate", ""))
            url = urllib.parse.urlsplit(link)
        except (ValueError, TypeError):
            continue
        if title and date.tzinfo and url.scheme == "https" and url.hostname == "www.federalreserve.gov":
            result.append((date, title[:300], link[:500]))
    if not result:
        raise MonitorError("公告源没有可读取的条目。")
    return result


class Desk:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "desk.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.actions = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS readings
                    (source TEXT, symbol TEXT, ts TEXT, value REAL, change REAL, state TEXT,
                     PRIMARY KEY(source, symbol, ts));
                CREATE TABLE IF NOT EXISTS events
                    (id TEXT PRIMARY KEY, ts TEXT, source TEXT, kind TEXT, title TEXT, detail TEXT,
                     delivery TEXT DEFAULT '', link TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

        return connection()

    def settings(self, db):
        row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return DEFAULTS | (json.loads(row["value"]) if row else {})

    def save(self, form):
        with self.connect() as db:
            conf = self.settings(db)
            source = form.get("source")
            if source not in ("demo", "csv", "alpha"):
                raise MonitorError("请选择支持的数据源。")
            symbol = str(form.get("symbol", "")).strip().upper()
            if symbol and not SYMBOL.fullmatch(symbol):
                raise MonitorError("交易代码只能包含字母、数字、点号和连字符。")
            threshold = form.get("threshold")
            if type(threshold) not in (int, float) or not 0.1 <= threshold <= 50:
                raise MonitorError("监控阈值应在 0.1% 至 50% 之间。")
            for field in ("alpha_key", "bark_key"):
                new = form.get(field, "")
                if not isinstance(new, str) or (new and not KEY.fullmatch(new)):
                    raise MonitorError("密钥格式不符，请粘贴服务提供的 Key。")
                if form.get(f"clear_{field}") is True:
                    conf[field] = ""
                elif new:
                    conf[field] = new
            if source == "alpha" and not (conf["alpha_key"] and symbol):
                raise MonitorError("Alpha Vantage 需要交易代码和 API Key。")
            conf.update(source=source, symbol=symbol, threshold=threshold,
                        fed=form.get("fed") is True, scheduled=form.get("scheduled") is True,
                        push=form.get("push") is True and bool(conf["bark_key"]))
            db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (json.dumps(conf),))

    def event(self, db, source, kind, title, detail, identity, link=""):
        key = hashlib.sha256(f"{source}|{kind}|{identity}".encode()).hexdigest()
        result = db.execute("INSERT OR IGNORE INTO events (id,ts,source,kind,title,detail,link) VALUES (?,?,?,?,?,?,?)",
                            (key, iso(now()), source, kind, title, detail, link))
        return (key, title, detail) if result.rowcount else None

    def record(self, db, conf, source, row):
        dt, symbol, value, change = row
        state = "fresh" if fresh(source, dt) else "stale"
        point = iso(dt)
        saved = db.execute("INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)",
                           (source, symbol, point, value, change, state))
        identity = f"{symbol}:{point}"
        if state == "stale":
            if not saved.rowcount:
                db.execute("UPDATE readings SET state='stale' WHERE source=? AND symbol=? AND ts=?",
                           (source, symbol, point))
            self.event(db, source, "blocked", f"{symbol} 数据已拦截", "时间戳超出允许范围。", identity)
        elif saved.rowcount and abs(change) >= conf["threshold"]:
            return self.event(db, source, "alert", f"{symbol} 达到监控阈值", f"变动 {change:+.2f}%。", identity)
        return None

    def set_status(self, db, key, value):
        db.execute("INSERT OR REPLACE INTO status VALUES (?,?)", (key, json.dumps(value)))

    def get_status(self, db, key, default=None):
        row = db.execute("SELECT value FROM status WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def advance_demo(self, db, conf):
        step = self.get_status(db, "demo_step", 0)
        value, change, stale = DEMO_MOVES[step % len(DEMO_MOVES)]
        point = now()
        if stale:
            point -= timedelta(minutes=45)
        if step % len(DEMO_MOVES) == 3:
            point = timestamp(self.get_status(db, "demo_last"))
        self.record(db, conf, "demo", (point, "DEMO", value, change))
        self.set_status(db, "demo_last", iso(point))
        self.set_status(db, "demo_step", step + 1)

    def seed(self):
        with self.connect() as db:
            if self.get_status(db, "demo_step") is not None:
                return
            conf = self.settings(db)
            for offset, value, change in ((8, 98, 0.0), (6, 99.1, 1.1), (4, 98.7, -0.4), (2, 100.0, 1.3)):
                self.record(db, conf, "demo", (now() - timedelta(minutes=offset), "DEMO", value, change))
            self.set_status(db, "demo_step", 1)

    def deliver(self, events, conf):
        if not conf["push"]:
            return
        for event in [e for e in events if e][:1]:
            key, title, body = event
            status = "sent"
            try:
                bark_push(conf["bark_key"], title, body)
            except MonitorError:
                status = "failed"
            with self.connect() as db:
                db.execute("UPDATE events SET delivery=? WHERE id=?", (status, key))

    def run(self, scheduled=False):
        if not self.actions.acquire(blocking=False):
            raise MonitorError("已有一次刷新在运行。")
        try:
            with self.connect() as db:
                conf = self.settings(db)
                if scheduled and (not conf["scheduled"] or (conf["source"] != "alpha" and not conf["fed"])):
                    return
            errors, events = [], []
            source = conf["source"]
            if source == "demo" and not scheduled:
                with self.connect() as db:
                    self.advance_demo(db, conf)
            if source == "alpha":
                try:
                    row = alpha_snapshot(conf["symbol"], conf["alpha_key"])
                    with self.connect() as db:
                        events.append(self.record(db, conf, source, row))
                        self.set_status(db, "alpha", {"ok": True, "at": iso(now()), "message": "日线响应已接收。"})
                except MonitorError as exc:
                    errors.append(str(exc))
                    with self.connect() as db:
                        self.set_status(db, "alpha", {"ok": False, "at": iso(now()), "message": str(exc)})
            if conf["fed"]:
                try:
                    rows = fed_events()
                    with self.connect() as db:
                        for date, title, link in rows:
                            self.event(db, "fed", "notice", title, f"公告时间 {iso(date)}", link, link)
                        self.set_status(db, "fed", {"ok": True, "at": iso(now()), "message": "公告已校验并去重。"})
                except MonitorError as exc:
                    errors.append(str(exc))
                    with self.connect() as db:
                        self.set_status(db, "fed", {"ok": False, "at": iso(now()), "message": str(exc)})
            self.deliver(events, conf)
            return errors
        finally:
            self.actions.release()

    def import_csv(self, text):
        rows = csv_rows(text)
        with self.connect() as db:
            conf = self.settings(db)
            if conf["source"] != "csv":
                raise MonitorError("请先保存 CSV 数据源设置。")
            for row in rows:
                self.record(db, conf, "csv", row)
            self.set_status(db, "csv", {"ok": True, "at": iso(now()), "message": f"已检查 {len(rows)} 行。"})
        return len(rows)

    def test_push(self):
        with self.connect() as db:
            key = self.settings(db)["bark_key"]
        if not key:
            raise MonitorError("请先保存 Bark 设备 Key。")
        bark_push(key, NAME, "测试通知。收到后可开启日线告警推送。")

    def snapshot(self):
        with self.connect() as db:
            conf = self.settings(db)
            visible = {k: v for k, v in conf.items() if k not in {"alpha_key", "bark_key"}}
            visible.update(has_alpha_key=bool(conf["alpha_key"]), has_bark_key=bool(conf["bark_key"]))
            source = conf["source"]
            samples = db.execute("SELECT * FROM readings WHERE source=? AND (?!='alpha' OR symbol=?) "
                                 "ORDER BY ts DESC LIMIT 40", (source, source, conf["symbol"])).fetchall()
            events = db.execute("SELECT * FROM events WHERE source=? OR (source='fed' AND ?) ORDER BY rowid DESC LIMIT 25",
                                (source, conf["fed"])).fetchall()
            counts = db.execute("SELECT kind, count(*) AS count FROM events WHERE (source=? OR (source='fed' AND ?)) "
                                "AND ts>=? GROUP BY kind", (source, conf["fed"], iso(now() - timedelta(hours=24)))).fetchall()
            rows = [dict(row) for row in reversed(samples)]
            for row in rows:
                if not fresh(source, timestamp(row["ts"])):
                    row["state"] = "stale"
            return {"config": visible, "samples": rows,
                    "events": [dict(r) for r in events], "counts": {r["kind"]: r["count"] for r in counts},
                    "status": {k: self.get_status(db, k) for k in ("alpha", "fed", "csv")}}
