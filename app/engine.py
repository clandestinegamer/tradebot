"""The trading loop: data → strategy → filters → risk → AI review → order → log/alert."""
from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .ai_review import AIReviewer
from .config import Config, SymbolCfg, apply_overrides, is_crypto, is_usd
from .models import Position
from .risk import RiskManager
from .storage import Store, now_iso
from .strategies import get_strategy

log = logging.getLogger("tradebot")
SEOUL = ZoneInfo("Asia/Seoul")
MKT = {"upbit": "Upbit", "binance": "Binance", "kis_kr": "KRX", "kis_us": "US"}


def trailed_stop(pos: Position, price: float, risk, fee_rate: float) -> float:
    """Where the stop should be given the best price seen. Shared by the engine, shadows and the backtester."""
    high = max(pos.highest or pos.avg_price, price)
    stop = pos.stop
    one_r = pos.avg_price - (pos.initial_stop or pos.stop)
    if risk.breakeven_r > 0 and one_r > 0 and high >= pos.avg_price + risk.breakeven_r * one_r:
        stop = max(stop, pos.avg_price * (1 + 2 * fee_rate))  # break-even after round-trip fees
    if risk.trail_atr > 0 and pos.entry_atr > 0:
        stop = max(stop, high - risk.trail_atr * pos.entry_atr)
    return stop


def is_crypto_weekend(now: datetime | None = None) -> bool:
    """Saturday 00:00 → Monday 09:00 KST: thin trading, sharp gaps."""
    t = (now or datetime.now(SEOUL)).astimezone(SEOUL)
    return t.weekday() in (5, 6) or (t.weekday() == 0 and t.hour < 9)


def day_start_utc() -> str:
    """Trading day starts at 00:00 KST."""
    t = datetime.now(SEOUL).replace(hour=0, minute=0, second=0, microsecond=0)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.4g}"


class Engine:
    def __init__(self, cfg: Config, store: Store, data, executor, ai: AIReviewer, notifier=None, fx=None):
        from .longterm import LongTerm
        from .monitor import Monitor
        from .shadow import ShadowTracker
        from .signals import MarketSignals
        self.base_cfg = cfg
        self.store, self.data, self.ex, self.ai = store, data, executor, ai
        self.notifier, self.fxr = notifier, fx
        self.lock = threading.RLock()
        self.last_prices: dict[str, float] = {}
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self._configure(apply_overrides(cfg, store.get("overrides")))
        self.shadow = ShadowTracker(lambda: self._cfg, store)
        self.monitor = Monitor(self)
        self.longterm = LongTerm(self)
        self.signals = MarketSignals(lambda: self._cfg, fx_rate=self._usdkrw, upbit=getattr(data, "upbit", None),
                                     demo=bool(getattr(data, "demo", None)), kis=getattr(data, "kis", None),
                                     finnhub_key=cfg.secrets.finnhub_key)

    # cfg is read through the engine so settings changed from the phone take effect everywhere
    @property
    def cfg(self) -> Config:
        return self._cfg

    def _configure(self, cfg: Config) -> None:
        self._cfg = cfg
        self.data.cfg = cfg
        self.ai.cfg = cfg.ai
        self.risk = RiskManager(cfg.risk)
        self.strategies = {s.key: get_strategy(s.strategy, s.params) for s in cfg.symbols}

    def apply_settings(self, overrides: dict) -> None:
        """Validate and apply settings changed from the dashboard (raises ValueError if invalid)."""
        new = apply_overrides(self.base_cfg, overrides)
        held = {p.key for p in self.store.positions()}
        missing = held - {s.key for s in new.symbols}
        if missing:
            raise ValueError(f"can't remove {', '.join(sorted(missing))} while a position is open — disable it instead")
        with self.lock:
            self.store.set("overrides", overrides)
            self._configure(new)
        self.store.event("settings changed from the dashboard")

    # ── alerts ──
    def notify(self, text: str, key: str | None = None) -> None:
        if self.notifier:
            self.notifier.send(text, key)

    # ── state flags ──
    @property
    def paused(self) -> bool:
        return bool(self.store.get("paused", False))

    def set_paused(self, v: bool) -> None:
        self.store.set("paused", v)
        self.store.event("bot paused — no new buys (exits still run)" if v else "bot resumed")

    # ── accounting ──
    def swing_positions(self) -> list[Position]:
        from .longterm import is_longterm
        return [p for p in self.store.positions() if not is_longterm(p.key)]

    def _fx_for(self, market: str) -> float:
        f = getattr(self.data, "fx_market", None)
        return f(market) if f else (self.cfg.usd_krw if is_usd(market) else 1.0)

    def position_value_krw(self) -> float:
        return sum(p.qty * self.last_prices.get(p.key, p.avg_price) * self._fx_for(p.market)
                   for p in self.store.positions())

    def capped(self, equity: float, cash: float) -> tuple[float, float]:
        """Small-live mode: the bot behaves as if the account were only live_capital_krw."""
        cap = self.cfg.live_capital_krw
        if not (self.cfg.is_live and cap > 0):
            return equity, cash
        return min(equity, cap), max(0.0, min(cash, cap - self.position_value_krw()))

    def open_risk_krw(self) -> float:
        """What the account loses if every swing stop is hit (positions at break-even or better count 0)."""
        return sum(max(0.0, p.avg_price - p.stop) * p.qty * self._fx_for(p.market) for p in self.swing_positions())

    def usd_exposure(self) -> dict:
        """How much of the account is in US dollars, and how much the won/dollar rate has added or cost."""
        fx_now = self._usdkrw()
        value, fx_pnl = 0.0, 0.0
        buys = {}
        for t in self.store.all_trades():
            if t["side"] == "BUY":
                buys[t["key"]] = t
        for p in self.store.positions():
            if not is_usd(p.market):
                continue
            px = self.last_prices.get(p.key, p.avg_price)
            value += p.qty * px * fx_now
            fx_buy = (buys.get(p.key) or {}).get("fx_usdkrw") or fx_now
            fx_pnl += p.qty * p.avg_price * (fx_now - fx_buy)
        eq = self.equity_krw()
        return {"usd_value_krw": value, "pct_of_equity": value / eq * 100 if eq else 0.0,
                "fx_effect_krw": fx_pnl, "usd_krw": fx_now}

    def equity_krw(self, cash: float | None = None) -> float:
        cash = self.ex.total_cash_krw() if cash is None else cash
        return cash + self.position_value_krw()

    def day_stats(self, equity: float) -> tuple[int, float]:
        """(buys today, today's P&L % vs the day's opening equity)."""
        key = f"day_open:{day_start_utc()[:10]}"
        opening = self.store.get(key)
        if opening is None:
            opening = equity
            self.store.set(key, opening)
        buys = sum(1 for t in self.store.trades_since(day_start_utc()) if t["side"] == "BUY")
        return buys, (equity / opening - 1) * 100 if opening else 0.0

    def history(self, s: SymbolCfg, bars: int):
        """Long history for re-tests (closed candles only)."""
        if self.data.demo:
            from .demo import DemoData
            return DemoData(bar_seconds=60, history=bars + 1).candles(s.key, bars + 1).iloc[:-1]
        if s.market == "upbit":
            return self.data.upbit.candles(s.symbol, s.interval, bars + 1).iloc[:-1]
        if s.market == "binance":
            return self.data.binance.candles(s.symbol, s.interval, bars + 1).iloc[:-1]
        if s.market == "kis_kr":
            return self.data.kis.kr_daily(s.symbol, bars)
        return self.data.kis.us_daily(s.symbol, s.exchange, bars)

    # ── main loop body ──
    def tick(self) -> None:
        ok = True
        with self.lock:
            for s in self.cfg.symbols:
                if s.market.startswith("kis") and getattr(self.data, "kis", True) is None:
                    continue  # stock symbols wait until KIS keys are added to .env
                try:
                    self._process(s)
                except Exception as e:
                    ok = False
                    self.last_error = f"{s.key}: {type(e).__name__}: {e}"
                    self.store.event(self.last_error, "error")
                    log.error(traceback.format_exc())
                    if self.cfg.alerts.errors:
                        self.notify(f"⚠️ {s.symbol}: {type(e).__name__}: {e}", key=f"err:{s.key}")
            try:
                self.longterm.tick()
            except Exception as e:
                ok = False
                self.last_error = f"long-term holdings: {e}"
            equity = None
            try:
                cash = self.ex.total_cash_krw()
                equity = self.equity_krw(cash)
                self.store.add_equity(equity, cash)
            except Exception as e:
                ok = False
                self.last_error = f"equity snapshot: {e}"
            if ok:
                self.last_error = None
            self.last_tick = now_iso()
        if equity is not None:
            self.monitor.after_tick(equity)
        if self.notifier:
            self.notifier.heartbeat(ok)

    def _process(self, s: SymbolCfg) -> None:
        pos = self.store.position(s.key)
        if not s.enabled and pos is None:
            return
        if not self.data.is_open(s):
            return
        price = self.data.price(s)
        self.last_prices[s.key] = price
        fx = self.data.fx(s)
        self.shadow.update(s, price)

        # 1) protective exits on every tick
        if pos:
            if price <= pos.stop:
                kind = "trailing stop" if pos.stop > pos.initial_stop > 0 else "stop-loss"
                self._sell(s, pos, price, fx, f"{kind} hit ({_fmt(price)} ≤ {_fmt(pos.stop)})")
                return
            if pos.target > 0 and price >= pos.target:
                self._sell(s, pos, price, fx, f"take-profit hit ({_fmt(price)} ≥ {_fmt(pos.target)})")
                return
            self._trail(s, pos, price)
            if s.inverse and self.data.regime(s.market)[0]:
                self._sell(s, pos, price, fx, "market turned back up — closing the inverse (falling-market) position")
                return
            self._scale(s, self.store.position(s.key), price, fx)

        # 2) strategy
        strat = self.strategies[s.key]
        candles, forming = self.data.candles(s, self.cfg.history_bars)
        if strat.uses_forming_bar:
            df, bar_id = candles, str(candles.index[-1])
        else:
            df = candles.iloc[:-1] if forming else candles
            bar_id = str(df.index[-1]) if len(df) else ""
            seen_key = f"last_bar:{s.key}"
            if self.store.get(seen_key) == bar_id:
                return  # already decided on this closed candle
            self.store.set(seen_key, bar_id)

        progress = self.data.bar_progress(s, bar_id) if strat.uses_forming_bar else None
        self.shadow.strategy_exits(s, strat, df, price, progress)
        sig = strat.evaluate(df, pos, price, progress)
        if sig.filtered and self.store.get(f"filtered_bar:{s.key}") != bar_id:
            self.store.set(f"filtered_bar:{s.key}", bar_id)  # log each filtered setup once per candle
            self.store.add_signal(s.key, "BUY", sig.reason, "filtered", sig.snapshot)
            self.shadow.record(s, "filtered", sig, price, bar_id, "filtered")
        if sig.action == "SELL" and pos:
            self._sell(s, pos, price, fx, sig.reason)
        elif sig.action == "BUY" and not pos and s.enabled:
            if strat.uses_forming_bar and self.store.get(f"entered_bar:{s.key}") == bar_id:
                return  # one breakout entry per candle
            self._try_buy(s, sig, price, fx, bar_id, df)

    def fast_check(self, key: str, price: float) -> None:
        """Real-time stop / trailing check for one position (called by the stop watcher between ticks)."""
        if not self.lock.acquire(timeout=2):
            return  # the main loop is busy and will check this position itself
        try:
            pos = self.store.position(key)
            s = self.cfg.symbol(key)
            if not pos or not s:
                return
            self.last_prices[key] = price
            fx = self.data.fx(s)
            if price <= pos.stop:
                kind = "trailing stop" if pos.stop > pos.initial_stop > 0 else "stop-loss"
                self._sell(s, pos, price, fx, f"{kind} hit ({_fmt(price)} ≤ {_fmt(pos.stop)}) [real-time]")
            elif pos.target > 0 and price >= pos.target:
                self._sell(s, pos, price, fx, f"take-profit hit ({_fmt(price)} ≥ {_fmt(pos.target)}) [real-time]")
            else:
                self._trail(s, pos, price)
        finally:
            self.lock.release()

    def _try_buy(self, s: SymbolCfg, sig, price: float, fx: float, bar_id: str, df) -> None:
        def skip(outcome: str, shadow_src: str | None = None):
            self.store.add_signal(s.key, "BUY", sig.reason, outcome, sig.snapshot)
            if shadow_src:
                self.shadow.record(s, shadow_src, sig, price, bar_id, outcome)

        if self.paused or self.store.get("killed", False):
            return skip("skipped: bot paused")
        block = self.data.entry_block(s)
        if block:
            return skip(f"skipped: {block}")
        ok, why = self.data.regime(s.market)
        if s.inverse and ok:
            return skip(f"inverse ETF only bought in a falling market: {why}")
        if not s.inverse and not ok:
            return skip(f"blocked by market regime: {why}", "regime")
        scale, notes = 1.0, []
        for d in self.signals.check(s.market, s.symbol):
            if s.inverse and d.source == "vix":
                continue  # fear helps an inverse position — don't block it
            if d.action == "block":
                return skip(f"blocked by market conditions: {d.reason}", d.source)
            scale *= 0.5
            notes.append(f"half size: {d.reason}")
        if is_crypto(s.market) and is_crypto_weekend() and self.cfg.risk.weekend_crypto_mult < 1:
            scale *= self.cfg.risk.weekend_crypto_mult
            notes.append(f"weekend size ×{self.cfg.risk.weekend_crypto_mult:g}")
        alloc, alloc_note = self._alloc_mult(s)
        if alloc <= 0:
            return skip(f"skipped: {alloc_note}")
        if alloc != 1:
            scale *= alloc
            notes.append(alloc_note)
        rank_block = self._rank_block(s)
        if rank_block:
            return skip(f"skipped: {rank_block}", "ranking")

        cash = self.ex.total_cash_krw()
        equity = self.equity_krw(cash)
        trades_today, day_pnl = self.day_stats(equity)
        positions = self.swing_positions()
        group = self.cfg.group_of(s.key) or []
        equity, cash_cap = self.capped(equity, self.ex.cash_krw(s.market))
        swing_equity = equity * (1 - self.cfg.core.pct / 100) if self.cfg.core.enabled else equity
        plan = self.risk.plan_buy(
            market=s.market, price=price, atr=sig.atr, fx=fx, fee_rate=self.cfg.fee_rate(s.market),
            equity_krw=swing_equity, cash_krw=cash_cap, n_open=len(positions),
            trades_today=trades_today, day_pnl_pct=day_pnl,
            group_open=sum(1 for p in positions if p.key in group),
            max_per_group=self.cfg.correlation.max_per_group if group else 0,
            open_risk_krw=self.open_risk_krw(), risk_scale=scale)
        if not plan.ok:
            if "daily loss limit" in plan.reason:
                self.notify(f"🛑 Daily loss limit hit ({day_pnl:.2f}%). No new buys until tomorrow.",
                            key=f"dll:{day_start_utc()[:10]}")
            return skip(f"blocked by risk: {plan.reason}")

        review = self.ai.review(
            symbol=s.symbol, market=s.market, price=price, reason=sig.reason, snapshot=sig.snapshot,
            recent_closes=list(df["close"].tail(20)), stop=plan.stop, target=plan.target,
            portfolio={"equity_krw": round(equity), "cash_krw": round(cash),
                       "open_positions": [p.symbol for p in positions], "today_pnl_pct": round(day_pnl, 2)})
        if notes:
            sig.reason += " [" + "; ".join(notes) + "]"
        if not review.approved:
            skip(f"AI {review.decision}: {review.reason}", "ai_veto" if review.decision == "reject" else None)
            self.store.event(f"{s.symbol}: buy vetoed — {review.reason}")
            return

        fill = self.ex.buy(s, plan.qty, price, fx)
        if not fill.ok:
            skip(f"order failed: {fill.message}")
            self.store.event(f"{s.symbol}: buy failed — {fill.message}", "error")
            self.notify(f"⚠️ {s.symbol} buy failed: {fill.message}", key=f"buyfail:{s.key}")
            return
        stop = fill.price - (price - plan.stop)  # re-anchor to the actual fill price
        target = fill.price + (plan.target - price) if plan.target > 0 else 0.0
        opened = now_iso()
        self.store.save_position(Position(
            key=s.key, symbol=s.symbol, market=s.market, qty=fill.qty, avg_price=fill.price,
            stop=stop, target=target, opened_at=opened, entry_bar=bar_id, entry_reason=sig.reason,
            initial_stop=stop, highest=fill.price, entry_atr=sig.atr))
        self.store.set(f"entered_bar:{s.key}", bar_id)
        self.store.set(f"pos_extra:{s.key}", {"entry0": fill.price, "init_qty": fill.qty, "partial": False, "adds": 0})
        slip = (fill.price / price - 1) * 100
        self.store.add_trade(
            key=s.key, symbol=s.symbol, market=s.market, side="BUY", qty=fill.qty, price=fill.price,
            value_krw=fill.qty * fill.price * fx, fee_krw=fill.fee_krw, pnl_krw=None, reason=sig.reason,
            ai_decision=review.decision, ai_reason=review.reason, mode=self.ex.mode, order_id=fill.order_id,
            expected_price=price, slippage_pct=slip, fx_usdkrw=self._usdkrw(), opened_at=opened)
        self.store.add_signal(s.key, "BUY", sig.reason, "executed", sig.snapshot)
        self.store.event(f"BUY {s.symbol} {fill.qty:g} @ {_fmt(fill.price)}")
        if self.cfg.alerts.trades:
            self.notify(f"🟢 BUY {s.symbol} ({MKT[s.market]}) {fill.qty:g} @ {_fmt(fill.price)}\n"
                        f"≈ ₩{fill.qty * fill.price * fx:,.0f} · stop {_fmt(stop)}\n{sig.reason}"
                        + (f"\n🤖 {review.reason}" if review.decision == "approve" else "")
                        + (" [paper]" if self.ex.mode == "paper" else ""))

    def _alloc_mult(self, s: SymbolCfg) -> tuple[float, str]:
        """Risk multiplier from the symbol's latest monthly re-test."""
        a = self.cfg.allocation
        if not a.enabled:
            return 1.0, ""
        r = self.store.get(f"retest:{s.key}") or {}
        if "pass" not in r:
            return 1.0, ""
        if not r["pass"]:
            return a.fail_mult, (f"risk ×{a.fail_mult:g}: failed the last re-test" if a.fail_mult > 0
                                 else "failed the last walk-forward re-test (allocation 0)")
        if (r.get("profit_factor") or 0) >= a.strong_pf:
            return a.strong_mult, f"risk ×{a.strong_mult:g}: strong re-test (PF {r['profit_factor']})"
        return 1.0, ""

    def _scale(self, s: SymbolCfg, pos: Position | None, price: float, fx: float) -> None:
        """Partial profit-taking and adding to winners (pyramiding), if enabled in [risk]."""
        r = self.cfg.risk
        if pos is None or (r.partial_r <= 0 and r.pyramid_r <= 0):
            return
        key = f"pos_extra:{s.key}"
        ex = self.store.get(key) or {"entry0": pos.avg_price, "init_qty": pos.qty, "partial": False, "adds": 0}
        one_r = ex["entry0"] - (pos.initial_stop or pos.stop)
        if one_r <= 0:
            return
        from .risk import round_qty
        if r.partial_r > 0 and not ex["partial"] and price >= ex["entry0"] + r.partial_r * one_r:
            q = round_qty(pos.qty * r.partial_pct / 100, s.market)
            if 0 < q < pos.qty and q * price * fx >= r.min_order_krw:
                ex["partial"] = True
                self.store.set(key, ex)
                self._sell(s, pos, price, fx, f"partial profit: +{r.partial_r:g}R reached, selling {r.partial_pct:g}%", qty=q)
                pos = self.store.position(s.key)
                if pos is None:
                    return
        if (r.pyramid_r > 0 and ex["adds"] == 0 and price >= ex["entry0"] + r.pyramid_r * one_r
                and pos.stop >= ex["entry0"] and not self.paused):
            q = round_qty(ex["init_qty"] * r.pyramid_pct / 100, s.market)
            equity = self.equity_krw()
            room = equity * r.max_total_risk_pct / 100 - self.open_risk_krw() if r.max_total_risk_pct > 0 else float("inf")
            _, cash = self.capped(equity, self.ex.cash_krw(s.market))
            cost = q * price * fx
            if q > 0 and cost >= r.min_order_krw and cost <= cash * 0.995 and q * (price - pos.stop) * fx <= room:
                fill = self.ex.buy(s, q, price, fx)
                if fill.ok:
                    pos.avg_price = (pos.qty * pos.avg_price + fill.qty * fill.price) / (pos.qty + fill.qty)
                    pos.qty += fill.qty
                    self.store.save_position(pos)
                    ex["adds"] = 1
                    self.store.set(key, ex)
                    reason = f"adding to a winner: +{r.pyramid_r:g}R with the stop at break-even"
                    self.store.add_trade(
                        key=s.key, symbol=s.symbol, market=s.market, side="BUY", qty=fill.qty, price=fill.price,
                        value_krw=fill.qty * fill.price * fx, fee_krw=fill.fee_krw, pnl_krw=None, reason=reason,
                        ai_decision=None, ai_reason=None, mode=self.ex.mode, order_id=fill.order_id,
                        expected_price=price, slippage_pct=(fill.price / price - 1) * 100, fx_usdkrw=self._usdkrw(),
                        opened_at=pos.opened_at)
                    self.store.event(f"ADD {s.symbol} {fill.qty:g} @ {_fmt(fill.price)} ({reason})")
                    if self.cfg.alerts.trades:
                        self.notify(f"➕ ADD {s.symbol} {fill.qty:g} @ {_fmt(fill.price)} — {reason}")
        self.store.set(key, ex)

    def _rank_block(self, s: SymbolCfg) -> str | None:
        """Momentum ranking: only the strongest top_n symbols (by daily-close return) may be bought."""
        rc = self.cfg.ranking
        if not rc.enabled:
            return None
        scores = {}
        for x in self.cfg.symbols:
            if not x.enabled:
                continue
            try:
                c = self.data.daily_closes(x, rc.lookback_days + 1)
                if len(c) > rc.lookback_days // 2:
                    scores[x.key] = float(c.iloc[-1] / c.iloc[0] - 1) * 100
            except Exception:
                continue
        self.store.set("momentum_scores", {k: round(v, 2) for k, v in scores.items()})
        if s.key not in scores or len(scores) <= rc.top_n:
            return None
        top = sorted(scores, key=scores.get, reverse=True)[: rc.top_n]
        if s.key in top:
            return None
        rank = sorted(scores, key=scores.get, reverse=True).index(s.key) + 1
        return f"ranked #{rank} of {len(scores)} by {rc.lookback_days}-day strength (only top {rc.top_n} are bought)"

    def _usdkrw(self) -> float:
        return self.fxr.usd_krw() if self.fxr else self.cfg.usd_krw

    def _trail(self, s: SymbolCfg, pos: Position, price: float) -> None:
        """Raise (never lower) the stop: break-even after +1R, then trail below the highest price."""
        new_stop = trailed_stop(pos, price, self.cfg.risk, self.cfg.fee_rate(s.market))
        high = max(pos.highest or pos.avg_price, price)
        if new_stop > pos.stop or high > pos.highest:
            if new_stop > pos.stop and pos.stop < pos.avg_price <= new_stop:
                self.store.event(f"{s.symbol}: stop moved to break-even ({_fmt(new_stop)})")
                if self.cfg.alerts.stops:
                    self.notify(f"🔒 {s.symbol}: stop moved to break-even ({_fmt(new_stop)}). This trade can no longer lose money (except gaps).")
            pos.stop, pos.highest = max(pos.stop, new_stop), high
            self.store.save_position(pos)

    def _sell(self, s: SymbolCfg, pos: Position, price: float, fx: float, reason: str, qty: float | None = None) -> None:
        fill = self.ex.sell(s, qty if qty else pos.qty, price, fx)
        if not fill.ok:
            self.store.event(f"{s.symbol}: SELL failed — {fill.message}", "error")
            self.notify(f"🚨 {s.symbol} SELL FAILED: {fill.message}\nCheck the broker app now.", key=f"sellfail:{s.key}")
            return
        buy = next((t for t in self.store.trades(500) if t["key"] == s.key and t["side"] == "BUY"), None)
        buy_fx = fx if not is_usd(s.market) else ((buy or {}).get("fx_usdkrw") or fx)
        cost_basis = fill.qty * pos.avg_price * buy_fx
        pnl = fill.qty * fill.price * fx - cost_basis - fill.fee_krw
        if buy and buy["qty"]:
            pnl -= (buy["fee_krw"] or 0) * min(1.0, fill.qty / buy["qty"])  # include the buy-side fee
        remaining = pos.qty - fill.qty
        if remaining * price * fx >= 1000:  # keep the rest (partial sell); drop crypto dust
            pos.qty = remaining
            self.store.save_position(pos)
        else:
            self.store.delete_position(s.key)
            self.store.set(f"pos_extra:{s.key}", None)
        self.store.add_trade(
            key=s.key, symbol=s.symbol, market=s.market, side="SELL", qty=fill.qty, price=fill.price,
            value_krw=fill.qty * fill.price * fx, fee_krw=fill.fee_krw, pnl_krw=pnl, reason=reason,
            ai_decision=None, ai_reason=None, mode=self.ex.mode, order_id=fill.order_id,
            expected_price=price, slippage_pct=(1 - fill.price / price) * 100, fx_usdkrw=self._usdkrw(),
            cost_basis_krw=cost_basis, opened_at=pos.opened_at)
        self.store.event(f"SELL {s.symbol} {fill.qty:g} @ {_fmt(fill.price)} — P&L ₩{pnl:,.0f} ({reason})")
        if self.cfg.alerts.trades:
            self.notify(f"{'✅' if pnl > 0 else '🔴'} SELL {s.symbol} {fill.qty:g} @ {_fmt(fill.price)}\n"
                        f"P&L ₩{pnl:,.0f} · {reason}" + (" [paper]" if self.ex.mode == "paper" else ""))

    # ── reports ──
    def summary_text(self) -> str:
        cash = self.ex.total_cash_krw()
        eq = self.equity_krw(cash)
        trades_today, day_pnl = self.day_stats(eq)
        sells = [t for t in self.store.trades_since(day_start_utc()) if t["side"] == "SELL"]
        realized = sum(t["pnl_krw"] or 0 for t in sells)
        sigs = [x for x in self.store.signals(500) if x["ts"] >= day_start_utc()]
        filt = sum(1 for x in sigs if x["outcome"] == "filtered")
        veto = sum(1 for x in sigs if x["outcome"].startswith("AI reject"))
        regime = sum(1 for x in sigs if "market regime" in x["outcome"])
        lines = [f"📋 TradeBot daily summary ({'PAPER' if self.ex.mode == 'paper' else 'LIVE'})",
                 f"Equity ₩{eq:,.0f} ({day_pnl:+.2f}% today)",
                 f"Buys {trades_today} · sells {len(sells)} · realized ₩{realized:,.0f}",
                 f"Blocked today: {filt} by filters, {regime} by market regime, {veto} by Claude"]
        lt = [p for p in self.store.positions() if p not in self.swing_positions()]
        pos = self.swing_positions()
        if lt:
            lines.append("Long-term: " + ", ".join(f"{p.symbol} ({p.key.split(':')[0]})" for p in lt))
        cautions = [d.reason for m in ("kis_us", "upbit", "binance") for d in self.signals.check(m)]
        if cautions:
            lines.append("Caution: " + "; ".join(dict.fromkeys(cautions)))
        if pos:
            lines.append("Open: " + ", ".join(
                f"{p.symbol} {(self.last_prices.get(p.key, p.avg_price) / p.avg_price - 1) * 100:+.1f}%" for p in pos))
        sh = self.shadow.summary()["ai_veto"]
        if sh["closed"]:
            lines.append(f"Claude vetoes so far: {sh['verdict']}")
        if self.paused:
            lines.append("⏸ Bot is PAUSED")
        return "\n".join(lines)

    def reconcile(self) -> list[str]:
        """Live mode: does the broker actually hold what the bot thinks it holds?"""
        problems = []
        by_market: dict[str, dict] = {}
        try:
            if self.data.upbit and self.data.upbit.access_key:
                by_market["upbit"] = {f"KRW-{a['currency']}": float(a["balance"]) + float(a["locked"])
                                      for a in self.data.upbit.accounts()}
            bn = getattr(self.data, "binance", None)
            if bn and bn.api_key:
                bal = bn.balances()
                by_market["binance"] = {p.symbol: bal.get(bn.symbol_info(p.symbol)["base"], 0.0)
                                        for p in self.store.positions() if p.market == "binance"}
            if self.data.kis:
                by_market["kis_kr"] = {k: v[0] for k, v in self.data.kis.kr_balance()[0].items()}
                us = {}
                for exch in {s.exchange for s in self.cfg.symbols if s.market == "kis_us"}:
                    us.update({k: v[0] for k, v in self.data.kis.us_holdings(exch).items()})
                by_market["kis_us"] = us
        except Exception as e:
            return [f"couldn't read broker balances: {e}"]
        bot: dict[tuple[str, str], float] = {}
        for p in self.store.positions():  # swing + long-term holdings of the same symbol add up
            bot[(p.market, p.symbol)] = bot.get((p.market, p.symbol), 0.0) + p.qty
        for (market, symbol), qty in bot.items():
            held = by_market.get(market, {}).get(symbol)
            if held is None and market in by_market:
                problems.append(f"{symbol}: bot holds {qty:g} but the broker shows none")
            elif held is not None and held < qty * 0.99:
                problems.append(f"{symbol}: bot holds {qty:g} but the broker shows only {held:g}")
        self.store.set("reconcile_result", {"ts": now_iso(), "problems": problems})
        self.store.event("position check: " + ("OK" if not problems else "; ".join(problems)),
                         "info" if not problems else "warn")
        return problems

    # ── kill switch ──
    def kill(self) -> list[str]:
        """Sell everything at market and pause. Positions in closed markets are left with a warning."""
        msgs = []
        with self.lock:
            self.store.set("paused", True)
            for p in self.store.positions():
                s = self.cfg.symbol(p.key)
                if s is None and self.longterm.symcfg_for(p.key):
                    lt = self.longterm.symcfg_for(p.key)
                    if not self.data.is_open(lt):
                        msgs.append(f"{p.symbol} ({p.key.split(':')[0]}): market closed — sell manually")
                    elif self.longterm._sell(p.key, lt, p.qty, "kill switch"):
                        msgs.append(f"{p.symbol} ({p.key.split(':')[0]}): sold")
                    continue
                if s is None:
                    msgs.append(f"{p.symbol}: not in watchlist, sell manually")
                    continue
                if not self.data.is_open(s):
                    msgs.append(f"{p.symbol}: market closed — will NOT sell; sell manually or resume later")
                    continue
                try:
                    price = self.data.price(s)
                    self._sell(s, p, price, self.data.fx(s), "kill switch")
                    msgs.append(f"{p.symbol}: sold")
                except Exception as e:
                    msgs.append(f"{p.symbol}: sell error {e}")
        self.store.event("KILL SWITCH: " + "; ".join(msgs or ["no positions"]), "warn")
        self.notify("🛑 KILL SWITCH used:\n" + "\n".join(msgs or ["no positions"]))
        return msgs


class Runner(threading.Thread):
    """Runs Engine.tick() every loop_seconds in the background."""

    def __init__(self, engine: Engine, interval: int, extras: list | None = None):
        super().__init__(daemon=True, name="engine")
        self.engine, self.interval = engine, interval
        self.extras = extras or []  # side-by-side paper accounts, ticked after the main bot
        self._halt = threading.Event()
        self._wake = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            self.engine.tick()
            for name, eng in self.extras:
                try:
                    eng.tick()
                except Exception as ex:  # a variant must never disturb the main bot
                    log.warning("variant %s tick failed: %s", name, ex)
            self._wake.wait(self.interval)
            self._wake.clear()

    def poke(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
