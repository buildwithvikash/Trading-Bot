#!/usr/bin/env python3
"""Live / paper signal bot.

It runs the SAME strategy classes the backtester uses. That is the whole point:
if the live logic and the backtest logic are two separate implementations, you
have no idea which one your results describe.

Defaults are paper-only. Order placement requires --live AND a config file with
`i_have_forward_tested: true`, because the failure mode here is expensive.

Requires MetaTrader5 (Windows):   pip install MetaTrader5 pandas numpy

    python live_bot.py --strategy trend_pullback --symbol XAUUSD --tf 15min
    python live_bot.py --strategy ny_orb --live --config live.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from gold_bot.costs import spread_at
from gold_bot.config import CostConfig
from gold_bot.strategies import STRATEGY_REGISTRY

LOG = Path("signals_log.csv")

TF_MAP = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "1h": 60, "4h": 240}


class Broker:
    """Thin MT5 wrapper. Everything that touches the account lives here."""

    def __init__(self, symbol: str, dry_run: bool = True):
        import MetaTrader5 as mt5  # imported lazily so backtests don't need it

        self.mt5 = mt5
        self.symbol = symbol
        self.dry_run = dry_run
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"cannot select {symbol}")

    def bars(self, timeframe: str, count: int = 1500) -> pd.DataFrame:
        mt5 = self.mt5
        tf_const = {
            1: mt5.TIMEFRAME_M1, 5: mt5.TIMEFRAME_M5, 15: mt5.TIMEFRAME_M15,
            30: mt5.TIMEFRAME_M30, 60: mt5.TIMEFRAME_H1, 240: mt5.TIMEFRAME_H4,
        }[TF_MAP[timeframe]]
        rates = mt5.copy_rates_from_pos(self.symbol, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError("no bars returned")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")[["open", "high", "low", "close", "tick_volume"]]
        df = df.rename(columns={"tick_volume": "volume"})
        # drop the still-forming bar: strategies only ever see closed bars
        return df.iloc[:-1]

    def equity(self) -> float:
        return float(self.mt5.account_info().equity)

    def current_spread(self) -> float:
        tick = self.mt5.symbol_info_tick(self.symbol)
        return float(tick.ask - tick.bid)

    def open_positions(self) -> int:
        pos = self.mt5.positions_get(symbol=self.symbol)
        return len(pos) if pos else 0

    def send(self, direction: int, lots: float, sl: float, tp: float, comment: str):
        mt5 = self.mt5
        if self.dry_run:
            print(f"  [DRY RUN] would send {('BUY' if direction == 1 else 'SELL')} "
                  f"{lots} lots  SL {sl:.2f}  TP {tp:.2f}")
            return None
        tick = mt5.symbol_info_tick(self.symbol)
        price = tick.ask if direction == 1 else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": 770077,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        print(f"  order_send -> retcode={result.retcode} {result.comment}")
        return result


class RiskGuard:
    """Hard limits. These stop a bug from becoming a blown account."""

    def __init__(self, cfg: dict):
        self.max_daily_loss_pct = cfg.get("max_daily_loss_pct", 3.0)
        self.max_trades_per_day = cfg.get("max_trades_per_day", 3)
        self.max_spread = cfg.get("max_spread_usd", 0.60)
        self.max_open = cfg.get("max_open_positions", 1)
        self.risk_pct = cfg.get("risk_per_trade_pct", 0.5)
        self.day = None
        self.start_equity = None
        self.trades_today = 0

    def new_day_check(self, equity: float):
        today = datetime.now(timezone.utc).date()
        if self.day != today:
            self.day = today
            self.start_equity = equity
            self.trades_today = 0

    def allows(self, broker: Broker, equity: float) -> tuple[bool, str]:
        self.new_day_check(equity)
        dd = 100 * (equity - self.start_equity) / self.start_equity
        if dd <= -self.max_daily_loss_pct:
            return False, f"daily loss limit hit ({dd:.2f}%)"
        if self.trades_today >= self.max_trades_per_day:
            return False, "max trades per day reached"
        if broker.open_positions() >= self.max_open:
            return False, "position already open"
        spread = broker.current_spread()
        if spread > self.max_spread:
            return False, f"spread too wide ({spread:.2f} USD)"
        return True, ""

    def lots(self, equity: float, risk_per_oz: float, contract: float = 100.0) -> float:
        if risk_per_oz <= 0:
            return 0.0
        amount = equity * self.risk_pct / 100.0
        return round(max(0.01, min(50.0, (amount / risk_per_oz) / contract)), 2)


def log_signal(row: dict):
    df = pd.DataFrame([row])
    df.to_csv(LOG, mode="a", header=not LOG.exists(), index=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="trend_pullback", choices=list(STRATEGY_REGISTRY))
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--tf", default="15min", choices=list(TF_MAP))
    p.add_argument("--live", action="store_true", help="actually place orders")
    p.add_argument("--config", default=None, help="json with risk limits")
    p.add_argument("--poll", type=int, default=20, help="seconds between checks")
    args = p.parse_args()

    cfg = json.loads(Path(args.config).read_text()) if args.config else {}

    if args.live and not cfg.get("i_have_forward_tested"):
        raise SystemExit(
            "Refusing to trade live. Run this on a demo account for at least "
            "one month, then set \"i_have_forward_tested\": true in the config."
        )

    broker = Broker(args.symbol, dry_run=not args.live)
    guard = RiskGuard(cfg)
    strategy = STRATEGY_REGISTRY[args.strategy]()
    costs = CostConfig()

    print(f"{args.strategy} on {args.symbol} {args.tf} — "
          f"{'LIVE' if args.live else 'PAPER (dry run)'}")

    last_bar = None
    state: dict = {}

    while True:
        try:
            df = broker.bars(args.tf)
            if df.empty:
                time.sleep(args.poll)
                continue
            if df.index[-1] == last_bar:
                time.sleep(args.poll)
                continue
            last_bar = df.index[-1]

            prepared = strategy.prepare(df.copy())
            i = len(prepared) - 1
            orders = strategy.generate(prepared, i, state)

            ts = prepared.index[i]
            print(f"[{ts}] close={prepared['close'].iloc[i]:.2f} "
                  f"spread={broker.current_spread():.2f} "
                  f"expected={spread_at(ts, costs):.2f}")

            for order in orders:
                equity = broker.equity()
                ok, why = guard.allows(broker, equity)
                ref = order.trigger if order.kind == "stop" else prepared["close"].iloc[i]
                risk_per_oz = abs(ref - order.stop_loss)
                lots = guard.lots(equity, risk_per_oz)
                tp = order.tp2 or order.tp1

                log_signal({
                    "time": ts, "strategy": strategy.name, "symbol": args.symbol,
                    "direction": order.direction, "kind": order.kind,
                    "ref_price": ref, "sl": order.stop_loss, "tp": tp,
                    "lots": lots, "accepted": ok, "blocked_reason": why,
                })

                if not ok:
                    print(f"  signal BLOCKED: {why}")
                    continue

                print(f"  SIGNAL {'BUY' if order.direction == 1 else 'SELL'} "
                      f"ref={ref:.2f} SL={order.stop_loss:.2f} TP={tp:.2f} lots={lots}")
                broker.send(order.direction, lots, order.stop_loss, tp, strategy.name)
                guard.trades_today += 1

        except KeyboardInterrupt:
            print("\nstopped")
            break
        except Exception as exc:
            print(f"error: {exc}")

        time.sleep(args.poll)


if __name__ == "__main__":
    main()
