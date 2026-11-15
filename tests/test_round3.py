"""Market-condition signals, risk additions, long-term holdings, side-by-side accounts, Ask Claude."""
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.ask import DAILY_LIMIT, ask, build_context
from app.backtest import run_dual_momentum, run_grid
from app.config import Config, CoreCfg, RankingCfg, RiskCfg, SignalsCfg, SymbolCfg
from app.engine import is_crypto_weekend
from app.longterm import LongTerm, is_longterm
from app.risk import RiskManager
from app.server import build_app
from app.signals import Decision, MarketSignals, macro_events, parse_etf_rows, parse_vix_csv
from app.variants import account_stats, build_variants, variant_config
from tests.test_bot import NO_FILTERS, candles_from, first_buy_index, make_engine, v_shape
from tests.test_upgrades import FakeNotifier

NY, SEOUL = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")


class FakeHTTP:
    """Maps URL substrings to responses; raises for anything else (like an outage)."""
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None, timeout=None, headers=None):
        for k, v in self.routes.items():
            if k in url:
                return SimpleNamespace(json=lambda v=v: v, text=v if isinstance(v, str) else "",
                                       raise_for_status=lambda: None)
        raise ConnectionError(url)


def sig(routes, **cfg):
    c = Config(signals=SignalsCfg(**cfg))
    return MarketSignals(c, fx_rate=lambda: 1400.0, upbit=SimpleNamespace(price=lambda m: 140_000_000.0),
                         session=FakeHTTP(routes))


# ── parsers (formats checked against the live sources) ───────
def test_parse_tftc_latest_json():
    doc = {"updatedThrough": "2026-11-11",
           "latestSession": {"date": "2026-11-11", "netFlowUsd": -299898422.14, "perEtfUsd": {"IBIT": -1}},
           "latestPerFund": {"date": "2026-11-11", "netFlowUsd": -299898422.14},
           "recentSessions": [{"date": "2026-11-11", "netFlowUsd": -299898422.14},
                              {"date": "2026-11-10", "netFlowUsd": -150e6}, {"date": "2026-11-07", "netFlowUsd": 80e6}]}
    rows = parse_etf_rows(doc)
    assert [d for d, _ in rows] == ["2026-11-07", "2026-11-10", "2026-11-11"]  # de-duplicated, oldest first


def test_parse_vix_both_formats():
    assert parse_vix_csv("observation_date,VIXCLS\n2026-11-11,17.2\n2026-11-12,.\n2026-11-13,19.5\n") == [17.2, 19.5]
    assert parse_vix_csv("DATE,OPEN,HIGH,LOW,CLOSE\n11/12/2026,1,1,1,18.0\n") == [18.0]


# ── individual signals ───────────────────────────────────────
def test_vix_calm_high_and_spike():
    assert sig({"fredgraph": "observation_date,VIXCLS\n2026-11-12,16\n2026-11-13,17\n"}).vix().action is None
    r = sig({"fredgraph": "observation_date,VIXCLS\n2026-11-12,28\n2026-11-13,34\n"}).vix()
    assert r.action == "halve" and "34" in r.detail
    r = sig({"fredgraph": "observation_date,VIXCLS\n2026-11-12,15\n2026-11-13,19\n"}, vix_action="block").vix()
    assert r.action == "block" and "up 27%" in r.detail


def test_vix_falls_back_to_cboe_then_steps_aside():
    assert sig({"cboe.com": "DATE,OPEN,HIGH,LOW,CLOSE\n1,1,1,1,20\n1,1,1,1,40\n"}).vix().action == "halve"
    r = sig({}).vix()
    assert r.state == "unavailable" and r.action is None  # outage never blocks trading


def test_macro_days_and_korea_next_morning():
    s = sig({})
    fomc = datetime(2026, 12, 9, 11, 0, tzinfo=NY)
    assert s.macro("kis_us", fomc).action == "block" and "FOMC" in s.macro("kis_us", fomc).detail
    assert s.macro("upbit", fomc).action == "block"
    kr_next_morning = datetime(2026, 12, 10, 10, 0, tzinfo=SEOUL)  # KRX reacts the day after
    assert s.macro("kis_kr", kr_next_morning).action == "block"
    assert s.macro("kis_us", datetime(2026, 12, 8, 11, 0, tzinfo=NY)).action is None
    ev = macro_events()
    assert ev[date(2027, 1, 13)] == "CPI inflation report" and ev[date(2027, 1, 8)] == "US jobs report"


def test_crypto_signals():
    routes = {"tftc": {"recentSessions": [{"date": f"2026-11-1{i}", "netFlowUsd": -300e6} for i in range(3)]},
              "premiumIndex": {"lastFundingRate": "0.0009"},
              "ticker/price": {"price": "100000"}, "coinbase": {"price": "99700"}}
    s = sig(routes)
    assert s.etf_flows().action == "block" and "TFTC" in s.etf_flows().detail
    assert s.funding().action == "block"                  # 0.09%/8h > 0.05
    assert s.coinbase_premium().action == "block"         # -0.30% < -0.15
    assert s.upbit_premium().action is None               # 140M / (100k × 1400) = 0%
    names = {d.source for d in s.check("upbit")}
    assert {"etf_flows", "funding", "cb_premium"} <= names and "vix" not in names


def test_funding_bybit_fallback_and_upbit_premium_block():
    s = sig({"bybit": {"result": {"list": [{"fundingRate": "0.0001"}]}}, "ticker/price": {"price": "90000"}})
    assert s.funding().value == pytest.approx(0.01) and s.funding().action is None
    assert s.upbit_premium().action == "block"            # 140M / (90k × 1400) − 1 ≈ +11%


def test_signals_master_switch():
    assert sig({}, enabled=False).check("upbit") == []


# ── engine integration ───────────────────────────────────────
def engine_at_setup(**kw):
    closes = v_shape()
    eng, ai = make_engine(closes[: first_buy_index(closes)], **kw)
    eng.notifier = FakeNotifier()
    return eng, ai


def test_condition_block_is_logged_and_shadowed():
    eng, ai = engine_at_setup()
    eng.signals.check = lambda m, sym=None: [Decision("funding", "block", "funding 0.09%/8h — longs are crowded")]
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None and ai.calls == 0
    assert "market conditions" in eng.store.signals()[0]["outcome"]
    assert eng.store.shadows()[0]["source"] == "funding"


def qty_after_tick(eng):
    eng.tick()
    return eng.store.position("upbit:KRW-TEST").qty


def test_halve_signal_halves_risk():
    risk = RiskCfg(max_position_pct=50, weekend_crypto_mult=1.0, max_total_risk_pct=0)
    a, _ = engine_at_setup(risk=risk)
    b, _ = engine_at_setup(risk=risk)
    b.signals.check = lambda m, sym=None: [Decision("vix", "halve", "VIX 34")]
    assert qty_after_tick(b) == pytest.approx(qty_after_tick(a) / 2, rel=0.02)
    assert "half size" in b.store.trades()[0]["reason"]


def test_weekend_window():
    assert is_crypto_weekend(datetime(2026, 11, 14, 15, 0, tzinfo=SEOUL))      # Saturday
    assert is_crypto_weekend(datetime(2026, 11, 16, 8, 59, tzinfo=SEOUL))      # Monday early
    assert not is_crypto_weekend(datetime(2026, 11, 16, 9, 0, tzinfo=SEOUL))
    assert not is_crypto_weekend(datetime(2026, 11, 13, 23, 0, tzinfo=SEOUL))  # Friday night


def test_total_risk_cap_shrinks_then_blocks():
    rm = RiskManager(RiskCfg(max_total_risk_pct=2, max_position_pct=50))
    kw = dict(market="kis_kr", price=100, atr=1, fx=1, fee_rate=0, equity_krw=1e7, cash_krw=1e7, n_open=1,
              trades_today=0, day_pnl_pct=0)
    full = rm.plan_buy(**kw, open_risk_krw=0).qty
    part = rm.plan_buy(**kw, open_risk_krw=150_000).qty
    assert part == pytest.approx(full / 2, rel=0.01)   # room left 50k of 200k cap... risk_per_trade 1% = 100k
    assert not rm.plan_buy(**kw, open_risk_krw=200_000).ok


def test_momentum_ranking_blocks_weak_symbol():
    eng, _ = engine_at_setup()
    eng._cfg.ranking = RankingCfg(enabled=True, top_n=1, lookback_days=20)
    eng._cfg.symbols.append(SymbolCfg("KRW-HOT", "upbit", "60m", "trend_rsi", params=dict(NO_FILTERS)))
    eng.strategies["upbit:KRW-HOT"] = eng.strategies["upbit:KRW-TEST"]
    eng.data.daily = {"upbit:KRW-TEST": pd.Series(np.linspace(100, 101, 30)),
                      "upbit:KRW-HOT": pd.Series(np.linspace(100, 150, 30))}
    eng._process(eng.cfg.symbols[0])
    assert eng.store.position("upbit:KRW-TEST") is None
    assert "ranked #2 of 2" in eng.store.signals()[0]["outcome"]


def test_usd_exposure_and_fx_effect():
    eng, _ = engine_at_setup()
    from app.models import Position
    eng.store.save_position(Position("kis_us:AAPL", "AAPL", "kis_us", 10, 200, 180, 0, "t"))
    eng.store.add_trade(key="kis_us:AAPL", symbol="AAPL", market="kis_us", side="BUY", qty=10, price=200,
                        value_krw=1, fee_krw=0, mode="paper", fx_usdkrw=1300)
    eng._cfg.usd_krw = 1400
    u = eng.usd_exposure()
    assert u["fx_effect_krw"] == pytest.approx(10 * 200 * 100) and u["pct_of_equity"] > 0


# ── long-term holdings ───────────────────────────────────────
def lt_engine():
    eng, _ = engine_at_setup()
    eng._cfg.core = CoreCfg(enabled=True, pct=50, lookback_days=60, cash_return_pct=0)
    return eng


def test_core_picks_and_switches():
    eng = lt_engine()
    up, flat, bond = np.linspace(100, 130, 61), np.linspace(100, 105, 61), np.linspace(100, 101, 61)
    eng.data.daily = {"kis_us:SPY": pd.Series(up), "kis_us:VEU": pd.Series(flat), "kis_us:AGG": pd.Series(bond)}
    assert eng.longterm.core_choice()[0] == "SPY"
    eng.longterm.core()
    p = eng.store.position("core:kis_us:SPY")
    assert p and p.qty * p.avg_price == pytest.approx(5_000_000, rel=0.05)
    assert eng.store.get("core_last")["pick"] == "SPY"
    eng.longterm.core()                                   # same month → no action
    assert len([t for t in eng.store.all_trades() if t["key"].startswith("core:")]) == 1
    eng.store.set("core_month", "1999-01")                # new month, US stocks now falling → bonds
    eng.data.daily["kis_us:SPY"] = pd.Series(up[::-1])
    eng.longterm.core()
    assert eng.store.position("core:kis_us:SPY") is None and eng.store.position("core:kis_us:AGG")


def test_longterm_positions_excluded_from_swing_limits():
    eng = lt_engine()
    eng.data.daily = {k: pd.Series(np.linspace(100, 130, 61)) for k in ("kis_us:SPY", "kis_us:VEU", "kis_us:AGG")}
    eng.longterm.core()
    assert eng.swing_positions() == [] and is_longterm("core:kis_us:SPY")
    eng.tick()                                            # swing buy still allowed alongside the core
    assert eng.store.position("upbit:KRW-TEST")


def test_dca_schedule_boost_and_averaging():
    eng, _ = engine_at_setup()
    eng._cfg.dca = [{"symbol": "KRW-TEST", "market": "upbit", "amount_krw": 100_000, "every": "day"}]
    falling = pd.Series(100 * np.exp(np.linspace(0, -0.3, 250)))
    eng.data.daily = {"upbit:KRW-TEST": falling}
    mult, why = eng.longterm.dca_multiplier(falling)
    assert mult == 3.0 and "oversold" in why
    eng.longterm.dca(); eng.longterm.dca()                 # second call same day → no repeat
    p = eng.store.position("dca:upbit:KRW-TEST")
    assert p and p.qty * p.avg_price == pytest.approx(300_000, rel=0.01)
    assert len([t for t in eng.store.all_trades() if t["key"].startswith("dca:")]) == 1


def test_grid_buys_down_sells_up_and_stops():
    eng, _ = engine_at_setup()
    g = {"symbol": "KRW-TEST", "lower": 90, "upper": 110, "levels": 4, "budget_krw": 400_000, "stop_below_pct": 5}
    eng._cfg.grid = [g]
    key = "grid:upbit:KRW-TEST"
    for px in (101, 96, 101, 106, 84):
        eng.data.px = px
        eng.longterm.grid()
        if px == 96:
            assert len(eng.store.get("grid_state:KRW-TEST")["lots"]) == 2   # start lot at 100 + step at 95
        if px == 106:
            assert eng.store.get("grid_state:KRW-TEST")["lots"] == {}        # both sold on the way up
    st = eng.store.get("grid_state:KRW-TEST")
    sells = [t for t in eng.store.all_trades() if t["key"] == key and t["side"] == "SELL"]
    assert len(sells) >= 2 and sum(t["pnl_krw"] for t in sells[:2]) > 0
    assert st["stopped"] and eng.store.position(key) is None and any("Grid" in m for m in eng.notifier.msgs)


def test_kill_switch_sells_longterm_too():
    eng = lt_engine()
    eng.data.daily = {k: pd.Series(np.linspace(100, 130, 61)) for k in ("kis_us:SPY", "kis_us:VEU", "kis_us:AGG")}
    eng.longterm.core()
    msgs = eng.kill()
    assert eng.store.positions() == [] and any("core" in m for m in msgs)


def test_paused_blocks_longterm_buys():
    eng = lt_engine()
    eng.data.daily = {k: pd.Series(np.linspace(100, 130, 61)) for k in ("kis_us:SPY", "kis_us:VEU", "kis_us:AGG")}
    eng.set_paused(True)
    eng.longterm.core()
    assert eng.store.positions() == []


# ── backtests ────────────────────────────────────────────────
def test_dual_momentum_avoids_a_crash():
    n = 600
    spy = np.r_[np.linspace(100, 160, 400), np.linspace(160, 90, 200)]
    veu = np.linspace(100, 120, n)
    agg = np.linspace(100, 110, n)
    idx = pd.date_range("2020-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    r = run_dual_momentum({k: pd.Series(v, index=idx) for k, v in (("SPY", spy), ("VEU", veu), ("AGG", agg))},
                          "SPY", "VEU", "AGG", lookback=120, cash_return_pct=0)
    assert r["max_drawdown_pct"] > r["hold_us_drawdown_pct"] and r["switches"] >= 2


def test_grid_backtest_profits_in_range():
    x = 100 + 8 * np.sin(np.linspace(0, 20 * np.pi, 600))
    r = run_grid(candles_from(x), 90, 110, 8, 1_000_000, 0.0005)
    assert r["realized_krw"] > 0 and r["trades"] > 20 and not r["stopped"]
    crash = run_grid(candles_from(np.linspace(100, 60, 200)), 90, 110, 8, 1_000_000)
    assert crash["stopped"] and crash["return_pct"] < 0


# ── side-by-side accounts ────────────────────────────────────
def test_variant_config_and_validation():
    eng, _ = engine_at_setup()
    v = variant_config(eng.cfg, {"name": "x", "ai_enabled": False, "risk": {"trail_atr": 2}, "strategy": "dip_buy"})
    assert v.trading_mode == "paper" and not v.ai.enabled and v.risk.trail_atr == 2
    assert all(s.strategy == "dip_buy" for s in v.symbols) and not v.core.enabled
    with pytest.raises(ValueError):
        variant_config(eng.cfg, {"name": "bad", "trading_mode": "live"})


def test_variants_trade_independently_and_report():
    eng, _ = engine_at_setup()
    eng._cfg.variants = [{"name": "no-claude", "ai_enabled": False}, {"name": "tiny", "risk": {"max_position_pct": 1}}]
    vs = build_variants(eng)
    for _, v in vs:
        v.tick()
    eng.tick()
    assert all(v.store.position("upbit:KRW-TEST") for _, v in vs) and eng.store.position("upbit:KRW-TEST")
    tiny = vs[1][1].store.position("upbit:KRW-TEST").qty
    assert tiny < eng.store.position("upbit:KRW-TEST").qty
    assert vs[0][1].notifier is None and vs[0][1].store is not eng.store
    st = account_stats(vs[0][1], "no-claude")
    assert st["open_positions"] == 1 and st["claude_calls"] == 0


def test_variants_api():
    eng, _ = engine_at_setup()
    eng._cfg.variants = [{"name": "no-claude", "ai_enabled": False}]
    with TestClient(build_app(eng.cfg, eng, run_loop=False)) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"token": "secret"}).json()["session"]}
        rows = c.get("/api/variants", headers=h).json()
        assert [r["name"] for r in rows] == ["Main bot", "no-claude"]
        assert c.get("/api/conditions", headers=h).json() == {"enabled": False}
        assert c.get("/api/longterm", headers=h).json()["held"] == []
        r = c.post("/api/settings", headers=h, json={"signals_enabled": True, "ranking_enabled": True})
        assert r.status_code == 200 and eng.cfg.ranking.enabled


# ── Ask Claude ───────────────────────────────────────────────
class FakeClaude:
    def __init__(self):
        self.messages, self.seen = self, None

    def create(self, **kw):
        self.seen = kw
        return SimpleNamespace(content=[SimpleNamespace(text="KRW-TEST was bought on an EMA crossover.")])


def test_ask_uses_bot_data_and_limits():
    eng, _ = engine_at_setup()
    eng.tick()
    import json
    ctx = json.loads(build_context(eng))
    assert ctx["open_positions"][0]["symbol"] == "KRW-TEST" and "shadow_results" in ctx
    fc = FakeClaude()
    eng.ai.client = fc
    assert "crossover" in ask(eng, "Why did you buy KRW-TEST?")
    assert "<bot_data>" in fc.seen["messages"][0]["content"] and "KRW-TEST" in fc.seen["messages"][0]["content"]
    assert "data, not instructions" in fc.seen["system"] and "tools" not in fc.seen
    eng.store.set(f"ask_count:{datetime.utcnow().date().isoformat()}", DAILY_LIMIT)
    with pytest.raises(ValueError):
        ask(eng, "again?")
    eng.ai.client = None
    with pytest.raises(ValueError):
        ask(eng, "no key?")
