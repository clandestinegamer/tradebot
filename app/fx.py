"""USD/KRW exchange rates (ECB reference rates via the free Frankfurter API), cached per day.

Used to value US stocks in KRW and to record the rate on every trade for tax reporting.
Falls back to `usd_krw` in config.toml if the API can't be reached.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import requests

log = logging.getLogger("tradebot")
URL = "https://api.frankfurter.dev/v1/{day}"


class FXRates:
    def __init__(self, store, fallback: float, online: bool = True, session=None):
        self.store, self.fallback, self.online = store, fallback, online
        self.http = session or requests.Session()
        self._failed_day: str | None = None

    def usd_krw(self, day: date | None = None) -> float:
        day = day or datetime.now(timezone.utc).date()
        key = f"fx:{day.isoformat()}"
        cached = self.store.get(key)
        if cached:
            return float(cached)
        if not self.online or self._failed_day == day.isoformat():
            return self.fallback
        try:
            path = "latest" if day >= datetime.now(timezone.utc).date() else day.isoformat()
            d = self.http.get(URL.format(day=path), params={"base": "USD", "symbols": "KRW"}, timeout=8).json()
            rate = float(d["rates"]["KRW"])
            self.store.set(key, rate)
            return rate
        except Exception as e:
            log.warning("FX lookup failed (%s); using fallback %.1f", e, self.fallback)
            self._failed_day = day.isoformat()  # don't hammer the API for the rest of the day
            return self.fallback
