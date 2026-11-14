"""دانلود و کش کندل‌های ۱ دقیقه‌ای تاریخی.

BTC  : آرشیو رسمی Binance   (data.binance.vision)
بقیه : آرشیو Dukascopy      (datafeed.dukascopy.com) — قیمت BID
"""
import io
import lzma
import os
import struct
import time
import urllib.error
import urllib.request
import zipfile

import pandas as pd

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
UA = {"User-Agent": "Mozilla/5.0 (backtest)"}

# ضریب قیمت در فایل‌های Dukascopy
DUKA_SCALE = {"XAUUSD": 1e3, "USDJPY": 1e3}
DEFAULT_SCALE = 1e5


def _get(url, tries=8):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(min(2 ** i, 30))
    raise RuntimeError(f"download failed: {url}")


def _cached(path, fetch):
    if os.path.exists(path):
        return pd.read_pickle(path)
    df = fetch()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_pickle(path)
    return df


# ---------------------------------------------------------------- Dukascopy
def _duka_day(sym, day):
    # ماه در آدرس Dukascopy از صفر شروع می‌شود
    url = (f"https://datafeed.dukascopy.com/datafeed/{sym}/{day.year}/{day.month - 1:02d}/"
           f"{day.day:02d}/BID_candles_min_1.bi5")
    raw = _get(url)
    cols = ["Open", "High", "Low", "Close"]
    if not raw:
        return pd.DataFrame(columns=cols, dtype=float)
    b = lzma.decompress(raw)
    scale = DUKA_SCALE.get(sym, DEFAULT_SCALE)
    rows, idx = [], []
    for i in range(len(b) // 24):
        sec, o, c, lo, hi, vol = struct.unpack(">5if", b[i * 24:(i + 1) * 24])
        if vol <= 0:                       # بازار بسته: کندل صاف بدون حجم
            continue
        idx.append(day + pd.Timedelta(seconds=sec))
        rows.append((o / scale, hi / scale, lo / scale, c / scale))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz="UTC"), columns=cols)


def dukascopy(sym, start, end, pause=0.3):
    days = pd.date_range(start, end, freq="D", tz="UTC", inclusive="left")
    parts = []
    for d in days:
        if d.weekday() == 5:               # شنبه: فارکس تعطیل
            continue
        path = os.path.join(CACHE, "duka", sym, f"{d:%Y-%m-%d}.pkl")
        fresh = not os.path.exists(path)
        if fresh and os.environ.get("BACKTEST_OFFLINE"):
            continue                       # فقط داده‌ی کش‌شده (روزهای جاافتاده رد می‌شوند)
        parts.append(_cached(path, lambda d=d: _duka_day(sym, d)))
        if fresh:
            time.sleep(pause)
    return pd.concat(parts).sort_index() if parts else None


# ---------------------------------------------------------------- Binance
def _binance_zip(url):
    raw = _get(url)
    if not raw:
        return None
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        df = pd.read_csv(z.open(z.namelist()[0]), header=None, usecols=[0, 1, 2, 3, 4])
    df.columns = ["t", "Open", "High", "Low", "Close"]
    if df["t"].iloc[0] > 1e14:             # از 2025 زمان‌ها میکروثانیه‌اند
        df.index = pd.to_datetime(df["t"], unit="us", utc=True)
    else:
        df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df[["Open", "High", "Low", "Close"]].astype(float)


def binance(sym, start, end):
    start, end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    base = "https://data.binance.vision/data/spot"
    parts = []
    m = start.replace(day=1)
    while m < end:
        nxt = m + pd.offsets.MonthBegin(1)
        path = os.path.join(CACHE, "binance", sym, f"{m:%Y-%m}.pkl")
        if os.path.exists(path):
            parts.append(pd.read_pickle(path))
        else:
            df = _binance_zip(f"{base}/monthly/klines/{sym}/1m/{sym}-1m-{m:%Y-%m}.zip")
            if df is not None:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                df.to_pickle(path)
                parts.append(df)
            else:                           # ماه جاری: فایل‌های روزانه
                for d in pd.date_range(m, min(nxt, end), freq="D", inclusive="left"):
                    p = os.path.join(CACHE, "binance", sym, f"{d:%Y-%m-%d}.pkl")
                    if os.path.exists(p):
                        parts.append(pd.read_pickle(p))
                        continue
                    day = _binance_zip(f"{base}/daily/klines/{sym}/1m/{sym}-1m-{d:%Y-%m-%d}.zip")
                    if day is not None:
                        os.makedirs(os.path.dirname(p), exist_ok=True)
                        day.to_pickle(p)
                        parts.append(day)
        m = nxt
    df = pd.concat(parts).sort_index()
    return df[(df.index >= start) & (df.index < end)]


def candles(name, start, end):
    """کندل‌های ۱ دقیقه‌ای برای نماد ربات (نام‌های bot/config.py)."""
    if name == "BTCUSD":
        return binance("BTCUSDT", start, end)
    return dukascopy(name, start, end)
