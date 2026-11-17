"""Binance spot REST client (crypto, USDT pairs). Docs: https://developers.binance.com/docs/binance-spot-api-docs

Prices and balances are in USDT; the bot treats 1 USDT as 1 USD and converts to KRW with the
live USD/KRW rate, the same way it handles US stocks.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests

BASE = "https://api.binance.com"
SEOUL = ZoneInfo("Asia/Seoul")
INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h", "240m": "4h",
             "day": "1d", "week": "1w"}


class BinanceError(RuntimeError):
    pass


class BinanceClient:
    def __init__(self, api_key: str = "", api_secret: str = "", session: requests.Session | None = None,
                 base: str = BASE):
        self.api_key = api_key
        self.api_secret = api_secret
        self.s = session or requests.Session()
        self.base = base
        self._info: dict[str, dict] = {}

    # ── plumbing ──
    def _req(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        headers = {}
        if signed:
            if not (self.api_key and self.api_secret):
                raise BinanceError("Binance API keys are not set (BINANCE_API_KEY / BINANCE_API_SECRET)")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            q = urlencode(params)
            params["signature"] = hmac.new(self.api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        url = f"{self.base}{path}"
        if method == "GET":
            r = self.s.get(url, params=params, headers=headers, timeout=10)
        else:
            r = self.s.request(method, url, params=params, headers=headers, timeout=10)
        if r.status_code == 451:
            raise BinanceError("Binance refuses service from this location (HTTP 451)")
        if r.status_code in (418, 429):
            time.sleep(1)
            raise BinanceError(f"rate limited ({r.status_code})")
        if r.status_code >= 400:
            try:
                e = r.json()
                raise BinanceError(f"{e.get('code')}: {e.get('msg')}")
            except ValueError:
                raise BinanceError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def ping(self) -> bool:
        self._req("GET", "/api/v3/ping")
        return True

    def symbol_info(self, symbol: str) -> dict:
        """Base/quote asset and order-size rules for a pair, e.g. BTCUSDT. Cached."""
        if symbol not in self._info:
            s = self._req("GET", "/api/v3/exchangeInfo", {"symbol": symbol})["symbols"][0]
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
            if float(lot.get("stepSize", 0) or 0) == 0:
                lot = f.get("LOT_SIZE", {})
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._info[symbol] = {
                "base": s["baseAsset"], "quote": s["quoteAsset"], "status": s.get("status"),
                "step": float(lot.get("stepSize", "0.00000001")),
                "min_qty": float(lot.get("minQty", 0)),
                "min_notional": float(notional.get("minNotional", 0) or 0),
            }
        return self._info[symbol]

    def floor_qty(self, symbol: str, qty: float) -> float:
        step = self.symbol_info(symbol)["step"] or 1e-8
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(math.floor(qty / step + 1e-9) * step, decimals)

    # ── market data (no keys needed) ──
    def candles(self, symbol: str, interval: str = "60m", count: int = 200) -> pd.DataFrame:
        """Up to `count` candles, oldest → newest; the last row is the forming candle."""
        iv = INTERVALS.get(interval)
        if not iv:
            raise BinanceError(f"unsupported interval {interval}")
        rows: list[list] = []
        end = None
        while len(rows) < count:
            params = {"symbol": symbol, "interval": iv, "limit": min(1000, count - len(rows))}
            if end is not None:
                params["endTime"] = end
            page = self._req("GET", "/api/v3/klines", params)
            if not page:
                break
            rows = page + rows
            end = int(page[0][0]) - 1        # next page: candles before the oldest one so far
            if len(page) < params["limit"]:
                break
            time.sleep(0.1)
        df = pd.DataFrame({
            "ts": [datetime.fromtimestamp(r[0] / 1000, timezone.utc).astimezone(SEOUL).strftime("%Y-%m-%dT%H:%M:%S")
                   for r in rows],
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        })
        return df.drop_duplicates("ts").set_index("ts")

    def price(self, symbol: str) -> float:
        return float(self._req("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def ticker(self, symbol: str) -> tuple[float, float]:
        """(last price, age of that trade in seconds)."""
        t = self._req("GET", "/api/v3/trades", {"symbol": symbol, "limit": 1})[-1]
        return float(t["price"]), max(0.0, time.time() - float(t["time"]) / 1000)

    def quote(self, symbol: str) -> tuple[float, float]:
        """(best bid, best ask)."""
        b = self._req("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return float(b["bidPrice"]), float(b["askPrice"])

    # ── account / orders (keys needed) ──
    def balances(self) -> dict[str, float]:
        """asset → free + locked."""
        a = self._req("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        return {b["asset"]: float(b["free"]) + float(b["locked"]) for b in a.get("balances", [])}

    def free(self, asset: str = "USDT") -> float:
        a = self._req("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        for b in a.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    def buy_market_quote(self, symbol: str, quote_amount: float) -> dict:
        """Market buy spending `quote_amount` USDT."""
        o = self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "BUY", "type": "MARKET",
                                                 "quoteOrderQty": f"{quote_amount:.2f}",
                                                 "newOrderRespType": "FULL"}, signed=True)
        return self.parse_fill(symbol, o)

    def sell_market(self, symbol: str, qty: float) -> dict:
        q = self.floor_qty(symbol, qty)
        if q <= 0:
            raise BinanceError(f"{symbol}: quantity {qty} is below the minimum step")
        o = self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "SELL", "type": "MARKET",
                                                 "quantity": f"{q:.8f}".rstrip("0").rstrip("."),
                                                 "newOrderRespType": "FULL"}, signed=True)
        return self.parse_fill(symbol, o)

    def parse_fill(self, symbol: str, o: dict) -> dict:
        """qty (net of any fee taken in the coin itself), average price, fee in USDT, order id."""
        fills = o.get("fills") or []
        qty = sum(float(f["qty"]) for f in fills) or float(o.get("executedQty", 0))
        quote = sum(float(f["qty"]) * float(f["price"]) for f in fills) or float(o.get("cummulativeQuoteQty", 0))
        avg = quote / qty if qty else 0.0
        info = self.symbol_info(symbol)
        fee_usdt, fee_coin, fee_other = 0.0, 0.0, []
        for f in fills:
            c, a = float(f.get("commission", 0)), f.get("commissionAsset")
            if a == info["quote"]:
                fee_usdt += c
            elif a == info["base"]:
                fee_coin += c
                fee_usdt += c * float(f["price"])
            elif c:
                fee_other.append((a, c))      # e.g. paid in BNB; valued at the configured fee rate later
        net_qty = qty - fee_coin if o.get("side") == "BUY" else qty
        return {"state": o.get("status"), "order_id": str(o.get("orderId", "")), "qty": net_qty,
                "avg_price": avg, "fee_usdt": fee_usdt, "fee_other": fee_other, "quote": quote}
