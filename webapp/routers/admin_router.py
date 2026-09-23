"""One-time migration helper: restore the SQLite database from an uploaded
file — used to carry local trade history/wallets/running-strategies over to
a fresh deploy (whose database otherwise starts empty). Gated by the same
session auth as everything else (see webapp.server's AuthMiddleware) since
it's not in the public-path allowlist.

Meant to be removed once the one-time migration it exists for is done —
letting anyone with the login password overwrite the whole database forever
is more attack surface than a paper-trading app needs long-term.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from webapp import db
from webapp.routers import paper

router = APIRouter()

SQLITE_MAGIC = b"SQLite format 3\x00"


@router.post("/restore-db")
async def restore_db(file: UploadFile = File(...)):
    data = await file.read()
    if not data.startswith(SQLITE_MAGIC):
        raise HTTPException(400, "not a SQLite database file")

    paper.AUTOTRADERS.stop_all()
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.DB_PATH.write_bytes(data)
    db.init_db()  # re-apply schema migrations on top of the restored file
    paper.resume_saved_autotrades()

    return {"ok": True, "bytes_written": len(data)}


class PurgeTradesRequest(BaseModel):
    trade_ids: list[int]


@router.post("/purge-trades")
def purge_trades(req: PurgeTradesRequest):
    """Delete specific paper_trade_history rows by id (e.g. corrupted
    entries from a data bug) and recompute every wallet they touched —
    balance and peak_balance are replayed from starting_balance through
    that wallet's REMAINING trade history in exit_time order, rather than
    just subtracting the deleted rows' net_pnl, so the result is exact
    regardless of any other drift."""
    if not req.trade_ids:
        raise HTTPException(400, "trade_ids is empty")
    conn = db.get_conn()
    try:
        placeholders = ",".join("?" * len(req.trade_ids))
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM paper_trade_history WHERE id IN ({placeholders})", req.trade_ids
        ).fetchall()]
        if not rows:
            raise HTTPException(404, "no matching trade_history rows")

        conn.execute(f"DELETE FROM paper_trade_history WHERE id IN ({placeholders})", req.trade_ids)

        affected_keys = sorted({(r.get("tag") or "manual") for r in rows})
        wallets = {}
        for key in affected_keys:
            wallet = conn.execute("SELECT * FROM paper_wallets WHERE wallet_key = ?", (key,)).fetchone()
            starting = wallet["starting_balance"] if wallet else 1000.0
            remaining = conn.execute(
                "SELECT net_pnl FROM paper_trade_history WHERE COALESCE(tag,'manual') = ? ORDER BY exit_time ASC",
                (key,),
            ).fetchall()
            balance = starting
            peak = starting
            for t in remaining:
                balance += t["net_pnl"]
                peak = max(peak, balance)
            conn.execute(
                "INSERT INTO paper_wallets (wallet_key, starting_balance, balance, peak_balance, created_at) "
                "VALUES (?,?,?,?,datetime('now')) "
                "ON CONFLICT(wallet_key) DO UPDATE SET balance = excluded.balance, peak_balance = excluded.peak_balance",
                (key, starting, balance, peak),
            )
            wallets[key] = {"balance": balance, "peak_balance": peak}
        conn.commit()
        return {"deleted_ids": [r["id"] for r in rows], "wallets": wallets}
    finally:
        conn.close()
