"""FastAPI server: runs the bot in the background and serves the phone dashboard + API."""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .ai_review import AIReviewer
from .alerts import Notifier
from .brokers.kis import KISClient
from .brokers.upbit import UpbitClient
from .config import EDITABLE_RISK, Config, load_config, symbols_to_dicts
from .engine import Engine, Runner, is_crypto_weekend
from .execution import LiveExecutor, PaperExecutor
from .fx import FXRates
from .market import MarketData
from .security import TOTPVerifier, LoginLimiter, check_session, hash_session, new_session
from .storage import Store
from .strategies import STRATEGIES
from .tax import closed_trades, to_csv
from .variants import account_stats, build_variants

DASH = Path(__file__).resolve().parent.parent / "dashboard"
log = logging.getLogger("tradebot")


def build_engine(cfg: Config, store: Store | None = None, ai_client=None, notifier=None) -> Engine:
    s = cfg.secrets
    store = store or Store(cfg.db_path)
    upbit = UpbitClient(s.upbit_access, s.upbit_secret)
    kis = None
    if s.kis_app_key:
        kis = KISClient(s.kis_app_key, s.kis_app_secret, s.kis_account_no, s.kis_account_product, s.kis_env)
    if cfg.data_source == "live" and any(x.market.startswith("kis") for x in cfg.symbols) and kis is None:
        # Start anyway: crypto trades, and stock symbols are skipped until KIS keys are added.
        log.warning("KIS keys missing in .env: stock symbols are paused, crypto keeps trading")
        store.event("KIS keys not set up yet: stock symbols are paused until they're added to .env", "warn")
    fx = FXRates(store, cfg.usd_krw, online=cfg.data_source == "live")
    data = MarketData(cfg, upbit=upbit, kis=kis)
    data.fx_rates = fx
    ex = LiveExecutor(cfg, upbit, kis) if cfg.is_live else PaperExecutor(cfg, store)
    ai = AIReviewer(cfg.ai, s.anthropic_key, client=ai_client)
    notifier = notifier or Notifier(s, store, cfg.alerts.error_cooldown_minutes)
    return Engine(cfg, store, data, ex, ai, notifier=notifier, fx=fx)


class LoginBody(BaseModel):
    token: str
    code: str = ""
    device: str = ""


class KillBody(BaseModel):
    confirm: str


class AskBody(BaseModel):
    question: str


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


def _candle_utc(market: str, ts: str, demo: bool) -> datetime | None:
    """Candle timestamp → UTC, so trades can be placed on a chart."""
    try:
        if demo:
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        if market == "upbit":
            return datetime.fromisoformat(ts).replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(timezone.utc)
        d = datetime.strptime(ts, "%Y%m%d")
        if market == "kis_kr":
            return d.replace(hour=9, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(timezone.utc)
        return d.replace(hour=9, minute=30, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    except ValueError:
        return None


def build_app(cfg: Config | None = None, engine: Engine | None = None, run_loop: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    engine = engine or build_engine(cfg)
    token = cfg.secrets.dashboard_token
    if not token:
        token = secrets.token_urlsafe(18)
        log.warning("DASHBOARD_TOKEN not set — temporary password for this run: %s", token)
        print(f"\n  Dashboard password (set DASHBOARD_TOKEN in .env to make it permanent): {token}\n")
    totp = TOTPVerifier(cfg.secrets.totp_secret) if cfg.secrets.totp_secret else None
    limiter = LoginLimiter()
    try:
        variants = build_variants(engine)
    except ValueError as ex:
        log.error("side-by-side accounts disabled: %s", ex)
        engine.store.event(f"side-by-side accounts disabled: {ex}", "error")
        variants = []
    app_variants = variants
    runner = Runner(engine, cfg.loop_seconds, variants)
    from .realtime import StopWatcher
    watcher = StopWatcher(engine) if cfg.execution.fast_stops else None
    app_watcher = watcher
    store = engine.store

    @asynccontextmanager
    async def lifespan(app):
        if cfg.is_live:
            store.event("LIVE TRADING MODE — real orders will be placed", "warn")
        if not totp:
            store.event("two-step login is OFF — run `python -m app.security totp-setup` to turn it on", "warn")
        store.event(f"bot started ({cfg.trading_mode} mode, {cfg.data_source} data)")
        engine.notify(f"▶️ TradeBot started ({cfg.trading_mode} mode, {cfg.data_source} data)")
        if run_loop:
            runner.start()
            if watcher:
                watcher.start()
        yield
        runner.stop()
        if watcher:
            watcher.stop()

    app = FastAPI(title="TradeBot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.variants = app_variants
    app.state.watcher = app_watcher

    def auth(authorization: str = Header(default="")) -> dict:
        s = check_session(store, authorization.removeprefix("Bearer ").strip())
        if not s:
            raise HTTPException(401, "not logged in")
        return s

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    # ── static ──
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(DASH / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(DASH / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg", include_in_schema=False)
    def icon():
        return FileResponse(DASH / "icon.svg", media_type="image/svg+xml")

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return FileResponse(DASH / "sw.js", media_type="text/javascript")

    # ── Android app updates (no login: the APK holds no secrets, and Android only
    #    installs an update signed with the same key as the app already on the phone) ──
    app_dir = None if str(store.path) == ":memory:" else Path(store.path).resolve().parent / "app"

    @app.get("/app/version.json", include_in_schema=False)
    def app_version():
        f = app_dir / "version.json" if app_dir else None
        if not f or not f.is_file():
            raise HTTPException(404, "no app update published on this server yet")
        return FileResponse(f, media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.get("/app/TradeBot.apk", include_in_schema=False)
    def app_apk():
        f = app_dir / "TradeBot.apk" if app_dir else None
        if not f or not f.is_file():
            raise HTTPException(404, "no app update published on this server yet")
        return FileResponse(f, media_type="application/vnd.android.package-archive", filename="TradeBot.apk",
                            headers={"Cache-Control": "no-store"})

    # ── health (no login; reveals nothing sensitive) ──
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        last = engine.last_tick
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
        ok = age is not None and age < cfg.loop_seconds * 3 + 60
        return JSONResponse({"ok": ok, "seconds_since_check": None if age is None else round(age)},
                            status_code=200 if ok else 503)

    # ── guardian (backup stop-keeper on another machine) ──
    @app.get("/api/guardian", include_in_schema=False)
    def guardian(x_guardian_token: str = Header(default="")):
        gt = os.environ.get("GUARDIAN_TOKEN", "")
        if not gt or not hmac.compare_digest(x_guardian_token.encode(), gt.encode()):
            raise HTTPException(401, "bad guardian token")
        store.set("guardian_seen", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        out = []
        for p in engine.swing_positions():
            s = engine.cfg.symbol(p.key)
            out.append({"key": p.key, "symbol": p.symbol, "market": p.market, "qty": p.qty, "stop": p.stop,
                        "exchange": s.exchange if s else "NASD"})
        return {"ts": datetime.now(timezone.utc).isoformat(), "last_tick": engine.last_tick, "positions": out}

    # ── login / sessions ──
    @app.get("/api/login-info")
    def login_info():
        return {"two_step": bool(totp)}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        ip = _client_ip(request)
        if limiter.blocked(ip):
            raise HTTPException(429, "too many failed attempts — try again in 15 minutes")
        ok = hmac.compare_digest(body.token.encode(), token.encode()) and (totp is None or totp.verify(body.code))
        if not ok:
            n = limiter.fail(ip)
            store.event(f"failed dashboard login from {ip} ({n} recent)", "warn")
            if n >= 3:
                engine.notify(f"🔐 {n} failed TradeBot logins from {ip}. If this wasn't you, change DASHBOARD_TOKEN.",
                              key=f"loginfail:{ip}")
            raise HTTPException(401, "wrong password or code")
        limiter.reset(ip)
        device = body.device or request.headers.get("user-agent", "")[:80]
        sid = new_session(store, device, ip)
        store.event(f"new dashboard login: {device[:40]} from {ip}")
        engine.notify(f"🔐 New TradeBot login: {device[:60]} ({ip})", key=f"login:{ip}:{device[:20]}")
        return {"session": sid}

    @app.get("/api/sessions")
    def sessions(me: dict = Depends(auth)):
        return [{"id": s["id"][:12], "device": s["device"], "created": s["created"], "last_seen": s["last_seen"],
                 "ip": s["ip"], "current": s["id"] == me["id"]} for s in store.sessions()]

    @app.delete("/api/sessions/{prefix}")
    def revoke(prefix: str, me: dict = Depends(auth)):
        hits = [s for s in store.sessions() if s["id"].startswith(prefix)]
        if len(prefix) < 8 or len(hits) != 1:
            raise HTTPException(404, "session not found")
        store.delete_session(hits[0]["id"])
        return {"revoked": prefix}

    @app.post("/api/sessions/revoke-others")
    def revoke_others(me: dict = Depends(auth)):
        for s in store.sessions():
            if s["id"] != me["id"]:
                store.delete_session(s["id"])
        store.event("all other dashboard sessions signed out")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(me: dict = Depends(auth)):
        store.delete_session(me["id"])
        return {"ok": True}

    # ── status ──
    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        e = engine
        c = e.cfg
        try:
            cash = e.ex.total_cash_krw()
        except Exception as ex:
            cash, e.last_error = 0.0, f"cash lookup: {ex}"
        equity = e.equity_krw(cash)
        trades_today, day_pnl = e.day_stats(equity)
        start = store.get("start_equity") or equity
        positions = []
        for p in store.positions():
            s = c.symbol(p.key)
            fx = e.data.fx(s) if s else 1.0
            px = e.last_prices.get(p.key, p.avg_price)
            positions.append({**p.to_dict(), "price": px, "value_krw": p.qty * px * fx,
                              "pnl_krw": p.qty * (px - p.avg_price) * fx,
                              "pnl_pct": (px / p.avg_price - 1) * 100 if p.avg_price else 0})
        recent = [t for t in store.trades(50) if t.get("slippage_pct") is not None]
        return {
            "mode": c.trading_mode, "data_source": c.data_source, "kis_env": c.secrets.kis_env,
            "paused": e.paused, "ai_active": e.ai.active, "ai_enabled": c.ai.enabled,
            "equity_krw": equity, "cash_krw": cash, "day_pnl_pct": day_pnl, "trades_today": trades_today,
            "total_pnl_pct": (equity / start - 1) * 100 if start else 0,
            "drawdown_pct": store.get("drawdown_pct") or 0, "recent_profit_factor": store.get("recent_profit_factor"),
            "positions": positions, "last_tick": e.last_tick, "last_error": e.last_error,
            "loop_seconds": c.loop_seconds,
            "avg_slippage_pct": round(sum(t["slippage_pct"] for t in recent) / len(recent), 3) if recent else None,
            "alert_channels": e.notifier.channels if e.notifier else [],
            "two_step": bool(totp),
            "watchlist": [{"key": s.key, "symbol": s.symbol, "market": s.market, "strategy": s.strategy,
                           "interval": s.interval, "open": e.data.is_open(s), "enabled": s.enabled, "inverse": s.inverse,
                           "price": e.last_prices.get(s.key), "retest": store.get(f"retest:{s.key}")}
                          for s in c.symbols],
            "risk": {k: getattr(c.risk, k) for k in EDITABLE_RISK},
            "regime": {"enabled": c.regime.enabled,
                       "status": {m: getattr(e.data, "_regime_cache", {}).get(m, (0, None, ""))[2]
                                  for m in ("upbit", "kis_kr", "kis_us")}},
            "reconcile": store.get("reconcile_result"),
            "backup_last": store.get("backup_last"), "guardian_seen": store.get("guardian_seen"),
            "live_capital_krw": c.live_capital_krw if c.is_live else 0,
            "fast_stops": getattr(app_watcher, "status", None) if app_watcher else "off",
            "open_risk_pct": e.open_risk_krw() / equity * 100 if equity else 0.0,
            "usd": e.usd_exposure(),
            "weekend_crypto": is_crypto_weekend(),
            "momentum_scores": store.get("momentum_scores") if c.ranking.enabled else None,
        }

    @app.get("/api/trades", dependencies=[Depends(auth)])
    def trades(limit: int = 50):
        return store.trades(min(limit, 500))

    @app.get("/api/signals", dependencies=[Depends(auth)])
    def signals(limit: int = 50):
        return store.signals(min(limit, 500))

    @app.get("/api/events", dependencies=[Depends(auth)])
    def events(limit: int = 30):
        return store.events(min(limit, 200))

    @app.get("/api/equity", dependencies=[Depends(auth)])
    def equity(days: int = 7):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = store.equity_curve(since)
        step = max(1, len(rows) // 300)  # keep the chart light on phones
        return rows[::step] + ([rows[-1]] if rows and (len(rows) - 1) % step else [])

    @app.get("/api/conditions", dependencies=[Depends(auth)])
    def conditions():
        return engine.signals.status()

    @app.get("/api/longterm", dependencies=[Depends(auth)])
    def longterm():
        from .longterm import LongTerm, is_longterm
        c = engine.cfg
        held = [{**p.to_dict(), "price": engine.last_prices.get(p.key, p.avg_price),
                 "value_krw": p.qty * engine.last_prices.get(p.key, p.avg_price) * engine._fx_for(p.market)}
                for p in store.positions() if is_longterm(p.key)]
        grids = [{"symbol": g["symbol"], "lower": g["lower"], "upper": g["upper"], "levels": g["levels"],
                  "budget_krw": g["budget_krw"], "state": store.get(f"grid_state:{g['symbol']}")} for g in c.grid]
        return {"held": held, "core": {"enabled": c.core.enabled, "pct": c.core.pct, "last": store.get("core_last")},
                "dca": [{**d, "last_period": store.get(f"dca_last:{d['market']}:{d['symbol']}")} for d in c.dca],
                "grids": grids}

    @app.get("/api/variants", dependencies=[Depends(auth)])
    def variants_view():
        rows = [account_stats(engine, "Main bot", f"{engine.cfg.trading_mode} — your real settings")]
        descs = {v.get("name"): v.get("description", "") for v in engine.cfg.variants}
        descs.setdefault("paper-twin", "Exact paper copy of your live settings")
        for name, eng in app_variants:
            try:
                rows.append(account_stats(eng, name, descs.get(name, "")))
            except Exception as ex:
                rows.append({"name": name, "error": str(ex)})
        return rows

    @app.post("/api/ask", dependencies=[Depends(auth)])
    def ask_claude(body: AskBody):
        from .ask import ask
        try:
            stats = variants_view() if app_variants else None
            return {"answer": ask(engine, body.question, stats)}
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        except Exception as ex:
            raise HTTPException(502, f"Claude couldn't answer: {type(ex).__name__}: {ex}")

    @app.get("/api/shadow", dependencies=[Depends(auth)])
    def shadow():
        return {"summary": engine.shadow.summary(), "recent": store.shadows(limit=50)}

    # ── trade journal ──
    @app.get("/api/journal", dependencies=[Depends(auth)])
    def journal(limit: int = 50):
        out, buys = [], {}
        for t in store.all_trades():
            if t["side"] == "BUY":
                buys[t["key"]] = t
            else:
                b = buys.get(t["key"], {})
                out.append({"id": t["id"], "symbol": t["symbol"], "market": t["market"], "qty": t["qty"],
                            "entry": b.get("price"), "exit": t["price"], "opened": b.get("ts"), "closed": t["ts"],
                            "pnl_krw": t["pnl_krw"], "entry_reason": b.get("reason"), "exit_reason": t["reason"],
                            "ai": b.get("ai_reason"), "slippage_pct": t.get("slippage_pct")})
        return out[::-1][:limit]

    @app.get("/api/journal/{trade_id}/chart", dependencies=[Depends(auth)])
    def journal_chart(trade_id: int):
        sell = store.trade(trade_id)
        if not sell or sell["side"] != "SELL":
            raise HTTPException(404, "no such closed trade")
        buy = next((t for t in reversed(store.all_trades()) if t["key"] == sell["key"] and t["side"] == "BUY"
                    and t["id"] < sell["id"]), None)
        s = engine.cfg.symbol(sell["key"])
        if not s:
            raise HTTPException(404, "symbol no longer in the watchlist")
        try:
            df, _ = engine.data.candles(s, engine.cfg.history_bars)
        except Exception as ex:
            raise HTTPException(502, f"couldn't load candles: {ex}")
        times = [_candle_utc(s.market, str(ts), bool(engine.data.demo)) for ts in df.index]

        def locate(iso):
            if not iso:
                return None
            t = datetime.fromisoformat(iso)
            idx = [i for i, ct in enumerate(times) if ct and ct <= t]
            return idx[-1] if idx else None

        return {"symbol": s.symbol, "ts": [str(x) for x in df.index], "close": [float(x) for x in df["close"]],
                "entry_i": locate(buy and buy["ts"]), "exit_i": locate(sell["ts"]),
                "entry": buy and buy["price"], "exit": sell["price"]}

    # ── settings from the phone ──
    def settings_payload():
        c = engine.cfg
        return {"risk": {k: getattr(c.risk, k) for k in EDITABLE_RISK},
                "ranges": {k: list(v) for k, v in EDITABLE_RISK.items()},
                "regime_enabled": c.regime.enabled, "ai_enabled": c.ai.enabled,
                "signals_enabled": c.signals.enabled, "ranking_enabled": c.ranking.enabled,
                "symbols": symbols_to_dicts(c.symbols),
                "strategies": {k: v.DEFAULTS for k, v in STRATEGIES.items()},
                "held": [p.key for p in store.positions()]}

    @app.get("/api/settings", dependencies=[Depends(auth)])
    def get_settings():
        return settings_payload()

    @app.post("/api/settings", dependencies=[Depends(auth)])
    async def post_settings(request: Request):
        body = await request.json()
        ov = dict(store.get("overrides") or {})
        if "risk" in body:
            bad = set(body["risk"]) - set(EDITABLE_RISK)
            if bad:
                raise HTTPException(400, f"not editable: {sorted(bad)}")
            ov["risk"] = {**ov.get("risk", {}), **body["risk"]}
        for k in ("regime_enabled", "ai_enabled", "signals_enabled", "ranking_enabled"):
            if k in body:
                ov[k] = bool(body[k])
        if "symbols" in body:
            ov["symbols"] = body["symbols"]
        try:
            engine.apply_settings(ov)
        except (ValueError, TypeError) as ex:
            raise HTTPException(400, str(ex))
        engine.notify("⚙️ TradeBot settings changed from the dashboard", key="settings")
        return settings_payload()

    @app.post("/api/settings/reset", dependencies=[Depends(auth)])
    def reset_settings():
        engine.apply_settings({})
        return settings_payload()

    # ── controls ──
    @app.post("/api/pause", dependencies=[Depends(auth)])
    def pause():
        engine.set_paused(True)
        return {"paused": True}

    @app.post("/api/resume", dependencies=[Depends(auth)])
    def resume():
        store.set("killed", False)
        engine.set_paused(False)
        return {"paused": False}

    @app.post("/api/tick", dependencies=[Depends(auth)])
    def tick():
        runner.poke() if runner.is_alive() else engine.tick()
        return {"ok": True}

    @app.post("/api/kill", dependencies=[Depends(auth)])
    def kill(body: KillBody):
        if body.confirm != "SELL ALL":
            raise HTTPException(400, 'type "SELL ALL" to confirm')
        return {"results": engine.kill()}

    @app.post("/api/retest", dependencies=[Depends(auth)])
    def retest():
        return {"started": engine.monitor.start_retest()}

    @app.post("/api/reconcile", dependencies=[Depends(auth)])
    def reconcile():
        if not engine.cfg.is_live:
            return {"problems": [], "note": "position checks only run in live mode"}
        return {"problems": engine.reconcile()}

    @app.post("/api/alerts/test", dependencies=[Depends(auth)])
    def alert_test():
        if not engine.notifier or not engine.notifier.channels:
            raise HTTPException(400, "no alert channel set up (see README → Alerts)")
        engine.notify("✅ TradeBot test alert")
        return {"sent_via": engine.notifier.channels}

    @app.get("/api/tax/review", dependencies=[Depends(auth)])
    def tax_review():
        from .tax import harvest_review
        return harvest_review(engine)

    @app.get("/api/review", dependencies=[Depends(auth)])
    def weekly_review():
        return store.get("weekly_review") or {}

    @app.post("/api/review", dependencies=[Depends(auth)])
    def weekly_review_now():
        if engine.ai.client is None:
            raise HTTPException(400, "ANTHROPIC_API_KEY is not set")
        text = engine.monitor.run_review()
        if not text:
            raise HTTPException(502, "the review failed — see the activity log")
        return store.get("weekly_review")

    @app.post("/api/backup", dependencies=[Depends(auth)])
    def backup_now():
        if store.path == ":memory:":
            raise HTTPException(400, "no database file to back up")
        return engine.monitor.run_backup()

    @app.get("/api/montecarlo", dependencies=[Depends(auth)])
    def montecarlo():
        from .backtest import monte_carlo
        from .longterm import is_longterm
        start = store.get("start_equity") or engine.equity_krw()
        pcts = [t["pnl_krw"] / start * 100 for t in store.all_trades()
                if t["side"] == "SELL" and t["pnl_krw"] is not None and not is_longterm(t["key"])]
        try:
            return monte_carlo(pcts, n_trades=max(len(pcts), 50))
        except ValueError as ex:
            return {"error": str(ex), "trades": len(pcts)}

    @app.get("/api/export/tax.csv", dependencies=[Depends(auth)])
    def tax_csv(year: int | None = None):
        rows = closed_trades(store.all_trades(), year)
        name = f"tradebot-trades-{year or 'all'}.csv"
        return Response(to_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


def create_app() -> FastAPI:  # uvicorn --factory entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return build_app()
