"""Synthetic XAUUSD M15 data.

THIS IS FAKE DATA. It exists so you can run the pipeline end-to-end and check
the plumbing before you have real history. Any performance number produced
from it is meaningless — a random walk with a session volatility profile has
no edge in it. Replace with Dukascopy or MT5 data before drawing conclusions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _session_vol(hour: int) -> float:
    # rough XAUUSD intraday volatility profile (UTC)
    if 0 <= hour < 6:
        return 0.45          # Asia: quiet
    if 6 <= hour < 12:
        return 1.00          # London
    if 12 <= hour < 17:
        return 1.60          # London/NY overlap
    if 17 <= hour < 21:
        return 0.90          # NY afternoon
    return 0.35              # rollover / late


def generate(
    start: str = "2023-09-01",
    end: str = "2025-09-01",
    start_price: float = 1_950.0,
    annual_drift: float = 0.28,
    base_vol_per_bar: float = 0.0011,   # ~0.11% per 15m bar at session factor 1.0
    seed: int = 7,
    freq: str = "15min",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq=freq, tz="UTC", inclusive="left")
    idx = idx[idx.dayofweek < 5]                       # no weekends
    idx = idx[~((idx.dayofweek == 4) & (idx.hour >= 21))]
    idx = idx[~((idx.dayofweek == 0) & (idx.hour < 0))]

    n = len(idx)
    bar_minutes = pd.Timedelta(freq).total_seconds() / 60
    bars_per_year = (24 * 60 / bar_minutes) * 252
    drift = annual_drift / bars_per_year

    # base_vol_per_bar is calibrated for a 15-minute bar; scale by sqrt(time)
    # for other frequencies so annualised volatility stays consistent and
    # coarser timeframes are a believable aggregate of finer ones.
    vol_scale = (bar_minutes / 15) ** 0.5
    vol = np.array([_session_vol(h) for h in idx.hour]) * base_vol_per_bar * vol_scale
    # volatility clustering
    shock = rng.normal(0, 1, n)
    garch = np.ones(n)
    for i in range(1, n):
        garch[i] = 0.90 * garch[i - 1] + 0.10 * (1 + abs(shock[i - 1]))
    vol = vol * garch

    log_ret = drift + vol * shock
    close = start_price * np.exp(np.cumsum(log_ret))

    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = close[:-1]

    wick_up = np.abs(rng.normal(0, 1, n)) * vol * close * 0.6
    wick_dn = np.abs(rng.normal(0, 1, n)) * vol * close * 0.6
    high = np.maximum(open_, close) + wick_up
    low = np.minimum(open_, close) - wick_dn
    volume = rng.integers(50, 1500, n).astype(float)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


if __name__ == "__main__":
    df = generate()
    df.to_csv("XAUUSD_M15_SYNTHETIC.csv", index_label="time")
    print(f"wrote {len(df):,} synthetic bars")
