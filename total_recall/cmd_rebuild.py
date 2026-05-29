"""``total-recall rebuild`` — DROP + recreate the index, then full ingest.

Destructive. Always prompts unless ``--yes``. Useful after a schema bump or
when an extractor's output format changes (the UNIQUE(kind, source_uuid)
constraint stops idempotent re-extraction from picking up the new content
otherwise).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from .util import resolve_db_path


@click.command(help="DESTRUCTIVE: drop the index, recreate the schema, then full ingest.")
@click.option("--yes", is_flag=True, help="Skip the y/N prompt.")
@click.option(
    "--projects-root",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Override the ~/.claude/projects root.",
)
@click.option(
    "--keep-file",
    is_flag=True,
    help="Drop tables instead of deleting the DB file. Preserves perms / inode.",
)
@click.pass_context
def rebuild_cmd(
    ctx: click.Context,
    yes: bool,
    projects_root: str | None,
    keep_file: bool,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    verbose = bool(ctx.obj.get("verbose"))

    if not yes:
        click.echo(f"This will WIPE the total-recall index at: {db_path}")
        click.confirm("Continue?", abort=True, default=False)

    try:
        from index.db import apply_schema, connect  # type: ignore[import-not-found]
        from index.ingest import ingest_all  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    if keep_file and db_path.exists():
        if verbose:
            click.echo(f"[rebuild] dropping tables in {db_path}", err=True)
        conn = connect(db_path)
        try:
            _drop_all_tables(conn)
            apply_schema(conn)
        finally:
            conn.close()
    else:
        # Also remove WAL/SHM siblings so we don't carry stale state.
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                if verbose:
                    click.echo(f"[rebuild] unlink {p}", err=True)
                try:
                    p.unlink()
                except OSError as exc:
                    click.echo(f"total-recall: failed to remove {p}: {exc}", err=True)
                    raise click.Abort from exc
        # Recreate schema by opening a fresh connection.
        connect(db_path).close()

    if verbose:
        click.echo(f"[rebuild] full ingest into {db_path}", err=True)
    started = time.monotonic()
    result = ingest_all(
        db_path=db_path,
        force_full=True,
        cwd_filter=None,
        projects_root=Path(projects_root).expanduser() if projects_root else None,
        dry_run=False,
    )
    # Consolidation pass (cold path). The per-file incremental merge that
    # runs during ingest can freeze an early, non-global winner for
    # frequency-ranked identity scalars (e.g. a handle decided by one file
    # resists later correction via append-supersede). On a full rebuild we
    # have the whole corpus, so re-derive the operator profile in ONE pass
    # over every session and persist the globally-correct values. This is
    # the cold-path reconcile the incremental hot path defers to.
    try:
        from extractors.operator_profile import (  # type: ignore[import-not-found]
            extract_operator_profile,
            persist_profile,
        )

        root = (
            Path(projects_root).expanduser()
            if projects_root
            else Path("~/.claude/projects").expanduser()
        )
        all_jsonl = sorted(root.glob("*/*.jsonl"))
        if all_jsonl:
            full_profile = extract_operator_profile(all_jsonl)
            conn = connect(db_path)
            try:
                persist_profile(conn, full_profile)
                conn.commit()
            finally:
                conn.close()
            if verbose:
                click.echo(
                    f"[rebuild] consolidated operator profile from "
                    f"{len(all_jsonl)} sessions (single full pass)",
                    err=True,
                )
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail the rebuild
        click.echo(f"total-recall: profile consolidation skipped ({exc})", err=True)

    # Consolidation for the additional aggregated profiles. Same rationale —
    # per-file incremental can drift; the full-corpus pass is authoritative.
    # Each is wrapped independently so one failure doesn't block the others.
    root = (
        Path(projects_root).expanduser()
        if projects_root
        else Path("~/.claude/projects").expanduser()
    )
    all_jsonl = sorted(root.glob("*/*.jsonl"))

    if all_jsonl:
        # Workflow profile.
        try:
            from extractors.workflow import extract_workflow
            from index.workflow import persist_workflow
            wf = extract_workflow(all_jsonl)
            conn = connect(db_path)
            try:
                persist_workflow(conn, wf)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"total-recall: workflow consolidation skipped ({exc})", err=True)

        # Implicit preferences.
        try:
            from extractors.implicit_preferences import extract_implicit_preferences
            from index.implicit_preferences import persist_implicit_preferences
            ip = extract_implicit_preferences(all_jsonl)
            conn = connect(db_path)
            try:
                persist_implicit_preferences(conn, ip)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"total-recall: implicit_preferences consolidation skipped ({exc})", err=True)

        # Satisfaction profile.
        try:
            from extractors.satisfaction import extract_satisfaction
            from index.satisfaction import persist_satisfaction
            sat = extract_satisfaction(all_jsonl)
            conn = connect(db_path)
            try:
                persist_satisfaction(conn, sat)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"total-recall: satisfaction consolidation skipped ({exc})", err=True)

        # Ontology — re-derive projects + co-mention graph + vocabulary from
        # the full corpus so related_projects gets populated authoritatively.
        try:
            from extractors.ontology import extract_ontology, persist_ontology
            snap = extract_ontology(root)
            conn = connect(db_path)
            try:
                persist_ontology(conn, snap)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"total-recall: ontology consolidation skipped ({exc})", err=True)

    # Optional local-LLM refinement (cold path, opt-in via env).
    # Heuristics produce the authoritative baseline; the LLM only REFINES
    # specific weak fields (machines NER, vocabulary definitions, project
    # narratives). Skipped silently if ollama isn't running or the model
    # isn't pulled — heuristic results remain canonical.
    try:
        from extractors.llm.client import get_default_client  # type: ignore[import-not-found]
        _llm = get_default_client()
        if _llm.available:
            if verbose:
                click.echo(
                    f"[rebuild] LLM refinement enabled"
                    f" (provider={_llm.provider}, model={_llm.model})",
                    err=True,
                )

            # Machines refinement
            try:
                from extractors.llm.refine_machines import refine_machines  # type: ignore[import-not-found]
                from index.operator import get_profile, upsert_profile_field  # type: ignore[import-not-found]
                conn = connect(db_path)
                try:
                    prof = get_profile(conn)
                    machines = prof.get("machines") or {}
                    email = prof.get("email_primary") or None

                    # Build per-host context snippets so refine_machines can judge
                    # bare hostnames against real corpus evidence rather than thin
                    # heuristic metadata. Mirrors the _snippets_for_term pattern
                    # used below for vocabulary grounding.
                    sample_contexts: dict[str, list[str]] | None = None
                    try:
                        _host_ctx: dict[str, list[str]] = {}
                        for _host in machines:
                            _snips: list[str] = []
                            try:
                                _rows = conn.execute(
                                    "SELECT m.text FROM messages_fts f "
                                    "JOIN messages m ON m.id = f.rowid "
                                    "WHERE f.text MATCH ? "
                                    "AND m.role IN ('user', 'assistant') "
                                    "ORDER BY length(m.text) ASC LIMIT 4",
                                    (_host,),
                                ).fetchall()
                                for (_txt,) in _rows:
                                    if len(_snips) >= 2:
                                        break
                                    _s = (_txt or "").strip().replace("\n", " ")
                                    # Skip command-output-heavy or near-empty lines.
                                    if _s.count("\t") > 2 or len(_s) < 20:
                                        continue
                                    _snips.append(_s[:200])
                            except Exception:  # noqa: BLE001
                                pass
                            _host_ctx[_host] = _snips
                        sample_contexts = _host_ctx
                    except Exception:  # noqa: BLE001
                        sample_contexts = None

                    refined = refine_machines(
                        machines,
                        operator_email=email,
                        client=_llm,
                        sample_contexts=sample_contexts,
                    )
                    if refined is not None and refined != machines:
                        upsert_profile_field(
                            conn,
                            key="machines",
                            value=refined,
                            confidence=prof.get("_confidence", {}).get("machines", 0.5),
                            sources=prof.get("_sources", {}).get("machines", []),
                        )
                        conn.commit()
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                click.echo(f"total-recall: machines LLM refinement skipped ({exc})", err=True)

            # Vocabulary definitions + project narratives refinement.
            #
            # WHY gated: small CPU models (e.g. gemma4:e2b, 2.3B default) do
            # classification near-perfectly (machines keep/drop: measured P/R
            # 1.0/1.0) but are weak at GENERATION (definitions, narratives).
            # As of v0.9.5 the anti-echo detector + few-shot prompts eliminate
            # the verbatim-parroting failure (measured echo_rate 0.0 on e2b) —
            # output is now clean defs or null, never garbage — but coverage is
            # sparse on a 2B model (~20% of definable terms get a real def; the
            # rest correctly return null).  Text-gen is therefore SAFE but
            # LOW-YIELD on small models, so it stays opt-in for the latency
            # cost; a larger model (>=7B) raises coverage.  Machines refinement
            # above is classification-only and stays always-on.
            _refine_text_raw = os.environ.get("TOTAL_RECALL_LLM_REFINE_TEXT", "").strip().lower()
            _refine_text = _refine_text_raw in ("1", "true", "yes")

            if not _refine_text:
                if verbose:
                    click.echo(
                        "[rebuild] LLM text refinement (vocab/narratives) off"
                        " — clean but low-yield on small models; set"
                        " TOTAL_RECALL_LLM_REFINE_TEXT=1 to enable (a >=7B model"
                        " gives better coverage)",
                        err=True,
                    )
            else:
                try:
                    from extractors.llm.refine_ontology import (  # type: ignore[import-not-found]
                        refine_project_narratives,
                        refine_vocabulary_definitions,
                    )
                    from index.ontology import (  # type: ignore[import-not-found]
                        list_projects,
                        list_terms,
                        upsert_project,
                        upsert_vocabulary_term,
                    )
                    conn = connect(db_path)
                    try:
                        # Helpers: gather grounding snippets from messages FTS so the
                        # LLM has actual operator-corpus context (not its own training
                        # data). Without grounding, the anti-hallucination guard
                        # collapses every output to null.
                        def _snippets_for_term(term: str, limit: int = 3) -> list[str]:
                            try:
                                rows = conn.execute(
                                    "SELECT m.text FROM messages_fts f "
                                    "JOIN messages m ON m.id = f.rowid "
                                    "WHERE f.text MATCH ? AND m.role='user' "
                                    "ORDER BY length(m.text) ASC LIMIT ?",
                                    (term, limit),
                                ).fetchall()
                                out = []
                                for (txt,) in rows:
                                    snippet = (txt or "").strip().replace("\n", " ")
                                    # Reject snippets that look like raw command
                                    # output (high tab density / numeric noise) —
                                    # they mislead the model.
                                    if snippet.count("\t") > 2 or len(snippet) < 20:
                                        continue
                                    out.append(snippet[:300])
                                return out
                            except Exception:  # noqa: BLE001
                                return []

                        def _snippets_for_cwd(cwd: str, limit: int = 3) -> list[str]:
                            try:
                                rows = conn.execute(
                                    "SELECT text FROM messages WHERE cwd=? AND role='user' "
                                    "AND length(text) BETWEEN 50 AND 400 "
                                    "ORDER BY ts ASC LIMIT ?",
                                    (cwd, limit),
                                ).fetchall()
                                return [(t or "").strip().replace("\n", " ")[:300]
                                        for (t,) in rows if t and t.strip()]
                            except Exception:  # noqa: BLE001
                                return []

                        # Vocabulary — top 50 by frequency, with grounding snippets.
                        vocab_rows = list_terms(conn)
                        if vocab_rows:
                            ranked = sorted(
                                vocab_rows,
                                key=lambda r: (r.get("frequency", 0) if isinstance(r, dict)
                                               else getattr(r, "frequency", 0)),
                                reverse=True,
                            )[:50]
                            enriched = []
                            for r in ranked:
                                row = dict(r) if isinstance(r, dict) else {
                                    "term": getattr(r, "term", ""),
                                    "definition": getattr(r, "definition", "") or "",
                                    "category": getattr(r, "category", "concept"),
                                    "frequency": getattr(r, "frequency", 1),
                                }
                                snips = _snippets_for_term(row["term"])
                                row["context_snippet"] = " | ".join(snips) if snips else (row.get("definition") or "")
                                enriched.append(row)
                            refined_vocab = refine_vocabulary_definitions(enriched, client=_llm)
                            for v in refined_vocab:
                                if v.get("definition"):
                                    upsert_vocabulary_term(
                                        conn,
                                        v["term"],
                                        v.get("definition") or "",
                                        category=v.get("category") or None,
                                        frequency=v.get("frequency") or None,
                                    )

                        # Project narratives — top 25 by message_count, with grounding.
                        proj_rows = list_projects(conn)
                        if proj_rows:
                            ranked_p = sorted(
                                proj_rows,
                                key=lambda p: (p.get("message_count", 0) if isinstance(p, dict)
                                               else getattr(p, "message_count", 0)),
                                reverse=True,
                            )[:25]
                            enriched_p = []
                            for p in ranked_p:
                                row = dict(p) if isinstance(p, dict) else {
                                    "cwd": getattr(p, "cwd", ""),
                                    "display_name": getattr(p, "display_name", ""),
                                    "purpose": getattr(p, "purpose", "") or "",
                                    "primary_language": getattr(p, "primary_language", "") or "",
                                    "related_projects": getattr(p, "related_projects", []) or [],
                                }
                                row["context_snippets"] = _snippets_for_cwd(row["cwd"])
                                enriched_p.append(row)
                            refined_projs = refine_project_narratives(enriched_p, client=_llm)
                            for p in refined_projs:
                                if p.get("narrative"):
                                    upsert_project(
                                        conn,
                                        cwd=p["cwd"],
                                        display_name=p.get("display_name"),
                                        purpose=p["narrative"][:300],
                                        primary_language=p.get("primary_language"),
                                    )
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as exc:  # noqa: BLE001
                    click.echo(f"total-recall: ontology LLM refinement skipped ({exc})", err=True)
        elif verbose:
            click.echo(
                "[rebuild] LLM refinement off"
                " (ollama/model absent or TOTAL_RECALL_LLM_PROVIDER=none)",
                err=True,
            )
    except Exception as exc:  # noqa: BLE001
        click.echo(f"total-recall: LLM layer load failed ({exc})", err=True)

    elapsed = time.monotonic() - started

    files = _result_get(result, "files", 0)
    messages = _result_get(result, "messages", 0)
    extractions = _result_get(result, "extractions", 0)

    click.echo(
        f"Rebuilt {db_path}: {files} files, {messages} messages, "
        f"{extractions} extractions, {elapsed:.2f}s."
    )


def _drop_all_tables(conn: object) -> None:
    # Order matters: drop FTS shadow tables first, then triggers/indices, then base.
    statements = [
        "DROP TRIGGER IF EXISTS messages_ai",
        "DROP TRIGGER IF EXISTS messages_ad",
        "DROP TRIGGER IF EXISTS messages_au",
        "DROP TRIGGER IF EXISTS extractions_ai",
        "DROP TRIGGER IF EXISTS extractions_ad",
        "DROP TRIGGER IF EXISTS extractions_au",
        "DROP TABLE IF EXISTS messages_fts",
        "DROP TABLE IF EXISTS extractions_fts",
        "DROP TABLE IF EXISTS extractions",
        "DROP TABLE IF EXISTS messages",
        "DROP TABLE IF EXISTS ingest_state",
        "DROP TABLE IF EXISTS schema_meta",
    ]
    for sql in statements:
        conn.execute(sql)  # type: ignore[attr-defined]


def _result_get(result: object, key: str, default: int) -> int:
    if result is None:
        return default
    if isinstance(result, dict):
        return int(result.get(key, default))
    if isinstance(result, list):
        if key == "files":
            return len(result)
        if key == "messages":
            return sum(int(getattr(r, "new_messages", 0) or 0) for r in result)
        if key == "extractions":
            return sum(int(getattr(r, "new_extractions", 0) or 0) for r in result)
        return default
    val = getattr(result, key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
