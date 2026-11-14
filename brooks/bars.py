"""Al Brooks price action — book 1 (Trends), chapters 1-2, coded.

Separate project: depends only on pandas/numpy and an OHLC DataFrame
(columns Open, High, Low, Close). Nothing here imports the MACD bot.

Concepts (summarised in our own words; thresholds are tunable, see PARAMS):

  trend bar vs doji   Every bar is either a trend bar (one side controlled it, it has a
                      real body) or a doji (small body relative to the bar and to recent
                      bodies: a one-bar trading range). "Close is close enough."
  trend bar strength  Body at least the median body of the recent bars, open near one
                      extreme and close near the other, close at or beyond the closes and
                      highs of several prior bars, high beyond one or more prior bars,
                      small tails. A very large body after a protracted move (30+ bars
                      without much of a pullback) is flagged as possible exhaustion.
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
    "climax_min_bars": 3,       # consecutive trend bars that make a climax (the book's example: three)
    "reversal_within": 10,      # bars after a climax to look for the opposite strong bar
                                # (book: the two trend bars "are often separated by many bars")
    "prior_bars": 3,            # "closes and highs of several prior bars"
    "strong_score": 4,          # strength score (0-6) for a "strong" trend bar
    "exhaustion_body_x": 2.0,   # "extremely large body": body >= this x median body
    "protracted_bars": 30,      # "a trend that has gone on for 30 or more bars without much of a pullback"
    "ema": 20,                  # the book's 20-bar exponential moving average
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

    # Strength = the signs of a strong trend bar listed in the book (score 0-6), bull side:
    #   1 body at least the median body of the recent bars
    #   2 open on or near the low            3 close on or near the high
    #   4 close at or above the closes and highs of several prior bars
    #   5 high above the high of one or more prior bars
    #   6 small tails
    ex, k = p["extreme_frac"], p["prior_bars"]
    strength = np.zeros(len(c), dtype=int)
    for i in range(len(c)):
        d = direction[i]
        if d == 0 or rng[i] <= 0:
            continue
        a = max(0, i - k)
        s = int(not np.isnan(med[i]) and body[i] >= med[i])
        small_tails = (h[i] - max(o[i], c[i]) <= p["small_tail_frac"] * rng[i]
                       and min(o[i], c[i]) - l[i] <= p["small_tail_frac"] * rng[i])
        if d > 0:
            s += o[i] <= l[i] + ex * rng[i]
            s += c[i] >= h[i] - ex * rng[i]
            s += i > 0 and c[i] >= max(c[a:i].max(), h[a:i].max())
            s += i > 0 and h[i] > h[a:i].min()
        else:
            s += o[i] >= h[i] - ex * rng[i]
            s += c[i] <= l[i] + ex * rng[i]
            s += i > 0 and c[i] <= min(c[a:i].min(), l[a:i].min())
            s += i > 0 and l[i] < l[a:i].max()
        s += small_tails
        strength[i] = s
    # Exhaustion: an extremely large body after a protracted move (30+ bars on one side of
    # the 20-bar EMA, i.e. without much of a pullback) can be a climactic end, not a start.
    ema = pd.Series(c).ewm(span=p["ema"], adjust=False).mean().to_numpy()
    above = pd.Series((c > ema).astype(int)).rolling(p["protracted_bars"]).sum().to_numpy()
    below = pd.Series((c < ema).astype(int)).rolling(p["protracted_bars"]).sum().to_numpy()
    big = body >= p["exhaustion_body_x"] * np.nan_to_num(med, nan=np.inf)
    prot = np.where(direction > 0, np.r_[np.nan, above[:-1]], np.r_[np.nan, below[:-1]]) >= p["protracted_bars"]
    out = df.copy()
    out["body"], out["range"] = body, rng
    out["direction"], out["is_doji"] = direction, is_doji
    out["strength"] = strength
    out["strong"] = strength >= p["strong_score"]
    out["exhaustion"] = (direction != 0) & big & prot
    out["ema20"] = ema
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
    """Buying minus selling pressure over a rolling window (positive = buying pressure).

    Signs of buying pressure listed in the book (selling pressure is the mirror):
      - bars with large bull bodies (body at least the recent median)
      - tails at the bottoms of bars
      - bull reversal bars at the bottoms of down swings (new low, close up near the high)
      - two-bar reversals (a bear bar followed by a bull bar of similar size that closes
        above the bear bar's open)
    """
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    rng = np.where(h - l > 0, h - l, np.nan)
    body = np.abs(c - o)
    med = pd.Series(body).shift(1).rolling(p["median_window"], min_periods=3).median().to_numpy()
    large = body >= np.nan_to_num(med, nan=np.inf)
    lower_tail = (np.minimum(o, c) - l) / rng >= 1 / 3
    upper_tail = (h - np.maximum(o, c)) / rng >= 1 / 3
    prev_low, prev_high = np.r_[np.inf, l[:-1]], np.r_[-np.inf, h[:-1]]
    po, pc, pb = np.r_[np.nan, o[:-1]], np.r_[np.nan, c[:-1]], np.r_[np.nan, body[:-1]]
    bull_rev = (l < prev_low) & (c > o) & (c >= h - rng / 3)
    bear_rev = (h > prev_high) & (c < o) & (c <= l + rng / 3)
    two_bull = (pc < po) & (c > o) & (c > po) & (body >= 0.7 * pb)
    two_bear = (pc > po) & (c < o) & (c < po) & (body >= 0.7 * pb)
    buy = ((c > o) & large).astype(int) + lower_tail + bull_rev + two_bull
    sell = ((c < o) & large).astype(int) + upper_tail + bear_rev + two_bear
    w = p["pressure_window"]
    return pd.Series((buy - sell).astype(float), index=df.index).rolling(w, min_periods=1).sum()


def trending_dojis(df, p=PARAMS):
    """A series of dojis, each with a higher close and most with a high above the prior
    bar's high and a low above the prior bar's low, is a trend (mirror for down).
    Returns list of dict(start, end, direction)."""
    dj = df["is_doji"].to_numpy()
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    n, out = len(c), []
    for s in (1, -1):
        i = 1
        while i < n:
            j = i
            while j < n and dj[j] and s * (c[j] - c[j - 1]) > 0:
                j += 1
            run = j - i
            if run >= p["trending_doji_min"]:
                hh = sum(s * (h[t] - h[t - 1]) > 0 and s * (l[t] - l[t - 1]) > 0 for t in range(i, j))
                if hh > run / 2:
                    out.append({"start": i, "end": j - 1, "direction": s})
            i = max(j, i + 1)
    return sorted(out, key=lambda x: x["start"])


def analyse(df, p=PARAMS):
    """Everything at once: classified bars + leg labels + pressure, plus event lists."""
    b = classify(df, p)
    b["leg"] = leg_counts(b)
    b["pressure"] = pressure(b, p)
    return b, climaxes(b, p), trending_dojis(b, p)
