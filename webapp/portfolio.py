"""One shared wallet for every auto-trading strategy, plus the portfolio-level
entry rules that only make sense once strategies share money.

Before this, each strategy traded its own $10k wallet, so nothing stopped
three strategies from opening the same trade at once, or one strategy going
long while another went short. Now every AutoTrader sizes against, and is
limited by, SHARED_WALLET. Each order/position/trade still carries its
strategy name in `tag` (so per-strategy history and the optimizer report
keep working); the new `wallet_key` column says whose money it is. Rows
written before this change have wallet_key NULL and keep counting toward
their old per-strategy wallet (see WALLET_SQL), so the old trade log stays
intact as archived wallets instead of being merged or deleted.

Manual trades from the order ticket are untouched — they never set
wallet_key and keep the 'manual' wallet.

Limits come from the Oct 2026 review of 194 paper trades (20 Sep - 5 Oct):
one same-direction entry per 30 minutes (de-duplicating cut drawdown from
18.8R to 12.3R for 0.9R of profit), never long and short at once, at most
2 open, 6 trades a day / 3 per session, and stop for the day at -3R or after
3 straight losses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from gold_bot.sessions import session_of

SHARED_WALLET = "portfolio"

# whose money a row is: the explicit wallet if it has one, else the
# pre-shared-wallet rule (one wallet per strategy tag, 'manual' if untagged)
WALLET_SQL = "COALESCE(wallet_key, tag, 'manual')"

MAX_OPEN = 2               # open positions + pending entry orders
MAX_TRADES_PER_DAY = 6
MAX_TRADES_PER_SESSION = 3
DAILY_STOP_R = -3.0
MAX_CONSECUTIVE_LOSSES = 3
DEDUPE_MINUTES = 30


def _utc(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _entries_on(conn, wallet: str, day: str) -> list[pd.Timestamp]:
    """Entry times of every trade this wallet opened on `day` (UTC date),
    whether still open or already closed."""
    rows = conn.execute(
        f"SELECT entry_time FROM paper_positions WHERE {WALLET_SQL} = ? AND substr(entry_time,1,10) = ? "
        f"UNION ALL SELECT entry_time FROM paper_trade_history WHERE {WALLET_SQL} = ? AND substr(entry_time,1,10) = ?",
        (wallet, day, wallet, day),
    ).fetchall()
    return [_utc(r[0]) for r in rows]


def entry_block_reason(conn, wallet: str, ts) -> str | None:
    """Wallet-wide reasons not to look for a new entry at all on this bar."""
    if wallet != SHARED_WALLET:
        return None
    ts = _utc(ts)
    day = ts.date().isoformat()

    open_now = conn.execute(
        f"SELECT (SELECT COUNT(*) FROM paper_positions WHERE {WALLET_SQL} = ?) + "
        f"(SELECT COUNT(*) FROM paper_orders WHERE status = 'pending' AND {WALLET_SQL} = ?)",
        (wallet, wallet),
    ).fetchone()[0]
    if open_now >= MAX_OPEN:
        return f"portfolio already has {open_now} open/pending trades (max {MAX_OPEN})"

    closed_today = [r[0] for r in conn.execute(
        f"SELECT r_multiple FROM paper_trade_history WHERE {WALLET_SQL} = ? AND substr(exit_time,1,10) = ? "
        "ORDER BY exit_time ASC",
        (wallet, day),
    ).fetchall()]
    day_r = sum(r or 0.0 for r in closed_today)
    if day_r <= DAILY_STOP_R:
        return f"daily stop hit ({day_r:+.2f}R today, limit {DAILY_STOP_R:+.0f}R)"
    tail = closed_today[-MAX_CONSECUTIVE_LOSSES:]
    if len(tail) == MAX_CONSECUTIVE_LOSSES and all((r or 0.0) <= 0 for r in tail):
        return f"{MAX_CONSECUTIVE_LOSSES} losses in a row today — done for the day"

    entries = _entries_on(conn, wallet, day)
    if len(entries) >= MAX_TRADES_PER_DAY:
        return f"{len(entries)} trades already taken today (max {MAX_TRADES_PER_DAY})"
    session = session_of(ts)
    in_session = sum(1 for e in entries if session_of(e) == session)
    if in_session >= MAX_TRADES_PER_SESSION:
        return f"{in_session} trades already taken this {session} session (max {MAX_TRADES_PER_SESSION})"
    return None


def order_conflict(conn, wallet: str, direction: int, ts) -> str | None:
    """Reasons to drop one specific signal: it would hedge an open trade, or
    duplicate a same-direction entry another strategy just took."""
    if wallet != SHARED_WALLET:
        return None
    ts = _utc(ts)
    opposite = conn.execute(
        f"SELECT (SELECT COUNT(*) FROM paper_positions WHERE {WALLET_SQL} = ? AND direction = ?) + "
        f"(SELECT COUNT(*) FROM paper_orders WHERE status = 'pending' AND {WALLET_SQL} = ? AND direction = ?)",
        (wallet, -direction, wallet, -direction),
    ).fetchone()[0]
    if opposite:
        return "the portfolio already holds the opposite direction (no hedging)"

    if conn.execute(
        f"SELECT COUNT(*) FROM paper_orders WHERE status = 'pending' AND {WALLET_SQL} = ? AND direction = ?",
        (wallet, direction),
    ).fetchone()[0]:
        return "a same-direction entry is already pending"
    since = ts - pd.Timedelta(minutes=DEDUPE_MINUTES)
    rows = conn.execute(
        f"SELECT entry_time FROM paper_positions WHERE {WALLET_SQL} = ? AND direction = ? "
        f"UNION ALL SELECT entry_time FROM paper_trade_history WHERE {WALLET_SQL} = ? AND direction = ?",
        (wallet, direction, wallet, direction),
    ).fetchall()
    if any(since <= _utc(r[0]) <= ts + pd.Timedelta(minutes=DEDUPE_MINUTES) for r in rows):
        return f"a same-direction entry was taken within {DEDUPE_MINUTES} minutes (same signal twice)"
    return None


# ---------------------------------------------------------------------- #
# one-time switch-over from per-strategy wallets
# ---------------------------------------------------------------------- #
MIGRATION_NAME = "shared_portfolio_wallet_v1"
START_BALANCE = 1_000.0
RISK_PCT = 1.0  # $10 a trade: the smallest that still sizes to 0.01 lot on a $1,000 wallet

# Entry windows are on the SIGNAL bar's open time (15min bars, UTC); the
# fill lands one bar later. Donchian: entries 00:00-07:45 only. EMA: no
# entries 07:00-11:59 (London). RSI Extremes: any time.
CORE_SESSIONS = {
    "rsi_extremes": None,
    "donchian_breakout": {"start": "23:45", "end": "07:30"},
    "ema_cross_rsi": {"start": "11:45", "end": "06:30"},
}


def _with_session(params: dict, session: dict | None) -> dict:
    params = json.loads(json.dumps(params))
    spec = params.setdefault("spec", {})
    if session is None:
        spec.pop("session", None)
    else:
        spec["session"] = session
    return params


def migrate(conn) -> list[str]:
    """Runs once per database: keeps only the three core strategies running,
    applies their entry windows and 1% risk, opens the shared $1,000 wallet,
    and turns off the daily optimizer (it would otherwise start re-tuning
    risk/RR after 10 trades). Trade history is never touched."""
    conn.execute("CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM app_migrations WHERE name = ?", (MIGRATION_NAME,)).fetchone():
        return []
    now = datetime.now(timezone.utc).isoformat()
    notes: list[str] = []

    saved = {r["wallet_key"]: dict(r) for r in conn.execute("SELECT * FROM autotrade_saved").fetchall()}
    stopped = sorted(k for k in saved if k not in CORE_SESSIONS)
    if stopped:
        conn.execute(
            f"DELETE FROM autotrade_saved WHERE wallet_key NOT IN ({','.join('?' * len(CORE_SESSIONS))})",
            tuple(CORE_SESSIONS),
        )
        notes.append(f"stopped: {', '.join(stopped)}")

    configs = {}
    for r in conn.execute("SELECT id, params_json FROM strategies WHERE strategy_class = 'custom_rule'").fetchall():
        params = json.loads(r["params_json"])
        name = (params.get("spec") or {}).get("strategy_name")
        if name in CORE_SESSIONS:
            configs[name] = (r["id"], params)

    for name, session in CORE_SESSIONS.items():
        if name in saved:
            params = _with_session(json.loads(saved[name]["params_json"]), session)
            conn.execute(
                "UPDATE autotrade_saved SET params_json = ?, risk_pct = ?, updated_at = ? WHERE wallet_key = ?",
                (json.dumps(params), RISK_PCT, now, name),
            )
        elif name in configs:
            params = _with_session(configs[name][1], session)
            conn.execute(
                "INSERT INTO autotrade_saved (wallet_key, strategy_id, params_json, risk_pct, updated_at) "
                "VALUES (?, 'custom_rule', ?, ?, ?)",
                (name, json.dumps(params), RISK_PCT, now),
            )
        else:
            notes.append(f"could not start {name}: no saved config found")
            continue
        if name in configs:
            conn.execute(
                "UPDATE strategies SET params_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(_with_session(configs[name][1], session)), now, configs[name][0]),
            )
    notes.append(f"running: {', '.join(CORE_SESSIONS)} at {RISK_PCT}% risk")

    cancelled = conn.execute(
        "UPDATE paper_orders SET status = 'cancelled', cancelled_at = ? "
        "WHERE status = 'pending' AND wallet_key IS NULL AND tag IS NOT NULL",
        (now,),
    ).rowcount
    if cancelled:
        notes.append(f"cancelled {cancelled} pending order(s) from the old per-strategy wallets")
    still_open = conn.execute("SELECT COUNT(*) FROM paper_positions WHERE wallet_key IS NULL AND tag IS NOT NULL").fetchone()[0]
    if still_open:
        notes.append(f"{still_open} open position(s) stay in their old per-strategy wallets until they close")

    conn.execute(
        "INSERT INTO paper_wallets (wallet_key, starting_balance, balance, peak_balance, leverage, created_at) "
        "VALUES (?, ?, ?, ?, 100.0, ?) ON CONFLICT(wallet_key) DO UPDATE SET "
        "starting_balance = excluded.starting_balance, balance = excluded.balance, peak_balance = excluded.peak_balance",
        (SHARED_WALLET, START_BALANCE, START_BALANCE, START_BALANCE, now),
    )
    notes.append(f"shared wallet '{SHARED_WALLET}' opened with ${START_BALANCE:,.0f}")

    cap = conn.execute("SELECT max_risk_per_trade_pct FROM risk_settings WHERE id = 1").fetchone()
    if cap is not None and cap[0] < RISK_PCT:
        conn.execute("UPDATE risk_settings SET max_risk_per_trade_pct = ? WHERE id = 1", (RISK_PCT,))
        notes.append(f"raised the risk-per-trade cap from {cap[0]}% to {RISK_PCT}%")

    conn.execute(
        "INSERT INTO optimizer_settings (id, enabled) VALUES (1, 0) ON CONFLICT(id) DO UPDATE SET enabled = 0"
    )
    notes.append("daily optimizer turned off so it doesn't re-tune the new settings")

    conn.execute("INSERT INTO app_migrations (name, applied_at) VALUES (?, ?)", (MIGRATION_NAME, now))
    return notes
