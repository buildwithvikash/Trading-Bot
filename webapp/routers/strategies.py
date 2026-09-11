"""Strategy management: parameter-schema introspection off the existing
@dataclass strategy classes, CRUD over saved named configs (SQLite), and a
side-by-side comparison run across several saved configs.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gold_bot.rule_strategy import INDICATOR_CATALOG, OPERATORS, PRICE_FIELDS, validate_spec
from gold_bot.strategies import BUILDER_DRIVEN_KEYS, STRATEGY_REGISTRY
from webapp import db
from webapp.routers.backtest import BacktestRequest, execute_backtest

router = APIRouter()


# ------------------------------------------------------------------ #
# indicator catalog for the no-code Strategy Builder
# ------------------------------------------------------------------ #
@router.get("/indicators")
def list_indicators():
    return {
        "indicators": [
            {
                "id": key, "label": cat["label"],
                "outputs": [f"{{id}}{suffix}" for suffix in cat["outputs"]],
                "params": cat["params"],
            }
            for key, cat in INDICATOR_CATALOG.items()
        ],
        "operators": OPERATORS,
        "price_fields": PRICE_FIELDS,
    }


# ------------------------------------------------------------------ #
# strategy class schema (auto-derived from @dataclass fields — no
# per-strategy UI code needed when a strategy gains/loses a param)
# ------------------------------------------------------------------ #
def _type_name(annotation: Any) -> str:
    s = str(annotation)
    if "tuple" in s:
        return "tuple"
    if "bool" in s:
        return "bool"
    if "float" in s:
        return "float"
    if "int" in s:
        return "int"
    return "str"


def _param_schema(cls) -> list[dict]:
    out = []
    for f in dataclasses.fields(cls):
        if f.name == "name":
            continue
        if f.default is not dataclasses.MISSING:
            default = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            default = f.default_factory()  # type: ignore[misc]
        else:
            default = None
        out.append({"name": f.name, "type": _type_name(f.type), "default": default})
    return out


@router.get("/classes")
def list_classes():
    return [
        {"id": key, "label": cls.__name__, "params": _param_schema(cls)}
        for key, cls in STRATEGY_REGISTRY.items()
        if key not in BUILDER_DRIVEN_KEYS
    ]


# ------------------------------------------------------------------ #
# saved config CRUD
# ------------------------------------------------------------------ #
class StrategyConfigIn(BaseModel):
    name: str
    strategy_class: str
    params: dict[str, Any] = {}


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "strategy_class": row["strategy_class"],
        "params": json.loads(row["params_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("")
def list_configs():
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT * FROM strategies ORDER BY updated_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/{config_id}")
def get_config(config_id: int):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM strategies WHERE id = ?", (config_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "strategy config not found")
        return _row_to_dict(row)
    finally:
        conn.close()


def _validate(cfg: StrategyConfigIn):
    if cfg.strategy_class not in STRATEGY_REGISTRY:
        raise HTTPException(400, f"unknown strategy class '{cfg.strategy_class}'")
    if cfg.strategy_class in BUILDER_DRIVEN_KEYS:
        try:
            validate_spec(cfg.params.get("spec", {}))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, f"invalid strategy: {exc}")
        return
    try:
        STRATEGY_REGISTRY[cfg.strategy_class](**{k: v for k, v in cfg.params.items() if v is not None})
    except TypeError as exc:
        raise HTTPException(400, f"invalid strategy parameters: {exc}")


@router.post("")
def create_config(cfg: StrategyConfigIn):
    _validate(cfg)
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO strategies (name, strategy_class, params_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cfg.name, cfg.strategy_class, json.dumps(cfg.params), now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM strategies WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


@router.put("/{config_id}")
def update_config(config_id: int, cfg: StrategyConfigIn):
    _validate(cfg)
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "UPDATE strategies SET name = ?, strategy_class = ?, params_json = ?, updated_at = ? "
            "WHERE id = ?",
            (cfg.name, cfg.strategy_class, json.dumps(cfg.params), now, config_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "strategy config not found")
        row = conn.execute("SELECT * FROM strategies WHERE id = ?", (config_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


@router.delete("/{config_id}")
def delete_config(config_id: int):
    conn = db.get_conn()
    try:
        cur = conn.execute("DELETE FROM strategies WHERE id = ?", (config_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "strategy config not found")
        return {"deleted": config_id}
    finally:
        conn.close()


@router.post("/{config_id}/duplicate")
def duplicate_config(config_id: int):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM strategies WHERE id = ?", (config_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "strategy config not found")
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO strategies (name, strategy_class, params_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{row['name']} (copy)", row["strategy_class"], row["params_json"], now, now),
        )
        conn.commit()
        new_row = conn.execute("SELECT * FROM strategies WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(new_row)
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# comparison dashboard — run several saved configs over the same window
# ------------------------------------------------------------------ #
def _declared_timeframe(strategy_class: str, params: dict) -> str:
    """A saved config's own working timeframe — Strategy.timeframe for
    hand-coded classes, spec.timeframe for custom_rule — read straight off
    its saved params rather than instantiating the class, since a bad
    param elsewhere in the config shouldn't block reading just this one."""
    if strategy_class == "custom_rule":
        return (params.get("spec") or {}).get("timeframe", "15min")
    return params.get("timeframe", "15min")


class CompareRequest(BaseModel):
    config_ids: list[int]
    timeframe: str | None = None  # None = each strategy's own declared timeframe
    session: str = "all"
    start: str | None = None
    end: str | None = None
    initial_equity: float = 10_000.0
    risk_per_trade_pct: float = 0.5
    base_spread: float = 0.30
    slippage_per_side: float = 0.10
    commission_per_lot_roundtrip: float = 0.0
    max_trades_per_day: int = 3
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None


@router.post("/compare")
def compare_configs(req: CompareRequest):
    conn = db.get_conn()
    try:
        rows = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT * FROM strategies WHERE id IN ({','.join('?' * len(req.config_ids))})",
                req.config_ids,
            ).fetchall()
        } if req.config_ids else {}
    finally:
        conn.close()

    out = []
    for cid in req.config_ids:
        row = rows.get(cid)
        if row is None:
            out.append({"id": cid, "error": "strategy config not found"})
            continue
        row_params = json.loads(row["params_json"])
        bt_req = BacktestRequest(
            strategy=row["strategy_class"],
            params=row_params,
            timeframe=req.timeframe or _declared_timeframe(row["strategy_class"], row_params),
            session=req.session,
            start=req.start,
            end=req.end,
            initial_equity=req.initial_equity,
            risk_per_trade_pct=req.risk_per_trade_pct,
            base_spread=req.base_spread,
            slippage_per_side=req.slippage_per_side,
            commission_per_lot_roundtrip=req.commission_per_lot_roundtrip,
            max_trades_per_day=req.max_trades_per_day,
            max_daily_loss_pct=req.max_daily_loss_pct,
            max_drawdown_pct=req.max_drawdown_pct,
        )
        result = execute_backtest(bt_req)
        if "error" in result:
            out.append({"id": cid, "name": row["name"], "error": result["error"]})
            continue
        out.append(
            {
                "id": cid,
                "name": row["name"],
                "strategy_class": row["strategy_class"],
                "timeframe": bt_req.timeframe,
                "warning": result["warning"],
                "stats": result["stats"],
                "equity": result["equity"],
            }
        )
    return out
