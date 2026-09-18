"""Automated strategy execution against the paper-trading account.

Runs the SAME Strategy.generate() the batch backtester uses, one new bar at
a time, and turns the Order objects it returns into paper_orders exactly
like a human would type them into the order ticket. No code path here ever
reaches a real broker — orders only ever land in the paper_* tables (see
webapp/routers/paper.py).

Signals are generated once per new bar, AFTER that bar's fills/exits have
already been processed for this same strategy's own wallet — so a signal
created at the close of bar i can only fill from bar i+1 onward, the same
no-lookahead rule gold_bot.engine.Backtester enforces.

Multiple strategies can run CONCURRENTLY, each fully independent:
  - its own wallet (fills/exits/trailing-stops scoped to its own tag, never
    touching another strategy's positions or the manual "wallet"),
  - its own working timeframe (Strategy.timeframe / RuleStrategy.timeframe)
    — not tied to webapp.feed.FEED's single shared timeframe at all,
  - its own background thread polling biquote.io directly
    (webapp.biquote_client) for bar closes at ITS OWN timeframe, falling
    back to an independent private replay of the local dataset if biquote
    is unreachable when it starts.
webapp.feed.FEED is only used here to read the local historical dataset as
a seed layer — never for bar-close timing, which each runner does for
itself so concurrent strategies at different timeframes don't collide.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from gold_bot import paper_engine
from gold_bot.config import CostConfig, EngineConfig
from gold_bot.data import resample
from gold_bot.risk import position_size
from gold_bot.strategies import STRATEGY_REGISTRY
from webapp import biquote_client
from webapp.routers.market import TF_MAP, _raw


def local_history(timeframe: str):
    """The local historical dataset resampled to an ARBITRARY timeframe,
    independent of webapp.feed.FEED's own current working timeframe — each
    AutoTrader runner seeds itself at its OWN declared timeframe, not
    whatever the shared feed happens to be showing right now. Mirrors
    webapp.feed.MarketFeed._load()'s own resampling logic exactly."""
    df, meta = _raw()
    alias, minutes = TF_MAP[timeframe]
    base_minutes = meta.get("base_minutes")
    return df if (base_minutes and minutes <= base_minutes) else resample(df, alias)


class ActivityLog:
    """What the strategies are actually doing, in plain language, newest
    first — one shared feed across every concurrently-running strategy
    (each message already names which strategy it's about). Separate from
    paper_trade_history (the permanent record) — this is a short-lived
    in-memory feed for watching decisions happen live; it does not survive
    a server restart and isn't meant to."""

    def __init__(self, maxlen: int = 300):
        self._events = deque(maxlen=maxlen)

    def add(self, kind: str, message: str, **data) -> None:
        self._events.appendleft({
            "time": datetime.now(timezone.utc).isoformat(),
            "kind": kind,  # "info" | "signal" | "fill" | "exit" | "blocked" | "cancel"
            "message": message,
            **data,
        })

    def list(self, limit: int = 100) -> list[dict]:
        return list(self._events)[:limit]


ACTIVITY = ActivityLog()


class AutoTrader:
    """One running strategy: its own dataframe, state, wallet, and a
    private background thread advancing it bar by bar."""

    def __init__(self, strategy_id: str, params: dict, risk_pct: float | None = None):
        if strategy_id not in STRATEGY_REGISTRY:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        cls = STRATEGY_REGISTRY[strategy_id]
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        self.strategy = cls(**clean)  # TypeError on a bad param name/value surfaces to the caller
        self.strategy_id = strategy_id
        self.params = params or {}
        self.risk_pct = risk_pct
        # the wallet a strategy trades against is whatever tag it stamps on
        # its own Orders — custom_rule uses spec.strategy_name (falling back
        # to "custom_rule"), every hand-coded strategy uses its own .name
        self.wallet_key = (
            (clean.get("spec", {}) or {}).get("strategy_name") or "custom_rule"
            if strategy_id == "custom_rule" else getattr(self.strategy, "name", strategy_id)
        )
        self.timeframe = getattr(self.strategy, "timeframe", None) or "15min"

        self.mode: str | None = None   # "biquote" | "simulated" — decided in start()
        self.raw_df = None             # unprepared OHLCV
        self.df = None                 # prepared (indicators added)
        self.state: dict = {}
        self.last_bar_time = None      # biquote mode: last bar already processed
        self.last_i = -1               # simulated mode: position in the private replay
        self.last_block_reason: str | None = None
        self._last_logged_block: str | None = None
        self.started_at: str | None = None
        self.enabled = False

        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    def start(self):
        tick = biquote_client.get_tick()
        if tick is not None:
            recent = _biquote_recent_history(self.timeframe)
            raw = local_history(self.timeframe)
            if recent is not None and len(recent):
                raw = pd.concat([raw, recent])
                raw = raw[~raw.index.duplicated(keep="last")].sort_index()
            if raw is None or len(raw) == 0:
                raise ValueError("no local data to seed the strategy with")
            self.mode = "biquote"
        else:
            raw = local_history(self.timeframe)
            if raw is None or len(raw) == 0:
                raise ValueError("no local data to seed the strategy with, and biquote is unreachable")
            self.mode = "simulated"

        self.raw_df = raw.copy()
        self.df = self.strategy.prepare(self.raw_df.copy())
        self.state = {}
        self.last_i = -1
        self.last_bar_time = None
        self.last_block_reason = None
        self.enabled = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._running = True
        target = self._biquote_loop if self.mode == "biquote" else self._sim_loop
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        ACTIVITY.add("info", f"Started {self.strategy_id} — seeded from {self.mode} data ({len(self.df)} bars), wallet '{self.wallet_key}'")

    def stop(self):
        if self.enabled:
            ACTIVITY.add("info", f"Stopped {self.strategy_id} (wallet '{self.wallet_key}')")
        self.enabled = False
        self._running = False

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "strategy": self.strategy_id,
            "wallet": self.wallet_key,
            "timeframe": self.timeframe,
            "params": self.params,
            "risk_pct": self.risk_pct,
            "mode": self.mode,
            "started_at": self.started_at,
            "last_block_reason": self.last_block_reason,
        }

    @staticmethod
    def _json_safe(val):
        if isinstance(val, pd.Timestamp):
            return val.isoformat()
        if isinstance(val, np.integer):
            return int(val)
        if isinstance(val, np.floating):
            return float(val)
        if isinstance(val, np.bool_):
            return bool(val)
        if isinstance(val, dict):
            return {k: AutoTrader._json_safe(v) for k, v in val.items()}
        return val

    def setup_status(self) -> dict:
        """What this strategy's own state machine (if it has one) is
        currently doing: which stage it's in, how many bars ago each
        anchor point in that stage happened, and — for strategies that
        track one — the funnel counters showing how many times each stage
        has EVER been reached this run. Exists so "why hasn't this taken a
        trade" has an answer besides re-reading the strategy's source: a
        strategy sitting in an early stage with a healthy funnel is just
        selective; one with an empty funnel and no live setup for hours is
        worth actually investigating.

        Not every strategy keeps a multi-bar `state["seq"]` — several
        (fib_retracement, trend_pullback, custom_rule, ...) decide fresh
        from the current bar's indicators with no persistent setup between
        bars, so `setup` is None for those; there's no "waiting for" to
        report because nothing is in flight between bars by design.
        """
        if self.df is None:
            return {
                "wallet": self.wallet_key, "strategy": self.strategy_id,
                "timeframe": self.timeframe, "mode": self.mode,
                "last_bar_time": None, "bars_seen": 0, "setup": None, "funnel": None,
            }
        i = self.last_i if self.mode == "simulated" else len(self.df) - 1
        last_ts = self.df.index[i] if 0 <= i < len(self.df) else None

        seq = self.state.get("seq") if isinstance(self.state, dict) else None
        setup = None
        if seq:
            setup = {k: self._json_safe(v) for k, v in seq.items()}
            if i >= 0:
                for key in list(setup.keys()):
                    if key.endswith("_bar") and isinstance(setup[key], int):
                        setup[f"{key}s_ago"] = i - setup[key]

        return {
            "wallet": self.wallet_key,
            "strategy": self.strategy_id,
            "timeframe": self.timeframe,
            "mode": self.mode,
            "last_bar_time": last_ts.isoformat() if last_ts is not None else None,
            "bars_seen": i + 1 if i >= 0 else 0,
            "setup": setup,
            "funnel": self._json_safe(getattr(self.strategy, "funnel_stats", None)),
        }

    MAX_OVERLAY_LINES = 6  # a chart with more reference lines than this reads as noise, not information

    def plot_data(self, count: int = 300) -> dict:
        """The running strategy's own indicator columns, classified for the
        Chart view into:
          - price_series: overlay lines drawn directly on the candles (EMA,
            PDH/PDL, swing levels, Bollinger, ...) — magnitude close to price.
          - oscillator_series: a separate small chart below (RSI, MACD, ADX,
            Stochastic, ...) — bounded but not in price units.
          - state: near-binary/ternary signals (e.g. a -1/0/1 HTF bias) shown
            as a plain-language badge instead of a barely-visible flat line.

        Classification is a heuristic (compares magnitude to price, and
        counts distinct values) rather than a hardcoded per-strategy list,
        so it works for hand-coded strategies and every custom-rule
        indicator without either side needing to know about the other.
        FVG boundary/size columns are excluded entirely — a flat line
        spanning the whole visible window is misleading for something
        that was only ever meaningful at the single bar it formed on.

        In simulated mode self.df is the WHOLE pre-loaded dataset, so the
        window is capped at the last bar actually played so far — never
        ahead of the replay."""
        empty = {"price_series": {}, "oscillator_series": {}, "state": {}}
        if self.df is None:
            return empty
        if self.mode == "simulated":
            end = self.last_i + 1 if self.last_i >= 0 else 0
        else:
            end = len(self.df)
        start = max(0, end - count)
        window = self.df.iloc[start:end]
        if window.empty:
            return empty

        close_mean = window["close"].dropna().mean()
        skip = {"open", "high", "low", "close", "volume", "atr"}
        price_series: dict[str, list] = {}
        oscillator_series: dict[str, list] = {}
        state: dict[str, float] = {}
        for col in window.columns:
            if col in skip or col.startswith("__") or "fvg" in col.lower():
                continue
            s = window[col]
            if s.dtype == bool:
                continue
            s = s.dropna()
            if s.empty:
                continue
            uniq = set(s.unique().tolist())
            if len(uniq) <= 3 and uniq <= {-1, 0, 1}:
                state[col] = float(s.iloc[-1])
                continue
            val_mean = abs(s.mean())
            if close_mean and 0.4 * close_mean <= val_mean <= 2.5 * close_mean:
                target = price_series
            elif val_mean <= 1000:
                target = oscillator_series
            else:
                continue  # neither price-scale nor a recognizable bounded oscillator
            target[col] = [[int(ts.timestamp()), float(v)] for ts, v in s.items()]

        # a "_wide" swing level supersedes its tighter "_prior" sibling for
        # display purposes — same concept, the wide one is what's actually
        # used for targets, so showing both is pure duplication
        for name in list(price_series):
            if name.endswith("_prior") and name.replace("_prior", "_wide") in price_series:
                del price_series[name]
        if len(price_series) > self.MAX_OVERLAY_LINES:
            price_series = dict(list(price_series.items())[: self.MAX_OVERLAY_LINES])

        return {"price_series": price_series, "oscillator_series": oscillator_series, "state": state}

    def latest_atr(self) -> float | None:
        """Current ATR for this strategy's own trailing-stop check — mode-
        aware, since a plain iloc[-1] would be lookahead in simulated mode
        (self.df there is the WHOLE dataset, not just what's played so far)
        but is exactly right in biquote mode (self.df ends at "now")."""
        if self.df is None or "atr" not in self.df.columns:
            return None
        if self.mode == "simulated":
            if self.last_i < 0 or self.last_i >= len(self.df):
                return None
            idx = self.last_i
        else:
            if len(self.df) == 0:
                return None
            idx = len(self.df) - 1
        val = self.df["atr"].iloc[idx]
        return float(val) if pd.notna(val) else None

    # ------------------------------------------------------------------ #
    # biquote mode: polls biquote's own OHLC directly at THIS strategy's
    # own timeframe — independent of webapp.feed.FEED entirely, so
    # concurrent strategies at different timeframes never collide.
    # ------------------------------------------------------------------ #
    def _biquote_loop(self):
        # biquote.io is a remote HTTP dependency and does occasionally time
        # out / reset the connection — verified in production logs. Without
        # this try/except, any such network hiccup raises out of
        # get_ohlc(), which only catches its own BiquoteError, and an
        # uncaught exception here silently kills this daemon thread for
        # good: `enabled` stays True in status() forever after, so the
        # wallet looks like it's still running while never processing
        # another bar. That was the real cause of every wallet showing zero
        # trades for hours, not any strategy being "selective".
        consecutive_failures = 0
        while True:
            if not self._running:
                return
            try:
                bars = biquote_client.get_ohlc(self.timeframe, limit=2)
                if not bars:
                    time.sleep(2.0)
                    continue
                closed = [b for b in bars if not b.get("isOpen")]
                if closed:
                    latest = closed[-1]
                    ts = pd.Timestamp(latest["openTime"])
                    if ts != self.last_bar_time:
                        self.last_bar_time = ts
                        bar = {
                            "time": ts, "open": float(latest["open"]), "high": float(latest["high"]),
                            "low": float(latest["low"]), "close": float(latest["close"]),
                        }
                        new_row = pd.DataFrame(
                            [{"open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"], "volume": 0.0}],
                            index=[ts],
                        )
                        self.raw_df = pd.concat([self.raw_df, new_row])
                        self.raw_df = self.raw_df[~self.raw_df.index.duplicated(keep="last")].sort_index()
                        self.df = self.strategy.prepare(self.raw_df.copy())
                        i = len(self.df) - 1
                        self._process_bar(bar, i)
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures in (1, 15) or consecutive_failures % 150 == 0:
                    ACTIVITY.add(
                        "error",
                        f"{self.strategy_id} (wallet '{self.wallet_key}'): biquote poll failed "
                        f"({consecutive_failures} in a row) — {exc!r}",
                    )
            time.sleep(2.0)

    # ------------------------------------------------------------------ #
    # simulated mode fallback: an independent private replay of the local
    # dataset (self.df is the WHOLE pre-loaded dataset from start()) —
    # only reachable up to self.last_i, same no-lookahead discipline as a
    # batch backtest.
    # ------------------------------------------------------------------ #
    def _sim_loop(self):
        while True:
            if not self._running:
                return
            try:
                self.last_i += 1
                if self.last_i >= len(self.df):
                    # Reached the end of the local historical dataset. This
                    # USED to wrap back to self.last_i = 0 and keep replaying
                    # from 2020 forever — a wallet left running for more than
                    # one lap (~5 days at 2s/bar for the 6-year dataset) would
                    # silently start trading 2020-era prices while everything
                    # downstream (entry_time stamps from a later bar, wallet
                    # balance, ATR) still reflected "now". That produced
                    # nonsense signals with a stop/target computed off a
                    # ~$1,900 basis filled against a ~$4,300 reference price —
                    # a five-figure phantom loss/gain in a single trade. Stop
                    # cleanly instead: no more bars to play without lookahead.
                    self.enabled = False
                    self._running = False
                    ACTIVITY.add(
                        "error",
                        f"{self.strategy_id} (wallet '{self.wallet_key}'): reached the end of the "
                        "local historical dataset in simulated mode (no live biquote feed) — stopped "
                        "instead of looping back to the start. Restart the wallet to replay again.",
                    )
                    return
                row = self.df.iloc[self.last_i]
                ts = self.df.index[self.last_i]
                bar = {
                    "time": ts, "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                }
                self._process_bar(bar, self.last_i)
            except Exception as exc:
                # same rationale as _biquote_loop: a strategy bug on one bar
                # must not permanently kill this wallet's thread.
                ACTIVITY.add("error", f"{self.strategy_id} (wallet '{self.wallet_key}'): sim step failed — {exc!r}")
            time.sleep(2.0)

    # ------------------------------------------------------------------ #
    def _process_bar(self, bar: dict, i: int) -> None:
        """Fills/exits/trailing-stops for THIS strategy's own wallet only,
        then a fresh signal check — mirrors what the old single shared
        _on_tick + maybe_generate used to do globally, now scoped so
        concurrent strategies never touch each other's positions."""
        from webapp import db
        from webapp.routers.paper import _close_position, _fill_order, _get_wallet, _risk_block_reason

        conn = db.get_conn()
        try:
            ts, o, h, l, c = bar["time"], bar["open"], bar["high"], bar["low"], bar["close"]
            costs_cfg = CostConfig()
            engine_cfg = EngineConfig()

            conn.execute(
                "UPDATE paper_orders SET status='cancelled', cancelled_at=? "
                "WHERE status='pending' AND COALESCE(tag,'manual')=? AND expiry IS NOT NULL AND expiry < ?",
                (ts.isoformat(), self.wallet_key, ts.isoformat()),
            )
            for row in conn.execute(
                "SELECT * FROM paper_orders WHERE status='pending' AND COALESCE(tag,'manual')=?", (self.wallet_key,)
            ).fetchall():
                r = dict(row)
                price = paper_engine.try_fill_order(r, o, h, l)
                if price is not None:
                    _fill_order(conn, r, price, ts, costs_cfg)

            atr_val = self.latest_atr()
            for row in conn.execute(
                "SELECT * FROM paper_positions WHERE COALESCE(tag,'manual')=?", (self.wallet_key,)
            ).fetchall():
                r = dict(row)
                if r.get("trail_atr_mult") and atr_val is not None and math.isfinite(atr_val):
                    dist = r["trail_atr_mult"] * atr_val
                    new_stop = (c - dist) if r["direction"] == 1 else (c + dist)
                    improved = (new_stop > r["stop_loss"]) if r["direction"] == 1 else (new_stop < r["stop_loss"])
                    if improved:
                        conn.execute("UPDATE paper_positions SET stop_loss = ? WHERE id = ?", (new_stop, r["id"]))
                        r["stop_loss"] = new_stop
                exit_info = paper_engine.try_close_position(costs_cfg, engine_cfg, r, ts, h, l, c)
                if exit_info is not None:
                    _close_position(conn, r, exit_info, ts)
            conn.commit()

            self._maybe_generate(conn, i, ts, _get_wallet, _risk_block_reason)
            conn.commit()
        finally:
            conn.close()

    def _maybe_generate(self, conn, i: int, ts, get_wallet, risk_block_reason) -> None:
        settings = dict(conn.execute("SELECT * FROM risk_settings WHERE id = 1").fetchone())
        wallet = get_wallet(conn, self.wallet_key)

        open_count = conn.execute(
            "SELECT COUNT(*) c FROM paper_positions WHERE COALESCE(tag,'manual') = ?", (self.wallet_key,)
        ).fetchone()["c"]
        if open_count >= settings["max_open_positions"]:
            block_reason = f"already at the max open positions limit ({settings['max_open_positions']})"
        else:
            block_reason = risk_block_reason(conn, settings, wallet, self.wallet_key)

        if block_reason and block_reason != self._last_logged_block:
            ACTIVITY.add("blocked", f"[{self.wallet_key}] New entries blocked: {block_reason}")
        self._last_logged_block = block_reason
        self.last_block_reason = block_reason
        if block_reason:
            return

        orders = self.strategy.generate(self.df, i, self.state)
        if not orders:
            return
        price = self._current_price()
        for order in orders:
            side = "LONG" if order.direction == 1 else "SHORT"
            tp = order.tp1 if order.tp1 is not None else order.tp2
            tp_txt = f", TP {tp:.2f}" if tp is not None else ""
            ACTIVITY.add(
                "signal",
                f"[{self.wallet_key}] {side} signal @ {ts.strftime('%Y-%m-%d %H:%M')} UTC — "
                f"close {self.df['close'].iloc[i]:.2f}, SL {order.stop_loss:.2f}{tp_txt}",
            )
            self._place(conn, order, settings, price)

    def _current_price(self) -> dict | None:
        """Fresh bid/ask for a market order's fill reference — direct from
        biquote (this runner's own concern, independent of any shared
        feed) in biquote mode, or a synthesized spread around the replay's
        own close in simulated mode."""
        if self.mode == "biquote":
            tick = biquote_client.get_tick()
            if tick is None:
                return None
            return {"bid": float(tick["bid"]), "ask": float(tick["ask"]), "mid": float(tick["mid"])}
        if self.df is None or self.last_i < 0:
            return None
        mid = float(self.df["close"].iloc[self.last_i])
        return {"bid": mid, "ask": mid, "mid": mid}

    def _place(self, conn, order, settings: dict, price: dict | None) -> None:
        if order.kind == "market":
            if price is None:
                return
            ref_price = price["ask"] if order.direction == 1 else price["bid"]
        else:
            ref_price = order.trigger

        take_profit = order.tp1 if order.tp1 is not None else order.tp2
        risk_per_oz = abs(ref_price - order.stop_loss)
        if risk_per_oz <= 0:
            return

        # Sanity guard: the stop/target a strategy computes and the price this
        # order actually fills at should come from (nearly) the same moment,
        # so the stop distance should be a small fraction of price — normal
        # ATR-based stops on gold intraday bars run well under 1%. A stop
        # this far from the fill price means the signal was generated off a
        # stale or otherwise bad reference bar (e.g. a data-feed hiccup, or a
        # replay that fell behind the live price) rather than a real setup.
        # Reject rather than open a position sized against a nonsense risk
        # distance — this is what let a single bad bar turn into a
        # five-figure phantom loss/gain before this guard existed.
        MAX_STOP_DISTANCE_PCT = 0.08
        if ref_price and risk_per_oz / ref_price > MAX_STOP_DISTANCE_PCT:
            ACTIVITY.add(
                "error",
                f"{self.strategy_id} (wallet '{self.wallet_key}'): rejected signal — stop distance "
                f"{risk_per_oz:.2f} is {100 * risk_per_oz / ref_price:.1f}% of price {ref_price:.2f} "
                f"(> {100 * MAX_STOP_DISTANCE_PCT:.0f}% sanity limit), likely a stale/bad reference bar",
            )
            return

        from webapp.routers.paper import _get_wallet  # deferred — see _process_bar

        balance = _get_wallet(conn, self.wallet_key)["balance"]
        risk_pct = self.risk_pct if self.risk_pct is not None else settings["risk_per_trade_pct"]
        lots, risk_amount = position_size(
            balance, risk_pct, risk_per_oz,
            contract_size=settings["contract_size"], min_lot=settings["min_lot"],
            max_lot=settings["max_lot"], max_risk_pct=settings["max_risk_per_trade_pct"],
        )
        if lots <= 0:
            return

        units = lots * settings["contract_size"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO paper_orders
               (kind, direction, trigger_price, stop_loss, take_profit, lots, units, risk_amount,
                tag, oco_group, expiry, time_exit, trail_atr_mult, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
            (
                order.kind, order.direction, order.trigger, order.stop_loss, take_profit,
                lots, units, risk_amount, order.tag, order.oco_group,
                order.expiry.isoformat() if order.expiry is not None else None,
                order.time_exit.isoformat() if order.time_exit is not None else None,
                order.trail_atr_mult, now,
            ),
        )


def _biquote_recent_history(timeframe: str, count: int = 500):
    """Real recent OHLC from biquote (closed bars only), independent of
    webapp.feed.FEED — each runner fetches at its OWN timeframe. See
    webapp.feed.MarketFeed.recent_history, which does the same thing for
    the Chart view's single shared feed."""
    bars = [b for b in biquote_client.get_ohlc(timeframe, count) if not b.get("isOpen")]
    if not bars:
        return None
    idx = pd.to_datetime([b["openTime"] for b in bars], utc=True)
    df = pd.DataFrame({
        "open": [float(b["open"]) for b in bars],
        "high": [float(b["high"]) for b in bars],
        "low": [float(b["low"]) for b in bars],
        "close": [float(b["close"]) for b in bars],
        "volume": [0.0] * len(bars),
    }, index=idx)
    df.index.name = "timestamp"
    return df


class AutoTraderManager:
    """Holds every strategy that has ever been started, keyed by wallet —
    at most one running instance per wallet at a time (starting the same
    wallet again while it's already running is rejected; a genuinely new
    run for that wallet replaces the old, stopped entry)."""

    def __init__(self):
        self.runners: dict[str, AutoTrader] = {}

    def start(self, strategy_id: str, params: dict, risk_pct: float | None = None) -> AutoTrader:
        runner = AutoTrader(strategy_id, params, risk_pct)  # validates strategy_id/params first
        existing = self.runners.get(runner.wallet_key)
        if existing is not None and existing.enabled:
            raise ValueError(
                f"'{runner.wallet_key}' is already running — stop it first if you want to restart it"
            )
        runner.start()
        self.runners[runner.wallet_key] = runner
        return runner

    def stop(self, wallet_key: str) -> None:
        runner = self.runners.get(wallet_key)
        if runner is None:
            raise ValueError(f"no strategy named '{wallet_key}' has been started this session")
        runner.stop()

    def stop_all(self) -> None:
        for runner in self.runners.values():
            runner.stop()

    def status_list(self) -> list[dict]:
        return [r.status() for r in self.runners.values()]

    def setup_list(self) -> list[dict]:
        return [r.setup_status() for r in self.runners.values() if r.enabled]

    def get(self, wallet_key: str) -> AutoTrader | None:
        return self.runners.get(wallet_key)

    def running(self) -> list[AutoTrader]:
        return [r for r in self.runners.values() if r.enabled]


AUTOTRADERS = AutoTraderManager()
