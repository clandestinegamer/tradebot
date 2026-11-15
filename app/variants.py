"""Side-by-side paper accounts: run alternative settings next to the main bot on the same live prices.

Each variant is a full paper-trading copy of the bot with a few settings changed (for example "no Claude
review" or "tighter stops"). Variants share all market data with the main bot (no extra API calls), never
place real orders, never send alerts, and keep their own database in data/variant_<name>.db.

[[variants]]
name = "no-claude"
ai_enabled = false
"""
from __future__ import annotations

import copy
import re
from dataclasses import replace
from pathlib import Path

from .ai_review import AIReviewer
from .config import SymbolCfg, validate
from .execution import PaperExecutor
from .storage import Store

ALLOWED = {"name", "description", "risk", "ai_enabled", "regime_enabled", "signals_enabled", "ranking_enabled",
           "strategy", "params"}


def variant_config(base, v: dict):
    unknown = set(v) - ALLOWED
    if unknown:
        raise ValueError(f"variant {v.get('name')}: unknown setting(s) {sorted(unknown)}; allowed {sorted(ALLOWED)}")
    cfg = copy.deepcopy(base)
    cfg.trading_mode = "paper"
    if base.is_live and base.live_capital_krw > 0:
        cfg.starting_cash_krw = base.live_capital_krw  # twin starts with the same money the live bot may use
    cfg.core.enabled, cfg.dca, cfg.grid, cfg.variants = False, [], [], []
    cfg.monitor.retest_day, cfg.monitor.reconcile, cfg.alerts.summary_time = 0, False, ""
    for k, val in (v.get("risk") or {}).items():
        if not hasattr(cfg.risk, k):
            raise ValueError(f"variant {v['name']}: unknown risk setting {k}")
        setattr(cfg.risk, k, type(getattr(cfg.risk, k))(val))
    if "ai_enabled" in v:
        cfg.ai.enabled = bool(v["ai_enabled"])
    if "regime_enabled" in v:
        cfg.regime.enabled = bool(v["regime_enabled"])
    if "signals_enabled" in v:
        cfg.signals.enabled = bool(v["signals_enabled"])
    if "ranking_enabled" in v:
        cfg.ranking.enabled = bool(v["ranking_enabled"])
    if "strategy" in v or "params" in v:
        cfg.symbols = [replace(s, strategy=v.get("strategy", s.strategy),
                               params=dict(v.get("params", {})) if "strategy" in v else {**s.params, **v.get("params", {})})
                       for s in cfg.symbols]
    validate(cfg)
    return cfg


def build_variants(main) -> list[tuple[str, object]]:
    from .engine import Engine
    from .market import DataView, MarketData
    out = []
    specs = list(main.cfg.variants)
    if main.cfg.is_live and main.cfg.paper_twin and not any(x.get("name") == "paper-twin" for x in specs):
        specs.insert(0, {"name": "paper-twin", "description": "Exact paper copy of your live settings"})
    for v in specs:
        cfg = variant_config(main.cfg, v)
        slug = re.sub(r"[^a-z0-9_-]+", "-", v["name"].lower()).strip("-")
        db = ":memory:" if main.store.path == ":memory:" else str(Path(main.store.path).with_name(f"variant_{slug}.db"))
        store = Store(db)
        data = DataView(main.data, cfg) if isinstance(main.data, MarketData) else main.data
        ai = AIReviewer(cfg.ai, "", client=main.ai.client)
        eng = Engine(cfg, store, data, PaperExecutor(cfg, store), ai, notifier=None, fx=main.fxr)
        eng.variant_name = v["name"]
        out.append((v["name"], eng))
    return out


def account_stats(engine, name: str, description: str = "") -> dict:
    from .longterm import is_longterm
    st = engine.store
    eq = engine.equity_krw()
    start = st.get("start_equity") or eq
    sells = [t for t in st.all_trades() if t["side"] == "SELL" and t["pnl_krw"] is not None and not is_longterm(t["key"])]
    wins = [t["pnl_krw"] for t in sells if t["pnl_krw"] > 0]
    losses = [-t["pnl_krw"] for t in sells if t["pnl_krw"] <= 0]
    curve = [r["equity_krw"] for r in st.equity_curve("0")]
    peak, dd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, (v / peak - 1) * 100 if peak else 0.0)
    # swing-trading P&L only (closed + open), so accounts with/without long-term holdings compare fairly
    unreal = sum((engine.last_prices.get(p.key, p.avg_price) - p.avg_price) * p.qty * engine._fx_for(p.market)
                 for p in engine.swing_positions())
    swing_pnl = sum(t["pnl_krw"] for t in sells) + unreal
    return {"name": name, "description": description, "equity_krw": eq, "swing_pnl_krw": swing_pnl,
            "swing_pnl_pct": swing_pnl / start * 100 if start else 0.0,
            "return_pct": (eq / start - 1) * 100 if start else 0.0, "closed_trades": len(sells),
            "win_rate": len(wins) / len(sells) * 100 if sells else None,
            "profit_factor": (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None,
            "max_drawdown_pct": dd, "open_positions": len(engine.swing_positions()),
            "claude_calls": sum(1 for t in st.signals(5000) if t["outcome"] == "executed" or t["outcome"].startswith("AI "))
            if engine.cfg.ai.enabled else 0}
