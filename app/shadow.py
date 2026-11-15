"""Shadow trades: what WOULD have happened to setups the bot didn't take.

Every setup blocked by Claude's veto, the market-regime filter or a strategy filter is followed with
the same stop, break-even and trailing rules as a real trade (no money involved). Results are in R
(multiples of the initial risk), so they're comparable across symbols and sizes:
  • vetoed trades that would have LOST money → the veto saved you that much
  • vetoed trades that would have WON → the veto cost you that much
After a month or two this tells you whether each filter, and Claude, is earning its keep.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .models import Position
from .storage import now_iso

SOURCES = {"ai_veto": "Claude vetoes", "regime": "Market-regime blocks", "filtered": "Strategy filters",
           "vix": "VIX (fear gauge)", "macro": "Fed / CPI / jobs days", "etf_flows": "Bitcoin ETF outflows",
           "funding": "Crowded funding rates", "cb_premium": "Coinbase premium", "upbit_premium": "Upbit premium",
           "ranking": "Momentum ranking", "fear_greed": "Extreme greed (crypto)",
           "krx_flows": "Foreign/institution selling (KRX)", "earnings": "US earnings dates"}


class ShadowTracker:
    def __init__(self, cfg, store):
        """`cfg` may be a Config or a function returning the current Config (settings can change live)."""
        self._get_cfg = cfg if callable(cfg) else (lambda: cfg)
        self.store = store

    @property
    def cfg(self):
        return self._get_cfg()

    def _on(self, source: str) -> bool:
        return self.cfg.shadow.enabled and source in self.cfg.shadow.track

    def record(self, s, source: str, sig, price: float, bar_id: str, outcome: str) -> None:
        if not self._on(source) or not sig.atr or math.isnan(sig.atr) or price <= 0:
            return
        if self.store.has_open_shadow(s.key, bar_id):
            return  # one hypothetical trade per symbol per candle
        stop = price - self.cfg.risk.stop_loss_atr * sig.atr
        if stop <= 0:
            return
        self.store.add_shadow(key=s.key, symbol=s.symbol, market=s.market, source=source, reason=sig.reason,
                              outcome=outcome, bar_id=bar_id, entry=price, stop=stop, initial_stop=stop,
                              highest=price, entry_atr=sig.atr)

    def _pos(self, sh: dict) -> Position:
        return Position(sh["key"], sh["symbol"], sh["market"], 1.0, sh["entry"], sh["stop"], 0.0, sh["created"],
                        sh["bar_id"], initial_stop=sh["initial_stop"], highest=sh["highest"],
                        entry_atr=sh["entry_atr"])

    def _open(self, key: str) -> list[dict]:
        return [sh for sh in self.store.shadows("open") if sh["key"] == key]

    def update(self, s, price: float) -> None:
        """Every tick: stops, trailing and the time limit."""
        from .engine import trailed_stop
        fee = self.cfg.fee_rate(s.market)
        for sh in self._open(s.key):
            if price <= sh["stop"]:
                kind = "trailing stop" if sh["stop"] > sh["initial_stop"] else "stop-loss"
                self._close(sh, price, kind, fee)
                continue
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(sh["created"])).days
            if age_days >= self.cfg.shadow.max_days:
                self._close(sh, price, f"tracking ended after {age_days} days", fee)
                continue
            p = self._pos(sh)
            new_stop = trailed_stop(p, price, self.cfg.risk, fee)
            high = max(sh["highest"], price)
            if new_stop > sh["stop"] or high > sh["highest"]:
                self.store.update_shadow(sh["id"], stop=max(sh["stop"], new_stop), highest=high)

    def strategy_exits(self, s, strat, candles, price: float, progress) -> None:
        """When the strategy would have sold, close the hypothetical trade too."""
        for sh in self._open(s.key):
            if strat.evaluate(candles, self._pos(sh), price, progress).action == "SELL":
                self._close(sh, price, "strategy exit", self.cfg.fee_rate(s.market))

    def _close(self, sh: dict, price: float, why: str, fee: float) -> None:
        risk = sh["entry"] - sh["initial_stop"]
        net = price * (1 - fee) - sh["entry"] * (1 + fee)
        self.store.update_shadow(sh["id"], status="closed", exit=price, exit_ts=now_iso(),
                                 r=net / risk if risk > 0 else 0.0, exit_reason=why)

    def summary(self) -> dict:
        out = {}
        rows = self.store.shadows(limit=5000)
        for src, label in SOURCES.items():
            closed = [r for r in rows if r["source"] == src and r["status"] == "closed"]
            n_open = sum(1 for r in rows if r["source"] == src and r["status"] == "open")
            total = sum(r["r"] or 0 for r in closed)
            wins = sum(1 for r in closed if (r["r"] or 0) > 0)
            if not closed:
                verdict = "no finished examples yet"
            elif len(closed) < 10:
                verdict = f"too early to judge ({len(closed)} finished)"
            elif total < 0:
                verdict = f"helping: blocked trades would have lost {abs(total):.1f}R in total"
            else:
                verdict = f"costing you: blocked trades would have made {total:.1f}R in total"
            out[src] = {"label": label, "closed": len(closed), "open": n_open, "total_r": round(total, 2),
                        "avg_r": round(total / len(closed), 2) if closed else 0.0,
                        "win_rate": round(wins / len(closed) * 100) if closed else 0, "verdict": verdict}
        return out
