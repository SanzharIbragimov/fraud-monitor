"""SQLite-хранилище: инциденты, журнал аудита, стоп-листы, подписки на вебхуки.

В продакшене — PostgreSQL. Схема та же.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, created TEXT NOT NULL, jurisdiction TEXT, user_id TEXT,
  event_type TEXT, amount REAL, currency TEXT, recipient_id TEXT,
  action TEXT, score INTEGER, scenarios TEXT, reasons TEXT, features TEXT,
  status TEXT NOT NULL DEFAULT 'open',   -- open | fraud | legit
  analyst TEXT, comment TEXT, resolved TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
  action TEXT NOT NULL, object TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS list_entries (
  list_name TEXT NOT NULL, jurisdiction TEXT NOT NULL, entity_type TEXT NOT NULL,
  value TEXT NOT NULL, reason TEXT, source TEXT, added TEXT,
  PRIMARY KEY (list_name, jurisdiction, entity_type, value)
);
CREATE TABLE IF NOT EXISTS webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, events TEXT NOT NULL, created TEXT
);
CREATE TABLE IF NOT EXISTS labels (
  event_id TEXT PRIMARY KEY, label INTEGER NOT NULL, features TEXT NOT NULL, ts TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def xmany(self, sql: str, rows: list[tuple]) -> None:
        with self.lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    # аудит
    def audit(self, actor: str, role: str, action: str, obj: str = "", details: dict | str | None = None) -> None:
        d = details if isinstance(details, str) or details is None else json.dumps(details, ensure_ascii=False)
        self.x("INSERT INTO audit(ts, actor, role, action, object, details) VALUES (?,?,?,?,?,?)",
               (utcnow(), actor, role, action, obj, d))
