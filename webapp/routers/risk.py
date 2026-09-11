"""Position-size calculator and the terminal's default risk settings.

The calculator wraps gold_bot.risk.position_size — the exact function the
backtester's Backtester._size uses — so what this endpoint shows you matches
what a backtest actually does with the same equity/risk%/stop distance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from gold_bot.risk import position_size
from webapp import db

router = APIRouter()


# ------------------------------------------------------------------ #
# calculator
# ------------------------------------------------------------------ #
class PositionSizeRequest(BaseModel):
    equity: float
    risk_pct: float
    entry_price: float
    stop_price: float
    contract_size: float = 100.0
    min_lot: float = 0.01
    max_lot: float = 50.0
    max_risk_pct: float | None = None


@router.post("/position-size")
def calc_position_size(req: PositionSizeRequest):
    risk_per_oz = abs(req.entry_price - req.stop_price)
    lots, risk_amount = position_size(
        req.equity, req.risk_pct, risk_per_oz,
        contract_size=req.contract_size, min_lot=req.min_lot, max_lot=req.max_lot,
        max_risk_pct=req.max_risk_pct,
    )
    capped = req.max_risk_pct is not None and req.risk_pct > req.max_risk_pct
    return {
        "lots": lots,
        "units": round(lots * req.contract_size, 4),
        "risk_per_oz": round(risk_per_oz, 4),
        "risk_amount_usd": round(risk_amount, 2),
        "risk_pct_used": min(req.risk_pct, req.max_risk_pct) if capped else req.risk_pct,
        "capped_by_max_risk": capped,
        "unsizeable": lots == 0.0,
    }


# ------------------------------------------------------------------ #
# default settings (single row)
# ------------------------------------------------------------------ #
class RiskSettingsIn(BaseModel):
    risk_per_trade_pct: float = 0.5
    max_risk_per_trade_pct: float = 2.0
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None
    max_open_positions: int = 1
    contract_size: float = 100.0
    min_lot: float = 0.01
    max_lot: float = 50.0
    # Separate from risk_per_trade_pct (which only sizes the POSITION from
    # the stop distance) — these are account-wide default distances for the
    # stop and target THEMSELVES, used to suggest SL/TP on the manual order
    # ticket when a strategy isn't already dictating its own (every
    # auto-traded strategy always computes its own explicit SL/TP and never
    # falls back to these).
    default_sl_atr_mult: float = 1.5
    default_tp_rr: float = 2.0


@router.get("/settings")
def get_settings():
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM risk_settings WHERE id = 1").fetchone()
        return dict(row)
    finally:
        conn.close()


@router.put("/settings")
def update_settings(cfg: RiskSettingsIn):
    conn = db.get_conn()
    try:
        conn.execute(
            """UPDATE risk_settings SET
                risk_per_trade_pct = ?, max_risk_per_trade_pct = ?,
                max_daily_loss_pct = ?, max_drawdown_pct = ?, max_open_positions = ?,
                contract_size = ?, min_lot = ?, max_lot = ?,
                default_sl_atr_mult = ?, default_tp_rr = ?, updated_at = ?
               WHERE id = 1""",
            (
                cfg.risk_per_trade_pct, cfg.max_risk_per_trade_pct,
                cfg.max_daily_loss_pct, cfg.max_drawdown_pct, cfg.max_open_positions,
                cfg.contract_size, cfg.min_lot, cfg.max_lot,
                cfg.default_sl_atr_mult, cfg.default_tp_rr,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM risk_settings WHERE id = 1").fetchone()
        return dict(row)
    finally:
        conn.close()
