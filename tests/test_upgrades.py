"""Tests for alerts, shadow mode, execution guards, calendars, monitors, portfolio test, tax, security, settings."""
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.alerts import Notifier
from app.backtest import run_portfolio
from app.calendars import Calendar, nyse_holidays
from app.config import CorrelationCfg, MonitorCfg, RiskCfg, Secrets, ShadowCfg, SymbolCfg, apply_overrides
from app.fx import FXRates
from app.risk import RiskManager
from app.security import TOTPVerifier, new_totp_secret, totp_now
from app.server import build_app
from app.storage import Store
from app.strategies import DipBuy
from app.tax import closed_trades, to_csv
from app.vault import decrypt_bytes, encrypt_values, parse_env
from tests.test_bot import NO_FILTERS, candles_from, first_buy_index, long_history, make_engine, v_shape


class FakeNotifier:
    channels = ["telegram"]

    def __init__(self):
        self.msgs, self.beats = [], []

    def send(self, text, key=None):
        self.msgs.append(text)

    def heartbeat(self, ok=True):
        self.beats.append(ok)


def engine_with_buy(**kw):
    closes = v_shape()
    eng, ai = make_engine(closes[: first_buy_index(closes)], **kw)
    eng.notifier = FakeNotifier()
    return eng, ai


# ── alerts ───────────────────────────────────────────────────
def test_trade_alert_and_heartbeat():
    eng, _ = engine_with_buy()
    eng.tick()
    assert any("BUY KRW-TEST" in m for m in eng.notifier.msgs)
    assert eng.notifier.beats == [True]


def test_notifier_cooldown_dedupes():
    n = Notifier(Secrets(), cooldown_minutes=30)
    n.send("a", key="x"); n.send("b", key="x"); n.send("c")
    assert n.sent == ["a", "c"]


def test_error_alert_sent_once_per_cooldown():
    eng, _ = engine_with_buy()
    eng.data.price = lambda s: (_ for _ in ()).throw(RuntimeError("API down"))
    eng.tick(); eng.tick()
    errs = [m for m in eng.notifier.msgs if "API down" in m]
    assert len(errs) == 2 and eng.notifier.beats == [False, False]  # FakeNotifier has no cooldown; real one dedupes
    assert eng.last_error and "API down" in eng.last_error


# ── shadow mode ──────────────────────────────────────────────
def test_veto_creates_shadow_that_tracks_outcome():
    eng, _ = engine_with_buy(ai_decision="reject")
    eng.tick()
    sh = eng.store.shadows()
    assert len(sh) == 1 and sh[0]["source"] == "ai_veto" and sh[0]["status"] == "open"
    eng.data.px = sh[0]["stop"] * 0.99   # would have been stopped out
    eng.tick()
    done = eng.store.shadows()[0]
    assert done["status"] == "closed" and done["r"] < -0.9
    summ = eng.shadow.summary()["ai_veto"]
    assert summ["closed"] == 1 and "too early" in summ["verdict"]


def test_regime_block_creates_shadow():
    eng, _ = engine_with_buy()
    eng.data.regime_ok = False
    eng.tick()
    assert eng.store.shadows()[0]["source"] == "regime"


def test_shadow_can_be_disabled():
    eng, _ = engine_with_buy(ai_decision="reject")
    eng._cfg.shadow = ShadowCfg(enabled=False)
    eng.tick()
    assert eng.store.shadows() == []


# ── execution guards / sizing ────────────────────────────────
def test_entry_block_skips_buy():
    eng, _ = engine_with_buy()
    eng.data.block = "bid-ask spread 0.9% > 0.3%"
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None
    assert "spread" in eng.store.signals()[0]["outcome"]


def test_slippage_and_fx_recorded():
    eng, _ = engine_with_buy()
    eng.tick()
    t = eng.store.trades()[0]
    assert t["expected_price"] > 0 and t["slippage_pct"] == pytest.approx(0.05, abs=1e-6)
    assert t["fx_usdkrw"] > 0


def test_correlation_group_limit():
    rm = RiskManager(RiskCfg())
    kw = dict(market="upbit", price=100, atr=1, fx=1, fee_rate=0, equity_krw=1e7, cash_krw=1e7, n_open=1,
              trades_today=0, day_pnl_pct=0)
    assert not rm.plan_buy(**kw, group_open=1, max_per_group=1).ok
    assert rm.plan_buy(**kw, group_open=0, max_per_group=1).ok


def test_market_risk_multiplier_shrinks_size():
    base = dict(price=100, atr=1, fx=1, fee_rate=0, equity_krw=1e7, cash_krw=1e7, n_open=0, trades_today=0,
                day_pnl_pct=0)
    r = RiskCfg(max_position_pct=50, market_risk_mult={"upbit": 0.5, "kis_kr": 1.0, "kis_us": 1.0})
    a = RiskManager(r).plan_buy(market="kis_kr", **base).qty
    b = RiskManager(r).plan_buy(market="upbit", **base).qty
    assert b == pytest.approx(a / 2, rel=0.01)


# ── calendars ────────────────────────────────────────────────
def test_nyse_2026_holidays():
    h = nyse_holidays(2026)
    assert date(2026, 11, 26) in h and date(2026, 7, 3) in h and date(2026, 4, 3) in h and date(2026, 11, 27) not in h
    assert date(2022, 12, 31) not in nyse_holidays(2022)  # Jan 1 2022 was a Saturday: no Friday close


def test_krx_calendar_uses_kis_once_per_day():
    class K:
        calls = 0
        def kr_holidays(self, start):
            K.calls += 1
            return {date(2026, 12, 31): False, date(2026, 12, 30): True}
    cal = Calendar(K(), krx_extra=["2026-12-29"])
    assert cal.is_trading_day("kis_kr", date(2026, 12, 30))
    assert not cal.is_trading_day("kis_kr", date(2026, 12, 31))
    assert not cal.is_trading_day("kis_kr", date(2026, 12, 29))  # from config
    assert not cal.is_trading_day("kis_kr", date(2026, 12, 26))  # Saturday
    assert K.calls == 1


# ── monitors ─────────────────────────────────────────────────
def test_drawdown_auto_pause_and_alert():
    eng, _ = engine_with_buy()
    eng._cfg.monitor = MonitorCfg(max_drawdown_pct=5)
    eng.monitor.drift(10_000_000)
    eng.monitor.drift(9_400_000)
    assert eng.paused and any("auto-paused" in m for m in eng.notifier.msgs)
    n = len(eng.notifier.msgs)
    eng.set_paused(False)
    eng.monitor.drift(9_300_000)          # slightly worse — already acknowledged, no new pause
    assert not eng.paused and len(eng.notifier.msgs) == n
    eng.monitor.drift(8_900_000)          # another half-limit deeper → pauses again
    assert eng.paused


def test_profit_factor_drift_pause():
    eng, _ = engine_with_buy()
    eng._cfg.monitor = MonitorCfg(drift_min_trades=5, min_profit_factor=0.8, max_drawdown_pct=99)
    for i in range(5):
        eng.store.add_trade(key="k", symbol="X", market="upbit", side="SELL", qty=1, price=1, value_krw=1,
                            fee_krw=0, pnl_krw=-1000 if i else 100, reason="t", mode="paper")
    eng.monitor.drift(10_000_000)
    assert eng.paused


def test_daily_summary_once(monkeypatch):
    eng, _ = engine_with_buy()
    eng._cfg.alerts.summary_time = "00:00"
    eng.monitor.daily_summary(); eng.monitor.daily_summary()
    assert sum("daily summary" in m for m in eng.notifier.msgs) == 1


def test_reconcile_finds_missing_position():
    eng, _ = engine_with_buy()
    eng.tick()

    class U:
        access_key = "x"
        def accounts(self):
            return [{"currency": "KRW", "balance": "1", "locked": "0"}]
    eng.data.upbit, eng.data.kis = U(), None
    problems = eng.reconcile()
    assert problems and "KRW-TEST" in problems[0]


def test_retest_runs_in_background():
    eng, _ = engine_with_buy()
    eng.history = lambda s, bars: long_history(900)
    eng._cfg.symbols[0].params = NO_FILTERS
    assert eng.monitor.start_retest()
    eng.monitor._retest_thread.join(60)
    r = eng.store.get("retest:upbit:KRW-TEST")
    assert r and "pass" in r and any("re-test" in m for m in eng.notifier.msgs)


# ── strategies / portfolio ───────────────────────────────────
def test_dip_buy_entry_and_exit():
    rng = np.random.default_rng(1)
    closes = 100 * np.exp(np.linspace(0, 0.6, 80) + rng.normal(0, 0.004, 80))
    closes[-3:] = closes[-4] * np.array([0.985, 0.97, 0.955])  # sharp 3-candle dip, still above the 50-candle average
    df = candles_from(closes)
    s = DipBuy(long_ma=50)
    sig = s.evaluate(df, None, closes[-1])
    assert sig.action == "BUY", sig.reason
    from app.models import Position
    p = Position("k", "s", "upbit", 1, closes[-1], 0, 0, "", df.index[-1])
    bounce = candles_from(np.r_[closes, closes[-1] * 1.08])
    assert s.evaluate(bounce, p, 0).action == "SELL"
    falling = 100 * np.exp(np.linspace(0, -0.3, 80))
    falling[-3:] *= np.array([0.97, 0.94, 0.91])
    assert s.evaluate(candles_from(falling), None, 1).action == "HOLD"


def test_portfolio_respects_shared_limits():
    specs = []
    for k in range(4):
        df = long_history(600, seed=10 + k)
        specs.append({"key": f"upbit:S{k}", "candles": df, "strategy": "trend_rsi", "market": "upbit",
                      "params": NO_FILTERS, "fx": 1.0, "fee_rate": 0.0005})
    free = run_portfolio(specs, RiskCfg(max_open_positions=4), 10_000_000)
    tight = run_portfolio(specs, RiskCfg(max_open_positions=1), 10_000_000,
                          groups=[["upbit:S0", "upbit:S1"]], max_per_group=1)
    assert free["trades"] > tight["trades"] > 0
    assert set(free["per_symbol"]) == {s["key"] for s in specs}


# ── tax ──────────────────────────────────────────────────────
def test_tax_rows_krw_and_usd():
    trades = [
        {"id": 1, "key": "kis_us:AAPL", "symbol": "AAPL", "market": "kis_us", "side": "BUY", "qty": 10, "price": 200,
         "fee_krw": 7000, "fx_usdkrw": 1400, "ts": "2026-01-05T01:00:00+00:00", "mode": "paper"},
        {"id": 2, "key": "kis_us:AAPL", "symbol": "AAPL", "market": "kis_us", "side": "SELL", "qty": 10, "price": 220,
         "fee_krw": 7500, "fx_usdkrw": 1350, "ts": "2026-02-05T01:00:00+00:00", "opened_at": "2026-01-05T01:00:00+00:00",
         "mode": "paper"},
    ]
    r = closed_trades(trades, 2026)[0]
    assert r["cost_basis_krw"] == 2000 * 1400 + 7000 and r["proceeds_krw"] == 2200 * 1350 - 7500
    assert r["gain_usd"] == pytest.approx(200 - 5 - 7500 / 1350, abs=0.01) and r["holding_days"] == 31
    assert to_csv([r]).startswith("symbol,market")
    assert closed_trades(trades, 2025) == []


def test_fx_offline_uses_fallback_and_cache():
    st = Store(":memory:")
    fx = FXRates(st, 1400, online=False)
    assert fx.usd_krw() == 1400
    st.set(f"fx:{datetime.now(timezone.utc).date().isoformat()}", 1380.5)
    assert fx.usd_krw() == 1380.5


# ── security ─────────────────────────────────────────────────
def test_totp_verify_and_replay():
    sec = new_totp_secret()
    v = TOTPVerifier(sec)
    code = totp_now(sec)
    assert v.verify(code) and not v.verify(code)       # same code can't be used twice
    assert not TOTPVerifier(sec).verify("000000") or totp_now(sec) == "000000"


def test_vault_roundtrip():
    blob = encrypt_values({"UPBIT_SECRET_KEY": "s3cret"}, "correct horse battery")
    assert b"s3cret" not in blob
    assert decrypt_bytes(blob, "correct horse battery") == {"UPBIT_SECRET_KEY": "s3cret"}
    with pytest.raises(ValueError):
        decrypt_bytes(blob, "wrong passphrase!!")
    assert parse_env("A=1\n# c\nB = 'x' # note\nC=\n") == {"A": "1", "B": "x"}


def _client(eng):
    return TestClient(build_app(eng.cfg, eng, run_loop=False))


def test_login_with_two_step_and_lockout():
    eng, _ = engine_with_buy()
    sec = new_totp_secret()
    eng.base_cfg.secrets.totp_secret = sec
    with _client(eng) as c:
        assert c.get("/api/login-info").json()["two_step"]
        assert c.post("/api/login", json={"token": "secret", "code": "123"}).status_code == 401
        ok = c.post("/api/login", json={"token": "secret", "code": totp_now(sec), "device": "Galaxy"})
        assert ok.status_code == 200
        h = {"Authorization": f"Bearer {ok.json()['session']}"}
        assert c.get("/api/sessions", headers=h).json()[0]["device"] == "Galaxy"
        for _ in range(5):
            c.post("/api/login", json={"token": "nope"})
        assert c.post("/api/login", json={"token": "secret", "code": totp_now(sec)}).status_code == 429
        assert c.post("/api/logout", headers=h).status_code == 200
        assert c.get("/api/status", headers=h).status_code == 401


def test_healthz():
    eng, _ = engine_with_buy()
    with _client(eng) as c:
        assert c.get("/healthz").status_code == 503      # never ticked
        eng.tick()
        assert c.get("/healthz").status_code == 200


# ── settings from the phone ──────────────────────────────────
def test_settings_api_edit_validate_and_protect_open_positions():
    eng, _ = engine_with_buy()
    with _client(eng) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"token": "secret"}).json()["session"]}
        r = c.post("/api/settings", headers=h, json={"risk": {"max_position_pct": 5}, "regime_enabled": False})
        assert r.status_code == 200 and eng.cfg.risk.max_position_pct == 5 and not eng.cfg.regime.enabled
        assert c.post("/api/settings", headers=h, json={"risk": {"max_position_pct": 90}}).status_code == 400
        assert c.post("/api/settings", headers=h, json={"risk": {"usd_krw": 1}}).status_code == 400
        c.post("/api/tick", headers=h)
        assert eng.store.positions()
        assert c.post("/api/settings", headers=h, json={"symbols": []}).status_code == 400  # can't drop held symbol
        syms = c.get("/api/settings", headers=h).json()["symbols"]
        syms[0]["enabled"] = False
        assert c.post("/api/settings", headers=h, json={"symbols": syms}).status_code == 200
        assert not eng.cfg.symbols[0].enabled
        assert c.post("/api/settings/reset", headers=h).json()["risk"]["max_position_pct"] == 10


def test_overrides_survive_restart():
    eng, _ = engine_with_buy()
    eng.apply_settings({"risk": {"trail_atr": 4.5}})
    from app.engine import Engine
    again = Engine(eng.base_cfg, eng.store, eng.data, eng.ex, eng.ai)
    assert again.cfg.risk.trail_atr == 4.5


def test_journal_chart_and_tax_endpoint():
    eng, _ = engine_with_buy()
    eng.tick()
    pos = eng.store.position("upbit:KRW-TEST")
    eng.data.px = pos.stop * 0.99
    eng.tick()
    with _client(eng) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"token": "secret"}).json()["session"]}
        j = c.get("/api/journal", headers=h).json()
        assert len(j) == 1 and j[0]["pnl_krw"] < 0
        ch = c.get(f"/api/journal/{j[0]['id']}/chart", headers=h)
        assert ch.status_code == 200 and len(ch.json()["close"]) > 50
        csv = c.get("/api/export/tax.csv", headers=h)
        assert csv.status_code == 200 and "KRW-TEST" in csv.text
