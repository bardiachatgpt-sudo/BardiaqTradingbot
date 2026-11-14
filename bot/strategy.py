"""هسته‌ی استراتژی: تشخیص واگرایی، چرخه‌ی READY/FIRE و موتور هر نماد."""
from collections import deque

import pandas as pd

from .config import PRICE_WINDOW, READY_BARS, WARMUP
from .indicators import MACDHist


# ============================== لایه‌ی اول: تشخیص واگرایی ==============================
class DivDetector:
    """
    منطق صعودی روی هیستوگرام و قیمت آینه‌شده.
    مسیر نزولی با ضرب هیستوگرام در -1 اجرا می‌شود:
      p  = قیمت کف/قله (صعودی: Low ، نزولی: -High)
      pk = قیمت سقف/کف میانی (صعودی: High ، نزولی: -Low)  [فقط برای نمایش]
    """

    def __init__(self):
        self.h = deque(maxlen=12)
        self.p = deque(maxlen=12)
        self.pk = deque(maxlen=12)
        self.t = deque(maxlen=12)
        self.n = 0
        self.confirm_time = None
        self.detail = None
        self._clear()

    def _clear(self):
        self.active = False
        self.start = None
        self.first = None   # (hist, price, time)
        self.mid = None     # (hist, price, time)
        self.done = False

    def update(self, h, p, pk, ts, enabled):
        prev = self.h[-1] if self.h else None
        self.h.append(h)
        self.p.append(p)
        self.pk.append(pk)
        self.t.append(ts)
        idx = self.n
        self.n += 1
        if not enabled or prev is None:
            return False
        if h >= 0:                      # برگشت به مثبت: همه‌چیز پاک
            self._clear()
            return False
        if prev >= 0:                   # تغییر فاز مثبت به منفی: دوره‌ی جدید
            self._clear()
            self.active = True
            self.start = idx
        if not self.active or self.done or idx < 2:
            return False
        k = idx - 1
        if k < self.start:
            return False
        hk, hkk = self.h[-2], self.h[-3]
        tk = self.t[-2]
        is_trough = hk <= hkk and hk < h
        is_peak = hk >= hkk and hk > h
        w = PRICE_WINDOW + 2
        if is_peak:
            if self.first is not None and (self.mid is None or hk > self.mid[0]):
                self.mid = (hk, max(list(self.pk)[-w:-1]), tk)
        elif is_trough:
            price = min(list(self.p)[-w:-1])
            if self.first is None or hk < self.first[0]:
                self.first = (hk, price, tk)
                self.mid = None
            elif hk > self.first[0] and self.mid is not None and price < self.first[1]:
                self.done = True
                self.detail = {"first": self.first, "mid": self.mid,
                               "second": (hk, price, tk)}
                return True
        return False

# ============================== لایه‌ی دوم و سوم: چرخه ==============================
class Cycle:
    def __init__(self, s, div_time, detail):
        self.s = s
        self.div_time = div_time
        self.detail = detail
        self.stage = "WAIT_READY"
        self.ready_time = None
        self.ready_mode = None
        self.phase_bars = None
        self.alive = True

    def step(self, x2, pos2, x3h, now, pm, h1m, created=False):
        """
        x2   : هیستوگرام (12,26,9) آینه‌شده
        pos2 : تعداد میله‌ی پیاپی x2 > 0
        pm   : قیمت آینه‌شده‌ی کندل (صعودی: Low ، نزولی: -High)
        h1m  : هیستوگرام (48,104,36) آینه‌شده‌ی این کندل
        """
        # قانون ساختار: میله‌ای عمیق‌تر از کف/قله‌ی اول آمد، مرجع واگرایی باطل است
        if h1m < self.s * self.detail["first"][0]:
            self.alive = False
            if self.stage == "WAIT_FIRE":
                return [("INVALID", {"reason": "deeper"})]
            return []   # پیش از READY: بی‌صدا حذف می‌شود
        if self.stage == "WAIT_READY":
            if pos2 >= READY_BARS:
                self.stage = "WAIT_FIRE"
                self.ready_time = now
                self.ready_mode = "already" if created else "built"
                self.phase_bars = 1 if x3h[-1] < 0 else None
                return [("READY", {"pos2": pos2})]
            return []
        # WAIT_FIRE: ابطال‌ها مقدم‌اند
        if x2 < 0:
            self.alive = False
            return [("INVALID", {"reason": "hist"})]
        if pm < self.s * self.detail["second"][1]:     # قیمت از کف/قله‌ی دوم عبور کرد
            self.alive = False
            return [("INVALID", {"reason": "price"})]
        x3 = x3h[-1]
        if self.phase_bars is None:
            if x3 < 0:
                self.phase_bars = 1
            return []
        self.phase_bars += 1
        if x3 > 0:                                      # راه دوم
            self.alive = False
            return [("FIRE", {"path": 2})]
        if self.phase_bars >= 2 and len(x3h) >= 3 and x3 <= 0:
            hk, hkk = x3h[-2], x3h[-3]
            if hk <= hkk and abs(x3) < abs(hk):         # راه اول
                self.alive = False
                return [("FIRE", {"path": 1})]
        return []

# ============================== موتور هر نماد ==============================
class SymbolEngine:
    def __init__(self, name):
        self.name = name
        self.macd1 = MACDHist(48, 104, 36)
        self.macd2 = MACDHist(12, 26, 9)
        self.macd3 = MACDHist(3, 6, 2)
        self.macd5 = MACDHist(48, 104, 36)
        self.det1 = {1: DivDetector(), -1: DivDetector()}
        self.det5 = {1: DivDetector(), -1: DivDetector()}
        self.cycles = {1: [], -1: []}
        self.x3 = {1: deque(maxlen=3), -1: deque(maxlen=3)}
        self.pos2 = {1: 0, -1: 0}
        self.bucket = None
        self.n = 0
        self.last_time = None
        self.ready = False

    def _finish_bucket(self, enabled):
        b = self.bucket
        self.bucket = None
        h5 = self.macd5.update(b["c"])
        close5 = b["t"] + pd.Timedelta(minutes=5)
        for s in (1, -1):
            pm = b["lo"] if s > 0 else -b["hi"]
            pkm = b["hi"] if s > 0 else -b["lo"]
            if self.det5[s].update(s * h5, pm, pkm, b["t"], enabled):
                self.det5[s].confirm_time = close5

    def _mk(self, kind, info, cyc, now):
        ev = {"kind": kind, "sym": self.name, "s": cyc.s, "time": now,
              "div": cyc.div_time, "ready": cyc.ready_time, "mode": cyc.ready_mode,
              "detail": cyc.detail, "reason": info.get("reason"), "pos2": info.get("pos2")}
        if kind == "FIRE":
            d5 = self.det5[cyc.s]
            ev["path"] = info["path"]
            ev["strong"] = d5.done
            ev["m5_time"] = d5.confirm_time if d5.done else None
        return ev

    def on_bar(self, ts, op, hi, lo, cl):
        events = []
        close_t = ts + pd.Timedelta(minutes=1)
        enabled = self.n >= WARMUP

        # تجمیع M5 (فقط M5 کامل‌شده وارد می‌شود)
        b = ts.floor("5min")
        if self.bucket is not None and self.bucket["t"] != b:
            self._finish_bucket(enabled)
        if self.bucket is None:
            self.bucket = {"t": b, "hi": hi, "lo": lo, "c": cl}
        else:
            self.bucket["hi"] = max(self.bucket["hi"], hi)
            self.bucket["lo"] = min(self.bucket["lo"], lo)
            self.bucket["c"] = cl
        if ts.minute % 5 == 4:
            self._finish_bucket(enabled)

        # M1
        h1 = self.macd1.update(cl)
        h2 = self.macd2.update(cl)
        h3 = self.macd3.update(cl)
        for s in (1, -1):
            self.x3[s].append(s * h3)
            x2 = s * h2
            self.pos2[s] = self.pos2[s] + 1 if x2 > 0 else 0
            h1m = s * h1
            pm = lo if s > 0 else -hi
            pkm = hi if s > 0 else -lo
            for cyc in list(self.cycles[s]):
                for kind, info in cyc.step(x2, self.pos2[s], self.x3[s], close_t, pm, h1m):
                    events.append(self._mk(kind, info, cyc, close_t))
                if not cyc.alive:
                    self.cycles[s].remove(cyc)
            if self.det1[s].update(h1m, pm, pkm, ts, enabled):
                d = self.det1[s].detail
                detail = {k: (s * v[0], s * v[1], v[2]) for k, v in d.items()}
                cyc = Cycle(s, close_t, detail)
                for kind, info in cyc.step(x2, self.pos2[s], self.x3[s], close_t, pm, h1m, created=True):
                    events.append(self._mk(kind, info, cyc, close_t))
                if cyc.alive:
                    self.cycles[s].append(cyc)
        for ev in events:
            ev["price"] = cl          # قیمت لحظه‌ی رویداد = Close همین کندل
        self.n += 1
        self.last_time = ts
        return events
