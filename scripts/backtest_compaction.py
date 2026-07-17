#!/usr/bin/env python3
"""Backtest harness for the compaction continuation packet.

Replays every *real* historical compaction boundary in the corpus and asks:
**would the continuation packet have helped the model pick up where it left
off, beyond what the native compact summary already gave it?**

For each ``type=system, subtype=compact_boundary`` record we:

1. Build ``PROPOSED`` = :func:`build_continuation_packet` from the records
   *before* the boundary (``boundary_idx``), index queries time-guarded.
2. Read ``BASELINE`` = the native summary text — the first ``isCompactSummary``
   user record right after the boundary.
3. Mine ``GOLD`` from the post-boundary window ``(idx, idx+post_window]``
   (stopping at the next boundary): the file paths the model actually touched,
   Bash command heads it actually ran, and the first real post-boundary user
   text. This is the "future" the recovered context should have prepared for.

Metrics per case (see module-level ``METRICS`` docstring):

* ``file_coverage@k`` for k∈{5,10,25}: of the first-k distinct GOLD files, the
  fraction present (substring match) in BASELINE alone vs. BASELINE+PROPOSED.
  The **marginal lift** is ``(combined) − (baseline)`` — the packet's value-add.
* ``goal_alignment``: word-overlap between the packet's
  ``active_goal``+``last_user_directive`` and the first post-boundary
  user/assistant text (does the recovered intent match where work resumed?).
* ``rediscovery`` / ``packet_could_prevent``: post-boundary Read/Bash whose
  target already appeared in the pre-boundary tail — work the model *redid*
  after forgetting — and how many of those targets the packet already carried
  (i.e. could have prevented the rediscovery).
* ``packet_chars``.

Outputs to ``--out``: per-case JSONL, an aggregate JSON (means + win/loss vs
baseline), and a human-readable markdown summary.

Pure stdlib. Importable (for unit tests) and runnable as a CLI via the venv
python.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Make the repo root importable when run as a script from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from extractors.continuation_packet import (  # noqa: E402
    _is_real_user,
    _iter_tool_uses,
    _text_of,
    build_continuation_packet,
)

_WORD_RE = re.compile(r"[A-Za-z0-9_./-]+")
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "are",
    "this",
    "that",
    "it",
    "with",
    "as",
    "be",
    "at",
    "by",
    "we",
    "i",
    "you",
    "do",
    "did",
    "so",
    "then",
    "now",
    "next",
    "let",
    "let's",
    "ok",
    "okay",
}
# Path-ish file tokens: must contain a slash or a dotted extension.
_PATHLIKE_RE = re.compile(r"[\w./-]*[/.][\w./-]+")
_READ_TOOLS = {"Read", "Bash", "NotebookEdit", "Grep", "Glob"}


# ---------------------------------------------------------------------------
# Boundary enumeration
# ---------------------------------------------------------------------------


def enumerate_boundaries(transcript_path: str) -> list[dict]:
    """Return one descriptor per ``compact_boundary`` record in a transcript.

    Each descriptor: ``{idx, session_id, cwd, ts}`` where ``idx`` is the
    physical line number of the boundary record.
    """
    out: list[dict] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                out.append(
                    {
                        "idx": i,
                        "session_id": rec.get("sessionId"),
                        "cwd": rec.get("cwd"),
                        "ts": rec.get("timestamp"),
                    }
                )
    return out


def _all_records(transcript_path: str) -> list[dict]:
    recs: list[dict] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(rec, dict):
                recs.append(rec)
    return recs


# ---------------------------------------------------------------------------
# BASELINE: native compact summary text
# ---------------------------------------------------------------------------


def native_summary_after(records: list[dict], boundary_pos: int) -> str:
    """First ``isCompactSummary`` record's text at/after ``boundary_pos``.

    ``boundary_pos`` is an index into the *parsed* ``records`` list (not the
    physical line). Returns "" when none is found before the next boundary.
    """
    for rec in records[boundary_pos + 1 :]:
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            break
        if rec.get("isCompactSummary"):
            txt = _text_of(rec)
            if txt:
                return txt
    return ""


# ---------------------------------------------------------------------------
# GOLD: what the model actually did post-boundary
# ---------------------------------------------------------------------------


def gold_after(records: list[dict], boundary_pos: int, post_window: int) -> dict:
    """Mine the post-boundary window for the "future" the model walked into.

    Returns ``{files: [...ordered distinct...], bash_heads: [...],
    first_user_text: str|None}`` from records ``(boundary_pos,
    boundary_pos+post_window]``, stopping early at the next boundary.
    """
    files: list[str] = []
    seen_files: set[str] = set()
    bash_heads: list[str] = []
    first_user_text: str | None = None

    end = min(len(records), boundary_pos + 1 + post_window)
    for rec in records[boundary_pos + 1 : end]:
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            break
        if first_user_text is None and _is_real_user(rec):
            first_user_text = _text_of(rec)
        for tu in _iter_tool_uses(rec):
            name = tu.get("name")
            inp = tu.get("input") or {}
            if not isinstance(inp, dict):
                continue
            for k in ("file_path", "path", "notebook_path"):
                v = inp.get(k)
                if isinstance(v, str) and v.strip():
                    p = v.strip()
                    if p not in seen_files:
                        seen_files.add(p)
                        files.append(p)
            if name == "Bash":
                cmd = inp.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    bash_heads.append(cmd.strip()[:80])
                    # Pull path-like tokens out of the command as GOLD files.
                    for tok in _PATHLIKE_RE.findall(cmd):
                        if ("/" in tok or "." in tok) and len(tok) > 3 and tok not in seen_files:
                            seen_files.add(tok)
                            files.append(tok)
    return {
        "files": files,
        "bash_heads": bash_heads,
        "first_user_text": first_user_text,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _content_words(text: str | None) -> set[str]:
    if not text:
        return set()
    return {w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOP and len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def file_coverage(gold_files: list[str], haystack: str, k: int) -> float:
    """Fraction of the first-k distinct GOLD files present in ``haystack``.

    Substring match: a GOLD path is "covered" if it (or its basename) appears
    literally in the text blob.
    """
    topk = gold_files[:k]
    if not topk:
        return 0.0
    hit = 0
    for p in topk:
        base = os.path.basename(p) or p
        if p in haystack or (len(base) > 3 and base in haystack):
            hit += 1
    return hit / len(topk)


def _packet_text(packet: dict) -> str:
    """Flatten the packet to a searchable text blob."""
    return json.dumps(packet, ensure_ascii=False)


def _pre_tail_targets(records: list[dict], boundary_pos: int, n: int = 400) -> set[str]:
    """Distinct file paths + Bash command heads in the last ``n`` pre-records."""
    start = max(0, boundary_pos - n)
    targets: set[str] = set()
    for rec in records[start:boundary_pos]:
        for tu in _iter_tool_uses(rec):
            name = tu.get("name")
            inp = tu.get("input") or {}
            if not isinstance(inp, dict):
                continue
            for k in ("file_path", "path", "notebook_path"):
                v = inp.get(k)
                if isinstance(v, str) and v.strip():
                    targets.add(v.strip())
            if name == "Bash":
                cmd = inp.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    targets.add(cmd.strip()[:80])
    return targets


def rediscovery_metrics(
    records: list[dict], boundary_pos: int, post_window: int, packet: dict
) -> dict:
    """Count post-boundary Read/Bash targets that were already seen pre-boundary.

    ``rediscovery`` = how many such repeated targets exist (work the model
    redid). ``packet_could_prevent`` = how many of those repeated targets the
    PROPOSED packet already contains (substring) — i.e. the packet would have
    spared the model the round-trip.
    """
    pre = _pre_tail_targets(records, boundary_pos)
    blob = _packet_text(packet)
    end = min(len(records), boundary_pos + 1 + post_window)
    rediscovery = 0
    could_prevent = 0
    for rec in records[boundary_pos + 1 : end]:
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            break
        for tu in _iter_tool_uses(rec):
            name = tu.get("name")
            if name not in _READ_TOOLS:
                continue
            inp = tu.get("input") or {}
            if not isinstance(inp, dict):
                continue
            target = None
            if name == "Bash":
                cmd = inp.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    target = cmd.strip()[:80]
            else:
                for k in ("file_path", "path", "notebook_path", "pattern"):
                    v = inp.get(k)
                    if isinstance(v, str) and v.strip():
                        target = v.strip()
                        break
            if not target:
                continue
            if target in pre:
                rediscovery += 1
                base = os.path.basename(target) or target
                if target in blob or (len(base) > 3 and base in blob):
                    could_prevent += 1
    return {"rediscovery": rediscovery, "packet_could_prevent": could_prevent}


def score_case(
    records: list[dict],
    boundary_pos: int,
    boundary_line_idx: int,
    transcript_path: str,
    session_id: str | None,
    cwd: str | None,
    db_path: str | None,
    post_window: int,
    max_chars: int = 2000,
) -> dict:
    """Compute the full metric bundle for one boundary."""
    packet = build_continuation_packet(
        transcript_path,
        session_id,
        cwd,
        db_path=db_path,
        boundary_idx=boundary_line_idx,
        max_chars=max_chars,
    )
    baseline = native_summary_after(records, boundary_pos)
    gold = gold_after(records, boundary_pos, post_window)

    packet_blob = _packet_text(packet)
    combined = baseline + "\n" + packet_blob

    cov: dict[str, Any] = {}
    for k in (5, 10, 25):
        b = file_coverage(gold["files"], baseline, k)
        c = file_coverage(gold["files"], combined, k)
        cov[f"k{k}"] = {
            "baseline": round(b, 4),
            "combined": round(c, 4),
            "lift": round(c - b, 4),
        }

    # goal_alignment: packet intent vs. where work actually resumed.
    intent = " ".join(str(packet.get(f, "")) for f in ("active_goal", "last_user_directive"))
    resumed = gold.get("first_user_text") or baseline[:400]
    goal_alignment = round(_jaccard(_content_words(intent), _content_words(resumed)), 4)

    redisc = rediscovery_metrics(records, boundary_pos, post_window, packet)

    return {
        "transcript": transcript_path,
        "boundary_line_idx": boundary_line_idx,
        "session_id": session_id,
        "cwd": cwd,
        "ts": records[boundary_pos].get("timestamp") if boundary_pos < len(records) else None,
        "n_gold_files": len(gold["files"]),
        "file_coverage": cov,
        "goal_alignment": goal_alignment,
        "rediscovery": redisc["rediscovery"],
        "packet_could_prevent": redisc["packet_could_prevent"],
        "packet_chars": len(packet_blob),
        "baseline_chars": len(baseline),
        "packet_fields": sorted(k for k in packet if not k.startswith("_")),
    }


# ---------------------------------------------------------------------------
# Aggregation + report
# ---------------------------------------------------------------------------


def _boundary_positions(records: list[dict]) -> list[int]:
    return [
        i
        for i, r in enumerate(records)
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
    ]


def run_backtest(
    projects_root: str,
    db_path: str | None,
    out_dir: str,
    limit: int | None,
    post_window: int,
) -> dict:
    """Enumerate every boundary in the corpus, score it, write outputs."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cases_path = out / "cases.jsonl"
    agg_path = out / "aggregate.json"
    md_path = out / "summary.md"

    cases: list[dict] = []
    n_boundaries = 0
    root = Path(projects_root).expanduser()
    files = sorted(root.glob("*/*.jsonl"))

    with cases_path.open("w", encoding="utf-8") as cf:
        for f in files:
            try:
                records = _all_records(str(f))
            except OSError:
                continue
            positions = _boundary_positions(records)
            if not positions:
                continue
            # Map parsed-position → physical line idx via re-scan: the builder
            # needs the *physical* line index, but our records list dropped
            # blank/garbage lines. Recover line idx by re-enumerating.
            line_descriptors = enumerate_boundaries(str(f))
            # positions and line_descriptors are in the same order.
            for pos, desc in zip(positions, line_descriptors, strict=False):
                n_boundaries += 1
                if limit is not None and len(cases) >= limit:
                    break
                try:
                    case = score_case(
                        records,
                        pos,
                        desc["idx"],
                        str(f),
                        desc.get("session_id"),
                        desc.get("cwd"),
                        db_path,
                        post_window,
                    )
                except Exception as e:  # noqa: BLE001
                    case = {"transcript": str(f), "boundary_line_idx": desc["idx"], "error": str(e)}
                cases.append(case)
                cf.write(json.dumps(case, ensure_ascii=False) + "\n")
            if limit is not None and len(cases) >= limit:
                break

    agg = _aggregate(cases)
    agg["n_boundaries_seen"] = n_boundaries
    agg["n_cases_scored"] = len(cases)
    agg["post_window"] = post_window
    agg["db_path"] = db_path
    agg_path.write_text(json.dumps(agg, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_render_markdown(agg, cases), encoding="utf-8")
    return agg


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _aggregate(cases: list[dict]) -> dict:
    scored = [c for c in cases if "file_coverage" in c]
    agg: dict[str, Any] = {"file_coverage": {}}
    for k in (5, 10, 25):
        b = [c["file_coverage"][f"k{k}"]["baseline"] for c in scored]
        co = [c["file_coverage"][f"k{k}"]["combined"] for c in scored]
        lift = [c["file_coverage"][f"k{k}"]["lift"] for c in scored]
        wins = sum(1 for x in lift if x > 0)
        losses = sum(1 for x in lift if x < 0)
        agg["file_coverage"][f"k{k}"] = {
            "baseline_mean": _mean(b),
            "combined_mean": _mean(co),
            "lift_mean": _mean(lift),
            "wins": wins,
            "losses": losses,
            "ties": len(lift) - wins - losses,
        }
    agg["goal_alignment_mean"] = _mean([c.get("goal_alignment") for c in scored])
    agg["rediscovery_total"] = sum(c.get("rediscovery", 0) for c in scored)
    agg["packet_could_prevent_total"] = sum(c.get("packet_could_prevent", 0) for c in scored)
    prevent_rate = (
        agg["packet_could_prevent_total"] / agg["rediscovery_total"]
        if agg["rediscovery_total"]
        else 0.0
    )
    agg["packet_prevent_rate"] = round(prevent_rate, 4)
    agg["packet_chars_mean"] = _mean([c.get("packet_chars") for c in scored])
    agg["baseline_chars_mean"] = _mean([c.get("baseline_chars") for c in scored])
    return agg


def _render_markdown(agg: dict, cases: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Compaction continuation-packet backtest")
    lines.append("")
    lines.append(
        f"Scored **{agg.get('n_cases_scored', 0)}** of "
        f"**{agg.get('n_boundaries_seen', 0)}** real compaction boundaries "
        f"(post-window = {agg.get('post_window')})."
    )
    lines.append("")
    lines.append("## File coverage: BASELINE vs BASELINE+PROPOSED")
    lines.append("")
    lines.append("| k | baseline | combined | mean lift | wins | losses | ties |")
    lines.append("|---|----------|----------|-----------|------|--------|------|")
    for k in (5, 10, 25):
        c = agg["file_coverage"][f"k{k}"]
        lines.append(
            f"| {k} | {c['baseline_mean']:.3f} | {c['combined_mean']:.3f} | "
            f"{c['lift_mean']:+.3f} | {c['wins']} | {c['losses']} | {c['ties']} |"
        )
    lines.append("")
    lines.append("## Continuation signal")
    lines.append("")
    lines.append(
        "- **goal_alignment** (mean Jaccard, packet intent vs resumed work): "
        f"{agg.get('goal_alignment_mean')}"
    )
    lines.append(
        f"- **rediscovery** (post-boundary re-reads of pre-boundary targets): "
        f"{agg.get('rediscovery_total')}"
    )
    lines.append(
        f"- **packet_could_prevent**: {agg.get('packet_could_prevent_total')} "
        f"({agg.get('packet_prevent_rate'):.1%} of rediscoveries already in the packet)"
    )
    lines.append(
        f"- **packet size** (mean chars): {agg.get('packet_chars_mean')} "
        f"vs native summary {agg.get('baseline_chars_mean')}"
    )
    lines.append("")
    lines.append("## Top cases by k10 lift")
    lines.append("")
    scored = [c for c in cases if "file_coverage" in c]
    scored.sort(key=lambda c: c["file_coverage"]["k10"]["lift"], reverse=True)
    lines.append("| lift@10 | gold files | could_prevent/redisc | chars | cwd |")
    lines.append("|---------|-----------|----------------------|-------|-----|")
    for c in scored[:10]:
        lift = c["file_coverage"]["k10"]["lift"]
        lines.append(
            f"| {lift:+.3f} | {c['n_gold_files']} | "
            f"{c['packet_could_prevent']}/{c['rediscovery']} | "
            f"{c['packet_chars']} | {os.path.basename((c.get('cwd') or '').rstrip('/')) or '?'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--projects-root",
        default=str(Path("~/.claude/projects").expanduser()),
        help="Root of the Claude Code projects corpus (read-only).",
    )
    ap.add_argument("--db", default=None, help="Read-only index DB path (optional).")
    ap.add_argument("--out", required=True, help="Output directory.")
    ap.add_argument("--limit", type=int, default=None, help="Max boundaries to score.")
    ap.add_argument("--post-window", type=int, default=200, help="Post-boundary GOLD window size.")
    args = ap.parse_args(argv)

    agg = run_backtest(
        projects_root=args.projects_root,
        db_path=args.db,
        out_dir=args.out,
        limit=args.limit,
        post_window=args.post_window,
    )
    print(json.dumps(agg, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
