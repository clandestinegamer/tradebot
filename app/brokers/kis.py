"""Korea Investment & Securities (KIS Developers) REST client — Korean and US stocks.

Endpoints and TR IDs follow the official samples at
https://github.com/koreainvestment/open-trading-api (examples_llm/).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

SERVERS = {
    "real": "https://openapi.koreainvestment.com:9443",
    "demo": "https://openapivts.koreainvestment.com:29443",  # 모의투자 (mock trading)
}
# order-exchange code → quote-exchange code
QUOTE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


class KISError(RuntimeError):
    pass


class KISClient:
    def __init__(self, app_key: str, app_secret: str, account_no: str, account_product: str = "01",
                 env: str = "demo", token_cache: str | None = None, session: requests.Session | None = None):
        if env not in SERVERS:
            raise KISError("KIS_ENV must be 'demo' or 'real'")
        self.app_key, self.app_secret = app_key, app_secret
        self.cano, self.prod = account_no, account_product
        self.env = env
        self.base = SERVERS[env]
        self.s = session or requests.Session()
        self.token_cache = Path(token_cache or Path(__file__).resolve().parents[2] / "data" / f".kis_token_{env}.json")
        self._token: str | None = None
        self._token_exp = datetime.min
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_gap = 0.55 if env == "demo" else 0.06  # mock server allows ~2 calls/sec

    # ── auth ──
    def _get_token(self) -> str:
        if self._token and datetime.now() < self._token_exp:
            return self._token
        if self.token_cache.exists():
            try:
                c = json.loads(self.token_cache.read_text())
                exp = datetime.fromisoformat(c["exp"])
                if c.get("key") == self.app_key[-6:] and datetime.now() < exp:
                    self._token, self._token_exp = c["token"], exp
                    return self._token
            except (ValueError, KeyError):
                pass
        if not (self.app_key and self.app_secret):
            raise KISError("KIS keys are not set (KIS_APP_KEY / KIS_APP_SECRET)")
        r = self.s.post(f"{self.base}/oauth2/tokenP", timeout=10, json={
            "grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret})
        d = r.json()
        if "access_token" not in d:
            raise KISError(f"token request failed: {d.get('error_description') or d}")
        # tokens last ~24h and KIS limits how often you can request new ones — cache it
        self._token = d["access_token"]
        self._token_exp = datetime.now() + timedelta(seconds=int(d.get("expires_in", 86400)) - 600)
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache.write_text(json.dumps(
            {"token": self._token, "exp": self._token_exp.isoformat(), "key": self.app_key[-6:]}))
        os.chmod(self.token_cache, 0o600)
        return self._token

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _call(self, method: str, path: str, tr_id: str, params: dict) -> dict:
        with self._lock:
            gap = time.time() - self._last_call
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            self._last_call = time.time()
        url = f"{self.base}{path}"
        if method == "GET":
            r = self.s.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        else:
            r = self.s.post(url, headers=self._headers(tr_id), json=params, timeout=10)
        try:
            d = r.json()
        except ValueError:
            raise KISError(f"HTTP {r.status_code}: {r.text[:200]}")
        if d.get("rt_cd") != "0":
            raise KISError(f"{d.get('msg_cd')}: {d.get('msg1')}")
        return d

    def _tr(self, real: str, demo: str) -> str:
        return real if self.env == "real" else demo

    # ── Korean stocks ──
    def kr_daily(self, code: str, bars: int = 200) -> pd.DataFrame:
        rows, end = [], datetime.now()
        for _ in range(bars // 90 + 2):  # each call returns up to 100 rows
            start = end - timedelta(days=150)
            d = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100", {
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
            page = [r for r in d.get("output2", []) if r.get("stck_bsop_date")]
            if not page:
                break
            rows += page
            if len(rows) >= bars:
                break
            end = datetime.strptime(page[-1]["stck_bsop_date"], "%Y%m%d") - timedelta(days=1)
        df = pd.DataFrame({
            "ts": [r["stck_bsop_date"] for r in rows],
            "open": [float(r["stck_oprc"]) for r in rows], "high": [float(r["stck_hgpr"]) for r in rows],
            "low": [float(r["stck_lwpr"]) for r in rows], "close": [float(r["stck_clpr"]) for r in rows],
            "volume": [float(r["acml_vol"]) for r in rows]})
        return df.drop_duplicates("ts").sort_values("ts").set_index("ts").tail(bars)

    def kr_price(self, code: str) -> float:
        d = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                       {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        return float(d["output"]["stck_prpr"])

    def kr_quote(self, code: str) -> tuple[float, float]:
        """(best bid, best ask)."""
        d = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn", "FHKST01010200",
                       {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        o = d.get("output1") or {}
        return float(o.get("bidp1") or 0), float(o.get("askp1") or 0)

    def kr_holidays(self, start: date) -> dict[date, bool]:
        """{date: market open?} for the coming weeks. KIS asks for at most ~1 call per day."""
        d = self._call("GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                       {"BASS_DT": start.strftime("%Y%m%d"), "CTX_AREA_FK": "", "CTX_AREA_NK": ""})
        return {datetime.strptime(r["bass_dt"], "%Y%m%d").date(): r.get("opnd_yn") == "Y"
                for r in d.get("output", []) if r.get("bass_dt")}

    def kr_order(self, side: str, code: str, qty: int, order_type: str = "market") -> dict:
        """KRX order. "best_limit" = 최유리지정가 (fills at the best opposite price only — no sweeping the
        order book); the mock server only supports market orders, so demo always uses market."""
        tr = {"buy": self._tr("TTTC0012U", "VTTC0012U"), "sell": self._tr("TTTC0011U", "VTTC0011U")}[side]
        ord_dvsn = "03" if order_type == "best_limit" and self.env == "real" else "01"
        return self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr, {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "PDNO": code, "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "", "CNDT_PRIC": ""})

    def kr_balance(self) -> tuple[dict[str, tuple[float, float]], float]:
        """Returns ({code: (qty, avg_price)}, KRW deposit)."""
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", self._tr("TTTC8434R", "VTTC8434R"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        holdings = {r["pdno"]: (float(r["hldg_qty"]), float(r["pchs_avg_pric"]))
                    for r in d.get("output1", []) if float(r.get("hldg_qty") or 0) > 0}
        summary = (d.get("output2") or [{}])[0]
        return holdings, float(summary.get("dnca_tot_amt") or 0)

    # ── US stocks ──
    def us_daily(self, symbol: str, exchange: str = "NASD", bars: int = 200) -> pd.DataFrame:
        rows, bymd = [], ""
        for _ in range(bars // 90 + 2):  # up to 100 rows per call
            d = self._call("GET", "/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000", {
                "AUTH": "", "EXCD": QUOTE_EXCD.get(exchange, "NAS"), "SYMB": symbol,
                "GUBN": "0", "BYMD": bymd, "MODP": "1"})
            page = [r for r in d.get("output2", []) if r.get("xymd")]
            if not page:
                break
            rows += page
            if len(rows) >= bars:
                break
            bymd = (datetime.strptime(page[-1]["xymd"], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        df = pd.DataFrame({
            "ts": [r["xymd"] for r in rows],
            "open": [float(r["open"]) for r in rows], "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows], "close": [float(r["clos"]) for r in rows],
            "volume": [float(r["tvol"]) for r in rows]})
        return df.drop_duplicates("ts").sort_values("ts").set_index("ts").tail(bars)

    def us_price(self, symbol: str, exchange: str = "NASD") -> float:
        d = self._call("GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                       {"AUTH": "", "EXCD": QUOTE_EXCD.get(exchange, "NAS"), "SYMB": symbol})
        return float(d["output"]["last"])

    def us_quote(self, symbol: str, exchange: str = "NASD") -> tuple[float, float]:
        d = self._call("GET", "/uapi/overseas-price/v1/quotations/inquire-asking-price", "HHDFS76200100",
                       {"AUTH": "", "EXCD": QUOTE_EXCD.get(exchange, "NAS"), "SYMB": symbol})
        for block in ("output1", "output2"):
            o = d.get(block) or {}
            o = o[0] if isinstance(o, list) and o else o
            if isinstance(o, dict) and o.get("pask1"):
                return float(o.get("pbid1") or 0), float(o.get("pask1") or 0)
        return 0.0, 0.0

    def us_order(self, side: str, symbol: str, exchange: str, qty: int, limit_price: float) -> dict:
        """US orders must be limit orders (the mock server accepts only '00' limit)."""
        tr = {"buy": self._tr("TTTT1002U", "VTTT1002U"), "sell": self._tr("TTTT1006U", "VTTT1001U")}[side]
        return self._call("POST", "/uapi/overseas-stock/v1/trading/order", tr, {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": exchange, "PDNO": symbol,
            "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{limit_price:.2f}", "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "", "SLL_TYPE": "00" if side == "sell" else "", "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00"})

    def us_modify(self, order_no: str, symbol: str, exchange: str, qty: int, new_price: float) -> dict:
        """Change the price of an unfilled US order (정정, RVSE_CNCL_DVSN_CD 01)."""
        return self._call("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", self._tr("TTTT1004U", "VTTT1004U"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": exchange, "PDNO": symbol,
            "ORGN_ODNO": order_no, "RVSE_CNCL_DVSN_CD": "01", "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{new_price:.2f}", "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0"})

    def us_cancel(self, order_no: str, symbol: str, exchange: str, qty: int) -> dict:
        """Cancel the unfilled part of a US order (취소, RVSE_CNCL_DVSN_CD 02)."""
        return self._call("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", self._tr("TTTT1004U", "VTTT1004U"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": exchange, "PDNO": symbol,
            "ORGN_ODNO": order_no, "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": "0", "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0"})

    def us_unfilled(self, exchange: str = "NASD") -> dict[str, float]:
        """{order number: unfilled quantity} for open US orders (real accounts only — not on the mock server)."""
        d = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-nccs", "TTTS3018R", {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": exchange, "SORT_SQN": "DS",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        return {r["odno"]: float(r.get("nccs_qty") or 0) for r in d.get("output", []) if r.get("odno")}

    def kr_investor_flows(self, code: str, days: int = 5) -> float | None:
        """Net shares bought by foreigners + institutions over the last `days` sessions (None if unknown)."""
        d = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900",
                       {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        rows = [r for r in d.get("output", []) if r.get("stck_bsop_date")]
        rows.sort(key=lambda r: r["stck_bsop_date"], reverse=True)
        vals = []
        for r in rows[:days]:
            try:
                vals.append(float(r.get("frgn_ntby_qty") or 0) + float(r.get("orgn_ntby_qty") or 0))
            except ValueError:
                continue
        return sum(vals) if vals else None

    def us_holdings(self, exchange: str = "NASD") -> dict[str, tuple[float, float]]:
        d = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", self._tr("TTTS3012R", "VTTS3012R"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": exchange, "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        return {r["ovrs_pdno"]: (float(r["ovrs_cblc_qty"]), float(r["pchs_avg_pric"]))
                for r in d.get("output1", []) if float(r.get("ovrs_cblc_qty") or 0) > 0}
