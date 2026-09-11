"""Paper-trading tick logic: reuses engine.py's exact fill_price/exit_events
rules so a paper trade behaves identically to what the batch Backtester would
have done with the same order, just applied one new bar at a time against a
persistent account instead of an in-memory loop.

Everything here is DB-agnostic — plain dicts in, plain dicts out — so
webapp/routers/paper.py owns persistence (SQLite) and this module stays
unit-testable and reusable for Phase 6's live tick loop.

Paper positions carry a single take-profit (not the backtester's TP1/TP2
partial-scale-out) — that matches how a real trading-platform order ticket
works. Passing tp2=None into exit_events naturally produces a full close at
the take-profit level rather than a partial, with no special-casing needed.
"""

from __future__ import annotations

import pandas as pd

from .config import CostConfig, EngineConfig
from .costs import entry_cost, exit_cost
from .engine import Order, Position, exit_events, fill_price


def order_to_Order(row: dict) -> Order:
    return Order(
        created_at=row["created_at"],
        direction=row["direction"],
        kind=row["kind"],
        stop_loss=row["stop_loss"],
        trigger=row.get("trigger_price"),
        tag=row.get("tag") or "",
    )


def try_fill_order(order_row: dict, o: float, h: float, l: float) -> float | None:
    """Would this pending order fill against this bar's O/H/L?"""
    return fill_price(order_to_Order(order_row), o, h, l)


def try_close_position(
    costs_cfg: CostConfig,
    engine_cfg: EngineConfig,
    pos_row: dict,
    ts,
    h: float,
    l: float,
    c: float,
) -> dict | None:
    """Would this open position hit its stop or take-profit on this bar?
    Returns the exit detail if so, else None."""
    time_exit = pos_row.get("time_exit")
    pos = Position(
        entry_time=pos_row["entry_time"],
        entry_price=pos_row["entry_price"],
        direction=pos_row["direction"],
        units=pos_row["units"],
        units_open=pos_row["units"],
        stop=pos_row["stop_loss"],
        initial_stop=pos_row["initial_stop"],
        risk_per_oz=pos_row["risk_per_oz"],
        entry_cost_per_oz=pos_row["entry_cost_per_oz"],
        tp1=pos_row.get("take_profit"),
        tp2=None,
        time_exit=pd.Timestamp(time_exit) if time_exit else None,
    )
    events = exit_events(engine_cfg, pos, ts, h, l, c)
    if not events:
        return None
    reason, price, units, _final = events[-1]
    ec = exit_cost(ts, costs_cfg)
    gross = (price - pos.entry_price) * pos.direction * units
    cost = (pos_row["entry_cost_per_oz"] + ec) * units
    net = gross - cost
    risk_amount = pos_row.get("risk_amount") or (pos_row["risk_per_oz"] * units)
    r_multiple = net / risk_amount if risk_amount else 0.0
    return {
        "reason": reason,
        "price": price,
        "units": units,
        "gross_pnl": gross,
        "costs": cost,
        "net_pnl": net,
        "r_multiple": r_multiple,
    }


def floating_pnl(costs_cfg: CostConfig, pos_row: dict, current_price: float) -> float:
    """Mark-to-market P&L of an open position at the current price, before
    the exit-side cost (which isn't charged until it actually closes)."""
    direction = pos_row["direction"]
    return (current_price - pos_row["entry_price"]) * direction * pos_row["units"]


def entry_cost_at(ts, costs_cfg: CostConfig) -> float:
    return entry_cost(ts, costs_cfg)
