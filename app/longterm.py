"""Long-term holdings, managed separately from the swing trades:

• Core (dual momentum) — once a month hold whichever is strongest over 12 months: US stocks (SPY),
  international stocks (VEU) — or bonds (AGG) when US stocks aren't beating cash. Slow and steady.
• Smart DCA — buy a fixed amount on a schedule, more when the price has dipped.
• Grid — for a crypto that's moving sideways: buy at each step down, sell one step up.

These positions have no stop-losses (they're meant to be held) and use their own position keys
("core:…", "dca:…", "grid:…") so the swing-trading rules never touch them. Pause stops their buys too;
the kill switch sells them too.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .config import SymbolCfg
from .indicators import rsi, sma
from .models import Position
from .risk import round_qty
from .storage import now_iso

log = logging.getLogger("tradebot")
SEOUL = ZoneInfo("Asia/Seoul")
PREFIXES = ("core:", "dca:", "grid:", "park:")


def is_longterm(key: str) -> bool:
    return key.startswith(PREFIXES)


def split_key(key: str) -> tuple[str, str, str]:
    kind, market, symbol = key.split(":", 2)
    return kind, market, symbol


class LongTerm:
    def __init__(self, engine):
        self.e = engine

    @property
    def cfg(self):
        return self.e.cfg

    # ── shared order helpers ──
    def symcfg(self, market: str, symbol: str, exchange: str = "NASD") -> SymbolCfg:
        return SymbolCfg(symbol, market, "day", "trend_rsi", exchange)

    def symcfg_for(self, key: str) -> SymbolCfg | None:
        if not is_longterm(key):
            return None
        kind, market, symbol = split_key(key)
        exch = "NASD"
        if kind == "core":
            exch = self.cfg.core.exchange
        for d in self.cfg.dca:
            if d["symbol"] == symbol and d["market"] == market:
                exch = d.get("exchange", exch)
        return self.symcfg(market, symbol, exch)

    def _buy(self, key: str, s: SymbolCfg, krw: float, reason: str) -> bool:
        if self.e.paused:
            return False
        price = self.e.data.price(s)
        fx = self.e.data.fx(s)
        qty = round_qty(krw / (price * fx), s.market)
        if qty <= 0 or qty * price * fx < self.cfg.risk.min_order_krw:
            self.e.store.event(f"{s.symbol}: {reason} skipped — ₩{krw:,.0f} is below one unit / the minimum order")
            return False
        _, cash = self.e.capped(self.e.equity_krw(), self.e.ex.cash_krw(s.market))
        if qty * price * fx > cash * 0.995:
            self.e.store.event(f"{s.symbol}: {reason} skipped — not enough cash", "warn")
            return False
        fill = self.e.ex.buy(s, qty, price, fx)
        if not fill.ok:
            self.e.store.event(f"{s.symbol}: {reason} buy failed — {fill.message}", "error")
            return False
        p = self.e.store.position(key)
        if p:
            total = p.qty + fill.qty
            p.avg_price = (p.qty * p.avg_price + fill.qty * fill.price) / total
            p.qty = total
        else:
            p = Position(key, s.symbol, s.market, fill.qty, fill.price, 0.0, 0.0, now_iso(), "", reason)
        self.e.store.save_position(p)
        self.e.last_prices[key] = price
        self.e.store.add_trade(key=key, symbol=s.symbol, market=s.market, side="BUY", qty=fill.qty, price=fill.price,
                               value_krw=fill.qty * fill.price * fx, fee_krw=fill.fee_krw, pnl_krw=None, reason=reason,
                               ai_decision=None, ai_reason=None, mode=self.e.ex.mode, order_id=fill.order_id,
                               expected_price=price, slippage_pct=(fill.price / price - 1) * 100,
                               fx_usdkrw=self.e._usdkrw(), opened_at=p.opened_at)
        self.e.store.event(f"{key.split(':')[0].upper()} BUY {s.symbol} {fill.qty:g} — {reason}")
        self.e.notify(f"🟦 {reason}: bought {fill.qty:g} {s.symbol} ≈ ₩{fill.qty * fill.price * fx:,.0f}"
                      + (" [paper]" if self.e.ex.mode == "paper" else ""))
        return True

    def _sell(self, key: str, s: SymbolCfg, qty: float, reason: str) -> float:
        p = self.e.store.position(key)
        if not p or qty <= 0:
            return 0.0
        qty = min(qty, p.qty)
        price = self.e.data.price(s)
        fx = self.e.data.fx(s)
        fill = self.e.ex.sell(s, qty, price, fx)
        if not fill.ok:
            self.e.store.event(f"{s.symbol}: {reason} sell failed — {fill.message}", "error")
            self.e.notify(f"🚨 {s.symbol} sell failed ({reason}): {fill.message}", key=f"ltsell:{key}")
            return 0.0
        cost = fill.qty * p.avg_price * fx
        pnl = fill.qty * fill.price * fx - cost - fill.fee_krw
        p.qty -= fill.qty
        if p.qty <= 1e-9 or (s.market == "upbit" and p.qty * price * fx < 1000):
            self.e.store.delete_position(key)
        else:
            self.e.store.save_position(p)
        self.e.store.add_trade(key=key, symbol=s.symbol, market=s.market, side="SELL", qty=fill.qty, price=fill.price,
                               value_krw=fill.qty * fill.price * fx, fee_krw=fill.fee_krw, pnl_krw=pnl, reason=reason,
                               ai_decision=None, ai_reason=None, mode=self.e.ex.mode, order_id=fill.order_id,
                               expected_price=price, slippage_pct=(1 - fill.price / price) * 100,
                               fx_usdkrw=self.e._usdkrw(), cost_basis_krw=cost, opened_at=p.opened_at)
        self.e.store.event(f"{key.split(':')[0].upper()} SELL {s.symbol} {fill.qty:g} — P&L ₩{pnl:,.0f} ({reason})")
        self.e.notify(f"🟪 {reason}: sold {fill.qty:g} {s.symbol}, P&L ₩{pnl:,.0f}"
                      + (" [paper]" if self.e.ex.mode == "paper" else ""))
        return fill.qty * fill.price * fx

    def refresh_prices(self) -> None:
        for p in self.e.store.positions():
            if is_longterm(p.key):
                s = self.symcfg_for(p.key)
                try:
                    if self.e.data.is_open(s):
                        self.e.last_prices[p.key] = self.e.data.price(s)
                except Exception:
                    pass

    def tick(self) -> None:
        for name, fn in (("core", self.core), ("dca", self.dca), ("grid", self.grid), ("parking", self.park)):
            try:
                fn()
            except Exception as ex:
                self.e.store.event(f"{name}: {type(ex).__name__}: {ex}", "error")
                self.e.notify(f"⚠️ {name}: {ex}", key=f"lterr:{name}")
        self.refresh_prices()

    # ── core: dual momentum ──
    def core_choice(self) -> tuple[str, dict]:
        c = self.cfg.core
        rets = {}
        for sym in (c.us, c.intl, c.bonds):
            closes = self.e.data.daily_closes(self.symcfg("kis_us", sym, c.exchange), c.lookback_days + 1)
            if len(closes) < c.lookback_days + 1:
                raise ValueError(f"not enough history for {sym} ({len(closes)} days)")
            rets[sym] = float(closes.iloc[-1] / closes.iloc[0] - 1) * 100
        cash = c.cash_return_pct * c.lookback_days / 252
        if rets[c.us] > cash:
            pick = c.us if rets[c.us] >= rets[c.intl] else c.intl
        else:
            pick = c.bonds
        return pick, rets

    def core(self) -> None:
        c = self.cfg.core
        if not c.enabled:
            return
        probe = self.symcfg("kis_us", c.us, c.exchange)
        if not self.e.data.is_open(probe):
            return
        month = datetime.now(SEOUL).strftime("%Y-%m")
        if self.e.store.get("core_month") == month:
            return
        pick, rets = self.core_choice()
        held = [p for p in self.e.store.positions() if p.key.startswith("core:")]
        for p in held:
            if p.symbol != pick:
                self._sell(p.key, self.symcfg("kis_us", p.symbol, c.exchange), p.qty,
                           f"Core switch {p.symbol} → {pick}")
        equity, _ = self.e.capped(self.e.equity_krw(), 0.0)
        target = equity * c.pct / 100
        key = f"core:kis_us:{pick}"
        s = self.symcfg("kis_us", pick, c.exchange)
        pos = self.e.store.position(key)
        current = pos.qty * self.e.data.price(s) * self.e.data.fx(s) if pos else 0.0
        gap = target - current
        if abs(gap) > target * c.rebalance_band_pct / 100:
            if gap > 0:
                self._buy(key, s, gap, f"Core (dual momentum) → {pick}")
            else:
                self._sell(key, s, abs(gap) / (self.e.data.price(s) * self.e.data.fx(s)), "Core rebalance")
        self.e.store.set("core_month", month)
        self.e.store.set("core_last", {"date": datetime.now(SEOUL).date().isoformat(), "pick": pick,
                                       "returns_12m": {k: round(v, 1) for k, v in rets.items()}})
        self.e.store.event(f"core check: {pick} (12-month returns {', '.join(f'{k} {v:+.1f}%' for k, v in rets.items())})")

    # ── smart DCA ──
    def _dca_due(self, d: dict, now: datetime) -> str | None:
        """Period id if this plan is due and hasn't run this period."""
        if d["every"] == "day":
            period = now.date().isoformat()
        elif d["every"] == "week":
            if now.weekday() < int(d.get("weekday", 0)):
                return None
            period = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        else:
            if now.day < int(d.get("day", 1)):
                return None
            period = now.strftime("%Y-%m")
        last = self.e.store.get(f"dca_last:{d['market']}:{d['symbol']}")
        return None if last == period else period

    def dca_multiplier(self, closes) -> tuple[float, str]:
        if len(closes) < 30:
            return 1.0, "normal amount"
        r = float(rsi(closes, 14).iloc[-1])
        ma = sma(closes, 200).iloc[-1] if len(closes) >= 200 else math.nan
        mult, why = 1.0, []
        if r < 30:
            mult *= 2.0
            why.append(f"RSI {r:.0f} (oversold) ×2")
        if not math.isnan(ma) and closes.iloc[-1] < ma:
            mult *= 1.5
            why.append("below 200-day average ×1.5")
        return min(mult, 3.0), ", ".join(why) or "normal amount"

    def dca(self) -> None:
        now = datetime.now(SEOUL)
        for d in self.cfg.dca:
            s = self.symcfg(d["market"], d["symbol"], d.get("exchange", "NASD"))
            if not self.e.data.is_open(s):
                continue
            period = self._dca_due(d, now)
            if not period:
                continue
            closes = self.e.data.daily_closes(s, 250)
            mult, why = self.dca_multiplier(closes) if d.get("dip_boost", True) else (1.0, "normal amount")
            ok = self._buy(f"dca:{d['market']}:{d['symbol']}", s, float(d["amount_krw"]) * mult,
                           f"DCA {({'day': 'daily', 'week': 'weekly', 'month': 'monthly'})[d['every']]} ({why})")
            if ok or not self.e.paused:
                self.e.store.set(f"dca_last:{d['market']}:{d['symbol']}", period)

    # ── idle cash parking ──
    def park(self) -> None:
        pc = self.cfg.parking
        if not pc.enabled:
            return
        s = self.symcfg("kis_kr", pc.symbol)
        if not self.e.data.is_open(s):
            return
        key = f"park:kis_kr:{pc.symbol}"
        price = self.e.data.price(s)
        equity, cash = self.e.capped(self.e.equity_krw(), self.e.ex.cash_krw("kis_kr"))
        keep = equity * pc.keep_cash_pct / 100
        pos = self.e.store.position(key)
        if cash < keep * 0.5 and pos:      # running low on cash → release some
            need = keep - cash
            self._sell(key, s, min(pos.qty, math.ceil(need / price)), "Parking: releasing cash")
        elif cash - keep > price * 1.01:   # spare cash → park whole shares
            self._buy(key, s, (cash - keep) // price * price, "Parking idle cash (money-market ETF)")

    # ── grid ──
    @staticmethod
    def grid_lines(g: dict) -> list[float]:
        return list(np.linspace(float(g["lower"]), float(g["upper"]), int(g["levels"]) + 1))

    def grid(self) -> None:
        for g in self.cfg.grid:
            sym = g["symbol"]
            s = self.symcfg("upbit", sym)
            key = f"grid:upbit:{sym}"
            st_key = f"grid_state:{sym}"
            st = self.e.store.get(st_key) or {"lots": {}, "stopped": False, "above_alerted": False}
            if st.get("stopped"):
                continue
            price = self.e.data.price(s)
            lines = self.grid_lines(g)
            stop_at = lines[0] * (1 - float(g.get("stop_below_pct", 5)) / 100)
            if price < stop_at:
                pos = self.e.store.position(key)
                if pos:
                    self._sell(key, s, pos.qty, f"Grid {sym} stopped: price fell below {stop_at:,.0f}")
                st.update(lots={}, stopped=True)
                self.e.store.set(st_key, st)
                self.e.notify(f"🛑 Grid {sym} stopped — price {price:,.0f} broke below the range. Set a new range to restart.")
                continue
            per_lot = float(g["budget_krw"]) / int(g["levels"])
            # sell lots whose next line up has been reached
            for i in sorted(st["lots"], key=int):
                i_int = int(i)
                if price >= lines[i_int + 1]:
                    lot = st["lots"][i]
                    if self._sell(key, s, lot["qty"], f"Grid {sym} take step {i_int + 1}/{len(lines) - 1}"):
                        del st["lots"][i]
            # buy the step the price has moved DOWN into (or the starting step), if it's empty
            prev = st.get("last_price")
            band = lambda p: max(i for i in range(len(lines) - 1) if lines[i] <= p) if p >= lines[0] else -1
            for i in range(len(lines) - 1):
                moved_down = prev is None or band(prev) > i
                if lines[i] <= price < lines[i + 1] and str(i) not in st["lots"] and moved_down:
                    before = (self.e.store.position(key).qty if self.e.store.position(key) else 0.0)
                    if self._buy(key, s, per_lot, f"Grid {sym} buy step {i + 1}/{len(lines) - 1}"):
                        after = self.e.store.position(key).qty
                        st["lots"][str(i)] = {"qty": after - before, "price": price}
                    break
            if price >= lines[-1] and not st.get("above_alerted"):
                st["above_alerted"] = True
                self.e.notify(f"ℹ️ Grid {sym}: price {price:,.0f} is above the range — nothing to do until it comes back.")
            elif price < lines[-1]:
                st["above_alerted"] = False
            st["last_price"] = price
            self.e.store.set(st_key, st)
