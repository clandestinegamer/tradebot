"""Routes market-data requests to Upbit, KIS or the demo generator, and knows market hours."""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .brokers.kis import KISClient
from .brokers.upbit import UpbitClient
from .config import Config, SymbolCfg
from .demo import DemoData
from .indicators import sma

SEOUL = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


SESSIONS = {"kis_kr": (SEOUL, dtime(9, 0), dtime(15, 20)),       # stop before the 15:20 closing auction
            "kis_us": (NEW_YORK, dtime(9, 30), dtime(16, 0))}


def market_open(market: str, now: datetime | None = None, calendar=None) -> bool:
    """Regular sessions only, on exchange trading days."""
    if market == "upbit":
        return True
    tz, o, c = SESSIONS[market]
    t = (now or datetime.now(tz)).astimezone(tz)
    if calendar is not None:
        if not calendar.is_trading_day(market, t.date()):
            return False
    elif t.weekday() >= 5:
        return False
    return o <= t.time() < c


def minutes_since_open(market: str, now: datetime | None = None) -> float | None:
    if market == "upbit":
        return None
    tz, o, _ = SESSIONS[market]
    t = (now or datetime.now(tz)).astimezone(tz)
    return (t - datetime.combine(t.date(), o, tz)).total_seconds() / 60


class MarketData:
    def __init__(self, cfg: Config, upbit: UpbitClient | None = None, kis: KISClient | None = None,
                 demo: DemoData | None = None, calendar=None):
        self.cfg = cfg
        self.demo = demo if cfg.data_source == "demo" else None
        if cfg.data_source == "demo" and self.demo is None:
            self.demo = DemoData(cfg.demo_bar_seconds)
        self.upbit = upbit
        self.kis = kis
        self._regime_cache: dict[str, tuple[float, bool, str]] = {}
        self._cache: dict[tuple, tuple[float, object]] = {}  # short-lived; shared by side-by-side accounts
        if calendar is None:
            from .calendars import Calendar
            calendar = Calendar(kis, cfg.holidays.krx_extra, cfg.holidays.us_extra)
        self.calendar = calendar
        self._lock = threading.Lock()

    def is_open(self, s: SymbolCfg) -> bool:
        return True if self.demo else market_open(s.market, calendar=self.calendar)

    def entry_block(self, s: SymbolCfg) -> str | None:
        """Reasons not to OPEN a position right now (exits are never blocked by these)."""
        ex = self.cfg.execution
        if not self.demo:
            m = minutes_since_open(s.market)
            if m is not None and m < ex.avoid_open_minutes:
                return f"first {ex.avoid_open_minutes} min after the open (prices are jumpy)"
            age = self.price_age(s)
            if age is not None and age > ex.stale_seconds:
                return f"last price is {age / 60:.0f} min old (stale data)"
        if ex.max_spread_pct > 0:
            sp = self.spread_pct(s)
            if sp is not None and sp > ex.max_spread_pct:
                return f"bid-ask spread {sp:.2f}% > {ex.max_spread_pct}%"
        return None

    def price_age(self, s: SymbolCfg) -> float | None:
        if self.demo:
            return 0.0
        if s.market == "upbit":
            return self.upbit.ticker(s.symbol)[1]
        return None  # KIS quotes carry no trade time; stale KIS data is caught by the candle-date check

    def spread_pct(self, s: SymbolCfg) -> float | None:
        if self.demo:
            return 0.05
        try:
            if s.market == "upbit":
                bid, ask = self.upbit.quote(s.symbol)
            elif s.market == "kis_kr":
                bid, ask = self.kis.kr_quote(s.symbol)
            else:
                bid, ask = self.kis.us_quote(s.symbol, s.exchange)
        except Exception:
            return None
        if bid <= 0 or ask <= 0:
            return None
        return (ask - bid) / ((ask + bid) / 2) * 100

    fx_rates = None  # set to an FXRates instance for live USD/KRW
    CACHE_TTL = {"price": 5.0, "candles": 20.0, "daily": 3600.0}

    def _memo(self, kind: str, key: tuple, fn):
        ttl = self.CACHE_TTL[kind] if not self.demo else min(self.CACHE_TTL[kind], 2.0)
        k = (kind, *key)
        with self._lock:
            hit = self._cache.get(k)
            if hit and _time.time() - hit[0] < ttl:
                return hit[1]
        v = fn()
        with self._lock:
            self._cache[k] = (_time.time(), v)
        return v

    def fx(self, s: SymbolCfg) -> float:
        return self.fx_market(s.market)

    def fx_market(self, market: str) -> float:
        if market != "kis_us":
            return 1.0
        return self.fx_rates.usd_krw() if self.fx_rates else self.cfg.usd_krw

    def daily_closes(self, s: SymbolCfg, n: int):
        """Completed daily closes (for momentum ranking, dual momentum, DCA dip checks). Cached 1 hour."""
        day = SymbolCfg(s.symbol, s.market, "day", s.strategy, s.exchange)
        def load():
            df, forming = self._candles_raw(day, n + 1)
            df = df.iloc[:-1] if forming else df
            return df["close"].tail(n)
        return self._memo("daily", (s.key, n), load)

    def bar_progress(self, s: SymbolCfg, bar_ts: str) -> float:
        """How far through the current candle we are (0–1). Used to pace intraday volume."""
        try:
            if self.demo:
                start = datetime.fromisoformat(bar_ts).replace(tzinfo=ZoneInfo("UTC"))
                length = self.demo.bar_seconds
            elif s.market == "upbit":
                start = datetime.fromisoformat(bar_ts).replace(tzinfo=SEOUL)
                length = 86400 if s.interval == "day" else int(s.interval.rstrip("m")) * 60
            else:  # KIS daily candle = one session
                tz, o, c = (SEOUL, dtime(9, 0), dtime(15, 30)) if s.market == "kis_kr" else (NEW_YORK, dtime(9, 30), dtime(16, 0))
                now = datetime.now(tz)
                open_dt, close_dt = (datetime.combine(now.date(), t, tz) for t in (o, c))
                return min(1.0, max(0.0, (now - open_dt) / (close_dt - open_dt)))
            return min(1.0, max(0.0, (datetime.now(start.tzinfo) - start).total_seconds() / length))
        except (ValueError, ZeroDivisionError):
            return 1.0

    def regime(self, market: str) -> tuple[bool, str]:
        """Is the wider market in an uptrend? Benchmark's last daily close vs its moving average. Cached 15 min."""
        r = self.cfg.regime
        if not r.enabled:
            return True, "regime filter off"
        ttl = self.demo.bar_seconds * 5 if self.demo else 900
        with self._lock:
            hit = self._regime_cache.get(market)
            if hit and _time.time() - hit[0] < ttl:
                return hit[1], hit[2]
        bench = getattr(r, market)
        if self.demo:
            df = self.demo.candles(f"{market}:{bench}", r.ma + 10).iloc[:-1]
        elif market == "upbit":
            df = self.upbit.candles(bench, "day", r.ma + 10).iloc[:-1]
        elif market == "kis_kr":
            df = self.kis.kr_daily(bench, r.ma + 10)
        else:
            df = self.kis.us_daily(bench, r.kis_us_exchange, r.ma + 10)
        if market != "upbit" and not self.demo and len(df):
            today = datetime.now(SEOUL if market == "kis_kr" else NEW_YORK).strftime("%Y%m%d")
            if str(df.index[-1]) == today:
                df = df.iloc[:-1]  # use the last COMPLETED day
        if len(df) < r.ma:
            ok, why = True, f"not enough {bench} history for regime check — allowing"
        else:
            close, avg = float(df["close"].iloc[-1]), float(sma(df["close"], r.ma).iloc[-1])
            ok = close > avg
            why = f"{bench} {'above' if ok else 'below'} its {r.ma}-day average ({close:,.2f} vs {avg:,.2f})"
        with self._lock:
            self._regime_cache[market] = (_time.time(), ok, why)
        return ok, why

    def candles(self, s: SymbolCfg, count: int = 200) -> tuple[pd.DataFrame, bool]:
        """Returns (candles oldest→newest, whether the last row is a still-forming candle)."""
        return self._memo("candles", (s.key, s.interval, count), lambda: self._candles_raw(s, count))

    def _candles_raw(self, s: SymbolCfg, count: int) -> tuple[pd.DataFrame, bool]:
        if self.demo:
            return self.demo.candles(s.key, count), True
        if s.market == "upbit":
            return self.upbit.candles(s.symbol, s.interval, count), True
        if s.market == "kis_kr":
            df = self.kis.kr_daily(s.symbol, count)
            today = datetime.now(SEOUL).strftime("%Y%m%d")
        else:
            df = self.kis.us_daily(s.symbol, s.exchange, count)
            today = datetime.now(NEW_YORK).strftime("%Y%m%d")
        return df, bool(len(df)) and str(df.index[-1]) == today

    def price(self, s: SymbolCfg) -> float:
        return self._memo("price", (s.key,), lambda: self._price_raw(s))

    def _price_raw(self, s: SymbolCfg) -> float:
        if self.demo:
            return self.demo.price(s.key)
        if s.market == "upbit":
            return self.upbit.price(s.symbol)
        if s.market == "kis_kr":
            return self.kis.kr_price(s.symbol)
        return self.kis.us_price(s.symbol, s.exchange)


class DataView(MarketData):
    """The same market data (clients, caches, calendar) seen through a different config —
    lets side-by-side paper accounts share every API call with the main bot."""

    def __init__(self, base: MarketData, cfg: Config):  # noqa: super().__init__ deliberately skipped
        self.__dict__.update(base.__dict__)
        self.cfg = cfg
