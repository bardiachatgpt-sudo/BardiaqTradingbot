"""قانون «فاز روند M1»:

  فروش (خرید برعکس):
    شرط   : (48,104,36) M1 زیر صفر و قدر مطلقش در حال زیاد شدن (میله‌ی فعلی منفی‌تر از قبلی)
    ماشه  : (3,6,2) M1 سقف بدهد (میله‌ی قبلی بالاتر از هر دو کناری‌اش) ← فروش در بسته شدن همین کندل
    حد ضرر: بالاترین High اطراف آن سقف + اسپرد
    حد سود: کف قبلی (پایین‌ترین Low اطراف کف قبلیِ (3,6,2))
    اگر حد سود جلوتر از قیمت ورود نباشد، معامله گرفته نمی‌شود.
  اسپرد یک بار از سود هر معامله کم می‌شود. اگر حد ضرر و حد سود در یک کندل لمس شوند، بدبینانه حد ضرر.

اجرا:  python -m backtest.m1_trend --symbols XAUUSD,BTCUSD --start 2026-05-20 --end 2026-11-14
"""
import argparse

import numpy as np
import pandas as pd

from . import data

SPREAD = {"XAUUSD": 0.30, "BTCUSD": 5.0}
DEFAULT_SPREAD_PIPS = 1.0          # فارکس: ۱ پیپ


def spread_for(sym):
    if sym in SPREAD:
        return SPREAD[sym]
    return DEFAULT_SPREAD_PIPS * (0.01 if "JPY" in sym else 0.0001)


def macd(close, fast, slow):
    f = pd.Series(close).ewm(span=fast, adjust=False).mean()
    s = pd.Series(close).ewm(span=slow, adjust=False).mean()
    return (f - s).to_numpy()


def run(sym, df, win=3):
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    big = macd(c, 48, 104)
    fast = macd(c, 3, 6)
    sp = spread_for(sym)
    n = len(c)
    last_trough = last_peak = None          # اندیس آخرین کف/سقف (3,6,2)
    open_tr, trades = [], []
    for i in range(1500, n):
        # مدیریت معامله‌های باز روی این کندل
        for tr in list(open_tr):
            s = tr["s"]
            hit_sl = h[i] >= tr["stop"] if s < 0 else l[i] <= tr["stop"]
            hit_tp = l[i] <= tr["target"] if s < 0 else h[i] >= tr["target"]
            if hit_sl or hit_tp:
                px = tr["stop"] if hit_sl else tr["target"]
                tr["exit_i"], tr["result"] = i, ("loss" if hit_sl else "win")
                tr["pnl"] = s * (px - tr["entry"]) - sp
                trades.append(tr)
                open_tr.remove(tr)
        k = i - 1
        is_peak = fast[k] > fast[k - 1] and fast[k] > fast[i]
        is_trough = fast[k] < fast[k - 1] and fast[k] < fast[i]
        for s, hit, prev in ((-1, is_peak, last_trough), (1, is_trough, last_peak)):
            if not hit or prev is None:
                continue
            trend = (big[i] * s > 0) and abs(big[i]) > abs(big[i - 1])
            if not trend:
                continue
            entry = c[i]
            lo, hi = max(0, k - win), i + 1
            if s < 0:
                stop = h[lo:hi].max() + sp
                target = l[max(0, prev - win):prev + 2].min()
            else:
                stop = l[lo:hi].min() - sp
                target = h[max(0, prev - win):prev + 2].max()
            risk, reward = s * (entry - stop), s * (target - entry)
            if risk <= 0 or reward <= 0:
                continue
            open_tr.append({"s": s, "entry_i": i, "entry": entry, "stop": stop, "target": target,
                            "risk": risk, "reward": reward})
        if is_peak:
            last_peak = k
        if is_trough:
            last_trough = k
    res = pd.DataFrame(trades)
    if not res.empty:
        res["t"] = df.index[res["entry_i"]]
        res["R"] = res["pnl"] / res["risk"]
    return res


def summarize(name, r):
    if r.empty:
        print(f"{name:<14} no trades")
        return
    w = r["result"] == "win"
    print(f"{name:<14} {len(r):>6} {100 * w.mean():>5.0f}%  {r['reward'].median() / r['risk'].median():>5.2f}"
          f"  {r['risk'].median():>8.2f}  {r['R'].mean():>+6.3f}  {r['R'].sum():>+8.1f}  {r['pnl'].sum():>+10.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="XAUUSD,BTCUSD")
    ap.add_argument("--start", default="2026-05-20")
    ap.add_argument("--end", default="2026-11-14")
    a = ap.parse_args()
    print(f"{a.start} → {a.end}\n")
    print(f"{'':<14} {'trades':>6} {'win':>6}  {'R:R':>5}  {'risk':>8}  {'avg R':>6}  {'total R':>8}  {'total $':>10}")
    for sym in a.symbols.split(","):
        df = data.candles(sym, a.start, a.end)
        r = run(sym, df)
        summarize(sym, r)
        if not r.empty:
            for s, nm in ((-1, "  sells"), (1, "  buys")):
                summarize(nm, r[r["s"] == s])
            r["hour"] = r["t"].dt.tz_convert("Asia/Tehran").dt.hour
    print("\nR:R = median reward / median risk.  risk = median stop distance (price units).")
    print("avg R = average result per trade in units of risk, after spread.")


if __name__ == "__main__":
    main()
