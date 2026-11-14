"""داده‌ی ساختگی قطعی (با seed ثابت) برای تست‌های رگرسیون استراتژی."""
import math
import random

import pandas as pd

# نماد → (قیمت شروع، نوسان نسبی هر دقیقه)
CASES = {
    "BTCUSD": (68000.0, 0.0012),
    "XAUUSD": (2650.0, 0.0006),
    "EURUSD": (1.0850, 0.0003),
    "USDJPY": (151.20, 0.0003),
}
N_BARS = 20000
START = "2026-11-12 00:00"   # پنجشنبه؛ داده از آخر هفته هم عبور می‌کند


def candles(name, n=N_BARS, seed=None):
    p0, vol = CASES[name]
    rnd = random.Random(seed if seed is not None else name)
    idx = pd.date_range(START, periods=n, freq="1min", tz="UTC")
    rows, price = [], p0
    for i in range(n):
        drift = vol * math.sin(i / 40.0) * math.sin(i / 148.0)   # موج‌هایی برای ساختن روند و واگرایی
        op = price
        cl = op * (1 + drift + rnd.gauss(0, vol))
        hi = max(op, cl) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(op, cl) * (1 - abs(rnd.gauss(0, vol / 2)))
        rows.append((op, hi, lo, cl))
        price = cl
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])
