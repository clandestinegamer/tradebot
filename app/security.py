"""Dashboard login: password (DASHBOARD_TOKEN) + 6-digit authenticator code (TOTP), then a per-device session.

  python -m app.security totp-setup     # creates a TOTP secret → add it to Google Authenticator / Authy

• Each phone/browser that logs in gets its own session (30 days). You can see and revoke them in the dashboard.
• Repeated wrong logins from one address are locked out for 15 minutes, and you get an alert.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

SESSION_DAYS = 30


# ── TOTP (RFC 6238, the standard used by Google Authenticator, Authy, 1Password…) ──
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_now(secret_b32: str, t: float | None = None) -> str:
    return _hotp(secret_b32, int((t or time.time()) // 30))


class TOTPVerifier:
    """Accepts the current code ±30s, and never the same code twice (replay protection)."""

    def __init__(self, secret_b32: str):
        self.secret = secret_b32
        self._last_counter = -1
        self._lock = threading.Lock()

    def verify(self, code: str, t: float | None = None) -> bool:
        code = (code or "").strip().replace(" ", "")
        if not (len(code) == 6 and code.isdigit()):
            return False
        now = int((t or time.time()) // 30)
        with self._lock:
            for c in (now - 1, now, now + 1):
                if c > self._last_counter and hmac.compare_digest(_hotp(self.secret, c), code):
                    self._last_counter = c
                    return True
        return False


# ── sessions ──
def hash_session(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def new_session(store, device: str, ip: str) -> str:
    sid = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    store.add_session(hash_session(sid), (device or "unknown device")[:80], exp, ip)
    return sid


def check_session(store, sid: str) -> dict | None:
    if not sid:
        return None
    h = hash_session(sid)
    s = store.session(h)
    if not s:
        return None
    if datetime.fromisoformat(s["expires"]) < datetime.now(timezone.utc):
        store.delete_session(h)
        return None
    if (datetime.now(timezone.utc) - datetime.fromisoformat(s["last_seen"])).total_seconds() > 300:
        store.touch_session(h)
    return s


class LoginLimiter:
    """5 failures per address per 15 minutes → locked out until the window passes."""

    def __init__(self, max_failures: int = 5, window_s: int = 900):
        self.max, self.window = max_failures, window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            now = time.time()
            self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < self.window]
            return len(self._fails[ip]) >= self.max

    def fail(self, ip: str) -> int:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())
            return len(self._fails[ip])

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "totp-setup":
        sec = new_totp_secret()
        uri = f"otpauth://totp/TradeBot?secret={sec}&issuer=TradeBot&digits=6&period=30"
        print("1) In Google Authenticator / Authy: Add account → Enter a setup key")
        print(f"   Account: TradeBot   Key: {sec}   Type: time-based\n")
        print(f"   (or turn this into a QR code with any QR tool: {uri})\n")
        print(f"2) Add to .env:  TOTP_SECRET={sec}")
        print(f"3) Check it works — your app should show: {totp_now(sec)}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
