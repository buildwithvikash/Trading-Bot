#!/usr/bin/env python3
"""Run the gold strategies over historical data.

Examples
--------
    # smoke test on synthetic data (no download needed)
    python run_backtest.py --synthetic

    # real data, 15-minute bars, last two years
    python run_backtest.py --csv data/XAUUSD_M15.csv --tf 15min \
        --start 2023-09-01 --end 2025-09-01

    # multi-timeframe robustness check
    python run_backtest.py --csv data/XAUUSD_M1.csv --tf 5min,15min,1h

    # sweep a parameter
    python run_backtest.py --synthetic --strategy trend_pullback \
        --sweep adx_threshold=20,25,30,35
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from gold_bot import data as data_mod
from gold_bot import metrics as metrics_mod
from gold_bot.config import AccountConfig, CostConfig, EngineConfig, RunConfig
from gold_bot.engine import Backtester
from gold_bot.sample_data import generate as generate_synthetic
from gold_bot.strategies import STRATEGY_REGISTRY


def build_config(args) -> RunConfig:
    return RunConfig(
        timeframe=args.tf,
        account=AccountConfig(
            initial_equity=args.equity,
            risk_per_trade_pct=args.risk,
            compound=not args.no_compound,
        ),
        costs=CostConfig(
            base_spread=args.spread,
            slippage_per_side=args.slippage,
            commission_per_lot_roundtrip=args.commission,
        ),
        engine=EngineConfig(
            max_trades_per_day=args.max_trades,
            ambiguous_bar_resolution="tp" if args.optimistic_bars else "stop",
        ),
    )


def run_one(df, strategy, cfg, news_dates=None):
    bt = Backtester(cfg)
    res = bt.run(df, strategy)
    trades = metrics_mod.tag_news_days(res.trades, news_dates)
    stats = metrics_mod.summarise(trades, res.equity, cfg.account.initial_equity)
    splits = {}
    if not trades.empty:
        for key in ("year", "side", "exit_reason", "news_day"):
            splits[key] = metrics_mod.split_report(trades, key)
    return res, trades, stats, splits


def main(argv=None):
    p = argparse.ArgumentParser(description="Gold strategy backtester")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="path to OHLC csv")
    src.add_argument("--synthetic", action="store_true", help="use generated fake data")

    p.add_argument("--tf", default="15min", help="timeframe(s), comma separated")
    p.add_argument("--source-tz", default=None, help="tz of the csv if not UTC, e.g. Etc/GMT-3")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--strategy", default="all",
                   help=f"one of {list(STRATEGY_REGISTRY)} or 'all'")
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--risk", type=float, default=0.5, help="%% of equity per trade")
    p.add_argument("--spread", type=float, default=0.30, help="base spread USD/oz")
    p.add_argument("--slippage", type=float, default=0.10, help="USD/oz per side")
    p.add_argument("--commission", type=float, default=0.0, help="USD per lot round trip")
    p.add_argument("--max-trades", type=int, default=3, help="per day")
    p.add_argument("--no-compound", action="store_true")
    p.add_argument("--optimistic-bars", action="store_true",
                   help="resolve ambiguous bars as target-first (shows you the fantasy number)")
    p.add_argument("--sweep", default=None, help="param=v1,v2,v3 (single strategy only)")
    p.add_argument("--news-file", default=None, help="csv/txt with one high-impact date per line")
    p.add_argument("--out", default="results", help="output directory")
    args = p.parse_args(argv)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    names = list(STRATEGY_REGISTRY) if args.strategy == "all" else [args.strategy]
    timeframes = [t.strip() for t in args.tf.split(",")]

    # ---- data ------------------------------------------------------- #
    if args.synthetic:
        # generate at the finest requested timeframe; coarser ones are then a
        # real resample of it instead of a duplicate of the next-finest bar
        finest = min(timeframes, key=lambda tf: pd.Timedelta(tf).total_seconds())
        raw = generate_synthetic(freq=finest)
        print(f"!! SYNTHETIC DATA — results are plumbing checks, not evidence !! (base freq {finest})\n")
    else:
        raw = data_mod.load_csv(args.csv, source_tz=args.source_tz)

    raw = data_mod.slice_period(raw, args.start, args.end)
    if raw.empty:
        sys.exit("No data in the requested period.")

    news_dates = None
    if args.news_file:
        news_dates = [ln.strip() for ln in Path(args.news_file).read_text().splitlines() if ln.strip()]

    summary_rows = []

    for tf in timeframes:
        df = data_mod.resample(raw, tf)
        print(data_mod.sanity_report(df))
        print()

        for name in names:
            cls = STRATEGY_REGISTRY[name]
            cfg = build_config(args)
            cfg.timeframe = tf

            variants = [({}, name)]
            if args.sweep and len(names) == 1:
                key, values = args.sweep.split("=")
                variants = []
                for v in values.split(","):
                    try:
                        val = float(v)
                    except ValueError:
                        val = v
                    variants.append(({key: val}, f"{name}[{key}={v}]"))

            for overrides, label in variants:
                strategy = cls(**overrides)
                res, trades, stats, splits = run_one(df, strategy, cfg, news_dates)
                title = f"{label}  |  {tf}  |  {df.index[0].date()} -> {df.index[-1].date()}"
                print(metrics_mod.format_report(title, stats, splits))
                print()

                row = {"strategy": label, "timeframe": tf, **stats}
                summary_rows.append(row)

                if not trades.empty:
                    trades.to_csv(outdir / f"trades_{label}_{tf}.csv".replace("/", "_"), index=False)
                    res.equity.to_csv(outdir / f"equity_{label}_{tf}.csv".replace("/", "_"))

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(outdir / "summary.csv", index=False)
    print("=" * 66)
    print("  SUMMARY")
    print("=" * 66)
    cols = [c for c in ["strategy", "timeframe", "trades", "win_rate_pct", "expectancy_R",
                        "profit_factor", "total_return_pct", "max_drawdown_pct"] if c in summary]
    print(summary[cols].to_string(index=False))
    print(f"\nwritten to {outdir.resolve()}")

    try:
        _plot(outdir, summary_rows)
    except Exception as exc:  # matplotlib optional
        print(f"(chart skipped: {exc})")


def _plot(outdir: Path, rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    files = sorted(outdir.glob("equity_*.csv"))
    if not files:
        return
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for f in files:
        s = pd.read_csv(f, index_col=0, parse_dates=True).iloc[:, 0]
        ax.plot(s.index, s.to_numpy(), lw=1.2, label=f.stem.replace("equity_", ""))
    ax.set_title("Equity curves")
    ax.set_ylabel("Equity (USD)")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(outdir / "equity_curves.png", dpi=130)
    print(f"chart: {outdir / 'equity_curves.png'}")


if __name__ == "__main__":
    main()
