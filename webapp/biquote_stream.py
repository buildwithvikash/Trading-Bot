"""Real-time XAU/USD tick stream from biquote.io's SignalR hub, the
officially recommended way to get live prices from biquote instead of
polling (their own docs: "Polling for live prices? Don't. ... it does not
count against the rate limit at all."). Pushes ticks the instant they
happen — in testing, ~2/second — instead of only ever seeing whatever
price happened to be current at the last periodic poll.

webapp.feed still polls biquote's REST OHLC endpoint on a slower interval
for authoritative bar-close detection (matches biquote's own bar
boundaries exactly, avoiding any drift from self-aggregating bars purely
from the tick stream) — this module only replaces the tick/price side.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from signalrcore.hub_connection_builder import HubConnectionBuilder

HUB_URL = "https://biquote.io/hubs/tick"
SYMBOL = "XAUUSD"


class TickStream:
    def __init__(self):
        self._lock = threading.Lock()
        self._hub = None
        self._connected = False
        self._latest: Optional[dict] = None
        self._on_tick: Optional[Callable[[dict], None]] = None

    def _handle_tick(self, args):
        tick = args[0] if args else None
        if not tick or tick.get("symbol") != SYMBOL:
            return
        with self._lock:
            self._latest = tick
            cb = self._on_tick
        if cb:
            try:
                cb(tick)
            except Exception as exc:  # a bad callback must never kill the stream
                print(f"[biquote_stream] on_tick callback error: {exc}")

    def _set_connected(self, value: bool):
        with self._lock:
            self._connected = value
            hub = self._hub
        if value and hub is not None:
            try:
                hub.send("Subscribe", [[SYMBOL]])
            except Exception:
                pass

    def start(self, on_tick: Optional[Callable[[dict], None]] = None) -> bool:
        """Idempotent in the useful sense: calling this again while genuinely
        connected just updates the callback and does nothing else. But a
        hub OBJECT existing doesn't mean the connection is alive — on_close/
        on_error can fire (network drop, server restart) without
        signalrcore's own automatic-reconnect actually recovering the
        socket, and the old code treated "a hub object exists" as "nothing
        to do," permanently stranding the stream disconnected with no way
        to kick it back up short of a full feed stop/start. Any call made
        while not connected now discards the stale hub and builds a fresh
        one. Returns whether a connection attempt could be made at all (not
        whether it's connected yet — that's is_connected(), since the
        handshake is asynchronous)."""
        with self._lock:
            self._on_tick = on_tick
            if self._hub is not None and self._connected:
                return True
            stale_hub = self._hub
            self._hub = None
        if stale_hub is not None:
            try:
                stale_hub.stop()
            except Exception:
                pass
        with self._lock:
            hub = (
                HubConnectionBuilder()
                .with_url(HUB_URL)
                # CRITICAL, not WARNING: signalrcore logs its own socket-closed
                # exception at ERROR level on every clean stop() — expected
                # noise, not a real problem; our own on_error/on_close/status()
                # already surface anything actually worth knowing about.
                .configure_logging(logging.CRITICAL)
                .with_automatic_reconnect({
                    "type": "raw", "keep_alive_interval": 10,
                    "reconnect_interval": 5, "max_attempts": None,
                })
                .build()
            )
            hub.on_open(lambda: self._set_connected(True))
            hub.on_close(lambda: self._set_connected(False))
            hub.on_error(lambda err: self._set_connected(False))
            hub.on("ReceiveTick", self._handle_tick)
            self._hub = hub
        try:
            hub.start()
            hub.send("Subscribe", [[SYMBOL]])
            return True
        except Exception as exc:
            print(f"[biquote_stream] failed to start: {exc}")
            with self._lock:
                self._hub = None
                self._connected = False
            return False

    def stop(self):
        with self._lock:
            hub, self._hub, self._connected, self._on_tick = self._hub, None, False, None
        if hub is not None:
            try:
                hub.stop()
            except Exception:
                pass

    def latest_tick(self) -> Optional[dict]:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected
