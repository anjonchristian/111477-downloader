"""Small sqlite cache for index listings."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    path       TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    path       TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get_listing(self, path: str, ttl_seconds: float) -> tuple[list[dict[str, Any]] | None, float | None]:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, payload FROM listings WHERE path = ?", (path,)
            ).fetchone()
        if not row:
            return None, None
        fetched_at, payload = row
        if time.time() - fetched_at > ttl_seconds:
            return None, fetched_at
        try:
            return json.loads(payload), fetched_at
        except ValueError:
            return None, fetched_at

    def get_stale_listing(self, path: str) -> tuple[list[dict[str, Any]] | None, float | None]:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, payload FROM listings WHERE path = ?", (path,)
            ).fetchone()
        if not row:
            return None, None
        try:
            return json.loads(row[1]), row[0]
        except ValueError:
            return None, row[0]

    def put_listing(self, path: str, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO listings(path, fetched_at, payload) VALUES(?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
                (path, time.time(), json.dumps(entries, separators=(",", ":"))),
            )
            self._conn.commit()

    def put_link(self, path: str, url: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO links(path, url, created_at) VALUES(?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET url=excluded.url, created_at=excluded.created_at",
                (path, url, time.time()),
            )
            self._conn.commit()

    def clear_listings(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM listings")
            self._conn.commit()

    def clear_links(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM links")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
