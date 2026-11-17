"""Loads config.toml (settings) and .env or secrets.enc (secrets)."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent

VALID_MARKETS = {"upbit", "binance", "kis_kr", "kis_us"}
CRYPTO_MARKETS = {"upbit", "binance"}   # trade 24/7, fractional quantities
USD_MARKETS = {"binance", "kis_us"}     # priced in USD (Binance: USDT, counted 1:1 as USD)


def is_crypto(market: str) -> bool:
    return market in CRYPTO_MARKETS


def is_usd(market: str) -> bool:
    return market in USD_MARKETS
VALID_STRATEGIES = {"trend_rsi", "vol_breakout", "dip_buy", "auto"}


@dataclass
class SymbolCfg:
    symbol: str
    market: str
    interval: str = "day"
    strategy: str = "trend_rsi"
    exchange: str = "NASD"  # only used for kis_us
    params: dict = field(default_factory=dict)  # strategy parameter overrides
    enabled: bool = True    # disabled symbols are not traded (open positions still get exits)
    inverse: bool = False   # inverse ETF: only bought when the market is FALLING, sold when it turns up

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"


@dataclass
class RiskCfg:
    max_position_pct: float = 10
    risk_per_trade_pct: float = 1.0
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 3
    max_trades_per_day: int = 10
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 0.0   # 0 = no fixed target; the trailing stop handles exits
    trail_atr: float = 3.0         # trailing stop distance below the highest price (0 = off)
    breakeven_r: float = 1.0       # move stop to break-even once up this many R (0 = off)
    min_order_krw: float = 5000
    max_total_risk_pct: float = 5.0   # combined loss if EVERY open stop were hit at once (0 = off)
    partial_r: float = 0.0            # take partial profit once up this many R (0 = off)
    partial_pct: float = 50.0         # …selling this % of the position
    pyramid_r: float = 0.0            # add to a winner once up this many R and its stop is at break-even (0 = off)
    pyramid_pct: float = 50.0         # …adding this % of the original size (once per trade)
    weekend_crypto_mult: float = 0.5  # crypto risk multiplier from Sat 00:00 to Mon 09:00 KST
    market_risk_mult: dict = field(default_factory=lambda: {"upbit": 1.0, "binance": 1.0, "kis_kr": 1.0, "kis_us": 1.0})


# fields a phone can change, with allowed ranges
EDITABLE_RISK = {
    "max_position_pct": (1, 50), "risk_per_trade_pct": (0.1, 5), "max_open_positions": (1, 20),
    "daily_loss_limit_pct": (0.5, 20), "max_trades_per_day": (1, 50), "stop_loss_atr": (0.5, 6),
    "take_profit_atr": (0, 20), "trail_atr": (0, 10), "breakeven_r": (0, 5),
    "max_total_risk_pct": (0, 20), "weekend_crypto_mult": (0, 1),
    "partial_r": (0, 10), "partial_pct": (10, 90), "pyramid_r": (0, 10), "pyramid_pct": (10, 100),
}


@dataclass
class RegimeCfg:
    """Only buy when the wider market is in an uptrend (benchmark daily close > its moving average)."""
    enabled: bool = True
    ma: int = 50
    upbit: str = "KRW-BTC"
    binance: str = "BTCUSDT"
    kis_kr: str = "069500"         # KODEX 200 ETF ≈ KOSPI 200
    kis_us: str = "SPY"
    kis_us_exchange: str = "AMEX"  # SPY trades on NYSE Arca → KIS code AMEX


@dataclass
class AICfg:
    enabled: bool = True
    model: str = "claude-sonnet-5-5"
    web_search: bool = True
    fail_open: bool = False
    min_confidence: float = 0.55


@dataclass
class ExecutionCfg:
    avoid_open_minutes: int = 15     # no new stock buys this many minutes after the open
    max_spread_pct: float = 0.3      # skip a buy if the bid-ask spread is wider than this %
    kr_order_type: str = "best_limit"  # KRX buys/sells: "best_limit" (최유리지정가) or "market"
    stale_seconds: int = 300         # skip buys if the last trade price is older than this
    us_retries: int = 2              # unfilled US limit order: re-price this many times…
    us_step_pct: float = 0.5         # …moving the limit this much further each time, then cancel the rest
    fast_stops: bool = True          # real-time stop checks (Upbit live feed; KIS positions every 10 s)


@dataclass
class CorrelationCfg:
    """Symbols in the same group move together, so they count as one bet."""
    groups: list = field(default_factory=list)  # e.g. [["upbit:KRW-BTC", "upbit:KRW-ETH"]]
    max_per_group: int = 1


@dataclass
class AlertsCfg:
    trades: bool = True            # message on every buy/sell
    stops: bool = True             # message when a stop moves to break-even
    errors: bool = True
    error_cooldown_minutes: int = 30
    summary_time: str = "21:00"    # daily summary, Korea time ("" = off)


@dataclass
class MonitorCfg:
    auto_pause: bool = True
    max_drawdown_pct: float = 10.0   # pause if equity falls this far from its peak
    drift_min_trades: int = 20       # judge win-rate/profit-factor drift after this many closed trades
    min_profit_factor: float = 0.8   # pause if the last N trades' profit factor falls below this
    reconcile: bool = True           # live mode: compare bot positions with the broker daily
    retest_day: int = 1              # day of month to re-run walk-forward tests (0 = off)
    retest_bars: int = 1500


@dataclass
class ShadowCfg:
    enabled: bool = True
    track: list = field(default_factory=lambda: ["ai_veto", "regime", "filtered", "vix", "macro", "etf_flows",
                                                 "funding", "cb_premium", "upbit_premium", "ranking",
                                                 "fear_greed", "krx_flows", "earnings"])
    max_days: int = 30               # stop tracking a hypothetical trade after this long


@dataclass
class SignalsCfg:
    """Market-condition filters. Each can only BLOCK (or shrink) buys; if its data can't be fetched it
    steps aside rather than blocking everything."""
    enabled: bool = True
    vix: bool = True
    vix_max: float = 30.0            # VIX above this → act
    vix_spike_pct: float = 20.0      # or VIX up this % in one day → act
    vix_action: str = "halve"        # "halve" position sizes or "block" stock buys
    macro: bool = True               # no buys on FOMC decision / CPI / jobs-report days (US date)
    macro_extra: list = field(default_factory=list)  # more event dates "YYYY-MM-DD"
    etf_flows: bool = True           # US spot Bitcoin ETF flows (data: The Commoner Bitcoin ETF Tracker by TFTC)
    etf_outflow_musd: float = 500.0  # block crypto buys if the last 3 sessions' net flow < −$500M
    funding: bool = True             # BTC perpetual funding rate (Binance, Bybit fallback)
    funding_max_pct: float = 0.05    # per 8h; above this the long side is crowded → block crypto buys
    coinbase_premium: bool = True
    cb_premium_min_pct: float = -0.15  # Coinbase below Binance by more than this → US selling → block crypto
    upbit_premium: bool = True
    upbit_premium_max_pct: float = 5.0  # Upbit above global price by more than this → overheated → block Upbit
    krx_flows: bool = True           # Korean stocks: foreigners + institutions must be net buyers lately
    krx_flow_days: int = 5
    earnings: bool = True            # US stocks: no buys within earnings_days of an earnings report (needs FINNHUB_API_KEY)
    earnings_days: int = 2
    fear_greed: bool = True          # crypto: no buys in "extreme greed" (data: Alternative.me)
    fng_max: int = 80


@dataclass
class RankingCfg:
    """Only buy the strongest few symbols (by return over lookback_days of daily candles)."""
    enabled: bool = False
    top_n: int = 3
    lookback_days: int = 63          # ≈ 3 months


@dataclass
class AllocationCfg:
    """Shift risk toward symbols that keep passing the monthly walk-forward re-test."""
    enabled: bool = False
    fail_mult: float = 0.5           # failed last re-test → half risk (0 = stop buying it)
    strong_mult: float = 1.25        # passed with a strong profit factor → a bit more risk
    strong_pf: float = 1.5


@dataclass
class ParkingCfg:
    """Park spare KIS cash in a money-market ETF so it earns interest (KRX hours only)."""
    enabled: bool = False
    symbol: str = "459580"           # KODEX CD금리액티브(합성) — one share costs about ₩1.07M
    keep_cash_pct: float = 20.0      # always keep this % of equity as cash (US buys happen while KRX is closed)


@dataclass
class CoreCfg:
    """Dual momentum: a slow, monthly 'core' holding in US stocks, international stocks or bonds."""
    enabled: bool = False
    pct: float = 50.0                # share of the account held in the core
    us: str = "SPY"
    intl: str = "VEU"
    bonds: str = "AGG"
    exchange: str = "AMEX"           # all three trade on NYSE Arca (KIS code AMEX)
    lookback_days: int = 252         # 12-month momentum
    cash_return_pct: float = 4.0     # annual; US stocks must beat this to count as 'trending up'
    rebalance_band_pct: float = 10.0 # top up / trim only if the holding drifts this far from target


@dataclass
class HolidaysCfg:
    krx_extra: list = field(default_factory=list)  # extra closed days "YYYY-MM-DD"
    us_extra: list = field(default_factory=list)


@dataclass
class Secrets:
    dashboard_token: str = ""
    totp_secret: str = ""
    upbit_access: str = ""
    upbit_secret: str = ""
    binance_key: str = ""
    binance_secret: str = ""
    kis_env: str = "demo"
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    kis_account_product: str = "01"
    anthropic_key: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    kakao_rest_key: str = ""
    kakao_refresh_token: str = ""
    healthcheck_url: str = ""
    finnhub_key: str = ""


SECRET_ENV = {
    "dashboard_token": "DASHBOARD_TOKEN", "totp_secret": "TOTP_SECRET",
    "upbit_access": "UPBIT_ACCESS_KEY", "upbit_secret": "UPBIT_SECRET_KEY",
    "binance_key": "BINANCE_API_KEY", "binance_secret": "BINANCE_API_SECRET",
    "kis_env": "KIS_ENV", "kis_app_key": "KIS_APP_KEY", "kis_app_secret": "KIS_APP_SECRET",
    "kis_account_no": "KIS_ACCOUNT_NO", "kis_account_product": "KIS_ACCOUNT_PRODUCT",
    "anthropic_key": "ANTHROPIC_API_KEY", "telegram_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID", "kakao_rest_key": "KAKAO_REST_API_KEY",
    "kakao_refresh_token": "KAKAO_REFRESH_TOKEN", "healthcheck_url": "HEALTHCHECK_URL",
    "finnhub_key": "FINNHUB_API_KEY",
}


@dataclass
class Config:
    trading_mode: str = "paper"
    data_source: str = "live"
    loop_seconds: int = 60
    starting_cash_krw: float = 10_000_000
    usd_krw: float = 1400
    demo_bar_seconds: int = 30
    history_bars: int = 300
    live_capital_krw: float = 0      # small-live start: the bot only uses this much (0 = whole account)
    paper_twin: bool = True          # in live mode, run a paper copy of the same settings to compare
    risk: RiskCfg = field(default_factory=RiskCfg)
    fees: dict = field(default_factory=lambda: {"upbit": 0.05, "binance": 0.10, "kis_kr": 0.10, "kis_us": 0.25})
    ai: AICfg = field(default_factory=AICfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    correlation: CorrelationCfg = field(default_factory=CorrelationCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    monitor: MonitorCfg = field(default_factory=MonitorCfg)
    shadow: ShadowCfg = field(default_factory=ShadowCfg)
    holidays: HolidaysCfg = field(default_factory=HolidaysCfg)
    signals: SignalsCfg = field(default_factory=SignalsCfg)
    ranking: RankingCfg = field(default_factory=RankingCfg)
    allocation: AllocationCfg = field(default_factory=AllocationCfg)
    parking: ParkingCfg = field(default_factory=ParkingCfg)
    weekly_review: bool = True       # Claude writes a short review every Sunday evening
    core: CoreCfg = field(default_factory=CoreCfg)
    dca: list = field(default_factory=list)       # scheduled buys, see config.toml
    grid: list = field(default_factory=list)      # crypto grids, see config.toml
    variants: list = field(default_factory=list)  # side-by-side paper accounts
    symbols: list[SymbolCfg] = field(default_factory=list)
    secrets: Secrets = field(default_factory=Secrets)
    db_path: str = str(ROOT / "data" / "tradebot.db")

    def fee_rate(self, market: str) -> float:
        return float(self.fees.get(market, 0.1)) / 100.0

    def symbol(self, key: str) -> SymbolCfg | None:
        return next((s for s in self.symbols if s.key == key), None)

    def group_of(self, key: str) -> list[str] | None:
        return next((g for g in self.correlation.groups if key in g), None)

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


def _section(cls, raw: dict):
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"[{cls.__name__}] unknown setting(s): {sorted(unknown)}")
    return cls(**raw)


def load_secrets() -> Secrets:
    """From the environment / .env, or from secrets.enc when TRADEBOT_PASSPHRASE is set."""
    values = {}
    enc = Path(os.environ.get("TRADEBOT_SECRETS", ROOT / "secrets.enc"))
    if enc.exists():
        from .vault import decrypt_file
        values = decrypt_file(enc, os.environ.get("TRADEBOT_PASSPHRASE", ""))
    e = lambda k: os.environ.get(k) or values.get(k, "")
    s = Secrets(**{name: e(env) for name, env in SECRET_ENV.items()})
    s.kis_env = s.kis_env or "demo"
    s.kis_account_product = s.kis_account_product or "01"
    return s


def load_config(path: str | os.PathLike | None = None, env_file: str | None = None) -> Config:
    path = Path(path or os.environ.get("TRADEBOT_CONFIG", ROOT / "config.toml"))
    if load_dotenv:
        load_dotenv(env_file or ROOT / ".env")
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    g = raw.get("general", {})
    risk_raw = dict(raw.get("risk", {}))
    risk = _section(RiskCfg, risk_raw)
    risk.market_risk_mult = {**RiskCfg().market_risk_mult, **risk_raw.get("market_risk_mult", {})}
    cfg = Config(
        trading_mode=g.get("trading_mode", "paper"),
        data_source=g.get("data_source", "live"),
        loop_seconds=int(g.get("loop_seconds", 60)),
        starting_cash_krw=float(g.get("starting_cash_krw", 10_000_000)),
        usd_krw=float(g.get("usd_krw", 1400)),
        demo_bar_seconds=int(g.get("demo_bar_seconds", 30)),
        history_bars=int(g.get("history_bars", 300)),
        live_capital_krw=float(g.get("live_capital_krw", 0)),
        paper_twin=bool(g.get("paper_twin", True)),
        risk=risk,
        fees={**Config().fees, **raw.get("fees", {})},
        ai=_section(AICfg, raw.get("ai", {})),
        regime=_section(RegimeCfg, raw.get("regime", {})),
        execution=_section(ExecutionCfg, raw.get("execution", {})),
        correlation=_section(CorrelationCfg, raw.get("correlation", {})),
        alerts=_section(AlertsCfg, raw.get("alerts", {})),
        monitor=_section(MonitorCfg, raw.get("monitor", {})),
        shadow=_section(ShadowCfg, raw.get("shadow", {})),
        holidays=_section(HolidaysCfg, raw.get("holidays", {})),
        signals=_section(SignalsCfg, raw.get("signals", {})),
        ranking=_section(RankingCfg, raw.get("ranking", {})),
        allocation=_section(AllocationCfg, raw.get("allocation", {})),
        parking=_section(ParkingCfg, raw.get("parking", {})),
        weekly_review=bool(g.get("weekly_review", True)),
        core=_section(CoreCfg, raw.get("core", {})),
        dca=list(raw.get("dca", [])),
        grid=list(raw.get("grid", [])),
        variants=list(raw.get("variants", [])),
        symbols=[SymbolCfg(**s) for s in raw.get("symbols", [])],
    )
    cfg.db_path = os.environ.get("TRADEBOT_DB", cfg.db_path)
    cfg.secrets = load_secrets()
    validate(cfg)
    return cfg


def apply_overrides(cfg: Config, ov: dict | None) -> Config:
    """Settings changed from the phone are stored in the database and layered over config.toml."""
    if not ov:
        return cfg
    out = copy.deepcopy(cfg)
    for k, v in (ov.get("risk") or {}).items():
        if k in EDITABLE_RISK:
            setattr(out.risk, k, type(getattr(out.risk, k))(v))
    if "regime_enabled" in ov:
        out.regime.enabled = bool(ov["regime_enabled"])
    if "ai_enabled" in ov:
        out.ai.enabled = bool(ov["ai_enabled"])
    if "signals_enabled" in ov:
        out.signals.enabled = bool(ov["signals_enabled"])
    if "ranking_enabled" in ov:
        out.ranking.enabled = bool(ov["ranking_enabled"])
    if ov.get("symbols") is not None:
        out.symbols = [SymbolCfg(**s) for s in ov["symbols"]]
    validate(out)
    return out


def symbols_to_dicts(symbols: list[SymbolCfg]) -> list[dict]:
    return [asdict(s) for s in symbols]


def validate(cfg: Config) -> None:
    if cfg.trading_mode not in ("paper", "live"):
        raise ValueError("trading_mode must be 'paper' or 'live'")
    if cfg.data_source not in ("live", "demo"):
        raise ValueError("data_source must be 'live' or 'demo'")
    if cfg.trading_mode == "live" and cfg.data_source == "demo":
        raise ValueError("live trading cannot use demo data")
    if cfg.signals.vix_action not in ("halve", "block"):
        raise ValueError("signals.vix_action must be 'halve' or 'block'")
    for d in cfg.dca:
        missing = {"symbol", "market", "amount_krw", "every"} - set(d)
        if missing:
            raise ValueError(f"[[dca]] entry {d} is missing {sorted(missing)}")
        if d["every"] not in ("day", "week", "month") or d["market"] not in VALID_MARKETS:
            raise ValueError(f"[[dca]] {d['symbol']}: every must be day/week/month and market valid")
    for g in cfg.grid:
        if not is_crypto(g.get("market", "upbit")):
            raise ValueError("grid trading is for crypto (Upbit or Binance) only")
        if not 0 < g["lower"] < g["upper"] or int(g["levels"]) < 2 or g["budget_krw"] <= 0:
            raise ValueError(f"[[grid]] {g.get('symbol')}: need 0 < lower < upper, levels ≥ 2, budget > 0")
    names = [v.get("name") for v in cfg.variants]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError("every [[variants]] entry needs a unique name")
    if cfg.execution.kr_order_type not in ("best_limit", "market"):
        raise ValueError("execution.kr_order_type must be 'best_limit' or 'market'")
    from .strategies import get_strategy
    seen = set()
    for s in cfg.symbols:
        if s.market not in VALID_MARKETS:
            raise ValueError(f"{s.symbol}: unknown market '{s.market}'")
        if s.strategy not in VALID_STRATEGIES:
            raise ValueError(f"{s.symbol}: unknown strategy '{s.strategy}'")
        if s.market.startswith("kis") and s.interval != "day":
            raise ValueError(f"{s.symbol}: KIS symbols support interval 'day' only")
        try:
            get_strategy(s.strategy, s.params)
        except TypeError as e:
            raise ValueError(f"{s.symbol}: bad strategy params {s.params}: {e}")
        if s.key in seen:
            raise ValueError(f"duplicate symbol {s.key}")
        seen.add(s.key)
    for k, (lo, hi) in EDITABLE_RISK.items():
        v = getattr(cfg.risk, k)
        if not lo <= v <= hi:
            raise ValueError(f"risk.{k} = {v} is outside the allowed range {lo}–{hi}")
