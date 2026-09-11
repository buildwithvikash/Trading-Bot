"""Login/logout endpoints — see webapp.auth for the session mechanism and
webapp.server's auth middleware for how everything else is gated on it."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from webapp import auth

router = APIRouter()


@router.get("/status")
def status():
    """Whether login is actually enforced — so the frontend can hide the
    Logout button in plain local dev (no APP_USERNAME/APP_PASSWORD set)
    instead of sending someone to a login screen they have no way past."""
    return {"enabled": auth.is_configured()}


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    if not auth.check_credentials(req.username, req.password):
        raise HTTPException(401, "invalid username or password")
    token = auth.create_session()
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    auth.delete_session(session)
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}
