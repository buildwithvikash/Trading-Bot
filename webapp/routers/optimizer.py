"""Daily optimizer reports: what it found, what it changed, why, and the expected effect."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webapp import db, optimizer

router = APIRouter()


class SettingsRequest(BaseModel):
    enabled: bool


@router.get("/status")
def status():
    return {"enabled": optimizer.is_enabled(), "run_hour_utc": optimizer.RUN_HOUR_UTC,
            "min_trades": optimizer.MIN_TRADES, "lookback_days": optimizer.LOOKBACK_DAYS}


@router.post("/settings")
def settings(req: SettingsRequest):
    optimizer.set_enabled(req.enabled)
    return status()


@router.post("/run")
def run_now():
    return {"run_id": optimizer.run_review("manual")}


@router.get("/reports")
def reports(limit: int = 30, start: str | None = None, end: str | None = None):
    """start/end are ISO datetimes (any offset, e.g. from an IST <input
    type=datetime-local> converted client-side) filtering on run_at."""
    conn = db.get_conn()
    try:
        clauses, params = [], []
        if start:
            clauses.append("run_at >= ?")
            params.append(start)
        if end:
            clauses.append("run_at <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(limit, 200))
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM optimizer_runs {where} ORDER BY id DESC LIMIT ?", params).fetchall()]
    finally:
        conn.close()


@router.get("/reports/{run_id}")
def report(run_id: int):
    conn = db.get_conn()
    try:
        run = conn.execute("SELECT * FROM optimizer_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "report not found")
        acts = [dict(r) for r in conn.execute(
            "SELECT * FROM optimizer_actions WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()]
        return {**dict(run), "actions": acts}
    finally:
        conn.close()
