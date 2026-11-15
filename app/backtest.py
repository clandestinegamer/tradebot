"""Backtest and walk-forward test strategies with the same rules, risk limits, stops and fees as the bot.

Examples:
  python -m app.backtest --symbol KRW-BTC --market upbit --interval day --bars 1500
  python -m app.backtest --symbol KRW-BTC --market upbit --interval 60m --bars 3000 --walkforward
  python -m app.backtest --symbol 005930 --market kis_kr --bars 1000 --walkforward --regime
  python -m app.backtest --demo --bars 2000 --walkforward       # synthetic data, no keys or internet
  python -m app.backtest --portfolio --bars 750 --regime        # whole watchlist, one shared account (daily candles)

Walk-forward = tune settings on one stretch of history, then score them on the NEXT stretch they
have never seen, and repeat. Only the unseen ("out-of-sample") results tell you whether the rules
work, rather than whether they were fitted to the past.

Not simulated: the Claude review (it needs live news). For vol_breakout, the volume filter uses the
whole breakout candle's volume, which is slightly optimistic compared with live trading.
"""
from __future__ import annotations

import argparse
import itertools
import math
from collections import Counter

import numpy as np
import pandas as pd

from .config import Config, RiskCfg, SymbolCfg, load_config
from .engine import trailed_stop
from .indicators import sma
from .models import Position
from .risk import RiskManager, round_qty
from .strategies import get_strategy

SLIP = 0.0005

GRIDS = {
    "trend_rsi": {
        "fast_slow": [(20, 50), (10, 30)],
        "adx_min": [0, 20, 25],
        "vol_mult": [0, 1.2],
        "long_ma": [0, 200],
    },
    "vol_breakout": {
        "k": [0.4, 0.5, 0.6],
        "vol_mult": [0, 1.5],
    },
    "auto": {
        "adx_switch": [15, 20, 25],
        "long_ma": [0, 200],
    },
    "dip_buy": {
        "rsi_entry": [5, 10, 15],
        "exit_ma": [5, 10],
        "long_ma": [0, 200],
    },
}


def expand_grid(strategy: str) -> list[dict]:
    g = GRIDS[strategy]
    keys = list(g)
    out = []
    for combo in itertools.product(*(g[k] for k in keys)):
        p = {}
        for k, v in zip(keys, combo):
            if k == "fast_slow":
                p["fast"], p["slow"] = v
            else:
                p[k] = v
        out.append(p)
    return out


def _date_key(ts: str) -> str:
    s = str(ts)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s[:10]


def regime_by_date(bench_daily: pd.DataFrame, ma: int) -> dict[str, bool]:
    """For each date: was the benchmark's PREVIOUS completed day above its moving average?"""
    closes = bench_daily["close"]
    ok = (closes > sma(closes, ma)).shift(1)
    return {_date_key(d): bool(v) for d, v in ok.items() if not pd.isna(v)}


class SymbolSim:
    """One symbol's simulated trading: the exact entry/exit/stop rules the live bot uses.
    Shared by the single-symbol backtest and the portfolio backtest."""

    def __init__(self, candles: pd.DataFrame, strategy_name: str, market: str, risk: RiskCfg, fee_rate: float,
                 fx: float = 1.0, params: dict | None = None, prepared=None, key: str = "bt"):
        self.strat = get_strategy(strategy_name, params)
        self.p = prepared or self.strat.prepare(candles)
        self.market, self.risk, self.fee, self.fx, self.key = market, risk, fee_rate, fx, key
        self.params = params or {}
        self.o, self.h, self.l, self.c = (candles[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.idx = [str(x) for x in candles.index]
        self.pos: Position | None = None
        self.pos_fee = 0.0
        self.trades: list[dict] = []
        self.bars_in_market = 0
        self.entry0 = self.init_qty = 0.0
        self.partial_done, self.adds = False, 0

    @property
    def min_bars(self) -> int:
        return self.strat.min_bars

    def close(self, px: float, i: int, why: str, qty: float | None = None) -> float:
        """Sell all (or `qty`) at px; returns cash received."""
        pos = self.pos
        q = pos.qty if qty is None else min(qty, pos.qty)
        share = q / pos.qty
        fill = px * (1 - SLIP)
        value = q * fill * self.fx
        fee = value * self.fee
        buy_fee = self.pos_fee * share
        pnl = value - fee - q * pos.avg_price * self.fx - buy_fee
        risk_krw = q * (self.entry0 - pos.initial_stop) * self.fx if self.entry0 else q * (pos.avg_price - pos.initial_stop) * self.fx
        self.trades.append({"key": self.key, "entry_bar": pos.entry_bar, "exit_bar": self.idx[i],
                            "entry": pos.avg_price, "exit": fill, "pnl": pnl,
                            "r": pnl / risk_krw if risk_krw > 0 else 0.0, "why": why})
        if q >= pos.qty - 1e-12:
            self.pos = None
        else:
            pos.qty -= q
            self.pos_fee -= buy_fee
        return value - fee

    def scale(self, i: int, cash_available: float = float("inf")) -> float:
        """Partial profit / pyramiding inside bar i, at the trigger level. Returns net cash (+ in, − out)."""
        r, pos = self.risk, self.pos
        if pos is None or (r.partial_r <= 0 and r.pyramid_r <= 0):
            return 0.0
        one_r = self.entry0 - pos.initial_stop
        if one_r <= 0:
            return 0.0
        cash = 0.0
        if r.partial_r > 0 and not self.partial_done:
            level = self.entry0 + r.partial_r * one_r
            if self.h[i] >= level:
                q = round_qty(pos.qty * r.partial_pct / 100, self.market)
                if 0 < q < pos.qty:
                    self.partial_done = True
                    cash += self.close(max(self.o[i], level), i, f"partial +{r.partial_r:g}R", q)
        pos = self.pos
        if pos and r.pyramid_r > 0 and self.adds == 0 and pos.stop >= self.entry0:
            level = self.entry0 + r.pyramid_r * one_r
            if self.h[i] >= level:
                px = max(self.o[i], level) * (1 + SLIP)
                q = round_qty(self.init_qty * r.pyramid_pct / 100, self.market)
                cost = q * px * self.fx
                if q > 0 and cost * (1 + self.fee) <= cash_available + cash:
                    pos.avg_price = (pos.qty * pos.avg_price + q * px) / (pos.qty + q)
                    pos.qty += q
                    self.pos_fee += cost * self.fee
                    self.adds = 1
                    cash -= cost * (1 + self.fee)
        return cash

    def protective_exits(self, i: int) -> float:
        """Stops inside bar i (checked before anything good happens — conservative). Returns cash received."""
        cash = 0.0
        o, l, h = self.o[i], self.l[i], self.h[i]
        if self.pos and o <= self.pos.stop:
            cash += self.close(o, i, "stop (gap)")
        if self.pos and self.strat.uses_forming_bar and self.pos.entry_bar != self.idx[i]:
            cash += self.close(o, i, "breakout hold period over (new candle)")  # exits at the open
        if self.pos:
            if l <= self.pos.stop:
                cash += self.close(self.pos.stop, i, "trailing stop" if self.pos.stop > self.pos.initial_stop else "stop-loss")
            elif self.pos.target > 0 and h >= self.pos.target:
                cash += self.close(max(o, self.pos.target), i, "target")
        return cash

    def signal(self, i: int):
        """(signal, fill price) for bar i."""
        if self.strat.uses_forming_bar:
            if self.pos is None:
                trigger = self.o[i] + self.strat.k * (self.h[i - 1] - self.l[i - 1])
                live = trigger if self.h[i] >= trigger else self.c[i]  # a breakout fills at the trigger price
            else:
                live = self.o[i]
            return self.strat.decide(self.p, i, self.pos, float(live), 1.0), float(live)
        # decide on the closed candle, fill at this bar's open
        return self.strat.decide(self.p, i - 1, self.pos, float(self.o[i])), float(self.o[i])

    def enter(self, i: int, px: float, plan, reason: str = "") -> float:
        """Buy per the risk plan; returns cash spent (net of any same-bar stop-out)."""
        fill = px * (1 + SLIP)
        cost = plan.qty * fill * self.fx
        self.pos_fee = cost * self.fee
        stop = fill - (px - plan.stop)
        self.pos = Position(self.key, self.key, self.market, plan.qty, fill, stop,
                            fill + (plan.target - px) if plan.target > 0 else 0.0, "", self.idx[i], reason,
                            initial_stop=stop, highest=fill, entry_atr=plan.atr)
        self.entry0, self.init_qty, self.partial_done, self.adds = fill, plan.qty, False, 0
        spent = cost + self.pos_fee
        # a breakout entered mid-bar can still be stopped out later in the same bar
        if self.strat.uses_forming_bar and self.l[i] <= self.pos.stop and self.c[i] < px:
            spent -= self.close(self.pos.stop, i, "stop-loss")
        return spent

    def end_of_bar(self, i: int) -> None:
        """Ratchet the stop using this bar's high (applies from the next bar on)."""
        if self.pos:
            new_stop = trailed_stop(self.pos, self.h[i], self.risk, self.fee)
            self.pos.highest = max(self.pos.highest, self.h[i])
            self.pos.stop = max(self.pos.stop, new_stop)
            self.bars_in_market += 1

    def mark(self, i: int) -> float:
        return self.pos.qty * self.c[i] * self.fx if self.pos else 0.0


def run_backtest(candles: pd.DataFrame, strategy_name: str, market: str, risk: RiskCfg, fee_rate: float,
                 fx: float = 1.0, cash: float = 10_000_000, params: dict | None = None,
                 regime: dict[str, bool] | None = None, start: int = 0, end: int | None = None,
                 prepared=None) -> dict:
    """Simulate bars [start, end) of one symbol. Earlier bars are only used to warm up indicators."""
    sim = SymbolSim(candles, strategy_name, market, risk, fee_rate, fx, params, prepared)
    rm = RiskManager(risk)
    end = len(candles) if end is None else end
    start = max(start, sim.min_bars)
    start_cash, curve = cash, []
    for i in range(start, end):
        cash += sim.protective_exits(i)
        cash += sim.scale(i, cash)
        sig, px = sim.signal(i)
        if sig.action == "SELL" and sim.pos:
            cash += sim.close(px, i, sig.reason)
        elif sig.action == "BUY" and sim.pos is None:
            allowed = regime is None or regime.get(_date_key(sim.idx[i]), True)
            plan = rm.plan_buy(market=market, price=px, atr=sig.atr, fx=fx, fee_rate=fee_rate,
                               equity_krw=cash, cash_krw=cash, n_open=0, trades_today=0, day_pnl_pct=0)
            if allowed and plan.ok:
                plan.atr = sig.atr
                cash -= sim.enter(i, px, plan, sig.reason)
        sim.end_of_bar(i)
        curve.append(cash + sim.mark(i))
    if sim.pos:
        cash += sim.close(sim.c[end - 1], end - 1, "end of test")
        curve[-1] = cash
    return summarize(sim.trades, curve, start_cash, sim.c[start:end], sim.bars_in_market, params or {},
                     alloc=min(1.0, risk.max_position_pct / 100))


def run_portfolio(specs: list[dict], risk: RiskCfg, cash: float = 10_000_000,
                  regime: dict[str, dict[str, bool]] | None = None, groups: list[list[str]] | None = None,
                  max_per_group: int = 1) -> dict:
    """All symbols at once with ONE shared account — shared cash, the max-positions limit and correlation
    groups — which is how the bot really trades. spec: key, candles, strategy, market, params, fx, fee_rate."""
    sims = {sp["key"]: SymbolSim(sp["candles"], sp["strategy"], sp["market"], risk, sp["fee_rate"], sp["fx"],
                                 sp.get("params"), key=sp["key"]) for sp in specs}
    timeline: dict[str, list[tuple[str, int]]] = {}
    for key, sim in sims.items():
        for i in range(sim.min_bars, len(sim.idx)):
            timeline.setdefault(_date_key(sim.idx[i]) if len(sim.idx[i]) == 8 else sim.idx[i], []).append((key, i))
    rm = RiskManager(risk)
    start_cash, curve, marks, last_i = cash, [], {k: 0.0 for k in sims}, {}
    exposure = []
    for ts in sorted(timeline):
        events = timeline[ts]
        for key, i in events:
            cash += sims[key].protective_exits(i)
            cash += sims[key].scale(i, cash)
        intents = []
        for key, i in events:
            sim = sims[key]
            sig, px = sim.signal(i)
            if sig.action == "SELL" and sim.pos:
                cash += sim.close(px, i, sig.reason)
                marks[key] = 0.0
            elif sig.action == "BUY" and sim.pos is None:
                intents.append((key, i, sig, px))
        for key, i, sig, px in intents:
            sim = sims[key]
            if regime and not regime.get(sim.market, {}).get(_date_key(sim.idx[i]), True):
                continue
            held = [k for k, s in sims.items() if s.pos]
            group = next((g for g in (groups or []) if key in g), [])
            equity = cash + sum(marks[k] for k in held)
            plan = rm.plan_buy(market=sim.market, price=px, atr=sig.atr, fx=sim.fx, fee_rate=sim.fee,
                               equity_krw=equity, cash_krw=cash, n_open=len(held), trades_today=0, day_pnl_pct=0,
                               group_open=sum(1 for k in held if k in group),
                               max_per_group=max_per_group if group else 0)
            if plan.ok:
                plan.atr = sig.atr
                cash -= sim.enter(i, px, plan, sig.reason)
        for key, i in events:
            sims[key].end_of_bar(i)
            marks[key] = sims[key].mark(i)
            last_i[key] = i
        invested = sum(marks.values())
        curve.append(cash + invested)
        exposure.append(invested / curve[-1] if curve[-1] else 0)
    for key, sim in sims.items():
        if sim.pos:
            cash += sim.close(sim.c[last_i[key]], last_i[key], "end of test")
    if curve:
        curve[-1] = cash
    trades = [t for s in sims.values() for t in s.trades]
    holds = [(s.c[-1] / s.c[s.min_bars] - 1) * 100 for s in sims.values() if len(s.c) > s.min_bars]
    out = summarize(trades, curve, start_cash, np.array([1.0, 1.0]), 0, {})
    out["exposure_pct"] = float(np.mean(exposure) * 100) if exposure else 0.0
    out["equal_weight_hold_pct"] = float(np.mean(holds)) if holds else 0.0
    out["per_symbol"] = {k: {"trades": len(s.trades), "pnl_krw": sum(t["pnl"] for t in s.trades),
                             "win_rate": (sum(1 for t in s.trades if t["pnl"] > 0) / len(s.trades) * 100) if s.trades else 0.0}
                         for k, s in sims.items()}
    out["start"], out["end"] = (min(timeline) if timeline else ""), (max(timeline) if timeline else "")
    return out


def run_dual_momentum(closes: dict[str, pd.Series], us: str, intl: str, bonds: str, lookback: int = 252,
                      cash_return_pct: float = 4.0, fee_rate: float = 0.0025) -> dict:
    """Monthly: hold US stocks or international stocks (whichever is stronger over `lookback` days) while
    US stocks beat cash; otherwise hold bonds. Compared with simply holding US stocks."""
    df = pd.DataFrame({k: v for k, v in closes.items()}).dropna()
    if len(df) <= lookback + 21:
        raise ValueError(f"need more than {lookback + 21} days of history for all three funds (have {len(df)})")
    dates = list(df.index)
    equity, hold, curve, picks = 1.0, None, [], []
    cash_hurdle = cash_return_pct * lookback / 252
    last_month = None
    for i in range(lookback, len(df)):
        month = str(dates[i])[:7] if "-" in str(dates[i])[:7] else str(dates[i])[:6]
        if hold is not None:
            equity *= df[hold].iloc[i] / df[hold].iloc[i - 1]
        if month != last_month:  # first trading day of a new month → decide
            last_month = month
            r = {k: df[k].iloc[i] / df[k].iloc[i - lookback] - 1 for k in (us, intl, bonds)}
            pick = (us if r[us] >= r[intl] else intl) if r[us] * 100 > cash_hurdle else bonds
            if pick != hold:
                equity *= (1 - fee_rate) ** (2 if hold else 1)
                picks.append((str(dates[i]), pick))
                hold = pick
        curve.append(equity)
    eq = pd.Series(curve)
    spy = df[us].iloc[lookback:] / df[us].iloc[lookback]
    return {"return_pct": (eq.iloc[-1] - 1) * 100, "max_drawdown_pct": float((eq / eq.cummax() - 1).min() * 100),
            "hold_us_pct": float(spy.iloc[-1] - 1) * 100,
            "hold_us_drawdown_pct": float((spy / spy.cummax() - 1).min() * 100),
            "switches": len(picks), "picks": picks, "start": str(dates[lookback]), "end": str(dates[-1])}


def run_grid(candles: pd.DataFrame, lower: float, upper: float, levels: int, budget: float,
             fee_rate: float = 0.0005, stop_below_pct: float = 5.0) -> dict:
    """Simulate the grid on candles. Inside each candle the price is assumed to visit open → low → high →
    close on up-candles and open → high → low → close on down-candles."""
    lines = list(np.linspace(lower, upper, levels + 1))
    per_lot = budget / levels
    cash, lots, prev, trades, profit = budget, {}, None, 0, 0.0
    stop_at = lower * (1 - stop_below_pct / 100)
    stopped = False

    def band(p):
        return max(i for i in range(levels) if lines[i] <= p) if p >= lines[0] else -1

    for o, h, l, c in candles[["open", "high", "low", "close"]].itertuples(index=False):
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        for p in path:
            if p < stop_at:
                for lot in lots.values():
                    cash += lot["qty"] * p * (1 - fee_rate)
                    profit += lot["qty"] * p * (1 - fee_rate) - lot["cost"]
                    trades += 1
                lots, stopped = {}, True
                break
            for i in sorted(lots):
                if p >= lines[i + 1]:
                    lot = lots.pop(i)
                    cash += lot["qty"] * p * (1 - fee_rate)
                    profit += lot["qty"] * p * (1 - fee_rate) - lot["cost"]
                    trades += 1
            for i in range(levels):
                if lines[i] <= p < lines[i + 1] and i not in lots and (prev is None or band(prev) > i) and cash >= per_lot:
                    lots[i] = {"qty": per_lot * (1 - fee_rate) / p, "cost": per_lot}
                    cash -= per_lot
                    trades += 1
                    break
            prev = p
        if stopped:
            break
    last = float(candles["close"].iloc[-1])
    inventory = sum(l["qty"] for l in lots.values()) * last
    final = cash + inventory
    return {"return_pct": (final / budget - 1) * 100, "realized_krw": profit, "trades": trades,
            "open_lots": len(lots), "stopped": stopped,
            "hold_pct": (last / float(candles["open"].iloc[0]) - 1) * 100}


CRASHES = {
    "2018 crypto winter": ("2018-01-08", "2018-12-15"),
    "COVID crash (2020)": ("2020-02-19", "2020-04-30"),
    "2022 stock bear market": ("2022-01-03", "2022-10-14"),
    "2022 crypto crash (LUNA, FTX)": ("2022-04-01", "2022-12-31"),
}


def stress_test(candles: pd.DataFrame, strategy: str, market: str, risk: RiskCfg, fee_rate: float, fx: float = 1.0,
                cash: float = 10_000_000, params: dict | None = None) -> list[dict]:
    """Replay the strategy through famous crash periods that fall inside the data."""
    keys = [_date_key(str(i)) for i in candles.index]
    prep = get_strategy(strategy, params).prepare(candles)
    out = []
    for name, (a, b) in CRASHES.items():
        idx = [i for i, k in enumerate(keys) if a <= k <= b]
        if len(idx) < 10:
            out.append({"name": name, "skipped": "not in the loaded history"})
            continue
        start, end = idx[0], idx[-1] + 1
        warm = get_strategy(strategy, params).min_bars
        if start < warm:
            out.append({"name": name, "skipped": f"needs {warm} candles of history before {a}"})
            continue
        r = run_backtest(candles, strategy, market, risk, fee_rate, fx, cash, params, None, start, end, prep)
        out.append({"name": name, "period": f"{a} → {b}", **{k: r[k] for k in (
            "return_pct", "max_drawdown_pct", "trades", "win_rate", "buy_hold_pct", "hold_same_size_pct", "exposure_pct")}})
    return out


def monte_carlo(trade_returns_pct: list[float], n_trades: int | None = None, sims: int = 5000,
                pain_pct: float = 20.0, seed: int = 7) -> dict:
    """Shuffle (resample) the trade results thousands of times to see how bad an unlucky ordering could get.
    trade_returns_pct: each trade's result as % of the account."""
    r = np.asarray(trade_returns_pct, float) / 100
    if len(r) < 10:
        raise ValueError(f"need at least 10 trades (have {len(r)})")
    n = n_trades or len(r)
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(sims, n), replace=True)
    eq = np.cumprod(1 + draws, axis=1)
    peaks = np.maximum.accumulate(np.concatenate([np.ones((sims, 1)), eq], axis=1), axis=1)[:, 1:]
    dd = (eq / peaks - 1).min(axis=1) * 100
    streak = np.zeros(sims, int)
    cur = np.zeros(sims, int)
    for j in range(n):
        cur = np.where(draws[:, j] <= 0, cur + 1, 0)
        streak = np.maximum(streak, cur)
    final = (eq[:, -1] - 1) * 100
    return {"trades": n, "sims": sims,
            "final_return_pct": {"bad_5pct": float(np.percentile(final, 5)), "median": float(np.median(final)),
                                 "good_95pct": float(np.percentile(final, 95))},
            "max_drawdown_pct": {"median": float(np.median(dd)), "bad_5pct": float(np.percentile(dd, 5)),
                                 "worst": float(dd.min())},
            "losing_streak": {"median": int(np.median(streak)), "bad_5pct": int(np.percentile(streak, 95))},
            "chance_of_drawdown_beyond_pct": float((dd <= -abs(pain_pct)).mean() * 100), "pain_pct": pain_pct,
            "chance_of_loss_pct": float((final < 0).mean() * 100)}


def trades_as_pct(trades: list[dict], risk_per_trade_pct: float) -> list[float]:
    """Backtest trades → % of account, using their R multiple × the risk taken per trade."""
    return [t["r"] * risk_per_trade_pct for t in trades]


def print_mc(mc: dict) -> None:
    print(f"\n  Monte Carlo — {mc['sims']:,} random re-orderings of {mc['trades']} trades")
    f, d, s = mc["final_return_pct"], mc["max_drawdown_pct"], mc["losing_streak"]
    print(f"    Result after {mc['trades']} trades : unlucky {f['bad_5pct']:+.1f}% · typical {f['median']:+.1f}% · lucky {f['good_95pct']:+.1f}%")
    print(f"    Worst drop along the way  : typical {d['median']:.1f}% · unlucky (1 in 20) {d['bad_5pct']:.1f}% · worst seen {d['worst']:.1f}%")
    print(f"    Losing streak             : typical {s['median']} · unlucky {s['bad_5pct']} in a row")
    print(f"    Chance of a {mc['pain_pct']:g}%+ drop: {mc['chance_of_drawdown_beyond_pct']:.1f}% · chance of ending down: {mc['chance_of_loss_pct']:.1f}%")


def summarize(trades: list[dict], curve: list[float], start_cash: float, closes: np.ndarray,
              bars_in_market: int, params: dict, alloc: float = 1.0) -> dict:
    eq = pd.Series(curve or [start_cash])
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    ret = (eq.iloc[-1] / start_cash - 1) * 100
    dd = float((eq / eq.cummax() - 1).min() * 100)
    pf = sum(wins) / sum(losses) if losses and sum(losses) > 0 else (float("inf") if wins else 0.0)
    bh = (closes[-1] / closes[0] - 1) * 100 if len(closes) > 1 else 0.0
    return {
        "params": params, "bars": len(curve), "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "return_pct": ret, "buy_hold_pct": bh, "max_drawdown_pct": dd, "profit_factor": pf,
        # fair comparison: holding the same slice of the account the bot is allowed to use
        "hold_same_size_pct": bh * alloc,
        "avg_r": float(np.mean([t["r"] for t in trades])) if trades else 0.0,
        "exposure_pct": bars_in_market / len(curve) * 100 if curve else 0.0,
        "log": trades,
    }


def score(r: dict, min_trades: int = 4) -> float:
    """How settings are ranked during tuning: return per unit of drawdown, with enough trades to mean anything."""
    if r["trades"] < min_trades:
        return -math.inf
    return r["return_pct"] / max(1.0, abs(r["max_drawdown_pct"]))


def walk_forward(candles: pd.DataFrame, strategy: str, market: str, risk: RiskCfg, fee_rate: float,
                 fx: float = 1.0, cash: float = 10_000_000, folds: int = 4,
                 regime: dict[str, bool] | None = None, grid: list[dict] | None = None) -> dict:
    grid = grid or expand_grid(strategy)
    warm = max(get_strategy(strategy, g).min_bars for g in grid)
    usable = len(candles) - warm
    test_len = usable // (folds + 2)
    train_len = 2 * test_len
    if test_len < 30:
        raise ValueError(f"not enough history: need about {warm + 30 * (folds + 2)} candles, have {len(candles)}")

    cache = {}
    def prep(g):
        key = tuple(sorted(g.items()))
        if key not in cache:
            cache[key] = get_strategy(strategy, g).prepare(candles)
        return cache[key]

    out_folds = []
    for f in range(folds):
        test_start = warm + train_len + f * test_len
        test_end = min(len(candles), test_start + test_len)
        train_start = test_start - train_len
        ranked = []
        for g in grid:
            r = run_backtest(candles, strategy, market, risk, fee_rate, fx, cash, g, regime,
                             train_start, test_start, prep(g))
            ranked.append((score(r), r))
        ranked.sort(key=lambda x: x[0], reverse=True)
        best_score, best = ranked[0]
        if best_score == -math.inf:  # nothing traded enough to judge → fall back to default settings
            best = run_backtest(candles, strategy, market, risk, fee_rate, fx, cash, {}, regime,
                                train_start, test_start)
        oos = run_backtest(candles, strategy, market, risk, fee_rate, fx, cash, best["params"], regime,
                           test_start, test_end, prep(best["params"]))
        out_folds.append({"train": (str(candles.index[train_start]), str(candles.index[test_start - 1])),
                          "test": (str(candles.index[test_start]), str(candles.index[test_end - 1])),
                          "params": best["params"], "in_sample": best, "out_of_sample": oos})

    oos = [f["out_of_sample"] for f in out_folds]
    compounded = (np.prod([1 + r["return_pct"] / 100 for r in oos]) - 1) * 100
    bh = (np.prod([1 + r["buy_hold_pct"] / 100 for r in oos]) - 1) * 100
    bh_same = (np.prod([1 + r["hold_same_size_pct"] / 100 for r in oos]) - 1) * 100
    all_trades = [t for r in oos for t in r["log"]]
    wins = [t["pnl"] for t in all_trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in all_trades if t["pnl"] <= 0]
    is_avg = float(np.mean([f["in_sample"]["return_pct"] for f in out_folds]))
    oos_avg = float(np.mean([r["return_pct"] for r in oos]))

    # settings for live use: tuned on the most recent train+test stretch
    recent_start = len(candles) - train_len
    final = max((run_backtest(candles, strategy, market, risk, fee_rate, fx, cash, g, regime,
                              recent_start, len(candles), prep(g)) for g in grid), key=score)
    return {
        "folds": out_folds,
        "oos_return_pct": compounded, "oos_buy_hold_pct": bh, "oos_hold_same_size_pct": bh_same,
        "oos_trades": len(all_trades), "oos_win_rate": len(wins) / len(all_trades) * 100 if all_trades else 0.0,
        "oos_profit_factor": sum(wins) / sum(losses) if losses and sum(losses) > 0 else float("inf") if wins else 0.0,
        "oos_worst_drawdown_pct": min(r["max_drawdown_pct"] for r in oos),
        "in_sample_avg_return_pct": is_avg, "oos_avg_return_pct": oos_avg,
        "param_stability": Counter(str(f["params"]) for f in out_folds).most_common(1)[0][1] / folds,
        "recommended_params": final["params"] if score(final) > -math.inf else None,
    }


def looks_tradeable(wf: dict) -> bool:
    return (wf["oos_trades"] >= 15 and wf["oos_return_pct"] > 0
            and wf["oos_return_pct"] >= wf["oos_hold_same_size_pct"] and wf["oos_profit_factor"] > 1.1)


def verdict(wf: dict) -> list[str]:
    """Plain-language reading of a walk-forward result."""
    notes = []
    if wf["oos_trades"] < 15:
        notes.append("Too few out-of-sample trades to judge — get more history or test more symbols.")
    if wf["oos_return_pct"] <= 0:
        notes.append("Lost money on unseen data. Don't trade these rules live on this symbol.")
    elif wf["oos_return_pct"] < wf["oos_hold_same_size_pct"]:
        notes.append("Made money on unseen data, but less than just holding the same-size position — "
                     "the rules aren't adding value here.")
    else:
        notes.append("Beat holding the same-size position on unseen data. Promising — confirm with weeks of "
                     "paper trading before going live.")
    if wf["in_sample_avg_return_pct"] > 0 and wf["oos_avg_return_pct"] < 0.4 * wf["in_sample_avg_return_pct"]:
        notes.append("Big drop from tuning results to unseen results — a sign of overfitting.")
    if wf["param_stability"] < 0.5:
        notes.append("The best settings change from period to period — the edge, if any, is fragile.")
    return notes


def _load(a, cfg: Config, symbol: str, market: str, interval: str, bars: int) -> pd.DataFrame:
    s = cfg.secrets
    if a.demo:
        from .demo import DemoData
        return DemoData(bar_seconds=60, history=bars + 1).candles(f"{market}:{symbol}", bars + 1).iloc[:-1]
    if market == "upbit":
        from .brokers.upbit import UpbitClient
        return UpbitClient().candles(symbol, interval, bars + 1).iloc[:-1]
    from .brokers.kis import KISClient
    kis = KISClient(s.kis_app_key, s.kis_app_secret, s.kis_account_no, s.kis_account_product, s.kis_env)
    return kis.kr_daily(symbol, bars) if market == "kis_kr" else kis.us_daily(symbol, a.exchange, bars)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="KRW-BTC")
    ap.add_argument("--market", default="upbit", choices=["upbit", "kis_kr", "kis_us"])
    ap.add_argument("--exchange", default="NASD")
    ap.add_argument("--interval", default="day")
    ap.add_argument("--strategy", default="trend_rsi", choices=["trend_rsi", "vol_breakout", "dip_buy", "auto"])
    ap.add_argument("--bars", type=int, default=1000, help="candles of history to fetch")
    ap.add_argument("--walkforward", action="store_true", help="tune on past data, score on unseen data")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--regime", action="store_true", help="apply the market-regime filter from config.toml")
    ap.add_argument("--demo", action="store_true", help="use synthetic data")
    ap.add_argument("--dual-momentum", action="store_true", help="test the dual-momentum core (US ETFs, daily)")
    ap.add_argument("--grid", nargs=3, type=float, metavar=("LOWER", "UPPER", "LEVELS"),
                    help="test a crypto grid on --symbol, e.g. --grid 2700 3300 10")
    ap.add_argument("--budget", type=float, default=1_000_000)
    ap.add_argument("--stress", action="store_true", help="replay famous crashes (loads lots of history)")
    ap.add_argument("--montecarlo", action="store_true", help="add a Monte Carlo worst-case analysis")
    ap.add_argument("--portfolio", action="store_true",
                    help="test every enabled watchlist symbol together with one shared account (daily candles)")
    ap.add_argument("--trades", action="store_true", help="print every trade")
    a = ap.parse_args()

    try:
        cfg = load_config()
    except FileNotFoundError:
        cfg = Config()
    if a.portfolio:
        return portfolio_main(a, cfg)
    if a.dual_momentum:
        c = cfg.core
        closes = {}
        for sym in (c.us, c.intl, c.bonds):
            a2 = argparse.Namespace(**{**vars(a), "exchange": c.exchange})
            closes[sym] = _load(a2, cfg, sym, "kis_us", "day", a.bars)["close"]
        r = run_dual_momentum(closes, c.us, c.intl, c.bonds, c.lookback_days, c.cash_return_pct, cfg.fee_rate("kis_us"))
        print(f"\nDual momentum ({c.us} / {c.intl} / {c.bonds}) · {r['start']} → {r['end']}")
        print(f"  Return        : {r['return_pct']:+.2f}%   (holding {c.us}: {r['hold_us_pct']:+.2f}%)")
        print(f"  Max drawdown  : {r['max_drawdown_pct']:.2f}%   (holding {c.us}: {r['hold_us_drawdown_pct']:.2f}%)")
        print(f"  Switches      : {r['switches']}  → " + ", ".join(f"{d} {p}" for d, p in r["picks"][-6:]))
        print("\n  Its main job is smaller crashes, not beating the market every year.\n")
        return
    if a.grid:
        lo, hi, lv = a.grid
        df = _load(a, cfg, a.symbol, "upbit", a.interval, a.bars)
        r = run_grid(df, lo, hi, int(lv), a.budget, cfg.fee_rate("upbit"))
        print(f"\nGrid {a.symbol} {lo:,.0f}–{hi:,.0f} × {int(lv)} steps · {len(df)} candles")
        print(f"  Return  : {r['return_pct']:+.2f}%  (holding: {r['hold_pct']:+.2f}%)   realized ₩{r['realized_krw']:,.0f}")
        print(f"  Trades  : {r['trades']}  · open lots at end {r['open_lots']}" + ("  · STOPPED below range" if r["stopped"] else ""))
        print("\n  Grids earn in sideways markets and lag (or stop out) in strong trends.\n")
        return
    fx = cfg.usd_krw if a.market == "kis_us" else 1.0
    df = _load(a, cfg, a.symbol, a.market, a.interval, a.bars)
    print(f"\n{a.symbol} · {a.strategy} · {len(df)} candles ({df.index[0]} → {df.index[-1]})")

    regime = None
    if a.regime and cfg.regime.enabled:
        bench = getattr(cfg.regime, a.market)
        days = max(a.bars if a.interval == "day" else a.bars // 6, cfg.regime.ma + 50)
        a2 = argparse.Namespace(**{**vars(a), "exchange": cfg.regime.kis_us_exchange})
        bdf = _load(a2, cfg, bench, a.market, "day", days)
        regime = regime_by_date(bdf, cfg.regime.ma)
        print(f"  regime filter: only buy when {bench} is above its {cfg.regime.ma}-day average")

    fee = cfg.fee_rate(a.market)
    sym = next((s for s in cfg.symbols if s.symbol == a.symbol and s.market == a.market), None)
    params = sym.params if sym and sym.strategy == a.strategy else {}
    if a.stress:
        print("  Crash replays (rules as configured, Claude review not simulated):")
        for row in stress_test(df, a.strategy, a.market, cfg.risk, fee, fx, cfg.starting_cash_krw, params):
            if "skipped" in row:
                print(f"   • {row['name']}: skipped — {row['skipped']}")
            else:
                print(f"   • {row['name']} ({row['period']}): bot {row['return_pct']:+.1f}% (max drop {row['max_drawdown_pct']:.1f}%, "
                      f"{row['trades']} trades) vs holding the same size {row['hold_same_size_pct']:+.1f}% / all-in {row['buy_hold_pct']:+.1f}%")
        print("\n  Tip: use --bars 3500 to reach back to 2018–2020.\n")
        return
    if not a.walkforward:
        r = run_backtest(df, a.strategy, a.market, cfg.risk, fee, fx, cfg.starting_cash_krw, params, regime)
        print(f"  settings        : {get_strategy(a.strategy, params).params}")
        print(f"  Strategy return : {r['return_pct']:+.2f}%   (holding the same-size position {r['hold_same_size_pct']:+.2f}%;"
              f" all-in {r['buy_hold_pct']:+.2f}%)")
        print(f"  Max drawdown    : {r['max_drawdown_pct']:.2f}%   time in market {r['exposure_pct']:.0f}%")
        print(f"  Trades          : {r['trades']}  win rate {r['win_rate']:.0f}%  profit factor {r['profit_factor']:.2f}"
              f"  avg {r['avg_r']:+.2f}R")
        if a.trades:
            for t in r["log"]:
                print(f"   {t['entry_bar']} → {t['exit_bar']}  {t['entry']:,.4g} → {t['exit']:,.4g}  "
                      f"₩{t['pnl']:,.0f} ({t['r']:+.2f}R, {t['why']})")
        if a.montecarlo:
            try:
                print_mc(monte_carlo(trades_as_pct(r["log"], cfg.risk.risk_per_trade_pct)))
            except ValueError as ex:
                print(f"\n  Monte Carlo skipped: {ex}")
        print("\n  In-sample only — run with --walkforward before trusting these numbers.\n")
        return

    wf = walk_forward(df, a.strategy, a.market, cfg.risk, fee, fx, cfg.starting_cash_krw, a.folds, regime)
    print(f"  {len(expand_grid(a.strategy))} setting combinations × {a.folds} folds\n")
    for i, f in enumerate(wf["folds"], 1):
        ins, oos = f["in_sample"], f["out_of_sample"]
        print(f"  Fold {i}: tuned {f['train'][0]}→{f['train'][1]}  tested {f['test'][0]}→{f['test'][1]}")
        print(f"     picked {f['params']}")
        print(f"     tuning {ins['return_pct']:+6.2f}% ({ins['trades']} trades)   "
              f"UNSEEN {oos['return_pct']:+6.2f}% ({oos['trades']} trades, DD {oos['max_drawdown_pct']:.1f}%)"
              f"   hold-same-size {oos['hold_same_size_pct']:+6.2f}%")
    print(f"\n  Out-of-sample total : {wf['oos_return_pct']:+.2f}%")
    print(f"  Just holding        : {wf['oos_hold_same_size_pct']:+.2f}% with the same position size "
          f"({wf['oos_buy_hold_pct']:+.2f}% if you put everything in)")
    print(f"  Out-of-sample trades: {wf['oos_trades']}  win rate {wf['oos_win_rate']:.0f}%  "
          f"profit factor {wf['oos_profit_factor']:.2f}  worst drawdown {wf['oos_worst_drawdown_pct']:.1f}%")
    print(f"  Settings stability  : {wf['param_stability'] * 100:.0f}% of folds picked the same settings")
    if a.montecarlo:
        oos_trades = [t for f in wf["folds"] for t in f["out_of_sample"]["log"]]
        try:
            print_mc(monte_carlo(trades_as_pct(oos_trades, cfg.risk.risk_per_trade_pct)))
        except ValueError as ex:
            print(f"\n  Monte Carlo skipped: {ex}")
    print("\n  Verdict:")
    for n in verdict(wf):
        print(f"   • {n}")
    if looks_tradeable(wf) and wf["recommended_params"] is not None:
        rp = ", ".join(f"{k} = {v}" for k, v in wf["recommended_params"].items())
        print(f"\n  Settings to paper-trade next (put under the symbol in config.toml):\n     params = {{ {rp} }}")
    else:
        print("\n  No settings recommended — this strategy didn't hold up on unseen data for this symbol.")
    print("\n  Claude review not simulated. Past results don't predict future returns.\n")


def portfolio_main(a, cfg: Config) -> None:
    syms = [s for s in cfg.symbols if s.enabled]
    specs, regime = [], {}
    for s in syms:
        a2 = argparse.Namespace(**{**vars(a), "exchange": s.exchange})
        df = _load(a2, cfg, s.symbol, s.market, "day", a.bars)
        specs.append({"key": s.key, "candles": df, "strategy": s.strategy, "market": s.market, "params": s.params,
                      "fx": cfg.usd_krw if s.market == "kis_us" else 1.0, "fee_rate": cfg.fee_rate(s.market)})
        if a.regime and cfg.regime.enabled and s.market not in regime:
            bench = getattr(cfg.regime, s.market)
            a3 = argparse.Namespace(**{**vars(a), "exchange": cfg.regime.kis_us_exchange})
            regime[s.market] = regime_by_date(_load(a3, cfg, bench, s.market, "day", a.bars + cfg.regime.ma), cfg.regime.ma)
    r = run_portfolio(specs, cfg.risk, cfg.starting_cash_krw, regime or None, cfg.correlation.groups,
                      cfg.correlation.max_per_group)
    print(f"\nPortfolio · {len(specs)} symbols · daily candles · {r['start']} → {r['end']}")
    print(f"  Return          : {r['return_pct']:+.2f}%   (equal-weight buy & hold, fully invested: "
          f"{r['equal_weight_hold_pct']:+.2f}%)")
    print(f"  Max drawdown    : {r['max_drawdown_pct']:.2f}%   average invested {r['exposure_pct']:.0f}% of the account")
    print(f"  Trades          : {r['trades']}  win rate {r['win_rate']:.0f}%  profit factor {r['profit_factor']:.2f}"
          f"  avg {r['avg_r']:+.2f}R")
    for k, v in r["per_symbol"].items():
        print(f"     {k:<18} {v['trades']:>3} trades  ₩{v['pnl_krw']:>12,.0f}  win {v['win_rate']:.0f}%")
    print("\n  Shared cash + position limits + correlation groups applied. In-sample: walk-forward each symbol too.\n")


if __name__ == "__main__":
    main()
