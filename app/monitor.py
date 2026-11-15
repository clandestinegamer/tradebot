"""Background safety checks that run after every tick:

• drift auto-pause — pause new buys if equity drops too far from its peak, or if recent trades'
  profit factor collapses (the market stopped behaving the way the tests assumed)
• daily summary   — one message every evening
• reconciliation  — live mode: compare what the bot thinks it holds with what Upbit/KIS actually hold
• monthly re-test — re-run the walk-forward test on fresh data and report which symbols still pass
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger("tradebot")
SEOUL = ZoneInfo("Asia/Seoul")


class Monitor:
    def __init__(self, engine):
        self.e = engine
        self._retest_thread: threading.Thread | None = None

    @property
    def cfg(self):
        return self.e.cfg

    def after_tick(self, equity: float) -> None:
        steps = ((self.drift, (equity,)), (self.daily_summary, ()), (self.reconcile_daily, ()),
                 (self.retest_monthly, ()), (self.nightly_backup, ()), (self.weekly_review, ()),
                 (self.tax_reminder, ()))
        for fn, args in steps:
            try:
                fn(*args)
            except Exception as ex:
                log.warning("monitor step %s failed: %s", fn.__name__, ex)

    # ── drift ──
    def drift(self, equity: float) -> None:
        st, m = self.e.store, self.cfg.monitor
        peak = st.get("equity_peak") or equity
        if equity >= peak:
            st.set("equity_peak", equity)
            st.set("dd_ack", None)
            peak = equity
        dd = (1 - equity / peak) * 100 if peak else 0.0
        st.set("drawdown_pct", round(dd, 2))
        if not m.auto_pause:
            return
        ack = st.get("dd_ack")
        if dd >= m.max_drawdown_pct and (ack is None or dd >= ack + m.max_drawdown_pct / 2):
            st.set("dd_ack", dd)
            self._auto_pause(f"equity is {dd:.1f}% below its peak (limit {m.max_drawdown_pct}%)")

        from .longterm import is_longterm  # long-term rebalances / grid steps aren't swing results
        sells = [t for t in st.all_trades()
                 if t["side"] == "SELL" and t["pnl_krw"] is not None and not is_longterm(t["key"])]
        n = m.drift_min_trades
        if len(sells) >= n and len(sells) >= (st.get("pf_ack") or 0) + n:
            recent = sells[-n:]
            wins = sum(t["pnl_krw"] for t in recent if t["pnl_krw"] > 0)
            losses = -sum(t["pnl_krw"] for t in recent if t["pnl_krw"] <= 0)
            pf = wins / losses if losses > 0 else float("inf")
            st.set("recent_profit_factor", None if pf == float("inf") else round(pf, 2))
            if pf < m.min_profit_factor:
                st.set("pf_ack", len(sells))
                self._auto_pause(f"last {n} trades have a profit factor of {pf:.2f} (limit {m.min_profit_factor})")

    def _auto_pause(self, why: str) -> None:
        if not self.e.paused:
            self.e.set_paused(True)
        self.e.store.event(f"AUTO-PAUSE: {why}. Review, then resume from the dashboard.", "warn")
        self.e.notify(f"⏸ TradeBot auto-paused: {why}.\nExits keep running. Resume from the dashboard when ready.")

    # ── daily summary ──
    def daily_summary(self) -> None:
        t = self.cfg.alerts.summary_time
        if not t:
            return
        now = datetime.now(SEOUL)
        if now.time() < dtime.fromisoformat(t) or self.e.store.get("summary_sent") == now.date().isoformat():
            return
        self.e.store.set("summary_sent", now.date().isoformat())
        self.e.notify(self.e.summary_text())

    # ── reconciliation ──
    def reconcile_daily(self) -> None:
        if not (self.cfg.is_live and self.cfg.monitor.reconcile):
            return
        now = datetime.now(SEOUL)
        if now.time() < dtime(10, 0) or self.e.store.get("reconciled") == now.date().isoformat():
            return
        self.e.store.set("reconciled", now.date().isoformat())
        problems = self.e.reconcile()
        if problems:
            self.e.notify("⚠️ Position check found problems:\n• " + "\n• ".join(problems))

    # ── Claude's weekly review (Sunday evening) ──
    def weekly_review(self) -> None:
        if not self.cfg.weekly_review or getattr(self.e, "variant_name", None) or self.e.ai.client is None:
            return
        now = datetime.now(SEOUL)
        week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if now.weekday() != 6 or now.time() < dtime(20, 0) or self.e.store.get("review_week") == week:
            return
        self.e.store.set("review_week", week)
        threading.Thread(target=self.run_review, daemon=True, name="weekly-review").start()

    def run_review(self) -> str:
        from .ask import WEEKLY_QUESTION, ask
        try:
            text = ask(self.e, WEEKLY_QUESTION, count=False)
        except Exception as ex:
            self.e.store.event(f"weekly review failed: {ex}", "error")
            return ""
        self.e.store.set("weekly_review", {"date": datetime.now(SEOUL).date().isoformat(), "text": text})
        self.e.notify("🗒 Weekly TradeBot review\n\n" + text[:3500])
        return text

    # ── year-end tax reminder (Dec 1) ──
    def tax_reminder(self) -> None:
        if getattr(self.e, "variant_name", None):
            return
        now = datetime.now(SEOUL)
        if now.month != 12 or self.e.store.get("tax_reminder_year") == now.year:
            return
        self.e.store.set("tax_reminder_year", now.year)
        from .tax import harvest_review
        r = harvest_review(self.e)
        if not r["candidates"]:
            return
        lines = [f"• {c['symbol']} ({c['kind']}): {c['unrealized_krw']:+,.0f} KRW / {c['unrealized_usd']:+,.0f} USD"
                 for c in r["candidates"][:8]]
        self.e.notify("🧾 Year-end tax review\n"
                      f"Realized this year: {r['realized_ytd_krw']:+,.0f} KRW ({r['realized_ytd_usd']:+,.0f} USD)\n"
                      "Positions currently at a loss (selling before Dec 31 could offset gains):\n" + "\n".join(lines) +
                      "\n\nCheck with your tax advisor first — US wash-sale rules can apply if you rebuy within 30 days.")

    # ── nightly backup ──
    def nightly_backup(self) -> None:
        now = datetime.now(SEOUL)
        if getattr(self.e, "variant_name", None) or self.e.store.conn is None:
            return
        if now.time() < dtime(3, 0) or self.e.store.get("backup_day") == now.date().isoformat():
            return
        if self.e.store.path == ":memory:":
            return
        self.e.store.set("backup_day", now.date().isoformat())
        self.run_backup()

    def run_backup(self) -> dict:
        import os
        from pathlib import Path
        from .backup import make_backup, ship
        path = make_backup(self.e.store, Path(self.e.store.path).parent / "backups",
                           os.environ.get("TRADEBOT_PASSPHRASE", ""))
        shipped = ship(path, self.e.notifier)
        res = {"ts": datetime.now(SEOUL).isoformat(timespec="minutes"), "file": path.name,
               "size_kb": round(path.stat().st_size / 1024), "shipped": shipped}
        self.e.store.set("backup_last", res)
        self.e.store.event(f"backup {path.name} ({res['size_kb']} KB) " + ("; ".join(shipped) or "kept on the server only"))
        return res

    # ── monthly walk-forward re-test ──
    def retest_monthly(self) -> None:
        day = self.cfg.monitor.retest_day
        now = datetime.now(SEOUL)
        month = now.strftime("%Y-%m")
        if not day or now.day < day or self.e.store.get("retest_month") == month:
            return
        self.e.store.set("retest_month", month)
        self.start_retest()

    def start_retest(self) -> bool:
        if self._retest_thread and self._retest_thread.is_alive():
            return False
        self._retest_thread = threading.Thread(target=self._retest, daemon=True, name="retest")
        self._retest_thread.start()
        return True

    def _retest(self) -> None:
        from .backtest import looks_tradeable, verdict, walk_forward
        e, lines = self.e, []
        e.store.event("monthly walk-forward re-test started")
        for s in [x for x in self.cfg.symbols if x.enabled]:
            try:
                df = e.history(s, self.cfg.monitor.retest_bars)
                fx = e.data.fx(s)
                wf = walk_forward(df, s.strategy, s.market, self.cfg.risk, self.cfg.fee_rate(s.market), fx,
                                  self.cfg.starting_cash_krw)
                ok = looks_tradeable(wf)
                res = {"date": datetime.now(SEOUL).date().isoformat(), "pass": ok,
                       "oos_return_pct": round(wf["oos_return_pct"], 2),
                       "hold_same_size_pct": round(wf["oos_hold_same_size_pct"], 2),
                       "profit_factor": None if wf["oos_profit_factor"] == float("inf") else round(wf["oos_profit_factor"], 2),
                       "trades": wf["oos_trades"], "notes": verdict(wf),
                       "recommended_params": wf["recommended_params"] if ok else None}
                e.store.set(f"retest:{s.key}", res)
                lines.append(f"{'✅' if ok else '❌'} {s.symbol}: {res['oos_return_pct']:+.1f}% unseen "
                             f"vs hold {res['hold_same_size_pct']:+.1f}% ({res['trades']} trades)")
            except Exception as ex:
                e.store.set(f"retest:{s.key}", {"date": datetime.now(SEOUL).date().isoformat(), "error": str(ex)})
                lines.append(f"⚠️ {s.symbol}: re-test failed ({ex})")
        e.store.event("re-test finished: " + "; ".join(lines))
        e.notify("📊 Monthly strategy re-test\n" + "\n".join(lines) +
                 "\n\nSymbols marked ❌ no longer pass — consider disabling them in Settings.")
