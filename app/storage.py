"""SQLite storage for positions, trades, signals, equity and bot state."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS positions (
  key TEXT PRIMARY KEY, symbol TEXT, market TEXT, qty REAL, avg_price REAL,
  stop REAL, target REAL, opened_at TEXT, entry_bar TEXT, entry_reason TEXT,
  initial_stop REAL DEFAULT 0, highest REAL DEFAULT 0, entry_atr REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, key TEXT, symbol TEXT, market TEXT,
  side TEXT, qty REAL, price REAL, value_krw REAL, fee_krw REAL, pnl_krw REAL,
  reason TEXT, ai_decision TEXT, ai_reason TEXT, mode TEXT, order_id TEXT,
  expected_price REAL, slippage_pct REAL, fx_usdkrw REAL, cost_basis_krw REAL, opened_at TEXT);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, key TEXT, symbol TEXT, market TEXT, source TEXT,
  reason TEXT, outcome TEXT, bar_id TEXT, entry REAL, stop REAL, initial_stop REAL, highest REAL,
  entry_atr REAL, status TEXT DEFAULT 'open', exit REAL, exit_ts TEXT, r REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, device TEXT, created TEXT, last_seen TEXT, expires TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, key TEXT, action TEXT, reason TEXT,
  outcome TEXT, snapshot TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity_krw REAL, cash_krw REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        for col in ("initial_stop", "highest", "entry_atr"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col} REAL DEFAULT 0")
        tcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        for col, typ in (("expected_price", "REAL"), ("slippage_pct", "REAL"), ("fx_usdkrw", "REAL"),
                         ("cost_basis_krw", "REAL"), ("opened_at", "TEXT")):
            if col not in tcols:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")

    # ── state ──
    def get(self, k: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, k: str, v) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, json.dumps(v)))
            self.conn.commit()

    # ── positions ──
    def positions(self) -> list[Position]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
        return [Position(**dict(r)) for r in rows]

    def position(self, key: str) -> Position | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM positions WHERE key=?", (key,)).fetchone()
        return Position(**dict(r)) if r else None

    def save_position(self, p: Position) -> None:
        d = p.to_dict()
        with self.lock:
            self.conn.execute(
                f"INSERT OR REPLACE INTO positions ({','.join(d)}) VALUES ({','.join('?' * len(d))})",
                list(d.values()),
            )
            self.conn.commit()

    def delete_position(self, key: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM positions WHERE key=?", (key,))
            self.conn.commit()

    # ── logs ──
    def add_trade(self, **t) -> None:
        t.setdefault("ts", now_iso())
        with self.lock:
            self.conn.execute(
                f"INSERT INTO trades ({','.join(t)}) VALUES ({','.join('?' * len(t))})", list(t.values())
            )
            self.conn.commit()

    def trades(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def trades_since(self, ts_iso: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM trades WHERE ts >= ?", (ts_iso,)).fetchall()
        return [dict(r) for r in rows]

    def add_signal(self, key: str, action: str, reason: str, outcome: str, snapshot: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO signals (ts,key,action,reason,outcome,snapshot) VALUES (?,?,?,?,?,?)",
                (now_iso(), key, action, reason, outcome, json.dumps(snapshot)),
            )
            self.conn.commit()

    def signals(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = json.loads(d["snapshot"] or "{}")
            out.append(d)
        return out

    def add_equity(self, equity: float, cash: float) -> None:
        ts = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (ts, equity, cash))
            self.conn.commit()

    def equity_curve(self, since_iso: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM equity WHERE ts >= ? ORDER BY ts", (since_iso,)
            ).fetchall()
        return [dict(r) for r in rows]

    def event(self, msg: str, level: str = "info") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO events (ts,level,msg) VALUES (?,?,?)", (now_iso(), level, msg))
            self.conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 500 FROM events)")
            self.conn.commit()

    PAPER_STATE_PREFIXES = ("paper_cash", "start_equity", "day_open:", "last_bar:", "dca_last:", "grid_state:",
                            "reconcile_result", "drift", "core_", "retest")

    def reset_paper(self, starting_cash: float) -> None:
        """Start the paper account over: no positions, no trade history, fresh cash.
        Keeps sign-ins, dashboard settings and alert setup."""
        with self.lock:
            for t in ("positions", "trades", "shadow", "signals", "equity"):
                self.conn.execute(f"DELETE FROM {t}")
            for k in [r["k"] for r in self.conn.execute("SELECT k FROM state")]:
                if k.startswith(self.PAPER_STATE_PREFIXES):
                    self.conn.execute("DELETE FROM state WHERE k = ?", (k,))
            self.conn.commit()
        self.set("paper_cash", starting_cash)
        self.set("start_equity", starting_cash)
        self.event("paper account started over")

    def events(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── shadow (hypothetical) trades ──
    def add_shadow(self, **d) -> None:
        d.setdefault("created", now_iso())
        with self.lock:
            self.conn.execute(
                f"INSERT INTO shadow ({','.join(d)}) VALUES ({','.join('?' * len(d))})", list(d.values()))
            self.conn.commit()

    def shadows(self, status: str | None = None, limit: int = 500) -> list[dict]:
        q, args = "SELECT * FROM shadow", []
        if status:
            q, args = q + " WHERE status=?", [status]
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def update_shadow(self, sid: int, **d) -> None:
        with self.lock:
            self.conn.execute(f"UPDATE shadow SET {','.join(f'{k}=?' for k in d)} WHERE id=?", [*d.values(), sid])
            self.conn.commit()

    def has_open_shadow(self, key: str, bar_id: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM shadow WHERE key=? AND bar_id=? LIMIT 1",
                                     (key, bar_id)).fetchone() is not None

    # ── device sessions ──
    def add_session(self, sid_hash: str, device: str, expires: str, ip: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                              (sid_hash, device, now_iso(), now_iso(), expires, ip))
            self.conn.commit()

    def session(self, sid_hash: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid_hash,)).fetchone()
        return dict(r) if r else None

    def touch_session(self, sid_hash: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now_iso(), sid_hash))
            self.conn.commit()

    def sessions(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM sessions ORDER BY last_seen DESC")]

    def delete_session(self, sid_hash: str | None = None) -> None:
        with self.lock:
            if sid_hash:
                self.conn.execute("DELETE FROM sessions WHERE id=?", (sid_hash,))
            else:
                self.conn.execute("DELETE FROM sessions")
            self.conn.commit()

    def all_trades(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM trades ORDER BY id")]

    def trade(self, tid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None
