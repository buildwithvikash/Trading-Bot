"""Bars for the chart. Backed by real data when a CSV exists in data/,
else the synthetic generator — see gold_bot.data.load_best_available.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query

from gold_bot.data import load_best_available, resample, slice_period
from webapp import biquote_client, goldprice

router = APIRouter()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"

# UI timeframe token -> (pandas resample alias, minutes)
TF_MAP = {
    "1min": ("1min", 1),
    "5min": ("5min", 5),
    "15min": ("15min", 15),
    "30min": ("30min", 30),
    "1h": ("1h", 60),
    "4h": ("4h", 240),
    "1d": ("1D", 1440),
}

_cache: dict = {}


def _raw():
    if "raw" not in _cache:
        df, meta = load_best_available(DATA_DIR)
        _cache["raw"] = (df, meta)
    return _cache["raw"]


def _bars_payload(df: pd.DataFrame) -> list[dict]:
    return [
        {
            "time": int(ts.timestamp()),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v),
        }
        for ts, o, h, l, c, v in zip(
            df.index, df["open"], df["high"], df["low"], df["close"], df["volume"]
        )
    ]


@router.get("/meta")
def get_meta():
    df, meta = _raw()
    return {
        **meta,
        "start": df.index[0].isoformat(),
        "end": df.index[-1].isoformat(),
        "bars": len(df),
        "timeframes": list(TF_MAP.keys()),
    }


@router.get("/bars")
def get_bars(
    tf: str = Query("15min", description="one of 1min,5min,15min,1h,4h,1d"),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(6000, le=20000),
):
    df, meta = _raw()
    if tf not in TF_MAP:
        return {"error": f"unknown timeframe '{tf}'", "timeframes": list(TF_MAP.keys())}

    alias, minutes = TF_MAP[tf]
    base_minutes = meta.get("base_minutes")
    warning = None
    effective_tf = tf

    if base_minutes and minutes < base_minutes:
        # can't manufacture resolution finer than the source data actually has
        warning = (
            f"data source is {base_minutes}-minute bars; '{tf}' would just "
            f"duplicate them, so showing the native resolution instead"
        )
        out = df
        effective_tf = [k for k, (a, m) in TF_MAP.items() if m == base_minutes]
        effective_tf = effective_tf[0] if effective_tf else tf
    elif minutes == base_minutes:
        out = df
    else:
        out = resample(df, alias)

    out = slice_period(out, start, end)
    if len(out) > limit:
        out = out.iloc[-limit:]

    return {
        "meta": meta,
        "tf": tf,
        "effective_tf": effective_tf,
        "warning": warning,
        "bars": _bars_payload(out),
    }


@router.get("/live-bars")
def get_live_bars(tf: str = Query("15min")):
    """Recent real bars straight from biquote.io (forming bar included), for
    the live chart. No local/historical data is mixed in."""
    if tf not in TF_MAP:
        return {"error": f"unknown timeframe '{tf}'", "timeframes": list(TF_MAP.keys())}
    raw = biquote_client.get_ohlc(tf, limit=5000)
    if not raw:
        return {"error": "Live market data is unavailable right now (biquote.io unreachable).", "bars": []}
    bars = [
        {
            "time": int(pd.Timestamp(b["openTime"]).timestamp()),
            "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume": float(b.get("tickVolume") or 0),
        }
        for b in raw
    ]
    return {"tf": tf, "warning": None, "bars": bars}


@router.get("/spot-check")
def spot_check():
    """Independent real-world XAU/USD spot price from gold-api.com, as a
    cross-check next to whatever feed (biquote or local data) the app is
    actually trading against. Cached server-side for a few minutes regardless
    of how often this is polled — see webapp.goldprice."""
    return goldprice.get_spot_price()
