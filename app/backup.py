"""Nightly database backups.

Every night at 03:00 KST the bot copies its database (trades, positions, settings, history) to
data/backups/, keeps the last 14, and — if configured — sends the copy off the server:
  • Telegram: the file is sent to your alert chat (encrypted if TRADEBOT_PASSPHRASE is set)
  • BACKUP_COMMAND: any command you like, run with the file path as its last argument
    (e.g. BACKUP_COMMAND="rclone copy" + a remote → Google Drive, Dropbox, S3…)

API keys are never in the database. Restore:
  python -m app.backup restore data/backups/tradebot-20261114.db.enc   (stop the bot first)
"""
from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KEEP = 14


def make_backup(store, out_dir: str | Path, passphrase: str = "") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M")
    tmp = out_dir / f".tmp-{stamp}.db"
    dst = sqlite3.connect(tmp)
    with store.lock:
        store.conn.backup(dst)  # consistent snapshot even while the bot is running
    dst.close()
    data = tmp.read_bytes()
    tmp.unlink()
    if passphrase:
        from .vault import encrypt_raw
        path = out_dir / f"tradebot-{stamp}.db.enc"
        path.write_bytes(encrypt_raw(data, passphrase))
    else:
        path = out_dir / f"tradebot-{stamp}.db"
        path.write_bytes(data)
    os.chmod(path, 0o600)
    for old in sorted(out_dir.glob("tradebot-*.db*"))[:-KEEP]:
        old.unlink()
    return path


def ship(path: Path, notifier=None) -> list[str]:
    """Send the backup off the server. Returns what was done."""
    done = []
    cmd = os.environ.get("BACKUP_COMMAND", "").strip()
    if cmd:
        r = subprocess.run(shlex.split(cmd) + [str(path)], capture_output=True, text=True, timeout=600)
        done.append("BACKUP_COMMAND ok" if r.returncode == 0 else f"BACKUP_COMMAND failed: {r.stderr[:200]}")
    if notifier is not None and hasattr(notifier, "send_file") and path.stat().st_size < 45_000_000:
        try:
            if notifier.send_file(path, f"TradeBot backup {path.name}"):
                done.append("sent to Telegram")
        except Exception as e:
            done.append(f"Telegram upload failed: {e}")
    return done


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "restore":
        src = Path(sys.argv[2])
        data = src.read_bytes()
        if src.suffix == ".enc":
            from .vault import decrypt_raw
            import getpass
            data = decrypt_raw(data, os.environ.get("TRADEBOT_PASSPHRASE") or getpass.getpass("Passphrase: "))
        dest = Path(os.environ.get("TRADEBOT_DB", Path(__file__).resolve().parents[1] / "data" / "tradebot.db"))
        if dest.exists():
            dest.rename(dest.with_suffix(f".before-restore-{datetime.now():%Y%m%d%H%M}"))
        dest.write_bytes(data)
        print(f"Restored {src.name} → {dest}. Start the bot again.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
