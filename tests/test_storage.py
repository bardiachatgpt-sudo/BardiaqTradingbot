"""تست ذخیره‌سازی. تست Postgres فقط وقتی اجرا می‌شود که TEST_DATABASE_URL تنظیم شده باشد."""
import json
import os
import sys
import tempfile
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot.storage import PgState, State, clean_db_url  # noqa: E402

T1 = pd.Timestamp("2026-11-14 01:07", tz="UTC")
T2 = pd.Timestamp("2026-11-14 02:00", tz="UTC")


class Common:
    def make(self):
        raise NotImplementedError

    def test_roundtrip(self):
        st = self.make().load()
        st.add_user(111)
        st.add_user(222)
        st.add_user(111)
        st.record_fire("BTCUSD", -1, T1, "msg1")
        st.record_fire("BTCUSD", 1, T2, "msg2")
        st.record_fire("BTCUSD", -1, T1, "older")       # قدیمی‌تر: نباید جایگزین شود
        st.record_fire("EURUSD", 1, T1, None)
        again = self.make().load()                     # مثل راه‌اندازی دوباره پس از دیپلوی
        self.assertEqual(again.users, {111, 222})
        self.assertEqual(again.last_fire["BTCUSD"], {"t": T2.isoformat(), "d": 1, "msg": "msg2"})
        self.assertEqual(again.last_fire["EURUSD"], {"t": T1.isoformat(), "d": 1, "msg": None})


class CleanUrlTest(unittest.TestCase):
    URL = "postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

    def test_variants(self):
        for raw in [
            self.URL,
            f"  {self.URL}  ",
            f'DATABASE_URL="{self.URL}"',
            f"DATABASE_URL='{self.URL}'",
            f"psql '{self.URL}'",
            f'DATABASE_URL="{self.URL}"\nDATABASE_URL_UNPOOLED="postgresql://other"\nPGHOST=ep-x',
            f'# comment\nDATABASE_URL={self.URL} PGUSER=u',
        ]:
            self.assertEqual(clean_db_url(raw), self.URL, raw)
        self.assertEqual(clean_db_url(None), "")
        self.assertEqual(clean_db_url(""), "")


class JsonStateTest(Common, unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "state.json")

    def make(self):
        return State(self.path)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL not set")
class PgStateTest(Common, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        with PgState(self.url)._connect() as con:
            con.execute("DROP TABLE IF EXISTS users, last_fire")

    def make(self):
        return PgState(self.url)

    def test_imports_old_json_once(self):
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        with open(path, "w") as f:
            json.dump({"users": [333], "last_fire": {
                "XAUUSD": {"t": T1.isoformat(), "d": -1, "msg": "old"}}}, f)
        st = PgState(self.url, path).load()
        self.assertEqual(st.users, {333})
        fresh = PgState(self.url).load()               # بدون فایل: از دیتابیس خوانده می‌شود
        self.assertEqual(fresh.users, {333})
        self.assertEqual(fresh.last_fire["XAUUSD"]["msg"], "old")


if __name__ == "__main__":
    unittest.main()
