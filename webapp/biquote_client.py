"""Server-side client for biquote.io (https://pypi.org/project/biquote/) —
a free, keyless market-data service backed by a real MetaTrader 5 feed.

This is the app's live feed source: real bid/ask ticks AND genuine recent
OHLC history (unlike gold-api.com, a single spot price with no history at
all) — so a freshly (re)started Live/Demo session has real continuity right
up to now instead of a bare live tick with nothing behind it. No API key
needed; anonymous quota is 15,000 requests/min per IP, far more than this
app's ~2-second poll cadence will ever approach.

biquote's own interval tokens ("1m","5m","15m","30m","1h","4h","1d") differ
from this app's internal timeframe strings ("1min","5min",...) — TF_ALIAS
below maps between them.
"""

from __future__ import annotations

import requests
from biquote import Biquote, BiquoteError

SYMBOL = "XAUUSD"
TF_ALIAS = {"1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}

# BiquoteError only covers HTTP-level failures (biquote._get raises it for a
# non-2xx response) — a transport failure (DNS, connect/read timeout,
# connection reset) comes straight out of `requests` unwrapped. Every caller
# here treats get_tick()/get_ohlc() as "None/[] on any failure" (that's what
# lets AutoTrader.start() fall back to simulated mode instead of crashing),
# so both exception families have to be caught the same way.
_NETWORK_ERRORS = (BiquoteError, requests.exceptions.RequestException)

_client = Biquote()


def get_tick() -> dict | None:
    try:
        return _client.tick(SYMBOL)
    except _NETWORK_ERRORS:
        return None


def get_ohlc(tf: str, limit: int = 500) -> list:
    """Bars oldest-first; the last one is still forming (isOpen=True) unless
    biquote currently has no data at all for this interval. Empty list on
    any error or an unrecognized tf."""
    interval = TF_ALIAS.get(tf)
    if interval is None:
        return []
    try:
        return _client.ohlc(SYMBOL, interval=interval, limit=limit)
    except _NETWORK_ERRORS:
        return []
