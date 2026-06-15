"""MCP tool: ``get_voice_profile``.

Reads the cached voice profile from the ``voice_profile`` SQLite table
and returns it as a flat dict the model can consult before phrasing a
response. The tool is *cheap* — a single ``SELECT * FROM voice_profile``
against a read-only handle. It deliberately does NOT re-mine the corpus;
that happens during ``total-recall index`` runs via the voice extractor.

Companion to :mod:`mcp_server.extras.operator_tools`: that one tells
sessions WHO is asking, this one tells them HOW that person talks.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


def _index_missing_error() -> dict[str, Any]:
    return {
        "error": "index not initialized; run `total-recall index` to build it",
        "db_path": str(DB_PATH),
    }


@mcp.tool()
def get_voice_profile() -> dict:
    (
        "Return the operator's measured communication style — casing, brevity, profanity rate, "
        "signature typos, top first words. Use to match cadence in responses. Calling this AND "
        "following its rules eliminates the 'you sound like an AI' feel that prompts pushback."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    try:
        try:
            from index.voice import get_voice
        except Exception as exc:  # pragma: no cover — import error path
            log.warning("index.voice unavailable: %s", exc)
            return {"error": f"index.voice not available: {exc!r}"}

        try:
            profile = get_voice(conn)
        except Exception as exc:
            log.exception("get_voice_profile() query failed")
            return {"error": f"get_voice_profile failed: {exc!r}"}

        # Empty DB / table → return a stable error shape rather than a
        # bare metadata-only dict; that way callers can branch on the
        # `error` key without parsing values.
        if not profile or set(profile.keys()) <= {"_measured_at", "_sample_size"}:
            return {
                "error": (
                    "voice profile not yet measured; run `total-recall index` "
                    "after upgrading to a build with the voice_profile "
                    "extractor enabled"
                ),
                "db_path": str(DB_PATH),
            }

        return profile
    finally:
        with contextlib.suppress(Exception):
            conn.close()


__all__ = ["get_voice_profile"]
