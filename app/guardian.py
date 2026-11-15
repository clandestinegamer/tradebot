"""Guardian: protects your stops if the main server dies.

Run it on a DIFFERENT machine (an old PC, a second cheap VPS, a Raspberry Pi):

    pip install requests PyJWT python-dotenv
    python -m app.guardian --url https://bot.yourdomain.com

It needs in its own .env:
    GUARDIAN_TOKEN=...            same value as on the bot server
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (or Kakao keys)   for alerts
    Optional, to SELL on its own:  UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY  — create a SEPARATE Upbit key
        whitelisted to this machine's IP (Upbit keys are IP-locked), trading permission only, never withdrawals.
    Optional: KIS_* keys (same account) so it can watch and sell stock positions too.

Every 30 s it copies the bot's open positions and stops. If the bot can't be reached for
--takeover-minutes (default 5), it starts watching prices itself: when a stop is hit it sells (if it has
keys) or sends you an urgent "sell now" alert (if it doesn't). When the bot comes back, it tells you what it did.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("guardian")


class Guardian:
    def __init__(self, url: str, token: str, notifier, upbit=None, kis=None, takeover_minutes: float = 5,
                 state_file: str = "guardian_state.json", session=None):
        self.url, self.token = url.rstrip("/"), token
        self.notify = notifier
        self.upbit, self.kis = upbit, kis
        self.takeover_s = takeover_minutes * 60
        self.state_path = Path(state_file)
        self.http = session or requests.Session()
        self.state = {"positions": [], "last_ok": None, "down_since": None, "taken_over": False,
                      "sold": [], "warned": []}
        if self.state_path.exists():
            try:
                self.state.update(json.loads(self.state_path.read_text()))
            except ValueError:
                pass

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1))

    def fetch(self) -> dict | None:
        try:
            r = self.http.get(f"{self.url}/api/guardian", headers={"X-Guardian-Token": self.token}, timeout=10)
            if r.status_code == 200:
                return r.json()
            log.warning("bot answered %s", r.status_code)
        except Exception as e:
            log.warning("bot unreachable: %s", e)
        return None

    def price(self, p: dict) -> float | None:
        try:
            if p["market"] == "upbit":
                from .brokers.upbit import UpbitClient
                return (self.upbit or UpbitClient()).price(p["symbol"])
            if self.kis is None:
                return None
            if p["market"] == "kis_kr":
                return self.kis.kr_price(p["symbol"])
            return self.kis.us_price(p["symbol"], p.get("exchange", "NASD"))
        except Exception as e:
            log.warning("price %s failed: %s", p["symbol"], e)
            return None

    def sell(self, p: dict, price: float) -> str:
        try:
            if p["market"] == "upbit" and self.upbit and self.upbit.access_key:
                o = self.upbit.sell_market(p["symbol"], p["qty"])
                return f"sold (Upbit order {o.get('uuid', '?')})"
            if p["market"] == "kis_kr" and self.kis:
                self.kis.kr_order("sell", p["symbol"], int(p["qty"]), "market")
                return "sell order sent to KIS"
            if p["market"] == "kis_us" and self.kis:
                self.kis.us_order("sell", p["symbol"], p.get("exchange", "NASD"), int(p["qty"]), round(price * 0.99, 2))
                return "sell order sent to KIS (limit 1% below)"
        except Exception as e:
            return f"SELL FAILED: {e}"
        return "no keys on the guardian"

    def step(self, now: float | None = None) -> None:
        now = now or time.time()
        snap = self.fetch()
        st = self.state
        if snap is not None:
            if st["taken_over"]:
                done = "; ".join(st["sold"]) or "no stops were hit"
                self.notify(f"✅ TradeBot is back online. While it was down the guardian: {done}. "
                            "Open the dashboard and run 'Check positions vs broker'.")
            st.update(positions=snap.get("positions", []), last_ok=now, down_since=None, taken_over=False,
                      sold=[], warned=[])
            self._save()
            return
        st["down_since"] = st["down_since"] or now
        if now - st["down_since"] < self.takeover_s:
            self._save()
            return
        if not st["taken_over"]:
            st["taken_over"] = True
            names = ", ".join(f"{p['symbol']} (stop {p['stop']:,.4g})" for p in st["positions"]) or "no positions"
            self.notify(f"🚨 TradeBot unreachable for {int((now - st['down_since']) / 60)} min. "
                        f"Guardian is now watching: {names}.")
        for p in st["positions"]:
            if p["key"] in [s.split("|")[0] for s in st["sold"]]:
                continue
            px = self.price(p)
            if px is None:
                if p["key"] not in st["warned"]:
                    st["warned"].append(p["key"])
                    self.notify(f"⚠️ Guardian can't watch {p['symbol']} (no price access). Watch it yourself — stop {p['stop']:,.4g}.")
                continue
            if px <= p["stop"]:
                result = self.sell(p, px)
                st["sold"].append(f"{p['key']}| {p['symbol']} at {px:,.4g}: {result}")
                self.notify(f"🛑 STOP HIT on {p['symbol']} ({px:,.4g} ≤ {p['stop']:,.4g}) while the bot is down. "
                            f"Guardian: {result}." + (" SELL IT MANUALLY NOW." if "no keys" in result or "FAILED" in result else ""))
        self._save()

    def run(self, every: float = 30) -> None:
        log.info("guardian watching %s", self.url)
        while True:
            try:
                self.step()
            except Exception as e:
                log.error("guardian step failed: %s", e)
            time.sleep(every)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the bot's dashboard address, e.g. https://bot.example.com")
    ap.add_argument("--takeover-minutes", type=float, default=5)
    a = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    import os
    from .alerts import Notifier
    from .brokers.kis import KISClient
    from .brokers.upbit import UpbitClient
    from .config import load_secrets
    s = load_secrets()
    token = os.environ.get("GUARDIAN_TOKEN", "")
    if not token:
        raise SystemExit("set GUARDIAN_TOKEN (the same value as on the bot server)")
    n = Notifier(s)
    if not n.channels:
        raise SystemExit("set up Telegram or KakaoTalk alerts first — the guardian's job is to warn you")
    upbit = UpbitClient(s.upbit_access, s.upbit_secret) if s.upbit_access else None
    kis = KISClient(s.kis_app_key, s.kis_app_secret, s.kis_account_no, s.kis_account_product, s.kis_env) \
        if s.kis_app_key else None
    n.send(f"🛡 Guardian started — watching {a.url}" + ("" if (upbit or kis) else " (alert-only: no broker keys)"))
    Guardian(a.url, token, n.send, upbit, kis, a.takeover_minutes).run()


if __name__ == "__main__":
    main()
