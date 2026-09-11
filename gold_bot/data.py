"""Data loading. Everything is normalised to a tz-aware UTC DatetimeIndex with
columns open/high/low/close/volume.

Supported inputs
----------------
* Dukascopy CSV export  -> "Gmt time,Open,High,Low,Close,Volume"
* MetaTrader 5 export   -> tab separated "<DATE> <TIME> <OPEN> ..."
* Generic CSV           -> any column named time/timestamp/date/datetime

IMPORTANT: check what timezone your broker's data is in. MT5 servers are
usually GMT+2/+3, which shifts every session boundary in this codebase. Pass
`source_tz` if your file is not already UTC/GMT.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

_OHLC = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_TF_MINUTES = {
    "m1": 1, "1min": 1, "1m": 1,
    "m5": 5, "5min": 5, "5m": 5,
    "m15": 15, "15min": 15, "15m": 15,
    "m30": 30, "30min": 30, "30m": 30,
    "h1": 60, "1h": 60, "60min": 60,
    "h4": 240, "4h": 240, "240min": 240,
    "d1": 1440, "1d": 1440, "daily": 1440,
}


def load_csv(
    path: str | Path,
    source_tz: str | None = None,
    dayfirst: bool = True,
) -> pd.DataFrame:
    path = Path(path)
    sep = "\t" if path.suffix.lower() in {".tsv"} else None
    df = pd.read_csv(path, sep=sep, engine="python")

    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]

    # find / build the timestamp column
    if "date" in df.columns and "time" in df.columns and "gmt time" not in df.columns:
        ts = df["date"].astype(str) + " " + df["time"].astype(str)
        df = df.drop(columns=["date", "time"])
    else:
        cand = [c for c in df.columns if c in {"gmt time", "timestamp", "datetime", "date", "time", "local time"}]
        if not cand:
            raise ValueError(f"No timestamp column found in {path.name}: {list(df.columns)}")
        ts = df[cand[0]].astype(str)
        df = df.drop(columns=[cand[0]])

    index = pd.to_datetime(ts, dayfirst=dayfirst, format="mixed")
    df.index = index

    rename = {}
    for col in list(df.columns):
        for want in ("open", "high", "low", "close", "volume"):
            if col.startswith(want) or col == want[0]:
                rename[col] = want
    df = df.rename(columns=rename)

    missing = {"open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns {missing} in {path.name}")
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = df[["open", "high", "low", "close", "volume"]].astype(float)

    if df.index.tz is None:
        df.index = df.index.tz_localize(source_tz or "UTC")
    df.index = df.index.tz_convert("UTC")

    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    return df


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample to e.g. '5min', '15min', '1h', '4h'. Left-labelled, left-closed:
    a bar stamped 13:30 covers 13:30:00-13:44:59.
    """
    out = df.resample(timeframe, label="left", closed="left").agg(_OHLC).dropna()
    return out


def slice_period(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]
    return df


def _guess_minutes(stem: str) -> int | None:
    tokens = re.sub(r"[^a-z0-9]", "_", stem.lower()).split("_")
    for tok in tokens:
        if tok in _TF_MINUTES:
            return _TF_MINUTES[tok]
    return None


def find_finest_csv(data_dir: str | Path) -> tuple[Path, int | None] | None:
    """Look in `data_dir` for a CSV named with a timeframe token (XAUUSD_M1.csv,
    XAUUSD_15min.csv, ...) and return the finest one found, so coarser
    timeframes can be a real resample of it rather than a separate file.
    Falls back to any single CSV present (timeframe unknown) if none match.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return None
    tagged = []
    for f in sorted(data_dir.glob("*.csv")):
        minutes = _guess_minutes(f.stem)
        if minutes is not None:
            tagged.append((minutes, f))
    if tagged:
        tagged.sort(key=lambda x: x[0])
        return tagged[0][1], tagged[0][0]
    untagged = sorted(data_dir.glob("*.csv"))
    return (untagged[0], None) if untagged else None


def load_best_available(
    data_dir: str | Path = "data", source_tz: str | None = None
) -> tuple[pd.DataFrame, dict]:
    """Real data if a CSV is present in `data_dir`, else the synthetic generator.

    Returns (df, meta) where meta = {"source": "real"|"synthetic",
    "file": str|None, "base_minutes": int|None} so callers (and the UI) can be
    honest about what's actually backing the numbers.
    """
    found = find_finest_csv(data_dir)
    if found is None:
        from .sample_data import generate

        df = generate()
        return df, {"source": "synthetic", "file": None, "base_minutes": 15}

    path, minutes = found
    df = load_csv(path, source_tz=source_tz)
    return df, {"source": "real", "file": str(path), "base_minutes": minutes}


def sanity_report(df: pd.DataFrame) -> str:
    """Things worth eyeballing before you trust any backtest run on this data."""
    gaps = df.index.to_series().diff().dropna()
    typical = gaps.mode().iloc[0] if not gaps.empty else pd.Timedelta(0)
    big = gaps[gaps > typical * 4]
    weekend = big[big.index.dayofweek.isin([0, 6])]
    lines = [
        f"bars          : {len(df):,}",
        f"period        : {df.index[0]} -> {df.index[-1]}",
        f"bar size      : {typical}",
        f"price range   : {df['low'].min():.2f} - {df['high'].max():.2f}",
        f"gaps > 4 bars : {len(big)} (of which {len(weekend)} at weekends, expected)",
        f"zero-range bars: {(df['high'] == df['low']).sum()}",
    ]
    return "\n".join(lines)
