"""Phone alerts via Telegram and/or KakaoTalk (나에게 보내기), plus a heartbeat ping for uptime monitors.

Setup helpers:
  python -m app.alerts telegram-chat-id   # after you message your bot once, prints your chat id
  python -m app.alerts kakao-setup        # one-time Kakao login → prints KAKAO_REFRESH_TOKEN
  python -m app.alerts test               # sends a test message to every configured channel
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time

import requests

log = logging.getLogger("tradebot")


class Notifier:
    def __init__(self, secrets, store=None, cooldown_minutes: int = 30, session=None):
        self.s = secrets
        self.store = store
        self.http = session or requests.Session()
        self.cooldown = cooldown_minutes * 60
        self._last: dict[str, float] = {}
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._kakao_access: tuple[str, float] | None = None
        self.sent: list[str] = []  # last messages, for the dashboard/tests
        self._worker = threading.Thread(target=self._run, daemon=True, name="notifier")
        self._worker.start()

    @property
    def channels(self) -> list[str]:
        out = []
        if self.s.telegram_token and self.s.telegram_chat_id:
            out.append("telegram")
        if self.s.kakao_rest_key and self._kakao_refresh():
            out.append("kakao")
        return out

    def send(self, text: str, key: str | None = None) -> None:
        """Queue a message. With `key`, the same kind of message is sent at most once per cooldown."""
        if key:
            now = time.time()
            if now - self._last.get(key, 0) < self.cooldown:
                return
            self._last[key] = now
        self.sent = (self.sent + [text])[-50:]
        try:
            self._q.put_nowait(text)
        except queue.Full:
            pass

    def flush(self, timeout: float = 5) -> None:
        end = time.time() + timeout
        while not self._q.empty() and time.time() < end:
            time.sleep(0.05)

    def _run(self) -> None:
        while True:
            text = self._q.get()
            for ch in self.channels:
                try:
                    (self._telegram if ch == "telegram" else self._kakao)(text)
                except Exception as e:  # never let alerts break trading
                    log.warning("alert via %s failed: %s", ch, e)

    # ── Telegram ──
    def _telegram(self, text: str) -> None:
        r = self.http.post(f"https://api.telegram.org/bot{self.s.telegram_token}/sendMessage",
                           json={"chat_id": self.s.telegram_chat_id, "text": text[:4000],
                                 "disable_web_page_preview": True}, timeout=10)
        r.raise_for_status()

    # ── KakaoTalk (send to myself) ──
    def _kakao_refresh(self) -> str:
        stored = self.store.get("kakao_refresh_token") if self.store else None
        return stored or self.s.kakao_refresh_token

    def _kakao_token(self) -> str:
        if self._kakao_access and time.time() < self._kakao_access[1]:
            return self._kakao_access[0]
        r = self.http.post("https://kauth.kakao.com/oauth/token", timeout=10, data={
            "grant_type": "refresh_token", "client_id": self.s.kakao_rest_key,
            "refresh_token": self._kakao_refresh()})
        d = r.json()
        if "access_token" not in d:
            raise RuntimeError(f"Kakao token refresh failed: {d}")
        if d.get("refresh_token") and self.store:  # Kakao rotates refresh tokens — keep the new one
            self.store.set("kakao_refresh_token", d["refresh_token"])
        self._kakao_access = (d["access_token"], time.time() + int(d.get("expires_in", 3600)) - 120)
        return d["access_token"]

    def _kakao(self, text: str) -> None:
        url = os.environ.get("DASHBOARD_URL", "https://example.com")
        tmpl = {"object_type": "text", "text": text[:990], "link": {"web_url": url, "mobile_web_url": url}}
        r = self.http.post("https://kapi.kakao.com/v2/api/talk/memo/default/send", timeout=10,
                           headers={"Authorization": f"Bearer {self._kakao_token()}"},
                           data={"template_object": json.dumps(tmpl, ensure_ascii=False)})
        r.raise_for_status()

    def send_file(self, path, caption: str = "") -> bool:
        """Send a file to Telegram (used for nightly backups). Returns True if sent."""
        if not (self.s.telegram_token and self.s.telegram_chat_id):
            return False
        with open(path, "rb") as f:
            r = self.http.post(f"https://api.telegram.org/bot{self.s.telegram_token}/sendDocument",
                               data={"chat_id": self.s.telegram_chat_id, "caption": caption[:1000]},
                               files={"document": (os.path.basename(str(path)), f)}, timeout=60)
        r.raise_for_status()
        return True

    # ── heartbeat (healthchecks.io, Uptime Kuma push, etc.) ──
    def heartbeat(self, ok: bool = True) -> None:
        if not self.s.healthcheck_url:
            return
        url = self.s.healthcheck_url + ("" if ok else "/fail")
        threading.Thread(target=self._ping, args=(url,), daemon=True).start()

    def _ping(self, url: str) -> None:
        try:
            self.http.get(url, timeout=10)
        except Exception as e:
            log.warning("heartbeat failed: %s", e)


def main() -> None:
    from .config import load_config
    cfg = load_config()
    s = cfg.secrets
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "telegram-chat-id":
        d = requests.get(f"https://api.telegram.org/bot{s.telegram_token}/getUpdates", timeout=10).json()
        ids = {u["message"]["chat"]["id"] for u in d.get("result", []) if "message" in u}
        print("Chat ids:", ids or "none yet — send your bot any message first, then run this again")
    elif cmd == "kakao-setup":
        redirect = "https://localhost"
        print("1) In developers.kakao.com: enable Kakao Login, add Redirect URI https://localhost,")
        print("   and turn on the consent item '카카오톡 메시지 전송' (talk_message).")
        print("2) Open this link on your phone/PC, log in and agree:\n")
        print(f"   https://kauth.kakao.com/oauth/authorize?client_id={s.kakao_rest_key}"
              f"&redirect_uri={redirect}&response_type=code&scope=talk_message\n")
        code = input("3) The browser lands on a page that won't load. Paste the 'code=' value from its address: ").strip()
        d = requests.post("https://kauth.kakao.com/oauth/token", timeout=10, data={
            "grant_type": "authorization_code", "client_id": s.kakao_rest_key,
            "redirect_uri": redirect, "code": code}).json()
        if "refresh_token" in d:
            print(f"\nAdd to .env:\nKAKAO_REFRESH_TOKEN={d['refresh_token']}")
        else:
            print("Failed:", d)
    elif cmd == "test":
        n = Notifier(s)
        if not n.channels:
            sys.exit("No alert channel configured (see .env.example).")
        n.send("✅ TradeBot test message")
        n.flush(10)
        time.sleep(2)
        print("Sent via:", ", ".join(n.channels))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
