"""Optional MCP resources for ``total-recall``.

Resources let the model pull a named blob via URI instead of calling a tool —
useful for stable, addressable artifacts like a session digest. We expose
just one resource here: ``session-digest://{session_id}`` which renders the
same data as :func:`mcp_server.tools.get_session_digest` as Markdown.

A resource is *content-shaped* (a single blob), while a tool is
*action-shaped* (returns structured data). Both are valid surfaces for the
session digest, but the Markdown rendering is more compact for direct
display when the model wants to quote it back to the user.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server.server import mcp
from mcp_server.tools import get_session_digest

log = logging.getLogger(__name__)


def _format_block(title: str, items: list[Any]) -> list[str]:
    """Render a list of extraction dicts as a Markdown bullet block."""
    out: list[str] = [f"## {title}"]
    if not items:
        out.append("_none_\n")
        return out
    for it in items:
        if not isinstance(it, dict):
            out.append(f"- {it}")
            continue
        content = (it.get("content") or "").strip().replace("\n", " ")
        score = it.get("score")
        score_part = f" _(score {score:.2f})_" if isinstance(score, (int, float)) else ""
        if len(content) > 280:
            content = content[:277] + "..."
        out.append(f"- {content}{score_part}")
    out.append("")
    return out


@mcp.resource("session-digest://{session_id}")
def session_digest_resource(session_id: str) -> str:
    """Markdown digest for a single past session.

    Wraps :func:`mcp_server.tools.get_session_digest` and renders the result
    in a compact human-readable form. Returns an error-flavored Markdown
    string instead of raising when the digest fails so the resource read
    never blows up the MCP loop.
    """
    digest = get_session_digest(session_id)
    if not isinstance(digest, dict):
        return f"# Session {session_id}\n\n_unexpected digest type: {type(digest).__name__}_\n"
    if "error" in digest:
        return (
            f"# Session {session_id}\n\n"
            f"**Error:** {digest['error']}\n"
        )

    lines: list[str] = [f"# Session {session_id}"]
    title = digest.get("ai_title")
    if title:
        lines.append(f"**Title:** {title}")
    cwd = digest.get("cwd")
    if cwd:
        lines.append(f"**cwd:** `{cwd}`")
    started = digest.get("started_at")
    if started:
        lines.append(f"**Started:** {started}")
    msg_count = digest.get("message_count")
    if msg_count is not None:
        lines.append(f"**Messages:** {msg_count}")
    last = digest.get("last_prompt")
    if last:
        snippet = str(last).strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        lines.append(f"**Last prompt:** {snippet}")
    lines.append("")

    lines.extend(_format_block("Top decisions", digest.get("top_decisions") or []))
    lines.extend(_format_block("Top corrections", digest.get("top_corrections") or []))
    lines.extend(_format_block("Progress markers", digest.get("progress_markers") or []))

    return "\n".join(lines)


__all__ = ["session_digest_resource"]
