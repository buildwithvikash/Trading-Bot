"""Configuration objects for the backtester and the live signal bot."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CostConfig:
    """Transaction cost model for XAUUSD, in USD per ounce (i.e. price units).

    Gold spreads are NOT constant. They widen sharply around the 13:30 UTC US
    data window and again at the 21:00-22:00 UTC daily rollover. Ignoring this
    is the single most common reason a NY-session backtest looks profitable and
    then dies live.
    """

    base_spread: float = 0.30          # typical retail XAUUSD spread, USD/oz
    news_window_multiplier: float = 4.0
    rollover_multiplier: float = 5.0
    slippage_per_side: float = 0.10    # USD/oz, each side
    commission_per_lot_roundtrip: float = 0.0  # USD per 100oz lot, if ECN

    # UTC hour:minute windows where the spread multiplier applies
    news_window: tuple[str, str] = ("13:25", "13:45")
    rollover_window: tuple[str, str] = ("20:55", "22:05")


@dataclass
class AccountConfig:
    initial_equity: float = 10_000.0
    risk_per_trade_pct: float = 0.5     # % of equity risked to the initial stop
    max_risk_per_trade_pct: float = 2.0
    contract_size: float = 100.0        # 1 standard lot = 100 oz
    min_lot: float = 0.01
    max_lot: float = 50.0
    compound: bool = True               # size off current equity vs initial


@dataclass
class EngineConfig:
    """Execution assumptions. Deliberately pessimistic where ambiguous."""

    one_position_at_a_time: bool = True
    # If a bar's range contains both the stop and the target, which filled
    # first is unknowable from OHLC. "stop" = assume the loss. Always keep
    # this as "stop" for honest results; "tp" exists only to show you the gap.
    ambiguous_bar_resolution: str = "stop"
    partial_at_tp1_pct: float = 50.0    # % of position closed at TP1
    move_stop_to_breakeven_after_tp1: bool = True
    max_trades_per_day: int = 3
    warmup_bars: int = 200

    # ---- risk circuit breakers (Phase 4) ------------------------------- #
    # None disables the check. Both are "no NEW entries", not a forced exit
    # of whatever is already open — the position in flight still manages
    # normally via its own stop/target/trail.
    max_daily_loss_pct: float | None = None   # halt new entries for the rest of the day
    max_drawdown_pct: float | None = None     # halt new entries for the rest of the run
    # Informational cap only in this single-position batch engine (which only
    # ever holds one position via one_position_at_a_time); the paper-trading
    # engine (Phase 5) is where multiple concurrent positions — and this
    # limit — actually apply.
    max_open_positions: int = 1


@dataclass
class RunConfig:
    symbol: str = "XAUUSD"
    timeframe: str = "15min"
    account: AccountConfig = field(default_factory=AccountConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
