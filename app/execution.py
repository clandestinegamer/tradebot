"""Order execution: PaperExecutor (simulated) and LiveExecutor (real Upbit / KIS orders)."""
from __future__ import annotations

import time

from .brokers.kis import KISClient
from .brokers.upbit import UpbitClient
from .config import Config, SymbolCfg
from .models import Fill
from .storage import Store

SLIPPAGE = 0.0005  # paper fills assume 0.05% worse than the quoted price


class PaperExecutor:
    """Simulated account in KRW. Uses live (or demo) prices; never touches a broker."""

    mode = "paper"

    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        if store.get("paper_cash") is None:
            store.set("paper_cash", cfg.starting_cash_krw)
            store.set("start_equity", cfg.starting_cash_krw)

    def cash_krw(self, market: str | None = None) -> float:
        return float(self.store.get("paper_cash", 0.0))

    def total_cash_krw(self) -> float:
        return self.cash_krw()

    def buy(self, s: SymbolCfg, qty: float, price: float, fx: float) -> Fill:
        px = price * (1 + SLIPPAGE)
        value = qty * px * fx
        fee = value * self.cfg.fee_rate(s.market)
        cash = self.cash_krw()
        if value + fee > cash:
            return Fill(False, message="not enough paper cash")
        self.store.set("paper_cash", cash - value - fee)
        return Fill(True, qty, px, fee, order_id="paper")

    def sell(self, s: SymbolCfg, qty: float, price: float, fx: float) -> Fill:
        px = price * (1 - SLIPPAGE)
        value = qty * px * fx
        fee = value * self.cfg.fee_rate(s.market)
        self.store.set("paper_cash", self.cash_krw() + value - fee)
        return Fill(True, qty, px, fee, order_id="paper")


class LiveExecutor:
    """Places real orders. Fills are confirmed from the broker before the bot records them."""

    mode = "live"

    def __init__(self, cfg: Config, upbit: UpbitClient | None, kis: KISClient | None):
        self.cfg, self.upbit, self.kis = cfg, upbit, kis

    def cash_krw(self, market: str | None = None) -> float:
        if market == "upbit":
            return self.upbit.krw_available() if self.upbit else 0.0
        if market in ("kis_kr", "kis_us"):
            # kis_us assumes 통합증거금 (integrated margin) so KRW can fund USD buys
            return self.kis.kr_balance()[1] if self.kis else 0.0
        return self.total_cash_krw()

    def total_cash_krw(self) -> float:
        total = 0.0
        if self.upbit and self.upbit.access_key:
            total += self.upbit.krw_available()
        if self.kis and self.kis.app_key:
            total += self.kis.kr_balance()[1]
        return total

    # ── helpers ──
    def _kis_qty(self, s: SymbolCfg) -> tuple[float, float]:
        if s.market == "kis_kr":
            return self.kis.kr_balance()[0].get(s.symbol, (0.0, 0.0))
        return self.kis.us_holdings(s.exchange).get(s.symbol, (0.0, 0.0))

    def _us_chase(self, s: SymbolCfg, side: str, qty: int, price: float, before: float) -> tuple[float, str]:
        """Place a US limit order; if it hasn't filled, re-price it step by step, then cancel what's left.
        Returns (filled quantity, order number)."""
        ex = self.cfg.execution
        sign = 1 if side == "buy" else -1
        limit = round(price * (1 + sign * 0.005), 2)
        o = self.kis.us_order(side, s.symbol, s.exchange, qty, limit)
        oid = (o.get("output") or {}).get("ODNO", "")
        filled, _ = self._kis_wait(s, before, side)
        for attempt in range(ex.us_retries):
            if filled >= qty or not oid:
                break
            limit = round(limit * (1 + sign * ex.us_step_pct / 100), 2)
            try:
                self.kis.us_modify(oid, s.symbol, s.exchange, int(qty - filled), limit)
            except Exception as err:  # e.g. it filled in the meantime
                self.last_note = f"re-price failed: {err}"
                break
            more, _ = self._kis_wait(s, before, side)
            filled = max(filled, more)
        if filled < qty and oid:
            try:
                self.kis.us_cancel(oid, s.symbol, s.exchange, int(qty - filled))
            except Exception as err:
                self.last_note = f"cancel failed — CHECK THE KIS APP: {err}"
            filled = max(filled, self._kis_qty(s)[0] - before if side == "buy" else before - self._kis_qty(s)[0])
        return max(0.0, filled), oid

    last_note = ""

    def _kis_wait(self, s: SymbolCfg, before: float, side: str, timeout: float = 20) -> tuple[float, float]:
        deadline = time.time() + timeout
        qty, avg = before, 0.0
        while time.time() < deadline:
            time.sleep(2)
            qty, avg = self._kis_qty(s)
            if (side == "buy" and qty > before) or (side == "sell" and qty < before):
                break
        return abs(qty - before), avg

    # ── orders ──
    def buy(self, s: SymbolCfg, qty: float, price: float, fx: float) -> Fill:
        fee_rate = self.cfg.fee_rate(s.market)
        if s.market == "upbit":
            o = self.upbit.buy_market_krw(s.symbol, qty * price)
            f = self.upbit.wait_fill(o["uuid"])
            if f["qty"] <= 0:
                return Fill(False, order_id=o["uuid"], message=f"order {f['state']} with no fill")
            return Fill(True, f["qty"], f["avg_price"], f["fee"], o["uuid"])

        before, _ = self._kis_qty(s)
        self.last_note = ""
        if s.market == "kis_kr":
            o = self.kis.kr_order("buy", s.symbol, int(qty), self.cfg.execution.kr_order_type)
            oid = (o.get("output") or {}).get("ODNO", "")
            filled, avg = self._kis_wait(s, before, "buy")
        else:  # marketable limit, re-priced if it doesn't fill, remainder cancelled
            filled, oid = self._us_chase(s, "buy", int(qty), price, before)
            avg = 0.0
        if filled <= 0:
            return Fill(False, order_id=oid, message="order didn't fill and was cancelled" +
                        (f" ({self.last_note})" if self.last_note else ""))
        px = price if s.market == "kis_us" else (avg or price)
        return Fill(True, filled, px, filled * px * fx * fee_rate, oid)

    def sell(self, s: SymbolCfg, qty: float, price: float, fx: float) -> Fill:
        fee_rate = self.cfg.fee_rate(s.market)
        if s.market == "upbit":
            o = self.upbit.sell_market(s.symbol, qty)
            f = self.upbit.wait_fill(o["uuid"])
            if f["qty"] <= 0:
                return Fill(False, order_id=o["uuid"], message=f"order {f['state']} with no fill")
            return Fill(True, f["qty"], f["avg_price"], f["fee"], o["uuid"])

        before, _ = self._kis_qty(s)
        qty = min(int(qty), int(before))
        if qty <= 0:
            return Fill(False, message="broker shows no shares to sell")
        self.last_note = ""
        if s.market == "kis_kr":
            o = self.kis.kr_order("sell", s.symbol, qty, self.cfg.execution.kr_order_type)
            oid = (o.get("output") or {}).get("ODNO", "")
            filled, _ = self._kis_wait(s, before, "sell")
        else:
            filled, oid = self._us_chase(s, "sell", qty, price, before)
        if filled <= 0:
            return Fill(False, order_id=oid, message="sell didn't fill and was cancelled — the bot will retry next check" +
                        (f" ({self.last_note})" if self.last_note else ""))
        return Fill(True, filled, price, filled * price * fx * fee_rate, oid)
