"""Bar-by-bar execution engine.

Deliberately conservative. The assumptions that matter:

1. Signals are generated at the CLOSE of bar i and can only be acted on from
   bar i+1 onward. No lookahead.
2. When one bar's range contains both the stop and a target, OHLC cannot tell
   you which came first. We assume the stop (configurable, but don't change it).
3. Stop-order entries fill at the worse of (trigger, bar open), so gaps hurt.
4. Costs are charged per side using the session-aware spread model.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import EngineConfig, RunConfig
from .costs import entry_cost, exit_cost
from .risk import position_size


@dataclass
class Order:
    """A pending instruction produced by a strategy."""

    created_at: pd.Timestamp
    direction: int                 # +1 long, -1 short
    kind: str                      # "market" | "stop"
    stop_loss: float = 0.0
    trigger: float | None = None   # required for kind == "stop"
    tp1: float | None = None
    tp2: float | None = None
    expiry: pd.Timestamp | None = None
    time_exit: pd.Timestamp | None = None
    trail_atr_mult: float | None = None
    tag: str = ""
    oco_group: str | None = None


@dataclass
class Position:
    entry_time: pd.Timestamp
    entry_price: float
    direction: int
    units: float
    units_open: float
    stop: float
    initial_stop: float
    risk_per_oz: float
    entry_cost_per_oz: float
    tp1: float | None = None
    tp2: float | None = None
    time_exit: pd.Timestamp | None = None
    trail_atr_mult: float | None = None
    tag: str = ""
    tp1_done: bool = False
    realised_pnl: float = 0.0
    total_costs: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0
    entry_bar: int = 0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    strategy_name: str
    config: RunConfig
    bars: int = 0
    rejected_no_size: int = 0
    dd_halted_at: pd.Timestamp | None = None
    daily_loss_breach_days: int = 0


def fill_price(order: Order, o: float, h: float, l: float) -> float | None:
    """Would this order fill on a bar with this open/high/low?

    "stop" = breakout entry (buy above market / sell below); "limit" = pullback
    entry (buy below market / sell above). Both fill at the worse of (trigger,
    bar open) — gaps hurt, never help. The three existing strategies only
    ever create "market"/"stop" orders; "limit" exists for the paper-trading
    order ticket (Phase 5), which offers both pending-order types.

    Free function (not a Backtester method) so the paper-trading engine
    (gold_bot.paper_engine) can call the identical rule bar-by-bar or
    tick-by-tick instead of re-deriving it.
    """
    if order.kind == "market":
        return o
    trig = order.trigger
    if order.kind == "stop":
        if order.direction == 1 and h >= trig:
            return max(trig, o)
        if order.direction == -1 and l <= trig:
            return min(trig, o)
        return None
    if order.kind == "limit":
        if order.direction == 1 and l <= trig:
            return min(trig, o)
        if order.direction == -1 and h >= trig:
            return max(trig, o)
        return None
    return None


def exit_events(eng: EngineConfig, pos: Position, ts, h, l, c):
    """Return list of (reason, price, units, is_final) for this bar, given
    an open Position and this bar's high/low/close. Same sharing rationale
    as fill_price above."""
    d = pos.direction
    events = []

    pos.mae = max(pos.mae, (pos.entry_price - l) if d == 1 else (h - pos.entry_price))
    pos.mfe = max(pos.mfe, (h - pos.entry_price) if d == 1 else (pos.entry_price - l))

    hit_stop = (l <= pos.stop) if d == 1 else (h >= pos.stop)
    hit_tp1 = (
        pos.tp1 is not None
        and not pos.tp1_done
        and ((h >= pos.tp1) if d == 1 else (l <= pos.tp1))
    )
    hit_tp2 = pos.tp2 is not None and ((h >= pos.tp2) if d == 1 else (l <= pos.tp2))

    if hit_stop and (hit_tp1 or hit_tp2) and eng.ambiguous_bar_resolution == "stop":
        return [("stop_ambiguous", pos.stop, pos.units_open, True)]

    if hit_stop and not (hit_tp1 or hit_tp2):
        if abs(pos.stop - pos.initial_stop) < 1e-9:
            reason = "stop"
        elif abs(pos.stop - pos.entry_price) < 1e-9:
            reason = "breakeven"
        else:
            reason = "trail_stop"
        return [(reason, pos.stop, pos.units_open, True)]

    if hit_tp1:
        part = round(pos.units * (eng.partial_at_tp1_pct / 100.0), 4)
        if 0 < part < pos.units_open - 1e-6 and pos.tp2 is not None:
            events.append(("tp1", pos.tp1, part, False))
            pos.units_open -= part
            pos.tp1_done = True
            if eng.move_stop_to_breakeven_after_tp1:
                pos.stop = pos.entry_price
        else:
            return [("tp1", pos.tp1, pos.units_open, True)]

    if hit_tp2 and pos.units_open > 0:
        events.append(("tp2", pos.tp2, pos.units_open, True))
        return events

    if pos.time_exit is not None and ts >= pos.time_exit and pos.units_open > 0:
        events.append(("time_exit", c, pos.units_open, True))
        return events

    return events


class Backtester:
    def __init__(self, config: RunConfig):
        self.cfg = config

    # ------------------------------------------------------------------ #
    def _size(self, equity: float, risk_per_oz: float) -> float:
        acct = self.cfg.account
        base = equity if acct.compound else acct.initial_equity
        lots, _ = position_size(
            base, acct.risk_per_trade_pct, risk_per_oz,
            contract_size=acct.contract_size, min_lot=acct.min_lot, max_lot=acct.max_lot,
            max_risk_pct=acct.max_risk_per_trade_pct,
        )
        return lots * acct.contract_size

    # ------------------------------------------------------------------ #
    def _fill_price(self, order: Order, o: float, h: float, l: float) -> float | None:
        return fill_price(order, o, h, l)

    # ------------------------------------------------------------------ #
    def _exit_events(self, pos: Position, ts, h, l, c):
        return exit_events(self.cfg.engine, pos, ts, h, l, c)

    # ------------------------------------------------------------------ #
    def _book(self, pos: Position, ts, events, equity, trades, bar_i):
        """Apply exit events to equity; append a trade record when flat."""
        cfg = self.cfg
        closed = False
        for reason, price, units, final in events:
            ec = exit_cost(ts, cfg.costs)
            gross = (price - pos.entry_price) * pos.direction * units
            cost = (pos.entry_cost_per_oz + ec) * units
            cost += (
                cfg.costs.commission_per_lot_roundtrip * units / cfg.account.contract_size
            )
            net = gross - cost
            pos.realised_pnl += net
            pos.total_costs += cost
            equity += net

            if final:
                risk_amount = pos.risk_per_oz * pos.units
                trades.append(
                    {
                        "tag": pos.tag,
                        "direction": pos.direction,
                        "side": "long" if pos.direction == 1 else "short",
                        "entry_time": pos.entry_time,
                        "exit_time": ts,
                        "entry_price": pos.entry_price,
                        "exit_price": price,
                        "units": pos.units,
                        "lots": pos.units / cfg.account.contract_size,
                        "initial_stop": pos.initial_stop,
                        "tp1": pos.tp1,
                        "tp2": pos.tp2,
                        "risk_per_oz": pos.risk_per_oz,
                        "risk_amount": risk_amount,
                        "gross_pnl": pos.realised_pnl + pos.total_costs,
                        "costs": pos.total_costs,
                        "net_pnl": pos.realised_pnl,
                        "r_multiple": pos.realised_pnl / risk_amount if risk_amount else 0.0,
                        "exit_reason": reason,
                        "bars_held": bar_i - pos.entry_bar,
                        "mae_r": pos.mae / pos.risk_per_oz if pos.risk_per_oz else 0.0,
                        "mfe_r": pos.mfe / pos.risk_per_oz if pos.risk_per_oz else 0.0,
                        "equity_after": equity,
                    }
                )
                closed = True
                break
        return equity, closed

    # ------------------------------------------------------------------ #
    def run(self, df: pd.DataFrame, strategy) -> BacktestResult:
        cfg = self.cfg
        eng = cfg.engine

        df = strategy.prepare(df.copy())
        idx = df.index
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        atr_col = (
            df["atr"].to_numpy(float) if "atr" in df.columns else np.full(len(df), np.nan)
        )

        equity = cfg.account.initial_equity
        equity_curve = np.empty(len(df))

        pending: list[Order] = []
        pos: Position | None = None
        trades: list[dict] = []
        rejected = 0
        trades_today = 0
        current_day = None
        state: dict = {}

        day_start_equity = equity
        peak_equity = equity
        dd_halted_at: pd.Timestamp | None = None
        daily_loss_breach_days = 0
        daily_loss_breached_today = False

        for i in range(len(df)):
            ts = idx[i]
            day = ts.normalize()
            if day != current_day:
                current_day = day
                trades_today = 0
                day_start_equity = equity
                daily_loss_breached_today = False

            # 1. manage open position -------------------------------------
            if pos is not None:
                events = self._exit_events(pos, ts, h[i], l[i], c[i])
                if events:
                    equity, closed = self._book(pos, ts, events, equity, trades, i)
                    if closed:
                        pos = None

            # risk circuit breakers — "no NEW entries", not a forced exit of
            # whatever is already open (handled above, unaffected by this) ---
            peak_equity = max(peak_equity, equity)
            if eng.max_drawdown_pct is not None and dd_halted_at is None and peak_equity > 0:
                dd_pct = 100 * (equity - peak_equity) / peak_equity
                if dd_pct <= -eng.max_drawdown_pct:
                    dd_halted_at = ts
            if (
                eng.max_daily_loss_pct is not None
                and not daily_loss_breached_today
                and day_start_equity > 0
            ):
                daily_pct = 100 * (equity - day_start_equity) / day_start_equity
                if daily_pct <= -eng.max_daily_loss_pct:
                    daily_loss_breached_today = True
                    daily_loss_breach_days += 1
            risk_blocked = dd_halted_at is not None or daily_loss_breached_today

            # 2. drop expired orders --------------------------------------
            if pending:
                pending = [x for x in pending if x.expiry is None or ts <= x.expiry]

            # 3. attempt fills --------------------------------------------
            if pos is None and pending and trades_today < eng.max_trades_per_day and not risk_blocked:
                keep: list[Order] = []
                filled_group = None
                for order in pending:
                    if pos is not None or order.created_at >= ts:
                        keep.append(order)
                        continue
                    fill = self._fill_price(order, o[i], h[i], l[i])
                    if fill is None:
                        keep.append(order)
                        continue

                    risk = abs(fill - order.stop_loss)
                    units = self._size(equity, risk)
                    if units <= 0:
                        rejected += 1
                        continue

                    pos = Position(
                        entry_time=ts,
                        entry_price=fill,
                        direction=order.direction,
                        units=units,
                        units_open=units,
                        stop=order.stop_loss,
                        initial_stop=order.stop_loss,
                        risk_per_oz=risk,
                        entry_cost_per_oz=entry_cost(ts, cfg.costs),
                        tp1=order.tp1,
                        tp2=order.tp2,
                        time_exit=order.time_exit,
                        trail_atr_mult=order.trail_atr_mult,
                        tag=order.tag,
                        entry_bar=i,
                    )
                    trades_today += 1
                    filled_group = order.oco_group

                    events = self._exit_events(pos, ts, h[i], l[i], c[i])
                    if events:
                        equity, closed = self._book(pos, ts, events, equity, trades, i)
                        if closed:
                            pos = None

                if filled_group is not None:
                    keep = [x for x in keep if x.oco_group != filled_group]
                pending = keep

            # 4. trailing stop --------------------------------------------
            if pos is not None and pos.trail_atr_mult and np.isfinite(atr_col[i]):
                dist = pos.trail_atr_mult * atr_col[i]
                if pos.direction == 1:
                    pos.stop = max(pos.stop, c[i] - dist)
                else:
                    pos.stop = min(pos.stop, c[i] + dist)

            # 5. new signals at bar close ---------------------------------
            if eng.warmup_bars <= i < len(df) - 1:
                busy = pos is not None and eng.one_position_at_a_time
                if not busy and trades_today < eng.max_trades_per_day and not risk_blocked:
                    new = strategy.generate(df, i, state)
                    if new:
                        pending.extend(new)

            equity_curve[i] = equity

        return BacktestResult(
            trades=pd.DataFrame(trades),
            equity=pd.Series(equity_curve, index=idx),
            strategy_name=strategy.name,
            dd_halted_at=dd_halted_at,
            daily_loss_breach_days=daily_loss_breach_days,
            config=cfg,
            bars=len(df),
            rejected_no_size=rejected,
        )
