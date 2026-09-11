"""No-code strategy engine: a single Strategy subclass driven entirely by a
JSON spec (indicators + entry conditions + stop-loss/take-profit rules)
instead of a hand-written dataclass per strategy.

This is the mechanism behind the Strategy Builder UI. It plugs into the
existing Backtester/paper-trading/auto-trade machinery exactly like
AsianSweepReversal or TrendPullback do — same Strategy contract
(prepare/generate), same Order objects, same costs/risk/engine code. No
other part of the system needs to know a strategy came from the builder
instead of being hand-coded.

Scope, deliberately: entries are rule-driven; exits are stop-loss /
take-profit / optional ATR trailing stop — the same exit vocabulary the
hand-coded strategies use. Arbitrary "exit when indicator condition flips"
rules are not supported yet (that needs engine.py's exit_events() to accept
a per-bar callback, which no strategy needs today) — a strategy needing that
still has to be hand-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .engine import Order
from .indicators import (
    adx,
    atr,
    bearish_engulfing,
    bollinger_bands,
    bullish_engulfing,
    doji,
    ema,
    hammer,
    macd,
    prior_swing_high,
    prior_swing_low,
    rsi,
    shooting_star,
    sma,
    stochastic,
    supertrend,
    vwap,
)


def _hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def _prefixed(frame: pd.DataFrame, ind_id: str) -> dict[str, pd.Series]:
    return {f"{ind_id}_{col}": frame[col] for col in frame.columns}


# --------------------------------------------------------------------- #
# indicator catalog — id -> (label, builder, param schema for the UI)
# builder(df, params, ind_id) -> {column_name: Series, ...}
# --------------------------------------------------------------------- #
def _build_ema(df, p, ind_id):
    return {ind_id: ema(df[p.get("source", "close")], int(p["period"]))}


def _build_sma(df, p, ind_id):
    return {ind_id: sma(df[p.get("source", "close")], int(p["period"]))}


def _build_rsi(df, p, ind_id):
    return {ind_id: rsi(df[p.get("source", "close")], int(p.get("period", 14)))}


def _build_atr(df, p, ind_id):
    return {ind_id: atr(df, int(p.get("period", 14)))}


def _build_adx(df, p, ind_id):
    return _prefixed(adx(df, int(p.get("period", 14))), ind_id)


def _build_macd(df, p, ind_id):
    return _prefixed(
        macd(df[p.get("source", "close")], int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9))),
        ind_id,
    )


def _build_bollinger(df, p, ind_id):
    return _prefixed(
        bollinger_bands(df[p.get("source", "close")], int(p.get("period", 20)), float(p.get("mult", 2.0))), ind_id
    )


def _build_vwap(df, p, ind_id):
    return {ind_id: vwap(df)}


def _build_stochastic(df, p, ind_id):
    return _prefixed(stochastic(df, int(p.get("k_period", 14)), int(p.get("d_period", 3))), ind_id)


def _build_supertrend(df, p, ind_id):
    return _prefixed(supertrend(df, int(p.get("period", 10)), float(p.get("mult", 3.0))), ind_id)


def _build_support(df, p, ind_id):
    # offset=1: excludes the current bar. A condition like "close crosses
    # above resistance" needs the level to be knowable BEFORE this bar's own
    # high could feed into it — an inclusive rolling window is
    # self-referential and such a condition would almost never fire.
    return {ind_id: prior_swing_low(df, int(p.get("lookback", 20)), offset=1)}


def _build_resistance(df, p, ind_id):
    return {ind_id: prior_swing_high(df, int(p.get("lookback", 20)), offset=1)}


def _build_dma(df, p, ind_id):
    """Daily moving average — SMA of DAILY closes, not a same-count rolling
    window on the trading timeframe. A "200-period SMA" on a 15min chart is
    ~2 trading days of bars, not 200 calendar days — a materially different
    (much faster) line, which is exactly the mix-up the reference doc this
    was built from warns about. shift(1): only a fully COMPLETED day counts,
    so intraday bars never see today's own still-forming daily close."""
    period = int(p.get("period", 200))
    source = p.get("source", "close")
    daily = df.resample("1D").agg({source: "last"}).dropna()
    line = sma(daily[source], period).shift(1)
    return {ind_id: line.reindex(df.index, method="ffill")}


def _build_pattern(fn):
    def _builder(df, p, ind_id):
        return {ind_id: fn(df).astype(float)}
    return _builder


INDICATOR_CATALOG: dict[str, dict[str, Any]] = {
    "ema": {"label": "EMA", "builder": _build_ema, "outputs": [""],
            "params": [{"name": "period", "type": "int", "default": 20}, {"name": "source", "type": "source", "default": "close"}]},
    "sma": {"label": "SMA", "builder": _build_sma, "outputs": [""],
            "params": [{"name": "period", "type": "int", "default": 20}, {"name": "source", "type": "source", "default": "close"}]},
    "rsi": {"label": "RSI", "builder": _build_rsi, "outputs": [""],
            "params": [{"name": "period", "type": "int", "default": 14}]},
    "atr": {"label": "ATR", "builder": _build_atr, "outputs": [""],
            "params": [{"name": "period", "type": "int", "default": 14}]},
    "adx": {"label": "ADX", "builder": _build_adx, "outputs": ["_adx", "_plus_di", "_minus_di"],
            "params": [{"name": "period", "type": "int", "default": 14}]},
    "macd": {"label": "MACD", "builder": _build_macd, "outputs": ["_macd", "_signal", "_hist"],
             "params": [{"name": "fast", "type": "int", "default": 12}, {"name": "slow", "type": "int", "default": 26},
                        {"name": "signal", "type": "int", "default": 9}, {"name": "source", "type": "source", "default": "close"}]},
    "bollinger": {"label": "Bollinger Bands", "builder": _build_bollinger, "outputs": ["_mid", "_upper", "_lower", "_bandwidth"],
                  "params": [{"name": "period", "type": "int", "default": 20}, {"name": "mult", "type": "float", "default": 2.0},
                             {"name": "source", "type": "source", "default": "close"}]},
    "vwap": {"label": "VWAP (session)", "builder": _build_vwap, "outputs": [""], "params": []},
    "stochastic": {"label": "Stochastic", "builder": _build_stochastic, "outputs": ["_k", "_d"],
                   "params": [{"name": "k_period", "type": "int", "default": 14}, {"name": "d_period", "type": "int", "default": 3}]},
    "supertrend": {"label": "Supertrend", "builder": _build_supertrend, "outputs": ["_line", "_direction"],
                   "params": [{"name": "period", "type": "int", "default": 10}, {"name": "mult", "type": "float", "default": 3.0}]},
    "support": {"label": "Rolling Support (prior swing low)", "builder": _build_support, "outputs": [""],
                "params": [{"name": "lookback", "type": "int", "default": 20}]},
    "resistance": {"label": "Rolling Resistance (prior swing high)", "builder": _build_resistance, "outputs": [""],
                   "params": [{"name": "lookback", "type": "int", "default": 20}]},
    "dma": {"label": "Daily Moving Average (SMA of daily closes)", "builder": _build_dma, "outputs": [""],
            "params": [{"name": "period", "type": "int", "default": 200}, {"name": "source", "type": "source", "default": "close"}]},
    "bullish_engulfing": {"label": "Bullish Engulfing (pattern)", "builder": _build_pattern(bullish_engulfing), "outputs": [""], "params": []},
    "bearish_engulfing": {"label": "Bearish Engulfing (pattern)", "builder": _build_pattern(bearish_engulfing), "outputs": [""], "params": []},
    "hammer": {"label": "Hammer (pattern)", "builder": _build_pattern(hammer), "outputs": [""], "params": []},
    "shooting_star": {"label": "Shooting Star (pattern)", "builder": _build_pattern(shooting_star), "outputs": [""], "params": []},
    "doji": {"label": "Doji (pattern)", "builder": _build_pattern(doji), "outputs": [""], "params": []},
}

PRICE_FIELDS = ["open", "high", "low", "close", "volume"]
OPERATORS = ["<", "<=", ">", ">=", "==", "cross_above", "cross_below"]


# --------------------------------------------------------------------- #
# condition evaluation
# --------------------------------------------------------------------- #
def _operand_series(df: pd.DataFrame, ref: Any) -> pd.Series:
    if isinstance(ref, (int, float)) and not isinstance(ref, bool):
        return pd.Series(float(ref), index=df.index)
    if isinstance(ref, str) and ref in df.columns:
        return df[ref]
    raise ValueError(f"unknown indicator/price reference '{ref}' — check the strategy's condition rows")


def _eval_condition(df: pd.DataFrame, cond: dict) -> pd.Series:
    left = _operand_series(df, cond["left"])
    right = _operand_series(df, cond["right"])
    op = cond["op"]
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "==":
        return np.isclose(left, right, equal_nan=False)
    if op == "cross_above":
        return (left.shift(1) <= right.shift(1)) & (left > right)
    if op == "cross_below":
        return (left.shift(1) >= right.shift(1)) & (left < right)
    raise ValueError(f"unknown operator '{op}'")


def _eval_group(df: pd.DataFrame, group: dict | None) -> pd.Series:
    if not group or not group.get("conditions"):
        return pd.Series(False, index=df.index)
    conds = [_eval_condition(df, c) for c in group["conditions"]]
    combinator = group.get("combinator", "and")
    out = conds[0]
    for c in conds[1:]:
        out = (out & c) if combinator == "and" else (out | c)
    return out.fillna(False)


def validate_spec(spec: dict) -> None:
    """Raises ValueError with a human-readable message on anything the
    builder UI shouldn't have let through — cheap enough to call on every
    save/backtest, and the only thing standing between a typo and a
    confusing KeyError deep in a backtest run."""
    if not spec.get("entry_long") and not spec.get("entry_short"):
        raise ValueError("define at least one entry rule (long or short)")
    sl = spec.get("stop_loss")
    if not sl or sl.get("type") not in ("atr_mult", "fixed_points", "fixed_pct"):
        raise ValueError("stop-loss is required (ATR multiple, fixed points, or fixed %)")
    tp = spec.get("take_profit")
    if tp and tp.get("type") not in ("rr", "fixed_points", "fixed_pct"):
        raise ValueError(f"unknown take-profit type '{tp.get('type')}'")
    for key in ("indicators",):
        for ind in spec.get(key, []):
            if ind["type"] not in INDICATOR_CATALOG:
                raise ValueError(f"unknown indicator type '{ind['type']}'")
    for key in ("entry_long", "entry_short"):
        group = spec.get(key)
        if not group:
            continue
        for cond in group.get("conditions", []):
            if cond["op"] not in OPERATORS:
                raise ValueError(f"unknown operator '{cond['op']}'")


@dataclass
class RuleStrategy:
    """Data-driven strategy — see module docstring. `spec` holds everything;
    this class has no other configurable fields (unlike the hand-coded
    strategies, whose dataclass fields ARE their config)."""

    name: str = "custom_rule"
    spec: dict = field(default_factory=dict)

    @property
    def timeframe(self) -> str:
        """The working timeframe this config trades on — lives inside spec
        (there's no per-field UI for custom_rule the way hand-coded
        strategies get one from their own dataclass fields) rather than as
        a dataclass field, but exposed the same way so
        webapp.autotrade.AutoTrader.start() can read it uniformly off any
        strategy without caring which kind it is."""
        return self.spec.get("timeframe", "15min")

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        for ind in self.spec.get("indicators", []):
            cols = INDICATOR_CATALOG[ind["type"]]["builder"](df, ind.get("params", {}), ind["id"])
            for col_name, series in cols.items():
                df[col_name] = series
        df["__entry_long"] = _eval_group(df, self.spec.get("entry_long"))
        df["__entry_short"] = _eval_group(df, self.spec.get("entry_short"))
        return df

    def _stop_price(self, direction: int, entry: float, a: float, rule: dict) -> float | None:
        t = rule["type"]
        if t == "atr_mult":
            if not np.isfinite(a) or a <= 0:
                return None
            return entry - rule["mult"] * a if direction == 1 else entry + rule["mult"] * a
        if t == "fixed_points":
            return entry - rule["points"] if direction == 1 else entry + rule["points"]
        if t == "fixed_pct":
            return entry * (1 - rule["pct"] / 100.0) if direction == 1 else entry * (1 + rule["pct"] / 100.0)
        return None

    def _tp_price(self, direction: int, entry: float, stop: float, rule: dict | None) -> float | None:
        if not rule:
            return None
        t = rule["type"]
        risk = abs(entry - stop)
        if t == "rr":
            return entry + rule["rr"] * risk if direction == 1 else entry - rule["rr"] * risk
        if t == "fixed_points":
            return entry + rule["points"] if direction == 1 else entry - rule["points"]
        if t == "fixed_pct":
            return entry * (1 + rule["pct"] / 100.0) if direction == 1 else entry * (1 - rule["pct"] / 100.0)
        return None

    def generate(self, df: pd.DataFrame, i: int, state: dict) -> list[Order]:
        session = self.spec.get("session")
        ts = df.index[i]
        if session:
            m = ts.hour * 60 + ts.minute
            if not (_hm(session["start"]) <= m <= _hm(session["end"])):
                return []

        row = df.iloc[i]
        long_sig = bool(row["__entry_long"])
        short_sig = bool(row["__entry_short"])
        if long_sig and short_sig:
            return []  # contradictory rules fired on the same bar — skip rather than guess

        if not long_sig and not short_sig:
            return []

        direction = 1 if long_sig else -1
        entry = row["close"]
        a = row.get("atr", np.nan)
        stop = self._stop_price(direction, entry, a, self.spec["stop_loss"])
        if stop is None or (direction == 1 and stop >= entry) or (direction == -1 and stop <= entry):
            return []
        tp = self._tp_price(direction, entry, stop, self.spec.get("take_profit"))

        return [
            Order(
                created_at=ts, direction=direction, kind="market", stop_loss=stop, tp1=tp,
                trail_atr_mult=self.spec.get("trail_atr_mult"), tag=self.spec.get("strategy_name", "custom_rule"),
            )
        ]
