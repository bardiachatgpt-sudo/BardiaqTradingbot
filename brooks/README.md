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
