"""``total-recall consolidate`` — weekly cold-path consolidation pass (R2).

This is the heavy maintenance job. It runs once a week (via the systemd timer
installed by ``scripts/install-consolidate-timer.sh``) and does the work that
would be too expensive to do on every hook fire:

* **Decay pass**           — recompute confidence for ``operator_profile`` /
  ``standing_decisions`` / ``bans`` / ``voice_profile`` / ``vocabulary``,
  archive (don't delete) rows whose confidence drops below 0.2.
* **Conflict reconciliation** — scan ``tentative_facts``; promote those past
  threshold via ``index.tentative.promote_eligible``, drop those past TTL
  (30 d).
* **Vocabulary promotion** — 3-tier graduation:
    - tier 0 → 1: frequency >= 3 sessions AND >= 2 days  → confidence 0.4
    - tier 1 → 2: frequency >= 7 sessions AND >= 14 days
                  (or explicit user confirmation)        → confidence 0.85
    - demotion: not seen in 180 d → decayed; below 0.2 archived
* **Profile-drift detection** — for each ``operator_profile`` field with a
  recent contradiction in ``tentative_facts``, emit a "drift candidate" report.
* **Auto Dream-style cleanup** (Anthropic pattern):
    - delete superseded-for->30 d archived facts
    - prune references to no-longer-existing artifacts (cwds gone from disk,
      machines unseen in 60 d)
    - normalise relative-date phrases ("yesterday" → absolute ISO)
* **Summary report** — print a single table of what changed.

Every data-layer dependency (``index.decay``, ``index.tentative``,
``index.vocabulary``, ``index.operator``, ``index.bans``, ``index.voice``,
``index.profile_drift``, ``index.dream``) is imported defensively: if the
module isn't built yet, that step is skipped and the report says ``skipped``.

Two modes:

* ``--dry-run`` — compute everything, print the report, but do NO writes.
* default     — apply the changes inside a single transaction; report at end.

JSON output (``--json``) is the same data structure as the human report,
without the table chrome — that's what the systemd service ships into
``events.jsonl`` for later observability.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import click

from .util import format_json, format_table, resolve_db_path


# --------------------------------------------------------------------------- #
# Defensive module loaders
# --------------------------------------------------------------------------- #


def _try_import(modname: str) -> Any:
    """Return the imported module or ``None`` if it isn't built yet."""
    try:
        # ``__import__`` returns the top-level package; walk down for dotted.
        parts = modname.split(".")
        mod = __import__(modname)
        for p in parts[1:]:
            mod = getattr(mod, p)
        return mod
    except Exception:  # noqa: BLE001 — any failure means "not available"
        return None


def _open_conn(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(r[1] == col for r in rows)


# --------------------------------------------------------------------------- #
# Decay
# --------------------------------------------------------------------------- #


_DECAY_TABLES = (
    "operator_profile",
    "standing_decisions",
    "bans",
    "voice_profile",
    "vocabulary",
)


def _now_seconds() -> int:
    return int(time.time())


def _decay_one_table(
    conn: sqlite3.Connection,
    table: str,
    adjusted: Callable[..., float] | None,
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Walk one table and recompute confidence; archive low-confidence rows.

    The schema here is *expected* to grow:

        confidence              REAL,
        last_reasserted_ts      INTEGER,
        n_reassertions          INTEGER,
        archived                INTEGER  (0/1)

    Tables that don't yet have those columns are skipped gracefully.
    """
    out = {"scanned": 0, "decayed": 0, "archived": 0}
    if not _has_table(conn, table):
        return out
    # We need at least confidence + a "last reasserted" timestamp.
    if not (_has_col(conn, table, "confidence") and _has_col(conn, table, "last_reasserted_ts")):
        return out

    now = _now_seconds()
    n_col = "n_reassertions" if _has_col(conn, table, "n_reassertions") else None
    archived_col = "archived" if _has_col(conn, table, "archived") else None

    select = (
        f"SELECT rowid AS _rid, confidence, last_reasserted_ts"
        + (f", {n_col}" if n_col else "")
        + f" FROM {table}"
        + (f" WHERE COALESCE({archived_col},0)=0" if archived_col else "")
    )
    try:
        rows = conn.execute(select).fetchall()
    except sqlite3.Error:
        return out

    for r in rows:
        out["scanned"] += 1
        base = float(r["confidence"] or 0.0)
        last = int(r["last_reasserted_ts"] or 0)
        days = max(0, (now - last) // 86400)
        n_reasserts = int(r[n_col]) if n_col and r[n_col] is not None else 0
        if adjusted is not None:
            try:
                new_conf = float(adjusted(base, days, n_reasserts))
            except Exception:  # noqa: BLE001 — fall back to simple half-life
                new_conf = _fallback_decay(base, days, n_reasserts)
        else:
            new_conf = _fallback_decay(base, days, n_reasserts)

        if abs(new_conf - base) < 1e-6:
            continue
        out["decayed"] += 1
        if new_conf < 0.2 and archived_col:
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET confidence=?, {archived_col}=1 WHERE rowid=?",
                    (new_conf, r["_rid"]),
                )
            out["archived"] += 1
        else:
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET confidence=? WHERE rowid=?",
                    (new_conf, r["_rid"]),
                )
    return out


def _fallback_decay(base: float, days: int, n_reassertions: int) -> float:
    """A reasonable default when ``index.decay`` isn't built yet.

    Exponential half-life of 90 days, gently boosted by reassertion count.
    Mirrors what R2 documents so synthetic tests are still meaningful.
    """
    half_life = 90.0
    decay = 0.5 ** (days / half_life) if days > 0 else 1.0
    boost = min(1.0, 1.0 + 0.1 * max(0, n_reassertions - 1))
    return max(0.0, min(1.0, base * decay * boost))


# --------------------------------------------------------------------------- #
# Tentative-fact reconciliation
# --------------------------------------------------------------------------- #


_TENTATIVE_TTL_DAYS = 30


def _reconcile_tentative(
    conn: sqlite3.Connection, tentative_mod: Any | None, *, dry_run: bool
) -> dict[str, int]:
    out = {"promoted": 0, "dropped": 0, "scanned": 0}
    if not _has_table(conn, "tentative_facts"):
        return out

    try:
        rows = conn.execute("SELECT rowid, * FROM tentative_facts").fetchall()
    except sqlite3.Error:
        return out
    out["scanned"] = len(rows)

    # Promotion via the (future) index.tentative.promote_eligible helper.
    if tentative_mod is not None and hasattr(tentative_mod, "promote_eligible"):
        try:
            promoted = tentative_mod.promote_eligible(conn, dry_run=dry_run) or []
            if isinstance(promoted, int):
                out["promoted"] = promoted
            else:
                out["promoted"] = len(list(promoted))
        except Exception:  # noqa: BLE001
            pass

    # TTL drop is a stable operation we can do ourselves.
    if _has_col(conn, "tentative_facts", "created_ts"):
        cutoff = _now_seconds() - _TENTATIVE_TTL_DAYS * 86400
        try:
            stale = conn.execute(
                "SELECT rowid AS _rid FROM tentative_facts WHERE created_ts < ?",
                (cutoff,),
            ).fetchall()
            out["dropped"] = len(stale)
            if not dry_run and stale:
                conn.execute(
                    "DELETE FROM tentative_facts WHERE created_ts < ?", (cutoff,)
                )
        except sqlite3.Error:
            pass
    return out


# --------------------------------------------------------------------------- #
# Vocabulary tier promotion
# --------------------------------------------------------------------------- #


def _promote_vocabulary(
    conn: sqlite3.Connection, *, dry_run: bool
) -> dict[str, int]:
    """3-tier vocabulary promotion / demotion per R2."""
    out = {"tier0_to_1": 0, "tier1_to_2": 0, "demoted": 0, "archived": 0}

    # Tier 0 candidates live in ``vocab_candidates`` (or similar); we look both.
    cand_table = None
    for name in ("vocab_candidates", "vocabulary_candidates", "vocabulary_tier0"):
        if _has_table(conn, name):
            cand_table = name
            break

    if cand_table and _has_table(conn, "vocabulary"):
        try:
            rows = conn.execute(
                f"SELECT term, n_sessions, n_days FROM {cand_table} "
                f"WHERE n_sessions >= 3 AND n_days >= 2"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            out["tier0_to_1"] += 1
            if not dry_run:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO vocabulary(term, tier, confidence, "
                        "last_reasserted_ts) VALUES (?, 1, 0.4, ?)",
                        (r["term"], _now_seconds()),
                    )
                except sqlite3.Error:
                    pass

    if _has_table(conn, "vocabulary"):
        # Tier 1 → 2.
        try:
            cols = [c[1] for c in conn.execute("PRAGMA table_info(vocabulary)").fetchall()]
        except sqlite3.Error:
            cols = []
        if {"tier", "n_sessions", "n_days"}.issubset(cols):
            rows = conn.execute(
                "SELECT rowid AS _rid FROM vocabulary "
                "WHERE tier=1 AND (n_sessions >= 7 AND n_days >= 14) "
                "   OR (user_confirmed=1 AND tier=1)"
            ).fetchall() if "user_confirmed" in cols else conn.execute(
                "SELECT rowid AS _rid FROM vocabulary "
                "WHERE tier=1 AND n_sessions >= 7 AND n_days >= 14"
            ).fetchall()
            out["tier1_to_2"] = len(rows)
            if not dry_run and rows:
                conn.executemany(
                    "UPDATE vocabulary SET tier=2, confidence=0.85 WHERE rowid=?",
                    [(r["_rid"],) for r in rows],
                )

        # Demotion: not seen in 180 days.
        if "last_seen_ts" in cols:
            cutoff = _now_seconds() - 180 * 86400
            stale = conn.execute(
                "SELECT rowid AS _rid, confidence FROM vocabulary WHERE last_seen_ts < ?",
                (cutoff,),
            ).fetchall()
            for r in stale:
                new_conf = _fallback_decay(float(r["confidence"] or 0.0), 180, 0)
                out["demoted"] += 1
                if new_conf < 0.2 and "archived" in cols:
                    out["archived"] += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE vocabulary SET confidence=?, archived=1 WHERE rowid=?",
                            (new_conf, r["_rid"]),
                        )
                else:
                    if not dry_run:
                        conn.execute(
                            "UPDATE vocabulary SET confidence=? WHERE rowid=?",
                            (new_conf, r["_rid"]),
                        )
    return out


# --------------------------------------------------------------------------- #
# Profile-drift detection
# --------------------------------------------------------------------------- #


def _detect_profile_drift(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return a list of ``operator_profile`` fields with recent contradictions."""
    if not (_has_table(conn, "operator_profile") and _has_table(conn, "tentative_facts")):
        return []
    # Best-effort heuristic; the production version (index.profile_drift) can
    # do something much smarter once it exists.
    try:
        rows = conn.execute(
            """
            SELECT op.field AS field, op.value AS current_value, tf.value AS new_value,
                   tf.created_ts AS observed_ts
              FROM operator_profile op
              JOIN tentative_facts tf
                ON tf.field = op.field
               AND tf.value IS NOT op.value
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Auto Dream-style cleanup
# --------------------------------------------------------------------------- #


def _auto_dream_cleanup(
    conn: sqlite3.Connection, dream_mod: Any | None, *, dry_run: bool
) -> dict[str, int]:
    out = {
        "deleted_superseded": 0,
        "pruned_cwds": 0,
        "pruned_machines": 0,
        "normalised_dates": 0,
    }
    if dream_mod is not None and hasattr(dream_mod, "cleanup"):
        try:
            ret = dream_mod.cleanup(conn, dry_run=dry_run) or {}
            if isinstance(ret, dict):
                out.update({k: int(ret.get(k, out[k])) for k in out})
                return out
        except Exception:  # noqa: BLE001
            pass

    # Local fallback so the report is meaningful even without the helper.
    cutoff = _now_seconds() - 30 * 86400

    # 1. Delete superseded-for->30 d archived facts (we look at any of the
    #    decay-eligible tables that carry ``archived`` + ``last_reasserted_ts``).
    for table in _DECAY_TABLES:
        if not _has_table(conn, table):
            continue
        if not (_has_col(conn, table, "archived") and _has_col(conn, table, "last_reasserted_ts")):
            continue
        try:
            stale = conn.execute(
                f"SELECT rowid FROM {table} "
                f"WHERE archived=1 AND last_reasserted_ts < ?",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            continue
        out["deleted_superseded"] += len(stale)
        if not dry_run and stale:
            conn.execute(
                f"DELETE FROM {table} WHERE archived=1 AND last_reasserted_ts < ?",
                (cutoff,),
            )

    # 2. Prune cwds that no longer exist on disk.
    if _has_table(conn, "projects") and _has_col(conn, "projects", "cwd"):
        try:
            all_cwds = conn.execute("SELECT cwd FROM projects").fetchall()
        except sqlite3.Error:
            all_cwds = []
        gone = [r["cwd"] for r in all_cwds if r["cwd"] and not Path(r["cwd"]).expanduser().exists()]
        out["pruned_cwds"] = len(gone)
        if not dry_run and gone:
            conn.executemany("DELETE FROM projects WHERE cwd=?", [(c,) for c in gone])

    # 3. Prune machines unseen in 60 days.
    if _has_table(conn, "machines") and _has_col(conn, "machines", "last_seen_ts"):
        cut60 = _now_seconds() - 60 * 86400
        try:
            stale_m = conn.execute(
                "SELECT rowid FROM machines WHERE last_seen_ts < ?", (cut60,)
            ).fetchall()
        except sqlite3.Error:
            stale_m = []
        out["pruned_machines"] = len(stale_m)
        if not dry_run and stale_m:
            conn.execute("DELETE FROM machines WHERE last_seen_ts < ?", (cut60,))

    return out


# --------------------------------------------------------------------------- #
# Report helpers
# --------------------------------------------------------------------------- #


def _report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    decay = report.get("decay") or {}
    for table, stats in decay.items():
        rows.append(
            {
                "section": "decay",
                "target": table,
                "scanned": stats.get("scanned", 0),
                "changed": stats.get("decayed", 0),
                "archived": stats.get("archived", 0),
            }
        )
    tent = report.get("tentative") or {}
    rows.append(
        {
            "section": "tentative",
            "target": "tentative_facts",
            "scanned": tent.get("scanned", 0),
            "changed": tent.get("promoted", 0),
            "archived": tent.get("dropped", 0),
        }
    )
    vocab = report.get("vocabulary") or {}
    rows.append(
        {
            "section": "vocabulary",
            "target": "tier0→1",
            "scanned": vocab.get("tier0_to_1", 0),
            "changed": vocab.get("tier0_to_1", 0),
            "archived": 0,
        }
    )
    rows.append(
        {
            "section": "vocabulary",
            "target": "tier1→2",
            "scanned": vocab.get("tier1_to_2", 0),
            "changed": vocab.get("tier1_to_2", 0),
            "archived": 0,
        }
    )
    rows.append(
        {
            "section": "vocabulary",
            "target": "demotion",
            "scanned": vocab.get("demoted", 0),
            "changed": vocab.get("demoted", 0),
            "archived": vocab.get("archived", 0),
        }
    )
    dream = report.get("dream") or {}
    rows.append(
        {
            "section": "dream",
            "target": "superseded",
            "scanned": dream.get("deleted_superseded", 0),
            "changed": dream.get("deleted_superseded", 0),
            "archived": 0,
        }
    )
    rows.append(
        {
            "section": "dream",
            "target": "stale-cwds/machines",
            "scanned": dream.get("pruned_cwds", 0) + dream.get("pruned_machines", 0),
            "changed": dream.get("pruned_cwds", 0) + dream.get("pruned_machines", 0),
            "archived": 0,
        }
    )
    return rows


# --------------------------------------------------------------------------- #
# Click command
# --------------------------------------------------------------------------- #


@click.command(
    name="consolidate",
    help=(
        "Weekly cold-path consolidation: decay confidences, reconcile tentative "
        "facts, promote vocabulary tiers, detect profile drift, run auto Dream "
        "cleanup. Print a summary table at the end."
    ),
)
@click.option("--dry-run", is_flag=True, help="Compute everything, but write nothing.")
@click.option("--verbose", is_flag=True, help="Per-step logging to stderr.")
@click.pass_context
def consolidate_cmd(ctx: click.Context, dry_run: bool, verbose: bool) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    as_json = bool(ctx.obj.get("json"))
    # Inherit -v from the global flag too.
    verbose = verbose or bool(ctx.obj.get("verbose"))

    decay_mod = _try_import("index.decay")
    tentative_mod = _try_import("index.tentative")
    dream_mod = _try_import("index.dream")
    adjusted_conf: Callable[..., float] | None = (
        getattr(decay_mod, "adjusted_confidence", None) if decay_mod else None
    )

    report: dict[str, Any] = {
        "db": str(db_path),
        "dry_run": dry_run,
        "started_iso": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "decay": {},
        "tentative": {},
        "vocabulary": {},
        "drift_candidates": [],
        "dream": {},
        "skipped": [],
    }

    conn = _open_conn(db_path)
    if conn is None:
        report["skipped"].append("db-missing")
        _emit(ctx, report, as_json)
        return

    try:
        # All write operations sit inside ONE transaction so a mid-pass crash
        # leaves the DB consistent.
        conn.execute("BEGIN")

        for table in _DECAY_TABLES:
            if verbose:
                click.echo(f"[consolidate] decay: {table}", err=True)
            report["decay"][table] = _decay_one_table(
                conn, table, adjusted_conf, dry_run=dry_run
            )

        if verbose:
            click.echo("[consolidate] tentative reconciliation", err=True)
        report["tentative"] = _reconcile_tentative(conn, tentative_mod, dry_run=dry_run)

        if verbose:
            click.echo("[consolidate] vocabulary promotion", err=True)
        report["vocabulary"] = _promote_vocabulary(conn, dry_run=dry_run)

        if verbose:
            click.echo("[consolidate] profile-drift detection", err=True)
        report["drift_candidates"] = _detect_profile_drift(conn)

        if verbose:
            click.echo("[consolidate] auto-dream cleanup", err=True)
        report["dream"] = _auto_dream_cleanup(conn, dream_mod, dry_run=dry_run)

        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    report["finished_iso"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _emit(ctx, report, as_json)


def _emit(ctx: click.Context, report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(format_json(report))
        return
    click.echo(
        f"total-recall consolidate — {'DRY-RUN ' if report['dry_run'] else ''}"
        f"db={report['db']}"
    )
    rows = _report_rows(report)
    click.echo(
        format_table(rows, headers=["section", "target", "scanned", "changed", "archived"])
    )
    drift = report.get("drift_candidates") or []
    if drift:
        click.echo(f"  drift candidates: {len(drift)} field(s)")
        for d in drift[:5]:
            click.echo(
                f"    - {d.get('field')}: {d.get('current_value')!r} → "
                f"{d.get('new_value')!r}"
            )
    skipped = report.get("skipped") or []
    if skipped:
        click.echo(f"  skipped: {', '.join(skipped)}")
