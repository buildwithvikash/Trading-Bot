"""Session-aware transaction cost model."""

from __future__ import annotations

import pandas as pd

from .config import CostConfig


def _in_window(ts: pd.Timestamp, window: tuple[str, str]) -> bool:
    start_h, start_m = (int(x) for x in window[0].split(":"))
    end_h, end_m = (int(x) for x in window[1].split(":"))
    minutes = ts.hour * 60 + ts.minute
    return (start_h * 60 + start_m) <= minutes <= (end_h * 60 + end_m)


def spread_at(ts: pd.Timestamp, cfg: CostConfig) -> float:
    """Spread in USD/oz at a given UTC timestamp."""
    spread = cfg.base_spread
    if _in_window(ts, cfg.news_window):
        spread *= cfg.news_window_multiplier
    elif _in_window(ts, cfg.rollover_window):
        spread *= cfg.rollover_multiplier
    return spread


def entry_cost(ts: pd.Timestamp, cfg: CostConfig) -> float:
    """Cost charged on entry, in USD/oz: half the spread plus slippage."""
    return spread_at(ts, cfg) / 2.0 + cfg.slippage_per_side


def exit_cost(ts: pd.Timestamp, cfg: CostConfig) -> float:
    return spread_at(ts, cfg) / 2.0 + cfg.slippage_per_side


def roundtrip_cost(entry_ts: pd.Timestamp, exit_ts: pd.Timestamp, cfg: CostConfig) -> float:
    """Total round-trip cost in USD per ounce."""
    return entry_cost(entry_ts, cfg) + exit_cost(exit_ts, cfg)
