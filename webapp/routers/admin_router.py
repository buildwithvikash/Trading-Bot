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
