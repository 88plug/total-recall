"""Regression: rebuild --keep-file can DROP vec0 tables without prior load.

index.db.connect never loads sqlite-vec. DROP TABLE vec_chunks needs the
module (xDestroy) or raises ``no such module: vec0``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _have_sqlite_vec() -> bool:
    try:
        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("CREATE VIRTUAL TABLE t USING vec0(embedding float[4])")
        conn.close()
        return True
    except Exception:
        return False


HAS_SQLITE_VEC = _have_sqlite_vec()


@pytest.mark.skipif(not HAS_SQLITE_VEC, reason="sqlite_vec required")
def test_drop_all_tables_drops_l2_vec_without_prior_load(tmp_path: Path) -> None:
    """Mirror production connect: reopen without extension, then wipe."""
    import sqlite_vec

    from total_recall.cmd_rebuild import _drop_all_tables

    db_path = tmp_path / "index.db"
    setup = sqlite3.connect(str(db_path))
    setup.enable_load_extension(True)
    sqlite_vec.load(setup)
    setup.enable_load_extension(False)
    # L2 default (pre-cosine) + side tables rebuild must clear.
    setup.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[4])")
    setup.executescript(
        """
        CREATE TABLE chunk_embeddings (
            id INTEGER PRIMARY KEY,
            extraction_id INTEGER,
            chunk_text TEXT,
            chunk_index INTEGER
        );
        CREATE TABLE vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO vec_meta(key, value) VALUES ('format', '2'), ('dim', '4');
        INSERT INTO chunk_embeddings(id, extraction_id, chunk_text, chunk_index)
            VALUES (1, 1, 'seed', 0);
        CREATE TABLE extractions (id INTEGER PRIMARY KEY, content TEXT);
        CREATE VIRTUAL TABLE extractions_fts USING fts5(content);
        CREATE TABLE messages (id INTEGER PRIMARY KEY);
        CREATE VIRTUAL TABLE messages_fts USING fts5(body);
        CREATE TABLE ingest_state (k TEXT PRIMARY KEY);
        CREATE TABLE schema_meta (k TEXT PRIMARY KEY);
        """
    )
    setup.commit()
    setup.close()

    # isolation_level=None matches index.db.connect (autocommit).
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    # Deliberately do NOT load sqlite-vec here — _drop_all_tables must.
    _drop_all_tables(conn)

    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    conn.close()

    for gone in (
        "vec_chunks",
        "chunk_embeddings",
        "vec_meta",
        "extractions",
        "extractions_fts",
        "messages",
        "messages_fts",
        "ingest_state",
        "schema_meta",
    ):
        assert gone not in names, f"{gone} should be dropped; remaining={names}"


@pytest.mark.skipif(not HAS_SQLITE_VEC, reason="sqlite_vec required")
def test_drop_all_tables_noop_without_vec_tables(tmp_path: Path) -> None:
    """No vec_chunks → no extension load required; FTS still drops."""
    from total_recall.cmd_rebuild import _drop_all_tables

    db_path = tmp_path / "plain.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE extractions (id INTEGER PRIMARY KEY);
        CREATE VIRTUAL TABLE extractions_fts USING fts5(content);
        """
    )
    _drop_all_tables(conn)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    conn.close()
    assert "extractions" not in names
    assert "extractions_fts" not in names


@pytest.mark.skipif(not HAS_SQLITE_VEC, reason="sqlite_vec required")
def test_ensure_stamps_missing_distance_metric_meta() -> None:
    """Search/backfill path fills distance_metric when cosine DDL is present."""
    import sqlite_vec

    from vec.store import _ensure_dim_matches, _load_sqlite_vec, _read_meta

    class _Fake:
        model = "fake"
        backend = "fake"

        def dim(self) -> int:
            return 8

        def identity(self) -> str:
            return "fake:fake"

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        "CREATE VIRTUAL TABLE vec_chunks USING vec0("
        "embedding float[8] distance_metric=cosine)"
    )
    conn.executescript(
        """
        CREATE TABLE chunk_embeddings (
            id INTEGER PRIMARY KEY,
            extraction_id INTEGER,
            chunk_text TEXT,
            chunk_index INTEGER
        );
        CREATE TABLE vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO vec_meta(key, value) VALUES
            ('format', '2'),
            ('dim', '8'),
            ('model', 'fake'),
            ('backend', 'fake');
        """
    )
    conn.commit()
    assert _read_meta(conn, "distance_metric") is None
    _load_sqlite_vec(conn)
    _ensure_dim_matches(conn, _Fake())
    assert _read_meta(conn, "distance_metric") == "cosine"
    conn.close()
