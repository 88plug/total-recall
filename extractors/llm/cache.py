"""SQLite-backed content-hash cache for LLM call results.

Avoids redundant model calls when the input (model + prompts + schema) hasn't
changed since the last successful run.  The cache is caller-owned: callers
pass in the DB path (typically inside the plugin data directory).

Schema
------
Single table::

    llm_cache(
        key   TEXT PRIMARY KEY,   -- sha256(model || system || user || schema_json)
        model TEXT,               -- model name, for diagnostics / eviction
        value TEXT,               -- JSON-serialised result dict
        ts    INTEGER             -- unix timestamp of insertion (seconds)
    )

The caller is responsible for any TTL-based eviction; this class never deletes
rows automatically so that incremental rebuilds stay deterministic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key   TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    value TEXT NOT NULL,
    ts    INTEGER NOT NULL
)
"""


class LLMCache:
    """SQLite-backed content-hash cache for LLM JSON responses.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  The parent directory must exist
        (the plugin data dir is created by the indexer before this is called).
        If the file doesn't exist it will be created on first use.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.execute(_CREATE_TABLE)
            self._conn.commit()
        return self._conn

    @staticmethod
    def _make_key(model: str, system: str, user: str, schema_json: str | None) -> str:
        payload = model + "\x00" + system + "\x00" + user + "\x00" + (schema_json or "")
        return hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        model: str,
        system: str,
        user: str,
        schema_json: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the cached result dict, or None on a cache miss.

        Parameters
        ----------
        model:
            Model name (part of the cache key so different models don't share
            cached results for the same prompt).
        system:
            System prompt string.
        user:
            User prompt string.
        schema_json:
            Optional JSON-serialised schema; included in the key so schema
            changes invalidate the cache entry.
        """
        key = self._make_key(model, system, user, schema_json)
        try:
            conn = self._connect()
            row = conn.execute("SELECT value FROM llm_cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            result: dict[str, Any] = json.loads(row[0])
            log.debug("LLMCache: hit for key %.12s…", key)
            return result
        except Exception as exc:  # noqa: BLE001
            log.warning("LLMCache.get: error — %s", exc)
            return None

    def put(
        self,
        model: str,
        system: str,
        user: str,
        schema_json: str | None,
        result: dict[str, Any],
    ) -> None:
        """Store a result dict in the cache.

        Parameters
        ----------
        model:
            Model name.
        system:
            System prompt string.
        user:
            User prompt string.
        schema_json:
            Optional JSON-serialised schema (pass None when no schema was used).
        result:
            The parsed dict returned by the model (must be JSON-serialisable).
        """
        key = self._make_key(model, system, user, schema_json)
        value_text = json.dumps(result)
        ts = int(time.time())
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, model, value, ts) VALUES (?, ?, ?, ?)",
                (key, model, value_text, ts),
            )
            conn.commit()
            log.debug("LLMCache: stored key %.12s… (model=%s)", key, model)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLMCache.put: error — %s", exc)
