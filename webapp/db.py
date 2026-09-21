"""SQLite persistence for the terminal. One file, opened per-request — this is
a single-user local app, not a service under concurrent write load.

Phase 3 adds the `strategies` table (saved configs). Later phases (paper
trading) extend SCHEMA with accounts/positions/orders/trade_history rather
than introducing a second database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Overridable via DB_PATH so a deploy can point this at a mounted volume
# WITHOUT that volume's mount path colliding with data/'s read-only CSV
# price history baked into the image — an empty volume mounted directly on
# top of data/ hides those CSVs, silently falling back to synthetic data.
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "terminal.sqlite3")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    strategy_class TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- single-row table: the terminal's default risk settings (Phase 4). Applied
-- as defaults when opening the Backtest view and, from Phase 5, to size and
-- gate paper-trading orders.
CREATE TABLE IF NOT EXISTS risk_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    risk_per_trade_pct REAL NOT NULL DEFAULT 0.5,
    max_risk_per_trade_pct REAL NOT NULL DEFAULT 2.0,
    max_daily_loss_pct REAL,
    max_drawdown_pct REAL,
    max_open_positions INTEGER NOT NULL DEFAULT 1,
    contract_size REAL NOT NULL DEFAULT 100.0,
    min_lot REAL NOT NULL DEFAULT 0.01,
    max_lot REAL NOT NULL DEFAULT 50.0,
    default_sl_atr_mult REAL NOT NULL DEFAULT 1.5,
    default_tp_rr REAL NOT NULL DEFAULT 2.0,
    updated_at TEXT NOT NULL
);

-- Phase 5: paper trading. One account (single-user local app).
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_balance REAL NOT NULL DEFAULT 10000.0,
    balance REAL NOT NULL DEFAULT 10000.0,
    peak_balance REAL NOT NULL DEFAULT 10000.0,
    leverage REAL NOT NULL DEFAULT 100.0,
    created_at TEXT NOT NULL
);

-- One wallet per strategy tag (manual trades use wallet_key='manual') —
-- replaces paper_account's single shared balance so each strategy's P&L,
-- position sizing and risk limits are tracked independently. paper_account
-- above is kept only as the migration source for the pre-existing 'manual'
-- wallet (see init_db) so nobody's balance silently resets.
CREATE TABLE IF NOT EXISTS paper_wallets (
    wallet_key TEXT PRIMARY KEY,
    starting_balance REAL NOT NULL DEFAULT 10000.0,
    balance REAL NOT NULL DEFAULT 10000.0,
    peak_balance REAL NOT NULL DEFAULT 10000.0,
    leverage REAL NOT NULL DEFAULT 100.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- market | stop | limit
    direction INTEGER NOT NULL,       -- 1 long, -1 short
    trigger_price REAL,               -- null for market
    stop_loss REAL NOT NULL,
    take_profit REAL,
    lots REAL NOT NULL,
    units REAL NOT NULL,
    risk_amount REAL,
    tag TEXT,
    oco_group TEXT,                   -- strategy orders only: cancel siblings on fill
    expiry TEXT,                      -- strategy orders only: cancel if still pending past this
    time_exit TEXT,                   -- carried onto the position once filled
    trail_atr_mult REAL,              -- carried onto the position once filled
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | filled | cancelled
    created_at TEXT NOT NULL,
    filled_at TEXT,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    direction INTEGER NOT NULL,
    units REAL NOT NULL,
    lots REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    initial_stop REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL,
    risk_per_oz REAL NOT NULL,
    risk_amount REAL NOT NULL,
    entry_cost_per_oz REAL NOT NULL,
    tag TEXT,
    time_exit TEXT,
    trail_atr_mult REAL
);

-- Which auto-trade strategies should be running, so a server restart can
-- bring them back up on its own instead of silently leaving every wallet
-- stopped until someone re-clicks Start in the UI for each one.
CREATE TABLE IF NOT EXISTS autotrade_saved (
    wallet_key TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    risk_pct REAL,
    updated_at TEXT NOT NULL
);

-- Login sessions, once the app is exposed beyond localhost (see webapp.auth).
-- The cookie only ever holds this opaque token — never the password — so a
-- server restart just means everyone has to log in again, nothing worse.
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction INTEGER NOT NULL,
    units REAL NOT NULL,
    lots REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_price REAL NOT NULL,
    exit_time TEXT NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL,
    gross_pnl REAL NOT NULL,
    costs REAL NOT NULL,
    net_pnl REAL NOT NULL,
    r_multiple REAL,
    exit_reason TEXT NOT NULL,
    tag TEXT
);
CREATE TABLE IF NOT EXISTS optimizer_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS optimizer_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    trigger TEXT NOT NULL,            -- scheduled | manual
    strategies_reviewed INTEGER NOT NULL DEFAULT 0,
    changes_applied INTEGER NOT NULL DEFAULT 0,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS optimizer_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    wallet_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    trades INTEGER NOT NULL DEFAULT 0,
    win_rate REAL,
    profit_factor REAL,
    expectancy_r REAL,
    net_pnl REAL,
    finding TEXT NOT NULL,
    action TEXT NOT NULL,             -- none | deferred | adjust_rr | adjust_risk | pause
    param TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    expected_effect TEXT,
    applied INTEGER NOT NULL DEFAULT 0
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, coldef: str) -> None:
    """ALTER TABLE ... ADD COLUMN is not part of CREATE TABLE IF NOT EXISTS,
    so a database file created before a column was added needs this to
    catch up. Safe to call every startup — no-ops once the column exists."""
    col = coldef.split()[0]
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        for coldef in ("oco_group TEXT", "expiry TEXT", "time_exit TEXT", "trail_atr_mult REAL"):
            _add_column_if_missing(conn, "paper_orders", coldef)
        for coldef in ("time_exit TEXT", "trail_atr_mult REAL"):
            _add_column_if_missing(conn, "paper_positions", coldef)
        for coldef in ("default_sl_atr_mult REAL NOT NULL DEFAULT 1.5", "default_tp_rr REAL NOT NULL DEFAULT 2.0"):
            _add_column_if_missing(conn, "risk_settings", coldef)
        conn.execute(
            "INSERT OR IGNORE INTO risk_settings (id, updated_at) VALUES (1, datetime('now'))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO paper_account (id, created_at) VALUES (1, datetime('now'))"
        )
        # NOTE: no migration copies paper_account's old aggregate balance
        # into a wallet here — webapp.routers.paper._ensure_wallet
        # reconstructs each wallet (including 'manual') from that wallet's
        # OWN tagged trade history on first touch, which is what actually
        # keeps per-wallet numbers correct. Copying the old shared balance
        # into 'manual' directly double-counts every other wallet's P&L
        # once it's ALSO reconstructed from the same underlying trades.
        conn.commit()
    finally:
        conn.close()
