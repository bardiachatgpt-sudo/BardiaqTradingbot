"""Draw a candlestick chart with Brooks labels, for checking the code by eye.

    python -m brooks.chart <ohlc.pkl|ohlc.csv> --tf 5min --day 2026-11-13 --out chart.png

The input is any OHLC file with a datetime index (pickle or CSV with a time column first).
Labels: D = doji, strong trend bars outlined, H1/H2.. under bars, L1/L2.. above bars,
climaxes shaded, climactic reversal bars starred. 20-bar EMA drawn like in the book.
"""
import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from brooks import bars  # noqa: E402


def load(path):
    if path.endswith(".pkl"):
        return pd.read_pickle(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)


def resample(df, tf):
    return df.resample(tf).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()


def draw(df, title, out):
    b, cl, td = bars.analyse(df)
    n = len(b)
    fig, ax = plt.subplots(figsize=(max(12, n * 0.13), 6.5), dpi=110)
    rng = (b["High"].max() - b["Low"].min()) or 1
    for i, (o, h, l, c) in enumerate(zip(b.Open, b.High, b.Low, b.Close)):
        bull = c >= o
        ax.vlines(i, l, h, color="black", linewidth=0.8)
        ax.add_patch(plt.Rectangle((i - 0.32, min(o, c)), 0.64, max(abs(c - o), rng * 0.0015),
                                   facecolor="white" if bull else "black", edgecolor="black",
                                   linewidth=1.6 if b["strong"].iat[i] else 0.8))
        if b["exhaustion"].iat[i]:
            ax.text(i, (l if c < o else h) + (-1 if c < o else 1) * rng * 0.07, "X", ha="center",
                    fontsize=9, color="darkorange", weight="bold")
        if b["is_doji"].iat[i]:
            ax.text(i, h + rng * 0.012, "D", ha="center", fontsize=7, color="tab:blue")
        lab = b["leg"].iat[i]
        for part in lab.split():
            if part.startswith("H"):
                ax.text(i, l - rng * 0.03, part, ha="center", fontsize=7, color="tab:green", weight="bold")
            else:
                ax.text(i, h + rng * 0.035, part, ha="center", fontsize=7, color="tab:red", weight="bold")
    for x in cl:
        ax.axvspan(x["start"] - 0.5, x["end"] + 0.5, color="orange" if x["direction"] > 0 else "purple", alpha=0.12)
        if x["reversal_bar"] is not None:
            r = x["reversal_bar"]
            y = b["Low"].iat[r] - rng * 0.06 if x["direction"] > 0 else b["High"].iat[r] + rng * 0.06
            ax.text(r, y, "★", ha="center", fontsize=11, color="crimson")
    ax.plot(range(n), b["Close"].ewm(span=20, adjust=False).mean(), color="gray", linewidth=1)
    step = max(1, n // 16)
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([t.strftime("%H:%M") for t in b.index[::step]], fontsize=8)
    ax.set_xlim(-1, n)
    ax.set_title(title + "   (D doji · bold outline = strong trend bar · shaded = climax · ★ climactic reversal · X possible exhaustion)",
                 fontsize=9)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out)
    return b, cl, td


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tf", default="5min")
    ap.add_argument("--day", required=True, help="UTC date, e.g. 2026-11-13")
    ap.add_argument("--start", default="06:00")
    ap.add_argument("--end", default="20:00")
    ap.add_argument("--out", default="brooks_chart.png")
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    df = resample(load(a.path), a.tf)
    day = df[(df.index >= pd.Timestamp(f"{a.day} {a.start}", tz="UTC")) & (df.index < pd.Timestamp(f"{a.day} {a.end}", tz="UTC"))]
    b, cl, td = draw(day, a.title or f"{a.day} {a.tf}", a.out)
    print(f"bars {len(b)}  dojis {int(b.is_doji.sum())}  strong trend bars {int(b.strong.sum())}  "
          f"climaxes {len(cl)} (reversals {sum(x['reversal_bar'] is not None for x in cl)})  "
          f"H labels {int(b.leg.str.contains('H').sum())}  L labels {int(b.leg.str.contains('L').sum())}  -> {a.out}")


if __name__ == "__main__":
    main()
