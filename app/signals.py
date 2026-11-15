"""Market-condition filters — whole-market signals that can block (or shrink) new buys.

Signal              Data source (free, no key)                         Applies to   Action
──────────────────  ─────────────────────────────────────────────────  ───────────  ─────────────────────────
VIX ("fear gauge")  FRED VIXCLS (Cboe CSV fallback), daily close        stocks       halve size or block
Economic calendar   FOMC decisions, CPI and jobs-report dates (built in) everything    block on event days
Bitcoin ETF flows   The Commoner Bitcoin ETF Tracker by TFTC (CC BY 4.0) crypto       block after big outflows
Funding rate        Binance BTCUSDT perpetual (Bybit fallback)          crypto       block when longs are crowded
Coinbase premium    Coinbase BTC-USD vs Binance BTCUSDT                 crypto       block when US is selling
Upbit premium       Upbit KRW-BTC vs Binance × USD/KRW                  Upbit        block when Korea is overheated
Fear & Greed        Alternative.me Crypto Fear & Greed Index            crypto       block in extreme greed
KRX flows           KIS: foreign + institutional net buying (per stock)  KRX          block if they've been selling
US earnings         Finnhub earnings calendar (free key, per stock)     US stocks    block near an earnings report

Each filter steps aside (allows the trade) if its data can't be fetched, so an outage at one data
provider never freezes the bot. Every block is also tracked in shadow mode, so the Insights tab shows
whether each filter is actually helping.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("tradebot")
NY = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")

# Scheduled US events (Eastern-time dates). Sources: federalreserve.gov FOMC calendar; BLS release schedule.
FOMC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
        "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
        "2027-10-27", "2027-12-08"]
CPI = ["2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12",
       "2027-06-10", "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"]
JOBS = ["2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07", "2027-06-04",
        "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03"]
CALENDAR_ENDS = date(2027, 12, 31)

URLS = {
    "vix_fred": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
    "vix_cboe": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "etf": "https://www.tftc.io/bitcoin-etf-flows/latest.json",
    "binance_funding": "https://fapi.binance.com/fapi/v1/premiumIndex",
    "bybit_ticker": "https://api.bybit.com/v5/market/tickers",
    "binance_price": "https://api.binance.com/api/v3/ticker/price",
    "coinbase_ticker": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
    "fng": "https://api.alternative.me/fng/",
    "finnhub_earnings": "https://finnhub.io/api/v1/calendar/earnings",
}


@dataclass
class Reading:
    value: float | None
    detail: str
    state: str = "ok"          # ok | caution | unavailable | off
    action: str | None = None  # None | "block" | "halve"


@dataclass
class Decision:
    source: str
    action: str   # "block" | "halve"
    reason: str


def macro_events() -> dict[date, str]:
    out: dict[date, str] = {}
    for label, days in (("FOMC rate decision", FOMC), ("CPI inflation report", CPI), ("US jobs report", JOBS)):
        for d in days:
            day = date.fromisoformat(d)
            out[day] = f"{out[day]} + {label}" if day in out else label
    return out


def parse_etf_rows(doc) -> list[tuple[str, float]]:
    """Find (date, netFlowUsd) rows anywhere in the TFTC JSON, oldest → newest."""
    rows = []

    def walk(x):
        if isinstance(x, dict):
            if "netFlowUsd" in x:
                d = next((str(x[k]) for k in ("date", "session", "day", "tradingDay", "updatedThrough") if k in x), "")
                try:
                    rows.append((d[:10], float(x["netFlowUsd"])))
                except (TypeError, ValueError):
                    pass
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(doc)
    return sorted(set(rows))


def parse_vix_csv(text: str) -> list[float]:
    """FRED (observation_date,VIXCLS) or Cboe (DATE,OPEN,HIGH,LOW,CLOSE) → daily closes, oldest → newest."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        v = row.get("VIXCLS") or row.get("CLOSE") or row.get("Close")
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue  # FRED marks holidays with "."
    return out


class MarketSignals:
    def __init__(self, get_cfg, fx_rate=None, upbit=None, demo: bool = False, session=None, kis=None,
                 finnhub_key: str = ""):
        self._get_cfg = get_cfg if callable(get_cfg) else (lambda: get_cfg)
        self.kis, self.finnhub_key = kis, finnhub_key
        self.fx_rate = fx_rate or (lambda: 1400.0)
        self.upbit = upbit
        self.demo = demo
        self.http = session or requests.Session()
        self._cache: dict[str, tuple[float, Reading]] = {}
        self._lock = threading.Lock()

    @property
    def cfg(self):
        return self._get_cfg().signals

    # ── caching ──
    def _cached(self, name: str, ttl: float, fn) -> Reading:
        with self._lock:
            hit = self._cache.get(name)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        try:
            r = fn()
        except Exception as e:
            log.warning("signal %s unavailable: %s", name, e)
            r = Reading(None, f"data unavailable ({type(e).__name__})", "unavailable")
            ttl_store = time.time() - ttl + 600  # retry failed sources after 10 minutes
            with self._lock:
                self._cache[name] = (ttl_store, r)
            return r
        with self._lock:
            self._cache[name] = (time.time(), r)
        return r

    def _get(self, url: str, **params):
        r = self.http.get(url, params=params or None, timeout=10, headers={"User-Agent": "TradeBot/1.0"})
        r.raise_for_status()
        return r

    # ── individual signals ──
    def vix(self) -> Reading:
        if not self.cfg.vix:
            return Reading(None, "off", "off")
        return self._cached("vix", 6 * 3600 if not self.demo else 30, self._vix)

    def _vix(self) -> Reading:
        if self.demo:
            closes = [18.0, 18 + 16 * max(0.0, math.sin(time.time() / 900))]
        else:
            try:
                closes = parse_vix_csv(self._get(URLS["vix_fred"]).text)
            except Exception:
                closes = parse_vix_csv(self._get(URLS["vix_cboe"]).text)
        if len(closes) < 2:
            raise ValueError("no VIX data")
        v, prev = closes[-1], closes[-2]
        chg = (v / prev - 1) * 100 if prev else 0.0
        c = self.cfg
        if v > c.vix_max or chg > c.vix_spike_pct:
            why = f"VIX {v:.1f}" + (f" (> {c.vix_max:g})" if v > c.vix_max else f" (up {chg:.0f}% in a day)")
            return Reading(v, why, "caution", c.vix_action)
        return Reading(v, f"VIX {v:.1f} ({chg:+.0f}% on the day) — calm enough", "ok")

    def macro(self, market: str, now: datetime | None = None) -> Reading:
        if not self.cfg.macro:
            return Reading(None, "off", "off")
        now = now or datetime.now(SEOUL)
        # Korean stocks react the morning AFTER a US release, so check yesterday's US date for KRX
        day = (now.astimezone(SEOUL).date() - timedelta(days=1)) if market == "kis_kr" else now.astimezone(NY).date()
        events = macro_events()
        for d in self.cfg.macro_extra:
            events.setdefault(date.fromisoformat(d), "custom event")
        if day in events:
            return Reading(None, f"{events[day]} ({day.isoformat()} US)", "caution", "block")
        upcoming = sorted(d for d in events if d > now.astimezone(NY).date())
        if now.date() > CALENDAR_ENDS:
            return Reading(None, "built-in event dates have run out — add [signals] macro_extra", "unavailable")
        nxt = f"next: {events[upcoming[0]]} on {upcoming[0].isoformat()}" if upcoming else "no upcoming events"
        return Reading(None, f"no event today · {nxt}", "ok")

    def etf_flows(self) -> Reading:
        if not self.cfg.etf_flows:
            return Reading(None, "off", "off")
        return self._cached("etf", 3 * 3600 if not self.demo else 30, self._etf)

    def _etf(self) -> Reading:
        if self.demo:
            rows = [("d1", 2e8), ("d2", -1e8), ("d3", -7e8 * max(0.0, math.sin(time.time() / 1100)))]
        else:
            rows = parse_etf_rows(self._get(URLS["etf"]).json())
        if len(rows) < 1:
            raise ValueError("no ETF flow rows")
        last3 = sum(v for _, v in rows[-3:]) / 1e6
        detail = f"last 3 sessions {last3:+,.0f}M USD (data: The Commoner Bitcoin ETF Tracker by TFTC)"
        if last3 < -abs(self.cfg.etf_outflow_musd):
            return Reading(last3, "heavy ETF outflows: " + detail, "caution", "block")
        return Reading(last3, detail, "ok")

    def funding(self) -> Reading:
        if not self.cfg.funding:
            return Reading(None, "off", "off")
        return self._cached("funding", 600 if not self.demo else 30, self._funding)

    def _funding(self) -> Reading:
        if self.demo:
            rate = 0.01 + 0.06 * max(0.0, math.sin(time.time() / 700))
        else:
            try:
                rate = float(self._get(URLS["binance_funding"], symbol="BTCUSDT").json()["lastFundingRate"]) * 100
            except Exception:
                d = self._get(URLS["bybit_ticker"], category="linear", symbol="BTCUSDT").json()
                rate = float(d["result"]["list"][0]["fundingRate"]) * 100
        if rate > self.cfg.funding_max_pct:
            return Reading(rate, f"funding {rate:.3f}%/8h — longs are crowded", "caution", "block")
        return Reading(rate, f"funding {rate:.3f}%/8h", "ok")

    def _binance_btc(self) -> float:
        return float(self._get(URLS["binance_price"], symbol="BTCUSDT").json()["price"])

    def coinbase_premium(self) -> Reading:
        if not self.cfg.coinbase_premium:
            return Reading(None, "off", "off")
        return self._cached("cb_premium", 120 if not self.demo else 30, self._cb)

    def _cb(self) -> Reading:
        if self.demo:
            prem = 0.05 - 0.3 * max(0.0, math.sin(time.time() / 800 + 1))
        else:
            cb = float(self._get(URLS["coinbase_ticker"]).json()["price"])
            prem = (cb / self._binance_btc() - 1) * 100
        if prem < self.cfg.cb_premium_min_pct:
            return Reading(prem, f"Coinbase premium {prem:+.2f}% — US investors selling", "caution", "block")
        return Reading(prem, f"Coinbase premium {prem:+.2f}%", "ok")

    def upbit_premium(self) -> Reading:
        if not self.cfg.upbit_premium:
            return Reading(None, "off", "off")
        return self._cached("upbit_premium", 120 if not self.demo else 30, self._upbit_prem)

    def _upbit_prem(self) -> Reading:
        if self.demo:
            prem = 2.0 + 4.0 * max(0.0, math.sin(time.time() / 1300 + 2))
        else:
            krw = self.upbit.price("KRW-BTC")
            prem = (krw / (self._binance_btc() * self.fx_rate()) - 1) * 100
        if prem > self.cfg.upbit_premium_max_pct:
            return Reading(prem, f"Upbit premium {prem:+.2f}% — Korean market overheated", "caution", "block")
        return Reading(prem, f"Upbit premium {prem:+.2f}% vs global", "ok")

    def fear_greed(self) -> Reading:
        if not self.cfg.fear_greed:
            return Reading(None, "off", "off")
        return self._cached("fng", 3600 if not self.demo else 30, self._fng)

    def _fng(self) -> Reading:
        if self.demo:
            v, label = int(50 + 40 * math.sin(time.time() / 1500)), "demo"
        else:
            d = self._get(URLS["fng"], limit=1).json()["data"][0]
            v, label = int(d["value"]), d.get("value_classification", "")
        detail = f"Fear & Greed {v} ({label}) — data: Alternative.me"
        if v >= self.cfg.fng_max:
            return Reading(v, "extreme greed: " + detail, "caution", "block")
        return Reading(v, detail, "ok")

    def krx_flows(self, code: str) -> Reading:
        if not self.cfg.krx_flows:
            return Reading(None, "off", "off")
        return self._cached(f"krxflow:{code}", 3 * 3600 if not self.demo else 30, lambda: self._krx(code))

    def _krx(self, code: str) -> Reading:
        n = self.cfg.krx_flow_days
        if self.demo:
            net = 1e5 * math.sin(time.time() / 1700 + hash(code) % 7)
        else:
            if self.kis is None:
                raise ValueError("no KIS connection")
            net = self.kis.kr_investor_flows(code, n)
            if net is None:
                raise ValueError("no investor data")
        if net < 0:
            return Reading(net, f"foreigners + institutions sold {abs(net):,.0f} shares net over {n} days", "caution", "block")
        return Reading(net, f"foreigners + institutions bought {net:,.0f} shares net over {n} days", "ok")

    def earnings(self, symbol: str, today: date | None = None) -> Reading:
        if not self.cfg.earnings:
            return Reading(None, "off", "off")
        if not self.finnhub_key and not self.demo:
            return Reading(None, "set FINNHUB_API_KEY (free) to check earnings dates", "unavailable")
        return self._cached(f"earn:{symbol}", 12 * 3600 if not self.demo else 30, lambda: self._earn(symbol, today))

    def _earn(self, symbol: str, today: date | None) -> Reading:
        today = today or datetime.now(NY).date()
        until = today + timedelta(days=self.cfg.earnings_days)
        if self.demo:
            dates = []
        else:
            d = self._get(URLS["finnhub_earnings"], **{"from": today.isoformat(), "to": until.isoformat(),
                                                      "symbol": symbol, "token": self.finnhub_key}).json()
            dates = sorted(r["date"] for r in d.get("earningsCalendar", []) if r.get("symbol") == symbol and r.get("date"))
        if dates:
            return Reading(None, f"{symbol} reports earnings on {dates[0]}", "caution", "block")
        return Reading(None, f"no {symbol} earnings in the next {self.cfg.earnings_days} days", "ok")

    # ── combined ──
    def check(self, market: str, symbol: str | None = None) -> list[Decision]:
        """Filters that apply to a buy in this market right now."""
        if not self.cfg.enabled:
            return []
        out = []
        checks = [("macro", lambda: self.macro(market))]
        if market in ("kis_kr", "kis_us"):
            checks.append(("vix", self.vix))
            if symbol and market == "kis_kr":
                checks.append(("krx_flows", lambda: self.krx_flows(symbol)))
            if symbol and market == "kis_us":
                checks.append(("earnings", lambda: self.earnings(symbol)))
        else:
            checks += [("etf_flows", self.etf_flows), ("funding", self.funding),
                       ("cb_premium", self.coinbase_premium), ("upbit_premium", self.upbit_premium),
                       ("fear_greed", self.fear_greed)]
        for name, fn in checks:
            r = fn()
            if r.action:
                out.append(Decision(name, r.action, r.detail))
        return out

    def status(self) -> dict:
        if not self.cfg.enabled:
            return {"enabled": False}
        items = {"vix": self.vix(), "macro": self.macro("kis_us"), "etf_flows": self.etf_flows(),
                 "funding": self.funding(), "cb_premium": self.coinbase_premium(),
                 "upbit_premium": self.upbit_premium(), "fear_greed": self.fear_greed()}
        return {"enabled": True, **{k: {"value": None if r.value is None else round(r.value, 3),
                                        "detail": r.detail, "state": r.state, "action": r.action}
                                    for k, r in items.items()}}
