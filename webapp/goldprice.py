"""Server-side client for the gold-api.com spot-price API.

Used purely as an independent, real-world cross-check next to the app's own
feed (biquote or local historical data) — never as the live tick source. No key
required (fully public). MIN_REFRESH_SECONDS still caches server-side so a
page with multiple tabs/frequent reloads doesn't hammer a free public API
that publishes no rate-limit terms of its own.
"""

from __future__ import annotations

import time
from threading import Lock

import requests

API_URL = "https://api.gold-api.com/price/XAU"
MIN_REFRESH_SECONDS = 5 * 60  # no published quota — just a courteous floor

_cache: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def get_spot_price() -> dict:
    """Cached XAU/USD spot price. Only calls the real API when the cache is
    older than MIN_REFRESH_SECONDS; otherwise returns the cached value with
    its original fetch time so the frontend can show how stale it is."""
    with _lock:
        now = time.time()
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < MIN_REFRESH_SECONDS:
            return _cache["data"]

        try:
            resp = requests.get(API_URL, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
            result = {
                "price": float(payload["price"]),
                "updated_at": payload.get("updatedAt"),
                "fetched_at": now,
                "source": "gold-api.com",
            }
            _cache["data"] = result
            _cache["fetched_at"] = now
            return result
        except Exception as exc:
            # a transient upstream hiccup shouldn't blank out a background
            # badge — keep serving the last good value if there is one
            if _cache["data"] is not None:
                return _cache["data"]
            return {"error": str(exc)}


def fetch_live_tick() -> float | None:
    """Direct, UNCACHED call — used by the live feed loop (webapp.feed),
    which needs a fresh price every poll to actually build moving candles.
    Deliberately bypasses get_spot_price()'s cache (that one's for the
    occasional cross-check badge, a completely different job). gold-api.com
    publishes no rate-limit terms, so this polls at the caller's own
    judgment — see webapp/feed.py for the interval chosen and why."""
    try:
        resp = requests.get(API_URL, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None
