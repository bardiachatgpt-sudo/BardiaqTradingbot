"""سناریوی صعودی، قدم ۱: آیا واگرایی صعودی تایم‌های خیلی پایین، کف (3,6,2) دیلی را پیش‌بینی می‌کند؟

آماده‌باش: (3,6,2) دیلی N میله زیر صفر (N = 1 یا 2).
  - «با امروز»: میله‌ی امروزِ هنوز باز (با قیمت لحظه‌ای) هم شمرده می‌شود.
  - «بسته‌شده»: فقط میله‌های بسته‌شده.
ماشه: واگرایی صعودی روی (48,104,36) M1 یا (12,26,9) M5 (همان منطق واگرایی ربات).

برای هر سیگنال:
  - آیا همان میله‌ی دیلی (یا فردایش) کفِ (3,6,2) دیلی شد؟ (کف = پایین‌تر از هر دو میله‌ی کناری)
  - حرکت قیمت ۲۴ و ۷۲ ساعت بعد.
مبنا: همه‌ی واگرایی‌های صعودی، بدون آماده‌باش.

اجرا:  python -m backtest.daily_bottom --symbols XAUUSD,BTCUSD --start 2026-06-01 --end 2026-11-14
"""
import argparse

import numpy as np
import pandas as pd

from bot.indicators import MACDHist
from bot.strategy import DivDetector, SymbolEngine

from . import data
from .mtf import Bars, macd_peek, server_time


def scan(name, start, end, warm_days=30):
    df = data.candles(name, pd.Timestamp(start) - pd.Timedelta(days=warm_days), end)
    t_from = pd.Timestamp(start, tz="UTC")
    eng = SymbolEngine(name)
    d_bars = Bars(lambda st: st.date())
    m5_bars = Bars(lambda st: (st.date(), st.hour, st.minute // 5))
    d362 = MACDHist(3, 6, 2)
    d_h = []                                   # (3,6,2) دیلی میله‌های بسته‌شده
    m5m = MACDHist(12, 26, 9)
    m5_div, m5_n = DivDetector(), 0
    seen = None
    sigs = []
    o_, h_, l_, c_ = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    for i, ts in enumerate(df.index):
        o, h, l, c = o_[i], h_[i], l_[i], c_[i]
        st = server_time(ts)
        done = d_bars.update(st, o, h, l, c)
        if done:
            d_h.append(d362.update(done["c"]))
        src = []
        done = m5_bars.update(st, o, h, l, c)
        if done:
            m5_n += 1
            if m5_div.update(m5m.update(done["c"]), done["l"], done["h"], ts, m5_n > 300):
                src.append("M5 (12,26,9)")
        eng.on_bar(ts, o, h, l, c)
        d = eng.det1[1].detail
        if d is not None and d is not seen:
            seen = d
            src.append("M1 (48,104,36)")
        if not src or ts < t_from or len(d_h) < 20:
            continue
        k_closed = 0
        for v in reversed(d_h):
            if v >= 0:
                break
            k_closed += 1
        running = macd_peek(d362, c)
        for s in src:
            sigs.append({"symbol": name, "src": s, "t": ts + pd.Timedelta(minutes=1), "price": c,
                         "day": len(d_h), "k_closed": k_closed,
                         "k_today": (k_closed + 1) if running < 0 else 0})
    # نتیجه‌ها
    if d_bars.cur:                             # میله‌ی آخر (هنوز باز) برای ارزیابی روز آخر
        d_h.append(d362.update(d_bars.cur["c"]))
    idx = df.index

    def is_bottom(j):
        return 0 < j < len(d_h) - 1 and d_h[j] < d_h[j - 1] and d_h[j] < d_h[j + 1]

    for sg in sigs:
        D = sg["day"]
        sg["bottom_today"] = is_bottom(D)
        sg["bottom_today_or_next"] = is_bottom(D) or is_bottom(D + 1)
        for hrs in (24, 72):
            j = idx.searchsorted(sg["t"] + pd.Timedelta(hours=hrs))
            sg[f"ret_{hrs}h"] = 100 * (c_[j] - sg["price"]) / sg["price"] if j < len(idx) else np.nan
    return sigs


def report(df, title):
    if df.empty:
        print(f"{title:<34} -")
        return
    days = df.drop_duplicates(["symbol", "day"])
    r24, r72 = df["ret_24h"].dropna(), df["ret_72h"].dropna()
    print(f"{title:<34} {len(df):>5} {len(days):>5}  {100 * days['bottom_today'].mean():>5.0f}%"
          f" {100 * days['bottom_today_or_next'].mean():>6.0f}%   {100 * (r24 > 0).mean():>4.0f}% {r24.mean():+.2f}%"
          f"   {100 * (r72 > 0).mean():>4.0f}% {r72.mean():+.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="XAUUSD,BTCUSD")
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-11-14")
    a = ap.parse_args()
    rows = []
    for s in a.symbols.split(","):
        rows += scan(s, a.start, a.end)
    df = pd.DataFrame(rows)
    print(f"{a.start} → {a.end}   {a.symbols}\n")
    hdr = (f"{'':<34} {'sigs':>5} {'days':>5}  {'bottom':>6} {'b+next':>7}   {'up24h':>5} {'avg':>6}"
           f"   {'up72h':>5} {'avg':>6}")
    for src in ("M1 (48,104,36)", "M5 (12,26,9)", None):
        sub = df if src is None else df[df["src"] == src]
        print(f"=== {src or 'both sources'} ===")
        print(hdr)
        report(sub, "baseline: every bullish divergence")
        for n in (1, 2):
            report(sub[sub["k_today"] >= n], f"armed: {n} bar(s) below 0, incl. today")
            report(sub[sub["k_closed"] >= n], f"armed: {n} closed bar(s) below 0")
        print()
    print("bottom  = that daily bar became the daily (3,6,2) bottom")
    print("b+next  = that day or the next one did")
    print("up24h/up72h = share of signals with price higher 24h/72h later, and the average move")


if __name__ == "__main__":
    main()
