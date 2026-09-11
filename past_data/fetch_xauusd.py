"""
Fetch XAUUSD historical bars and write them in the same format as the CSVs
that came with this script (timestamp, open, high, low, close, volume, spread).

Source: https://github.com/Dypoi/XAUUSD_Dataset
        M1 bid/ask bars, UTC timestamps, one file per Sep-to-Sep year,
        currently covering 2016-09-01 through 2026-09-01.

Usage:
    python fetch_xauusd.py                      # last 2 years, M5 (default)
    python fetch_xauusd.py --years 2025 2026 --tf 1min
    python fetch_xauusd.py --years 2024 2025 2026 --tf 1h --out data/

"years" refers to the END year of each file, e.g. 2026 -> 2025-09-01..2026-09-01.

Requires: pandas, requests
"""

import argparse
import io
import os

import pandas as pd
import requests

BASE = "https://raw.githubusercontent.com/Dypoi/XAUUSD_Dataset/main"
FIRST_YEAR, LAST_YEAR = 2017, 2026

AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
    "spread": "mean",
}


def load_year(end_year: int) -> pd.DataFrame:
    """Download one Sep-to-Sep M1 file and reduce bid/ask to mid prices."""
    name = f"XAUUSD_M1_{end_year - 1}0901_{end_year}0901.csv"
    url = f"{BASE}/{name}"
    print(f"  downloading {name} ...", flush=True)
    r = requests.get(url, timeout=300)
    r.raise_for_status()

    raw = pd.read_csv(io.BytesIO(r.content), parse_dates=["timestamp"])
    out = pd.DataFrame(index=raw["timestamp"])
    for col in ("open", "high", "low", "close"):
        out[col] = ((raw[f"{col}_bid"].values + raw[f"{col}_ask"].values) / 2).round(3)
    out["volume"] = (raw["volume_bid"].values + raw["volume_ask"].values).round(4)
    out["spread"] = (raw["close_ask"].values - raw["close_bid"].values).round(3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2025, 2026],
                    help=f"end years to fetch, {FIRST_YEAR}-{LAST_YEAR}")
    ap.add_argument("--tf", default="5min",
                    help="pandas offset alias: 1min, 5min, 15min, 1h, 4h, 1D")
    ap.add_argument("--out", default=".", help="output directory")
    args = ap.parse_args()

    for y in args.years:
        if not FIRST_YEAR <= y <= LAST_YEAR:
            raise SystemExit(f"year {y} is outside the available range "
                             f"{FIRST_YEAR}-{LAST_YEAR}")

    print(f"Fetching {len(args.years)} year file(s) at {args.tf}:")
    m1 = pd.concat([load_year(y) for y in sorted(args.years)])
    m1 = m1[~m1.index.duplicated()].sort_index()

    if args.tf in ("1min", "1T"):
        bars = m1.copy()
    else:
        bars = (m1.resample(args.tf, label="left", closed="left")
                  .agg(AGG)
                  .dropna(subset=["open"]))
        bars["spread"] = bars["spread"].round(3)
        bars["volume"] = bars["volume"].round(4)

    os.makedirs(args.out, exist_ok=True)
    start = bars.index.min().date()
    end = bars.index.max().date()
    path = os.path.join(args.out, f"XAUUSD_{args.tf}_{start}_{end}.csv")
    bars.reset_index().to_csv(path, index=False)

    print(f"\n{len(bars):,} bars  {start} -> {end}")
    print(f"written to {path}")


if __name__ == "__main__":
    main()
