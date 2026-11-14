"""دکمه‌ی آخرین FIRE: همیشه با قالب فعلی، حتی برای FIREهای قدیمیِ بدون قیمت."""
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot import telegram_bot  # noqa: E402
from bot.storage import State  # noqa: E402

T = pd.Timestamp("2026-11-14 01:07", tz="UTC")      # 04:37 تهران
OLD_MSG = "🔥 <b>FIRE · BEARISH📉</b>\n...\n📐 <b>Divergence · MACD (48,104,36)</b>"


def make_tg():
    st = State(os.path.join(tempfile.mkdtemp(), "s.json"))
    return telegram_bot.Telegram(None, st), st


class LastFireTest(unittest.TestCase):
    def test_new_record_uses_current_format(self):
        tg, st = make_tg()
        st.record_fire("BTCUSD", -1, T, OLD_MSG, 68400.0, True)
        txt = tg.last_fire_text("BTCUSD")
        self.assertIn("💰 Price: <b>68400</b>", txt)
        self.assertIn("Saturday 2026-11-14 04:37", txt)
        self.assertIn("London (for Kamyar)", txt)
        self.assertIn("STRONG", txt)
        self.assertNotIn("Divergence", txt)

    def test_old_record_backfills_price_from_candles(self):
        tg, st = make_tg()
        st.record_fire("BTCUSD", -1, T, OLD_MSG)            # مثل رکورد قدیمی: بدون قیمت
        idx = pd.date_range(T - pd.Timedelta(minutes=5), periods=10, freq="1min")
        df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": range(68400, 68410)}, index=idx)
        with mock.patch.object(telegram_bot, "get_closed_candles", return_value=df) as g:
            txt = tg.last_fire_text("BTCUSD")
            self.assertIn("💰 Price: <b>68404</b>", txt)     # Close کندل 01:06 (بسته‌شدن 01:07)
            self.assertNotIn("Divergence", txt)
            self.assertNotIn("STRONG", txt)
            tg.last_fire_text("BTCUSD")                      # بار دوم از حافظه، بدون دریافت دوباره
            self.assertEqual(g.call_count, 1)
        self.assertEqual(st.last_fire["BTCUSD"]["price"], 68404.0)

    def test_old_record_falls_back_to_old_text_when_candles_unavailable(self):
        tg, st = make_tg()
        st.record_fire("BTCUSD", -1, T, OLD_MSG)
        with mock.patch.object(telegram_bot, "get_closed_candles", return_value=None):
            self.assertEqual(tg.last_fire_text("BTCUSD"), OLD_MSG)


if __name__ == "__main__":
    unittest.main()
