"""``total-recall sources`` — manage which CLI clients are ingested.

total-recall ingests session transcripts from a growing list of CLI
agent clients (Claude Code, OpenCode, Gemini CLI, Codex, Cursor,
Continue, Cline, Aider). Each client is implemented as a
:class:`lib.sources.base.SessionSource` adapter. This subcommand is the
operator-facing surface for that registry:

* ``sources list``    — print every known source + whether it has
                        detectable on-disk data + whether it is enabled
                        in the local config.
* ``sources detect``  — same as ``list`` but only prints sources that
                        report ``is_available() == True``.
* ``sources enable``  — move a name from ``disabled`` to ``enabled``
                        in the persisted config.
* ``sources disable`` — the inverse.
* ``sources test``    — call ``discover_sessions()`` on one named
                        source and report how many sessions it found
                        (cheap; no record bodies are read).

The default policy is **all sources enabled**. Concrete adapters that
have no on-disk data simply yield zero sessions; there's no harm in
leaving them in the ``enabled`` list. Operators only need to touch the
config to *suppress* a source that they want total-recall to ignore
even when its directory exists (e.g. throw-away experiments under
``~/.codex`` they don't want indexed).

Storage
-------

Config lives at ``${CLAUDE_PLUGIN_DATA}/total-recall/sources.json``
(falling back to ``~/.local/share/total-recall/sources.json`` when
``CLAUDE_PLUGIN_DATA`` is unset). Format:

.. code-block:: json

    {
      "enabled": ["claude_code", "opencode", ...],
      "disabled": []
    }

Missing names from either list are treated as **enabled** — config is
additive, not exhaustive. New adapters that ship in a later release
become enabled-by-default without operator intervention.

All adapter imports are *defensive*. If an adapter module fails to
import (missing optional dep, parallel-build skew), this command
swallows the error and continues — losing one source must not break
the whole CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from .util import format_json, format_table

# --------------------------------------------------------------------------- #
# Known sources
# --------------------------------------------------------------------------- #
#
# This is the canonical list of source names the CLI is willing to talk
# about. It's intentionally hardcoded so ``sources list`` produces stable
# output even if one of the adapter modules fails to import (a docs page
# referencing the source is still valuable).

KNOWN_SOURCES: tuple[str, ...] = (
    "claude_code",
    "opencode",
    "gemini_cli",
    "codex",
    "cursor",
    "continue",
    "cline",
    "aider",
)


# --------------------------------------------------------------------------- #
# Config persistence
# --------------------------------------------------------------------------- #


def _config_dir() -> Path:
    """Return the directory where ``sources.json`` lives.

    Precedence: ``$CLAUDE_PLUGIN_DATA/total-recall`` then
    ``~/.local/share/total-recall``. We do NOT create the directory
    here — :func:`_save_config` handles that lazily.
    """
    base = os.environ.get("CLAUDE_PLUGIN_DATA")
    if base:
        return Path(base).expanduser() / "total-recall"
    return Path("~/.local/share/total-recall").expanduser()


def _config_path() -> Path:
    return _config_dir() / "sources.json"


def _default_config() -> dict[str, list[str]]:
    """All known sources enabled; nothing disabled."""
    return {"enabled": list(KNOWN_SOURCES), "disabled": []}


def _load_config() -> dict[str, list[str]]:
    path = _config_path()
    if not path.exists():
        return _default_config()
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _default_config()
    if not isinstance(raw, dict):
        return _default_config()
    enabled = raw.get("enabled")
    disabled = raw.get("disabled")
    if not isinstance(enabled, list):
        enabled = list(KNOWN_SOURCES)
    if not isinstance(disabled, list):
        disabled = []
    # Coerce to str, dedup, preserve order.
    enabled = list(dict.fromkeys(str(x) for x in enabled))
    disabled = list(dict.fromkeys(str(x) for x in disabled))
    return {"enabled": enabled, "disabled": disabled}


def _save_config(cfg: dict[str, list[str]]) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp.replace(path)
    return path


def _is_enabled(name: str, cfg: dict[str, list[str]]) -> bool:
    """A source is enabled unless it appears in the disabled list.

    Membership in ``enabled`` is informational. Absence from both lists
    means enabled-by-default (this lets new adapters light up without
    a config migration).
    """
    return name not in cfg.get("disabled", [])


# --------------------------------------------------------------------------- #
# Source-registry access (defensive)
# --------------------------------------------------------------------------- #


def _load_sources() -> dict[str, Any]:
    """Return ``{name: SessionSource_instance_or_None}`` for every known name.

    Triggers import of ``lib.sources`` so adapters can self-register.
    Sources that fail to import or instantiate map to ``None`` — callers
    treat that as "adapter not available, can't probe is_available".
    """
    out: dict[str, Any] = {name: None for name in KNOWN_SOURCES}
    try:
        import lib.sources as _sources  # type: ignore[import-not-found]
    except Exception:
        return out

    # Trigger registration for every shipped adapter, even ones the
    # package __init__ hasn't gotten around to importing yet.
    for modname in (
        "lib.sources.claude_code",
        "lib.sources.opencode",
        "lib.sources.gemini_cli",
        "lib.sources.codex",
        "lib.sources.cursor",
        "lib.sources.continue_dev",
        "lib.sources.aider",
        "lib.sources.cline",
    ):
        try:
            __import__(modname)
        except Exception:
            continue

    try:
        registry = _sources.SOURCES  # type: ignore[attr-defined]
    except Exception:
        return out

    for cls in registry:
        try:
            name = getattr(cls, "name", None)
            if not name or name not in out:
                continue
            out[name] = cls()
        except Exception:
            # Bad adapter — leave entry as None.
            continue
    return out


def _is_available(src: Any) -> bool | None:
    """Call ``src.is_available()`` defensively. Returns ``None`` if it raises
    or if ``src`` is ``None``."""
    if src is None:
        return None
    try:
        return bool(src.is_available())
    except Exception:
        return None


def _count_sessions(src: Any, limit: int | None = None) -> int | None:
    """Count sessions a source discovers. ``None`` on error.

    ``limit`` short-circuits after that many (defensive against an
    adapter that discovers tens of thousands of sessions and we just
    want a non-zero signal).
    """
    if src is None:
        return None
    try:
        n = 0
        for _ in src.discover_sessions():
            n += 1
            if limit is not None and n >= limit:
                break
        return n
    except Exception:
        return None


def _row(name: str, cfg: dict[str, list[str]], src: Any) -> dict[str, Any]:
    avail = _is_available(src)
    return {
        "name": name,
        "enabled": _is_enabled(name, cfg),
        "registered": src is not None,
        "is_available": avail if avail is not None else False,
        "available_known": avail is not None,
    }


# --------------------------------------------------------------------------- #
# Click group + subcommands
# --------------------------------------------------------------------------- #


@click.group(
    name="sources",
    help=(
        "Manage which CLI clients (Claude Code, OpenCode, Codex, ...) "
        "total-recall ingests sessions from."
    ),
)
def sources_cmd() -> None:
    """Top-level group; subcommands do the work."""


@sources_cmd.command("list", help="Show every known source plus its status.")
@click.pass_context
def sources_list(ctx: click.Context) -> None:
    cfg = _load_config()
    registry = _load_sources()
    rows = [_row(name, cfg, registry.get(name)) for name in KNOWN_SOURCES]

    if bool(ctx.obj and ctx.obj.get("json")):
        click.echo(format_json({"config_path": str(_config_path()), "sources": rows}))
        return

    click.echo(f"config: {_config_path()}")
    table_rows = [
        {
            "name": r["name"],
            "enabled": "yes" if r["enabled"] else "no",
            "registered": "yes" if r["registered"] else "no",
            "available": (
                "yes" if r["is_available"] else ("no" if r["available_known"] else "?")
            ),
        }
        for r in rows
    ]
    click.echo(
        format_table(
            table_rows, headers=["name", "enabled", "registered", "available"]
        )
    )


@sources_cmd.command(
    "detect", help="Scan and report only sources that have detectable data."
)
@click.pass_context
def sources_detect(ctx: click.Context) -> None:
    cfg = _load_config()
    registry = _load_sources()
    found = []
    for name in KNOWN_SOURCES:
        src = registry.get(name)
        avail = _is_available(src)
        if avail:
            found.append(
                {
                    "name": name,
                    "enabled": _is_enabled(name, cfg),
                    "is_available": True,
                }
            )

    if bool(ctx.obj and ctx.obj.get("json")):
        click.echo(format_json({"detected": found}))
        return

    if not found:
        click.echo("No sources with detectable data on this machine.")
        return
    click.echo(f"Detected {len(found)} source(s) with on-disk data:")
    for r in found:
        marker = "" if r["enabled"] else "  (disabled in config)"
        click.echo(f"  - {r['name']}{marker}")


@sources_cmd.command("enable", help="Mark a source as enabled.")
@click.argument("name")
@click.pass_context
def sources_enable(ctx: click.Context, name: str) -> None:
    _toggle(ctx, name, enable=True)


@sources_cmd.command("disable", help="Mark a source as disabled.")
@click.argument("name")
@click.pass_context
def sources_disable(ctx: click.Context, name: str) -> None:
    _toggle(ctx, name, enable=False)


def _toggle(ctx: click.Context, name: str, *, enable: bool) -> None:
    if name not in KNOWN_SOURCES:
        msg = (
            f"Unknown source {name!r}. Known: {', '.join(KNOWN_SOURCES)}."
        )
        if bool(ctx.obj and ctx.obj.get("json")):
            click.echo(format_json({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        raise click.exceptions.Exit(2)

    cfg = _load_config()
    enabled = [n for n in cfg.get("enabled", []) if n != name]
    disabled = [n for n in cfg.get("disabled", []) if n != name]
    if enable:
        enabled.append(name)
    else:
        disabled.append(name)
    new_cfg = {"enabled": enabled, "disabled": disabled}
    path = _save_config(new_cfg)

    payload = {
        "ok": True,
        "name": name,
        "enabled": enable,
        "config_path": str(path),
    }
    if bool(ctx.obj and ctx.obj.get("json")):
        click.echo(format_json(payload))
    else:
        verb = "enabled" if enable else "disabled"
        click.echo(f"{name}: {verb} (config: {path})")


@sources_cmd.command(
    "test",
    help="Probe one source: load adapter, count sessions, print availability.",
)
@click.argument("name")
@click.option(
    "--max-count",
    type=int,
    default=10000,
    show_default=True,
    help="Short-circuit session counting after this many.",
)
@click.pass_context
def sources_test(ctx: click.Context, name: str, max_count: int) -> None:
    if name not in KNOWN_SOURCES:
        msg = f"Unknown source {name!r}. Known: {', '.join(KNOWN_SOURCES)}."
        if bool(ctx.obj and ctx.obj.get("json")):
            click.echo(format_json({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        raise click.exceptions.Exit(2)

    registry = _load_sources()
    src = registry.get(name)
    available = _is_available(src)
    count = _count_sessions(src, limit=max_count) if available else (
        0 if available is False else None
    )

    payload = {
        "ok": True,
        "name": name,
        "registered": src is not None,
        "is_available": available if available is not None else False,
        "available_known": available is not None,
        "session_count": count,
    }
    if bool(ctx.obj and ctx.obj.get("json")):
        click.echo(format_json(payload))
        return

    if src is None:
        click.echo(f"{name}: adapter not registered (import failed or not built yet).")
        return
    if available is None:
        click.echo(f"{name}: registered, but is_available() raised.")
        return
    if not available:
        click.echo(f"{name}: registered, no on-disk data detected.")
        return
    click.echo(f"{name}: available — {count} session(s) discovered.")


@sources_cmd.command(
    "verify",
    help="Round-trip probe EVERY known source (adapter load + availability + "
    "session count) in one report. Triage when cross-CLI data is missing.",
)
@click.option(
    "--max-count",
    type=int,
    default=10000,
    show_default=True,
    help="Short-circuit per-source session counting after this many.",
)
@click.pass_context
def sources_verify(ctx: click.Context, max_count: int) -> None:
    """The all-sources counterpart to ``sources test``.

    ``list`` shows config/registration, ``detect`` shows only sources with
    data; ``verify`` runs the full discover round-trip for every known source
    and reports registered / available / session_count together — the single
    command to answer "why is source X not showing up in recall?".
    """
    cfg = _load_config()
    registry = _load_sources()
    rows: list[dict[str, Any]] = []
    for name in KNOWN_SOURCES:
        src = registry.get(name)
        available = _is_available(src)
        count = (
            _count_sessions(src, limit=max_count)
            if available
            else (0 if available is False else None)
        )
        rows.append(
            {
                "name": name,
                "enabled": _is_enabled(name, cfg),
                "registered": src is not None,
                "is_available": available if available is not None else False,
                "available_known": available is not None,
                "session_count": count,
            }
        )

    if bool(ctx.obj and ctx.obj.get("json")):
        click.echo(format_json({"config_path": str(_config_path()), "sources": rows}))
        return

    click.echo(f"config: {_config_path()}")
    table_rows = [
        {
            "name": r["name"],
            "enabled": "yes" if r["enabled"] else "no",
            "registered": "yes" if r["registered"] else "no",
            "available": (
                "yes" if r["is_available"] else ("no" if r["available_known"] else "?")
            ),
            "sessions": ("?" if r["session_count"] is None else str(r["session_count"])),
        }
        for r in rows
    ]
    click.echo(
        format_table(
            table_rows,
            headers=["name", "enabled", "registered", "available", "sessions"],
        )
    )


__all__ = ["sources_cmd"]
