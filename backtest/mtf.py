"""بک‌تست نسخه‌ی اولیه‌ی استراتژی چندتایم‌فریمی (docs/strategy.md).

تعریف‌ها هنوز قطعی نیستند؛ فرض‌های این نسخه (v0):

  جهت (دیلی):
    - کندل دیلی مثل بروکرهای MT4: روز ساعت 17:00 نیویورک شروع می‌شود (سرور = نیویورک + 7 ساعت).
    - (3,6,2) دیلی با قیمت لحظه‌ای (کندل امروز هنوز باز) حساب می‌شود.
      سقف = دیروز بالاتر از پریروز و مقدار لحظه‌ای امروز پایین‌تر از دیروز ← جهت فروش.
      کف  = برعکس ← جهت خرید. جهت تا وقتی نقطه‌ی برگشت مخالف بیاید ثابت می‌ماند.
    - مستطیل: سقف = بالاترین High سه روز اخیر (برای خرید: پایین‌ترین Low)،
      ارتفاع = نصف میانگین دامنه‌ی ۱۴ روز.

  ورود اول:
    - واگرایی (48,104,36) روی H1 (= 12,26,9 روی H4) هم‌جهت، با قله/کف دوم داخل مستطیل،
      تأییدشده حداکثر ۴۸ ساعت قبل از تعیین جهت یا بعد از آن.
    - سپس اولین FIRE هم‌جهت موتور فعلی ربات روی M1، در حالی که قیمت در ۲ ساعت اخیر داخل مستطیل بوده.
    - برای هر جهت فقط یک بار.

  ورودهای بعدی:
    - (48,104,36) روی H4 در جهت معامله و قدر مطلقش در حال زیاد شدن (فاز روند)،
    - و (3,6,2) روی M5 یک سقف بالای صفر بدهد (برای خرید: کف زیر صفر).

  خروج:
    - همه‌ی معامله‌ها وقتی جهت دیلی برعکس شود. (هنوز حد ضرر نداریم؛ بیشترین ضرر شناور گزارش می‌شود.)

اجرا:
    python -m backtest.mtf --symbol XAUUSD --from 2026-11-03 --to 2026-11-14
"""
import argparse
from collections import deque

import numpy as np
import pandas as pd

from bot.indicators import MACDHist
from bot.strategy import DivDetector, SymbolEngine

from . import data

SERVER_SHIFT = pd.Timedelta(hours=7)   # زمان سرور MT4 = نیویورک + 7


def server_time(ts):
    return ts.tz_convert("America/New_York").tz_localize(None) + SERVER_SHIFT


class Bars:
    """تجمیع کندل M1 به تایم بالاتر بر اساس کلید زمان سرور؛ هنگام بسته شدن کندل، آن را برمی‌گرداند."""

    def __init__(self, key):
        self.key = key
        self.cur = None
        self.k = None

    def update(self, st, o, h, l, c):
        k = self.key(st)
        done = None
        if self.k is not None and k != self.k:
            done = self.cur
            self.cur = None
        if self.cur is None:
            self.cur = {"o": o, "h": h, "l": l, "c": c}
        else:
            self.cur["h"] = max(self.cur["h"], h)
            self.cur["l"] = min(self.cur["l"], l)
            self.cur["c"] = c
        self.k = k
        return done


def macd_peek(m, close):
    """مقدار MACD با قیمت لحظه‌ای، بدون ثبت (برای کندل در حال تشکیل)."""
    f = close if m.f.v is None else m.f.a * close + (1 - m.f.a) * m.f.v
    s = close if m.s.v is None else m.s.a * close + (1 - m.s.a) * m.s.v
    return f - s


def run(sym, start, end, warm_days=150, zero_rule="trend", mode="swing"):
    df = data.candles(sym, pd.Timestamp(start) - pd.Timedelta(days=warm_days), end)
    t_from = pd.Timestamp(start, tz="UTC")

    eng = SymbolEngine(sym)
    d_bars = Bars(lambda st: st.date())
    h4_bars = Bars(lambda st: (st.date(), st.hour // 4))
    h1_bars = Bars(lambda st: (st.date(), st.hour))
    m5_bars = Bars(lambda st: (st.date(), st.hour, st.minute // 5))

    d362 = MACDHist(3, 6, 2)
    d12 = MACDHist(12, 26, 9)
    d12_v = 0.0
    d_hist = deque(maxlen=2)                     # (3,6,2) دیلی دو روز بسته‌شده‌ی اخیر
    d_rng, d_hi, d_lo = deque(maxlen=14), deque(maxlen=3), deque(maxlen=3)
    h4m = MACDHist(48, 104, 36)
    h4_hist = deque(maxlen=2)
    h1m = MACDHist(48, 104, 36)
    h1_div = {1: DivDetector(), -1: DivDetector()}
    h1_n = 0
    h1_events = []                               # (زمان تأیید، جهت، قیمت نقطه‌ی دوم)
    m5m = MACDHist(3, 6, 2)
    m5_hist = deque(maxlen=3)
    recent = deque(maxlen=120)                   # (High, Low) دو ساعت اخیر M1

    bias, bias_t, zone, entry1_done = 0, None, None, False
    seen_div = {1: None, -1: None}               # آخرین واگرایی دیده‌شده‌ی (48,104,36) M1
    trades, open_tr, log = [], [], []

    def close_all(t, price, why):
        for tr in open_tr:
            tr.update(exit_t=t, exit=price, why=why, pnl=tr["s"] * (price - tr["entry"]))
            trades.append(tr)
        open_tr.clear()

    def enter(kind, t, price):
        open_tr.append({"kind": kind, "s": bias, "entry_t": t, "entry": price, "mae": 0.0})

    ts_all = df.index
    o_, h_, l_, c_ = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    for i, ts in enumerate(ts_all):
        o, h, l, c = o_[i], h_[i], l_[i], c_[i]
        t = ts + pd.Timedelta(minutes=1)           # زمان بسته شدن کندل M1
        st = server_time(ts)
        live = ts >= t_from

        # ---- تایم‌های بالاتر (فقط کندل‌های بسته‌شده)
        done = d_bars.update(st, o, h, l, c)
        if done:
            d_hist.append(d362.update(done["c"]))
            d12_v = d12.update(done["c"])
            d_rng.append(done["h"] - done["l"])
            d_hi.append(done["h"])
            d_lo.append(done["l"])
        done = h4_bars.update(st, o, h, l, c)
        if done:
            h4_hist.append(h4m.update(done["c"]))
        done = h1_bars.update(st, o, h, l, c)
        if done:
            hv = h1m.update(done["c"])
            h1_n += 1
            for s in (1, -1):
                pm = done["l"] if s > 0 else -done["h"]
                pk = done["h"] if s > 0 else -done["l"]
                if h1_div[s].update(s * hv, pm, pk, ts, h1_n > 300):
                    h1_events.append((t, s, s * h1_div[s].detail["second"][1]))
        done = m5_bars.update(st, o, h, l, c)
        m5_peak = 0
        if done:
            m5_hist.append(m5m.update(done["c"]))
            if len(m5_hist) == 3:
                a, b, x = m5_hist
                if b > a and b > x and b > 0:
                    m5_peak = -1                 # سقف ← سیگنال فروش
                elif b < a and b < x and b < 0:
                    m5_peak = 1                  # کف ← سیگنال خرید

        # ---- جهت دیلی با قیمت لحظه‌ای
        if len(d_hist) == 2 and len(d_rng) >= 5:
            now = macd_peek(d362, c)
            y2, y1 = d_hist
            # سقف بالای صفر / کف زیر صفر همیشه معتبر است. در روند شدید (به تشخیص 12,26,9 دیلی)
            # سقف زیر صفر (روند نزولی) و کف بالای صفر (روند صعودی) هم معتبرند.
            if zero_rule == "strict":
                top_ok, bot_ok = y1 > 0, y1 < 0
            elif zero_rule == "none":
                top_ok = bot_ok = True
            else:
                top_ok, bot_ok = (y1 > 0 or d12_v < 0), (y1 < 0 or d12_v > 0)
            turn = -1 if (y1 > y2 and now < y1 and top_ok) else (1 if (y1 < y2 and now > y1 and bot_ok) else 0)
            if turn and turn != bias:
                if open_tr:
                    close_all(t, c, "daily flip")
                bias, bias_t, entry1_done = turn, t, False
                adr = float(np.mean(d_rng))
                if bias < 0:
                    top = max(max(d_hi), h)
                    zone = (top - adr / 2, top)
                else:
                    bot = min(min(d_lo), l)
                    zone = (bot, bot + adr / 2)
                if live:
                    log.append(f"{t:%a %m-%d %H:%M}  daily (3,6,2) {'TOP → SELL' if bias < 0 else 'BOTTOM → BUY'}"
                               f"  zone {zone[0]:.1f}–{zone[1]:.1f}")

        recent.append((h, l))
        for tr in open_tr:
            tr["mae"] = min(tr["mae"], tr["s"] * ((l if tr["s"] > 0 else h) - tr["entry"]))

        # ---- موتور M1 فعلی ربات
        evs = eng.on_bar(ts, o, h, l, c)
        new_div = []
        for s in (1, -1):
            d = eng.det1[s].detail
            if d is not None and d is not seen_div[s]:
                seen_div[s] = d
                new_div.append(s)
        if not live or bias == 0:
            continue

        # خروج (mode=swing): واگرایی خلاف جهت روی (48,104,36) M1
        if mode == "swing" and open_tr and -bias in new_div:
            close_all(t, c, "M1 4x opposite divergence")

        # ورود اول
        if not entry1_done:
            def in_rect(p):
                return p >= zone[0] if bias < 0 else p <= zone[1]
            armed = any(s == bias and et >= bias_t - pd.Timedelta(hours=48) and in_rect(p2)
                        for et, s, p2 in h1_events)
            in_zone = (max(x for x, _ in recent) >= zone[0]) if bias < 0 else (min(y for _, y in recent) <= zone[1])
            fire = any(e["kind"] == "FIRE" and e["s"] == bias for e in evs)
            if armed and in_zone and fire:
                enter("ENTRY-1 (H1 div + M1 FIRE)", t, c)
                entry1_done = True

        # ورود دوباره (mode=swing): بعد از خروج، FIRE هم‌جهت بعدی روی M1، تا وقتی جهت دیلی عوض نشده
        elif mode == "swing" and not open_tr and any(e["kind"] == "FIRE" and e["s"] == bias for e in evs):
            enter("RE-ENTRY (M1 FIRE)", t, c)

        # ورودهای بعدی (mode=v0)
        if mode == "v0" and m5_peak == bias and len(h4_hist) == 2:
            p, q = h4_hist
            trend = (q * bias > 0) and abs(q) > abs(p)
            if trend:
                enter("ADD (H4 trend + M5 3,6,2)", t, c)

    if open_tr:
        close_all(ts_all[-1] + pd.Timedelta(minutes=1), c_[-1], "still open (end)")
    return trades, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--from", dest="start", default="2026-11-03")
    ap.add_argument("--to", dest="end", default="2026-11-14")
    ap.add_argument("--warm", type=int, default=150, help="روزهای گرم‌کردن اندیکاتورها قبل از --from")
    ap.add_argument("--zero", default="trend", choices=["trend", "strict", "none"],
                    help="شرط صفر برای سقف/کف (3,6,2) دیلی")
    ap.add_argument("--mode", default="swing", choices=["swing", "v0"],
                    help="swing: خروج با واگرایی مخالف M1 و ورود دوباره؛ v0: نگه داشتن تا برگشت دیلی + ADDها")
    a = ap.parse_args()
    trades, log = run(a.symbol, a.start, a.end, a.warm, a.zero, a.mode)
    teh = "Asia/Tehran"
    print(f"{a.symbol}  {a.start} → {a.end}\n")
    print("Daily direction changes (UTC):")
    print("\n".join("  " + x for x in log) or "  none")
    print(f"\nTrades: {len(trades)}")
    for tr in trades:
        side = "SELL" if tr["s"] < 0 else "BUY "
        print(f"  {side} {tr['kind']:<28} in {tr['entry_t'].tz_convert(teh):%a %m-%d %H:%M} @ {tr['entry']:.2f}"
              f"  out {tr['exit_t'].tz_convert(teh):%a %m-%d %H:%M} @ {tr['exit']:.2f}"
              f"  P&L {tr['pnl']:+.2f}  worst {tr['mae']:+.2f}  ({tr['why']})")
    if trades:
        pnl = np.array([t["pnl"] for t in trades])
        print(f"\nTotal P&L: {pnl.sum():+.2f} $/oz   wins {int((pnl > 0).sum())}/{len(pnl)}")


if __name__ == "__main__":
    main()
