"""قانون «فاز روند» با حد ضرر بالای سقف قبلی (فروش؛ خرید برعکس):

  شرط   : (48,104,36) هم‌جهت و قدر مطلقش در حال زیاد شدن
  ماشه  : (3,6,2) سقف بدهد ← فروش در بسته شدن کندل تأیید
  حد ضرر: بالای سقف قبلیِ (3,6,2) (بالاترین High اطراف آن) + اسپرد      ← stop=prev
          (برای مقایسه: stop=cur = بالای همین سقف)
  حد سود: کف قبلی، یا 1R / 2R
  هر بار فقط یک معامله روی هر نماد. حساب: ۵۰۰ دلار، ریسک ۳٪ هر معامله (مرکب).

اجرا:  python -m backtest.prev_stop --tf 5min --symbols XAUUSD,BTCUSD
"""
import argparse

import numpy as np
import pandas as pd

from . import data
from .research import macd, resample, spread_for


def run(sym, df, stop_mode="prev", target_mode="prev", hours=None, win=3):
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    big, fast = macd(c, 48, 104), macd(c, 3, 6)
    hr = df.index.tz_convert("Asia/Tehran").hour.to_numpy()
    sp = spread_for(sym)
    n = len(c)
    peaks, troughs = [], []
    pos, trades = None, []
    for i in range(300, n):
        if pos is not None:
            s = pos["s"]
            hit_sl = h[i] >= pos["stop"] if s < 0 else l[i] <= pos["stop"]
            hit_tp = l[i] <= pos["target"] if s < 0 else h[i] >= pos["target"]
            if hit_sl or hit_tp:
                px = pos["stop"] if hit_sl else pos["target"]
                pos.update(exit_i=i, R=(s * (px - pos["entry"]) - sp) / pos["risk"])
                trades.append(pos)
                pos = None
        k = i - 1
        is_peak = fast[k] > fast[k - 1] and fast[k] > fast[i]
        is_trough = fast[k] < fast[k - 1] and fast[k] < fast[i]
        if pos is None and (hours is None or hours[0] <= hr[i] < hours[1]):
            for s, hit, same, opp in ((-1, is_peak, peaks, troughs), (1, is_trough, troughs, peaks)):
                if not hit or not opp or not same:
                    continue
                if not ((big[i] * s > 0) and abs(big[i]) > abs(big[i - 1])):
                    continue
                entry = c[i]
                ref = same[-1] if stop_mode == "prev" else k          # سقف قبلی یا همین سقف
                lo, hi_ = max(0, ref - win), (ref + 2 if stop_mode == "prev" else i + 1)
                stop = h[lo:hi_].max() + sp if s < 0 else l[lo:hi_].min() - sp
                if stop_mode == "prev":                               # حد ضرر نباید نزدیک‌تر از همین سقف باشد
                    cur = h[max(0, k - win):i + 1].max() + sp if s < 0 else l[max(0, k - win):i + 1].min() - sp
                    stop = max(stop, cur) if s < 0 else min(stop, cur)
                risk = s * (entry - stop)
                if risk <= 0:
                    continue
                p = opp[-1]
                prev_ext = l[max(0, p - win):p + 2].min() if s < 0 else h[max(0, p - win):p + 2].max()
                target = {"prev": prev_ext, "1R": entry + s * risk, "2R": entry + s * 2 * risk}[target_mode]
                if s * (target - entry) <= 0:
                    continue
                pos = {"s": s, "entry_i": i, "entry": entry, "stop": stop, "target": target, "risk": risk}
                break
        if is_peak:
            peaks.append(k)
        if is_trough:
            troughs.append(k)
    r = pd.DataFrame(trades)
    if not r.empty:
        r["t"] = df.index[r["entry_i"]]
    return r


def equity(R, start=500.0, risk=0.03):
    e, peak, mdd = start, start, 0.0
    for x in R:
        e *= 1 + risk * x
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    return e, mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5min")
    ap.add_argument("--symbols", default="XAUUSD,BTCUSD")
    ap.add_argument("--start", default="2026-05-14")
    ap.add_argument("--end", default="2026-11-14")
    a = ap.parse_args()
    print(f"{a.tf}  {a.start} → {a.end}   $500, 3% risk per trade, one trade at a time per symbol\n")
    print(f"{'':<34}{'trades':>7}{'win':>6}{'avg R':>8}{'final $':>12}{'max DD':>8}")
    for sym in a.symbols.split(","):
        m1 = data.candles(sym, a.start, a.end)
        df = m1 if a.tf == "1min" else resample(m1, a.tf)
        for stop in ("cur", "prev"):
            for tgt in ("prev", "1R", "2R"):
                for hrs, hl in ((None, ""), ((4, 16), " 04-16")):
                    r = run(sym, df, stop, tgt, hrs)
                    if r.empty:
                        continue
                    e, m = equity(r["R"])
                    lab = f"{sym} stop={stop:<4} tp={tgt:<4}{hl}"
                    print(f"{lab:<34}{len(r):>7}{100 * (r['R'] > 0).mean():>5.0f}%{r['R'].mean():>+8.3f}"
                          f"{e:>12,.0f}{100 * m:>7.0f}%")
        print()


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------- نسخه‌ی ۲: جهت دیلی + حد ضرر روی سقف تایم بالاتر
def htf_last_extreme(df_base, m1, rule, win=1):
    """برای هر کندل تایم پایه: High آخرین سقفِ تأییدشده‌ی (3,6,2) تایم بالاتر و Low آخرین کف آن."""
    htf = resample(m1, rule)
    hc, hh, hl = (htf[k].to_numpy() for k in ("Close", "High", "Low"))
    f = macd(hc, 3, 6)
    top = np.full(len(f), np.nan)
    bot = np.full(len(f), np.nan)
    lt = lb = np.nan
    for j in range(2, len(f)):
        k = j - 1
        if f[k] > f[k - 1] and f[k] > f[j]:
            lt = hh[max(0, k - win):j + 1].max()
        if f[k] < f[k - 1] and f[k] < f[j]:
            lb = hl[max(0, k - win):j + 1].min()
        top[j], bot[j] = lt, lb
    # مقدار کندل j وقتی معلوم است که کندل j بسته شده ← در زمان شروع کندل j+1
    period = pd.Timedelta(rule)
    idx = htf.index + period
    t = pd.Series(top, index=idx).reindex(df_base.index, method="ffill").to_numpy()
    b = pd.Series(bot, index=idx).reindex(df_base.index, method="ffill").to_numpy()
    return t, b


def daily_dir(df_base, m1, mode):
    """+1 = (3,6,2) دیلی رو به بالا (فقط خرید)، -1 = رو به پایین (فقط فروش)."""
    from .research import daily_running
    st_idx = df_base.index.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    daily = resample(m1, "1D", server=True)
    now, y1, y2 = daily_running(df_base["Close"].to_numpy(), st_idx, daily, 3, 6)
    return np.sign(now - y1) if mode == "run" else np.sign(y1 - y2)


def run2(sym, df, m1, stop_mode="prev", target_mode="1R", hours=(4, 16), bias=None, htf=None, win=3):
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    big, fast = macd(c, 48, 104), macd(c, 3, 6)
    hr = df.index.tz_convert("Asia/Tehran").hour.to_numpy()
    sp = spread_for(sym)
    if isinstance(bias, np.ndarray):           # جهت از پیش محاسبه‌شده (مثلاً قدم ۱)
        dd = bias
    else:
        dd = daily_dir(df, m1, bias) if bias else None
    ht, hb = htf_last_extreme(df, m1, htf) if stop_mode == "htf" else (None, None)
    n = len(c)
    peaks, troughs = [], []
    pos, trades = None, []
    for i in range(300, n):
        if pos is not None:
            s = pos["s"]
            hit_sl = h[i] >= pos["stop"] if s < 0 else l[i] <= pos["stop"]
            hit_tp = l[i] <= pos["target"] if s < 0 else h[i] >= pos["target"]
            if hit_sl or hit_tp:
                px = pos["stop"] if hit_sl else pos["target"]
                pos.update(exit_i=i, R=(s * (px - pos["entry"]) - sp) / pos["risk"])
                trades.append(pos)
                pos = None
        k = i - 1
        is_peak = fast[k] > fast[k - 1] and fast[k] > fast[i]
        is_trough = fast[k] < fast[k - 1] and fast[k] < fast[i]
        if pos is None and (hours is None or hours[0] <= hr[i] < hours[1]):
            for s, hit, same, opp in ((-1, is_peak, peaks, troughs), (1, is_trough, troughs, peaks)):
                if not hit or not opp or not same:
                    continue
                if dd is not None and dd[i] != s:
                    continue
                if not ((big[i] * s > 0) and abs(big[i]) > abs(big[i - 1])):
                    continue
                entry = c[i]
                cur = h[max(0, k - win):i + 1].max() + sp if s < 0 else l[max(0, k - win):i + 1].min() - sp
                if stop_mode == "cur":
                    stop = cur
                elif stop_mode == "prev":
                    ref = same[-1]
                    p_ = h[max(0, ref - win):ref + 2].max() + sp if s < 0 else l[max(0, ref - win):ref + 2].min() - sp
                    stop = max(p_, cur) if s < 0 else min(p_, cur)
                else:   # htf: آخرین سقف/کف (3,6,2) تایم بالاتر
                    x = ht[i] if s < 0 else hb[i]
                    if np.isnan(x):
                        continue
                    stop = max(x + sp, cur) if s < 0 else min(x - sp, cur)
                risk = s * (entry - stop)
                if risk <= 0:
                    continue
                p = opp[-1]
                prev_ext = l[max(0, p - win):p + 2].min() if s < 0 else h[max(0, p - win):p + 2].max()
                target = {"prev": prev_ext, "1R": entry + s * risk, "2R": entry + s * 2 * risk}[target_mode]
                if s * (target - entry) <= 0:
                    continue
                pos = {"s": s, "entry_i": i, "entry": entry, "stop": stop, "target": target, "risk": risk}
                break
        if is_peak:
            peaks.append(k)
        if is_trough:
            troughs.append(k)
    r = pd.DataFrame(trades)
    if not r.empty:
        r["t"] = df.index[r["entry_i"]]
    return r
