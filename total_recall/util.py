"""Shared formatting helpers for the total-recall CLI.

Three output shapes:

* :func:`format_table` — ASCII table for humans on a terminal.
* :func:`format_json`  — ``json.dumps(..., default=str)``  for machines.
* :func:`format_human` — single-record key/value pretty print.

Everything is stdlib — no ``rich`` / ``tabulate`` dep so ``total-recall``
keeps a tight install footprint.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "format_table",
    "format_json",
    "format_human",
    "resolve_db_path",
    "iso_or_none",
    "shorten",
    "human_bytes",
]


# --------------------------------------------------------------------------- #
# DB-path resolution
# --------------------------------------------------------------------------- #


def resolve_db_path(cli_value: str | os.PathLike[str] | None) -> Path:
    """Resolve the on-disk DB path.

    Precedence (highest first):

    1. ``--db`` CLI flag (``cli_value``).
    2. :func:`index.db.resolve_db_path` (``TOTAL_RECALL_DB`` /
       ``TOTAL_RECALL_DB_DIR`` / ``CLAUDE_PLUGIN_DATA`` / plugin discovery / XDG).
    """
    if cli_value:
        return Path(cli_value).expanduser()
    try:
        from index.db import resolve_db_path as _resolve  # type: ignore[import-not-found]

        return Path(_resolve())
    except Exception:
        env = os.environ.get("TOTAL_RECALL_DB")
        if env:
            return Path(env).expanduser()
        base_env = os.environ.get("CLAUDE_PLUGIN_DATA")
        if base_env:
            return Path(base_env).expanduser() / "total-recall" / "index.db"
        return Path("~/.local/share/total-recall/index.db").expanduser()


# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if isinstance(value, float):
        # Avoid scientific notation in tables.
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def format_table(
    rows: Sequence[Mapping[str, Any]] | Sequence[Sequence[Any]],
    headers: Sequence[str] | None = None,
    max_col_width: int = 60,
) -> str:
    """Render ``rows`` as a fixed-width ASCII table.

    Accepts a sequence of dicts (headers inferred from the first row's keys
    when not supplied) or a sequence of tuples (``headers`` required).
    Empty input → ``"(no rows)"``.
    """
    row_list = list(rows)
    if not row_list:
        return "(no rows)"

    # Normalise to list[list[str]].
    first = row_list[0]
    if isinstance(first, Mapping):
        if headers is None:
            headers = list(first.keys())
        body = [
            [_stringify(row.get(h, "")) for h in headers]  # type: ignore[union-attr]
            for row in row_list
        ]
    else:
        if headers is None:
            raise ValueError("headers required when rows are tuples")
        body = [[_stringify(v) for v in row] for row in row_list]  # type: ignore[arg-type]

    # Truncate over-wide cells.
    def _truncate(s: str) -> str:
        if len(s) <= max_col_width:
            return s
        return s[: max_col_width - 1] + "…"

    body = [[_truncate(c) for c in row] for row in body]
    headers_disp = [_truncate(h) for h in headers]

    widths = [len(h) for h in headers_disp]
    for row in body:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    sep = "+".join("-" * (w + 2) for w in widths)
    sep = f"+{sep}+"
    header_line = (
        "| " + " | ".join(h.ljust(w) for h, w in zip(headers_disp, widths, strict=False)) + " |"
    )
    out_lines = [sep, header_line, sep]
    for row in body:
        out_lines.append(
            "| " + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=False)) + " |"
        )
    out_lines.append(sep)
    return "\n".join(out_lines)


def format_json(obj: Any, *, indent: int = 2) -> str:
    """Wrap :func:`json.dumps` with a default that stringifies anything weird."""
    return json.dumps(obj, indent=indent, default=str, sort_keys=False)


def format_human(record: Mapping[str, Any]) -> str:
    """Pretty-print a single record as ``key: value`` pairs."""
    if not record:
        return "(empty)"
    width = max(len(str(k)) for k in record)
    return "\n".join(f"{str(k).ljust(width)}  {_stringify(v)}" for k, v in record.items())


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #


def iso_or_none(ts) -> str | None:
    """Convert a unix-seconds timestamp OR datetime to an ISO-8601 string."""
    if ts is None:
        return None
    if isinstance(ts, _dt.datetime):
        return ts.isoformat()
    try:
        return _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def shorten(text: str | None, limit: int = 120) -> str:
    """Collapse whitespace + truncate, for one-line table cells."""
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def human_bytes(n: int | None) -> str:
    """Format ``n`` bytes as e.g. ``"4.2 MB"``."""
    if n is None:
        return "0 B"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(val) < 1024.0:
            if unit == "B":
                return f"{int(val)} {unit}"
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} PB"


def emit(
    ctx_obj: Mapping[str, Any] | None,
    payload: Any,
    *,
    table_rows: Iterable[Mapping[str, Any]] | None = None,
    table_headers: Sequence[str] | None = None,
) -> None:
    """Write a payload to stdout in the format requested by the global flags.

    * If ``ctx_obj["json"]`` → JSON dump of ``payload``.
    * Else if ``table_rows`` is given → table render.
    * Else → ``format_human(payload)`` if it's a mapping, else ``str``.
    """
    as_json = bool(ctx_obj and ctx_obj.get("json"))
    if as_json:
        sys.stdout.write(format_json(payload))
        sys.stdout.write("\n")
        return
    if table_rows is not None:
        sys.stdout.write(format_table(list(table_rows), headers=table_headers))
        sys.stdout.write("\n")
        return
    if isinstance(payload, Mapping):
        sys.stdout.write(format_human(payload))
        sys.stdout.write("\n")
        return
    sys.stdout.write(str(payload))
    sys.stdout.write("\n")
