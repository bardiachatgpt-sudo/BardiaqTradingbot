"""پژوهش: در چه شرایطی قانون «فاز روند» درصد برد و نتیجه‌ی بهتری دارد؟

پایه: روی تایم پایه (پیش‌فرض M5)، وقتی (48,104,36) هم‌جهت است و قدر مطلقش زیاد می‌شود،
هر سقف (3,6,2) ← فروش (کف ← خرید). حد ضرر پشت آن سقف/کف + اسپرد.

برای هر سیگنال (بدون حذف هم‌پوشانی، هر سیگنال مستقل) ویژگی‌ها و نتیجه‌ها ثبت می‌شوند:
  نتیجه‌ها: هدف = کف/سقف قبلی، یا 1R / 1.5R / 2R ثابت (حداکثر یک روز). اسپرد کم می‌شود.
  ویژگی‌ها: وضعیت MACDهای تایم‌های بالاتر، شیب (3,6,2) دیلی، شماره‌ی سیگنال در فاز، ساعت، نوسان، ...

خروجی: backtest/results/research_<tf>.pkl
"""
import os
import sys

import numpy as np
import pandas as pd

from . import data

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
SPREAD = {"XAUUSD": 0.30, "BTCUSD": 5.0}
MAX_BARS_MIN = 24 * 60


def spread_for(sym):
    return SPREAD.get(sym, 0.01 if "JPY" in sym else 0.0001)


def ema(x, span):
    return pd.Series(x).ewm(span=span, adjust=False).mean().to_numpy()


def macd(x, f, s):
    return ema(x, f) - ema(x, s)


def resample(df, rule, server=False):
    idx = df.index
    if server:   # روز/H4 مثل MT4: سرور = نیویورک + 7
        idx = df.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    g = df.set_axis(idx).resample(rule)
    out = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return out


def htf_state(base_idx_server, htf, f, s):
    """مقدار MACD آخرین کندل بسته‌شده‌ی تایم بالا و مقدار قبلی‌اش، روی زمان‌های تایم پایه."""
    h = pd.Series(macd(htf["Close"].to_numpy(), f, s), index=htf.index)
    closed = h.shift(1)               # کندلی که از T شروع شده، در T+period بسته می‌شود
    prev = h.shift(2)
    a = closed.reindex(base_idx_server, method="ffill").to_numpy()
    b = prev.reindex(base_idx_server, method="ffill").to_numpy()
    return a, b


def daily_running(base_close, base_idx_server, daily, f, s):
    """(f,s) دیلی با قیمت لحظه‌ای (کندل امروز هنوز باز) + مقدار دیروز."""
    c = daily["Close"].to_numpy()
    ef = pd.Series(ema(c, f), index=daily.index).shift(1).reindex(base_idx_server, method="ffill").to_numpy()
    es = pd.Series(ema(c, s), index=daily.index).shift(1).reindex(base_idx_server, method="ffill").to_numpy()
    af, as_ = 2 / (f + 1), 2 / (s + 1)
    now = (af * base_close + (1 - af) * ef) - (as_ * base_close + (1 - as_) * es)
    y = pd.Series(macd(c, f, s), index=daily.index)
    y1 = y.shift(1).reindex(base_idx_server, method="ffill").to_numpy()
    y2 = y.shift(2).reindex(base_idx_server, method="ffill").to_numpy()
    return now, y1, y2


def collect(sym, m1, base="5min", base_min=5):
    df = m1 if base_min == 1 else resample(m1, base)
    st_idx = df.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    n = len(c)
    big, fast, mid = macd(c, 48, 104), macd(c, 3, 6), macd(c, 12, 26)
    tr = np.maximum(h - l, np.abs(h - np.roll(c, 1)))
    atr = pd.Series(tr).rolling(14).mean().to_numpy()
    sp = spread_for(sym)

    feats = {}
    for rule, name in (("15min", "m15"), ("1h", "h1"), ("4h", "h4")):
        if pd.Timedelta(rule) <= pd.Timedelta(base):
            continue
        htf = resample(m1, rule, server=True)
        for (f, s_), tag in (((12, 26), "def"), ((48, 104), "x4")):
            a, b = htf_state(st_idx, htf, f, s_)
            feats[f"{name}_{tag}"], feats[f"{name}_{tag}_prev"] = a, b
    daily = resample(m1, "1D", server=True)
    d_now, d_y1, d_y2 = daily_running(c, st_idx, daily, 3, 6)
    d12_now, d12_y1, _ = daily_running(c, st_idx, daily, 12, 26)
    adr = (daily["High"] - daily["Low"]).rolling(14).mean().shift(1).reindex(st_idx, method="ffill").to_numpy()

    max_bars = MAX_BARS_MIN // base_min
    rows = []
    last_trough = last_peak = None
    phase_start = {1: None, -1: None}
    nth = {1: 0, -1: 0}
    for i in range(300, n - 1):
        for s in (1, -1):
            if big[i] * s > 0 and big[i - 1] * s <= 0:
                phase_start[s], nth[s] = i, 0
        k = i - 1
        is_peak = fast[k] > fast[k - 1] and fast[k] > fast[i]
        is_trough = fast[k] < fast[k - 1] and fast[k] < fast[i]
        for s, hit, prev in ((-1, is_peak, last_trough), (1, is_trough, last_peak)):
            if not hit or prev is None:
                continue
            if not ((big[i] * s > 0) and abs(big[i]) > abs(big[i - 1])):
                continue
            nth[s] += 1
            entry = c[i]
            lo = max(0, k - 3)
            if s < 0:
                stop = h[lo:i + 1].max() + sp
                tgt = l[max(0, prev - 3):prev + 2].min()
            else:
                stop = l[lo:i + 1].min() - sp
                tgt = h[max(0, prev - 3):prev + 2].max()
            risk, reward = s * (entry - stop), s * (tgt - entry)
            if risk <= 0:
                continue
            # نتیجه‌ها
            j1 = min(n, i + 1 + max_bars)
            hh, ll, cc = h[i + 1:j1], l[i + 1:j1], c[i + 1:j1]
            if s < 0:
                sl_hit = np.flatnonzero(hh >= stop)
            else:
                sl_hit = np.flatnonzero(ll <= stop)
            sl_at = sl_hit[0] if len(sl_hit) else 10 ** 9
            res = {}
            for name, target in (("prev", tgt if reward > 0 else None),
                                 ("r1", entry + s * 1.0 * risk), ("r15", entry + s * 1.5 * risk),
                                 ("r2", entry + s * 2.0 * risk), ("r3", entry + s * 3.0 * risk)):
                if target is None:
                    res[name] = np.nan
                    continue
                tp_hit = np.flatnonzero(ll <= target) if s < 0 else np.flatnonzero(hh >= target)
                tp_at = tp_hit[0] if len(tp_hit) else 10 ** 9
                if tp_at < sl_at:
                    pnl = s * (target - entry)
                elif sl_at < 10 ** 9:
                    pnl = s * (stop - entry)            # برابر هم ← بدبینانه حد ضرر
                else:
                    pnl = s * (cc[-1] - entry) if len(cc) else 0.0
                res[name] = (pnl - sp) / risk
            row = {"sym": sym, "t": df.index[i], "s": s, "risk": risk, "risk_atr": risk / atr[i],
                   "reward_risk": reward / risk, "nth": nth[s],
                   "phase_age": i - phase_start[s] if phase_start[s] is not None else np.nan,
                   "fast_peak_side": s * fast[k] < 0,     # سقف بالای صفر (برای فروش) = اصلاح واقعی
                   "big_depth": abs(big[i]) / (pd.Series(np.abs(big[max(0, i - 500):i + 1])).max() + 1e-12),
                   "mid_with": s * mid[i] > 0,
                   "d362_now_slope": s * (d_now[i] - d_y1[i]) > 0,
                   "d362_closed_slope": s * (d_y1[i] - d_y2[i]) > 0,
                   "d362_with": s * d_now[i] > 0,
                   "d12_with": s * d12_now[i] > 0,
                   "d12_rising": s * (d12_now[i] - d12_y1[i]) > 0,
                   "risk_adr": risk / adr[i],
                   "hour": df.index[i].tz_convert("Asia/Tehran").hour,
                   "wday": df.index[i].tz_convert("Asia/Tehran").day_of_week}
            for key, val in feats.items():
                if key.endswith("_prev"):
                    continue
                v, pv = val[i], feats[key + "_prev"][i]
                row[f"{key}_with"] = s * v > 0
                row[f"{key}_trend"] = s * v > 0 and abs(v) > abs(pv)
                row[f"{key}_turning"] = s * (v - pv) > 0         # در جهت معامله حرکت می‌کند
            row.update({f"R_{k_}": v_ for k_, v_ in res.items()})
            rows.append(row)
        if is_peak:
            last_peak = k
        if is_trough:
            last_trough = k
    return pd.DataFrame(rows)


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "5min"
    syms = (sys.argv[2] if len(sys.argv) > 2 else "XAUUSD,BTCUSD,EURUSD,GBPUSD,USDCHF,USDJPY").split(",")
    base_min = int(pd.Timedelta(base).total_seconds() // 60)
    parts = []
    for sym in syms:
        m1 = data.candles(sym, "2026-05-14", "2026-11-14")
        d = collect(sym, m1, base, base_min)
        print(sym, len(d), file=sys.stderr)
        parts.append(d)
    out = pd.concat(parts, ignore_index=True)
    os.makedirs(OUT, exist_ok=True)
    out.to_pickle(os.path.join(OUT, f"research_{base}.pkl"))
    print("saved", len(out))


if __name__ == "__main__":
    main()
