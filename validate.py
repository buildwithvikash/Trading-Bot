#!/usr/bin/env python3
"""Validation, not optimisation.

Three checks that separate a real edge from a curve fit:

1. WALK-FORWARD — split the history into consecutive blocks and report each
   one. A strategy that only works in one block is a fit to that block.
2. BOOTSTRAP — resample the trade sequence 5,000 times to get a distribution
   for expectancy and max drawdown. Tells you how much of your result is luck
   and what drawdown you should actually plan for.
3. RANDOM-ENTRY BENCHMARK — run the same exit logic on random entries. If your
   strategy doesn't beat that, the edge is in the exits (or nowhere).

Usage
-----
    python validate.py --synthetic --strategy trend_pullback
    python validate.py --csv data/XAUUSD_M15.csv --strategy asian_sweep --blocks 6
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from gold_bot import data as data_mod
from gold_bot import metrics as metrics_mod
from gold_bot.config import AccountConfig, CostConfig, EngineConfig, RunConfig
from gold_bot.engine import Backtester, Order
from gold_bot.indicators import atr
from gold_bot.sample_data import generate as generate_synthetic
from gold_bot.strategies import STRATEGY_REGISTRY, Strategy


class RandomEntry(Strategy):
    """Benchmark: random entries, same style of stop/target as the real thing."""

    name = "random_entry_benchmark"

    def __init__(self, rate=0.004, rr=2.0, atr_stop=1.5, seed=1):
        self.rate, self.rr, self.atr_stop = rate, rr, atr_stop
        self.rng = np.random.default_rng(seed)

    def prepare(self, df):
        df["atr"] = atr(df, 14)
        return df

    def generate(self, df, i, state):
        if self.rng.random() > self.rate:
            return []
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []
        d = 1 if self.rng.random() < 0.5 else -1
        risk = self.atr_stop * a
        entry = row["close"]
        return [
            Order(
                created_at=df.index[i], direction=d, kind="market",
                stop_loss=entry - d * risk, tp2=entry + d * self.rr * risk,
                tag=self.name,
            )
        ]


def bootstrap(r: np.ndarray, n_iter: int = 5000, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    n = len(r)
    exp = np.empty(n_iter)
    dd = np.empty(n_iter)
    for k in range(n_iter):
        sample = rng.choice(r, size=n, replace=True)
        exp[k] = sample.mean()
        curve = np.cumsum(sample)
        peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
        dd[k] = (curve - peak).min()
    return {
        "expectancy_R_mean": round(float(exp.mean()), 4),
        "expectancy_R_5th_pct": round(float(np.percentile(exp, 5)), 4),
        "expectancy_R_95th_pct": round(float(np.percentile(exp, 95)), 4),
        "prob_expectancy_negative": round(float((exp <= 0).mean()), 3),
        "median_max_drawdown_R": round(float(np.median(dd)), 2),
        "worst_5pct_drawdown_R": round(float(np.percentile(dd, 5)), 2),
    }


def main():
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--synthetic", action="store_true")
    p.add_argument("--tf", default="15min")
    p.add_argument("--source-tz", default=None)
    p.add_argument("--strategy", default="trend_pullback", choices=list(STRATEGY_REGISTRY))
    p.add_argument("--blocks", type=int, default=4)
    p.add_argument("--risk", type=float, default=0.5)
    p.add_argument("--spread", type=float, default=0.30)
    args = p.parse_args()

    raw = generate_synthetic() if args.synthetic else data_mod.load_csv(args.csv, args.source_tz)
    df = data_mod.resample(raw, args.tf)

    cfg = RunConfig(
        timeframe=args.tf,
        account=AccountConfig(risk_per_trade_pct=args.risk),
        costs=CostConfig(base_spread=args.spread),
        engine=EngineConfig(),
    )
    strat_cls = STRATEGY_REGISTRY[args.strategy]

    # ---------------- walk-forward blocks ---------------- #
    print("=" * 66)
    print(f"  WALK-FORWARD — {args.strategy} on {args.tf}, {args.blocks} blocks")
    print("=" * 66)
    edges = np.array_split(np.arange(len(df)), args.blocks)
    rows = []
    for b, sl in enumerate(edges, 1):
        sub = df.iloc[sl[0] : sl[-1] + 1]
        res = Backtester(cfg).run(sub, strat_cls())
        if res.trades.empty:
            rows.append({"block": b, "trades": 0})
            continue
        r = res.trades["r_multiple"]
        rows.append(
            {
                "block": b,
                "from": sub.index[0].date(),
                "to": sub.index[-1].date(),
                "trades": len(r),
                "win_rate_pct": round(100 * (r > 0).mean(), 1),
                "expectancy_R": round(r.mean(), 3),
                "total_R": round(r.sum(), 1),
            }
        )
    wf = pd.DataFrame(rows)
    print(wf.to_string(index=False))
    consistent = (wf.get("expectancy_R", pd.Series(dtype=float)) > 0).sum()
    print(f"\n  blocks with positive expectancy: {consistent}/{len(wf)}")
    print("  (fewer than ~75% positive = the edge is regime-dependent at best)\n")

    # ---------------- full run + bootstrap ---------------- #
    res = Backtester(cfg).run(df, strat_cls())
    if res.trades.empty:
        print("No trades on the full sample; nothing to bootstrap.")
        return
    r = res.trades["r_multiple"].to_numpy(float)
    print("=" * 66)
    print(f"  BOOTSTRAP — {len(r)} trades resampled 5,000x")
    print("=" * 66)
    for k, v in bootstrap(r).items():
        print(f"  {k:<28} : {v}")
    print()

    # ---------------- random-entry benchmark -------------- #
    print("=" * 66)
    print("  RANDOM-ENTRY BENCHMARK (same costs, ATR stop, 2R target)")
    print("=" * 66)
    bench = Backtester(cfg).run(df, RandomEntry())
    if bench.trades.empty:
        print("  benchmark produced no trades")
    else:
        br = bench.trades["r_multiple"]
        print(f"  trades        : {len(br)}")
        print(f"  win_rate_pct  : {round(100 * (br > 0).mean(), 1)}")
        print(f"  expectancy_R  : {round(br.mean(), 4)}")
        print(f"\n  strategy expectancy {r.mean():.4f}R vs benchmark {br.mean():.4f}R")
        print(f"  edge over random: {r.mean() - br.mean():+.4f}R per trade")


if __name__ == "__main__":
    main()
