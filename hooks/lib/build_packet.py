#!/usr/bin/env python3
"""Build a continuation packet for compaction recovery and print it as JSON.

Thin CLI wrapper around
``extractors.continuation_packet.build_continuation_packet`` so the bash
``PreCompact`` hook can invoke it the same way it invokes the other inline
python snippets. Mirrors the defensive ``sys.path`` bootstrap used by
``decide_and_format.py`` / ``query.py``.

Prints the packet JSON (compact) to stdout. Empty stdout means "nothing
useful to seed". Exit code is always 0 — a buggy builder must never block
compaction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _add_repo_root_to_syspath() -> None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "extractors" / "continuation_packet.py").exists():
            if str(parent) not in sys.path:
                sys.path.append(str(parent))
            return
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and root not in sys.path:
        sys.path.append(root)


_add_repo_root_to_syspath()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="build_packet")
    p.add_argument("--transcript", required=True)
    p.add_argument("--session", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--db", default="")
    p.add_argument("--max-chars", type=int, default=2000)
    args = p.parse_args(argv)

    try:
        from extractors.continuation_packet import build_continuation_packet
    except Exception:
        return 0

    if not args.transcript or not os.path.exists(args.transcript):
        return 0

    db_path = args.db if (args.db and os.path.exists(args.db)) else None
    try:
        packet = build_continuation_packet(
            transcript_path=args.transcript,
            session_id=args.session or None,
            cwd=args.cwd or None,
            db_path=db_path,
            max_chars=args.max_chars,
        )
    except Exception:
        return 0

    # A packet with only the _kind tag carries no useful state.
    if not isinstance(packet, dict) or set(packet) <= {"_kind"}:
        return 0

    try:
        sys.stdout.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
