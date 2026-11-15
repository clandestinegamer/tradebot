"""Encrypted secrets file, so API keys don't sit in plain text on the server.

  python -m app.vault encrypt .env          # writes secrets.enc (asks for a passphrase)
  python -m app.vault check                 # verifies the passphrase and lists which keys are set

Then delete .env from the server and start the bot with TRADEBOT_PASSPHRASE set (or enter it when
the container starts). The file is protected with scrypt + AES (Fernet). Someone who copies the server's
disk gets only ciphertext; the passphrase lives only in your head / password manager and in the bot's memory.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"TBV1"


def _key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt_values(values: dict, passphrase: str) -> bytes:
    if len(passphrase) < 12:
        raise ValueError("use a passphrase of at least 12 characters")
    salt = os.urandom(16)
    token = Fernet(_key(passphrase, salt)).encrypt(json.dumps(values).encode())
    return MAGIC + salt + token


def decrypt_bytes(blob: bytes, passphrase: str) -> dict:
    if not blob.startswith(MAGIC):
        raise ValueError("not a TradeBot secrets file")
    salt, token = blob[4:20], blob[20:]
    try:
        return json.loads(Fernet(_key(passphrase, salt)).decrypt(token))
    except InvalidToken:
        raise ValueError("wrong passphrase for secrets.enc") from None


def encrypt_raw(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    return MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(data)


def decrypt_raw(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("not an encrypted TradeBot file")
    try:
        return Fernet(_key(passphrase, blob[4:20])).decrypt(blob[20:])
    except InvalidToken:
        raise ValueError("wrong passphrase") from None


def decrypt_file(path: Path, passphrase: str) -> dict:
    if not passphrase:
        if sys.stdin.isatty():
            passphrase = getpass.getpass("Passphrase for secrets.enc: ")
        else:
            raise ValueError("secrets.enc found but TRADEBOT_PASSPHRASE is not set")
    return decrypt_bytes(Path(path).read_bytes(), passphrase)


def parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")
        if v:
            out[k.strip()] = v
    return out


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "encrypt":
        src = Path(sys.argv[2] if len(sys.argv) > 2 else root / ".env")
        values = parse_env(src.read_text())
        p1 = getpass.getpass("New passphrase (12+ chars): ")
        if p1 != getpass.getpass("Repeat passphrase: "):
            sys.exit("passphrases don't match")
        out = root / "secrets.enc"
        out.write_bytes(encrypt_values(values, p1))
        os.chmod(out, 0o600)
        print(f"Wrote {out} with {len(values)} keys. Now delete {src} from this machine.")
    elif cmd == "check":
        vals = decrypt_file(root / "secrets.enc", os.environ.get("TRADEBOT_PASSPHRASE", ""))
        print("OK. Keys set:", ", ".join(sorted(vals)))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
