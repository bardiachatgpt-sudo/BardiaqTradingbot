# Brooks price action (separate project)

Al Brooks, *Trading Price Action: Trends* — chapters 1–2 coded. Independent of the MACD bot:
it only needs an OHLC `DataFrame` (columns `Open, High, Low, Close`, datetime index).

| Function | What it does |
|---|---|
| `bars.classify(df)` | trend bar vs doji, direction, strength score 0–5, `strong` |
| `bars.leg_counts(df)` | H1/H2/… and L1/L2/… bar counting |
| `bars.climaxes(df)` | consecutive trend bars ended by a pause bar; climactic reversal bar if a strong opposite bar follows |
| `bars.pressure(df)` | rolling buying minus selling pressure |
| `bars.trending_dojis(df)` | runs of dojis with trending closes, highs and lows |
| `bars.analyse(df)` | all of the above |

Thresholds live in `bars.PARAMS`.

```
python -m unittest brooks.test_bars -v
python -m brooks.chart <ohlc.pkl|csv> --tf 5min --day 2026-11-12 --out chart.png
```

`chart.py` needs `matplotlib`.

## Brooks says → the code does (book 1, ch. 1–2)

| Book | Code |
|---|---|
| Every bar is a trend bar or a doji; a doji's body is tiny relative to the bar and the market ("close is close enough") | doji: body < 25% of the bar's range, or < half the median body of the last 10 bars |
| Ideal trend bar: body ≥ median body of the past 5–10 bars, open near the low, close near the high, close at or above the closes and highs of several prior bars, high above one or more prior bars, small tails | strength score 0–6, one point per sign (3 prior bars); `strong` = 4 or more |
| An extremely large body after a protracted move can be an exhaustive climax | `exhaustion`: body ≥ 2× median after 30+ bars on one side of the 20-bar EMA |
| A climax ends with the first pause bar: doji, inside bar, opposite trend bar, or a bar with a prominent tail (example: three bull trend bars) | `climaxes`: 3+ trend bars in a row ended by such a bar |
| Climactic reversal = climax + strong trend bar the other way, often separated by many bars | strong opposite bar within 10 bars |
| High 1/2/3/4, Low 1/2/3/4 leg counting | `leg_counts` (reset rules beyond these pages are our interpretation; the book covers them in book 2) |
| Buying pressure: large bull bodies, tails at the bottoms of bars, bull reversal bars, two-bar reversals (selling is the mirror) | `pressure`: rolling count of these signs, buying minus selling |
| A series of dojis each with a higher close, most with higher highs and lows, is a trend | `trending_dojis` |
