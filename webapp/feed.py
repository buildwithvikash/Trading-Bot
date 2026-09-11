"""Market feed for Paper Trading and Live mode.

Two sources behind one interface:
  - "simulated": replays the loaded historical dataset bar-by-bar on a
    wall-clock timer. Always available, no external dependency.
  - "biquote": real bid/ask ticks pushed instantly over biquote.io's
    SignalR stream (webapp.biquote_stream) — biquote's own recommendation
    over polling, and ~2/second in practice vs. a 2-second poll — plus
    genuine recent OHLC bars pulled periodically from their REST endpoint
    (webapp.biquote_client) for authoritative bar-close detection (matches
    biquote's own bar boundaries exactly, no self-aggregation drift).
    start() falls back to "simulated" if a fetch fails outright.

Listeners (the paper-trading tick processor) subscribe once and get called
for every new bar regardless of which source produced it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pandas as pd

from gold_bot.config import CostConfig
from gold_bot.costs import spread_at
from gold_bot.data import resample
from webapp import biquote_client
from webapp.biquote_stream import TickStream

REPO_ROOT = Path(__file__).resolve().parent.parent


class MarketFeed:
    def __init__(self, seconds_per_bar: float = 2.0):
        self.tf = "15min"
        self.seconds_per_bar = seconds_per_bar
        self.source = "simulated"  # "simulated" | "biquote"
        self._df = None
        self._i = 0
        self._cycles = 0
        self._running = False
        self._thread = None
        self._lock = threading.RLock()
        self._listeners = []
        self._costs = CostConfig()
        self._stream = TickStream()

        self._live_bar = None      # last fully closed real bar from biquote
        self._live_tick = None     # latest {bid, ask, mid} — pushed by the tick stream
        self._live_forming = None  # the in-progress bar, ticking live between closes
        self._live_error = None

    def configure(self, raw_getter, tf_map: dict, tf: str = "15min"):
        """Wire in market.py's cached raw dataframe + timeframe map without
        importing webapp.routers.market at module load time (circular)."""
        self._raw_getter = raw_getter
        self._tf_map = tf_map
        self.tf = tf

    def set_timeframe(self, tf: str):
        """Change the feed's working timeframe — used when auto-trade starts
        a strategy that wants a different bar size than whatever's
        currently configured (see Strategy.timeframe / RuleStrategy.timeframe
        and webapp.autotrade.AutoTrader.start). Safe to call while running:
        for "simulated", clearing _df makes the replay loop reload/resample
        at the new tf on its next iteration; for "biquote", _biquote_loop
        already reads self.tf fresh every iteration, so it starts polling
        the new timeframe within one cycle — no reconnect needed, since
        ticks themselves aren't timeframe-specific. Bar-shaped state is
        cleared so a stale bar from the OLD timeframe can never leak into
        the new one; the live tick/price is left alone (still valid)."""
        with self._lock:
            if tf == self.tf:
                return
            self.tf = tf
            self._df = None
            self._i = 0
            self._live_bar = None
            self._live_forming = None

    def subscribe(self, cb):
        self._listeners.append(cb)

    # ------------------------------------------------------------------ #
    # simulated source
    # ------------------------------------------------------------------ #
    def _load(self):
        df, meta = self._raw_getter()
        alias, minutes = self._tf_map[self.tf]
        base_minutes = meta.get("base_minutes")
        self._df = df if (base_minutes and minutes <= base_minutes) else resample(df, alias)
        self._i = 0

    def _sim_loop(self):
        while True:
            with self._lock:
                if not self._running or self.source != "simulated":
                    return
                self._i += 1
                if self._i >= len(self._df):
                    self._i = 0
                    self._cycles += 1
                bar = self._sim_bar()
            self._notify(bar)
            time.sleep(self.seconds_per_bar)

    def _sim_bar(self) -> dict | None:
        if self._df is None:
            self._load()
        if self._df is None or len(self._df) == 0:
            return None
        row = self._df.iloc[self._i]
        ts = self._df.index[self._i]
        return {
            "time": ts, "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
        }

    # ------------------------------------------------------------------ #
    # biquote source. Two independent paths feed the same state:
    #   - _on_ws_tick(): called by the SignalR stream on every live tick
    #     (~2/second) — updates price instantly and extends the forming
    #     bar's high/low/close in real time between REST polls.
    #   - _biquote_loop(): a slower REST poll purely for authoritative
    #     bar-close detection (biquote flags the current bar with
    #     isOpen=True, so a close is just that flipping False on a new
    #     openTime) and to periodically resync the forming bar against
    #     biquote's own server-side aggregation — merged rather than
    #     overwritten, so a brief gap in the tick stream can't regress an
    #     extreme either side has already seen.
    # ------------------------------------------------------------------ #
    def _on_ws_tick(self, tick: dict):
        with self._lock:
            if self.source != "biquote":
                return
            mid = float(tick["mid"])
            self._live_tick = {"bid": float(tick["bid"]), "ask": float(tick["ask"]), "mid": mid}
            self._live_error = None
            if self._live_forming is not None:
                self._live_forming["high"] = max(self._live_forming["high"], mid)
                self._live_forming["low"] = min(self._live_forming["low"], mid)
                self._live_forming["close"] = mid

    def _biquote_loop(self):
        # get_ohlc() only swallows its own BiquoteError — a raw network
        # exception (connect/read timeout, connection reset; all observed
        # against the real biquote.io endpoint) propagates straight out of
        # it. Uncaught here, that kills this whole thread for good, and
        # nothing else ever restarts it: the feed keeps reporting
        # "running": true forever after while silently never advancing a
        # bar again. Same failure mode fixed in AutoTrader._biquote_loop —
        # any exception in one poll iteration must not end the loop.
        last_closed_time = None
        while True:
            with self._lock:
                if not self._running or self.source != "biquote":
                    return
                tf = self.tf
            try:
                bars = biquote_client.get_ohlc(tf, limit=2)
                if not bars:
                    with self._lock:
                        self._live_error = "biquote.io unreachable"
                    time.sleep(2.0)
                    continue

                with self._lock:
                    self._live_error = None

                closed = [b for b in bars if not b.get("isOpen")]
                forming = [b for b in bars if b.get("isOpen")]
                if closed:
                    latest = closed[-1]
                    ts = pd.Timestamp(latest["openTime"])
                    if ts != last_closed_time:
                        last_closed_time = ts
                        bar = {
                            "time": ts, "open": float(latest["open"]), "high": float(latest["high"]),
                            "low": float(latest["low"]), "close": float(latest["close"]),
                        }
                        with self._lock:
                            self._live_bar = bar
                        self._notify(bar)
                if forming:
                    f = forming[-1]
                    ts = pd.Timestamp(f["openTime"])
                    new_high, new_low, new_close = float(f["high"]), float(f["low"]), float(f["close"])
                    with self._lock:
                        cur = self._live_forming
                        if cur is not None and cur["time"] == ts:
                            new_high = max(new_high, cur["high"])
                            new_low = min(new_low, cur["low"])
                        self._live_forming = {
                            "time": ts, "open": float(f["open"]), "high": new_high, "low": new_low, "close": new_close,
                        }
            except Exception as exc:
                with self._lock:
                    self._live_error = f"biquote poll error: {exc}"
                print(f"[feed] biquote poll error: {exc!r}")
            time.sleep(2.0)

    def _notify(self, bar):
        for cb in list(self._listeners):
            try:
                cb(bar)
            except Exception as exc:  # a bad tick must never kill the feed thread
                print(f"[feed] listener error: {exc}")

    # ------------------------------------------------------------------ #
    # public interface — same shape regardless of source
    # ------------------------------------------------------------------ #
    def current_bar(self) -> dict | None:
        with self._lock:
            return self._live_bar if self.source == "biquote" else self._sim_bar()

    def current_forming_bar(self) -> dict | None:
        """The in-progress bar for the Chart view to poll for a genuinely
        live-moving candle — ticks between closes instead of only updating
        once per bar close. biquote only: the simulated replay has no real
        between-bar tick data to synthesize this from."""
        with self._lock:
            return dict(self._live_forming) if (self.source == "biquote" and self._live_forming) else None

    def current_index(self) -> int | None:
        """Position of the current bar within dataframe(), for callers (the
        strategy auto-trader) that need to call Strategy.generate(df, i, ...)
        with the same index space the simulated feed is walking. None for the
        biquote source, which has no fixed historical index."""
        with self._lock:
            return self._i if (self.source == "simulated" and self._df is not None) else None

    def dataframe(self):
        """The raw (unprepared) local historical dataframe, at the feed's
        configured timeframe. Used by the simulated replay, and as the base
        layer a fresh biquote session's indicator history seeds from (see
        recent_history() below for the layer that closes the gap up to now).
        None until the feed has loaded data."""
        with self._lock:
            if self._df is None:
                self._load()
            return self._df

    def recent_history(self, count: int = 500):
        """Real recent OHLC from biquote (closed bars only — excludes the
        still-forming last one), for seeding a fresh biquote session's
        indicator warmup with actual continuity up to now, instead of a
        multi-day gap between the local dataset's last bar and the live
        tick. Only meaningful for the biquote source; None otherwise, or if
        biquote has nothing for this timeframe right now."""
        if self.source != "biquote":
            return None
        bars = [b for b in biquote_client.get_ohlc(self.tf, count) if not b.get("isOpen")]
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

    def current_price(self) -> dict | None:
        if self.source == "biquote":
            with self._lock:
                tick = self._live_tick
                bar = self._live_forming or self._live_bar
            if tick is None or bar is None:
                return None
            return {
                "bid": round(tick["bid"], 3), "ask": round(tick["ask"], 3), "mid": round(tick["mid"], 3),
                "spread": round(tick["ask"] - tick["bid"], 3), "time": bar["time"].isoformat(),
            }
        bar = self.current_bar()
        if bar is None:
            return None
        spread = spread_at(bar["time"], self._costs)
        mid = bar["close"]
        return {
            "bid": round(mid - spread / 2, 3), "ask": round(mid + spread / 2, 3),
            "mid": round(mid, 3), "spread": round(spread, 3), "time": bar["time"].isoformat(),
        }

    def start(self, source: str | None = None):
        with self._lock:
            switching = source is not None and self._running and source != self.source
            old_source = self.source
            if source:
                self.source = source
            if self._running and not switching:
                # already running the same source — still worth checking
                # whether the biquote tick stream has silently died (a
                # dropped connection signalrcore's own auto-reconnect
                # didn't recover) and needs a kick. Doesn't touch the REST
                # poll loop/thread at all, so this is safe to call as often
                # as a caller likes without disturbing anything running.
                if self.source == "biquote" and not self._stream.is_connected():
                    self._stream.start(on_tick=self._on_ws_tick)
                return
            if old_source == "biquote" and self.source != "biquote":
                self._stream.stop()
            if self.source == "biquote":
                if self._df is None:
                    self._load()  # still needed as the deep-history base layer
                if biquote_client.get_tick() is None:
                    self.source = "simulated"
                else:
                    self._stream.start(on_tick=self._on_ws_tick)
            elif self._df is None:
                self._load()
            self._running = True
            target = self._biquote_loop if self.source == "biquote" else self._sim_loop
            thread = threading.Thread(target=target, daemon=True)
            self._thread = thread
        thread.start()

    def stop(self):
        with self._lock:
            self._running = False
            self._stream.stop()

    def is_running(self) -> bool:
        return self._running

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "source": self.source,
                "index": self._i,
                "bars": len(self._df) if self._df is not None else 0,
                "cycles": self._cycles,
                "seconds_per_bar": self.seconds_per_bar,
                "timeframe": self.tf,
                "live_error": self._live_error,
                "stream_connected": self._stream.is_connected(),
            }

    def set_speed(self, seconds_per_bar: float):
        self.seconds_per_bar = max(0.2, seconds_per_bar)


FEED = MarketFeed()
