"""ذخیره‌ی کاربران و آخرین FIRE هر نماد.

دو حالت:
  - اگر متغیر محیطی DATABASE_URL تنظیم شده باشد: Postgres (ماندگار؛ با دیپلوی پاک نمی‌شود)
  - در غیر این صورت: فایل JSON محلی (روی Render با هر دیپلوی پاک می‌شود)
"""
import json
import os
import threading
import time

import pandas as pd


class State:
    """حالت در حافظه + ذخیره در فایل JSON."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.users = set()
        self.last_fire = {}   # {symbol: {"t": iso_utc, "d": +1/-1, "msg": متن کامل پیام FIRE}}

    def describe(self):
        return f"JSON file ({self.path})"

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    d = json.load(f)
                self.users = set(d.get("users", []))
                self.last_fire = d.get("last_fire", {})
        except Exception as e:
            print("load_state error:", e)
        return self

    def save(self):
        try:
            with self.lock:
                with open(self.path, "w") as f:
                    json.dump({"users": list(self.users), "last_fire": self.last_fire}, f)
        except Exception as e:
            print("save_state error:", e)

    def _store_user(self, chat_id):
        self.save()

    def _store_fire(self, name):
        self.save()

    def add_user(self, chat_id):
        if chat_id in self.users:
            return
        self.users.add(chat_id)
        self._store_user(chat_id)

    def record_fire(self, name, s, ts, msg=None):
        cur = self.last_fire.get(name)
        if cur is None or ts >= pd.Timestamp(cur["t"]):   # >= : همان FIRE پس از ری‌استارت، متنش به‌روز می‌شود
            self.last_fire[name] = {"t": ts.isoformat(), "d": s, "msg": msg}
            self._store_fire(name)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS last_fire (
    symbol TEXT PRIMARY KEY,
    t      TIMESTAMPTZ NOT NULL,
    d      SMALLINT    NOT NULL,
    msg    TEXT
);
"""


class PgState(State):
    """همان حالت در حافظه، اما هر تغییر در Postgres نوشته می‌شود.

    هر عملیات اتصال جداگانه باز می‌کند؛ نوشتن‌ها کم‌اند و این کار در برابر
    قطع اتصال‌های بیکاری (مثلاً خواب رفتن Neon) مقاوم است.
    """

    def __init__(self, url, json_path=None):
        super().__init__(json_path)
        self.url = url

    def describe(self):
        return "Postgres"

    def _connect(self):
        import psycopg
        return psycopg.connect(self.url, connect_timeout=15)

    def load(self):
        with self._connect() as con:
            con.execute(SCHEMA)
            self.users = {r[0] for r in con.execute("SELECT chat_id FROM users")}
            self.last_fire = {
                sym: {"t": pd.Timestamp(t).tz_convert("UTC").isoformat(), "d": d, "msg": msg}
                for sym, t, d, msg in con.execute("SELECT symbol, t, d, msg FROM last_fire")
            }
        self._import_json()
        return self

    def _import_json(self):
        """یک‌بار: اگر فایل JSON قدیمی موجود است، کاربرانش را به دیتابیس منتقل کن."""
        if not self.path or not os.path.exists(self.path):
            return
        old = State(self.path).load()
        for chat_id in old.users - self.users:
            self.add_user(chat_id)
        for sym, r in old.last_fire.items():
            self.record_fire(sym, r["d"], pd.Timestamp(r["t"]), r.get("msg"))

    def save(self):
        pass   # هر تغییر همان لحظه نوشته می‌شود

    def _store_user(self, chat_id):
        try:
            with self.lock, self._connect() as con:
                con.execute("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
                            (chat_id,))
        except Exception as e:
            print("db add_user error:", e)

    def _store_fire(self, name):
        r = self.last_fire[name]
        try:
            with self.lock, self._connect() as con:
                con.execute(
                    "INSERT INTO last_fire (symbol, t, d, msg) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (symbol) DO UPDATE SET t = EXCLUDED.t, d = EXCLUDED.d, msg = EXCLUDED.msg "
                    "WHERE last_fire.t <= EXCLUDED.t",
                    (name, pd.Timestamp(r["t"]).to_pydatetime(), r["d"], r["msg"]))
        except Exception as e:
            print("db record_fire error:", e)


def clean_db_url(raw):
    """رشته‌ی اتصال را از چیزهای اضافه‌ای که معمولاً همراهش کپی می‌شود پاک می‌کند:
    خطوط بعدی (.env کامل)، پیشوند DATABASE_URL=، دستور psql و گیومه‌ها."""
    s = (raw or "").strip()
    if not s:
        return ""
    for tok in s.replace("\n", " ").split():
        tok = tok.strip().strip("'\"")
        if "=" in tok and tok.split("=", 1)[0].isupper():
            tok = tok.split("=", 1)[1].strip().strip("'\"")      # DATABASE_URL=...
        if tok.startswith(("postgresql://", "postgres://")):
            return tok
    return s


def open_state(json_path):
    """Postgres اگر DATABASE_URL تنظیم شده باشد، وگرنه فایل JSON. اگر دیتابیس در دسترس نباشد، به فایل برمی‌گردد."""
    url = clean_db_url(os.environ.get("DATABASE_URL"))
    if url:
        for attempt in range(6):
            try:
                return PgState(url, json_path).load()
            except Exception as e:
                print(f"db connect error (attempt {attempt + 1}/6):", str(e)[:200])
                time.sleep(10)
        print("WARNING: database unavailable, falling back to JSON file")
    return State(json_path).load()
