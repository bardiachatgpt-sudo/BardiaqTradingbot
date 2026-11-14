"""بک‌تست: همان موتور استراتژی ربات (bot/strategy.py) روی داده‌ی تاریخی.

اجرا:
    python -m backtest.run --start 2026-05-14 --end 2026-11-14
    python -m backtest.run --symbols BTCUSD,XAUUSD --start 2026-08-01 --end 2026-11-14

برای هر FIRE:
  - حرکت قیمت پس از 15 دقیقه، 1 ساعت و 4 ساعت (درصد، در جهت سیگنال)
  - معامله‌ی فرضی: ورود = قیمت FIRE، حد ضرر = قله/کف دوم واگرایی (R = فاصله‌ی ورود تا حد ضرر)
    نتیجه: کدام زودتر لمس شد، 1R / 2R یا حد ضرر (حداکثر 24 ساعت). اسپرد و کارمزد لحاظ نشده.
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

from bot.config import SYMBOLS
from bot.strategy import SymbolEngine
from bot.timefmt import session_at

from . import data

HORIZONS = [15, 60, 240]
TRADE_MAX_MIN = 24 * 60
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def run_engine(name, df):
    eng = SymbolEngine(name)
    fires, readies, cancels = [], 0, 0
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    for i, ts in enumerate(df.index):
        for ev in eng.on_bar(ts, o[i], h[i], l[i], c[i]):
            if ev["kind"] == "READY":
                readies += 1
            elif ev["kind"] == "INVALID":
                cancels += 1
            else:
                fires.append((i, ev))
    return fires, readies, cancels


def trade_outcome(s, entry, stop, hi, lo, start, stop_idx):
    """از کندل بعدی: کدام زودتر لمس شود؟ اگر در یک کندل هر دو لمس شوند، بدبینانه حد ضرر."""
    risk = s * (entry - stop)
    if risk <= 0:
        return {"risk_pct": np.nan, "r1": None, "r2": None}
    res = {"risk_pct": 100 * risk / entry}
    for k, target_r in (("r1", 1), ("r2", 2)):
        target = entry + s * target_r * risk
        out = "open"
        for j in range(start, stop_idx):
            hit_sl = lo[j] <= stop if s > 0 else hi[j] >= stop
            hit_tp = hi[j] >= target if s > 0 else lo[j] <= target
            if hit_sl:
                out = "loss"
                break
            if hit_tp:
                out = "win"
                break
        res[k] = out
    return res


def analyse(name, df, fires):
    idx = df.index
    c, hi, lo = df["Close"].to_numpy(), df["High"].to_numpy(), df["Low"].to_numpy()
    rows = []
    for i, ev in fires:
        s, entry, t = ev["s"], ev["price"], ev["time"]
        row = {"symbol": name, "time": t, "side": "BULL" if s > 0 else "BEAR",
               "strong": ev["strong"], "path": ev["path"], "session": session_at(t),
               "entry": entry}
        for m in HORIZONS:
            j = idx.searchsorted(t + pd.Timedelta(minutes=m - 1))   # کندلی که در t+m بسته می‌شود
            ok = j < len(idx) and idx[j] <= t + pd.Timedelta(minutes=m + 30)
            row[f"ret_{m}m"] = 100 * s * (c[j] - entry) / entry if ok else np.nan
        end = idx.searchsorted(t + pd.Timedelta(minutes=TRADE_MAX_MIN))
        row.update(trade_outcome(s, entry, ev["detail"]["second"][1], hi, lo, i + 1, end))
        rows.append(row)
    return rows


def summary(df):
    def block(g):
        out = {"fires": len(g)}
        for m in HORIZONS:
            r = g[f"ret_{m}m"].dropna()
            out[f"win_{m}m"] = f"{100 * (r > 0).mean():.0f}%" if len(r) else "-"
            out[f"avg_{m}m"] = f"{r.mean():+.3f}%" if len(r) else "-"
        for k in ("r1", "r2"):
            done = g[g[k].isin(["win", "loss"])]
            out[f"{k}_win"] = f"{100 * (done[k] == 'win').mean():.0f}%" if len(done) else "-"
        out["avg_risk"] = f"{g['risk_pct'].mean():.3f}%"
        return pd.Series(out)
    return block


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    a = ap.parse_args()
    end = pd.Timestamp(a.end or pd.Timestamp.utcnow().normalize().tz_localize(None))
    start = pd.Timestamp(a.start) if a.start else end - pd.DateOffset(months=6)

    all_rows, counts = [], []
    for name in a.symbols.split(","):
        t0 = time.time()
        df = data.candles(name, start, end)
        fires, readies, cancels = run_engine(name, df)
        all_rows += analyse(name, df, fires)
        counts.append({"symbol": name, "bars": len(df), "READY": readies,
                       "FIRE": len(fires), "CANCELLED": cancels})
        print(f"{name}: {len(df)} bars, {len(fires)} FIRE ({time.time() - t0:.0f}s)", file=sys.stderr)

    res = pd.DataFrame(all_rows)
    os.makedirs(OUT, exist_ok=True)
    res.to_csv(os.path.join(OUT, "fires.csv"), index=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    print(f"\nPeriod: {start:%Y-%m-%d} → {end:%Y-%m-%d}\n")
    print(pd.DataFrame(counts).to_string(index=False))
    if res.empty:
        return
    b = summary(res)
    for key in ("symbol", "side", "strong", "path", "session"):
        print(f"\n=== by {key} ===")
        print(res.groupby(key).apply(b, include_groups=False).to_string())
    print("\n=== all ===")
    print(b(res).to_string())


if __name__ == "__main__":
    main()
