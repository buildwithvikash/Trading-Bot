"""Single-user login, for once this app is reachable beyond localhost.

Deliberately minimal: one username/password pair from env vars (APP_USERNAME/
APP_PASSWORD — there is no multi-user concept anywhere else in this app), an
opaque random session token stored in SQLite, and a plain httponly cookie
holding that token. No JWT/itsdangerous — the token carries no data to
forge, so nothing needs signing; it's just a lookup key.

If APP_USERNAME/APP_PASSWORD aren't set (plain local dev), auth is disabled
entirely — see is_configured() — so `python webapp/server.py` on your own
machine behaves exactly as before.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from webapp import db

COOKIE_NAME = "session"
SESSION_DAYS = 30


def is_configured() -> bool:
    return bool(os.environ.get("APP_USERNAME")) and bool(os.environ.get("APP_PASSWORD"))


def check_credentials(username: str, password: str) -> bool:
    expected_user = os.environ.get("APP_USERNAME", "")
    expected_pass = os.environ.get("APP_PASSWORD", "")
    # both comparisons always run (constant-time) so a wrong username can't
    # be distinguished from a wrong password by response timing
    ok_user = secrets.compare_digest(username, expected_user)
    ok_pass = secrets.compare_digest(password, expected_pass)
    return ok_user and ok_pass


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (token, created_at, expires_at) VALUES (?,?,?)",
            (token, now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def validate_session(token: str | None) -> bool:
    if not token:
        return False
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
        if row is None:
            return False
        return datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
    finally:
        conn.close()


def delete_session(token: str | None) -> None:
    if not token:
        return
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def purge_expired_sessions() -> None:
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
    finally:
        conn.close()
