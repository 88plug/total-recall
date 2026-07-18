#!/usr/bin/env python3
"""Head-to-head chat/refine bakeoff for total-recall DEFAULT_MODEL candidates.

Runs product JSON micro-tasks + production refine_machines + vocab refine
on GPU (full offload) and CPU (num_gpu=0) for each model.

Usage:
  PYTHONPATH=. python3 scripts/eval_llm_bakeoff.py
  PYTHONPATH=. python3 scripts/eval_llm_bakeoff.py --models qwen3.5:2b,gemma4:e4b-it-qat
  PYTHONPATH=. python3 scripts/eval_llm_bakeoff.py --device gpu   # or cpu, or both

Writes docs/eval-llm-bakeoff.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Production-shaped fixtures (self-contained; no gitignored local fixtures).
_MACHINE_HEURISTICS: dict[str, dict] = {
    "web-01": {"role": "web", "hits": 3},
    "cache-02": {"role": "cache", "hits": 2},
    "Monday": {"role": None, "hits": 1},
    "asyncpg": {"role": None, "hits": 1},
    "hardening": {"role": None, "hits": 1},
    "cloudflare": {"role": None, "hits": 1},
    "db-primary": {"role": "db", "hits": 2},
    "timeouts": {"role": None, "hits": 1},
}
_MACHINE_CONTEXTS: dict[str, list[str]] = {
    "web-01": ["ssh web-01 after cert rotation", "nginx on web-01 restarted"],
    "cache-02": ["redis on cache-02", "cache-02 is the redis host"],
    "Monday": ["see you Monday", "Monday standup"],
    "asyncpg": ["switched driver to asyncpg", "asyncpg for postgres"],
    "hardening": ["security hardening pass", "hardening checklist"],
    "cloudflare": ["cloudflare cdn in front", "cloudflare proxy"],
    "db-primary": ["pg on db-primary", "db-primary is the primary"],
    "timeouts": ["connection timeouts increased", "timeouts are too short"],
}
_TRUE_HOSTS = {"web-01", "cache-02", "db-primary"}

_VOCAB_TERMS: list[dict] = [
    {
        "term": "harness",
        "frequency": 4,
        "context_snippet": (
            "The harness loads MCP servers and skills for Claude Code / Grok sessions."
        ),
    },
    {
        "term": "project_key",
        "frequency": 3,
        "context_snippet": (
            "project_key collapses git worktree cwds back to the owning repo root "
            "so memory is pooled."
        ),
    },
    {
        "term": "sharechain",
        "frequency": 2,
        "context_snippet": "In p2pool, sharechain means the linked chain of miner shares.",
    },
    {
        "term": "xyzzy",
        "frequency": 1,
        "context_snippet": "xyzzy",  # empty signal → null expected
    },
]
_DEFINABLE = {"harness", "project_key", "sharechain"}

# Product JSON micro-tasks (subset of eval_product_models.LLM_TASKS)
from scripts.eval_product_models import LLM_TASKS  # noqa: E402


DEFAULT_MODELS = [
    "qwen3.5:2b",
    "gemma4:e2b-it-qat",
    "gemma4:e4b-it-qat",
    "gemma4:12b-it-qat",
]


def _token_overlap(a: str, b: str) -> float:
    import re

    ta = {w for w in re.split(r"[^a-z0-9]+", (a or "").lower()) if len(w) > 1}
    tb = {w for w in re.split(r"[^a-z0-9]+", (b or "").lower()) if len(w) > 1}
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


def _model_meta(name: str) -> dict[str, Any]:
    import urllib.request

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/show",
            data=json.dumps({"name": name}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    details = d.get("details") or {}
    mi = d.get("model_info") or {}
    mtp_keys = [k for k in mi if "mtp" in k.lower() or "draft" in k.lower()]
    # Heuristic: tensor-style keys sometimes list mtp.*
    caps = d.get("capabilities") or []
    return {
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "capabilities": caps,
        "mtp_metadata_keys": mtp_keys,
        "mtp_present_in_gguf_meta": bool(mtp_keys),
        "thinking_cap": "thinking" in caps,
        "tools_cap": "tools" in caps,
        "ollama_requires": (d.get("details") or {}).get("requires")
        or mi.get("general.requires"),
        "notes": (
            "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is "
            "primarily MLX/Apple (ollama ≥0.31). Qwen3.5 MTP needs special GGUF "
            "+ llama.cpp draft-mtp — not stock library tags."
        ),
    }


def _run_json_tasks(client) -> dict[str, Any]:
    results = []
    ok = 0
    latencies = []
    for task in LLM_TASKS:
        t0 = time.perf_counter()
        temp = task.get("temperature", None)
        out = client.generate_json(
            task["system"],
            task["user"],
            schema=task.get("schema"),
            temperature=temp,
        )
        ms = (time.perf_counter() - t0) * 1000
        latencies.append(ms)
        passed = False
        reason = []
        if out is None:
            reason.append("null")
        else:
            for k in task.get("require_keys") or []:
                if k not in out:
                    reason.append(f"missing:{k}")
            if "must_contain_any" in task and out:
                blob = json.dumps(out).lower()
                if not any(x.lower() in blob for x in task["must_contain_any"]):
                    reason.append("must_contain")
            if "bool_key" in task and out is not None:
                if out.get(task["bool_key"]) is not task.get("bool_expect"):
                    reason.append("bool")
            if not reason:
                passed = True
                ok += 1
        results.append(
            {"name": task["name"], "ok": passed, "ms": round(ms, 1), "reason": reason}
        )
    n = max(len(LLM_TASKS), 1)
    return {
        "pass_rate": ok / n,
        "n": n,
        "passed": ok,
        "latency_ms_mean": round(sum(latencies) / n, 1),
        "latency_ms_p50": round(sorted(latencies)[n // 2], 1),
        "tasks": results,
    }


def _run_machines(client) -> dict[str, Any]:
    from extractors.llm.refine_machines import refine_machines

    t0 = time.perf_counter()
    out = refine_machines(
        dict(_MACHINE_HEURISTICS),
        operator_email="ops@example.com",
        sample_contexts=_MACHINE_CONTEXTS,
        client=client,
    )
    ms = (time.perf_counter() - t0) * 1000
    kept = set(out.keys())
    tp = len(kept & _TRUE_HOSTS)
    fp = len(kept - _TRUE_HOSTS)
    fn = len(_TRUE_HOSTS - kept)
    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-9)
    return {
        "kept": sorted(kept),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
        "kept_real_hosts": _TRUE_HOSTS <= kept,
        "dropped_noise": not ({"Monday", "asyncpg", "hardening"} & kept),
        "ms": round(ms, 1),
        "gate_precision_ge_0.7": prec >= 0.7,
    }


def _run_vocab(client) -> dict[str, Any]:
    from extractors.llm.refine_ontology import refine_vocabulary_definitions

    t0 = time.perf_counter()
    out = refine_vocabulary_definitions(list(_VOCAB_TERMS), client=client)
    ms = (time.perf_counter() - t0) * 1000
    # out is list of terms with definition field or dict mapping — check API
    defs: dict[str, str | None] = {}
    if isinstance(out, list):
        for row in out:
            if isinstance(row, dict):
                defs[str(row.get("term") or "")] = row.get("definition")
    elif isinstance(out, dict):
        # maybe term -> def
        for k, v in out.items():
            if isinstance(v, dict):
                defs[k] = v.get("definition")
            else:
                defs[k] = v if isinstance(v, str) or v is None else str(v)

    non_null = 0
    echo = 0
    definable_ok = 0
    for term in _DEFINABLE:
        d = defs.get(term)
        snip = next(
            (t["context_snippet"] for t in _VOCAB_TERMS if t["term"] == term), ""
        )
        if d and isinstance(d, str) and len(d) > 5:
            non_null += 1
            ov = _token_overlap(d, snip)
            if ov >= 0.6:
                echo += 1
            else:
                definable_ok += 1
    # xyzzy should ideally be null
    xyz = defs.get("xyzzy")
    null_ok = xyz is None or xyz == "" or xyz == "null"
    n_def = max(len(_DEFINABLE), 1)
    return {
        "definitions": {k: (v[:80] if isinstance(v, str) else v) for k, v in defs.items()},
        "define_coverage": round(definable_ok / n_def, 4),
        "echo_rate": round(echo / max(non_null, 1), 4) if non_null else 0.0,
        "non_null": non_null,
        "xyzzy_null_ok": null_ok,
        "ms": round(ms, 1),
    }


def _score_row(json_r: dict, mach: dict, vocab: dict) -> float:
    """Higher is better composite for ranking (not a product gate)."""
    return (
        3.0 * float(json_r.get("pass_rate") or 0)
        + 2.0 * float(mach.get("f1") or 0)
        + 2.0 * float(vocab.get("define_coverage") or 0)
        - 1.0 * float(vocab.get("echo_rate") or 0)
        + (0.5 if mach.get("dropped_noise") else 0.0)
        + (0.5 if mach.get("kept_real_hosts") else 0.0)
    )


def run_one(model: str, device: str) -> dict[str, Any]:
    # Device: gpu → full offload; cpu → force num_gpu=0
    if device == "cpu":
        os.environ["TOTAL_RECALL_OLLAMA_NUM_GPU"] = "0"
        # Fair CPU bakeoff: pin threads to physical cores when known.
        try:
            n = os.cpu_count() or 8
            # Prefer physical-ish: half of logical if HT, else all.
            thr = max(1, n // 2) if n >= 8 else n
            os.environ["TOTAL_RECALL_OLLAMA_NUM_THREAD"] = str(thr)
        except Exception:  # noqa: BLE001
            pass
    else:
        os.environ["TOTAL_RECALL_OLLAMA_NUM_GPU"] = "999"
        os.environ.pop("TOTAL_RECALL_OLLAMA_NUM_THREAD", None)

    from extractors.llm.client import LLMClient, _resolve_sampling

    meta = _model_meta(model)
    opts, think = _resolve_sampling(model, None)
    meta["sampling_profile"] = {**opts, "think": think}
    client = LLMClient(provider="ollama", model=model, timeout=300.0)
    if not client.available:
        return {
            "model": model,
            "device": device,
            "available": False,
            "meta": meta,
            "error": "model not available",
        }

    print(f"  [{device}] {model}: json…", flush=True)
    json_r = _run_json_tasks(client)
    print(f"  [{device}] {model}: machines…", flush=True)
    mach = _run_machines(client)
    print(f"  [{device}] {model}: vocab…", flush=True)
    vocab = _run_vocab(client)
    score = _score_row(json_r, mach, vocab)
    return {
        "model": model,
        "device": device,
        "available": True,
        "meta": meta,
        "json_tasks": json_r,
        "machines": mach,
        "vocab": vocab,
        "composite_score": round(score, 4),
        "num_gpu_env": os.environ.get("TOTAL_RECALL_OLLAMA_NUM_GPU"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="comma-separated ollama tags",
    )
    ap.add_argument(
        "--device",
        default="both",
        choices=["gpu", "cpu", "both"],
        help="run on gpu (num_gpu=999), cpu (num_gpu=0), or both",
    )
    ap.add_argument(
        "--out",
        default=str(_REPO / "docs" / "eval-llm-bakeoff.md"),
    )
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    devices = ["gpu", "cpu"] if args.device == "both" else [args.device]

    from vec.runtime import ensure_product_ollama

    ensure_product_ollama(embed=False, chat=True, pull=True)

    rows: list[dict] = []
    for device in devices:
        for model in models:
            print(f"=== {device.upper()} / {model} ===", flush=True)
            try:
                row = run_one(model, device)
            except Exception as exc:  # noqa: BLE001
                row = {
                    "model": model,
                    "device": device,
                    "available": False,
                    "error": repr(exc),
                }
            rows.append(row)
            if row.get("available"):
                print(
                    f"  → json={row['json_tasks']['pass_rate']:.2f} "
                    f"mach_f1={row['machines']['f1']:.2f} "
                    f"def_cov={row['vocab']['define_coverage']:.2f} "
                    f"echo={row['vocab']['echo_rate']:.2f} "
                    f"score={row['composite_score']:.2f}",
                    flush=True,
                )
            else:
                print(f"  → SKIP {row.get('error')}", flush=True)

    # Rank per device among available
    ranking: dict[str, list] = {}
    for device in devices:
        avail = [r for r in rows if r.get("device") == device and r.get("available")]
        avail.sort(key=lambda r: r.get("composite_score") or 0, reverse=True)
        ranking[device] = [
            {
                "rank": i + 1,
                "model": r["model"],
                "score": r["composite_score"],
                "json_pass": r["json_tasks"]["pass_rate"],
                "mach_f1": r["machines"]["f1"],
                "define_coverage": r["vocab"]["define_coverage"],
                "echo_rate": r["vocab"]["echo_rate"],
                "mach_ms": r["machines"]["ms"],
                "vocab_ms": r["vocab"]["ms"],
            }
            for i, r in enumerate(avail)
        ]

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "ollama_version": _ollama_version(),
        "models_requested": models,
        "devices": devices,
        "ranking": ranking,
        "rows": rows,
        "recommendation": _recommend(ranking, rows),
        "mtp_summary": _mtp_summary(rows),
    }

    out = Path(args.out)
    out.write_text(_render_md(report), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    print(json.dumps({"ranking": ranking, "recommendation": report["recommendation"]}, indent=2))
    return 0


def _ollama_version() -> str:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=5) as r:
            return json.loads(r.read()).get("version", "?")
    except Exception:  # noqa: BLE001
        return "?"


def _mtp_summary(rows: list[dict]) -> dict:
    out = {}
    for r in rows:
        if not r.get("available"):
            continue
        m = r["model"]
        if m in out:
            continue
        meta = r.get("meta") or {}
        out[m] = {
            "mtp_present_in_gguf_meta": meta.get("mtp_present_in_gguf_meta"),
            "mtp_metadata_keys": meta.get("mtp_metadata_keys"),
            "note": meta.get("notes"),
        }
    return out


def _recommend(ranking: dict, rows: list[dict]) -> dict:
    """Product default recommendation: prefer GPU ranking if present."""
    board = ranking.get("gpu") or ranking.get("cpu") or []
    if not board:
        return {"default": "qwen3.5:2b", "reason": "no available results; keep current"}
    top = board[0]
    # Prefer current default if within 10% composite of top and much smaller/faster
    control = next((r for r in board if r["model"] == "qwen3.5:2b"), None)
    reason_parts = [
        f"top composite on primary device: {top['model']} score={top['score']}"
    ]
    switch = top["model"]
    if control and control["model"] != top["model"]:
        # only switch if challenger clearly better on define_coverage and not worse echo
        if (
            top["define_coverage"] + 1e-9 >= control["define_coverage"]
            and top["echo_rate"] <= control["echo_rate"] + 0.05
            and top["json_pass"] + 1e-9 >= control["json_pass"]
            and top["mach_f1"] + 1e-9 >= 0.7
        ):
            reason_parts.append(
                f"challenger beats control on def_cov/echo/json; switch default to {top['model']}"
            )
            switch = top["model"]
        else:
            reason_parts.append(
                f"control qwen3.5:2b still competitive "
                f"(def_cov={control['define_coverage']} echo={control['echo_rate']} "
                f"json={control['json_pass']}); keep default unless product wants quality>"
                f"latency"
            )
            switch = "qwen3.5:2b"
    return {
        "default": switch,
        "cpu_default": (ranking.get("cpu") or [{"model": switch}])[0]["model"]
        if ranking.get("cpu")
        else switch,
        "gpu_default": (ranking.get("gpu") or [{"model": switch}])[0]["model"]
        if ranking.get("gpu")
        else switch,
        "reason": "; ".join(reason_parts),
        "embed_unchanged": "qwen3-embedding:0.6b",
    }


def _render_md(report: dict) -> str:
    lines = [
        "# LLM refine bakeoff (total-recall chat model)",
        "",
        f"Generated: {report['generated']}",
        f"Ollama daemon: `{report['ollama_version']}`",
        "",
        "## Recommendation",
        "",
        "```json",
        json.dumps(report["recommendation"], indent=2),
        "```",
        "",
        "## Ranking",
        "",
    ]
    for device, board in (report.get("ranking") or {}).items():
        lines.append(f"### {device.upper()}")
        lines.append("")
        lines.append(
            "| rank | model | score | json | mach F1 | def_cov | echo | mach ms | vocab ms |"
        )
        lines.append("|------|-------|-------|------|---------|---------|------|---------|----------|")
        for r in board:
            lines.append(
                f"| {r['rank']} | `{r['model']}` | {r['score']} | {r['json_pass']} | "
                f"{r['mach_f1']} | {r['define_coverage']} | {r['echo_rate']} | "
                f"{r['mach_ms']} | {r['vocab_ms']} |"
            )
        lines.append("")
    lines += [
        "## MTP / speculative",
        "",
        "```json",
        json.dumps(report.get("mtp_summary"), indent=2),
        "```",
        "",
        "## Full rows",
        "",
        "```json",
        json.dumps(report.get("rows"), indent=2)[:120000],
        "```",
        "",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
