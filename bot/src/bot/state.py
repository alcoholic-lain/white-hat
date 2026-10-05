"""Tiny SQLite store: conversation key -> session id, plus local history for fallback mode."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class State:
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS local_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_local_messages_session
                ON local_messages(session_id, id);
            """
        )
        self._db.commit()

    # Calls are tiny single-row operations, so they run inline on the event loop.

    def get_session(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT session_id FROM conversations WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_session(self, key: str, session_id: str) -> None:
        self._db.execute(
            "INSERT INTO conversations(key, session_id) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET session_id = excluded.session_id",
            (key, session_id),
        )
        self._db.commit()

    def delete_session(self, key: str) -> None:
        row = self._db.execute(
            "SELECT session_id FROM conversations WHERE key = ?", (key,)
        ).fetchone()
        if row:
            self._db.execute("DELETE FROM local_messages WHERE session_id = ?", (row[0],))
        self._db.execute("DELETE FROM conversations WHERE key = ?", (key,))
        self._db.commit()

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self._db.execute(
            "INSERT INTO local_messages(session_id, role, content) VALUES(?, ?, ?)",
            (session_id, role, content),
        )
        self._db.commit()

    def get_messages(self, session_id: str, limit: int) -> list[dict[str, str]]:
        rows = self._db.execute(
            "SELECT role, content FROM local_messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def close(self) -> None:
        self._db.close()
