"""Shared helpers for driving the MCP server from tests.

``mcp`` 2.x answers ``call_tool`` with a ``CallToolResult`` object rather than
the ``(content, structured)`` tuple 1.x returned. These helpers keep the
assertion style stable across the suite so individual tests read the payload,
not the transport shape.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def call_tool(mcp: Any, name: str, args: dict) -> tuple[Any, Any]:
    """Invoke ``name`` and return ``(content_blocks, structured)``.

    Tools returning ``list`` get ``structured_content = {"result": [...]}``.
    Tools returning a bare ``dict`` get ``structured_content = None`` and carry
    their JSON in the first text block, so fall back to parsing that.
    """
    result = asyncio.run(mcp.call_tool(name, args))
    structured = result.structured_content
    if structured is None and result.content:
        text = getattr(result.content[0], "text", None)
        if text is not None:
            try:
                structured = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                structured = text
    return result.content, structured


def unwrap_call_tool_result(result: Any) -> Any:
    """Normalize a ``(content, structured)`` pair into a plain Python value."""
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        if structured is not None:
            return structured
        if isinstance(content, (list, tuple)) and content:
            first = content[0]
            text = getattr(first, "text", None)
            if text is not None:
                try:
                    return json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    return text
            return first
        return None
    return result
