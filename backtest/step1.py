"""قدم ۱ — جهت دیلی به روش بردیا:

  خرید (فروش برعکس):
    آماده‌باش : (3,6,2) دیلی N میله زیر صفر (میله‌ی امروزِ باز هم شمرده می‌شود)
    اعلام کف  : در حال آماده‌باش، واگرایی صعودی روی (48,104,36) M1 یا (12,26,9) M5
                ← از این لحظه جهت = خرید، تا وقتی «سقف» به همین روش اعلام شود.

daily_bias(m1, n) ← سری جهت (+1/-1/0) روی زمان‌های M1 (از لحظه‌ی بسته شدن کندل سیگنال)
"""
import numpy as np
import pandas as pd

from bot.indicators import MACDHist
from bot.strategy import DivDetector, SymbolEngine

from .mtf import Bars, macd_peek, server_time


def daily_calls(m1):
    """همه‌ی واگرایی‌های تایم پایین + وضعیت (3,6,2) دیلی در همان لحظه."""
    eng = SymbolEngine("X")
    d_bars = Bars(lambda st: st.date())
    m5_bars = Bars(lambda st: (st.date(), st.hour, st.minute // 5))
    d362 = MACDHist(3, 6, 2)
    d_h = []
    m5m = MACDHist(12, 26, 9)
    m5_div = {1: DivDetector(), -1: DivDetector()}
    m5_n = 0
    seen = {1: None, -1: None}
    out = []
    o_, h_, l_, c_ = (m1[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    for i, ts in enumerate(m1.index):
        o, h, l, c = o_[i], h_[i], l_[i], c_[i]
        st = server_time(ts)
        done = d_bars.update(st, o, h, l, c)
        if done:
            d_h.append(d362.update(done["c"]))
        sigs = []
        done = m5_bars.update(st, o, h, l, c)
        if done:
            m5_n += 1
            hv = m5m.update(done["c"])
            for s in (1, -1):
                pm = done["l"] if s > 0 else -done["h"]
                pk = done["h"] if s > 0 else -done["l"]
                if m5_div[s].update(s * hv, pm, pk, ts, m5_n > 300):
                    sigs.append((s, "M5"))
        eng.on_bar(ts, o, h, l, c)
        for s in (1, -1):
            d = eng.det1[s].detail
            if d is not None and d is not seen[s]:
                seen[s] = d
                sigs.append((s, "M1"))
        if not sigs or len(d_h) < 20:
            continue
        run = macd_peek(d362, c)
        for s, src in set(sigs):
            k = 1 if s * run < 0 else 0          # میله‌ی امروز در سمت «آماده‌باش»
            for v in reversed(d_h):
                if s * v >= 0:
                    break
                k += 1
            if k and s * run >= 0:               # امروز به سمت دیگر صفر رفته: شمارش قطع است
                k = 0
            out.append((ts + pd.Timedelta(minutes=1), s, k, src))
    return pd.DataFrame(out, columns=["t", "s", "armed_bars", "src"])


def daily_bias(calls, n, index, src=("M1", "M5")):
    """جهت روی index داده‌شده (زمان تصمیم). n = حداقل میله‌ی آماده‌باش. src = منبع واگرایی."""
    ev = calls[(calls["armed_bars"] >= n) & calls["src"].isin(src)]
    ev = ev[ev["s"] != ev["s"].shift(1)]        # فقط تغییر جهت
    if ev.empty:
        return np.zeros(len(index))
    s = pd.Series(ev["s"].to_numpy(), index=pd.DatetimeIndex(ev["t"]))
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(index, method="ffill").fillna(0).to_numpy()
