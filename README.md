# Gold Strategy Backtester

Three XAUUSD strategies, one execution engine, and the validation tools that
tell you whether a result is an edge or a coincidence.

```
gold_bot/
  config.py      account / cost / engine settings
  costs.py       session-aware spread model (news + rollover widening)
  data.py        CSV loading (Dukascopy, MT5, generic), resampling, sanity report
  indicators.py  EMA, ATR, ADX, swings — hand-rolled, no TA-Lib
  engine.py      bar-by-bar fills, stops, partials, trailing, costs
  strategies.py  the three strategies
  metrics.py     expectancy, R-stats, drawdown, year/side/news splits
  sample_data.py synthetic bars for plumbing tests
run_backtest.py  main CLI
validate.py      walk-forward, bootstrap, random-entry benchmark
live_bot.py      MT5 paper/live signal bot using the SAME strategy classes
```

## Install

```bash
pip install pandas numpy matplotlib
pip install MetaTrader5        # only for live_bot.py, Windows only
```

## Run it right now

```bash
python run_backtest.py --synthetic
```

This uses generated data, so the numbers mean nothing — it just proves the
pipeline works. On a random walk all three strategies lose roughly the cost of
trading, which is exactly what should happen. If a backtest engine shows profit
on random data, the engine is broken.

## Get real data

**Dukascopy** (free, tick-level, several years):
<https://www.dukascopy.com/swiss/english/marketwatch/historical/> — instrument
XAU/USD, export CSV, timezone GMT.

**MT5**: Tools → Options → Charts → set max bars to unlimited, then
View → Symbols → XAUUSD → Bars → export. MT5 server time is usually GMT+2/+3,
not GMT, so pass `--source-tz Etc/GMT-3` or every session boundary in this code
is silently wrong. Check by finding the 13:30 UTC US data spike in your data
and confirming it lands where you expect.

```bash
python run_backtest.py --csv data/XAUUSD_M15.csv --tf 15min \
    --start 2023-09-01 --end 2025-09-01 --spread 0.30 --risk 0.5
```

Outputs land in `results/`: per-strategy trade CSVs, equity curves, a summary
table and a PNG chart.

## Reading the output

| Metric | What it tells you |
|---|---|
| `expectancy_R` | The only number that matters. Average R won or lost per trade, after costs. |
| `expectancy_stderr_R` | If expectancy isn't ~2x this, you don't have a result yet. |
| `win_rate_95ci_pct` | The honest range around your win rate. 80% on 40 trades could really be 65%. |
| `costs_pct_of_gross_profit` | Above ~40% and you are trading for your broker. |
| `max_consec_losses` | What you must be able to sit through. Double it for live. |
| `stop_ambiguous` count | Bars where stop and target were both touched. High counts mean the result depends on an assumption you can't verify without tick data. |

## The strategies

**`asian_sweep`** — Asian range (00:00–06:00 UTC) high/low gets swept during
London (07:00–11:00), price closes back inside, fade the sweep. TP1 the
opposite side of the range, TP2 the previous day's extreme. Skips days where
the Asian range already exceeds 60% of the 20-day average daily range.

**`ny_orb`** — Opening range = first 15 minutes after 13:30 UTC. Buy stop and
sell stop either side, OCO, expire 16:00, target 1.5x the range height,
optional H4 20-EMA trend filter. Note the cost model quadruples the spread
across 13:25–13:45; this is the strategy most sensitive to that.

**`trend_pullback`** — ADX(14) > 30, pullback to the 20-EMA, entry on the first
close back in the trend direction. Stop below the swing, TP1 the recent 20-bar
extreme, TP2 3R, then a 2xATR trail. Generalises across timeframes best.

Change parameters by passing them to the constructor, or sweep from the CLI:

```bash
python run_backtest.py --csv data/XAUUSD_M15.csv --strategy trend_pullback \
    --sweep adx_threshold=20,25,30,35
```

## Validate before you believe anything

```bash
python validate.py --csv data/XAUUSD_M15.csv --strategy trend_pullback --blocks 6
```

- **Walk-forward**: consecutive blocks, reported separately. Fewer than ~75%
  positive means the edge is regime-dependent.
- **Bootstrap**: resamples your trades 5,000 times. `prob_expectancy_negative`
  is the chance your result is noise. `worst_5pct_drawdown_R` is the drawdown
  to actually plan around, not the one your backtest happened to produce.
- **Random-entry benchmark**: same exits, random entries. If your strategy
  doesn't beat it, the entry logic contributes nothing.

## Things that will bite you

1. **2023–2025 is a historic gold bull run.** A long-biased system will look
   superb and be fitted to a once-in-a-decade move. Always read the `by side`
   split — if shorts are unprofitable, you have a trend follower, not a
   strategy.
2. **Sub-100 trades tells you nothing.** The 95% CI on a win rate at n=50 is
   roughly ±14 points.
3. **Parameter sweeps find the luckiest setting, not the best one.** If
   ADX 30 works and 25 and 35 both fail, 30 is noise. Look for plateaus.
4. **Spread modelling is not optional on gold**, especially for `ny_orb`.
   Re-run with `--spread 0.50` and see if the edge survives. If it only works
   at 0.20, it doesn't work.
5. **Ambiguous bars**: `--optimistic-bars` resolves them as target-first. The
   gap between that run and the default run is the size of the uncertainty you
   can only remove with tick data.

## Live trading

`live_bot.py` runs the identical strategy objects against MT5 bars, drops the
still-forming bar, and refuses to place orders unless you pass `--live` and set
`"i_have_forward_tested": true` in a config file.

```json
{
  "i_have_forward_tested": false,
  "risk_per_trade_pct": 0.5,
  "max_daily_loss_pct": 3.0,
  "max_trades_per_day": 3,
  "max_spread_usd": 0.60,
  "max_open_positions": 1
}
```

Run it in paper mode for a month first and compare the logged signals against
what the backtest says should have happened over the same period. Divergence
there is the real test — it catches timezone bugs, bar-close mismatches and
spread assumptions that no amount of backtesting will.

**Instrument note (India):** leveraged spot XAUUSD through offshore brokers
sits outside what RBI permits for residents. MCX gold and gold mini futures are
the domestic route and cover 09:00–23:30 IST, which spans both sessions these
strategies target. MCX gold is INR-denominated, so it carries USD/INR exposure
on top of gold — if you test on XAUUSD and trade MCX, you are trading a
different instrument from the one you validated.

Nothing here is financial advice, and past performance in a backtest is a
weaker signal than it feels like.
