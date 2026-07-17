"""MCP tools backed by the ``projects`` / ``machines`` / ``vocabulary`` tables.

Three read-only tools form the *orientation* surface for a fresh session:

* :func:`get_project_graph` — what other projects this operator runs and how
  they cross-link, so the model can offer to look at sibling repos rather
  than re-deriving context.
* :func:`get_machine_inventory` — hostnames, LAN/Tailscale IPs, GPUs, so
  the model can reason about which box is being referenced when the
  operator says "spin it up on host-alpha".
* :func:`define_term` — operator-specific vocabulary lookup. Cheap to call,
  saves the model from confidently inventing a wrong definition.

All three degrade gracefully when the index is absent (returning an
``error`` payload) and never raise into the FastMCP transport.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from typing import Any

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_missing_error() -> dict:
    return {
        "error": "index not initialized; run `total-recall index` to build it",
        "db_path": str(DB_PATH),
    }


def _load_ontology_module():
    try:
        from index import ontology as ontology_mod
    except Exception as e:  # pragma: no cover - defensive
        log.warning("index.ontology unavailable: %s", e)
        return None
    return ontology_mod


def _close(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    with contextlib.suppress(Exception):
        conn.close()


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        try:
            return dict(row)
        except Exception:
            pass
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(vars(row))
    except TypeError:
        return {"value": str(row)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(title="Get Project Graph", annotations=ToolAnnotations(readOnlyHint=True))
def get_project_graph() -> dict:
    """
    Return the operator's project graph: cwd -> {purpose, primary_language,
    related_projects, last_active}. Use to orient at session start — know what other
    projects this one connects to.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    ontology_mod = _load_ontology_module()
    try:
        if ontology_mod is None:
            # Fall back to raw SQL so we don't crash if index.ontology is
            # missing on a stale install — the table shape is stable.
            try:
                rows = conn.execute(
                    "SELECT cwd, purpose, primary_language, related_projects, "
                    "last_active_ts, message_count FROM projects "
                    "ORDER BY last_active_ts DESC"
                ).fetchall()
            except sqlite3.OperationalError as e:
                return {"error": f"projects table missing: {e!r}"}
            graph: dict[str, dict] = {}
            for r in rows:
                d = _row_to_dict(r)
                related = d.get("related_projects") or "[]"
                if isinstance(related, str):
                    import json as _json

                    try:
                        related = _json.loads(related)
                    except Exception:
                        related = []
                graph[d["cwd"]] = {
                    "purpose": d.get("purpose") or "",
                    "primary_language": d.get("primary_language") or "",
                    "related_projects": related,
                    "last_active_ts": d.get("last_active_ts"),
                    "message_count": d.get("message_count"),
                }
            return {"projects": graph, "count": len(graph)}

        projects = ontology_mod.list_projects(conn)
        graph = {
            p["cwd"]: {
                "purpose": p.get("purpose") or "",
                "primary_language": p.get("primary_language") or "",
                "related_projects": p.get("related_projects") or [],
                "last_active_ts": p.get("last_active_ts"),
                "message_count": p.get("message_count"),
            }
            for p in projects
        }
        return {"projects": graph, "count": len(graph)}
    except Exception as e:
        log.exception("get_project_graph() failed")
        return {"error": f"get_project_graph failed: {e!r}"}
    finally:
        _close(conn)


@mcp.tool(title="Get Machine Inventory", annotations=ToolAnnotations(readOnlyHint=True))
def get_machine_inventory(name_pattern: str | None = None) -> list[dict]:
    """
    Return the operator's machine inventory: hostname -> {role, lan_ip, tailscale_ip, gpu}.
    Use when working on LAN/cluster tasks. Pattern is a regex applied to hostname; pass None
    for all.
    """
    conn = get_conn()
    if conn is None:
        return [_index_missing_error()]

    ontology_mod = _load_ontology_module()
    try:
        if ontology_mod is None:
            try:
                rows = conn.execute("SELECT * FROM machines ORDER BY hostname").fetchall()
            except sqlite3.OperationalError as e:
                return [{"error": f"machines table missing: {e!r}"}]
            machines = [_row_to_dict(r) for r in rows]
            if name_pattern:
                import re

                try:
                    pat = re.compile(name_pattern)
                    machines = [m for m in machines if pat.search(m.get("hostname", ""))]
                except re.error:
                    pass
        else:
            machines = ontology_mod.list_machines(conn, name_pattern=name_pattern)
        # Shape: keep the documented "shorthand" fields up front.
        return [
            {
                "hostname": m.get("hostname"),
                "role": m.get("role") or "",
                "lan_ip": m.get("lan_ip") or "",
                "tailscale_ip": m.get("tailscale_ip") or "",
                "public_ip": m.get("public_ip") or "",
                "gpu": m.get("gpu") or "",
                "os": m.get("os") or "",
                "notes": m.get("notes") or "",
                "last_seen_ts": m.get("last_seen_ts"),
            }
            for m in machines
        ]
    except Exception as e:
        log.exception("get_machine_inventory() failed")
        return [{"error": f"get_machine_inventory failed: {e!r}"}]
    finally:
        _close(conn)


@mcp.tool(title="Define Term", annotations=ToolAnnotations(readOnlyHint=True))
def define_term(term: str) -> dict | None:
    """
    Lookup operator-specific vocabulary. E.g. define_term('relay fleet') returns the
    operator's own definition of that term. Use when encountering unfamiliar nouns in the
    operator's prompts.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    ontology_mod = _load_ontology_module()
    try:
        if ontology_mod is None:
            try:
                row = conn.execute(
                    "SELECT * FROM vocabulary WHERE LOWER(term) = LOWER(?)",
                    (term,),
                ).fetchone()
            except sqlite3.OperationalError as e:
                return {"error": f"vocabulary table missing: {e!r}"}
            if row is None:
                return None
            d = _row_to_dict(row)
        else:
            d = ontology_mod.get_term(conn, term)
            if d is None:
                return None
        return {
            "term": d.get("term"),
            "definition": d.get("definition"),
            "category": d.get("category") or "concept",
            "frequency": d.get("frequency", 1),
            "first_seen_ts": d.get("first_seen_ts"),
        }
    except Exception as e:
        log.exception("define_term() failed")
        return {"error": f"define_term failed: {e!r}"}
    finally:
        _close(conn)


__all__ = [
    "get_project_graph",
    "get_machine_inventory",
    "define_term",
]
