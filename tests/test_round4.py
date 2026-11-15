"""Order safety, guardian, backups, stress/Monte Carlo, small-live, strategy options, new signals, conveniences."""
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.backtest import monte_carlo, run_backtest, stress_test, trades_as_pct
from app.backup import make_backup, ship
from app.config import AllocationCfg, Config, ExecutionCfg, ParkingCfg, RiskCfg, Secrets, SignalsCfg, SymbolCfg
from app.execution import LiveExecutor
from app.guardian import Guardian
from app.models import Position
from app.realtime import parse_upbit_tick
from app.server import build_app
from app.signals import MarketSignals
from app.storage import Store
from app.strategies import AutoSwitch
from app.tax import harvest_review
from app.variants import build_variants
from app.vault import decrypt_raw
from tests.test_bot import NO_FILTERS, candles_from, first_buy_index, long_history, make_engine, v_shape
from tests.test_round3 import FakeClaude, FakeHTTP, engine_at_setup


# ── US order re-pricing ──────────────────────────────────────
class FakeKIS:
    """Holdings change only after the Nth price modification (fills_after_modify) — or never."""
    def __init__(self, fills_after_modify=1, never=False):
        self.qty, self.mods, self.cancels, self.orders = 0.0, [], [], []
        self.fills_after, self.never, self.app_key = fills_after_modify, never, "k"
        self.pending = 0

    def us_order(self, side, sym, ex, qty, price):
        self.orders.append((side, qty, price)); self.pending = qty; self.side = side
        return {"output": {"ODNO": "123"}}

    def us_modify(self, oid, sym, ex, qty, price):
        self.mods.append(price)
        if not self.never and len(self.mods) >= self.fills_after:
            self.qty += self.pending if self.side == "buy" else -self.pending
        return {}

    def us_cancel(self, oid, sym, ex, qty):
        self.cancels.append(qty); return {}

    def us_holdings(self, ex):
        return {"AAPL": (self.qty, 200.0)} if self.qty else {}

    def kr_balance(self):
        return {}, 1e8


def live_exec(kis):
    cfg = Config(execution=ExecutionCfg(us_retries=2, us_step_pct=0.5))
    ex = LiveExecutor(cfg, None, kis)
    ex._kis_wait = lambda s, before, side, timeout=20: (abs(kis.qty - before), 200.0)  # no real sleeping
    return ex


def test_us_buy_is_repriced_until_filled():
    kis = FakeKIS(fills_after_modify=2)
    f = live_exec(kis).buy(SymbolCfg("AAPL", "kis_us", exchange="NASD"), 10, 200.0, 1400)
    assert f.ok and f.qty == 10
    assert kis.orders[0][2] == 201.0 and kis.mods == [pytest.approx(202.0, abs=0.01), pytest.approx(203.01, abs=0.01)]
    assert kis.cancels == []


def test_us_order_cancelled_when_it_never_fills():
    kis = FakeKIS(never=True)
    f = live_exec(kis).buy(SymbolCfg("AAPL", "kis_us"), 10, 200.0, 1400)
    assert not f.ok and "cancelled" in f.message and kis.cancels == [10] and len(kis.mods) == 2


def test_us_sell_reprices_downward():
    kis = FakeKIS(fills_after_modify=1)
    kis.qty = 10
    f = live_exec(kis).sell(SymbolCfg("AAPL", "kis_us"), 10, 200.0, 1400)
    assert f.ok and kis.orders[0][2] == 199.0 and kis.mods[0] < 199.0


# ── real-time stops ──────────────────────────────────────────
def test_upbit_tick_parsing():
    assert parse_upbit_tick(b'{"type":"ticker","code":"KRW-BTC","trade_price":140000000.0}') == ("KRW-BTC", 140000000.0)
    assert parse_upbit_tick(b"not json") is None


def test_fast_check_sells_on_stop_and_trails():
    eng, _ = engine_at_setup()
    eng.tick()
    pos = eng.store.position("upbit:KRW-TEST")
    eng.fast_check("upbit:KRW-TEST", pos.avg_price + 6 * pos.entry_atr)
    raised = eng.store.position("upbit:KRW-TEST").stop
    assert raised > pos.stop
    eng.fast_check("upbit:KRW-TEST", raised * 0.999)
    assert eng.store.position("upbit:KRW-TEST") is None and "[real-time]" in eng.store.trades()[0]["reason"]


# ── guardian ─────────────────────────────────────────────────
class GHttp:
    def __init__(self):
        self.up = True

    def get(self, url, headers=None, timeout=None):
        if not self.up:
            raise ConnectionError("down")
        return SimpleNamespace(status_code=200, json=lambda: {"positions": [
            {"key": "upbit:KRW-BTC", "symbol": "KRW-BTC", "market": "upbit", "qty": 0.01, "stop": 100.0}]})


def test_guardian_takes_over_and_sells(tmp_path):
    msgs, sold = [], []
    upbit = SimpleNamespace(access_key="k", price=lambda m: 95.0, sell_market=lambda m, q: sold.append((m, q)) or {"uuid": "u1"})
    http = GHttp()
    g = Guardian("https://bot", "tok", msgs.append, upbit=upbit, takeover_minutes=5,
                 state_file=str(tmp_path / "g.json"), session=http)
    g.step(now=1000)
    assert g.state["positions"][0]["stop"] == 100.0
    http.up = False
    g.step(now=1100)                       # down < 5 min: just waits
    assert not sold and not msgs
    g.step(now=1500)                       # down > 5 min: takes over, stop hit → sells
    assert sold == [("KRW-BTC", 0.01)] and any("STOP HIT" in m for m in msgs)
    g.step(now=1560)                       # doesn't sell twice
    assert len(sold) == 1
    http.up = True
    g.step(now=1600)
    assert any("back online" in m for m in msgs) and not g.state["taken_over"]


def test_guardian_alert_only_without_keys(tmp_path):
    msgs = []
    g = Guardian("https://bot", "tok", msgs.append, upbit=None, state_file=str(tmp_path / "g.json"), session=GHttp())
    g.step(now=0)
    g.http.up = False
    g.price = lambda p: 90.0
    g.step(now=1000)                 # bot just went down — guardian waits
    assert not msgs
    g.step(now=1400)                 # 6+ minutes later: takes over, can't sell → urgent alert
    assert any("SELL IT MANUALLY" in m for m in msgs)


def test_guardian_endpoint_needs_token():
    eng, _ = engine_at_setup()
    eng.tick()
    os.environ["GUARDIAN_TOKEN"] = "g-secret"
    try:
        with TestClient(build_app(eng.cfg, eng, run_loop=False)) as c:
            assert c.get("/api/guardian").status_code == 401
            r = c.get("/api/guardian", headers={"X-Guardian-Token": "g-secret"}).json()
            assert r["positions"][0]["symbol"] == "KRW-TEST" and r["positions"][0]["stop"] > 0
            assert eng.store.get("guardian_seen")
    finally:
        del os.environ["GUARDIAN_TOKEN"]


# ── backups ──────────────────────────────────────────────────
def test_backup_encrypted_plain_and_prune(tmp_path):
    st = Store(str(tmp_path / "bot.db"))
    st.set("hello", "world")
    enc = make_backup(st, tmp_path / "b", "a long passphrase!")
    assert enc.suffix == ".enc" and b"hello" not in enc.read_bytes()
    restored = tmp_path / "r.db"
    restored.write_bytes(decrypt_raw(enc.read_bytes(), "a long passphrase!"))
    assert Store(str(restored)).get("hello") == "world"
    for i in range(16):
        (tmp_path / "b" / f"tradebot-2026010{i:02d}-0300.db").write_bytes(b"x")
    make_backup(st, tmp_path / "b")
    assert len(list((tmp_path / "b").glob("tradebot-*"))) == 14


def test_backup_shipping(tmp_path, monkeypatch):
    p = tmp_path / "x.db"
    p.write_bytes(b"data")
    monkeypatch.setenv("BACKUP_COMMAND", "true")
    sent = []
    n = SimpleNamespace(send_file=lambda path, caption: sent.append(path) or True)
    assert ship(p, n) == ["BACKUP_COMMAND ok", "sent to Telegram"] and sent == [p]


# ── stress test & Monte Carlo ────────────────────────────────
def test_stress_test_uses_crash_windows():
    df = long_history(900)
    df.index = pd.bdate_range("2018-06-01", periods=900).strftime("%Y-%m-%d")
    rows = {r["name"]: r for r in stress_test(df, "trend_rsi", "upbit", RiskCfg(), 0.0005, params=NO_FILTERS)}
    assert "return_pct" in rows["COVID crash (2020)"] and "max_drawdown_pct" in rows["COVID crash (2020)"]
    assert "skipped" in rows["2022 stock bear market"]


def test_monte_carlo_numbers_make_sense():
    mc = monte_carlo([1.0] * 30 + [-1.0] * 20, sims=2000)
    assert mc["max_drawdown_pct"]["bad_5pct"] <= mc["max_drawdown_pct"]["median"] <= 0
    assert mc["losing_streak"]["bad_5pct"] >= mc["losing_streak"]["median"] >= 1
    assert mc["final_return_pct"]["bad_5pct"] < mc["final_return_pct"]["median"] < mc["final_return_pct"]["good_95pct"]
    with pytest.raises(ValueError):
        monte_carlo([1.0] * 5)
    assert trades_as_pct([{"r": 2.0}, {"r": -1.0}], 1.0) == [2.0, -1.0]


# ── small-live mode ──────────────────────────────────────────
def test_live_capital_cap_limits_size_and_twin_is_created():
    risk = RiskCfg(max_total_risk_pct=0, weekend_crypto_mult=1.0)
    big, _ = engine_at_setup(risk=risk)
    small, _ = engine_at_setup(risk=risk)
    small._cfg.trading_mode, small._cfg.live_capital_krw = "live", 1_000_000
    big.tick(); small.tick()
    assert small.store.position("upbit:KRW-TEST").qty == pytest.approx(big.store.position("upbit:KRW-TEST").qty / 10, rel=0.02)
    names = [n for n, _ in build_variants(small)]
    assert names[0] == "paper-twin"
    twin = build_variants(small)[0][1]
    assert twin.cfg.trading_mode == "paper" and twin.cfg.starting_cash_krw == 1_000_000


# ── strategy options ─────────────────────────────────────────
def inverse_engine(regime_ok):
    eng, ai = engine_at_setup()
    eng._cfg.symbols[0].inverse = True
    eng.data.regime_ok = regime_ok
    return eng


def test_inverse_buys_only_in_falling_market_and_exits_when_it_turns():
    up = inverse_engine(regime_ok=True)
    up.tick()
    assert up.store.position("upbit:KRW-TEST") is None and "falling market" in up.store.signals()[0]["outcome"]
    down = inverse_engine(regime_ok=False)
    down.tick()
    assert down.store.position("upbit:KRW-TEST")
    down.data.regime_ok = True
    down.store.set("last_bar:upbit:KRW-TEST", None)
    down.tick()
    assert down.store.position("upbit:KRW-TEST") is None and "turned back up" in down.store.trades()[0]["reason"]


def test_partial_profit_then_pyramid():
    eng, _ = engine_at_setup(risk=RiskCfg(partial_r=2, partial_pct=50, pyramid_r=1, pyramid_pct=50,
                                          max_total_risk_pct=0, weekend_crypto_mult=1.0, trail_atr=0))
    eng.tick()
    p0 = eng.store.position("upbit:KRW-TEST")
    one_r = p0.avg_price - p0.initial_stop
    eng.data.px = p0.avg_price + 1.05 * one_r            # +1R → break-even stop, then add 50%
    eng.tick()
    p1 = eng.store.position("upbit:KRW-TEST")
    assert p1.qty == pytest.approx(p0.qty * 1.5, rel=0.01) and any("ADD" in e["msg"] for e in eng.store.events())
    eng.data.px = p0.avg_price + 2.1 * one_r             # +2R → sell half
    eng.tick()
    p2 = eng.store.position("upbit:KRW-TEST")
    assert p2.qty == pytest.approx(p1.qty / 2, rel=0.01)
    assert "partial profit" in eng.store.trades()[0]["reason"] and eng.store.trades()[0]["pnl_krw"] > 0
    eng.tick()                                            # neither repeats
    assert eng.store.position("upbit:KRW-TEST").qty == pytest.approx(p2.qty)


def test_backtest_partial_profit_shows_up():
    r = run_backtest(long_history(900), "trend_rsi", "upbit", RiskCfg(partial_r=1.5), 0.0005, params=NO_FILTERS)
    assert any("partial" in t["why"] for t in r["log"])


def test_auto_strategy_switches_and_exits_with_its_own_rules():
    s = AutoSwitch(long_ma=0)
    df = long_history(600)
    p = s.prepare(df)
    sigs = [s.decide(p, i, None, 1.0) for i in range(s.min_bars, len(df))]
    buys = [x for x in sigs if x.action == "BUY"]
    assert buys and all(("trend mode" in b.reason) or ("dip mode" in b.reason) for b in buys)
    dip_pos = Position("k", "s", "upbit", 1, 1, 0, 0, "", p.index[-2], "dip mode (ADX 12): sharp dip")
    assert s.decide(p, len(df) - 1, dip_pos, 1.0).reason in {
        s.dip.decide(p.dip, len(df) - 1, dip_pos, 1.0).reason}


def test_allocation_scales_or_skips_by_retest():
    eng, _ = engine_at_setup()
    eng._cfg.allocation = AllocationCfg(enabled=True, fail_mult=0)
    eng.store.set("retest:upbit:KRW-TEST", {"pass": False})
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None and "allocation 0" in eng.store.signals()[0]["outcome"]
    eng2, _ = engine_at_setup()
    eng2._cfg.allocation = AllocationCfg(enabled=True, strong_mult=1.25, strong_pf=1.5)
    eng2.store.set("retest:upbit:KRW-TEST", {"pass": True, "profit_factor": 2.0})
    assert eng2._alloc_mult(eng2.cfg.symbols[0])[0] == 1.25


# ── new signals ──────────────────────────────────────────────
def test_fear_greed_krx_flows_and_earnings():
    kis = SimpleNamespace(kr_investor_flows=lambda code, days: -5000.0 if code == "005930" else 12000.0)
    c = Config(signals=SignalsCfg())
    http = FakeHTTP({"alternative.me": {"data": [{"value": "86", "value_classification": "Extreme Greed"}]},
                     "finnhub": {"earningsCalendar": [{"symbol": "AAPL", "date": "2026-11-17"}]}})
    s = MarketSignals(c, session=http, kis=kis, finnhub_key="f")
    assert s.fear_greed().action == "block" and "Alternative.me" in s.fear_greed().detail
    assert s.krx_flows("005930").action == "block" and s.krx_flows("000660").action is None
    assert s.earnings("AAPL", today=date(2026, 11, 16)).action == "block"
    assert {d.source for d in s.check("kis_kr", "005930")} >= {"krx_flows"}
    assert "earnings" in {d.source for d in s.check("kis_us", "AAPL")}
    no_key = MarketSignals(c, session=http)
    assert no_key.earnings("AAPL").state == "unavailable" and no_key.earnings("AAPL").action is None


# ── conveniences ────────────────────────────────────────────
def test_weekly_review_stored_and_sent():
    eng, _ = engine_at_setup()
    eng.ai.client = FakeClaude()
    text = eng.monitor.run_review()
    assert "crossover" in text and eng.store.get("weekly_review")["text"] == text
    assert any("Weekly TradeBot review" in m for m in eng.notifier.msgs)


def test_tax_review_lists_losers():
    eng, _ = engine_at_setup()
    eng.tick()
    p = eng.store.position("upbit:KRW-TEST")
    eng.last_prices[p.key] = p.avg_price * 0.9
    r = harvest_review(eng)
    assert r["candidates"][0]["symbol"] == "KRW-TEST" and r["candidates"][0]["unrealized_krw"] < 0
    assert "wash-sale" in r["note"]


def test_idle_cash_parking_buys_and_releases():
    eng, _ = engine_at_setup()
    eng._cfg.parking = ParkingCfg(enabled=True, symbol="PARK", keep_cash_pct=20)
    eng.data.px = 1_000_000.0
    eng.longterm.park()
    pos = eng.store.position("park:kis_kr:PARK")
    assert pos and pos.qty == 8                            # ₩10M − 20% kept → 8 shares
    eng.store.set("paper_cash", 500_000.0)                 # cash drops below half the buffer
    eng.longterm.park()
    assert eng.store.position("park:kis_kr:PARK").qty < 8


def test_new_endpoints():
    eng, _ = engine_at_setup()
    eng.ai.client = None
    with TestClient(build_app(eng.cfg, eng, run_loop=False)) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"token": "secret"}).json()["session"]}
        assert "candidates" in c.get("/api/tax/review", headers=h).json()
        assert c.get("/api/review", headers=h).json() == {}
        assert c.post("/api/review", headers=h).status_code == 400            # no Claude key
        assert c.get("/api/montecarlo", headers=h).json()["error"]
        assert c.post("/api/backup", headers=h).status_code == 400           # in-memory DB
        st = c.get("/api/status", headers=h).json()
        assert "fast_stops" in st and "backup_last" in st


def test_app_update_files(tmp_path):
    eng, _ = engine_at_setup()
    eng.store = Store(str(tmp_path / "bot.db"))
    with TestClient(build_app(eng.cfg, eng, run_loop=False)) as c:
        assert c.get("/app/version.json").status_code == 404           # nothing published yet
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "version.json").write_text('{"versionCode": 105}')
        (tmp_path / "app" / "TradeBot.apk").write_bytes(b"PK-apk")
        r = c.get("/app/version.json")
        assert r.status_code == 200 and r.json()["versionCode"] == 105  # no login needed
        r = c.get("/app/TradeBot.apk")
        assert r.content == b"PK-apk" and r.headers["content-type"] == "application/vnd.android.package-archive"
