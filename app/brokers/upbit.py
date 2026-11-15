"""Upbit REST client (crypto). Docs: https://docs.upbit.com"""
from __future__ import annotations

import hashlib
import time
import uuid
from urllib.parse import unquote, urlencode

import jwt
import pandas as pd
import requests

BASE = "https://api.upbit.com/v1"
INTERVAL_PATHS = {"1m": "minutes/1", "5m": "minutes/5", "15m": "minutes/15", "30m": "minutes/30",
                  "60m": "minutes/60", "240m": "minutes/240", "day": "days", "week": "weeks"}


class UpbitError(RuntimeError):
    pass


class UpbitClient:
    def __init__(self, access_key: str = "", secret_key: str = "", session: requests.Session | None = None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.s = session or requests.Session()

    # ── auth ──
    def _auth_header(self, params: dict | None = None) -> dict:
        if not (self.access_key and self.secret_key):
            raise UpbitError("Upbit API keys are not set (UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY)")
        payload = {"access_key": self.access_key, "nonce": str(uuid.uuid4())}
        if params:
            q = unquote(urlencode(params, doseq=True)).encode()
            payload["query_hash"] = hashlib.sha512(q).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return {"Authorization": f"Bearer {jwt.encode(payload, self.secret_key, algorithm='HS256')}"}

    def _req(self, method: str, path: str, params: dict | None = None, auth: bool = False):
        headers = self._auth_header(params) if auth else {}
        url = f"{BASE}{path}"
        if method == "GET":
            r = self.s.get(url, params=params, headers=headers, timeout=10)
        else:
            r = self.s.request(method, url, json=params, headers=headers, timeout=10)
        if r.status_code == 429:
            time.sleep(1)
            raise UpbitError("rate limited (429)")
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                raise UpbitError(f"{err.get('name')}: {err.get('message')}")
            except ValueError:
                raise UpbitError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # ── market data (no keys needed) ──
    def candles(self, market: str, interval: str = "60m", count: int = 200) -> pd.DataFrame:
        """Up to `count` candles, oldest → newest. Pages back 200 at a time (Upbit's per-call limit)."""
        path = INTERVAL_PATHS.get(interval)
        if not path:
            raise UpbitError(f"unsupported interval {interval}")
        rows: list[dict] = []
        to = None
        while len(rows) < count:
            params = {"market": market, "count": min(200, count - len(rows))}
            if to:
                params["to"] = to
            page = self._req("GET", f"/candles/{path}", params)
            if not page:
                break
            rows += page
            to = page[-1]["candle_date_time_utc"] + "Z"  # next page: candles before the oldest one so far
            if len(page) < params["count"]:
                break
            time.sleep(0.12)  # stay well under the public rate limit
        df = pd.DataFrame(
            {
                "ts": [r["candle_date_time_kst"] for r in rows],
                "open": [float(r["opening_price"]) for r in rows],
                "high": [float(r["high_price"]) for r in rows],
                "low": [float(r["low_price"]) for r in rows],
                "close": [float(r["trade_price"]) for r in rows],
                "volume": [float(r["candle_acc_trade_volume"]) for r in rows],
            }
        )
        df = df.drop_duplicates("ts").iloc[::-1].set_index("ts")
        return df  # last row is the forming candle

    def price(self, market: str) -> float:
        return self.ticker(market)[0]

    def ticker(self, market: str) -> tuple[float, float]:
        """(last price, age of that trade in seconds)."""
        r = self._req("GET", "/ticker", {"markets": market})[0]
        return float(r["trade_price"]), max(0.0, time.time() - float(r.get("trade_timestamp", 0)) / 1000)

    def quote(self, market: str) -> tuple[float, float]:
        """(best bid, best ask)."""
        u = self._req("GET", "/orderbook", {"markets": market})[0]["orderbook_units"][0]
        return float(u["bid_price"]), float(u["ask_price"])

    # ── account / orders (keys needed) ──
    def accounts(self) -> list[dict]:
        return self._req("GET", "/accounts", auth=True)

    def krw_available(self) -> float:
        for a in self.accounts():
            if a["currency"] == "KRW":
                return float(a["balance"])  # "balance" excludes "locked" (funds in open orders)
        return 0.0

    def buy_market_krw(self, market: str, krw: float) -> dict:
        params = {"market": market, "side": "bid", "ord_type": "price", "price": str(int(krw))}
        return self._req("POST", "/orders", params, auth=True)

    def sell_market(self, market: str, volume: float) -> dict:
        params = {"market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"}
        return self._req("POST", "/orders", params, auth=True)

    def order(self, order_uuid: str) -> dict:
        return self._req("GET", "/order", {"uuid": order_uuid}, auth=True)

    def wait_fill(self, order_uuid: str, timeout: float = 15) -> dict:
        """Poll until the order is finished. Returns qty, avg price and fee (KRW)."""
        deadline = time.time() + timeout
        o = {}
        while time.time() < deadline:
            o = self.order(order_uuid)
            # market buys by KRW amount often end as "cancel" with leftover dust — the fills are still in "trades"
            if o.get("state") in ("done", "cancel"):
                break
            time.sleep(0.7)
        trades = o.get("trades") or []
        qty = sum(float(t["volume"]) for t in trades)
        funds = sum(float(t["funds"]) for t in trades)
        return {
            "state": o.get("state"),
            "qty": qty,
            "avg_price": funds / qty if qty else 0.0,
            "fee": float(o.get("paid_fee") or 0),
        }
