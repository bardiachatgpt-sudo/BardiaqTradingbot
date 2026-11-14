# Progress: coding Al Brooks, rule by rule

Workflow per chapter: read pages → plain-language summary → "book → code" table
(ambiguous points listed below as our decisions) → code + unit tests → chart check,
ideally on the book's own figures.

## Books

### Book 1: *Trading Price Action: Trends*
| Part / chapter | Status | Code |
|---|---|---|
| Ch. 1 The spectrum of price action (trends ↔ trading ranges, legs, H/L counting basics) | done | `bars.leg_counts` |
| Ch. 2 Trend bars, doji bars, and climaxes | done | `bars.classify`, `climaxes`, `pressure`, `trending_dojis` |
| Remaining chapters | not started | |

### Book 2: *Trading Price Action: Trading Ranges*
Not started. Bar counting (H/L reset rules) is covered here.

### Book 3: *Trading Price Action: Reversals*
Not started.

## Our decisions where the book is not exact
Every one of these is a parameter in `bars.PARAMS` and can change once a later chapter
or a figure gives something more precise.

| Topic | Book says | Our decision |
|---|---|---|
| Doji | body tiny or nonexistent, relative to bar and market | body < 25% of range, or < 0.5× median body of last 10 bars |
| "Near the high/low" | on or near | within 1/3 of the bar's range |
| Small tail | small | ≤ 20% of the bar's range |
| Strong trend bar | lists six signs | 4 of 6 |
| "Several prior bars" | several | 3 |
| Climax length | example of three bars | 3+ trend bars |
| Climax → reversal bar | "often separated by many bars" | within 10 bars |
| Extremely large body | extremely large | ≥ 2× median body |
| Protracted move | 30 or more bars without much of a pullback | 30 bars closing on one side of the 20-bar EMA |
| H/L count resets | discussed in book 2 | our interpretation (see `leg_counts` docstring); to be replaced by book 2's rules |

## Open questions
- H/L counting reset rules → waiting for book 2's bar counting chapter.
