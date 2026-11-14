"""Al Brooks price action — book 1 (Trends), chapters 1-2, coded.

Separate project: depends only on pandas/numpy and an OHLC DataFrame
(columns Open, High, Low, Close). Nothing here imports the MACD bot.

Concepts (summarised in our own words; thresholds are tunable, see PARAMS):

  trend bar vs doji   Every bar is either a trend bar (one side controlled it, it has a
                      real body) or a doji (small body relative to the bar and to recent
                      bodies: a one-bar trading range). "Close is close enough."
  trend bar strength  Body at least the median body of recent bars, close near its
                      extreme, open near the other extreme, close beyond prior closes and
                      the prior bar's extreme, small tails.
  climax              Consecutive trend bars in one direction; it ends at the first pause
                      bar (doji, inside bar, opposite trend bar, or a bar with a prominent
                      tail against the move).
  climactic reversal  A climax followed soon by a strong trend bar the other way.
  H1/H2/... L1/L2/... Leg counting in pullbacks: in a down leg, a bar whose high goes
                      above the prior bar's high is H1; after another down leg, the next
                      such bar is H2, and so on. The count resets when the pullback is
                      over (price exceeds the high the pullback started from). L is the
                      mirror image.
  buying/selling      Cumulative signs inside a window: bull trend bars, lower tails, bull
  pressure            reversal bars (and the mirror for selling pressure).
  trending dojis      Several dojis in a row with higher closes, highs and lows (or lower):
                      a trend, not a range.
"""
import numpy as np
import pandas as pd

PARAMS = {
    "doji_body_frac": 0.25,     # body / range below this -> doji
    "doji_body_vs_median": 0.5,  # or body below this x median recent body -> doji
    "median_window": 10,        # bars for the "recent median body"
    "extreme_frac": 1 / 3,      # "near the high/low" = within this fraction of the range
    "small_tail_frac": 0.2,     # tail below this fraction of the range = small
    "pause_tail_frac": 0.4,     # tail against the move above this = prominent (pause)
    "climax_min_bars": 3,       # consecutive trend bars that make a climax
    "reversal_within": 5,       # bars after a climax to look for the opposite strong bar
    "strong_score": 3,          # strength score (0-5) for a "strong" trend bar
    "pressure_window": 20,
    "trending_doji_min": 3,
}


def classify(df, p=PARAMS):
    """Adds per-bar columns: body, range, direction (+1 bull / -1 bear / 0 doji),
    is_doji, strength (0-5, trend bars only), strong."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    rng = h - l
    body = np.abs(c - o)
    med = pd.Series(body).shift(1).rolling(p["median_window"], min_periods=3).median().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(rng > 0, body / rng, 0.0)
    is_doji = (frac < p["doji_body_frac"]) | (body < p["doji_body_vs_median"] * np.nan_to_num(med, nan=0.0))
    is_doji |= rng <= 0
    direction = np.where(is_doji, 0, np.sign(c - o)).astype(int)

    ex = p["extreme_frac"]
    strength = np.zeros(len(c), dtype=int)
    for i in range(len(c)):
        d = direction[i]
        if d == 0 or rng[i] <= 0:
            continue
        s = 0
        if not np.isnan(med[i]) and body[i] >= med[i]:
            s += 1
        if d > 0:
            s += c[i] >= h[i] - ex * rng[i]                     # close near the high
            s += o[i] <= l[i] + ex * rng[i]                     # open near the low
            s += i > 0 and c[i] > c[i - 1] and c[i] > h[i - 1]  # close above prior close and high
            s += (h[i] - c[i]) <= p["small_tail_frac"] * rng[i] and (o[i] - l[i]) <= p["small_tail_frac"] * rng[i]
        else:
            s += c[i] <= l[i] + ex * rng[i]
            s += o[i] >= h[i] - ex * rng[i]
            s += i > 0 and c[i] < c[i - 1] and c[i] < l[i - 1]
            s += (c[i] - l[i]) <= p["small_tail_frac"] * rng[i] and (h[i] - o[i]) <= p["small_tail_frac"] * rng[i]
        strength[i] = s
    out = df.copy()
    out["body"], out["range"] = body, rng
    out["direction"], out["is_doji"] = direction, is_doji
    out["strength"] = strength
    out["strong"] = strength >= p["strong_score"]
    return out


def is_pause(df, i, move):
    """Pause bar after a move (+1 up / -1 down): doji, inside bar, opposite trend bar,
    or a prominent tail against the move."""
    if df["is_doji"].iat[i] or df["direction"].iat[i] == -move:
        return True
    h, l, o, c = (df[k].iat[i] for k in ("High", "Low", "Open", "Close"))
    if i > 0 and h <= df["High"].iat[i - 1] and l >= df["Low"].iat[i - 1]:
        return True                                   # inside bar
    rng = h - l
    tail = (h - max(o, c)) if move > 0 else (min(o, c) - l)
    return rng > 0 and tail >= PARAMS["pause_tail_frac"] * rng


def climaxes(df, p=PARAMS):
    """List of climaxes: dict(start, end, pause, direction, bars, reversal_bar or None)."""
    d = df["direction"].to_numpy()
    out = []
    i, n = 0, len(df)
    while i < n:
        if d[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and d[j + 1] == d[i]:
            j += 1
        run = j - i + 1
        if run >= p["climax_min_bars"] and j + 1 < n and is_pause(df, j + 1, d[i]):
            rev = None
            for k in range(j + 1, min(n, j + 1 + p["reversal_within"])):
                if d[k] == -d[i] and df["strong"].iat[k]:
                    rev = k
                    break
            out.append({"start": i, "end": j, "pause": j + 1, "direction": int(d[i]),
                        "bars": run, "reversal_bar": rev})
        i = j + 1
    return out


def leg_counts(df):
    """Brooks bar counting. Returns a Series of labels ('H1','H2',... / 'L1','L2',... / '').

    H counting (pullback down in a bull move or a range):
      - a pullback starts at the first bar with a lower high or lower low; the high it
        started from (the swing high of the move) is remembered;
      - inside the pullback, a bar whose high goes above the prior bar's high gets the
        next H number, and a new down leg must start before the next one;
      - the count resets when price goes above the high the pullback started from
        (the pullback is over, the move made a new high);
      - after an H2 (or later) fails, i.e. price falls below the low of the leg that
        produced it, counting starts over (so a bear trend shows H1, H2, H1, H2, ...);
      - if the swing high after the last H bar is higher than the swing high after the
        H bar before it, the market is making higher highs (a bull leg, not a pullback),
        so the new H becomes H1 again.
    L counting is the mirror image.
    """
    h, l = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    n = len(h)
    lab = [[] for _ in range(n)]

    def run(hi, lo, tag, put):
        count, in_leg, ref, last_low, since = 0, False, None, None, 0
        last_h, prev_peak = None, None                     # bar of the last H, swing high after the H before it
        for i in range(1, n):
            if ref is not None and hi[i] > ref:            # new high: pullback over
                count, in_leg, ref, last_low, since = 0, False, None, None, i
                last_h, prev_peak = None, None
                continue
            if count >= 2 and last_low is not None and lo[i] < last_low:
                count, last_low = 0, None                  # failed H2+: start over
            if lo[i] < lo[i - 1] or hi[i] < hi[i - 1]:
                if not in_leg and ref is None:
                    ref = hi[since:i].max() if i > since else hi[i - 1]
                in_leg = True
            if in_leg and hi[i] > hi[i - 1]:
                peak = hi[last_h:i].max() if last_h is not None and i > last_h else None
                if peak is not None and prev_peak is not None and peak > prev_peak:
                    count = 0                              # higher highs: a bull leg, restart at H1
                prev_peak, last_h = peak, i
                count += 1
                put[i].append(f"{tag}{count}")
                in_leg = False
                last_low = lo[max(0, i - 3):i + 1].min()

    run(h, l, "H", lab)
    run(-l, -h, "L", lab)                                  # mirror: L counting = H counting upside down
    return pd.Series([" ".join(x) for x in lab], index=df.index)


def pressure(df, p=PARAMS):
    """Buying minus selling pressure over a rolling window (positive = buying pressure)."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    rng = np.where(h - l > 0, h - l, np.nan)
    d = df["direction"].to_numpy()
    lower_tail = (np.minimum(o, c) - l) / rng >= 1 / 3
    upper_tail = (h - np.maximum(o, c)) / rng >= 1 / 3
    prev_low = np.r_[np.inf, l[:-1]]
    prev_high = np.r_[-np.inf, h[:-1]]
    bull_rev = (l < prev_low) & (c > o) & (c >= h - rng / 3)     # new low, closes up near high
    bear_rev = (h > prev_high) & (c < o) & (c <= l + rng / 3)
    buy = (d > 0).astype(int) + lower_tail.astype(int) + bull_rev.astype(int)
    sell = (d < 0).astype(int) + upper_tail.astype(int) + bear_rev.astype(int)
    w = p["pressure_window"]
    return pd.Series(buy - sell, index=df.index).rolling(w, min_periods=1).sum()


def trending_dojis(df, p=PARAMS):
    """Runs of >= trending_doji_min dojis with rising (or falling) closes, highs and lows.
    Returns list of dict(start, end, direction)."""
    dj = df["is_doji"].to_numpy()
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    out = []
    for s in (1, -1):
        i, n = 1, len(c)
        while i < n:
            j = i
            while (j < n and dj[j] and s * (c[j] - c[j - 1]) > 0 and s * (h[j] - h[j - 1]) > 0
                   and s * (l[j] - l[j - 1]) > 0):
                j += 1
            if j - i >= p["trending_doji_min"]:
                out.append({"start": i, "end": j - 1, "direction": s})
            i = max(j, i + 1)
    return sorted(out, key=lambda x: x["start"])


def analyse(df, p=PARAMS):
    """Everything at once: classified bars + leg labels + pressure, plus event lists."""
    b = classify(df, p)
    b["leg"] = leg_counts(b)
    b["pressure"] = pressure(b, p)
    return b, climaxes(b, p), trending_dojis(b, p)
