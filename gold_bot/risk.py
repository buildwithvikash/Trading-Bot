"""Position sizing, shared by the batch Backtester (engine.py) and — from
Phase 5 onward — the paper-trading engine, so risk math lives in one place
instead of drifting apart between two implementations.
"""

from __future__ import annotations

import math


def position_size(
    equity: float,
    risk_pct: float,
    risk_per_oz: float,
    contract_size: float = 100.0,
    min_lot: float = 0.01,
    max_lot: float = 50.0,
    max_risk_pct: float | None = None,
) -> tuple[float, float]:
    """How many lots to risk `risk_pct`% of `equity` given a `risk_per_oz`
    (the distance from entry to stop, in price units).

    Returns (lots, risk_amount_usd). lots is 0.0 when the trade can't be
    sized — no risk distance, non-positive equity, or the size rounds below
    the broker's minimum lot.
    """
    if risk_per_oz <= 0 or not math.isfinite(risk_per_oz) or equity <= 0:
        return 0.0, 0.0

    pct = min(risk_pct, max_risk_pct) if max_risk_pct is not None else risk_pct
    risk_amount = equity * pct / 100.0
    lots = (risk_amount / risk_per_oz) / contract_size
    lots = max(0.0, min(round(lots, 2), max_lot))
    if lots < min_lot:
        return 0.0, risk_amount
    return lots, risk_amount
