"""Synthetic market data so you can run and test everything without API keys.

Prices follow a random walk with slowly shifting trends (so strategies get both
up- and down-trends). One candle lasts `bar_seconds` of real time.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time

import numpy as np
import pandas as pd

START_PRICES = {"KRW-BTC": 140_000_000.0, "KRW-ETH": 5_000_000.0, "KRW-XRP": 3_000.0,
                "005930": 75_000.0, "000660": 200_000.0, "AAPL": 230.0, "MSFT": 420.0, "NVDA": 140.0}


class DemoData:
    def __init__(self, bar_seconds: int = 30, history: int = 300):
        self.bar_seconds = max(1, int(bar_seconds))
        self.history = history
        self._paths: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _seed(self, key: str) -> int:
        return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)

    def _vol(self, key: str) -> float:
        return 0.012 if key.startswith("upbit") else 0.008

    def _start_price(self, key: str) -> float:
        sym = key.split(":", 1)[1]
        if sym in START_PRICES:
            return START_PRICES[sym]
        return 200.0 if key.startswith("kis_us") else 50_000.0

    def _extend(self, key: str, upto: int) -> dict:
        p = self._paths.get(key)
        if p is None:
            first = upto - self.history
            p = {"first": first, "rows": [], "last_close": self._start_price(key)}
            self._paths[key] = p
        while p["first"] + len(p["rows"]) < upto:  # complete bars up to (not incl.) `upto`
            i = p["first"] + len(p["rows"])
            o = p["last_close"]
            r, wig, z, vol_ = self._bar_draws(key, i)
            c = o * math.exp(r)
            p["rows"].append((i, o, max(o, c) * (1 + wig), min(o, c) * (1 - wig), c, vol_))
            p["last_close"] = c
        return p

    def _bar_draws(self, key: str, i: int) -> tuple[float, float, float, float]:
        """Deterministic per-bar randomness, so a candle's live price ends exactly where the candle closes."""
        rng = np.random.default_rng([self._seed(key), i])
        vol = self._vol(key)
        drift = 0.004 * math.sin(i / 35.0) + 0.0015 * math.sin(i / 9.0)
        return rng.normal(drift, vol), abs(rng.normal(0, vol / 2)), rng.normal(0, 1), float(rng.integers(100, 10_000))

    def _forming(self, key: str, idx: int, o: float, now: float) -> tuple:
        frac = (now % self.bar_seconds) / self.bar_seconds
        r, _, z, vol_ = self._bar_draws(key, idx)
        # Brownian bridge from open toward the candle's final close; volume accumulates through the candle
        c = o * math.exp(r * frac + z * self._vol(key) * 0.5 * math.sqrt(frac * (1 - frac)))
        return (idx, o, max(o, c), min(o, c), c, vol_ * frac)

    def candles(self, key: str, count: int = 200) -> pd.DataFrame:
        now = time.time()
        idx = int(now // self.bar_seconds)
        with self._lock:
            p = self._extend(key, idx)
            rows = p["rows"][-(count - 1):] + [self._forming(key, idx, p["last_close"], now)]
        df = pd.DataFrame(rows, columns=["i", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["i"] * self.bar_seconds, unit="s", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S")
        return df.drop(columns="i").set_index("ts")

    def price(self, key: str) -> float:
        return float(self.candles(key, 2)["close"].iloc[-1])
