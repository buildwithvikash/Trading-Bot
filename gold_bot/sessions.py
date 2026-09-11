"""Session tagging and filtering, shared by the backtest API and metrics splits.

Buckets are mutually exclusive (so a split_report by session sums to 100%):
London runs 07:00-16:00 UTC and New York 12:00-21:00 UTC, so their shared hours
(12:00-16:00) get their own "overlap" bucket rather than double-counting into
both. Everything outside those hours (21:00-07:00 UTC) is the Asian/Sydney-
Tokyo session. When a caller asks to backtest "the London session" or "the NY
session", that means the whole real-world window including the overlap
hours — see SESSION_MEMBERSHIP below.
"""

from __future__ import annotations

import pandas as pd

# mutually-exclusive post-hoc bucket for a timestamp
def session_of(ts: pd.Timestamp) -> str:
    h = ts.hour
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "overlap"
    if 16 <= h < 21:
        return "ny"
    return "asian"


# which buckets count as "in session" when filtering entries to a chosen
# real-world session (the overlap belongs to both London and NY)
SESSION_MEMBERSHIP = {
    "london": {"london", "overlap"},
    "ny": {"ny", "overlap"},
    "overlap": {"overlap"},
    "asian": {"asian"},
}


def tag_sessions(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty:
        return trades
    t = trades.copy()
    t["session"] = t["entry_time"].apply(session_of)
    return t


class SessionFilterStrategy:
    """Wraps any Strategy so new entries only fire inside a chosen session.

    Indicators still see the full continuous series — only `generate()` is
    gated, the same way the existing strategies already gate on their own
    trade_start/trade_end windows. This lets ANY strategy be session-filtered
    without touching its class.
    """

    def __init__(self, inner, session: str):
        self.inner = inner
        self.session = session
        self.name = inner.name

    def prepare(self, df):
        return self.inner.prepare(df)

    def generate(self, df, i, state):
        allowed = SESSION_MEMBERSHIP.get(self.session)
        if allowed is not None and session_of(df.index[i]) not in allowed:
            return []
        return self.inner.generate(df, i, state)
