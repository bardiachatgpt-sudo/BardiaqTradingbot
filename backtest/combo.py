"""دو بخش استراتژی با هم:

  بخش ۲ — برگشت روند: FIRE موتور ربات (واگرایی (48,104,36) ← READY ← FIRE).
          حد ضرر: پشت قله/کف دوم واگرایی + اسپرد. حد سود: 1R / 2R / قله‌ی میانی واگرایی (mid).
  بخش ۱ — داخل روند: (48,104,36) هم‌جهت و قدر مطلقش در حال زیاد شدن ← هر سقف (3,6,2) فروش (کف ← خرید).
          حد ضرر: بالای سقف قبلی (3,6,2) + اسپرد. حد سود: کف قبلی.
  جهت دیلی (اختیاری): قدم ۱ (step1.daily_bias)؛ می‌تواند روی هر دو بخش یا فقط بخش ۱ اعمال شود.
  هر بار فقط یک معامله روی هر نماد. ساعت معامله: 04–16 تهران (اختیاری).
"""
import numpy as np
import pandas as pd

from bot.strategy import SymbolEngine

from .research import macd, spread_for


def run(sym, df, parts=("trend", "rev"), rev_tp="2R", bias=None, bias_parts=("trend", "rev"),
        hours=(4, 16), win=3):
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    big, fast = macd(c, 48, 104), macd(c, 3, 6)
    hr = df.index.tz_convert("Asia/Tehran").hour.to_numpy()
    sp = spread_for(sym)
    eng = SymbolEngine(sym)
    peaks, troughs = [], []
    pos, trades = None, []
    for i, ts in enumerate(df.index):
        evs = eng.on_bar(ts, o[i], h[i], l[i], c[i])
        if pos is not None:
            s = pos["s"]
            hit_sl = h[i] >= pos["stop"] if s < 0 else l[i] <= pos["stop"]
            hit_tp = l[i] <= pos["target"] if s < 0 else h[i] >= pos["target"]
            if hit_sl or hit_tp:
                px = pos["stop"] if hit_sl else pos["target"]
                pos.update(exit_i=i, R=(s * (px - pos["entry"]) - sp) / pos["risk"])
                trades.append(pos)
                pos = None
        if i < 1500:
            continue
        k = i - 1
        is_peak = fast[k] > fast[k - 1] and fast[k] > fast[i]
        is_trough = fast[k] < fast[k - 1] and fast[k] < fast[i]
        ok_hour = hours is None or hours[0] <= hr[i] < hours[1]
        if pos is None and ok_hour:
            cand = []
            # بخش ۲: برگشت روند
            if "rev" in parts:
                for e in evs:
                    if e["kind"] != "FIRE":
                        continue
                    s = e["s"]
                    if bias is not None and "rev" in bias_parts and bias[i] != s:
                        continue
                    stop = e["detail"]["second"][1] - s * sp
                    risk = s * (c[i] - stop)
                    if risk <= 0:
                        continue
                    tgt = {"1R": c[i] + s * risk, "2R": c[i] + s * 2 * risk,
                           "mid": e["detail"]["mid"][1]}[rev_tp]
                    if s * (tgt - c[i]) > 0:
                        cand.append(("rev", s, stop, tgt, risk))
            # بخش ۱: داخل روند
            if "trend" in parts and not cand:
                for s, hit, same, opp in ((-1, is_peak, peaks, troughs), (1, is_trough, troughs, peaks)):
                    if not hit or not opp or not same:
                        continue
                    if bias is not None and "trend" in bias_parts and bias[i] != s:
                        continue
                    if not ((big[i] * s > 0) and abs(big[i]) > abs(big[i - 1])):
                        continue
                    ref = same[-1]
                    p_ = h[max(0, ref - win):ref + 2].max() + sp if s < 0 else l[max(0, ref - win):ref + 2].min() - sp
                    cur = h[max(0, k - win):i + 1].max() + sp if s < 0 else l[max(0, k - win):i + 1].min() - sp
                    stop = max(p_, cur) if s < 0 else min(p_, cur)
                    risk = s * (c[i] - stop)
                    p = opp[-1]
                    tgt = l[max(0, p - win):p + 2].min() if s < 0 else h[max(0, p - win):p + 2].max()
                    if risk > 0 and s * (tgt - c[i]) > 0:
                        cand.append(("trend", s, stop, tgt, risk))
            if cand:
                part, s, stop, tgt, risk = cand[0]
                pos = {"part": part, "s": s, "entry_i": i, "entry": c[i], "stop": stop, "target": tgt, "risk": risk}
        if is_peak:
            peaks.append(k)
        if is_trough:
            troughs.append(k)
    r = pd.DataFrame(trades)
    if not r.empty:
        r["t"] = df.index[r["entry_i"]]
    return r


def run_div(sym, df, entry="div", exit_on="div", hard_stop=True, bias=None, hours=None):
    """سیستم واگرایی‌به‌واگرایی روی (48,104,36):
    ورود با واگرایی (entry="div") یا FIRE (entry="fire")؛ خروج با واگرایی مخالف بعدی (exit_on="div")
    یا FIRE مخالف (exit_on="fire"). حد ضرر = پشت قله/کف دوم واگرایی (اندازه‌ی ریسک؛ با hard_stop واقعاً اجرا می‌شود).
    bias: فقط ورودهای هم‌جهت با دیلی (خروج همیشه با سیگنال مخالف)."""
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    hr = df.index.tz_convert("Asia/Tehran").hour.to_numpy()
    sp = spread_for(sym)
    eng = SymbolEngine(sym)
    seen = {1: None, -1: None}
    pos, trades = None, []

    def close(i, px, why):
        nonlocal pos
        s = pos["s"]
        pos.update(exit_i=i, why=why, R=(s * (px - pos["entry"]) - sp) / pos["risk"])
        trades.append(pos)
        pos = None

    for i, ts in enumerate(df.index):
        evs = eng.on_bar(ts, o[i], h[i], l[i], c[i])
        if pos is not None and hard_stop:
            s = pos["s"]
            if (l[i] <= pos["stop"]) if s > 0 else (h[i] >= pos["stop"]):
                close(i, pos["stop"], "stop")
        sig = {}                                   # s → قیمت قله/کف دوم
        if entry == "div" or exit_on == "div":
            for s in (1, -1):
                d = eng.det1[s].detail
                if d is not None and d is not seen[s]:
                    seen[s] = d
                    sig.setdefault(("div", s), s * d["second"][1])
        for e in evs:
            if e["kind"] == "FIRE":
                sig[("fire", e["s"])] = e["detail"]["second"][1]
        if i < 1500:
            continue
        if pos is not None and (exit_on, -pos["s"]) in sig:
            close(i, c[i], "opposite " + exit_on)
        if pos is None and (hours is None or hours[0] <= hr[i] < hours[1]):
            for s in (1, -1):
                if (entry, s) not in sig:
                    continue
                if bias is not None and bias[i] != s:
                    continue
                stop = sig[(entry, s)] - s * sp
                risk = s * (c[i] - stop)
                if risk <= 0:
                    continue
                pos = {"s": s, "entry_i": i, "entry": c[i], "stop": stop, "risk": risk}
                break
    r = pd.DataFrame(trades)
    if not r.empty:
        r["t"] = df.index[r["entry_i"]]
        r["hold_min"] = (r["exit_i"] - r["entry_i"])
    return r
