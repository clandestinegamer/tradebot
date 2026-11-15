import hashlib
from types import SimpleNamespace
from urllib.parse import unquote, urlencode

import jwt
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.ai_review import AIReviewer, _parse
from app.backtest import expand_grid, regime_by_date, run_backtest, walk_forward
from app.brokers.upbit import UpbitClient
from app.config import AICfg, Config, RiskCfg, Secrets, SignalsCfg, SymbolCfg
from app.engine import Engine, trailed_stop
from app.execution import PaperExecutor
from app.indicators import adx, atr, rsi
from app.models import Position, Signal
from app.risk import RiskManager
from app.server import build_app
from app.storage import Store
from app.strategies import TrendRSI, get_strategy

NO_FILTERS = {"long_ma": 0, "adx_min": 0, "vol_mult": 0}


# ── helpers ──────────────────────────────────────────────────
def candles_from(closes):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998,
                         "close": c, "volume": 1000.0}, index=[f"b{i:04d}" for i in range(len(c))])


def v_shape(n_down=60, n_up=60, start=100.0, seed=1):
    down = start * np.exp(np.linspace(0, -0.25, n_down))
    up = down[-1] * np.exp(np.linspace(0, 0.35, n_up))
    base = np.r_[down, up[1:]]
    noise = np.random.default_rng(seed).normal(0, 0.01, len(base))  # real prices zig-zag
    return base * np.exp(noise)


class FakeData:
    """Market data the test controls."""
    def __init__(self, closes):
        self.df = candles_from(closes)
        self.i = len(closes)
        self.px = None
        self.regime_ok = True

    def is_open(self, s): return True
    def regime(self, market): return (self.regime_ok, "test benchmark " + ("above" if self.regime_ok else "below") + " average")
    def bar_progress(self, s, ts): return 1.0
    def entry_block(self, s): return self.block
    def fx_market(self, m): return 1.0
    def daily_closes(self, s, n): return self.daily.get(s.key, self.df["close"]).tail(n)
    daily = {}
    block = None
    demo = None
    def fx(self, s): return 1.0
    def candles(self, s, count=200): return self.df.iloc[: self.i], False
    def price(self, s): return self.px if self.px is not None else float(self.df["close"].iloc[self.i - 1])


class FakeAI:
    def __init__(self, decision="approve", conf=0.9):
        self.d, self.c = decision, conf
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        txt = f'Checked news. {{"decision": "{self.d}", "confidence": {self.c}, "reason": "test"}}'
        return SimpleNamespace(content=[SimpleNamespace(text=txt)])


def make_engine(closes, ai_decision="approve", ai_enabled=True, params=NO_FILTERS, risk=None):
    sym = SymbolCfg("KRW-TEST", "upbit", "60m", "trend_rsi", params=dict(params))
    cfg = Config(symbols=[sym], risk=risk or RiskCfg(), ai=AICfg(enabled=ai_enabled, web_search=False),
                 secrets=Secrets(dashboard_token="secret"), starting_cash_krw=10_000_000,
                 signals=SignalsCfg(enabled=False),
                 weekly_review=False)  # otherwise Sunday-evening test runs also call the (fake) AI
    store = Store(":memory:")
    fake_ai = FakeAI(ai_decision)
    eng = Engine(cfg, store, FakeData(closes), PaperExecutor(cfg, store), AIReviewer(cfg.ai, "", client=fake_ai))
    return eng, fake_ai


def first_buy_index(closes, params=NO_FILTERS):
    s = TrendRSI(**params)
    for i in range(s.min_bars, len(closes)):
        if s.evaluate(candles_from(closes[:i]), None, closes[i - 1]).action == "BUY":
            return i
    return None


# ── indicators ───────────────────────────────────────────────
def test_rsi_extremes():
    up = pd.Series(np.arange(1, 50, dtype=float))
    assert rsi(up).iloc[-1] == pytest.approx(100)
    dn = pd.Series(np.arange(50, 1, -1, dtype=float))
    assert rsi(dn).iloc[-1] == pytest.approx(0, abs=1e-6)


def test_atr_positive():
    assert (atr(candles_from(v_shape())).dropna() > 0).all()


# ── risk ─────────────────────────────────────────────────────
def test_risk_sizing_respects_caps():
    rm = RiskManager(RiskCfg(max_position_pct=10, risk_per_trade_pct=1, stop_loss_atr=2, take_profit_atr=4))
    p = rm.plan_buy(market="kis_kr", price=50_000, atr=1_000, fx=1, fee_rate=0.001, equity_krw=10_000_000,
                    cash_krw=10_000_000, n_open=0, trades_today=0, day_pnl_pct=0)
    assert p.ok
    assert p.qty == 20                      # 10% cap: ₩1M / ₩50k = 20 (risk cap would allow 50)
    assert p.qty * (50_000 - p.stop) <= 100_000 + 1
    assert p.stop == 48_000 and p.target == 54_000
    no_target = RiskManager(RiskCfg(take_profit_atr=0)).plan_buy(
        market="kis_kr", price=50_000, atr=1_000, fx=1, fee_rate=0.001, equity_krw=10_000_000,
        cash_krw=10_000_000, n_open=0, trades_today=0, day_pnl_pct=0)
    assert no_target.ok and no_target.target == 0  # default: trailing stop instead of a fixed target


def test_risk_blocks():
    rm = RiskManager(RiskCfg(daily_loss_limit_pct=3, max_open_positions=2))
    kw = dict(market="upbit", price=100, atr=1, fx=1, fee_rate=0, equity_krw=1e6, cash_krw=1e6, trades_today=0)
    assert not rm.plan_buy(**kw, n_open=0, day_pnl_pct=-3.5).ok
    assert not rm.plan_buy(**kw, n_open=2, day_pnl_pct=0).ok
    small = rm.plan_buy(market="upbit", price=100, atr=1, fx=1, fee_rate=0, equity_krw=20_000, cash_krw=20_000,
                        n_open=0, trades_today=0, day_pnl_pct=0)
    assert not small.ok and "too small" in small.reason


# ── strategy ─────────────────────────────────────────────────
def test_trend_rsi_buys_after_reversal_and_not_in_downtrend():
    closes = v_shape()
    i = first_buy_index(closes)
    assert i is not None and i > 60  # only once the uptrend is established
    down_only = 100 * np.exp(np.linspace(0, -0.4, 120))
    assert first_buy_index(down_only) is None


# ── engine ───────────────────────────────────────────────────
def test_engine_buy_then_stop_loss():
    closes = v_shape()
    i = first_buy_index(closes)
    eng, ai = make_engine(closes[:i])
    eng.tick()
    pos = eng.store.position("upbit:KRW-TEST")
    assert pos is not None and ai.calls == 1
    assert eng.store.trades()[0]["ai_decision"] == "approve"
    cash_after_buy = eng.ex.cash_krw()
    assert cash_after_buy < 10_000_000
    # crash below the stop → sold on the next tick
    eng.data.px = pos.stop * 0.995
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None
    sell = eng.store.trades()[0]
    assert sell["side"] == "SELL" and sell["pnl_krw"] < 0 and "stop-loss" in sell["reason"]


def test_engine_ai_veto_blocks_buy():
    closes = v_shape()
    eng, ai = make_engine(closes[: first_buy_index(closes)], ai_decision="reject")
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None
    assert eng.store.signals()[0]["outcome"].startswith("AI reject")


def test_engine_low_confidence_is_rejected():
    closes = v_shape()
    eng, ai = make_engine(closes[: first_buy_index(closes)])
    ai.c = 0.3
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None


def test_engine_ai_unreachable_fails_closed():
    closes = v_shape()
    eng, _ = make_engine(closes[: first_buy_index(closes)])
    eng.ai = AIReviewer(AICfg(enabled=True), api_key="")  # no key
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None
    assert "AI error" in eng.store.signals()[0]["outcome"]


def test_engine_paused_blocks_buy_and_same_bar_not_reprocessed():
    closes = v_shape()
    eng, ai = make_engine(closes[: first_buy_index(closes)])
    eng.set_paused(True)
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None
    eng.set_paused(False)
    eng.tick()  # same closed candle → not re-evaluated
    assert eng.store.position("upbit:KRW-TEST") is None and ai.calls == 0


def test_kill_switch_sells_everything():
    closes = v_shape()
    eng, _ = make_engine(closes[: first_buy_index(closes)])
    eng.tick()
    assert eng.store.positions()
    eng.kill()
    assert not eng.store.positions() and eng.paused


# ── broker auth ──────────────────────────────────────────────
def test_upbit_jwt_query_hash():
    c = UpbitClient("ak", "sk")
    params = {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": "10000"}
    tok = c._auth_header(params)["Authorization"].split()[1]
    body = jwt.decode(tok, "sk", algorithms=["HS256"])
    assert body["access_key"] == "ak" and body["query_hash_alg"] == "SHA512"
    assert body["query_hash"] == hashlib.sha512(unquote(urlencode(params)).encode()).hexdigest()


def test_ai_parse():
    assert _parse('blah {"decision":"reject","confidence":0.8,"reason":"earnings tomorrow"} end')["decision"] == "reject"


# ── backtest ─────────────────────────────────────────────────
def test_backtest_runs():
    closes = np.r_[v_shape(), v_shape(start=110)[::-1], v_shape(start=90)]
    r = run_backtest(candles_from(closes), "trend_rsi", "upbit", RiskCfg(), 0.0005, params=NO_FILTERS)
    assert r["trades"] >= 1 and np.isfinite(r["return_pct"]) and r["max_drawdown_pct"] <= 0
    r2 = run_backtest(candles_from(closes), "vol_breakout", "upbit", RiskCfg(), 0.0005)
    assert np.isfinite(r2["return_pct"])


# ── API ──────────────────────────────────────────────────────
def test_api_auth_and_kill_confirmation():
    closes = v_shape()
    eng, _ = make_engine(closes[: first_buy_index(closes)])
    app = build_app(eng.cfg, eng, run_loop=False)
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Bearer secret"}).status_code == 401  # password ≠ session
        sid = c.post("/api/login", json={"token": "secret"}).json()["session"]
        h = {"Authorization": f"Bearer {sid}"}
        c.post("/api/tick", headers=h)
        st = c.get("/api/status", headers=h).json()
        assert st["mode"] == "paper" and len(st["positions"]) == 1
        assert c.post("/api/kill", headers=h, json={"confirm": "yes"}).status_code == 400
        assert c.post("/api/kill", headers=h, json={"confirm": "SELL ALL"}).status_code == 200
        assert c.get("/api/status", headers=h).json()["positions"] == []
        assert c.get("/").status_code == 200


# ── upgrades: filters ────────────────────────────────────────
def test_adx_high_in_trend_low_in_chop():
    trend = candles_from(100 * np.exp(np.linspace(0, 0.5, 150)) * np.exp(np.random.default_rng(2).normal(0, 0.003, 150)))
    chop = candles_from(100 * np.exp(np.random.default_rng(3).normal(0, 0.01, 150)))
    assert adx(trend).iloc[-1] > 30 > adx(chop).iloc[-1]


def test_volume_filter_blocks_then_allows():
    closes = v_shape()
    i = first_buy_index(closes)
    df = candles_from(closes[:i])
    params = {**NO_FILTERS, "vol_mult": 1.5}
    sig = TrendRSI(**params).evaluate(df, None, closes[i - 1])
    assert sig.action == "HOLD" and sig.filtered and "volume" in sig.reason
    df.iloc[-1, df.columns.get_loc("volume")] = 5000  # heavy volume on the trigger candle
    assert TrendRSI(**params).evaluate(df, None, closes[i - 1]).action == "BUY"


def test_long_ma_filter_needs_history():
    s = TrendRSI()
    assert s.min_bars == 210
    assert "warming up" in s.evaluate(candles_from(v_shape()), None, 1.0).reason


def test_unknown_param_rejected():
    with pytest.raises(TypeError):
        get_strategy("trend_rsi", {"adx_minimum": 20})


def test_engine_regime_blocks_buy():
    closes = v_shape()
    eng, ai = make_engine(closes[: first_buy_index(closes)])
    eng.data.regime_ok = False
    eng.tick()
    assert eng.store.position("upbit:KRW-TEST") is None and ai.calls == 0
    assert "market regime" in eng.store.signals()[0]["outcome"]


def test_engine_logs_filtered_setup_once():
    closes = v_shape()
    eng, _ = make_engine(closes[: first_buy_index(closes)], params={**NO_FILTERS, "vol_mult": 1.5})
    eng.tick(); eng.tick()
    sigs = eng.store.signals()
    assert len(sigs) == 1 and sigs[0]["outcome"] == "filtered"


# ── upgrades: trailing stops ─────────────────────────────────
def test_trailed_stop_breakeven_then_trail():
    risk = RiskCfg(stop_loss_atr=2, trail_atr=3, breakeven_r=1)
    p = Position("k", "s", "upbit", 1, 100.0, 90.0, 0.0, "", initial_stop=90.0, highest=100.0, entry_atr=5.0)
    assert trailed_stop(p, 105, risk, 0.0005) == 90.0           # not yet +1R
    be = trailed_stop(p, 110, risk, 0.0005)                       # +1R → break-even (+fees)
    assert 100 < be < 100.2
    p.highest = 130
    assert trailed_stop(p, 125, risk, 0.0005) == pytest.approx(115)  # 130 − 3×5


def test_engine_trailing_stop_locks_in_profit():
    closes = v_shape()
    eng, _ = make_engine(closes[: first_buy_index(closes)])
    eng.tick()
    pos = eng.store.position("upbit:KRW-TEST")
    first_stop = pos.stop
    eng.data.px = pos.avg_price + 6 * pos.entry_atr     # rally
    eng.tick()
    raised = eng.store.position("upbit:KRW-TEST").stop
    assert raised > pos.avg_price > first_stop           # stop now above entry
    eng.data.px = raised * 0.999                          # pull back through it
    eng.tick()
    sell = eng.store.trades()[0]
    assert sell["side"] == "SELL" and "trailing stop" in sell["reason"] and sell["pnl_krw"] > 0


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE positions (key TEXT PRIMARY KEY, symbol TEXT, market TEXT, qty REAL, avg_price REAL, "
                "stop REAL, target REAL, opened_at TEXT, entry_bar TEXT, entry_reason TEXT)")
    con.execute("INSERT INTO positions VALUES ('upbit:X','X','upbit',1,100,90,120,'t','b','r')")
    con.commit(); con.close()
    p = Store(str(db)).position("upbit:X")
    assert p.initial_stop == 0 and p.stop == 90


# ── upgrades: walk-forward ───────────────────────────────────
def long_history(n=900, seed=5):
    rng = np.random.default_rng(seed)
    drift = 0.004 * np.sin(np.arange(n) / 40)
    closes = 100 * np.exp(np.cumsum(rng.normal(drift, 0.015)))
    df = candles_from(closes)
    df["volume"] = rng.integers(500, 2000, n).astype(float)
    return df


def test_backtest_matches_between_prepared_and_fresh():
    df = long_history(400)
    a = run_backtest(df, "trend_rsi", "upbit", RiskCfg(), 0.0005, params=NO_FILTERS)
    s = get_strategy("trend_rsi", NO_FILTERS)
    b = run_backtest(df, "trend_rsi", "upbit", RiskCfg(), 0.0005, params=NO_FILTERS, prepared=s.prepare(df))
    assert a["return_pct"] == b["return_pct"] and a["trades"] == b["trades"]


def test_walk_forward_runs_and_reports():
    df = long_history()
    wf = walk_forward(df, "trend_rsi", "upbit", RiskCfg(), 0.0005, folds=3,
                      grid=[NO_FILTERS, {**NO_FILTERS, "adx_min": 20}])
    assert len(wf["folds"]) == 3
    for f in wf["folds"]:
        assert f["test"][0] > f["train"][1]          # tested only on data after the tuning window
    assert np.isfinite(wf["oos_return_pct"]) and 0 < wf["param_stability"] <= 1
    assert len(expand_grid("trend_rsi")) == 24


def test_regime_by_date_uses_previous_day():
    bench = candles_from(np.r_[np.full(60, 100.0), np.full(5, 200.0)])
    bench.index = pd.date_range("2026-01-01", periods=65).strftime("%Y-%m-%d")
    r = regime_by_date(bench, 50)
    assert r["2026-03-02"] is False and r["2026-03-03"] is True  # price jumps on 03-02; usable from 03-03
