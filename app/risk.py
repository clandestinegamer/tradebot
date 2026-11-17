"""Risk rules. Every buy must pass here; sells are never blocked."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskCfg


@dataclass
class BuyPlan:
    ok: bool
    qty: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    reason: str = ""
    atr: float = 0.0


def round_qty(qty: float, market: str) -> float:
    if market in ("upbit", "binance"):
        return math.floor(qty * 1e8) / 1e8  # crypto: 8 decimals (Binance trims to its step size when ordering)
    return float(math.floor(qty))           # stocks: whole shares


class RiskManager:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg

    def check_account(self, n_open: int, trades_today: int, day_pnl_pct: float,
                      group_open: int = 0, max_per_group: int = 0) -> str | None:
        c = self.cfg
        if day_pnl_pct <= -abs(c.daily_loss_limit_pct):
            return f"daily loss limit hit ({day_pnl_pct:.2f}% ≤ -{c.daily_loss_limit_pct}%)"
        if trades_today >= c.max_trades_per_day:
            return f"max trades per day reached ({trades_today})"
        if n_open >= c.max_open_positions:
            return f"max open positions reached ({n_open})"
        if max_per_group and group_open >= max_per_group:
            return f"already holding {group_open} closely-related position(s) (correlation limit)"
        return None

    def plan_buy(
        self,
        *,
        market: str,
        price: float,       # native currency
        atr: float,         # native currency
        fx: float,          # native → KRW (1 for KRW assets)
        fee_rate: float,
        equity_krw: float,
        cash_krw: float,
        n_open: int,
        trades_today: int,
        day_pnl_pct: float,
        group_open: int = 0,
        max_per_group: int = 0,
        open_risk_krw: float = 0.0,   # what the account loses if every open stop is hit
        risk_scale: float = 1.0,      # extra shrink (VIX caution, weekend crypto…)
    ) -> BuyPlan:
        c = self.cfg
        blocked = self.check_account(n_open, trades_today, day_pnl_pct, group_open, max_per_group)
        if blocked:
            return BuyPlan(False, reason=blocked)
        if price <= 0 or not math.isfinite(atr) or atr <= 0:
            return BuyPlan(False, reason="no valid price/volatility")

        stop_dist = c.stop_loss_atr * atr
        stop = price - stop_dist
        if stop <= 0:
            return BuyPlan(False, reason="stop-loss would be below zero (volatility too high)")
        target = price + c.take_profit_atr * atr if c.take_profit_atr > 0 else 0.0

        unit_krw = price * fx
        risk_pct = c.risk_per_trade_pct * float(c.market_risk_mult.get(market, 1.0)) * risk_scale
        qty_risk = (equity_krw * risk_pct / 100) / (stop_dist * fx)
        if c.max_total_risk_pct > 0:  # total risk cap ("portfolio heat")
            room = equity_krw * c.max_total_risk_pct / 100 - open_risk_krw
            if room <= 0:
                return BuyPlan(False, reason=f"total open risk already at the {c.max_total_risk_pct:g}% cap")
            qty_risk = min(qty_risk, room / (stop_dist * fx))
        qty_cap = (equity_krw * c.max_position_pct / 100) / unit_krw
        qty_cash = (cash_krw * 0.995) / (unit_krw * (1 + fee_rate))
        qty = round_qty(max(0.0, min(qty_risk, qty_cap, qty_cash)), market)

        value = qty * unit_krw
        if qty <= 0 or value < c.min_order_krw:
            limiter = min(
                (qty_risk, "risk-per-trade / total-risk cap"), (qty_cap, "max position size"), (qty_cash, "available cash")
            )[1]
            return BuyPlan(False, reason=f"order too small (limited by {limiter}; ₩{value:,.0f})")
        return BuyPlan(True, qty, stop, target, f"size {qty:g} ≈ ₩{value:,.0f}")
