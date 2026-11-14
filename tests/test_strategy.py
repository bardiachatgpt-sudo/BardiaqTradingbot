"""تست رگرسیون: خروجی استراتژی روی داده‌ی ساختگی باید دقیقاً با فایل مرجع یکی باشد.

اجرا:  python -m unittest discover -s tests -v

اگر عمداً منطق سیگنال یا متن پیام را تغییر دادید، فایل مرجع را دوباره بسازید:
    python tests/test_strategy.py --regen
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import synthetic  # noqa: E402
from bot.scanner import feed  # noqa: E402
from bot.storage import State  # noqa: E402
from bot.strategy import SymbolEngine  # noqa: E402

GOLDEN = os.path.join(ROOT, "tests", "golden_events.json")


def run_case(name):
    state = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    msgs = []
    feed(SymbolEngine(name), synthetic.candles(name), state, msgs.append, silent=False)
    return msgs, state.last_fire.get(name)


class GoldenTest(unittest.TestCase):
    def test_matches_golden(self):
        with open(GOLDEN) as f:
            golden = json.load(f)
        self.assertEqual(set(golden), set(synthetic.CASES))
        for name, exp in golden.items():
            with self.subTest(symbol=name):
                msgs, last = run_case(name)
                self.assertEqual(msgs, [e["msg"] for e in exp["events"]])
                self.assertEqual(last, exp["last_fire"])


def regen():
    out = {}
    for name in synthetic.CASES:
        msgs, last = run_case(name)
        out[name] = {"events": [{"msg": m} for m in msgs], "last_fire": last}
        print(name, len(msgs))
    with open(GOLDEN, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    if "--regen" in sys.argv:
        regen()
    else:
        unittest.main()
