"""Hand-rolled indicators (no TA-Lib / pandas-ta dependency).

All functions take/return pandas Series or DataFrames indexed by a tz-aware
DatetimeIndex in UTC.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed ATR."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder's ADX. Returns DataFrame with columns plus_di, minus_di, adx."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    alpha = 1.0 / period
    tr_s = true_range(df).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_s = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    minus_s = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_s / tr_s
        minus_di = 100.0 * minus_s / tr_s
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    adx_line = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    return pd.DataFrame(
        {"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line},
        index=df.index,
    )


def rolling_swing_low(df: pd.DataFrame, lookback: int) -> pd.Series:
    """Lowest low of the last `lookback` bars, inclusive of the current bar."""
    return df["low"].rolling(lookback, min_periods=1).min()


def rolling_swing_high(df: pd.DataFrame, lookback: int) -> pd.Series:
    return df["high"].rolling(lookback, min_periods=1).max()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out[avg_loss == 0] = 100.0
    out[(avg_gain == 0) & (avg_loss == 0)] = 50.0
    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Returns DataFrame with columns macd, signal, hist."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line},
        index=series.index,
    )


def bollinger_bands(series: pd.Series, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    """Returns DataFrame with columns mid, upper, lower, bandwidth (upper-lower)/mid."""
    mid = sma(series, period)
    sd = series.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + mult * sd
    lower = mid - mult * sd
    with np.errstate(divide="ignore", invalid="ignore"):
        bandwidth = (upper - lower) / mid
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "bandwidth": bandwidth}, index=series.index)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session (daily) VWAP — resets every calendar day, no lookahead beyond
    the current bar within that day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan)
    day = df.index.normalize()
    pv = (typical * vol).groupby(day).cumsum()
    cv = vol.groupby(day).cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = pv / cv
    return out.fillna(typical)


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """Returns DataFrame with columns k, d (both 0-100)."""
    lowest = df["low"].rolling(k_period, min_periods=k_period).min()
    highest = df["high"].rolling(k_period, min_periods=k_period).max()
    with np.errstate(divide="ignore", invalid="ignore"):
        k = 100.0 * (df["close"] - lowest) / (highest - lowest)
    d = k.rolling(d_period, min_periods=d_period).mean()
    return pd.DataFrame({"k": k, "d": d}, index=df.index)


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Returns DataFrame with columns line, direction (1 uptrend / -1 downtrend)."""
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper_basic = hl2 + mult * a
    lower_basic = hl2 - mult * a

    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)
    line = np.full(n, np.nan)

    close = df["close"].to_numpy(float)
    ub = upper_basic.to_numpy(float)
    lb = lower_basic.to_numpy(float)

    for i in range(n):
        if i == 0 or np.isnan(ub[i - 1]):
            upper[i], lower[i] = ub[i], lb[i]
            direction[i] = 1
            line[i] = lower[i] if not np.isnan(lower[i]) else np.nan
            continue
        upper[i] = ub[i] if (ub[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = lb[i] if (lb[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]

        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < lower[i] else 1
        else:
            direction[i] = 1 if close[i] > upper[i] else -1
        line[i] = lower[i] if direction[i] == 1 else upper[i]

    return pd.DataFrame({"line": line, "direction": direction}, index=df.index)


# --------------------------------------------------------------------- #
# candlestick patterns — boolean Series, hand-rolled ratio heuristics
# --------------------------------------------------------------------- #
def _body(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def _range(df: pd.DataFrame) -> pd.Series:
    return (df["high"] - df["low"]).replace(0, np.nan)


def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_bear = df["close"].shift(1) < df["open"].shift(1)
    cur_bull = df["close"] > df["open"]
    engulfs = (df["close"] >= df["open"].shift(1)) & (df["open"] <= df["close"].shift(1))
    return (prev_bear & cur_bull & engulfs).fillna(False)


def bearish_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_bull = df["close"].shift(1) > df["open"].shift(1)
    cur_bear = df["close"] < df["open"]
    engulfs = (df["open"] >= df["close"].shift(1)) & (df["close"] <= df["open"].shift(1))
    return (prev_bull & cur_bear & engulfs).fillna(False)


def hammer(df: pd.DataFrame) -> pd.Series:
    body = _body(df)
    rng = _range(df)
    body_floor = pd.Series(np.where(body == 0, rng * 0.1, body), index=df.index)
    lower_wick = pd.concat([df["open"], df["close"]], axis=1).min(axis=1) - df["low"]
    upper_wick = df["high"] - pd.concat([df["open"], df["close"]], axis=1).max(axis=1)
    return ((lower_wick >= 2 * body) & (upper_wick <= 0.3 * body_floor) & (body / rng < 0.4)).fillna(False)


def shooting_star(df: pd.DataFrame) -> pd.Series:
    body = _body(df)
    rng = _range(df)
    body_floor = pd.Series(np.where(body == 0, rng * 0.1, body), index=df.index)
    upper_wick = df["high"] - pd.concat([df["open"], df["close"]], axis=1).max(axis=1)
    lower_wick = pd.concat([df["open"], df["close"]], axis=1).min(axis=1) - df["low"]
    return ((upper_wick >= 2 * body) & (lower_wick <= 0.3 * body_floor) & (body / rng < 0.4)).fillna(False)


def doji(df: pd.DataFrame, body_ratio: float = 0.1) -> pd.Series:
    return (_body(df) / _range(df) <= body_ratio).fillna(False)


def daily_range_average(df: pd.DataFrame, days: int = 20) -> pd.Series:
    """Average daily range over the last `days` COMPLETED days.

    Returned as a series aligned to the intraday index (forward-filled), so at
    any bar you get the ADR computed only from days strictly before today.
    No lookahead.
    """
    daily = df.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    dr = daily["high"] - daily["low"]
    adr = dr.rolling(days, min_periods=max(3, days // 2)).mean().shift(1)
    adr.index = adr.index.normalize()

    out = pd.Series(index=df.index, dtype=float)
    day_key = df.index.normalize()
    mapped = adr.reindex(day_key)
    out[:] = mapped.to_numpy()
    return out


# --------------------------------------------------------------------- #
# liquidity-sweep / market-structure primitives, shared by the two
# XAUUSD liquidity strategies in strategies.py. Each is a plain vectorized
# helper — no lookahead as long as callers only read index i in generate().
# --------------------------------------------------------------------- #
def prior_swing_high(df: pd.DataFrame, lookback: int = 10, offset: int = 1) -> pd.Series:
    """Highest high over `lookback` bars ending `offset` bars ago — a cheap
    stand-in for "the prior swing high" without a full fractal-pivot
    detector. offset=1 means "excluding the current bar"."""
    return df["high"].shift(offset).rolling(lookback, min_periods=1).max()


def prior_swing_low(df: pd.DataFrame, lookback: int = 10, offset: int = 1) -> pd.Series:
    return df["low"].shift(offset).rolling(lookback, min_periods=1).min()


def confirmed_swing_high(df: pd.DataFrame, n: int) -> pd.Series:
    """The real fractal pivot prior_swing_high only approximates: bar i is a
    swing high iff high[i] == max(high[i-n : i+n+1]) — a genuine CENTERED
    window, not a trailing lookback. Critically, this can't be known until
    bar i+n has closed (nobody, live or in a backtest, can tell bar i was
    the local max before seeing the n bars after it) — so the pivot's own
    price is exposed starting at bar i+n, not bar i, then held (ffill)
    until a fresher pivot confirms. A trailing-lookback max has no such
    confirmation lag, which is exactly what makes it a different (and
    weaker) definition of "swing level"."""
    high = df["high"]
    is_pivot = high == high.rolling(2 * n + 1, center=True).max()
    return high.where(is_pivot).shift(n).ffill()


def confirmed_swing_low(df: pd.DataFrame, n: int) -> pd.Series:
    low = df["low"]
    is_pivot = low == low.rolling(2 * n + 1, center=True).min()
    return low.where(is_pivot).shift(n).ffill()


def fair_value_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """3-candle imbalance ending at the current bar (candle1 = i-2, candle3 = i).

    Bullish FVG: high[i-2] < low[i]  -> gap = (bottom=high[i-2], top=low[i])
    Bearish FVG: low[i-2]  > high[i] -> gap = (top=low[i-2], bottom=high[i])
    """
    bull_bottom = df["high"].shift(2)
    bull_top = df["low"]
    bear_top = df["low"].shift(2)
    bear_bottom = df["high"]
    return pd.DataFrame({
        "bull_fvg": bull_bottom < bull_top,
        "bull_fvg_top": bull_top,
        "bull_fvg_bottom": bull_bottom,
        "bull_fvg_size": bull_top - bull_bottom,
        "bear_fvg": bear_bottom < bear_top,
        "bear_fvg_top": bear_top,
        "bear_fvg_bottom": bear_bottom,
        "bear_fvg_size": bear_top - bear_bottom,
    }, index=df.index)


def displacement(df: pd.DataFrame, atr_col: pd.Series, range_atr_mult: float = 0.8, body_ratio: float = 0.6) -> pd.DataFrame:
    """A candle that "proves intent": range wide relative to ATR, most of it
    real body rather than wick. Returns columns disp_bull, disp_bear."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    body = (df["close"] - df["open"]).abs()
    wide_enough = rng >= range_atr_mult * atr_col
    strong_body = (body / rng) >= body_ratio
    return pd.DataFrame({
        "disp_bull": (df["close"] > df["open"]) & wide_enough & strong_body,
        "disp_bear": (df["close"] < df["open"]) & wide_enough & strong_body,
    }, index=df.index).fillna(False)


def htf_ema_bias(df: pd.DataFrame, timeframe: str, fast: int = 50, slow: int = 200) -> pd.Series:
    """+1 bullish / -1 bearish / 0 neither, from a higher timeframe's own
    close vs EMA(fast)/EMA(slow) — computed on last COMPLETED htf bar only
    (shift(1)) and forward-filled back onto df's index, so nothing here
    ever reads a still-forming higher-timeframe candle."""
    htf = df.resample(timeframe).agg({"close": "last"}).dropna()
    htf_fast = ema(htf["close"], fast).shift(1)
    htf_slow = ema(htf["close"], slow).shift(1)
    htf_close = htf["close"].shift(1)
    bias = pd.Series(0, index=htf.index, dtype=int)
    bias[(htf_close > htf_fast) & (htf_fast > htf_slow)] = 1
    bias[(htf_close < htf_fast) & (htf_fast < htf_slow)] = -1
    return bias.reindex(df.index, method="ffill").fillna(0).astype(int)


def zigzag_pivots(df: pd.DataFrame, atr_col: pd.Series, atr_mult: float = 2.0):
    """Confirmed alternating swing pivots using an ATR-scaled reversal
    threshold. Returns (pivot_high, pivot_low) Series stamped at the bar
    where the reversal was CONFIRMED (not the pivot's own bar) — a pivot
    only becomes visible once price has since moved atr_mult*ATR away from
    it, so there's no lookahead into the pivot's own future."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    a = atr_col.to_numpy(float)
    n = len(df)
    pivot_high = np.full(n, np.nan)
    pivot_low = np.full(n, np.nan)
    direction = 0
    candidate_price = np.nan
    for i in range(n):
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        threshold = atr_mult * a[i]
        if direction == 0:
            candidate_price = high[i]
            direction = 1
            continue
        if direction == 1:
            if high[i] > candidate_price:
                candidate_price = high[i]
            elif candidate_price - low[i] >= threshold:
                pivot_high[i] = candidate_price
                direction = -1
                candidate_price = low[i]
        else:
            if low[i] < candidate_price:
                candidate_price = low[i]
            elif high[i] - candidate_price >= threshold:
                pivot_low[i] = candidate_price
                direction = 1
                candidate_price = high[i]
    return (
        pd.Series(pivot_high, index=df.index),
        pd.Series(pivot_low, index=df.index),
    )
