import os
import re
import json
import time
import random
import string
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytz
import telebot
import websocket
from flask import Flask
from telebot import types

# ============================== تنظیمات ==============================
TEHRAN = pytz.timezone("Asia/Tehran")
PRICE_WINDOW = 7        # تعداد کندل قبل از کندل کف/قله برای قیمت (قابل‌تنظیم)
WARMUP = 1000           # کندل‌های گرم‌شدن اندیکاتور (بدون تشخیص)
INIT_BARS = 2500        # تاریخچه‌ی بازپخش هنگام راه‌اندازی
POLL_BARS = 60          # تعداد کندل در هر دور بررسی زنده
STATE_FILE = "bot_state.json"

def _chain(base):
    return [f"OANDA:{base}", f"FOREXCOM:{base}", f"FX:{base}"]

SYMBOLS = {
    "XAUUSD": ["OANDA:XAUUSD", "FOREXCOM:XAUUSD", "FX_IDC:XAUUSD"],
    "EURUSD": _chain("EURUSD"),
    "GBPUSD": _chain("GBPUSD"),
    "USDJPY": _chain("USDJPY"),
    "USDCHF": _chain("USDCHF"),
    "USDCAD": _chain("USDCAD"),
    "AUDUSD": _chain("AUDUSD"),
    "NZDUSD": _chain("NZDUSD"),
}

# ============================== حالت و ذخیره ==============================
_lock = threading.Lock()
users = set()
last_fire = {}   # {symbol: {"t": iso_utc, "d": +1/-1}}


def save_state():
    try:
        with _lock:
            with open(STATE_FILE, "w") as f:
                json.dump({"users": list(users), "last_fire": last_fire}, f)
    except Exception as e:
        print("save_state error:", e)


def load_state():
    global users, last_fire
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                d = json.load(f)
            users = set(d.get("users", []))
            last_fire = d.get("last_fire", {})
    except Exception as e:
        print("load_state error:", e)


def record_fire(name, s, ts):
    cur = last_fire.get(name)
    if cur is None or ts > pd.Timestamp(cur["t"]):
        last_fire[name] = {"t": ts.isoformat(), "d": s}
        save_state()


load_state()

# ============================== زمان ==============================
def to_tehran(ts):
    return ts.tz_convert(TEHRAN).strftime("%Y-%m-%d %H:%M")


def fmt_gap(td):
    m = int(td.total_seconds() // 60)
    h, m = divmod(m, 60)
    return f"{h}س {m}د" if h else f"{m}د"

# ============================== دریافت قیمت (وب‌سوکت TradingView) ==============================
def _send(ws, func, params):
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    ws.send(f"~m~{len(payload)}~m~{payload}")


def fetch_candles(tv_symbol, interval="1", n_bars=1000, timeout=20):
    ws = None
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            headers={
                "Origin": "https://www.tradingview.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            timeout=12,
        )
        cs = "cs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        _send(ws, "set_auth_token", ["unauthorized_user_token"])
        _send(ws, "chart_create_session", [cs, ""])
        sym = json.dumps({"symbol": tv_symbol, "adjustment": "splits"}, separators=(",", ":"))
        _send(ws, "resolve_symbol", [cs, "sds_sym_1", f"={sym}"])
        _send(ws, "create_series", [cs, "sds_1", "s1", "sds_sym_1", str(interval), n_bars, ""])

        bars = {}
        t0 = time.time()
        done = False
        while time.time() - t0 < timeout and not done:
            raw = ws.recv()
            if not raw:
                break
            if re.fullmatch(r"~m~\d+~m~~h~\d+", raw):
                ws.send(raw)
                continue
            for part in re.split(r"~m~\d+~m~", raw):
                if not part.strip() or part.startswith("~h~"):
                    continue
                try:
                    msg = json.loads(part)
                except Exception:
                    continue
                m = msg.get("m")
                if m in ("timescale_update", "du"):
                    p = msg.get("p", [])
                    if len(p) >= 2 and isinstance(p[1], dict):
                        for item in p[1].get("sds_1", {}).get("s", []):
                            v = item.get("v", [])
                            if len(v) >= 5:
                                bars[int(v[0])] = (float(v[1]), float(v[2]), float(v[3]), float(v[4]))
                elif m == "series_completed":
                    done = True
        ws.close()
        if not bars:
            return None, f"{tv_symbol}: no bars"
        df = pd.DataFrame.from_dict(
            bars, orient="index", columns=["Open", "High", "Low", "Close"]
        ).sort_index()
        df.index = pd.to_datetime(df.index, unit="s", utc=True)
        return df, None
    except Exception as e:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        return None, f"{tv_symbol}: {str(e)[:80]}"


def get_closed_candles(name, n_bars, min_bars):
    """فقط کندل‌های بسته‌شده؛ ردیف آخر (کندل در حال تشکیل) حذف می‌شود."""
    errors = []
    for tv_symbol in SYMBOLS[name]:
        df, err = fetch_candles(tv_symbol, "1", n_bars)
        if df is not None and len(df) > min_bars:
            return df.iloc[:-1]
        errors.append(err or f"{tv_symbol}: too few bars")
    print(f"[{name}] fetch failed: {' | '.join(errors)}")
    return None

# ============================== اندیکاتور ==============================
class EMA:
    def __init__(self, span):
        self.a = 2.0 / (span + 1)
        self.v = None

    def update(self, x):
        self.v = x if self.v is None else self.a * x + (1 - self.a) * self.v
        return self.v


class MACDHist:
    def __init__(self, fast, slow, sig):
        self.f, self.s, self.g = EMA(fast), EMA(slow), EMA(sig)

    def update(self, close):
        m = self.f.update(close) - self.s.update(close)
        return m - self.g.update(m)

# ============================== لایه‌ی اول: تشخیص واگرایی ==============================
class DivDetector:
    """
    منطق صعودی روی هیستوگرام و قیمت آینه‌شده.
    مسیر نزولی با ضرب هیستوگرام در -1 و استفاده از -High به‌جای Low اجرا می‌شود.
    """

    def __init__(self):
        self.h = deque(maxlen=12)
        self.p = deque(maxlen=12)
        self.n = 0
        self.confirm_time = None
        self._clear()

    def _clear(self):
        self.active = False
        self.start = None
        self.first = None   # (hist, price)
        self.mid = None     # hist
        self.done = False

    def update(self, h, p, enabled):
        prev = self.h[-1] if self.h else None
        self.h.append(h)
        self.p.append(p)
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
        is_trough = hk <= hkk and hk < h
        is_peak = hk >= hkk and hk > h
        if is_peak:
            if self.first is not None and (self.mid is None or hk > self.mid):
                self.mid = hk
        elif is_trough:
            price = min(list(self.p)[-(PRICE_WINDOW + 2):-1])
            if self.first is None or hk < self.first[0]:
                self.first = (hk, price)
                self.mid = None
            elif hk > self.first[0] and self.mid is not None and price < self.first[1]:
                self.done = True
                return True
        return False

# ============================== لایه‌ی دوم و سوم: چرخه ==============================
class Cycle:
    def __init__(self, s, div_time):
        self.s = s
        self.div_time = div_time
        self.stage = "WAIT_READY"
        self.ready_time = None
        self.ready_mode = None
        self.phase_bars = None
        self.alive = True

    def step(self, x2, x3h, now, created=False):
        if self.stage == "WAIT_READY":
            if x2 > 0:
                self.stage = "WAIT_FIRE"
                self.ready_time = now
                self.ready_mode = "already" if created else "cross"
                self.phase_bars = 1 if x3h[-1] < 0 else None
                return [("READY", {})]
            return []
        # WAIT_FIRE: ابطال مقدم است
        if x2 < 0:
            self.alive = False
            return [("INVALID", {})]
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
            if self.det5[s].update(s * h5, pm, enabled):
                self.det5[s].confirm_time = close5

    def _mk(self, kind, info, cyc, now):
        ev = {"kind": kind, "sym": self.name, "s": cyc.s, "time": now,
              "div": cyc.div_time, "ready": cyc.ready_time, "mode": cyc.ready_mode}
        if kind == "FIRE":
            d5 = self.det5[cyc.s]
            ev["path"] = info["path"]
            ev["strong"] = d5.done
            ev["m5_time"] = d5.confirm_time if d5.done else None
            record_fire(self.name, cyc.s, now)
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
            for cyc in list(self.cycles[s]):
                for kind, info in cyc.step(x2, self.x3[s], close_t):
                    events.append(self._mk(kind, info, cyc, close_t))
                if not cyc.alive:
                    self.cycles[s].remove(cyc)
            pm = lo if s > 0 else -hi
            if self.det1[s].update(s * h1, pm, enabled):
                cyc = Cycle(s, close_t)
                for kind, info in cyc.step(x2, self.x3[s], close_t, created=True):
                    events.append(self._mk(kind, info, cyc, close_t))
                if cyc.alive:
                    self.cycles[s].append(cyc)
        self.n += 1
        self.last_time = ts
        return events

# ============================== پیام‌ها ==============================
def build_msg(ev):
    s = ev["s"]
    side = "صعودی" if s > 0 else "نزولی"
    icon = "🟢" if s > 0 else "🔴"
    pos = "مثبت" if s > 0 else "منفی"
    sym = ev["sym"]
    if ev["kind"] == "READY":
        mode = f"از قبل {pos} بود" if ev["mode"] == "already" else f"عبور به {pos}"
        return (f"{icon} READY {side} ـ {sym}\n"
                f"واگرایی M1: {to_tehran(ev['div'])}\n"
                f"READY: {to_tehran(ev['time'])} ({mode})")
    if ev["kind"] == "INVALID":
        return (f"⚪ ابطال {side} ـ {sym}\n"
                f"واگرایی M1: {to_tehran(ev['div'])}\n"
                f"READY: {to_tehran(ev['ready'])}\n"
                f"ابطال: {to_tehran(ev['time'])}")
    # FIRE
    if ev["path"] == 1:
        path = "تأیید اولین دره" if s > 0 else "تأیید اولین قله"
    else:
        path = f"برگشت به {pos}"
    strong = "  ⭐ قوی" if ev["strong"] else ""
    txt = (f"🔥 FIRE {side} ـ {sym}{strong}\n"
           f"FIRE: {to_tehran(ev['time'])}\n"
           f"راه صدور: {ev['path']} ({path})\n"
           f"واگرایی M1: {to_tehran(ev['div'])}\n"
           f"فاصله‌ی واگرایی تا FIRE: {fmt_gap(ev['time'] - ev['div'])}")
    if ev["strong"] and ev["m5_time"] is not None:
        txt += f"\nواگرایی M5: {to_tehran(ev['m5_time'])}"
    return txt

# ============================== تلگرام ==============================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None


def broadcast(text):
    if not bot:
        print(text)
        return
    for chat_id in list(users):
        try:
            bot.send_message(chat_id, text)
        except Exception as e:
            print("telegram send error:", e)


def keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(n, callback_data="last:" + n) for n in SYMBOLS])
    return kb


def last_fire_text(name):
    r = last_fire.get(name)
    if not r:
        return f"{name}: FIRE ثبت نشده"
    side = "صعودی" if r["d"] > 0 else "نزولی"
    return f"{name}: {to_tehran(pd.Timestamp(r['t']))} ({side})"


if bot:
    @bot.message_handler(commands=["start", "last"])
    def on_start(m):
        users.add(m.chat.id)
        save_state()
        bot.send_message(m.chat.id,
                         "ثبت‌نام شد. الرت READY و FIRE می‌آید.\n"
                         "برای دیدن زمان آخرین FIRE، یک نماد را بزنید:",
                         reply_markup=keyboard())

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("last:"))
    def on_last(c):
        name = c.data.split(":", 1)[1]
        bot.answer_callback_query(c.id)
        if name in SYMBOLS:
            bot.send_message(c.message.chat.id, last_fire_text(name))

# ============================== اسکنر ==============================
def feed(eng, df, silent):
    if eng.last_time is not None:
        df = df[df.index > eng.last_time]
    for ts, row in df.iterrows():
        evs = eng.on_bar(ts, row["Open"], row["High"], row["Low"], row["Close"])
        if not silent:
            for ev in evs:
                broadcast(build_msg(ev))


def run_scanner():
    engines = {n: SymbolEngine(n) for n in SYMBOLS}
    pool = ThreadPoolExecutor(max_workers=4)

    def poll(name):
        eng = engines[name]
        if not eng.ready:
            return name, get_closed_candles(name, INIT_BARS, 1200)
        df = get_closed_candles(name, POLL_BARS, 20)
        if df is not None and df.index[0] > eng.last_time + pd.Timedelta(minutes=1):
            big = get_closed_candles(name, INIT_BARS, 1200)   # شکاف: دریافت بزرگ
            if big is not None:
                df = big
        return name, df

    while True:
        try:
            for name, df in pool.map(poll, list(SYMBOLS)):
                if df is None:
                    continue
                eng = engines[name]
                if not eng.ready:
                    feed(eng, df, silent=True)      # بازپخش بی‌صدا
                    eng.ready = True
                    print(f"[{name}] ready, bars={eng.n}")
                else:
                    feed(eng, df, silent=False)
        except Exception as e:
            print("scanner error:", e)
        time.sleep(60 - (time.time() % 60) + 4)

# ============================== وب‌سرور Render و اجرا ==============================
app = Flask(__name__)


@app.route("/")
def home():
    return "MACD divergence scanner is live"


def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=run_scanner, daemon=True).start()
    if bot:
        bot.infinity_polling(skip_pending=True)
    else:
        while True:
            time.sleep(60)
