"""Paper (demo) trading: a persistent virtual account driven by the
simulated feed (webapp.feed.FEED). Every fill/exit goes through the exact
same rules as the batch backtester (gold_bot.engine.fill_price/exit_events,
via gold_bot.paper_engine) so a paper trade never behaves differently from
what a backtest would have done with the same order.

Never places a real broker order — that is out of scope by design (a
separate, explicitly-gated feature would be required; see requirement #10
in the project brief). This account is play money, always.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gold_bot import paper_engine
from gold_bot.config import CostConfig, EngineConfig
from gold_bot.costs import exit_cost
from gold_bot.indicators import atr
from gold_bot.risk import position_size
from gold_bot.sessions import session_of
from webapp import biquote_client, db
from webapp.autotrade import ACTIVITY, AUTOTRADERS
from webapp.feed import FEED
from webapp.routers.market import TF_MAP, _raw

router = APIRouter()

FEED.configure(_raw, TF_MAP, tf="15min")


def _json_safe(d: dict) -> dict:
    return {
        k: (None if isinstance(v, float) and (math.isinf(v) or math.isnan(v)) else v)
        for k, v in d.items()
    }


# ------------------------------------------------------------------ #
# per-strategy wallets — one virtual balance per tag ('manual' for
# untagged/manually-placed orders), so each strategy's P&L, position
# sizing and daily-loss/drawdown limits are tracked independently instead
# of all sharing one pool.
# ------------------------------------------------------------------ #
def _wallet_key(tag: str | None) -> str:
    return tag if tag else "manual"


def _ensure_wallet(conn, key: str, default_balance: float = 10_000.0) -> None:
    """Creates the wallet on first touch — reconstructed from that tag's own
    trade history if any already exists (e.g. a strategy that traded before
    wallets existed, or before this specific wallet was ever explicitly
    opened), rather than silently starting it at the default and losing
    track of P&L that already happened."""
    if conn.execute("SELECT 1 FROM paper_wallets WHERE wallet_key = ?", (key,)).fetchone():
        return
    trades = conn.execute(
        "SELECT net_pnl FROM paper_trade_history WHERE COALESCE(tag,'manual') = ? ORDER BY exit_time ASC", (key,)
    ).fetchall()
    balance = default_balance
    peak = default_balance
    for t in trades:
        balance += t["net_pnl"]
        peak = max(peak, balance)
    conn.execute(
        """INSERT OR IGNORE INTO paper_wallets
           (wallet_key, starting_balance, balance, peak_balance, leverage, created_at)
           VALUES (?, ?, ?, ?, 100.0, ?)""",
        (key, default_balance, balance, peak, datetime.now(timezone.utc).isoformat()),
    )


def _ensure_all_known_wallets(conn) -> None:
    """Every tag that has ever appeared in a trade, open position, or
    pending order gets a wallet — so the Dashboard's wallet table doesn't
    silently omit a strategy just because it hasn't traded since the
    wallets feature shipped."""
    keys = {"manual"}
    for table in ("paper_trade_history", "paper_positions"):
        keys |= {r[0] for r in conn.execute(f"SELECT DISTINCT COALESCE(tag,'manual') FROM {table}").fetchall()}
    keys |= {r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(tag,'manual') FROM paper_orders WHERE status='pending'"
    ).fetchall()}
    for key in keys:
        _ensure_wallet(conn, key)


def _get_wallet(conn, key: str) -> dict:
    _ensure_wallet(conn, key)
    return dict(conn.execute("SELECT * FROM paper_wallets WHERE wallet_key = ?", (key,)).fetchone())


def _wallet_stats(conn, key: str, wallet: dict, price: dict | None) -> dict:
    """Everything the Dashboard's per-wallet row (or the account/stats
    endpoints, scoped to one wallet) needs — equity, margin, and the same
    win-rate/profit-factor/drawdown numbers /stats has always reported,
    just filtered to this wallet's own trades."""
    positions = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE COALESCE(tag, 'manual') = ?", (key,)
    ).fetchall()]
    floating = 0.0
    margin_used = 0.0
    if price is not None:
        for p in positions:
            floating += paper_engine.floating_pnl(CostConfig(), p, price["mid"])
            margin_used += (p["units"] * price["mid"]) / wallet["leverage"]
    equity = wallet["balance"] + floating

    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_trade_history WHERE COALESCE(tag, 'manual') = ? ORDER BY exit_time ASC", (key,)
    ).fetchall()]
    n = len(trades)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    curve = [wallet["starting_balance"]]
    for t in trades:
        curve.append(curve[-1] + t["net_pnl"])
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    today = datetime.now(timezone.utc).date().isoformat()
    today_pnl = conn.execute(
        "SELECT COALESCE(SUM(net_pnl),0) s FROM paper_trade_history "
        "WHERE substr(exit_time,1,10) = ? AND COALESCE(tag,'manual') = ?",
        (today, key),
    ).fetchone()["s"]

    return _json_safe({
        "wallet_key": key,
        "starting_balance": wallet["starting_balance"],
        "balance": wallet["balance"],
        "peak_balance": wallet["peak_balance"],
        "leverage": wallet["leverage"],
        "equity": round(equity, 2),
        "floating_pnl": round(floating, 2),
        "margin_used": round(margin_used, 2),
        "free_margin": round(equity - margin_used, 2),
        "open_positions": len(positions),
        "trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1) if n else None,
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "max_drawdown_pct": round(-max_dd, 2),
        "today_pnl": round(today_pnl, 2),
        "total_pnl": round(sum(t["net_pnl"] for t in trades), 2),
    })


# ------------------------------------------------------------------ #
# tick processing — the ONLY code path that fills orders / closes positions
# ------------------------------------------------------------------ #
def _fill_order(conn, order_row: dict, price: float, ts, costs_cfg: CostConfig):
    ec = paper_engine.entry_cost_at(ts, costs_cfg)
    risk_per_oz = abs(price - order_row["stop_loss"])
    conn.execute(
        "UPDATE paper_orders SET status='filled', filled_at=? WHERE id=?",
        (ts.isoformat(), order_row["id"]),
    )
    if order_row.get("oco_group"):
        # one side of a strategy's OCO pair (e.g. ny_orb's buy-stop/sell-stop)
        # just filled — the other side never fires against a real platform
        conn.execute(
            "UPDATE paper_orders SET status='cancelled', cancelled_at=? "
            "WHERE oco_group=? AND status='pending' AND id != ?",
            (ts.isoformat(), order_row["oco_group"], order_row["id"]),
        )
    conn.execute(
        """INSERT INTO paper_positions
           (order_id, direction, units, lots, entry_price, entry_time, initial_stop, stop_loss,
            take_profit, risk_per_oz, risk_amount, entry_cost_per_oz, tag, time_exit, trail_atr_mult)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            order_row["id"], order_row["direction"], order_row["units"], order_row["lots"],
            price, ts.isoformat(), order_row["stop_loss"], order_row["stop_loss"],
            order_row["take_profit"], risk_per_oz, order_row["risk_amount"] or 0.0, ec,
            order_row.get("tag"), order_row.get("time_exit"), order_row.get("trail_atr_mult"),
        ),
    )
    if order_row.get("tag"):
        side = "LONG" if order_row["direction"] == 1 else "SHORT"
        ACTIVITY.add("fill", f"{order_row['tag']}: {side} filled @ {price:.2f}, {order_row['lots']} lots, SL {order_row['stop_loss']:.2f}")


def _close_position(conn, pos_row: dict, exit_info: dict, ts):
    conn.execute("DELETE FROM paper_positions WHERE id = ?", (pos_row["id"],))
    conn.execute(
        """INSERT INTO paper_trade_history
           (direction, units, lots, entry_price, entry_time, exit_price, exit_time,
            stop_loss, take_profit, gross_pnl, costs, net_pnl, r_multiple, exit_reason, tag)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pos_row["direction"], pos_row["units"], pos_row["lots"], pos_row["entry_price"],
            pos_row["entry_time"], exit_info["price"], ts.isoformat(), pos_row["stop_loss"],
            pos_row["take_profit"], exit_info["gross_pnl"], exit_info["costs"],
            exit_info["net_pnl"], exit_info["r_multiple"], exit_info["reason"], pos_row.get("tag"),
        ),
    )
    key = _wallet_key(pos_row.get("tag"))
    wallet = _get_wallet(conn, key)
    new_balance = wallet["balance"] + exit_info["net_pnl"]
    new_peak = max(wallet["peak_balance"], new_balance)
    conn.execute(
        "UPDATE paper_wallets SET balance = ?, peak_balance = ? WHERE wallet_key = ?",
        (new_balance, new_peak, key),
    )
    if pos_row.get("tag"):
        ACTIVITY.add(
            "exit",
            f"{pos_row['tag']}: closed ({exit_info['reason']}) @ {exit_info['price']:.2f}, "
            f"P&L {exit_info['net_pnl']:+.2f} ({exit_info['r_multiple']:+.2f}R)",
            pnl=exit_info["net_pnl"],
        )


def _on_tick(bar: dict | None):
    """Driven by webapp.feed.FEED's own bar close — handles the "manual"
    wallet (the chart order ticket has no strategy/timeframe of its own to
    poll independently) and doubles as an extra, harmless fill/close check
    for every OTHER wallet too, since checking a pending order or an open
    position's stop/target against more bars than strictly necessary never
    causes a double-fill or double-close (status/row already
    filled/closed is simply skipped). Trailing-stop updates and signal
    generation are NOT done here — those need each strategy's own ATR and
    own bar cadence, which only that strategy's own AutoTrader runner has
    (see webapp.autotrade.AutoTrader._process_bar) — a single strategy
    used to own both jobs when only one could run at a time; now that
    several can run concurrently at different timeframes, each does its
    own."""
    if bar is None:
        return
    ts, o, h, l, c = bar["time"], bar["open"], bar["high"], bar["low"], bar["close"]
    costs_cfg = CostConfig()
    engine_cfg = EngineConfig()
    conn = db.get_conn()
    try:
        # strategy orders can carry an expiry (ny_orb's OCO pair expires
        # unfilled at 16:00) — drop those before attempting fills this bar
        conn.execute(
            "UPDATE paper_orders SET status='cancelled', cancelled_at=? "
            "WHERE status='pending' AND expiry IS NOT NULL AND expiry < ?",
            (ts.isoformat(), ts.isoformat()),
        )

        for row in conn.execute("SELECT * FROM paper_orders WHERE status = 'pending'").fetchall():
            r = dict(row)
            price = paper_engine.try_fill_order(r, o, h, l)
            if price is not None:
                _fill_order(conn, r, price, ts, costs_cfg)

        for row in conn.execute("SELECT * FROM paper_positions").fetchall():
            r = dict(row)
            exit_info = paper_engine.try_close_position(costs_cfg, engine_cfg, r, ts, h, l, c)
            if exit_info is not None:
                _close_position(conn, r, exit_info, ts)

        conn.commit()
    finally:
        conn.close()


FEED.subscribe(_on_tick)


# ------------------------------------------------------------------ #
# feed control
# ------------------------------------------------------------------ #
@router.get("/feed/status")
def feed_status():
    return FEED.status()


@router.post("/feed/start")
def feed_start(source: str | None = None):
    if source not in (None, "simulated", "biquote"):
        raise HTTPException(400, "source must be 'simulated' or 'biquote'")
    FEED.start(source=source)
    return FEED.status()


@router.post("/feed/stop")
def feed_stop():
    FEED.stop()
    return FEED.status()


class FeedSpeedRequest(BaseModel):
    seconds_per_bar: float


@router.put("/feed/speed")
def feed_speed(req: FeedSpeedRequest):
    FEED.set_speed(req.seconds_per_bar)
    return FEED.status()


@router.get("/feed/bar")
def feed_bar():
    """Current feed bar in lightweight-charts' time format (unix seconds),
    so the Charts view can call candleSeries.update() with it directly for
    a live-updating chart instead of re-fetching the whole history."""
    bar = FEED.current_bar()
    if bar is None:
        raise HTTPException(409, "feed is not running")
    return {
        "time": int(bar["time"].timestamp()),
        "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
    }


@router.get("/feed/forming")
def feed_forming_bar():
    """The in-progress bar, updated from live biquote.io ticks — unlike
    /feed/bar (only ever a fully closed bar, which is what a strategy is
    allowed to see), this is purely for the Chart view so the current
    candle visibly moves between closes instead of sitting frozen for up
    to a full timeframe interval. Always at the feed's own fixed working
    timeframe — see /feed/live for any-timeframe chart viewing."""
    bar = FEED.current_forming_bar()
    if bar is None:
        raise HTTPException(409, "no live forming bar available (not connected to biquote, or no tick yet)")
    return {
        "time": int(bar["time"].timestamp()),
        "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
    }


@router.get("/feed/live")
def feed_live(tf: str):
    """Live bar + forming bar straight from biquote at WHATEVER timeframe
    the Chart view is currently displaying — independent of the feed's own
    fixed working timeframe (which auto-trade's signal generation depends
    on and never changes). biquote can serve any of its supported
    intervals on demand, so there's no reason live movement should be
    locked to one timeframe; this is what makes every timeframe pill tick
    live instead of just the one matching FEED.tf."""
    if FEED.status()["source"] != "biquote":
        raise HTTPException(409, "not connected to biquote")
    bars = biquote_client.get_ohlc(tf, limit=2)
    if not bars:
        raise HTTPException(409, f"biquote has no data for timeframe '{tf}' right now")

    def _payload(b):
        return {
            "time": int(pd.Timestamp(b["openTime"]).timestamp()),
            "open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]), "close": float(b["close"]),
        }

    closed = [b for b in bars if not b.get("isOpen")]
    forming = [b for b in bars if b.get("isOpen")]
    return {
        "bar": _payload(closed[-1]) if closed else None,
        "forming": _payload(forming[-1]) if forming else None,
    }


@router.get("/price")
def get_price():
    p = FEED.current_price()
    if p is None:
        raise HTTPException(409, "feed is not running")
    return p


@router.get("/atr")
def get_atr(period: int = 14):
    """Current ATR at the feed's own working timeframe — real recent
    biquote bars layered onto local history when connected live (same
    gap-closing approach AutoTrader.start uses), the local historical
    dataset alone otherwise. This is NOT what any strategy trades on —
    strategies always compute their own ATR internally — it exists purely
    to suggest a stop-loss distance on the manual order ticket via
    risk_settings.default_sl_atr_mult."""
    raw = FEED.dataframe()
    if FEED.status()["source"] == "biquote":
        recent = FEED.recent_history(count=period + 50)
        if recent is not None and len(recent):
            raw = pd.concat([raw, recent])
            raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    if raw is None or len(raw) < period + 1:
        raise HTTPException(409, "not enough data to compute ATR yet")
    val = atr(raw, period).iloc[-1]
    if not math.isfinite(val):
        raise HTTPException(409, "ATR not available yet")
    return {"atr": round(float(val), 3), "period": period, "timeframe": FEED.tf}


# ------------------------------------------------------------------ #
# strategy auto-trading — runs Strategy.generate() one bar at a time and
# places/manages paper orders the same way a human would. Several
# strategies can run CONCURRENTLY (webapp.autotrade.AutoTraderManager),
# each fully independent: own wallet, own timeframe, own background loop —
# started/stopped/inspected individually, addressed by wallet key.
# ------------------------------------------------------------------ #
class AutoTradeStartRequest(BaseModel):
    strategy: str
    params: dict = {}
    risk_pct: float | None = None


@router.get("/autotrade/status")
def autotrade_status():
    """Every strategy started this session, running or stopped."""
    return AUTOTRADERS.status_list()


def _save_running(wallet_key: str, strategy_id: str, params: dict, risk_pct: float | None) -> None:
    """Remember that this wallet should be running so a server restart can
    bring it back up on its own — see resume_saved_autotrades()."""
    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO autotrade_saved (wallet_key, strategy_id, params_json, risk_pct, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(wallet_key) DO UPDATE SET
                 strategy_id=excluded.strategy_id, params_json=excluded.params_json,
                 risk_pct=excluded.risk_pct, updated_at=excluded.updated_at""",
            (wallet_key, strategy_id, json.dumps(params), risk_pct, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _forget_running(wallet_key: str | None = None) -> None:
    """Drop the saved-running record(s) — called on an explicit user Stop so
    a restart doesn't resurrect a strategy they meant to leave off."""
    conn = db.get_conn()
    try:
        if wallet_key is None:
            conn.execute("DELETE FROM autotrade_saved")
        else:
            conn.execute("DELETE FROM autotrade_saved WHERE wallet_key = ?", (wallet_key,))
        conn.commit()
    finally:
        conn.close()


@router.post("/autotrade/start")
def autotrade_start(req: AutoTradeStartRequest):
    try:
        runner = AUTOTRADERS.start(req.strategy, req.params, risk_pct=req.risk_pct)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    _save_running(runner.wallet_key, req.strategy, req.params, req.risk_pct)
    return runner.status()


@router.post("/autotrade/stop")
def autotrade_stop(wallet: str):
    try:
        AUTOTRADERS.stop(wallet)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    _forget_running(wallet)
    return AUTOTRADERS.status_list()


@router.post("/autotrade/stop-all")
def autotrade_stop_all():
    AUTOTRADERS.stop_all()
    _forget_running()
    return AUTOTRADERS.status_list()


def resume_saved_autotrades() -> None:
    """Called once at server startup: restart every strategy that was
    running when the process last stopped, so a restart (crash, redeploy,
    a dev re-running `python webapp/server.py`) no longer silently leaves
    every wallet disarmed until someone re-clicks Start for each one —
    that gap is what was causing live signals to be missed entirely."""
    conn = db.get_conn()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM autotrade_saved").fetchall()]
    finally:
        conn.close()
    for row in rows:
        try:
            AUTOTRADERS.start(row["strategy_id"], json.loads(row["params_json"]), risk_pct=row["risk_pct"])
        except Exception as exc:
            ACTIVITY.add(
                "error",
                f"could not auto-resume wallet '{row['wallet_key']}' ({row['strategy_id']}) on startup: {exc!r}",
            )


@router.get("/autotrade/activity")
def autotrade_activity(limit: int = 100):
    """Plain-language feed of what every running (or recently stopped)
    strategy is actually doing — signals evaluated, orders filled,
    positions closed and why — newest first, one shared feed across all of
    them (each message names its own strategy/wallet)."""
    return ACTIVITY.list(limit)


@router.get("/autotrade/indicators")
def autotrade_indicators(wallet: str | None = None, count: int = 300):
    """One running strategy's own indicator values (EMA/RSI/PDH/swing
    levels/...), classified into price-scale overlays vs oscillators, for
    the Chart view to draw directly. Pass `wallet` to pick which running
    strategy — omitted, this falls back to the first currently-running one
    for convenience. Empty dict (not an error) when nothing matches — this
    is a "nice to have" the chart degrades gracefully without."""
    if wallet is not None:
        runner = AUTOTRADERS.get(wallet)
    else:
        running = AUTOTRADERS.running()
        runner = running[0] if running else None
    if runner is None:
        return {"price_series": {}, "oscillator_series": {}, "state": {}}
    return runner.plot_data(count=count)


@router.get("/autotrade/setup")
def autotrade_setup(wallet: str | None = None):
    """What each running strategy's own state machine is currently doing —
    stage, side, how many bars ago each anchor point in that stage
    happened, and (for strategies that track one) the funnel counters
    showing how often each stage has ever been reached. Pass `wallet` for
    one strategy; omitted, returns every currently-running one. This is
    the "why hasn't it traded, and what's it actually waiting for" view —
    not every strategy has a multi-bar setup to report (see
    AutoTrader.setup_status's docstring), in which case `setup` is null."""
    if wallet is not None:
        runner = AUTOTRADERS.get(wallet)
        if runner is None or not runner.enabled:
            raise HTTPException(404, f"no running strategy for wallet '{wallet}'")
        return runner.setup_status()
    return AUTOTRADERS.setup_list()


# ------------------------------------------------------------------ #
# account / wallets / positions / orders / history
# ------------------------------------------------------------------ #
@router.get("/account")
def get_account(wallet: str = "manual"):
    """The order ticket and Chart-view ticket's own wallet — 'manual' by
    default (untagged orders), or pass ?wallet=<strategy tag> to check a
    specific strategy's wallet the same way."""
    conn = db.get_conn()
    try:
        w = _get_wallet(conn, wallet)
        conn.commit()
        stats = _wallet_stats(conn, wallet, w, FEED.current_price())
        return {**stats, "feed_running": FEED.is_running()}
    finally:
        conn.close()


@router.get("/wallets")
def list_wallets():
    """Every known wallet (one per strategy tag that's ever traded, plus
    'manual') with its own balance/equity/win-rate/drawdown — the Dashboard's
    per-strategy breakdown."""
    conn = db.get_conn()
    try:
        _ensure_all_known_wallets(conn)
        conn.commit()
        price = FEED.current_price()
        rows = conn.execute("SELECT * FROM paper_wallets ORDER BY wallet_key").fetchall()
        return [_wallet_stats(conn, r["wallet_key"], dict(r), price) for r in rows]
    finally:
        conn.close()


@router.post("/wallets/{wallet_key}/reset")
def reset_wallet(wallet_key: str, starting_balance: float = 10_000.0):
    """Resets ONE wallet — its balance and its own orders/positions/trade
    history — leaving every other strategy's wallet untouched."""
    conn = db.get_conn()
    try:
        conn.execute(
            "DELETE FROM paper_orders WHERE COALESCE(tag,'manual') = ?", (wallet_key,)
        )
        conn.execute(
            "DELETE FROM paper_positions WHERE COALESCE(tag,'manual') = ?", (wallet_key,)
        )
        conn.execute(
            "DELETE FROM paper_trade_history WHERE COALESCE(tag,'manual') = ?", (wallet_key,)
        )
        _ensure_wallet(conn, wallet_key, starting_balance)
        conn.execute(
            "UPDATE paper_wallets SET starting_balance = ?, balance = ?, peak_balance = ? WHERE wallet_key = ?",
            (starting_balance, starting_balance, starting_balance, wallet_key),
        )
        conn.commit()
        return _wallet_stats(conn, wallet_key, dict(conn.execute(
            "SELECT * FROM paper_wallets WHERE wallet_key = ?", (wallet_key,)
        ).fetchone()), FEED.current_price())
    finally:
        conn.close()


@router.get("/positions")
def get_positions():
    conn = db.get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_positions ORDER BY entry_time DESC"
        ).fetchall()]
        price = FEED.current_price()
        for r in rows:
            if price is not None:
                fp = paper_engine.floating_pnl(CostConfig(), r, price["mid"])
                r["floating_pnl"] = round(fp, 2)
                r["current_price"] = price["mid"]
                r["r_multiple"] = round(fp / r["risk_amount"], 3) if r["risk_amount"] else None
            else:
                r["floating_pnl"] = None
                r["current_price"] = None
                r["r_multiple"] = None
        return rows
    finally:
        conn.close()


@router.post("/positions/{position_id}/close")
def close_position_manual(position_id: int):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM paper_positions WHERE id = ?", (position_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "position not found")
        bar = FEED.current_bar()
        price = FEED.current_price()
        if bar is None or price is None:
            raise HTTPException(409, "feed is not running")

        pos = dict(row)
        exit_price = price["bid"] if pos["direction"] == 1 else price["ask"]
        ts = bar["time"]
        costs_cfg = CostConfig()
        ec = exit_cost(ts, costs_cfg)
        gross = (exit_price - pos["entry_price"]) * pos["direction"] * pos["units"]
        cost = (pos["entry_cost_per_oz"] + ec) * pos["units"]
        net = gross - cost
        risk_amount = pos.get("risk_amount") or (pos["risk_per_oz"] * pos["units"])
        r_multiple = net / risk_amount if risk_amount else 0.0
        exit_info = {
            "reason": "manual_close", "price": exit_price, "units": pos["units"],
            "gross_pnl": gross, "costs": cost, "net_pnl": net, "r_multiple": r_multiple,
        }
        _close_position(conn, pos, exit_info, ts)
        conn.commit()
        return {"closed": position_id, "net_pnl": round(net, 2)}
    finally:
        conn.close()


@router.get("/orders")
def get_orders():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _risk_block_reason(conn, settings: dict, wallet: dict, key: str) -> str | None:
    """Shared by the manual order ticket and the strategy auto-trader — both
    must respect the same daily-loss/drawdown circuit breakers, scoped to
    the wallet placing the order so one strategy hitting its daily loss
    limit doesn't halt every other strategy's wallet too."""
    today = datetime.now(timezone.utc).date().isoformat()
    if settings["max_daily_loss_pct"] is not None:
        today_pnl = conn.execute(
            "SELECT COALESCE(SUM(net_pnl),0) s FROM paper_trade_history "
            "WHERE substr(exit_time,1,10) = ? AND COALESCE(tag,'manual') = ?",
            (today, key),
        ).fetchone()["s"]
        day_pct = 100 * today_pnl / wallet["balance"] if wallet["balance"] else 0
        if day_pct <= -settings["max_daily_loss_pct"]:
            return f"daily loss limit ({settings['max_daily_loss_pct']}%) already hit today"
    if settings["max_drawdown_pct"] is not None and wallet["peak_balance"] > 0:
        dd_pct = 100 * (wallet["balance"] - wallet["peak_balance"]) / wallet["peak_balance"]
        if dd_pct <= -settings["max_drawdown_pct"]:
            return f"account drawdown limit ({settings['max_drawdown_pct']}%) breached"
    return None


class PlaceOrderRequest(BaseModel):
    kind: str  # market | stop | limit
    direction: int  # 1 long, -1 short
    trigger_price: float | None = None
    stop_loss: float
    take_profit: float | None = None
    risk_pct: float | None = None
    lots: float | None = None
    tag: str | None = None


@router.post("/orders")
def place_order(req: PlaceOrderRequest):
    if req.kind not in ("market", "stop", "limit"):
        raise HTTPException(400, "kind must be market, stop, or limit")
    if req.kind != "market" and req.trigger_price is None:
        raise HTTPException(400, "trigger_price is required for stop/limit orders")
    if req.direction not in (1, -1):
        raise HTTPException(400, "direction must be 1 (long) or -1 (short)")

    price = FEED.current_price()
    if price is None:
        raise HTTPException(409, "feed is not running — start it first")
    ref_price = req.trigger_price if req.kind != "market" else (
        price["ask"] if req.direction == 1 else price["bid"]
    )

    if req.direction == 1 and req.stop_loss >= ref_price:
        raise HTTPException(400, "stop-loss must be below the entry for a long")
    if req.direction == -1 and req.stop_loss <= ref_price:
        raise HTTPException(400, "stop-loss must be above the entry for a short")

    key = _wallet_key(req.tag)
    conn = db.get_conn()
    try:
        wallet = _get_wallet(conn, key)
        settings = dict(conn.execute("SELECT * FROM risk_settings WHERE id = 1").fetchone())

        open_count = conn.execute(
            "SELECT COUNT(*) c FROM paper_positions WHERE COALESCE(tag,'manual') = ?", (key,)
        ).fetchone()["c"]
        if open_count >= settings["max_open_positions"]:
            raise HTTPException(
                409, f"blocked: already at the max open positions limit ({settings['max_open_positions']}) for this wallet"
            )
        reason = _risk_block_reason(conn, settings, wallet, key)
        if reason:
            raise HTTPException(409, f"blocked: {reason}")

        risk_per_oz = abs(ref_price - req.stop_loss)
        if req.lots is not None:
            lots = req.lots
            risk_amount = lots * settings["contract_size"] * risk_per_oz
        else:
            risk_pct = req.risk_pct if req.risk_pct is not None else settings["risk_per_trade_pct"]
            lots, risk_amount = position_size(
                wallet["balance"], risk_pct, risk_per_oz,
                contract_size=settings["contract_size"], min_lot=settings["min_lot"],
                max_lot=settings["max_lot"], max_risk_pct=settings["max_risk_per_trade_pct"],
            )
        if lots <= 0:
            raise HTTPException(
                400, "position size rounds to zero — risk % too small, stop too wide, or balance too low"
            )

        units = lots * settings["contract_size"]
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO paper_orders
               (kind, direction, trigger_price, stop_loss, take_profit, lots, units, risk_amount,
                tag, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'pending',?)""",
            (
                req.kind, req.direction, req.trigger_price, req.stop_loss, req.take_profit,
                lots, units, risk_amount, req.tag, now,
            ),
        )
        order_id = cur.lastrowid
        conn.commit()

        if req.kind == "market":
            bar = FEED.current_bar()
            order_row = dict(conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,)).fetchone())
            fill = paper_engine.try_fill_order(order_row, bar["open"], bar["high"], bar["low"])
            if fill is not None:
                _fill_order(conn, order_row, fill, bar["time"], CostConfig())
                conn.commit()

        return dict(conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,)).fetchone())
    finally:
        conn.close()


@router.delete("/orders/{order_id}")
def cancel_order(order_id: int):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM paper_orders WHERE id = ? AND status = 'pending'", (order_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "pending order not found")
        conn.execute(
            "UPDATE paper_orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), order_id),
        )
        conn.commit()
        return {"cancelled": order_id}
    finally:
        conn.close()


@router.get("/history")
def get_history(limit: int = 200, tag: str | None = None):
    conn = db.get_conn()
    try:
        if tag is not None:
            rows = conn.execute(
                "SELECT * FROM paper_trade_history WHERE COALESCE(tag,'manual') = ? ORDER BY exit_time DESC LIMIT ?",
                (tag, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trade_history ORDER BY exit_time DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["session"] = session_of(pd.Timestamp(d["entry_time"]))
            out.append(d)
        return out
    finally:
        conn.close()


@router.get("/stats")
def get_stats(wallet: str = "manual"):
    """Same numbers as /account's stats fields, scoped to one wallet —
    kept as its own endpoint since it predates wallets and some callers
    only want the stats half."""
    conn = db.get_conn()
    try:
        w = _get_wallet(conn, wallet)
        conn.commit()
        stats = _wallet_stats(conn, wallet, w, FEED.current_price())
        return {k: stats[k] for k in ("trades", "win_rate_pct", "profit_factor", "max_drawdown_pct", "today_pnl", "total_pnl")}
    finally:
        conn.close()


@router.post("/reset")
def reset_account(starting_balance: float = 10_000.0):
    """Resets EVERY wallet (all strategies plus manual) — the nuclear
    option. To reset just one strategy's wallet, use
    POST /wallets/{wallet_key}/reset instead."""
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM paper_orders")
        conn.execute("DELETE FROM paper_positions")
        conn.execute("DELETE FROM paper_trade_history")
        keys = [r["wallet_key"] for r in conn.execute("SELECT wallet_key FROM paper_wallets").fetchall()] or ["manual"]
        for key in keys:
            _ensure_wallet(conn, key, starting_balance)
            conn.execute(
                "UPDATE paper_wallets SET starting_balance = ?, balance = ?, peak_balance = ? WHERE wallet_key = ?",
                (starting_balance, starting_balance, starting_balance, key),
            )
        conn.execute(
            "UPDATE paper_account SET starting_balance = ?, balance = ?, peak_balance = ? WHERE id = 1",
            (starting_balance, starting_balance, starting_balance),
        )
        conn.commit()
        return {"reset_wallets": keys, "starting_balance": starting_balance}
    finally:
        conn.close()
