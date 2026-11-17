"""Exchange holiday calendars.

US (NYSE/Nasdaq): computed from the exchange's standing holiday rules.
Korea (KRX): asked from KIS's 국내휴장일조회 API once a day (KIS asks callers to limit it to ~1/day),
falling back to weekends + [holidays].krx_extra in config.toml if KIS isn't reachable.
Rare one-off closures (e.g. a national day of mourning) → add them to [holidays] in config.toml.
Early-close days (US 1pm closes) are not modelled.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

log = logging.getLogger("tradebot")


def _easter(y: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> date:
    d = date(y + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:   # Saturday → Friday before
        return d - timedelta(days=1)
    if d.weekday() == 6:   # Sunday → Monday after
        return d + timedelta(days=1)
    return d


def nyse_holidays(y: int) -> set[date]:
    out = set()
    ny = date(y, 1, 1)
    if ny.weekday() == 6:
        out.add(ny + timedelta(days=1))
    elif ny.weekday() != 5:  # NYSE does not close the Friday before when Jan 1 is a Saturday
        out.add(ny)
    out.add(_nth_weekday(y, 1, 0, 3))            # Martin Luther King Jr. Day
    out.add(_nth_weekday(y, 2, 0, 3))            # Washington's Birthday
    out.add(_easter(y) - timedelta(days=2))      # Good Friday
    out.add(_last_weekday(y, 5, 0))              # Memorial Day
    if y >= 2022:
        out.add(_observed(date(y, 6, 19)))       # Juneteenth
    out.add(_observed(date(y, 7, 4)))            # Independence Day
    out.add(_nth_weekday(y, 9, 0, 1))            # Labor Day
    out.add(_nth_weekday(y, 11, 3, 4))           # Thanksgiving
    out.add(_observed(date(y, 12, 25)))          # Christmas
    return out


class Calendar:
    def __init__(self, kis=None, krx_extra: list[str] | None = None, us_extra: list[str] | None = None):
        self.kis = kis
        self.krx_extra = {date.fromisoformat(d) for d in (krx_extra or [])}
        self.us_extra = {date.fromisoformat(d) for d in (us_extra or [])}
        self._krx: dict[date, bool] = {}
        self._krx_fetched: date | None = None
        self._lock = threading.Lock()

    def is_trading_day(self, market: str, d: date) -> bool:
        if market in ("upbit", "binance"):
            return True
        if d.weekday() >= 5:
            return False
        if market == "kis_us":
            return d not in nyse_holidays(d.year) and d not in self.us_extra
        if d in self.krx_extra:
            return False
        return self._krx_open(d)

    def _krx_open(self, d: date) -> bool:
        with self._lock:
            if self.kis is not None and self._krx_fetched != date.today():
                try:
                    self._krx.update(self.kis.kr_holidays(date.today()))
                except Exception as e:
                    log.warning("KRX holiday lookup failed (%s) — assuming weekdays are open", e)
                self._krx_fetched = date.today()  # at most one attempt per day
            return self._krx.get(d, True)
