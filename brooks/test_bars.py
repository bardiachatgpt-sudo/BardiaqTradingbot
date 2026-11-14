"""Unit tests on small hand-made bar sequences.  Run:  python -m unittest brooks.test_bars -v"""
import unittest

import pandas as pd

from brooks import bars


def mk(rows):
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"],
                        index=pd.date_range("2026-01-01", periods=len(rows), freq="5min"))


# ten ordinary bars to build a "recent median body" of about 1.0
BASE = [(100, 101.2, 99.8, 101), (101, 101.3, 99.9, 100), (100, 101.2, 99.8, 101),
        (101, 101.3, 99.9, 100), (100, 101.2, 99.8, 101), (101, 101.3, 99.9, 100),
        (100, 101.2, 99.8, 101), (101, 101.3, 99.9, 100), (100, 101.2, 99.8, 101),
        (101, 101.3, 99.9, 100)]


class BarTypes(unittest.TestCase):
    def test_doji_and_trend_bar(self):
        df = bars.classify(mk(BASE + [(100, 101, 99, 100.1),          # tiny body -> doji
                                      (100, 103.1, 99.9, 103)]))      # big bull bar
        self.assertTrue(df["is_doji"].iat[-2])
        self.assertEqual(df["direction"].iat[-1], 1)
        self.assertTrue(df["strong"].iat[-1])
        self.assertEqual(df["strength"].iat[-1], 6)

    def test_weak_bull_bar_big_tail(self):
        df = bars.classify(mk(BASE + [(100, 104, 99.9, 101.1)]))     # long upper tail
        self.assertEqual(df["direction"].iat[-1], 1)
        self.assertFalse(df["strong"].iat[-1])


class Exhaustion(unittest.TestCase):
    def test_huge_bar_after_long_trend(self):
        rows = [(100 + i, 101.2 + i, 99.8 + i, 101 + i) for i in range(40)]   # 40-bar bull trend
        rows.append((140, 146.1, 139.9, 146))                                  # extremely large bull bar
        df = bars.classify(mk(rows))
        self.assertTrue(df["exhaustion"].iat[-1])
        self.assertFalse(df["exhaustion"].iat[20])


class Climax(unittest.TestCase):
    def test_three_bar_buy_climax_then_reversal(self):
        rows = BASE + [(100, 102.1, 99.9, 102), (102, 104.1, 101.9, 104), (104, 106.1, 103.9, 106),
                       (106, 106.2, 104.5, 105.8),     # pause: prominent upper tail? no -> small bear-ish
                       (105.8, 105.9, 102.9, 103)]     # strong bear trend bar
        df = bars.classify(mk(rows))
        cl = bars.climaxes(df)
        self.assertEqual(len(cl), 1)
        self.assertEqual(cl[0]["direction"], 1)
        self.assertEqual(cl[0]["bars"], 3)
        self.assertEqual(cl[0]["reversal_bar"], len(rows) - 1)


class LegCount(unittest.TestCase):
    def test_h1_h2(self):
        # up move to a high, then two down legs with a bounce in between
        rows = [(100, 101, 99, 100.5), (100.5, 103, 100.4, 102.8), (102.8, 105, 102.7, 104.9),  # high 105
                (104.9, 104.0, 103, 103.2),      # leg 1 down (lower high, lower low)
                (103.2, 104.2, 102.9, 104),      # high above prior high -> H1
                (103.9, 103.0, 102, 102.2),      # leg 2 down
                (102.2, 103.5, 101.8, 103.4),    # -> H2
                (103.4, 105.5, 103.3, 105.4)]    # new high -> resets
        lab = bars.leg_counts(mk(rows))
        self.assertIn("H1", lab.iat[4])
        self.assertIn("H2", lab.iat[6])
        self.assertNotIn("H", lab.iat[7])

    def test_l1_l2_mirror(self):
        rows = [(105, 106, 104, 104.5), (104.5, 104.6, 102, 102.2), (102.2, 102.3, 100, 100.1),  # low 100
                (100.1, 102, 101, 101.8),        # leg up (higher low)
                (101.8, 101.9, 100.8, 101),      # low below prior low -> L1
                (101, 103, 100.9, 102.8),        # leg up
                (102.8, 102.9, 100.85, 101.6)]   # -> L2
        lab = bars.leg_counts(mk(rows))
        self.assertIn("L1", lab.iat[4])
        self.assertIn("L2", lab.iat[6])


class TrendingDojis(unittest.TestCase):
    def test_rising_dojis(self):
        rows = BASE + [(100, 100.6, 99.6, 100.05), (100.1, 100.8, 99.7, 100.15),
                       (100.2, 101.0, 99.8, 100.25), (100.3, 101.2, 99.9, 100.35)]
        df = bars.classify(mk(rows))
        td = bars.trending_dojis(df)
        self.assertTrue(any(t["direction"] == 1 for t in td))


if __name__ == "__main__":
    unittest.main()
