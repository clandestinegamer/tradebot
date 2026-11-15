"""Real-time stop protection between the bot's once-a-minute checks.

• Upbit positions: a live WebSocket price feed (wss://api.upbit.com/websocket/v1). Every trade tick is
  checked against the position's stop, so a crash is acted on within seconds, day or night.
• KIS positions (Korean / US stocks): prices checked every 10 seconds while the market is open.

Only stops, break-even and the trailing stop are handled here; entries still happen in the normal loop.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

log = logging.getLogger("tradebot")
UPBIT_WS = "wss://api.upbit.com/websocket/v1"


def parse_upbit_tick(raw) -> tuple[str, float] | None:
    try:
        d = json.loads(raw if isinstance(raw, str) else raw.decode())
        return d["code"], float(d["trade_price"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class StopWatcher(threading.Thread):
    def __init__(self, engine, kis_every: float = 10.0):
        super().__init__(daemon=True, name="stop-watcher")
        self.e = engine
        self.kis_every = kis_every
        self._halt = threading.Event()
        self.last_ws_tick: float | None = None
        self.status = "starting"

    def stop(self) -> None:
        self._halt.set()

    def _swing_keys(self, market_filter) -> list:
        return [p for p in self.e.swing_positions() if market_filter(p.market)]

    def run(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="stop-poll").start()
        if self.e.data.demo:
            self.status = "demo: polling every few seconds"
            return
        while not self._halt.is_set():
            codes = sorted({p.symbol for p in self._swing_keys(lambda m: m == "upbit")})
            if not codes:
                self.status = "no crypto positions — live feed idle"
                self._halt.wait(15)
                continue
            try:
                self._ws_session(codes)
            except Exception as ex:
                self.status = f"live feed reconnecting ({type(ex).__name__})"
                log.warning("Upbit live feed error: %s", ex)
                self._halt.wait(5)

    def _ws_session(self, codes: list[str]) -> None:
        from websockets.sync.client import connect
        with connect(UPBIT_WS, open_timeout=10, ping_interval=60) as ws:
            ws.send(json.dumps([{"ticket": str(uuid.uuid4())}, {"type": "ticker", "codes": codes},
                                {"format": "DEFAULT"}]))
            self.status = f"live feed on {', '.join(codes)}"
            started = time.time()
            while not self._halt.is_set():
                raw = ws.recv(timeout=30)
                tick = parse_upbit_tick(raw)
                if tick:
                    self.last_ws_tick = time.time()
                    self.e.fast_check(f"upbit:{tick[0]}", tick[1])
                # re-subscribe when the set of held coins changes (checked every 30 s)
                if time.time() - started > 30:
                    if sorted({p.symbol for p in self._swing_keys(lambda m: m == "upbit")}) != codes:
                        return
                    started = time.time()

    def _poll_loop(self) -> None:
        demo = bool(self.e.data.demo)
        while not self._halt.is_set():
            every = 3.0 if demo else self.kis_every
            for p in self._swing_keys(lambda m: demo or m != "upbit"):
                s = self.e.cfg.symbol(p.key)
                try:
                    if s and self.e.data.is_open(s):
                        self.e.fast_check(p.key, self.e.data.price(s))
                except Exception as ex:
                    log.warning("fast stop check %s failed: %s", p.key, ex)
            self._halt.wait(every)
