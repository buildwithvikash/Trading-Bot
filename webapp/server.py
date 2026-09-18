#!/usr/bin/env python3
"""Gold Trading Terminal — web app entrypoint.

    python webapp/server.py

Serves the API under /api/* and the static frontend at /. Binds to
127.0.0.1 by default (not an internet service) — set PORT (Railway/Render/
Fly.io all do this automatically) to switch to 0.0.0.0 for a real deploy.

Login is gated behind APP_USERNAME/APP_PASSWORD (see webapp.auth) — if
neither is set, auth is skipped entirely so local dev is unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from webapp import auth, db
from webapp.routers import admin_router, ai, auth_router, backtest, market, paper, risk, strategies

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

db.init_db()
auth.purge_expired_sessions()

app = FastAPI(title="Gold Trading Terminal")

# paths reachable with no session — the login page itself, the endpoint that
# creates a session, and the handful of static assets the login page needs
# to not render as a plain unstyled form.
_PUBLIC_PATHS = {"/login", "/api/auth/login"}
_PUBLIC_PREFIXES = ("/css/", "/js/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth.is_configured():
            return await call_next(request)  # no APP_USERNAME/APP_PASSWORD set — local dev, auth off
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)
        if auth.validate_session(request.cookies.get(auth.COOKIE_NAME)):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return RedirectResponse(f"/login?next={path}")


app.add_middleware(AuthMiddleware)

app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin_router.router, prefix="/api/admin", tags=["admin"])
app.include_router(market.router, prefix="/api/market", tags=["market"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["backtest"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
app.include_router(risk.router, prefix="/api/risk", tags=["risk"])
app.include_router(paper.router, prefix="/api/paper", tags=["paper"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])



@app.get("/login")
def login_page():
    return FileResponse(FRONTEND_DIR / "login.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


@app.on_event("startup")
def _resume_autotrades() -> None:
    paper.resume_saved_autotrades()


if __name__ == "__main__":
    import uvicorn

    # Railway/Render/Fly.io all inject PORT; local dev never sets it, so the
    # default stays 127.0.0.1:8000 exactly as before.
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    # Railway terminates TLS at its edge and proxies plain HTTP to this
    # container, so request.url.scheme would otherwise always read "http"
    # (even for a visitor on https://) — proxy_headers trusts Railway's own
    # X-Forwarded-Proto so the login cookie's Secure flag is set correctly.
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
