"""Performance metrics.

The headline number here is EXPECTANCY IN R, not win rate. A system with an
80% win rate and a 0.3R average winner is worse than a 45% system with a 2R
winner, and gold's spread will kill the first one long before it kills the
second.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _safe(x, default=0.0):
    return float(x) if np.isfinite(x) else default


def summarise(trades: pd.DataFrame, equity: pd.Series, initial_equity: float) -> dict:
    if trades is None or trades.empty:
        return {"trades": 0, "note": "no trades generated"}

    r = trades["r_multiple"].to_numpy(float)
    wins = r[r > 0]
    losses = r[r <= 0]
    n = len(r)

    gross_win = trades.loc[trades["net_pnl"] > 0, "net_pnl"].sum()
    gross_loss = -trades.loc[trades["net_pnl"] <= 0, "net_pnl"].sum()

    eq = equity.dropna()
    peak = eq.cummax()
    dd = (eq - peak) / peak
    max_dd = float(dd.min()) if len(dd) else 0.0

    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    sharpe = _safe(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0

    days = max((eq.index[-1] - eq.index[0]).days, 1)
    total_ret = eq.iloc[-1] / initial_equity - 1.0
    cagr = (eq.iloc[-1] / initial_equity) ** (365.25 / days) - 1.0

    # standard error on the win rate — the number people forget
    win_rate = len(wins) / n
    se = np.sqrt(win_rate * (1 - win_rate) / n) if n > 1 else float("nan")

    return {
        "trades": n,
        "win_rate_pct": round(100 * win_rate, 1),
        "win_rate_95ci_pct": (
            f"{100 * max(0, win_rate - 1.96 * se):.1f} - {100 * min(1, win_rate + 1.96 * se):.1f}"
            if np.isfinite(se)
            else "n/a"
        ),
        "expectancy_R": round(float(r.mean()), 4),
        "expectancy_stderr_R": round(float(r.std(ddof=1) / np.sqrt(n)), 4) if n > 1 else None,
        "avg_win_R": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss_R": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "profit_factor": round(_safe(gross_win / gross_loss, np.inf), 3) if gross_loss > 0 else np.inf,
        "total_return_pct": round(100 * total_ret, 2),
        "cagr_pct": round(100 * cagr, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "sharpe_daily_ann": round(sharpe, 2),
        "final_equity": round(float(eq.iloc[-1]), 2),
        "total_costs": round(float(trades["costs"].sum()), 2),
        "costs_pct_of_gross_profit": round(
            100
            * trades["costs"].sum()
            / max(trades.loc[trades["gross_pnl"] > 0, "gross_pnl"].sum(), 1e-9),
            1,
        ),
        "avg_bars_held": round(float(trades["bars_held"].mean()), 1),
        "max_consec_losses": _max_consecutive(r <= 0),
    }


def _max_consecutive(mask) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def split_report(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    """Break results down by year / side / exit reason / news-day."""
    if trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    if by == "year":
        key = t["entry_time"].dt.year
    elif by == "month":
        key = t["entry_time"].dt.to_period("M").astype(str)
    elif by == "side":
        key = t["side"]
    elif by == "exit_reason":
        key = t["exit_reason"]
    elif by == "news_day":
        key = t["news_day"] if "news_day" in t.columns else pd.Series("unknown", index=t.index)
    elif by == "session":
        key = t["session"] if "session" in t.columns else pd.Series("unknown", index=t.index)
    elif by == "hour":
        key = t["entry_time"].dt.hour
    else:
        raise ValueError(by)

    g = t.groupby(key)
    out = pd.DataFrame(
        {
            "trades": g.size(),
            "win_rate_pct": (g["r_multiple"].apply(lambda x: 100 * (x > 0).mean())).round(1),
            "expectancy_R": g["r_multiple"].mean().round(3),
            "total_R": g["r_multiple"].sum().round(2),
            "net_pnl": g["net_pnl"].sum().round(2),
        }
    )
    return out


def tag_news_days(trades: pd.DataFrame, news_dates: list[str] | None = None) -> pd.DataFrame:
    """Flag trades that occurred on high-impact US data days.

    If you don't supply dates, this falls back to a crude proxy: the first
    Friday of each month (NFP). Replace it with a real calendar export from
    ForexFactory or Investing.com before you trust the split.
    """
    if trades.empty:
        return trades
    t = trades.copy()
    if news_dates:
        s = {pd.Timestamp(d).date() for d in news_dates}
        t["news_day"] = t["entry_time"].dt.date.isin(s)
    else:
        d = t["entry_time"].dt
        t["news_day"] = (d.dayofweek == 4) & (d.day <= 7)
    return t


def format_report(name: str, stats: dict, splits: dict[str, pd.DataFrame]) -> str:
    lines = [f"{'=' * 66}", f"  {name}", "=" * 66]
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        lines.append(f"  {k:<{width}} : {v}")
    for title, table in splits.items():
        if table is None or table.empty:
            continue
        lines.append(f"\n  --- by {title} ---")
        lines.append(table.to_string().replace("\n", "\n  "))
    return "\n".join(lines)
