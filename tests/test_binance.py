"""Binance connector: signing, candles, order fills and fees, and how the bot routes Binance symbols."""
import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode

import pytest

from app.brokers.binance import BinanceClient, BinanceError
from app.config import Config, Secrets, SignalsCfg, SymbolCfg, is_crypto, is_usd
from app.execution import LiveExecutor
from app.market import MarketData, market_open
from app.risk import round_qty

INFO = {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
                     "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                                 {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0"},
                                 {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


class FakeSession:
    """Answers like api.binance.com and records every call."""

    def __init__(self, balances=None):
        self.calls = []
        self.balances = balances or {"USDT": 1000.0, "BTC": 0.5}

    def _answer(self, method, url, params, headers):
        self.calls.append((method, url.split(".com")[1], dict(params or {}), dict(headers or {})))
        path = url.split(".com")[1]
        if path == "/api/v3/exchangeInfo":
            return Resp(INFO)
        if path == "/api/v3/klines":
            limit, end = params["limit"], params.get("endTime")
            newest = (end + 1) if end is not None else 3_600_000 * 2000
            opens = [newest - 3_600_000 * i for i in range(limit, 0, -1)]
            opens = [t for t in opens if t >= 0]
            return Resp([[t, "1", "2", "0.5", str(t / 3_600_000), "10", 0, "0", 0, "0", "0", "0"] for t in opens])
        if path == "/api/v3/ticker/price":
            return Resp({"symbol": params["symbol"], "price": "95000.5"})
        if path == "/api/v3/ticker/bookTicker":
            return Resp({"bidPrice": "94999", "askPrice": "95001"})
        if path == "/api/v3/account":
            return Resp({"balances": [{"asset": a, "free": str(v), "locked": "0"} for a, v in self.balances.items()]})
        if path == "/api/v3/order":
            if params["side"] == "BUY":
                return Resp({"orderId": 7, "status": "FILLED", "side": "BUY", "executedQty": "0.002",
                             "cummulativeQuoteQty": "190",
                             "fills": [{"price": "95000", "qty": "0.002", "commission": "0.000002", "commissionAsset": "BTC"}]})
            return Resp({"orderId": 8, "status": "FILLED", "side": "SELL", "executedQty": params["quantity"],
                         "fills": [{"price": "96000", "qty": params["quantity"], "commission": "0.19",
                                    "commissionAsset": "USDT"}]})
        return Resp({"code": -1, "msg": "unknown path"}, 404)

    def get(self, url, params=None, headers=None, timeout=None):
        return self._answer("GET", url, params, headers)

    def request(self, method, url, params=None, headers=None, timeout=None, json=None):
        return self._answer(method, url, params, headers)


def client(**kw):
    return BinanceClient("key", "secret", session=FakeSession(**kw))


def test_markets_are_classified():
    assert is_crypto("binance") and is_usd("binance") and is_usd("kis_us") and not is_usd("upbit")
    assert market_open("binance") and round_qty(0.123456789, "binance") == 0.12345678


def test_signed_requests_carry_a_valid_signature():
    c = client()
    c.free("USDT")
    _, path, params, headers = c.s.calls[-1]
    assert path == "/api/v3/account" and headers["X-MBX-APIKEY"] == "key"
    sig = params.pop("signature")
    assert sig == hmac.new(b"secret", urlencode(params).encode(), hashlib.sha256).hexdigest()


def test_keys_required_for_account_calls():
    with pytest.raises(BinanceError, match="keys are not set"):
        BinanceClient(session=FakeSession()).free()


def test_candles_page_back_and_come_out_oldest_first():
    df = client().candles("BTCUSDT", "60m", 1500)
    assert len(df) == 1500 and df.index.is_monotonic_increasing
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_buy_fill_nets_out_fee_taken_in_coin():
    f = client().buy_market_quote("BTCUSDT", 190)
    assert f["qty"] == pytest.approx(0.001998)              # 0.002 bought, 0.000002 BTC paid as fee
    assert f["avg_price"] == pytest.approx(95000) and f["fee_usdt"] == pytest.approx(0.19)


def test_sell_quantity_is_floored_to_step():
    c = client()
    f = c.sell_market("BTCUSDT", 0.0123456)
    assert c.s.calls[-1][2]["quantity"] == "0.01234"          # LOT_SIZE step 0.00001
    assert f["qty"] == pytest.approx(0.01234) and f["fee_usdt"] == pytest.approx(0.19)


def test_location_block_is_explained():
    c = client()
    c.s.get = lambda *a, **k: Resp({"code": 0, "msg": "restricted"}, 451)
    with pytest.raises(BinanceError, match="refuses service from this location"):
        c.price("BTCUSDT")


def test_live_executor_routes_binance_orders_in_krw():
    cfg = Config(secrets=Secrets(dashboard_token="x"))
    b = client()
    ex = LiveExecutor(cfg, None, None, b, fx=lambda: 1400.0)
    s = SymbolCfg("BTCUSDT", "binance", "60m", "trend_rsi")
    assert ex.cash_krw("binance") == pytest.approx(1000 * 1400)
    fill = ex.buy(s, 0.002, 95000, 1400.0)
    assert fill.ok and fill.qty == pytest.approx(0.001998) and fill.fee_krw == pytest.approx(0.19 * 1400)
    sell = ex.sell(s, 0.9, 96000, 1400.0)                     # more than held → capped at the 0.5 BTC balance
    assert sell.ok and sell.qty == pytest.approx(0.5)


def test_market_data_routes_binance_symbols():
    cfg = Config(data_source="live", secrets=Secrets(dashboard_token="x"), signals=SignalsCfg(enabled=False))
    md = MarketData(cfg, binance=client(), calendar=object())
    s = SymbolCfg("BTCUSDT", "binance", "60m", "trend_rsi")
    assert md.price(s) == pytest.approx(95000.5)
    assert md.spread_pct(s) == pytest.approx(2 / 95000 * 100)
    df, forming = md.candles(s, 100)
    assert forming and len(df) == 100
    md.cfg.usd_krw = 1350.0
    assert md.fx(s) == 1350.0                                  # USDT priced → converted like US stocks


def test_upbit_premium_does_not_block_binance_buys():
    from app.signals import MarketSignals
    cfg = SignalsCfg(enabled=True)
    ms = MarketSignals(lambda: type("C", (), {"signals": cfg})(), fx_rate=lambda: 1400.0, demo=True)
    checked = []
    for name in ("etf_flows", "funding", "coinbase_premium", "fear_greed", "upbit_premium", "macro"):
        setattr(ms, name, (lambda n: (lambda *a: checked.append(n) or type("R", (), {"action": None})()))(name))
    ms.check("binance")
    assert "upbit_premium" not in checked and "funding" in checked
    checked.clear()
    ms.check("upbit")
    assert "upbit_premium" in checked


def test_paper_account_can_start_over():
    from fastapi.testclient import TestClient
    from app.server import build_app
    from tests.test_round4 import engine_at_setup
    eng, _ = engine_at_setup()
    eng.store.set("paper_cash", 1.0)
    eng.store.set("last_bar:upbit:KRW-BTC", "x")
    eng.store.set("ai_enabled_note", "kept")
    with TestClient(build_app(eng.cfg, eng, run_loop=False)) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"token": "secret"}).json()["session"]}
        assert c.post("/api/paper/reset", json={"confirm": "nope"}, headers=h).status_code == 400
        assert c.post("/api/paper/reset", json={"confirm": "RESET"}, headers=h).status_code == 200
    assert eng.store.get("paper_cash") == eng.cfg.starting_cash_krw
    assert eng.store.get("last_bar:upbit:KRW-BTC") is None and eng.store.get("ai_enabled_note") == "kept"
