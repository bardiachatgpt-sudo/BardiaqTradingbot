"""حلقه‌ی اسکن زنده: هر دقیقه کندل‌های جدید را به موتور هر نماد می‌دهد."""
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .config import INIT_BARS, POLL_BARS, SYMBOLS, WORKERS
from .data import get_closed_candles
from .messages import build_msg
from .strategy import SymbolEngine


def feed(eng, df, state, send, silent):
    """کندل‌های جدیدِ df را به موتور می‌دهد؛ FIREها همیشه ثبت و (اگر silent نباشد) همه‌ی رویدادها ارسال می‌شوند."""
    if eng.last_time is not None:
        df = df[df.index > eng.last_time]
    for ts, row in df.iterrows():
        for ev in eng.on_bar(ts, row["Open"], row["High"], row["Low"], row["Close"]):
            msg = build_msg(ev)
            if ev["kind"] == "FIRE":
                state.record_fire(ev["sym"], ev["s"], ev["time"], msg)
            if not silent:
                send(msg)


def run_scanner(state, send):
    engines = {n: SymbolEngine(n) for n in SYMBOLS}
    pool = ThreadPoolExecutor(max_workers=WORKERS)

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
                    feed(eng, df, state, send, silent=True)      # بازپخش بی‌صدا
                    eng.ready = True
                    print(f"[{name}] ready, bars={eng.n}")
                else:
                    feed(eng, df, state, send, silent=False)
        except Exception as e:
            print("scanner error:", e)
        time.sleep(60 - (time.time() % 60) + 4)
