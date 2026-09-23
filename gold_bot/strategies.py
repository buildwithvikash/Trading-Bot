"""The three gold strategies.

Each strategy implements:
    prepare(df) -> df with indicator columns added (must include "atr")
    generate(df, i, state) -> list[Order] created at the CLOSE of bar i

Nothing here may read df at an index > i.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .engine import Order
from .indicators import (
    adx,
    atr,
    bearish_engulfing,
    bullish_engulfing,
    confirmed_swing_high,
    confirmed_swing_low,
    daily_range_average,
    displacement,
    ema,
    fair_value_gaps,
    hammer,
    htf_ema_bias,
    prior_swing_high,
    prior_swing_low,
    shooting_star,
    zigzag_pivots,
)
from .rule_strategy import RuleStrategy


# --------------------------------------------------------------------- #
class Strategy:
    name = "base"
    # The working timeframe this strategy actually trades on — live/demo
    # auto-trade reads this off the instantiated strategy and reconfigures
    # the feed to match (webapp.autotrade.AutoTrader.start), rather than
    # forcing every strategy onto one hardcoded bar size. Each subclass
    # re-declares this as its own dataclass field (not just inherited) so
    # it shows up as a tunable param the same way any other field does.
    timeframe = "15min"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        return df

    def generate(self, df: pd.DataFrame, i: int, state: dict) -> list[Order]:
        return []


def _minutes(ts: pd.Timestamp) -> int:
    return ts.hour * 60 + ts.minute


def _hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def _finite_min(*vals) -> float:
    xs = [v for v in vals if v is not None and np.isfinite(v)]
    return min(xs) if xs else np.nan


def _finite_max(*vals) -> float:
    xs = [v for v in vals if v is not None and np.isfinite(v)]
    return max(xs) if xs else np.nan


def _forward_target(direction: int, entry_level: float, *candidates) -> float:
    """Nearest candidate liquidity level that's still genuinely ahead of the
    entry (above it for a long, below it for a short). A level already
    behind price by the time the retracement entry triggers isn't a
    target — it's liquidity that's already been taken, which is exactly
    what MSS + displacement getting us this far usually means for the
    same-day/session levels (PDH, Asian high, ...)."""
    if direction == 1:
        xs = [v for v in candidates if v is not None and np.isfinite(v) and v > entry_level]
        return min(xs) if xs else np.nan
    xs = [v for v in candidates if v is not None and np.isfinite(v) and v < entry_level]
    return max(xs) if xs else np.nan


# --------------------------------------------------------------------- #
@dataclass
class AsianSweepReversal(Strategy):
    """Strategy 1 — Asian range liquidity sweep, London reversal.

    Gold ranges thinly through Asia. London takes out one side to trigger
    stops, then reverses. We fade the sweep once price closes back inside.
    """

    name: str = "asian_sweep_reversal"
    timeframe: str = "15min"
    asian_start: str = "00:00"
    asian_end: str = "06:00"
    trade_start: str = "07:00"
    trade_end: str = "11:00"
    sweep_atr_mult: float = 0.10        # how far beyond the range counts as a sweep
    reclaim_within_bars: int = 3
    stop_buffer_atr: float = 0.15
    max_range_vs_adr: float = 0.60      # skip already-exhausted days
    min_rr_to_tp1: float = 0.5
    session_exit: str = "20:00"
    trail_atr_mult: float | None = None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        df["adr20"] = daily_range_average(df, 20)

        mins = df.index.hour * 60 + df.index.minute
        in_asia = (mins >= _hm(self.asian_start)) & (mins < _hm(self.asian_end))
        day = df.index.normalize()

        asia = df[in_asia].groupby(day[in_asia]).agg({"high": "max", "low": "min"})
        df["asian_high"] = pd.Series(day, index=df.index).map(asia["high"])
        df["asian_low"] = pd.Series(day, index=df.index).map(asia["low"])

        daily = df.resample("1D").agg({"high": "max", "low": "min"}).dropna()
        prev = daily.shift(1)
        prev.index = prev.index.normalize()
        df["prev_high"] = pd.Series(day, index=df.index).map(prev["high"])
        df["prev_low"] = pd.Series(day, index=df.index).map(prev["low"])
        return df

    def generate(self, df, i, state):
        ts = df.index[i]
        m = _minutes(ts)
        if not (_hm(self.trade_start) <= m <= _hm(self.trade_end)):
            return []

        row = df.iloc[i]
        ah, al, a = row["asian_high"], row["asian_low"], row["atr"]
        if not np.isfinite(ah) or not np.isfinite(al) or not np.isfinite(a) or a <= 0:
            return []

        rng = ah - al
        adr = row["adr20"]
        if np.isfinite(adr) and adr > 0 and rng > self.max_range_vs_adr * adr:
            return []

        day = ts.normalize()
        st = state.setdefault("day", {})
        if st.get("date") != day:
            state["day"] = {"date": day, "side": None, "bar": None, "extreme": None, "done": False}
            st = state["day"]
        if st["done"]:
            return []

        buf = self.sweep_atr_mult * a

        # record / extend a sweep
        if row["high"] > ah + buf:
            if st["side"] != "high":
                st.update(side="high", bar=i, extreme=row["high"])
            else:
                st["extreme"] = max(st["extreme"], row["high"])
        elif row["low"] < al - buf:
            if st["side"] != "low":
                st.update(side="low", bar=i, extreme=row["low"])
            else:
                st["extreme"] = min(st["extreme"], row["low"])

        if st["side"] is None or i - st["bar"] > self.reclaim_within_bars:
            return []

        exit_at = ts.normalize() + pd.Timedelta(minutes=_hm(self.session_exit))
        stop_buf = self.stop_buffer_atr * a

        # reclaim back inside the range = signal
        if st["side"] == "high" and row["close"] < ah:
            entry_ref = row["close"]
            stop = st["extreme"] + stop_buf
            tp1 = al
            tp2 = min(row["prev_low"] if np.isfinite(row["prev_low"]) else al - a, al - 0.5 * a)
            risk = stop - entry_ref
            if risk <= 0 or (entry_ref - tp1) < self.min_rr_to_tp1 * risk:
                return []
            st["done"] = True
            return [
                Order(
                    created_at=ts, direction=-1, kind="market", stop_loss=stop,
                    tp1=tp1, tp2=tp2, time_exit=exit_at,
                    trail_atr_mult=self.trail_atr_mult, tag=self.name,
                )
            ]

        if st["side"] == "low" and row["close"] > al:
            entry_ref = row["close"]
            stop = st["extreme"] - stop_buf
            tp1 = ah
            tp2 = max(row["prev_high"] if np.isfinite(row["prev_high"]) else ah + a, ah + 0.5 * a)
            risk = entry_ref - stop
            if risk <= 0 or (tp1 - entry_ref) < self.min_rr_to_tp1 * risk:
                return []
            st["done"] = True
            return [
                Order(
                    created_at=ts, direction=1, kind="market", stop_loss=stop,
                    tp1=tp1, tp2=tp2, time_exit=exit_at,
                    trail_atr_mult=self.trail_atr_mult, tag=self.name,
                )
            ]

        return []


# --------------------------------------------------------------------- #
@dataclass
class NYOpeningRange(Strategy):
    """Strategy 2 — New York opening-range breakout.

    Range is the first `or_minutes` after 13:30 UTC (US data / equity open).
    Stop orders both sides, OCO. Optional H4 trend filter.
    """

    name: str = "ny_opening_range"
    timeframe: str = "15min"
    or_start: str = "13:30"
    or_minutes: int = 15
    buffer_atr_mult: float = 0.05
    target_range_mult: float = 1.5
    order_expiry: str = "16:00"
    session_exit: str = "20:00"
    use_trend_filter: bool = True
    bias_ema: int = 20
    trail_atr_mult: float | None = None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        h4 = df.resample("4h").agg({"close": "last"}).dropna()
        bias = ema(h4["close"], self.bias_ema).shift(1)   # last COMPLETED H4 bar
        df["h4_ema"] = bias.reindex(df.index, method="ffill")

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = bar_len
        start = _hm(self.or_start)
        # the opening range must be a whole number of bars starting exactly at
        # or_start, otherwise there is no bar boundary to measure it from
        self._or_minutes = max(self.or_minutes, bar_len)
        self._runnable = (start % bar_len == 0) and (self._or_minutes % bar_len == 0)
        if not self._runnable:
            print(
                f"  [warn] {self.name}: {bar_len}min bars do not align with an "
                f"opening range starting at {self.or_start}; strategy skipped. "
                f"Use a timeframe that divides into {self.or_start}."
            )
        elif self._or_minutes != self.or_minutes:
            print(
                f"  [warn] {self.name}: opening range widened from "
                f"{self.or_minutes}min to {self._or_minutes}min to fit "
                f"{bar_len}min bars — this is a different strategy, compare carefully."
            )
        return df

    def generate(self, df, i, state):
        if not getattr(self, "_runnable", False):
            return []

        ts = df.index[i]
        start = _hm(self.or_start)
        bar_len = self._bar_len
        end = start + self._or_minutes

        # fire once, on the bar that closes the opening range
        if _minutes(ts) + bar_len != end:
            return []

        n_bars = max(1, self._or_minutes // bar_len)
        window = df.iloc[max(0, i - n_bars + 1) : i + 1]
        window = window[(window.index.hour * 60 + window.index.minute) >= start]
        if window.empty:
            return []

        orh = float(window["high"].max())
        orl = float(window["low"].min())
        rng = orh - orl
        a = df["atr"].iloc[i]
        if rng <= 0 or not np.isfinite(a):
            return []

        buf = self.buffer_atr_mult * a
        day = ts.normalize()
        expiry = day + pd.Timedelta(minutes=_hm(self.order_expiry))
        exit_at = day + pd.Timedelta(minutes=_hm(self.session_exit))
        group = f"orb-{day.date()}"

        bias = df["h4_ema"].iloc[i]
        close = df["close"].iloc[i]
        allow_long = allow_short = True
        if self.use_trend_filter and np.isfinite(bias):
            allow_long = close > bias
            allow_short = close < bias

        orders = []
        if allow_long:
            entry = orh + buf
            orders.append(
                Order(
                    created_at=ts, direction=1, kind="stop", trigger=entry,
                    stop_loss=orl - buf, tp1=None,
                    tp2=entry + self.target_range_mult * rng,
                    expiry=expiry, time_exit=exit_at,
                    trail_atr_mult=self.trail_atr_mult,
                    tag=self.name, oco_group=group,
                )
            )
        if allow_short:
            entry = orl - buf
            orders.append(
                Order(
                    created_at=ts, direction=-1, kind="stop", trigger=entry,
                    stop_loss=orh + buf, tp1=None,
                    tp2=entry - self.target_range_mult * rng,
                    expiry=expiry, time_exit=exit_at,
                    trail_atr_mult=self.trail_atr_mult,
                    tag=self.name, oco_group=group,
                )
            )
        return orders


# --------------------------------------------------------------------- #
@dataclass
class TrendPullback(Strategy):
    """Strategy 3 — ADX-filtered pullback to the 20-EMA (Raschke style).

    Only trades when a real trend exists (ADX > threshold). Buys the first
    close back in the trend direction after price touches the 20-EMA.
    """

    name: str = "trend_pullback"
    timeframe: str = "15min"
    ema_period: int = 20
    adx_period: int = 14
    adx_threshold: float = 30.0
    swing_lookback: int = 6
    target_lookback: int = 20
    stop_buffer_atr: float = 0.10
    touch_valid_bars: int = 5
    min_rr: float = 1.0
    trail_atr_mult: float | None = 2.0
    session_filter: tuple[str, str] | None = ("07:00", "20:00")

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        df["ema"] = ema(df["close"], self.ema_period)
        a = adx(df, self.adx_period)
        df["adx"] = a["adx"]
        df["swing_low"] = df["low"].rolling(self.swing_lookback, min_periods=2).min()
        df["swing_high"] = df["high"].rolling(self.swing_lookback, min_periods=2).max()
        df["target_high"] = df["high"].rolling(self.target_lookback, min_periods=2).max()
        df["target_low"] = df["low"].rolling(self.target_lookback, min_periods=2).min()
        return df

    def generate(self, df, i, state):
        ts = df.index[i]
        if self.session_filter:
            m = _minutes(ts)
            if not (_hm(self.session_filter[0]) <= m <= _hm(self.session_filter[1])):
                return []

        row = df.iloc[i]
        e, a, adx_val = row["ema"], row["atr"], row["adx"]
        if not (np.isfinite(e) and np.isfinite(a) and np.isfinite(adx_val)) or a <= 0:
            return []
        if adx_val < self.adx_threshold:
            return []

        ema_prev = df["ema"].iloc[i - 1]
        up = row["close"] > e and e > ema_prev
        down = row["close"] < e and e < ema_prev

        touch = state.setdefault("touch", {"dir": 0, "bar": -10**9})
        if row["low"] <= e <= row["high"]:
            touch["dir"] = 1 if up else (-1 if down else 0)
            touch["bar"] = i
            return []

        if i - touch["bar"] > self.touch_valid_bars or touch["dir"] == 0:
            return []

        buf = self.stop_buffer_atr * a
        bullish = row["close"] > row["open"]

        if touch["dir"] == 1 and up and bullish:
            stop = min(row["swing_low"], e) - buf
            entry = row["close"]
            risk = entry - stop
            tp1 = max(row["target_high"], entry + risk)
            tp2 = entry + 3.0 * risk
            if risk <= 0 or (tp1 - entry) < self.min_rr * risk:
                return []
            touch["bar"] = -10**9
            return [
                Order(
                    created_at=ts, direction=1, kind="market", stop_loss=stop,
                    tp1=tp1, tp2=tp2, trail_atr_mult=self.trail_atr_mult, tag=self.name,
                )
            ]

        if touch["dir"] == -1 and down and not bullish:
            stop = max(row["swing_high"], e) + buf
            entry = row["close"]
            risk = stop - entry
            tp1 = min(row["target_low"], entry - risk)
            tp2 = entry - 3.0 * risk
            if risk <= 0 or (entry - tp1) < self.min_rr * risk:
                return []
            touch["bar"] = -10**9
            return [
                Order(
                    created_at=ts, direction=-1, kind="market", stop_loss=stop,
                    tp1=tp1, tp2=tp2, trail_atr_mult=self.trail_atr_mult, tag=self.name,
                )
            ]

        return []


# --------------------------------------------------------------------- #
@dataclass
class LiquiditySweepReversal(Strategy):
    """Strategy 4 — XAUUSD Liquidity Sweep Reversal (research brief,
    "Strategy 1"): HTF bias -> session liquidity sweep -> market structure
    shift (MSS) -> displacement -> fair value gap (FVG) -> retracement
    entry, targeting opposing liquidity at a minimum 1:2 RR.

    Simplifications from the brief (kept honest rather than hidden):
      - "prior swing high/low" for the MSS check is a rolling N-bar extreme
        (indicators.prior_swing_high/low), not a formal confirmed fractal
        pivot — cheaper to compute, close enough in practice.
      - Equal highs/lows and round-number levels aren't separate liquidity
        pools; PDH/PDL, the Asian range and the rolling swing extreme cover
        the same structural role.
      - H1 bias is a lightweight EMA filter, off by default (`require_htf_bias`)
        since the brief states this step as "bullish OR structurally
        supportive" rather than a hard formula — Strategy 2 below has an
        exact HTF formula and applies it as a hard gate instead.
    """

    name: str = "liquidity_sweep_reversal"
    timeframe: str = "15min"
    htf_timeframe: str = "1h"
    htf_ema_period: int = 50
    require_htf_bias: bool = False
    asian_start: str = "00:00"
    asian_end: str = "06:00"
    session_start: str = "07:00"   # London + New York, the brief's stated focus
    session_end: str = "20:00"
    swing_lookback: int = 10
    target_lookback: int = 50   # wider window for "next opposing liquidity" — the MSS-detection swing has usually just been broken, so it can't double as the target
    mss_within_bars: int = 20
    fvg_within_bars: int = 15
    entry_within_bars: int = 20
    displacement_atr_mult: float = 0.8
    displacement_body_ratio: float = 0.6
    fvg_min_atr_mult: float = 0.10
    stop_buffer_atr: float = 0.15
    min_rr: float = 2.0
    retracement_pct: float = 0.5
    trail_atr_mult: float | None = None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)

        h1 = df.resample(self.htf_timeframe).agg({"close": "last"}).dropna()
        htf_bias_ema = ema(h1["close"], self.htf_ema_period).shift(1)
        df["htf_bias_ema"] = htf_bias_ema.reindex(df.index, method="ffill")

        daily = df.resample("1D").agg({"high": "max", "low": "min"}).dropna()
        prev = daily.shift(1)
        prev.index = prev.index.normalize()
        day = df.index.normalize()
        df["pdh"] = pd.Series(day, index=df.index).map(prev["high"])
        df["pdl"] = pd.Series(day, index=df.index).map(prev["low"])

        mins = df.index.hour * 60 + df.index.minute
        in_asia = (mins >= _hm(self.asian_start)) & (mins < _hm(self.asian_end))
        asia = df[in_asia].groupby(day[in_asia]).agg({"high": "max", "low": "min"})
        df["asian_high"] = pd.Series(day, index=df.index).map(asia["high"])
        df["asian_low"] = pd.Series(day, index=df.index).map(asia["low"])

        df["swing_high_prior"] = prior_swing_high(df, self.swing_lookback)
        df["swing_low_prior"] = prior_swing_low(df, self.swing_lookback)
        df["swing_high_wide"] = prior_swing_high(df, self.target_lookback)
        df["swing_low_wide"] = prior_swing_low(df, self.target_lookback)

        fvg = fair_value_gaps(df)
        for col in fvg.columns:
            df[col] = fvg[col]
        disp = displacement(df, df["atr"], self.displacement_atr_mult, self.displacement_body_ratio)
        for col in disp.columns:
            df[col] = disp[col]

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = max(bar_len, 1)
        return df

    def generate(self, df, i, state):
        ts = df.index[i]
        if not (_hm(self.session_start) <= _minutes(ts) <= _hm(self.session_end)):
            return []
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []

        seq = state.get("seq")
        if seq is not None and i - seq["anchor_bar"] > self.mss_within_bars + self.fvg_within_bars:
            seq = None
        state["seq"] = seq

        if seq is None:
            sell_liq = _finite_max(row["pdl"], row["asian_low"], row["swing_low_prior"])
            buy_liq = _finite_min(row["pdh"], row["asian_high"], row["swing_high_prior"])
            bias_ema = row["htf_bias_ema"]
            htf_ok_long = (not self.require_htf_bias) or (np.isfinite(bias_ema) and row["close"] > bias_ema)
            htf_ok_short = (not self.require_htf_bias) or (np.isfinite(bias_ema) and row["close"] < bias_ema)

            if np.isfinite(sell_liq) and row["low"] < sell_liq and row["close"] > sell_liq and htf_ok_long:
                state["seq"] = {"side": 1, "extreme": row["low"], "anchor_bar": i, "stage": "swept"}
            elif np.isfinite(buy_liq) and row["high"] > buy_liq and row["close"] < buy_liq and htf_ok_short:
                state["seq"] = {"side": -1, "extreme": row["high"], "anchor_bar": i, "stage": "swept"}
            return []

        if seq["stage"] == "swept":
            if seq["side"] == 1 and row["close"] > row["swing_high_prior"]:
                seq["stage"], seq["mss_bar"] = "mss", i
            elif seq["side"] == -1 and row["close"] < row["swing_low_prior"]:
                seq["stage"], seq["mss_bar"] = "mss", i
            return []

        # stage == "mss": waiting for displacement + a same-direction FVG
        if i - seq["mss_bar"] > self.fvg_within_bars:
            state["seq"] = None
            return []

        top = bottom = None
        if seq["side"] == 1 and row["disp_bull"] and row["bull_fvg"] and row["bull_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bull_fvg_top"], row["bull_fvg_bottom"]
        elif seq["side"] == -1 and row["disp_bear"] and row["bear_fvg"] and row["bear_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bear_fvg_top"], row["bear_fvg_bottom"]
        if top is None:
            return []

        direction = seq["side"]
        extreme = seq["extreme"]
        entry_level = bottom + self.retracement_pct * (top - bottom)
        stop_buf = self.stop_buffer_atr * a
        expiry = ts + pd.Timedelta(minutes=self.entry_within_bars * self._bar_len)
        state["seq"] = None  # the sequence is consumed here whether or not a valid order results

        if direction == 1:
            stop = extreme - stop_buf
            risk = entry_level - stop
            if risk <= 0:
                return []
            target = _forward_target(1, entry_level, row["pdh"], row["asian_high"], row["swing_high_wide"])
            if not np.isfinite(target) or (target - entry_level) < self.min_rr * risk:
                return []  # no opposing liquidity clearing the minimum RR — no trade
        else:
            stop = extreme + stop_buf
            risk = stop - entry_level
            if risk <= 0:
                return []
            target = _forward_target(-1, entry_level, row["pdl"], row["asian_low"], row["swing_low_wide"])
            if not np.isfinite(target) or (entry_level - target) < self.min_rr * risk:
                return []

        return [
            Order(
                created_at=ts, direction=direction, kind="limit", trigger=entry_level,
                stop_loss=stop, tp1=target, expiry=expiry,
                trail_atr_mult=self.trail_atr_mult, tag=self.name,
            )
        ]


# --------------------------------------------------------------------- #
@dataclass
class LiquidityImbalanceContinuation(Strategy):
    """Strategy 5 — XAUUSD Liquidity + Imbalance Continuation (research
    brief, "Strategy 2"): a session-independent continuation model —
    liquidity sweep -> displacement -> MSS -> FVG -> retracement entry ->
    continuation toward opposing liquidity, gated by an explicit H4
    EMA(50)/EMA(200) bias filter.

    Mechanically the same pipeline as LiquiditySweepReversal above, with
    the differences the brief specifies: an explicit minimum sweep
    penetration (ATR-normalized, not "any" breach), a hard HTF bias gate,
    and no session restriction by default.

    Not implemented: the optional 0-10 "Setup Quality Score" gate — several
    of its factors (liquidity swept, strong displacement, HTF agreement)
    are already hard preconditions here rather than soft-scored ones, and
    the remaining factors (proximity to major liquidity, session
    volatility) would need liquidity-pool ranking this module doesn't do.
    Left out rather than half-built; the mechanical pipeline itself is
    the substance of the strategy.
    """

    name: str = "liquidity_imbalance_continuation"
    timeframe: str = "15min"
    htf_timeframe: str = "4h"
    htf_ema_fast: int = 50
    htf_ema_slow: int = 200
    session_filter: tuple[str, str] | None = None  # None = trade any session; brief says analyze sessions separately after the fact
    swing_lookback: int = 10
    target_lookback: int = 50   # wider window for "next opposing liquidity" — see LiquiditySweepReversal's note on the same field
    sweep_atr_mult: float = 0.05     # minimum penetration beyond the liquidity level
    mss_within_bars: int = 20
    fvg_within_bars: int = 15
    entry_within_bars: int = 20
    displacement_atr_mult: float = 0.8
    displacement_body_ratio: float = 0.6
    fvg_min_atr_mult: float = 0.10
    stop_buffer_atr: float = 0.10
    min_rr: float = 2.0
    retracement_pct: float = 0.5
    trail_atr_mult: float | None = None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        df["htf_bias"] = htf_ema_bias(df, self.htf_timeframe, self.htf_ema_fast, self.htf_ema_slow)

        daily = df.resample("1D").agg({"high": "max", "low": "min"}).dropna()
        prev = daily.shift(1)
        prev.index = prev.index.normalize()
        day = df.index.normalize()
        df["pdh"] = pd.Series(day, index=df.index).map(prev["high"])
        df["pdl"] = pd.Series(day, index=df.index).map(prev["low"])

        weekly = df.resample("1W").agg({"high": "max", "low": "min"}).dropna()
        prev_w = weekly.shift(1)
        # week-ending Sunday, computed arithmetically (not via to_period,
        # which silently drops the tz-aware index) to match resample("1W")'s
        # own (tz-aware) bin labels
        days_to_sunday = (6 - df.index.dayofweek) % 7
        week_key = (df.index + pd.to_timedelta(days_to_sunday, unit="D")).normalize()
        df["pwh"] = pd.Series(week_key, index=df.index).map(prev_w["high"])
        df["pwl"] = pd.Series(week_key, index=df.index).map(prev_w["low"])

        df["swing_high_prior"] = prior_swing_high(df, self.swing_lookback)
        df["swing_low_prior"] = prior_swing_low(df, self.swing_lookback)
        df["swing_high_wide"] = prior_swing_high(df, self.target_lookback)
        df["swing_low_wide"] = prior_swing_low(df, self.target_lookback)

        fvg = fair_value_gaps(df)
        for col in fvg.columns:
            df[col] = fvg[col]
        disp = displacement(df, df["atr"], self.displacement_atr_mult, self.displacement_body_ratio)
        for col in disp.columns:
            df[col] = disp[col]

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = max(bar_len, 1)
        return df

    def generate(self, df, i, state):
        ts = df.index[i]
        if self.session_filter:
            start, end = self.session_filter
            if not (_hm(start) <= _minutes(ts) <= _hm(end)):
                return []
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []

        seq = state.get("seq")
        if seq is not None and i - seq["anchor_bar"] > self.mss_within_bars + self.fvg_within_bars:
            seq = None
        state["seq"] = seq

        if seq is None:
            bias = row["htf_bias"]
            sell_liq = _finite_max(row["pwl"], row["pdl"], row["swing_low_prior"])
            buy_liq = _finite_min(row["pwh"], row["pdh"], row["swing_high_prior"])
            pen = self.sweep_atr_mult * a

            if bias == 1 and np.isfinite(sell_liq) and row["low"] < sell_liq - pen and row["close"] > sell_liq:
                state["seq"] = {"side": 1, "extreme": row["low"], "anchor_bar": i, "stage": "swept"}
            elif bias == -1 and np.isfinite(buy_liq) and row["high"] > buy_liq + pen and row["close"] < buy_liq:
                state["seq"] = {"side": -1, "extreme": row["high"], "anchor_bar": i, "stage": "swept"}
            return []

        if seq["stage"] == "swept":
            if seq["side"] == 1 and row["close"] > row["swing_high_prior"]:
                seq["stage"], seq["mss_bar"] = "mss", i
            elif seq["side"] == -1 and row["close"] < row["swing_low_prior"]:
                seq["stage"], seq["mss_bar"] = "mss", i
            return []

        if i - seq["mss_bar"] > self.fvg_within_bars:
            state["seq"] = None
            return []

        top = bottom = None
        if seq["side"] == 1 and row["disp_bull"] and row["bull_fvg"] and row["bull_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bull_fvg_top"], row["bull_fvg_bottom"]
        elif seq["side"] == -1 and row["disp_bear"] and row["bear_fvg"] and row["bear_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bear_fvg_top"], row["bear_fvg_bottom"]
        if top is None:
            return []

        direction = seq["side"]
        extreme = seq["extreme"]
        entry_level = bottom + self.retracement_pct * (top - bottom)
        stop_buf = self.stop_buffer_atr * a
        expiry = ts + pd.Timedelta(minutes=self.entry_within_bars * self._bar_len)
        state["seq"] = None

        if direction == 1:
            stop = extreme - stop_buf
            risk = entry_level - stop
            if risk <= 0:
                return []
            target = _forward_target(1, entry_level, row["pwh"], row["pdh"], row["swing_high_wide"])
            if not np.isfinite(target) or (target - entry_level) < self.min_rr * risk:
                return []
        else:
            stop = extreme + stop_buf
            risk = stop - entry_level
            if risk <= 0:
                return []
            target = _forward_target(-1, entry_level, row["pwl"], row["pdl"], row["swing_low_wide"])
            if not np.isfinite(target) or (entry_level - target) < self.min_rr * risk:
                return []

        return [
            Order(
                created_at=ts, direction=direction, kind="limit", trigger=entry_level,
                stop_loss=stop, tp1=target, expiry=expiry,
                trail_atr_mult=self.trail_atr_mult, tag=self.name,
            )
        ]


@dataclass
class FibonacciRetracement(Strategy):
    """Fibonacci retracement continuation: a confirmed ZigZag swing leg
    (indicators.zigzag_pivots) defines the impulse; once price retraces into
    the 50%-61.8% zone of that leg and a reversal candle confirms, enter in
    the direction of the ORIGINAL leg (continuation, not counter-trend),
    stop just beyond the 78.6% invalidation level, target the prior extreme.

    Direction is inferred from which pivot (high or low) confirmed more
    recently: high-after-low means the leg ran up (retracement -> long),
    low-after-high means the leg ran down (retracement -> short). Each leg
    (identified by its (high_bar, low_bar) pair) is only ever traded once.
    """

    name: str = "fib_retracement"
    timeframe: str = "15min"
    zigzag_atr_mult: float = 2.0
    min_leg_atr_mult: float = 4.0
    stop_buffer_atr: float = 0.15
    min_rr: float = 1.5
    trail_atr_mult: float | None = None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)
        pivot_high, pivot_low = zigzag_pivots(df, df["atr"], self.zigzag_atr_mult)
        df["pivot_high"] = pivot_high
        df["pivot_low"] = pivot_low
        df["cdl_bull_engulf"] = bullish_engulfing(df)
        df["cdl_bear_engulf"] = bearish_engulfing(df)
        df["cdl_hammer"] = hammer(df)
        df["cdl_star"] = shooting_star(df)
        return df

    def generate(self, df, i, state):
        ts = df.index[i]
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []

        if np.isfinite(row["pivot_high"]):
            state["last_high"], state["high_bar"] = row["pivot_high"], i
        if np.isfinite(row["pivot_low"]):
            state["last_low"], state["low_bar"] = row["pivot_low"], i

        last_high, last_low = state.get("last_high"), state.get("last_low")
        high_bar, low_bar = state.get("high_bar"), state.get("low_bar")
        if last_high is None or last_low is None:
            return []

        leg_size = last_high - last_low
        if leg_size <= 0 or leg_size < self.min_leg_atr_mult * a:
            return []

        leg_id = (high_bar, low_bar)
        if state.get("traded_leg") == leg_id:
            return []

        direction = 1 if high_bar > low_bar else -1
        if direction == 1:
            fib_50 = last_high - 0.5 * leg_size
            fib_618 = last_high - 0.618 * leg_size
            fib_786 = last_high - 0.786 * leg_size
        else:
            fib_50 = last_low + 0.5 * leg_size
            fib_618 = last_low + 0.618 * leg_size
            fib_786 = last_low + 0.786 * leg_size
        zone_lo, zone_hi = sorted([fib_50, fib_618])

        if direction == 1:
            touched = zone_lo <= row["low"] <= zone_hi or fib_786 <= row["low"] <= zone_hi
            reversal = row["cdl_bull_engulf"] or row["cdl_hammer"]
            if not (touched and row["low"] >= fib_786 and reversal and row["close"] > row["open"]):
                return []
            stop = fib_786 - self.stop_buffer_atr * a
            entry = row["close"]
            risk = entry - stop
            if risk <= 0:
                return []
            target = last_high
            if (target - entry) < self.min_rr * risk:
                return []
            state["traded_leg"] = leg_id
            return [Order(created_at=ts, direction=1, kind="market", stop_loss=stop,
                           tp1=target, trail_atr_mult=self.trail_atr_mult, tag=self.name)]
        else:
            touched = zone_lo <= row["high"] <= zone_hi or zone_lo <= row["high"] <= fib_786
            reversal = row["cdl_bear_engulf"] or row["cdl_star"]
            if not (touched and row["high"] <= fib_786 and reversal and row["close"] < row["open"]):
                return []
            stop = fib_786 + self.stop_buffer_atr * a
            entry = row["close"]
            risk = stop - entry
            if risk <= 0:
                return []
            target = last_low
            if (entry - target) < self.min_rr * risk:
                return []
            state["traded_leg"] = leg_id
            return [Order(created_at=ts, direction=-1, kind="market", stop_loss=stop,
                           tp1=target, trail_atr_mult=self.trail_atr_mult, tag=self.name)]


@dataclass
class LiquiditySweepMSSFVGRetest(Strategy):
    """XAUUSD 4H Liquidity Sweep + MSS + FVG Retest (user-supplied research
    brief): a 4H liquidity pool (4H swing high/low, PDH/PDL, PWH/PWL) is
    swept and reclaimed, a working-timeframe Market Structure Shift (MSS)
    confirms the reversal, the displacement that follows must carve a
    fresh, sufficiently-sized FVG, and the entry only triggers on a
    retracement back into that FVG — never on the initial displacement.

    Model: 4H LIQUIDITY -> SWEEP -> MSS -> DISPLACEMENT -> FVG -> RETEST ->
    ENTRY -> OPPOSING LIQUIDITY, with stop beyond the 4H sweep extreme and
    a minimum planned 1:2 RR to opposing liquidity, per the brief.

    Simplifications, kept honest rather than hidden:
      - "Equal highs/lows" as a separate liquidity pool aren't modelled;
        the 4H swing extreme + PDH/PDL + PWH/PWL cover the same structural
        role (the same simplification LiquiditySweepReversal already makes
        for its own liquidity pools).
      - The brief's "conservative" 5M/1M sweep+MSS confirmation isn't a
        genuine nested lower-timeframe check — this engine walks one bar
        series at a time, so there's no separate 5M feed to inspect mid-bar.
        `require_ltf_confirmation` substitutes a same-bar reversal
        candlestick pattern (engulfing/hammer/shooting star) at the retest
        bar as a proxy. Off by default, matching the brief's own
        "aggressive" model (more trades, no LTF confirmation required).
      - Session filtering (London vs New York) isn't built into this class
        — it's already handled uniformly for every strategy by the
        platform's session filter (SessionFilterStrategy), so the brief's
        Section 10 requirement is satisfied at the app level instead of
        being duplicated here.
    """

    name: str = "sweep_mss_fvg_4h"
    timeframe: str = "15min"
    htf_timeframe: str = "4h"
    htf_swing_lookback: int = 10
    mss_lookback: int = 10
    target_lookback: int = 50
    mss_within_bars: int = 20
    fvg_within_bars: int = 15
    entry_within_bars: int = 20
    displacement_atr_mult: float = 0.8
    displacement_body_ratio: float = 0.6
    fvg_min_atr_mult: float = 0.10
    stop_buffer_atr: float = 0.15
    min_rr: float = 2.0
    retracement_pct: float = 0.5
    require_ltf_confirmation: bool = False
    trail_atr_mult: float | None = None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)

        four_h = df.resample(self.htf_timeframe).agg({"high": "max", "low": "min"}).dropna()
        four_h_swing_high = prior_swing_high(four_h, self.htf_swing_lookback)
        four_h_swing_low = prior_swing_low(four_h, self.htf_swing_lookback)
        df["htf_swing_high"] = four_h_swing_high.reindex(df.index, method="ffill")
        df["htf_swing_low"] = four_h_swing_low.reindex(df.index, method="ffill")

        daily = df.resample("1D").agg({"high": "max", "low": "min"}).dropna()
        prev_d = daily.shift(1)
        prev_d.index = prev_d.index.normalize()
        day = df.index.normalize()
        df["pdh"] = pd.Series(day, index=df.index).map(prev_d["high"])
        df["pdl"] = pd.Series(day, index=df.index).map(prev_d["low"])

        week = df.index.tz_localize(None).to_period("W")
        weekly = df.groupby(week).agg({"high": "max", "low": "min"})
        prev_w = weekly.shift(1)
        df["pwh"] = pd.Series(week, index=df.index).map(prev_w["high"])
        df["pwl"] = pd.Series(week, index=df.index).map(prev_w["low"])

        df["swing_high_mss"] = prior_swing_high(df, self.mss_lookback)
        df["swing_low_mss"] = prior_swing_low(df, self.mss_lookback)
        df["swing_high_target"] = prior_swing_high(df, self.target_lookback)
        df["swing_low_target"] = prior_swing_low(df, self.target_lookback)

        fvg = fair_value_gaps(df)
        for col in fvg.columns:
            df[col] = fvg[col]
        disp = displacement(df, df["atr"], self.displacement_atr_mult, self.displacement_body_ratio)
        for col in disp.columns:
            df[col] = disp[col]

        df["cdl_bull_engulf"] = bullish_engulfing(df)
        df["cdl_bear_engulf"] = bearish_engulfing(df)
        df["cdl_hammer"] = hammer(df)
        df["cdl_star"] = shooting_star(df)

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = max(bar_len, 1)
        return df

    def generate(self, df, i, state):
        ts = df.index[i]
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []

        seq = state.get("seq")
        if seq is not None and i - seq["anchor_bar"] > self.mss_within_bars + self.fvg_within_bars:
            seq = None
        state["seq"] = seq

        if seq is None:
            sell_liq = _finite_max(row["pdl"], row["pwl"], row["htf_swing_low"])
            buy_liq = _finite_min(row["pdh"], row["pwh"], row["htf_swing_high"])

            if np.isfinite(sell_liq) and row["low"] < sell_liq and row["close"] > sell_liq:
                state["seq"] = {"side": 1, "extreme": row["low"], "anchor_bar": i, "stage": "swept"}
            elif np.isfinite(buy_liq) and row["high"] > buy_liq and row["close"] < buy_liq:
                state["seq"] = {"side": -1, "extreme": row["high"], "anchor_bar": i, "stage": "swept"}
            return []

        if seq["stage"] == "swept":
            if seq["side"] == 1 and row["close"] > row["swing_high_mss"]:
                seq["stage"], seq["mss_bar"] = "mss", i
            elif seq["side"] == -1 and row["close"] < row["swing_low_mss"]:
                seq["stage"], seq["mss_bar"] = "mss", i
            return []

        # stage == "mss": waiting for displacement + a fresh same-direction FVG
        if i - seq["mss_bar"] > self.fvg_within_bars:
            state["seq"] = None
            return []

        top = bottom = None
        if seq["side"] == 1 and row["disp_bull"] and row["bull_fvg"] and row["bull_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bull_fvg_top"], row["bull_fvg_bottom"]
        elif seq["side"] == -1 and row["disp_bear"] and row["bear_fvg"] and row["bear_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bear_fvg_top"], row["bear_fvg_bottom"]
        if top is None:
            return []

        if self.require_ltf_confirmation:
            reversal_ok = (row["cdl_bull_engulf"] or row["cdl_hammer"]) if seq["side"] == 1 else (row["cdl_bear_engulf"] or row["cdl_star"])
            if not reversal_ok:
                return []

        direction = seq["side"]
        extreme = seq["extreme"]
        entry_level = bottom + self.retracement_pct * (top - bottom)
        stop_buf = self.stop_buffer_atr * a
        expiry = ts + pd.Timedelta(minutes=self.entry_within_bars * self._bar_len)
        state["seq"] = None  # the sequence is consumed here whether or not a valid order results

        if direction == 1:
            stop = extreme - stop_buf
            risk = entry_level - stop
            if risk <= 0:
                return []
            target = _forward_target(1, entry_level, row["pdh"], row["pwh"], row["swing_high_target"])
            if not np.isfinite(target) or (target - entry_level) < self.min_rr * risk:
                return []
        else:
            stop = extreme + stop_buf
            risk = stop - entry_level
            if risk <= 0:
                return []
            target = _forward_target(-1, entry_level, row["pdl"], row["pwl"], row["swing_low_target"])
            if not np.isfinite(target) or (entry_level - target) < self.min_rr * risk:
                return []

        return [
            Order(
                created_at=ts, direction=direction, kind="limit", trigger=entry_level,
                stop_loss=stop, tp1=target, expiry=expiry,
                trail_atr_mult=self.trail_atr_mult, tag=self.name,
            )
        ]


@dataclass
class SecondFVGRetest(Strategy):
    """XAUUSD Liquidity Sweep -> Displacement -> FVG Retest, v2 (per the
    user's own revised spec, superseding the v1 build below). A multi-
    timeframe continuation model: the HTF supplies direction (a swing pivot
    is swept, setting bias), the LTF supplies timing (its own local sweep,
    then a displacement leg that carves a fresh FVG). Two variants of the
    SAME state machine, chosen with `variant`:
      - "A": enter on the FIRST FVG's own retest (baseline — more trades).
      - "B" (default, this class's namesake): wait for FVG #1 to be
        retested and HELD (not consumed), then enter on a SECOND FVG
        formed in the following leg — fewer, later, safer entries.

    v2 changes from v1, per the spec's own diagnosis of why v1 took zero
    trades:
      - Swing pivots are now a genuine n-bar CENTERED fractal
        (indicators.confirmed_swing_high/low: high[i] == max(high[i-n:i+n+1])),
        not a trailing rolling-lookback max. Critically, the pivot's own
        price is only exposed at bar i+n — the earliest point it's
        actually knowable — instead of being available immediately with no
        confirmation lag. A mismatch between how long a level takes to
        confirm and how long the following window allows to react to it is
        the single most likely cause of a strategy that backtests to zero
        trades; loosening timing windows without fixing the pivot
        definition would not have helped.
      - Every window tightened for QUALITY (fvg1_within_bars 15->5, the
        retest/fvg2/entry windows shortened) while ltf_sweep_within_bars
        widened substantially (20->48 at the 15min/4h reference pairing)
        and should be DERIVED from the HTF:LTF ratio via build_config()
        below when targeting a different timeframe pair — it's the only
        parameter that bridges two timeframes, so it's the only one that
        needs to scale.
      - Displacement and FVG-size filters tightened — 0.8x ATR is an
        ordinary candle, and 0.1x ATR on gold is inside the spread on low
        timeframes.
      - Stop moved from "beyond the entry FVG" to "beyond the ORIGINAL
        sweep wick that started the sequence" — a real, deliberate change
        in where risk is measured from, not a refinement of the same idea.
      - Explicit invalidation: a live sequence now resets not only on
        window expiry but also if price closes back through the original
        sweep extreme (the reversal premise is broken), if the active FVG
        is fully closed through (consumed rather than respected), or if a
        fresh HTF sweep — same direction or opposite — supersedes the one
        currently being tracked (only one live sequence at a time).
      - funnel_stats (populated during generate(), read after a backtest)
        counts how many times each stage was reached and why a sequence
        expired at each one — the difference between "genuinely rare" and
        "a filter is silently killing every candidate" is only visible
        with this instrumented, not guessed at from a raw trade count.

    Kept simplifications (both already true of v1, unchanged since the
    spec doesn't ask for anything different here):
      - Equal highs/lows aren't a separate liquidity pool — the HTF swing
        pivot alone is the liquidity reference, per the spec's own §2/§3
        (unlike v1, this version does NOT additionally mix in PDH/PDL/
        PWH/PWL — the spec defines the HTF leg purely off the swing pivot).
      - The displacement -> FVG check for stage 3->4->5 is combined into a
        single step (a bar showing both displacement AND a qualifying FVG
        advances directly), rather than tracking an intermediate
        "displaced, no FVG yet" state of its own — the spec describes the
        concept in two boxes but doesn't call this merge out as a source
        of v1's problems, unlike the pivot definition and window sizes.
    """

    name: str = "second_fvg_retest"
    timeframe: str = "15min"
    htf_timeframe: str = "4h"
    variant: str = "B"  # "A" = enter on FVG1's own retest; "B" = wait for FVG2
    htf_swing_lookback: int = 5
    ltf_swing_lookback: int = 5
    ltf_sweep_within_bars: int = 48
    fvg1_within_bars: int = 5
    fvg1_retest_within_bars: int = 20
    fvg2_within_bars: int = 15
    entry_within_bars: int = 10
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.5
    fvg_min_atr_mult: float = 0.2
    stop_buffer_atr: float = 0.25
    rr: float = 2.0
    retracement_pct: float = 0.5
    trail_atr_mult: float | None = 1.5

    # timeframe-pair helpers (spec §6.2-6.4) — plain class attributes, not
    # dataclass fields (unannotated, so @dataclass leaves them alone)
    TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30,
                  "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
    DEFAULT_HTF = {"1min": "15min", "5min": "1h", "15min": "4h",
                   "30min": "4h", "1h": "1d", "4h": "1w", "1d": "1w"}

    @classmethod
    def build_config(cls, ltf: str, htf: str | None = None, variant: str = "A") -> dict:
        """A scaled param set for a given entry/HTF pair (spec §6.4): every
        ATR-based and bar-count param stays fixed across timeframes — only
        ltf_sweep_within_bars is derived from the HTF:LTF ratio (roughly 3
        HTF bars' worth of reaction time)."""
        htf = htf or cls.DEFAULT_HTF[ltf]
        ratio = cls.TF_MINUTES[htf] / cls.TF_MINUTES[ltf]
        return {
            "timeframe": ltf, "htf_timeframe": htf, "variant": variant,
            "htf_swing_lookback": 5, "ltf_swing_lookback": 5,
            "ltf_sweep_within_bars": int(3 * ratio),
            "fvg1_within_bars": 5, "fvg1_retest_within_bars": 20,
            "fvg2_within_bars": 15, "entry_within_bars": 10,
            "displacement_atr_mult": 1.2, "displacement_body_ratio": 0.5,
            "fvg_min_atr_mult": 0.2, "stop_buffer_atr": 0.25,
            "rr": 2.0, "retracement_pct": 0.5, "trail_atr_mult": 1.5,
        }

    def __post_init__(self):
        # diagnostics only (spec §7) — never read by generate() itself
        self.funnel_stats = {
            "htf_pivots": 0, "htf_sweep": 0, "ltf_sweep": 0, "displacement": 0,
            "fvg1": 0, "fvg1_retest": 0, "fvg2": 0, "entry": 0,
            "expired": {}, "invalidated": {},
        }

    def _bump(self, bucket: str, key: str) -> None:
        self.funnel_stats[bucket][key] = self.funnel_stats[bucket].get(key, 0) + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)

        four_h = df.resample(self.htf_timeframe).agg({"high": "max", "low": "min"}).dropna()
        n = self.htf_swing_lookback
        htf_pivot_high = four_h["high"] == four_h["high"].rolling(2 * n + 1, center=True).max()
        htf_pivot_low = four_h["low"] == four_h["low"].rolling(2 * n + 1, center=True).min()
        self.funnel_stats["htf_pivots"] = int(htf_pivot_high.sum() + htf_pivot_low.sum())
        htf_swing_high = four_h["high"].where(htf_pivot_high).shift(n).ffill()
        htf_swing_low = four_h["low"].where(htf_pivot_low).shift(n).ffill()
        df["htf_swing_high"] = htf_swing_high.reindex(df.index, method="ffill")
        df["htf_swing_low"] = htf_swing_low.reindex(df.index, method="ffill")

        df["ltf_swing_high"] = confirmed_swing_high(df, self.ltf_swing_lookback)
        df["ltf_swing_low"] = confirmed_swing_low(df, self.ltf_swing_lookback)

        fvg = fair_value_gaps(df)
        for col in fvg.columns:
            df[col] = fvg[col]
        disp = displacement(df, df["atr"], self.displacement_atr_mult, self.displacement_body_ratio)
        for col in disp.columns:
            df[col] = disp[col]

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = max(bar_len, 1)
        return df

    def _htf_swept(self, row) -> int | None:
        """+1 if the HTF swing LOW was swept (bullish reversal bias), -1 if
        the HTF swing HIGH was swept (bearish), else None. A close ABOVE a
        swept high (or below a swept low) is a breakout, not a sweep —
        excluded here (spec §2.2)."""
        if pd.notna(row["htf_swing_low"]) and row["low"] < row["htf_swing_low"] and row["close"] > row["htf_swing_low"]:
            return 1
        if pd.notna(row["htf_swing_high"]) and row["high"] > row["htf_swing_high"] and row["close"] < row["htf_swing_high"]:
            return -1
        return None

    def _build_entry(self, ts, seq, top: float, bottom: float, a: float) -> "Order | None":
        direction = seq["side"]
        entry_level = bottom + self.retracement_pct * (top - bottom)
        stop_buf = self.stop_buffer_atr * a
        expiry = ts + pd.Timedelta(minutes=self.entry_within_bars * self._bar_len)
        extreme = seq["sweep_extreme"]  # spec §4: stop is beyond the ORIGINAL sweep wick, not the FVG

        if direction == 1:
            stop = extreme - stop_buf
            risk = entry_level - stop
            if risk <= 0:
                return None
            target = entry_level + self.rr * risk
        else:
            stop = extreme + stop_buf
            risk = stop - entry_level
            if risk <= 0:
                return None
            target = entry_level - self.rr * risk

        return Order(
            created_at=ts, direction=direction, kind="limit", trigger=entry_level,
            stop_loss=stop, tp1=target, expiry=expiry,
            trail_atr_mult=self.trail_atr_mult, tag=self.name,
        )

    def generate(self, df, i, state):
        ts = df.index[i]
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []

        seq = state.get("seq")

        # A fresh HTF sweep always supersedes a live sequence going the
        # OPPOSITE way (spec §5 rule 4: "an opposing HTF sweep... flipping
        # bias"), and always starts a brand new sequence if none is live.
        # A same-direction re-sweep while still waiting on the LTF leg
        # ("swept_htf" stage) is usually just chop around the SAME shallow
        # (n=5) pivot rather than a genuinely new setup — per the funnel
        # counter (spec §7), treating every one of those as a hard reset
        # left almost nothing alive long enough to reach an entry (547 of
        # 954 sequences died this way). Instead: only escalate the tracked
        # extreme if the re-sweep actually goes deeper (a real, larger
        # stop-hunt), and otherwise let the existing sequence's window and
        # progress keep running untouched — a shallower repeat sweep isn't
        # evidence the original one was wrong.
        fresh_sweep = self._htf_swept(row)
        if fresh_sweep is not None:
            if seq is None or fresh_sweep != seq["side"]:
                if seq is not None:
                    self._bump("invalidated", "opposing_htf_sweep")
                seq = {
                    "side": fresh_sweep, "stage": "swept_htf",
                    "sweep_extreme": row["low"] if fresh_sweep == 1 else row["high"],
                    "anchor_bar": i,
                }
                state["seq"] = seq
                self.funnel_stats["htf_sweep"] += 1
                return []
            if seq["stage"] == "swept_htf":
                new_extreme = row["low"] if fresh_sweep == 1 else row["high"]
                deeper = (new_extreme < seq["sweep_extreme"]) if fresh_sweep == 1 else (new_extreme > seq["sweep_extreme"])
                if deeper:
                    self._bump("invalidated", "same_direction_extreme_updated")
                    seq["sweep_extreme"] = new_extreme
                # else: noise around the same pool — fall through to the
                # normal stage handling below instead of resetting.

        if seq is None:
            return []

        # Rule 2 (§5): a close back beyond the original sweep extreme
        # breaks the reversal premise outright, at any stage.
        broken = (row["close"] < seq["sweep_extreme"]) if seq["side"] == 1 else (row["close"] > seq["sweep_extreme"])
        if broken:
            self._bump("invalidated", "sweep_extreme_broken")
            state["seq"] = None
            return []

        # Rule 3 (§5): the active FVG fully closed through — consumed, not
        # respected — invalidates before its retest can be traded.
        if seq["stage"] in ("fvg1_set", "fvg1_held") and "fvg1_top" in seq:
            consumed = (row["close"] < seq["fvg1_bottom"]) if seq["side"] == 1 else (row["close"] > seq["fvg1_top"])
            if consumed:
                self._bump("invalidated", "fvg1_consumed")
                state["seq"] = None
                return []
        if seq["stage"] == "fvg2_set":
            consumed = (row["close"] < seq["fvg2_bottom"]) if seq["side"] == 1 else (row["close"] > seq["fvg2_top"])
            if consumed:
                self._bump("invalidated", "fvg2_consumed")
                state["seq"] = None
                return []

        if seq["stage"] == "swept_htf":
            if i - seq["anchor_bar"] > self.ltf_sweep_within_bars:
                self._bump("expired", "ltf_sweep")
                state["seq"] = None
                return []
            swept = (
                pd.notna(row["ltf_swing_low"]) and row["low"] < row["ltf_swing_low"] and row["close"] > row["ltf_swing_low"]
                if seq["side"] == 1 else
                pd.notna(row["ltf_swing_high"]) and row["high"] > row["ltf_swing_high"] and row["close"] < row["ltf_swing_high"]
            )
            if swept:
                seq["stage"], seq["ltf_sweep_bar"] = "ltf_swept", i
                self.funnel_stats["ltf_sweep"] += 1
            return []

        if seq["stage"] == "ltf_swept":
            if i - seq["ltf_sweep_bar"] > self.fvg1_within_bars:
                self._bump("expired", "fvg1")
                state["seq"] = None
                return []
            disp_col, fvg_col = ("disp_bull", "bull_fvg") if seq["side"] == 1 else ("disp_bear", "bear_fvg")
            # fair_value_gaps() flags the gap on its THIRD candle (i), with
            # the displacement typically happening on the MIDDLE one (i-1)
            # — checking disp only on the same bar as the FVG (i) misses
            # ~3x as many genuine cases as checking i-1 too (verified
            # against real data before writing this). Spec §2.4 allows
            # either: "candle k-1 must be the displacement candle, or
            # within fvg1_within_bars of it".
            prior_disp = bool(df[disp_col].iloc[i - 1]) if i > 0 else False
            if row[disp_col] or prior_disp:
                self.funnel_stats["displacement"] += 1
                size_col, top_col, bottom_col = f"{fvg_col}_size", f"{fvg_col}_top", f"{fvg_col}_bottom"
                if row[fvg_col] and row[size_col] >= self.fvg_min_atr_mult * a:
                    seq["stage"] = "fvg1_set"
                    seq["fvg1_top"], seq["fvg1_bottom"], seq["fvg1_bar"] = row[top_col], row[bottom_col], i
                    self.funnel_stats["fvg1"] += 1
            return []

        if seq["stage"] == "fvg1_set":
            if i - seq["fvg1_bar"] > self.fvg1_retest_within_bars:
                self._bump("expired", "fvg1_retest")
                state["seq"] = None
                return []
            touched = (row["low"] <= seq["fvg1_top"]) if seq["side"] == 1 else (row["high"] >= seq["fvg1_bottom"])
            if touched:
                self.funnel_stats["fvg1_retest"] += 1
                if self.variant == "A":
                    order = self._build_entry(ts, seq, seq["fvg1_top"], seq["fvg1_bottom"], a)
                    state["seq"] = None
                    if order is None:
                        return []
                    self.funnel_stats["entry"] += 1
                    return [order]
                seq["stage"], seq["fvg1_retest_bar"] = "fvg1_held", i
            return []

        if seq["stage"] == "fvg1_held":
            if i - seq["fvg1_retest_bar"] > self.fvg2_within_bars:
                self._bump("expired", "fvg2")
                state["seq"] = None
                return []
            disp_col, fvg_col = ("disp_bull", "bull_fvg") if seq["side"] == 1 else ("disp_bear", "bear_fvg")
            prior_disp = bool(df[disp_col].iloc[i - 1]) if i > 0 else False
            if row[disp_col] or prior_disp:
                size_col, top_col, bottom_col = f"{fvg_col}_size", f"{fvg_col}_top", f"{fvg_col}_bottom"
                if row[fvg_col] and row[size_col] >= self.fvg_min_atr_mult * a:
                    seq["stage"] = "fvg2_set"
                    seq["fvg2_top"], seq["fvg2_bottom"], seq["fvg2_bar"] = row[top_col], row[bottom_col], i
                    self.funnel_stats["fvg2"] += 1
            return []

        if seq["stage"] == "fvg2_set":
            if i - seq["fvg2_bar"] > self.entry_within_bars:
                self._bump("expired", "entry")
                state["seq"] = None
                return []
            touched = (row["low"] <= seq["fvg2_top"]) if seq["side"] == 1 else (row["high"] >= seq["fvg2_bottom"])
            if touched:
                order = self._build_entry(ts, seq, seq["fvg2_top"], seq["fvg2_bottom"], a)
                state["seq"] = None
                if order is None:
                    return []
                self.funnel_stats["entry"] += 1
                return [order]
            return []

        return []


@dataclass
class OrderBlockSweep(Strategy):
    """XAUUSD Order Block Sweep, v1 — companion to SecondFVGRetest (the FVG
    v2 spec). Same HTF-sweep-then-LTF-reversal skeleton and the exact same
    confirmed-fractal swing pivots / displacement definition, but the
    reference zone traded is an order block (the last opposing-colour
    candle before the displacement leg, valid only if that leg then breaks
    structure) instead of a fair value gap.

    `ob_entry_mode` picks how the zone is traded:
      - "retest": enter on the very first touch of the block.
      - "sweep" (default, this class's namesake): wait for price to wick
        through the block's far extreme and close back inside, then enter
        against the participants that wick just trapped — stop tucked just
        beyond the wick itself rather than beyond the whole block, which is
        the whole point of preferring this mode (tighter risk on the same
        target).
      - "overlap": enter only where the OB and a same-leg FVG intersect —
        tightest zone, expect it to fire the least often; forced on
        whenever `require_fvg_overlap` is set.

    `ob_with_fvg_filter` (off by default, matching the spec's own testing
    order — run each component alone before combining) additionally
    requires a qualifying FVG somewhere in the same leg without changing
    the entry price itself. The spec is explicit that in a clean
    displacement leg the OB and the FVG are adjacent by construction (the
    OB is candle k-2, the FVG is the k-2/k gap) — requiring both mostly
    restates one condition twice rather than adding independent evidence,
    so this is a filter to measure, not a default to assume.

    Order-block mitigation (spec §2.4) is tracked explicitly via
    seq["entered"]: a block price has already dipped into and then left
    again, without ever producing the qualifying entry pattern, is spent —
    cancelled rather than left sitting there to be traded stale three legs
    later (the spec calls this out as the most common OB implementation
    bug).
    """

    name: str = "order_block_sweep"
    timeframe: str = "15min"
    htf_timeframe: str = "4h"
    htf_swing_lookback: int = 5
    ltf_swing_lookback: int = 5
    ltf_sweep_within_bars: int = 48
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.5
    bos_within_bars: int = 10
    ob_lookback_bars: int = 5
    ob_zone_type: str = "range"          # "range" | "body" — use "range" on gold (spec §2.1)
    ob_require_bos: bool = True          # spec: do not disable
    ob_retest_within_bars: int = 25
    ob_sweep_buffer_atr: float = 0.15
    ob_max_age_bars: int = 60
    ob_entry_mode: str = "sweep"         # "retest" | "sweep" | "overlap"
    ob_with_fvg_filter: bool = False
    require_fvg_overlap: bool = False    # forces ob_entry_mode == "overlap"
    fvg_min_atr_mult: float = 0.2
    stop_buffer_atr: float = 0.25
    rr: float = 2.0
    retracement_pct: float = 0.5         # unused by OB entry math; kept for a uniform param surface with SecondFVGRetest
    trail_atr_mult: float | None = 1.5

    TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30,
                  "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
    DEFAULT_HTF = {"1min": "15min", "5min": "1h", "15min": "4h",
                   "30min": "4h", "1h": "1d", "4h": "1w", "1d": "1w"}

    @classmethod
    def build_config(cls, ltf: str, htf: str | None = None, entry_mode: str = "sweep",
                      fvg_filter: bool = False) -> dict:
        """A scaled param set for a given entry/HTF pair (spec §7.1) — every
        ATR-based and bar-count param stays fixed across timeframes; only
        ltf_sweep_within_bars is derived from the HTF:LTF ratio."""
        htf = htf or cls.DEFAULT_HTF[ltf]
        ratio = cls.TF_MINUTES[htf] / cls.TF_MINUTES[ltf]
        return {
            "timeframe": ltf, "htf_timeframe": htf,
            "htf_swing_lookback": 5, "ltf_swing_lookback": 5,
            "ltf_sweep_within_bars": int(3 * ratio),
            "displacement_atr_mult": 1.2, "displacement_body_ratio": 0.5,
            "bos_within_bars": 10, "ob_lookback_bars": 5,
            "ob_zone_type": "range", "ob_require_bos": True,
            "ob_retest_within_bars": 25, "ob_sweep_buffer_atr": 0.15,
            "ob_max_age_bars": 60, "ob_entry_mode": entry_mode,
            "ob_with_fvg_filter": fvg_filter, "fvg_min_atr_mult": 0.2,
            "stop_buffer_atr": 0.25, "rr": 2.0, "trail_atr_mult": 1.5,
        }

    def __post_init__(self):
        if self.require_fvg_overlap:
            self.ob_entry_mode = "overlap"
        # diagnostics only (spec §8) — never read by generate() itself
        self.funnel_stats = {
            "htf_pivots": 0, "htf_sweep": 0, "ltf_sweep": 0, "displacement": 0,
            "bos": 0, "ob_found": 0, "ob_retest": 0, "ob_sweep": 0, "entry": 0,
            "cancelled": {},
        }

    def _bump(self, key: str) -> None:
        self.funnel_stats["cancelled"][key] = self.funnel_stats["cancelled"].get(key, 0) + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, 14)

        four_h = df.resample(self.htf_timeframe).agg({"high": "max", "low": "min"}).dropna()
        n = self.htf_swing_lookback
        htf_pivot_high = four_h["high"] == four_h["high"].rolling(2 * n + 1, center=True).max()
        htf_pivot_low = four_h["low"] == four_h["low"].rolling(2 * n + 1, center=True).min()
        self.funnel_stats["htf_pivots"] = int(htf_pivot_high.sum() + htf_pivot_low.sum())
        htf_swing_high = four_h["high"].where(htf_pivot_high).shift(n).ffill()
        htf_swing_low = four_h["low"].where(htf_pivot_low).shift(n).ffill()
        df["htf_swing_high"] = htf_swing_high.reindex(df.index, method="ffill")
        df["htf_swing_low"] = htf_swing_low.reindex(df.index, method="ffill")

        df["ltf_swing_high"] = confirmed_swing_high(df, self.ltf_swing_lookback)
        df["ltf_swing_low"] = confirmed_swing_low(df, self.ltf_swing_lookback)

        disp = displacement(df, df["atr"], self.displacement_atr_mult, self.displacement_body_ratio)
        for col in disp.columns:
            df[col] = disp[col]

        if self.ob_with_fvg_filter or self.ob_entry_mode == "overlap":
            fvg = fair_value_gaps(df)
            for col in fvg.columns:
                df[col] = fvg[col]

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = max(bar_len, 1)
        return df

    def _htf_swept(self, row) -> int | None:
        if pd.notna(row["htf_swing_low"]) and row["low"] < row["htf_swing_low"] and row["close"] > row["htf_swing_low"]:
            return 1
        if pd.notna(row["htf_swing_high"]) and row["high"] > row["htf_swing_high"] and row["close"] < row["htf_swing_high"]:
            return -1
        return None

    def _find_ob(self, df: pd.DataFrame, disp_bar: int, side: int) -> dict | None:
        """Backward search from just before the displacement candle for the
        last opposing-colour candle (spec §2.1): bullish OB = last
        down-close, bearish = last up-close. None (discard the setup,
        don't substitute the nearest candle) if none found within
        ob_lookback_bars."""
        lo = max(0, disp_bar - self.ob_lookback_bars)
        for k in range(disp_bar - 1, lo - 1, -1):
            o, c = df["open"].iloc[k], df["close"].iloc[k]
            is_opposing = (c < o) if side == 1 else (c > o)
            if is_opposing:
                if self.ob_zone_type == "body":
                    return {"bar": k, "high": max(o, c), "low": min(o, c)}
                return {"bar": k, "high": df["high"].iloc[k], "low": df["low"].iloc[k]}
        return None

    def _leg_has_fvg(self, df: pd.DataFrame, ob_bar: int, bos_bar: int, side: int) -> dict | None:
        """A qualifying FVG anywhere between the order block and the BOS
        bar, inclusive — used only when ob_with_fvg_filter/overlap is on."""
        fvg_col = "bull_fvg" if side == 1 else "bear_fvg"
        size_col, top_col, bottom_col = f"{fvg_col}_size", f"{fvg_col}_top", f"{fvg_col}_bottom"
        a = df["atr"].iloc[bos_bar]
        for k in range(ob_bar, bos_bar + 1):
            if bool(df[fvg_col].iloc[k]) and df[size_col].iloc[k] >= self.fvg_min_atr_mult * a:
                return {"top": df[top_col].iloc[k], "bottom": df[bottom_col].iloc[k]}
        return None

    def _build_entry(self, ts, seq, entry_level: float, stop_ref: float, a: float, kind: str) -> "Order | None":
        direction = seq["side"]
        stop_buf = self.stop_buffer_atr * a
        expiry = ts + pd.Timedelta(minutes=self.ob_retest_within_bars * self._bar_len)

        if direction == 1:
            stop = stop_ref - stop_buf
            risk = entry_level - stop
            if risk <= 0:
                return None
            target = entry_level + self.rr * risk
        else:
            stop = stop_ref + stop_buf
            risk = stop - entry_level
            if risk <= 0:
                return None
            target = entry_level - self.rr * risk

        return Order(
            created_at=ts, direction=direction, kind=kind,
            trigger=entry_level if kind == "limit" else None,
            stop_loss=stop, tp1=target, expiry=expiry,
            trail_atr_mult=self.trail_atr_mult, tag=self.name,
        )

    def generate(self, df, i, state):
        ts = df.index[i]
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []

        seq = state.get("seq")

        # Same HTF-sweep handling as SecondFVGRetest, including the same
        # refinement (only escalate a same-direction re-sweep if it's
        # actually deeper) — see that class's docstring/comments for why a
        # hard reset on every same-direction re-sweep starves the sequence
        # before it ever reaches an entry.
        fresh_sweep = self._htf_swept(row)
        if fresh_sweep is not None:
            if seq is None or fresh_sweep != seq["side"]:
                if seq is not None:
                    self._bump("opposing_htf_sweep")
                seq = {
                    "side": fresh_sweep, "stage": "swept_htf",
                    "sweep_extreme": row["low"] if fresh_sweep == 1 else row["high"],
                    "anchor_bar": i,
                }
                state["seq"] = seq
                self.funnel_stats["htf_sweep"] += 1
                return []
            if seq["stage"] == "swept_htf":
                new_extreme = row["low"] if fresh_sweep == 1 else row["high"]
                deeper = (new_extreme < seq["sweep_extreme"]) if fresh_sweep == 1 else (new_extreme > seq["sweep_extreme"])
                if deeper:
                    seq["sweep_extreme"] = new_extreme

        if seq is None:
            return []

        side = seq["side"]

        if seq["stage"] == "swept_htf":
            if i - seq["anchor_bar"] > self.ltf_sweep_within_bars:
                self._bump("ltf_sweep_expired")
                state["seq"] = None
                return []
            swept = (
                pd.notna(row["ltf_swing_low"]) and row["low"] < row["ltf_swing_low"] and row["close"] > row["ltf_swing_low"]
                if side == 1 else
                pd.notna(row["ltf_swing_high"]) and row["high"] > row["ltf_swing_high"] and row["close"] < row["ltf_swing_high"]
            )
            if swept:
                seq["stage"], seq["ltf_sweep_bar"] = "ltf_swept", i
                self.funnel_stats["ltf_sweep"] += 1
            return []

        # The spec names bos_within_bars only for "structure break must
        # follow promptly" — i.e. promptly AFTER displacement, not after
        # the LTF sweep — and gives no separate window for "displacement
        # must follow the LTF sweep" at all. Measured directly against the
        # real data: displacement alone takes a median of ~6 bars after the
        # LTF sweep, so budgeting that wait out of the same 10-bar
        # bos_within_bars clock left almost nothing for BOS afterward and
        # was the dominant false bottleneck in an earlier version of this
        # method. ltf_sweep_within_bars (already the "how long to wait for
        # the LTF leg to develop" parameter) is reused for the sweep ->
        # displacement wait; bos_within_bars gets its own fresh clock
        # starting at the displacement bar for displacement -> BOS.
        if seq["stage"] == "ltf_swept":
            if i - seq["ltf_sweep_bar"] > self.ltf_sweep_within_bars:
                self._bump("displacement_expired")
                state["seq"] = None
                return []
            disp_col = "disp_bull" if side == 1 else "disp_bear"
            if row[disp_col]:
                seq["disp_bar"] = i
                self.funnel_stats["displacement"] += 1
                seq["stage"] = "displaced"
            return []

        if seq["stage"] == "displaced":
            if i - seq["disp_bar"] > self.bos_within_bars:
                self._bump("bos_expired")
                state["seq"] = None
                return []

            # waiting for BOS against the LTF swing pivot on the OPPOSITE
            # side from the one just swept (spec §2.2) — reuses the same
            # confirmed pivot columns as the sweep check itself, just the
            # other side of them.
            broke = (
                pd.notna(row["ltf_swing_high"]) and row["close"] > row["ltf_swing_high"]
                if side == 1 else
                pd.notna(row["ltf_swing_low"]) and row["close"] < row["ltf_swing_low"]
            )
            if not broke:
                return []
            self.funnel_stats["bos"] += 1

            ob = self._find_ob(df, seq["disp_bar"], side)
            if ob is None:
                self._bump("no_ob_found")
                state["seq"] = None
                return []

            fvg_zone = None
            if self.ob_with_fvg_filter or self.ob_entry_mode == "overlap":
                fvg_zone = self._leg_has_fvg(df, ob["bar"], i, side)
                if fvg_zone is None:
                    self._bump("no_fvg_in_leg")
                    state["seq"] = None
                    return []

            seq["stage"] = "ob_set"
            seq["ob_bar"], seq["ob_high"], seq["ob_low"] = ob["bar"], ob["high"], ob["low"]
            seq["ob_set_bar"] = i
            seq["entered"] = False
            if fvg_zone is not None:
                top, bottom = min(ob["high"], fvg_zone["top"]), max(ob["low"], fvg_zone["bottom"])
                if top <= bottom:
                    self._bump("no_overlap")
                    state["seq"] = None
                    return []
                seq["overlap_top"], seq["overlap_bottom"] = top, bottom
            self.funnel_stats["ob_found"] += 1
            return []

        if seq["stage"] == "ob_set":
            ob_high, ob_low = seq["ob_high"], seq["ob_low"]

            # Rule 2 (§5): a close through the far side of the block means
            # it failed outright — the reversal premise this OB stood for
            # is broken.
            failed = (row["close"] < ob_low) if side == 1 else (row["close"] > ob_high)
            if failed:
                self._bump("ob_failed")
                state["seq"] = None
                return []
            if i - seq["ob_bar"] > self.ob_max_age_bars:
                self._bump("ob_max_age")
                state["seq"] = None
                return []
            if i - seq["ob_set_bar"] > self.ob_retest_within_bars:
                self._bump("ob_retest_expired")
                state["seq"] = None
                return []

            touched = (row["low"] <= ob_high) if side == 1 else (row["high"] >= ob_low)

            if self.ob_entry_mode == "retest":
                if touched:
                    self.funnel_stats["ob_retest"] += 1
                    entry_level = ob_high if side == 1 else ob_low
                    stop_ref = ob_low if side == 1 else ob_high
                    order = self._build_entry(ts, seq, entry_level, stop_ref, a, kind="limit")
                    state["seq"] = None
                    if order is None:
                        return []
                    self.funnel_stats["entry"] += 1
                    return [order]
                return []

            if self.ob_entry_mode == "overlap":
                top, bottom = seq["overlap_top"], seq["overlap_bottom"]
                overlap_touched = (row["low"] <= top) if side == 1 else (row["high"] >= bottom)
                if overlap_touched:
                    self.funnel_stats["ob_retest"] += 1
                    entry_level = (top + bottom) / 2.0
                    stop_ref = ob_low if side == 1 else ob_high
                    order = self._build_entry(ts, seq, entry_level, stop_ref, a, kind="limit")
                    state["seq"] = None
                    if order is None:
                        return []
                    self.funnel_stats["entry"] += 1
                    return [order]
                return []

            # ob_entry_mode == "sweep" (default): wait for a wick through
            # the block's far extreme, within the depth tolerance, closing
            # back inside (spec §2.5) — a plain touch alone isn't enough,
            # but it does count as "entered the zone" for mitigation (§2.4).
            if touched:
                if not seq["entered"]:
                    seq["entered"] = True
                    self.funnel_stats["ob_retest"] += 1
                buffer = self.ob_sweep_buffer_atr * a
                if side == 1:
                    too_deep = row["low"] < ob_low - buffer
                    swept = (not too_deep) and row["low"] < ob_low and row["close"] > ob_low
                else:
                    too_deep = row["high"] > ob_high + buffer
                    swept = (not too_deep) and row["high"] > ob_high and row["close"] < ob_high
                if too_deep:
                    self._bump("sweep_too_deep")
                    state["seq"] = None
                    return []
                if swept:
                    self.funnel_stats["ob_sweep"] += 1
                    entry_level = row["close"]
                    stop_ref = row["low"] if side == 1 else row["high"]
                    order = self._build_entry(ts, seq, entry_level, stop_ref, a, kind="market")
                    state["seq"] = None
                    if order is None:
                        return []
                    self.funnel_stats["entry"] += 1
                    return [order]
            elif seq["entered"]:
                # price came into the zone and has now left it again without
                # ever producing the sweep pattern — mitigated, spent (§2.4).
                self._bump("mitigated_no_entry")
                state["seq"] = None
            return []

        return []


# --------------------------------------------------------------------- #
def _session_hilo(df: pd.DataFrame, day: pd.Series, start_min: int, end_min: int):
    """This calendar day's high/low over [start_min, end_min) minutes-of-day,
    broadcast onto every bar of that same day (like LiquiditySweepReversal's
    asian_high/low) — safe to read from any LATER session the same day
    without lookahead, since the window that produced it has already closed
    by the time a later session starts checking it."""
    mins = df.index.hour * 60 + df.index.minute
    in_win = (mins >= start_min) & (mins < end_min)
    agg = df[in_win].groupby(day[in_win]).agg({"high": "max", "low": "min"})
    return pd.Series(day, index=df.index).map(agg["high"]), pd.Series(day, index=df.index).map(agg["low"])


@dataclass
class GoldConfluenceSweep(Strategy):
    """"Gold Confluence Sweep" — user-supplied spec (v1.0, 2026-09-23)
    combining four of this file's other strategies into layered filters:
      1. 4H EMA(fast)/EMA(slow) trend bias (same rule as htf_ema_bias) —
         only long in an uptrend, only short in a downtrend.
      2-3. A session-specific liquidity level is swept AGAINST the bias
         (a low sweep in an uptrend, a high sweep in a downtrend) and
         reclaimed within `sweep_reclaim_bars` candles — Asian session
         watches the prior day's high/low, London watches the Asian
         range, New York watches the London range or its own 13:30-13:45
         opening range once that's closed.
      4. A market-structure shift (close beyond the prior swing), then a
         same-direction FVG from the displacement candle. The entry is
         the FVG midpoint, taken only if it falls in the 50-78.6%
         retracement zone (0% at the MSS close, 100% at the sweep
         extreme — this file's existing sweep strategies don't need this
         extra fib check since they already gate on RR against a target;
         this one uses it purely as the spec's "entry quality filter").

    Stop = whichever is WIDER of (beyond the sweep wick) or (sl_atr_mult
    x ATR); rejected outright if that distance exceeds max_sl_atr_mult x
    ATR or max_sl_usd (the spec's "too wide for this account" skip).
    tp1/tp2 are 1.5R/3R with a 2xATR trail — the Backtester's tp1/tp2
    partial-close-then-breakeven-then-trail applies exactly as specified;
    live/demo auto-trade collapses to a single take-profit (tp1) same as
    every other strategy here that sets both (see webapp.autotrade's
    "take_profit = order.tp1 if ... else order.tp2"), so it exits fully
    at 1.5R rather than scaling out live.

    NOT implemented (spec sections not expressible as a bar-by-bar entry
    signal, or needing data this app doesn't have): the high-impact-news
    blackout, a live spread filter, US DST auto-shifting the NY session
    window, and the account-level daily/weekly loss caps and per-trade
    dollar risk (this app's risk circuit breakers and position sizing are
    shared settings across every strategy/wallet, not per-strategy — set
    those in Settings/Risk if you want this wallet to match section 8
    exactly).
    """

    name: str = "gold_confluence_sweep"
    timeframe: str = "15min"
    htf_timeframe: str = "4h"
    ema_fast: int = 50
    ema_slow: int = 200
    asian_start: str = "00:00"
    asian_end: str = "06:00"
    london_start: str = "07:00"
    london_end: str = "11:00"
    ny_start: str = "13:30"
    ny_end: str = "17:00"
    ny_or_minutes: int = 15
    sweep_reclaim_bars: int = 3
    swing_lookback: int = 10
    mss_within_bars: int = 15
    fvg_within_bars: int = 10
    displacement_atr_mult: float = 0.8
    displacement_body_ratio: float = 0.6
    fvg_min_atr_mult: float = 0.05
    fib_lo: float = 0.50
    fib_hi: float = 0.786
    entry_expiry_bars: int = 4
    atr_period: int = 14
    sl_atr_mult: float = 1.5
    max_sl_atr_mult: float = 2.5
    max_sl_usd: float = 20.0
    tp1_rr: float = 1.5
    tp2_rr: float = 3.0
    trail_atr_mult: float | None = 2.0
    time_exit_utc: str = "20:00"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["atr"] = atr(df, self.atr_period)
        df["bias"] = htf_ema_bias(df, self.htf_timeframe, self.ema_fast, self.ema_slow)

        daily = df.resample("1D").agg({"high": "max", "low": "min"}).dropna()
        prev = daily.shift(1)
        prev.index = prev.index.normalize()
        day = df.index.normalize()
        df["pdh"] = pd.Series(day, index=df.index).map(prev["high"])
        df["pdl"] = pd.Series(day, index=df.index).map(prev["low"])

        df["asian_high"], df["asian_low"] = _session_hilo(df, day, _hm(self.asian_start), _hm(self.asian_end))
        df["london_high"], df["london_low"] = _session_hilo(df, day, _hm(self.london_start), _hm(self.london_end))
        ny_start_m = _hm(self.ny_start)
        df["ny_or_high"], df["ny_or_low"] = _session_hilo(df, day, ny_start_m, ny_start_m + self.ny_or_minutes)

        df["swing_high"] = prior_swing_high(df, self.swing_lookback)
        df["swing_low"] = prior_swing_low(df, self.swing_lookback)

        fvg = fair_value_gaps(df)
        for col in fvg.columns:
            df[col] = fvg[col]
        disp = displacement(df, df["atr"], self.displacement_atr_mult, self.displacement_body_ratio)
        for col in disp.columns:
            df[col] = disp[col]

        bar_len = int((df.index[1] - df.index[0]).total_seconds() // 60)
        self._bar_len = max(bar_len, 1)
        return df

    def _session_level(self, row, m: int):
        """(high-side level, low-side level) for whichever session `m`
        (minutes since midnight UTC) falls in, or (None, None) outside all
        three — matching this file's `_minutes`/`_hm` convention."""
        if _hm(self.asian_start) <= m < _hm(self.asian_end):
            return row["pdh"], row["pdl"]
        if _hm(self.london_start) <= m < _hm(self.london_end):
            return row["asian_high"], row["asian_low"]
        ny_start_m, ny_end_m = _hm(self.ny_start), _hm(self.ny_end)
        if ny_start_m <= m < ny_end_m:
            or_end_m = ny_start_m + self.ny_or_minutes
            if m >= or_end_m and np.isfinite(row["ny_or_high"]):
                return row["ny_or_high"], row["ny_or_low"]
            return row["london_high"], row["london_low"]
        return None, None

    def generate(self, df, i, state):
        ts = df.index[i]
        m = _minutes(ts)
        row = df.iloc[i]
        a = row["atr"]
        if not np.isfinite(a) or a <= 0:
            return []
        bias = row["bias"]
        level_hi, level_lo = self._session_level(row, m)

        seq = state.get("seq")
        if seq is not None and i - seq["anchor_bar"] > self.sweep_reclaim_bars + self.mss_within_bars + self.fvg_within_bars:
            seq = None
        state["seq"] = seq

        if seq is None:
            if bias == 1 and level_lo is not None and np.isfinite(level_lo) and row["low"] < level_lo:
                seq = {"side": 1, "level": level_lo, "extreme": row["low"], "anchor_bar": i, "stage": "sweeping"}
            elif bias == -1 and level_hi is not None and np.isfinite(level_hi) and row["high"] > level_hi:
                seq = {"side": -1, "level": level_hi, "extreme": row["high"], "anchor_bar": i, "stage": "sweeping"}
            else:
                return []
            state["seq"] = seq

        if seq["stage"] == "sweeping":
            # >3 candles closed beyond the level without reclaiming = a
            # breakout, not a sweep — invalidate rather than keep waiting.
            if i - seq["anchor_bar"] > self.sweep_reclaim_bars:
                state["seq"] = None
                return []
            if seq["side"] == 1:
                seq["extreme"] = min(seq["extreme"], row["low"])
                reclaimed = row["close"] > seq["level"]
            else:
                seq["extreme"] = max(seq["extreme"], row["high"])
                reclaimed = row["close"] < seq["level"]
            if not reclaimed:
                return []
            seq["stage"], seq["reclaim_bar"] = "mss_wait", i
            # fall through — the reclaim candle can be the MSS candle too

        if seq["stage"] == "mss_wait":
            if i - seq["reclaim_bar"] > self.mss_within_bars:
                state["seq"] = None
                return []
            mss = (
                (seq["side"] == 1 and np.isfinite(row["swing_high"]) and row["close"] > row["swing_high"])
                or (seq["side"] == -1 and np.isfinite(row["swing_low"]) and row["close"] < row["swing_low"])
            )
            if not mss:
                return []
            seq["stage"], seq["mss_bar"], seq["mss_price"] = "fvg_wait", i, row["close"]
            # fall through — the displacement candle that caused the MSS can carry its own FVG too

        # stage == "fvg_wait"
        if i - seq["mss_bar"] > self.fvg_within_bars:
            state["seq"] = None
            return []

        top = bottom = None
        if seq["side"] == 1 and row["disp_bull"] and row["bull_fvg"] and row["bull_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bull_fvg_top"], row["bull_fvg_bottom"]
        elif seq["side"] == -1 and row["disp_bear"] and row["bear_fvg"] and row["bear_fvg_size"] >= self.fvg_min_atr_mult * a:
            top, bottom = row["bear_fvg_top"], row["bear_fvg_bottom"]
        if top is None:
            return []

        direction = seq["side"]
        extreme = seq["extreme"]
        mss_price = seq["mss_price"]
        entry_level = (top + bottom) / 2.0
        state["seq"] = None  # the sequence is consumed here whether or not a valid order results

        span = mss_price - extreme
        if span == 0:
            return []
        zone_a = mss_price - self.fib_lo * span
        zone_b = mss_price - self.fib_hi * span
        zone_lo, zone_hi = min(zone_a, zone_b), max(zone_a, zone_b)
        if not (zone_lo <= entry_level <= zone_hi):
            return []  # entry quality filter: outside the 50-78.6% retracement zone

        dist_wick = abs(entry_level - extreme)
        dist_atr = self.sl_atr_mult * a
        stop_distance = max(dist_wick, dist_atr)
        if stop_distance <= 0 or stop_distance > self.max_sl_atr_mult * a or stop_distance > self.max_sl_usd:
            return []

        stop = entry_level - direction * stop_distance
        tp1 = entry_level + direction * self.tp1_rr * stop_distance
        tp2 = entry_level + direction * self.tp2_rr * stop_distance
        expiry = ts + pd.Timedelta(minutes=self.entry_expiry_bars * self._bar_len)
        day = ts.normalize()
        time_exit = day + pd.Timedelta(minutes=_hm(self.time_exit_utc))
        if time_exit <= ts:
            time_exit += pd.Timedelta(days=1)

        return [
            Order(
                created_at=ts, direction=direction, kind="limit", trigger=entry_level,
                stop_loss=stop, tp1=tp1, tp2=tp2, expiry=expiry, time_exit=time_exit,
                trail_atr_mult=self.trail_atr_mult, tag=self.name,
            )
        ]


STRATEGY_REGISTRY = {
    "asian_sweep": AsianSweepReversal,
    "ny_orb": NYOpeningRange,
    "trend_pullback": TrendPullback,
    "liquidity_sweep_reversal": LiquiditySweepReversal,
    "liquidity_imbalance_continuation": LiquidityImbalanceContinuation,
    "fib_retracement": FibonacciRetracement,
    "sweep_mss_fvg_4h": LiquiditySweepMSSFVGRetest,
    "second_fvg_retest": SecondFVGRetest,
    "order_block_sweep": OrderBlockSweep,
    "gold_confluence_sweep": GoldConfluenceSweep,
    "custom_rule": RuleStrategy,
}

# strategy_class keys whose config isn't a flat set of dataclass fields (a
# JSON spec instead) — excluded from the hand-coded-strategy param-schema
# introspection in webapp/routers/strategies.py; the Strategy Builder UI
# talks to /api/strategies/indicators instead.
BUILDER_DRIVEN_KEYS = {"custom_rule"}
