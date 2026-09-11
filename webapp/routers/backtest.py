"""Run a backtest through the existing gold_bot engine and return everything
the Backtesting view needs: headline stats, equity curve, session/month/year/
exit-reason splits, best/worst months, and the full trade-by-trade log
(including tp1/tp2 so the UI can draw SL/TP levels).
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
from fastapi import APIRouter
from pydantic import BaseModel

from gold_bot import metrics as metrics_mod
from gold_bot.config import AccountConfig, CostConfig, EngineConfig, RunConfig
from gold_bot.data import resample, slice_period
from gold_bot.engine import Backtester
from gold_bot.sessions import SessionFilterStrategy, tag_sessions
from gold_bot.strategies import STRATEGY_REGISTRY
from webapp.routers.market import TF_MAP, _raw


def _json_safe(d: dict) -> dict:
    """np.inf (e.g. profit_factor with zero gross loss) isn't valid JSON —
    JS's JSON.parse rejects a literal Infinity token. Swap it for None."""
    return {
        k: (None if isinstance(v, float) and (math.isinf(v) or math.isnan(v)) else v)
        for k, v in d.items()
    }

router = APIRouter()


# ------------------------------------------------------------------ #
# run — also called directly by strategies.py's comparison endpoint,
# so the execution logic lives in one place regardless of caller.
# ------------------------------------------------------------------ #
class BacktestRequest(BaseModel):
    strategy: str
    params: dict[str, Any] = {}
    timeframe: str = "15min"
    session: str = "all"  # all | london | ny | overlap
    start: str | None = None
    end: str | None = None
    initial_equity: float = 10_000.0
    risk_per_trade_pct: float = 0.5
    base_spread: float = 0.30
    slippage_per_side: float = 0.10
    commission_per_lot_roundtrip: float = 0.0
    max_trades_per_day: int = 3
    optimistic_bars: bool = False
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None


def _clean_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop None values for params so the strategy's own default is used
    instead of failing the dataclass constructor on an optional field."""
    return {k: v for k, v in raw.items() if v is not None}


def _equity_downsample(equity: pd.Series, target: int = 500) -> list[list]:
    n = len(equity)
    if n == 0:
        return []
    stride = max(1, n // target)
    ds = equity.iloc[::stride]
    if ds.index[-1] != equity.index[-1]:
        ds = pd.concat([ds, equity.iloc[[-1]]])
    return [[ts.isoformat(), round(float(v), 2)] for ts, v in ds.items()]


def _drawdown_series(equity: pd.Series, target: int = 500) -> list[list]:
    eq = equity.dropna()
    peak = eq.cummax()
    dd = ((eq - peak) / peak * 100).fillna(0.0)
    n = len(dd)
    if n == 0:
        return []
    stride = max(1, n // target)
    ds = dd.iloc[::stride]
    if ds.index[-1] != dd.index[-1]:
        ds = pd.concat([ds, dd.iloc[[-1]]])
    return [[ts.isoformat(), round(float(v), 3)] for ts, v in ds.items()]


def _trades_payload(trades: pd.DataFrame) -> list[dict]:
    if trades is None or trades.empty:
        return []
    out = []
    for r in trades.itertuples():
        out.append(
            {
                "entry_time": r.entry_time.isoformat(),
                "exit_time": r.exit_time.isoformat(),
                "direction": "long" if r.direction == 1 else "short",
                "entry_price": round(float(r.entry_price), 2),
                "exit_price": round(float(r.exit_price), 2),
                "initial_stop": round(float(r.initial_stop), 2),
                "tp1": None if pd.isna(r.tp1) else round(float(r.tp1), 2),
                "tp2": None if pd.isna(r.tp2) else round(float(r.tp2), 2),
                "lots": round(float(r.lots), 3),
                "net_pnl": round(float(r.net_pnl), 2),
                "r_multiple": round(float(r.r_multiple), 3),
                "exit_reason": r.exit_reason,
                "bars_held": int(r.bars_held),
            }
        )
    return out


def _split_payload(trades: pd.DataFrame, by: str) -> list[dict]:
    table = metrics_mod.split_report(trades, by)
    if table.empty:
        return []
    return [{"key": str(idx), **row} for idx, row in table.to_dict(orient="index").items()]


def _best_worst_months(trades: pd.DataFrame) -> dict | None:
    if trades is None or trades.empty:
        return None
    m = metrics_mod.split_report(trades, "month")
    if m.empty:
        return None
    best_key = m["net_pnl"].idxmax()
    worst_key = m["net_pnl"].idxmin()
    return {
        "best": {"period": best_key, **m.loc[best_key].to_dict()},
        "worst": {"period": worst_key, **m.loc[worst_key].to_dict()},
    }


def execute_backtest(req: BacktestRequest) -> dict:
    if req.timeframe not in TF_MAP:
        return {"error": f"unknown timeframe '{req.timeframe}'"}
    if req.strategy not in STRATEGY_REGISTRY:
        return {"error": f"unknown strategy '{req.strategy}'"}

    df, meta = _raw()
    alias, minutes = TF_MAP[req.timeframe]
    base_minutes = meta.get("base_minutes")
    warning = None

    if base_minutes and minutes < base_minutes:
        bar_df = df
        warning = (
            f"data source is {base_minutes}-minute bars; results reflect that "
            f"native resolution, not a genuine {req.timeframe} backtest"
        )
    elif minutes == base_minutes:
        bar_df = df
    else:
        bar_df = resample(df, alias)

    if req.start is None and req.end is None and not bar_df.empty:
        # default window: the trailing year of data actually available,
        # ending at the dataset's own last bar (not wall-clock "today" —
        # a static/synthetic dataset may not extend that far, and "1 year
        # ending in a gap" would silently return zero trades)
        end_ts = bar_df.index[-1]
        bar_df = bar_df[bar_df.index >= end_ts - pd.Timedelta(days=365)]
    else:
        bar_df = slice_period(bar_df, req.start, req.end)
    if bar_df.empty:
        return {"error": "no data in the requested period"}

    try:
        strategy = STRATEGY_REGISTRY[req.strategy](**_clean_params(req.params))
    except TypeError as exc:
        return {"error": f"invalid strategy parameters: {exc}"}

    if req.session != "all":
        strategy = SessionFilterStrategy(strategy, req.session)

    cfg = RunConfig(
        timeframe=req.timeframe,
        account=AccountConfig(
            initial_equity=req.initial_equity, risk_per_trade_pct=req.risk_per_trade_pct
        ),
        costs=CostConfig(
            base_spread=req.base_spread,
            slippage_per_side=req.slippage_per_side,
            commission_per_lot_roundtrip=req.commission_per_lot_roundtrip,
        ),
        engine=EngineConfig(
            max_trades_per_day=req.max_trades_per_day,
            ambiguous_bar_resolution="tp" if req.optimistic_bars else "stop",
            max_daily_loss_pct=req.max_daily_loss_pct,
            max_drawdown_pct=req.max_drawdown_pct,
        ),
    )

    result = Backtester(cfg).run(bar_df, strategy)
    trades = result.trades
    stats = metrics_mod.summarise(trades, result.equity, cfg.account.initial_equity)

    splits: dict[str, list[dict]] = {}
    best_worst = None
    if not trades.empty:
        trades = tag_sessions(trades)
        for key in ("year", "month", "session", "exit_reason"):
            splits[key] = _split_payload(trades, key)
        best_worst = _best_worst_months(trades)

    if isinstance(stats, dict) and "avg_win_R" in stats and stats.get("avg_loss_R"):
        stats["rr_ratio"] = round(abs(stats["avg_win_R"] / stats["avg_loss_R"]), 2)
    else:
        stats["rr_ratio"] = None
    stats = _json_safe(stats)

    return {
        "warning": warning,
        "stats": stats,
        "equity": _equity_downsample(result.equity),
        "drawdown": _drawdown_series(result.equity),
        "splits": splits,
        "best_worst": best_worst,
        "trades": _trades_payload(trades),
        "bars": len(bar_df),
        "risk": {
            "dd_halted_at": result.dd_halted_at.isoformat() if result.dd_halted_at is not None else None,
            "daily_loss_breach_days": result.daily_loss_breach_days,
        },
    }


@router.post("/run")
def run_backtest(req: BacktestRequest):
    return execute_backtest(req)
