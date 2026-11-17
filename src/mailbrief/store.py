"""SQLite state: cached LLM results, extraction failures, action item status, notifications, stories."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import MailInsight

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insights (
  email_id TEXT, model TEXT, data TEXT, created TEXT, PRIMARY KEY (email_id, model));
CREATE TABLE IF NOT EXISTS failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email_id TEXT, subject TEXT, code TEXT, message TEXT,
  body_chars INTEGER, raw TEXT, warnings TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS action_status (
  key TEXT PRIMARY KEY, status TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS notified (
  email_id TEXT PRIMARY KEY, created TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, mails INTEGER, llm_calls INTEGER, errors INTEGER);
-- Stories (see stories.py). Times are UTC ISO 8601, so text order is time order.
CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, goal TEXT, owner TEXT, end_condition TEXT,
  status TEXT, flags TEXT, merged_into INTEGER, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS story_links (
  key TEXT PRIMARY KEY, story_id INTEGER, how TEXT, last_seen TEXT, people TEXT);
CREATE TABLE IF NOT EXISTS story_blocks (
  key TEXT, story_id INTEGER, created TEXT, PRIMARY KEY (key, story_id));
CREATE TABLE IF NOT EXISTS story_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER, thread_key TEXT, revision TEXT, email_id TEXT,
  subject TEXT, summary TEXT, source_time TEXT, captured_at TEXT, content_hash TEXT,
  UNIQUE (story_id, content_hash));
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_key TEXT UNIQUE, keys TEXT, revision TEXT, email_id TEXT,
  subject TEXT, summary TEXT, category TEXT, reasons TEXT, proposal TEXT, status TEXT, story_id INTEGER,
  source_time TEXT, created TEXT, updated TEXT, people TEXT);
"""
_ADDED_COLUMNS = [("story_links", "people", "TEXT"), ("reviews", "people", "TEXT")]


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(_SCHEMA)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(insights)")]
        if "model" not in cols:  # very old schema
            self.db.executescript("DROP TABLE insights;" + _SCHEMA)
        for table, col, typ in _ADDED_COLUMNS:  # databases made by an earlier version
            if col not in [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    def _exec(self, sql: str, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def query(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            cur = self.db.execute(sql, args)
            names = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def get_insight(self, key: str, model: str) -> MailInsight | None:
        """key: "thr:<thread revision>" (see Extractor.store_key)."""
        with self.lock:
            row = self.db.execute("SELECT data FROM insights WHERE email_id=? AND model=?", (key, model)).fetchone()
        return MailInsight.from_dict(json.loads(row[0])) if row else None

    def put_insight(self, ins: MailInsight, model: str, key: str | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO insights VALUES (?,?,?,?)",
                   (key or ins.email_id, model, json.dumps(ins.to_dict(), ensure_ascii=False),
                    datetime.now().isoformat()))

    def add_failure(self, f) -> None:
        self._exec("INSERT INTO failures(email_id, subject, code, message, body_chars, raw, warnings, created) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (f.email_id, f.subject, f.code, f.message, f.body_chars, f.raw,
                    json.dumps(f.warnings, ensure_ascii=False), datetime.now().isoformat()))

    def failures(self, limit: int = 200) -> list[dict]:
        with self.lock:
            cur = self.db.execute("SELECT email_id, subject, code, message, body_chars, raw, warnings, created "
                                  "FROM failures ORDER BY id DESC LIMIT ?", (limit,))
            names = [c[0] for c in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def set_action_status(self, key: str, status: str) -> None:
        self._exec("INSERT OR REPLACE INTO action_status VALUES (?,?,?)", (key, status, datetime.now().isoformat()))

    def done_keys(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT key FROM action_status WHERE status='done'")}

    def mark_notified(self, email_id: str) -> bool:
        """Return True if this is the first notification for the mail."""
        cur = self._exec("INSERT OR IGNORE INTO notified VALUES (?,?)", (email_id, datetime.now().isoformat()))
        return cur.rowcount == 1

    def log_run(self, started: datetime, mails: int, llm_calls: int, errors: int) -> None:
        self._exec("INSERT INTO runs(started, mails, llm_calls, errors) VALUES (?,?,?,?)",
                   (started.isoformat(), mails, llm_calls, errors))
