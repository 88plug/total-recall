# total-recall product model eval

Generated: 2026-07-17 20:31:12 -0400

## Product runtime
```json
{
  "daemon_reachable": true,
  "bin": "/usr/local/bin/ollama",
  "ensure": {
    "base_url": "http://localhost:11434",
    "embed": true,
    "chat": true,
    "daemon": true,
    "embed_model": "qwen3-embedding:0.6b",
    "chat_model": "qwen3.5:2b",
    "embed_ready": true,
    "chat_ready": true,
    "bin": "/usr/local/bin/ollama"
  },
  "models_present": {
    "qwen3-embedding:0.6b": true,
    "qwen3.5:2b": true
  },
  "gates": {
    "daemon_up": true,
    "embed_model": true,
    "chat_model": true
  }
}
```

## MTP (qwen3.5:2b)
```json
{
  "model": "qwen3.5:2b",
  "n_tensors": 728,
  "n_mtp_tensors": 15,
  "sample_mtp": [
    "mtp.fc.weight",
    "mtp.layers.0.attn_k.weight",
    "mtp.layers.0.attn_k_norm.weight",
    "mtp.layers.0.attn_norm.weight",
    "mtp.layers.0.attn_output.weight"
  ],
  "gates": {
    "has_mtp_tensors": true
  }
}
```

## Dense embeds (qwen3-embedding:0.6b)
```json
{
  "runtime": {
    "base_url": "http://localhost:11434",
    "embed": true,
    "chat": false,
    "daemon": true,
    "embed_model": "qwen3-embedding:0.6b",
    "chat_model": null,
    "embed_ready": true,
    "chat_ready": false,
    "bin": "/usr/local/bin/ollama"
  },
  "model": "qwen3-embedding:0.6b",
  "backend": "ollama",
  "dim": 1024,
  "backfill": {
    "embedded": 28,
    "chunks": 28,
    "seconds": 0.627
  },
  "pure_dense": {
    "n": 20,
    "p@1": 0.8,
    "p@5": 0.95,
    "mrr": 0.8729166666666668,
    "latency_ms_p50": 275.4541980102658,
    "latency_ms_p95": 370.4859220306389,
    "latency_ms_mean": 276.7624404077651
  },
  "fts_only": {
    "n": 20,
    "p@1": 0.2,
    "p@5": 0.25,
    "mrr": 0.23125,
    "latency_ms_p50": 0.633572053629905,
    "latency_ms_p95": 1.0545109980739653,
    "latency_ms_mean": 0.6904487469000742
  },
  "hybrid": {
    "n": 20,
    "p@1": 0.8,
    "p@5": 0.95,
    "mrr": 0.8729166666666668,
    "latency_ms_p50": 270.01538599142805,
    "latency_ms_p95": 384.22725995769724,
    "latency_ms_mean": 278.49626915121917
  },
  "pairwise_target_beats_distractor": 1.0,
  "gates": {
    "hybrid_not_worse_than_fts_p@5": true,
    "hybrid_p@1_near_dense": true,
    "hybrid_p@1_ge_0.75": true,
    "pure_dense_p@1_ge_0.5": true,
    "pure_dense_mrr_ge_0.6": true,
    "pairwise_ge_0.9": true
  }
}
```

## LLM refine (qwen3.5:2b)
```json
{
  "runtime": {
    "base_url": "http://localhost:11434",
    "embed": false,
    "chat": true,
    "daemon": true,
    "embed_model": null,
    "chat_model": "qwen3.5:2b",
    "embed_ready": false,
    "chat_ready": true,
    "bin": "/usr/local/bin/ollama"
  },
  "model": "qwen3.5:2b",
  "n": 5,
  "pass_rate": 1.0,
  "tasks": [
    {
      "name": "extract_decision",
      "pass": true,
      "latency_ms": 1386.6,
      "output": {
        "decision": "use asyncpg",
        "topic": "database connection"
      },
      "reasons": []
    },
    {
      "name": "extract_ban",
      "pass": true,
      "latency_ms": 1611.4,
      "output": {
        "banned": "committing .env files with secrets",
        "reason": "Always use a secret management vault instead"
      },
      "reasons": []
    },
    {
      "name": "classify_correction",
      "pass": true,
      "latency_ms": 1641.3,
      "output": {
        "is_correction": true,
        "summary": "User requested switching from Black to Ruff for code formatting."
      },
      "reasons": []
    },
    {
      "name": "machine_ner",
      "pass": true,
      "latency_ms": 2456.7,
      "output": {
        "hosts": [
          "web-01",
          "cache-02"
        ],
        "services": [
          "nginx",
          "redis"
        ]
      },
      "reasons": []
    },
    {
      "name": "vocab_def",
      "pass": true,
      "latency_ms": 2338.0,
      "output": {
        "term": "harness",
        "definition": "the Claude Code / Grok plugin runner"
      },
      "reasons": []
    }
  ],
  "production_refine": {
    "machines_ms": 2079.6,
    "machines": {
      "kept": [
        "asyncpg",
        "cache-02",
        "web-01"
      ],
      "kept_real_hosts": true,
      "dropped_monday": true,
      "dropped_asyncpg": false
    },
    "machines_ok": true,
    "vocab_ms": 2824.3,
    "vocab_definitions": {
      "harness": "A plugin runner that manages Claude Code and Grok sessions by loading associated MCP servers and skills.",
      "project_key": "A mechanism used to collapse Git worktrees back to the repository root, which helps manage memory pooling."
    },
    "vocab_ok": true
  },
  "gates": {
    "json_task_pass_rate_ge_0.6": true,
    "mean_latency_ms_lt_15000": true,
    "machines_refine_keeps_real_hosts": true,
    "vocab_refine_defines_term": true
  }
}
```

## Gate summary
- `PASS` runtime.daemon_up
- `PASS` runtime.embed_model
- `PASS` runtime.chat_model
- `PASS` mtp.has_mtp_tensors
- `PASS` embeds.hybrid_not_worse_than_fts_p@5
- `PASS` embeds.hybrid_p@1_near_dense
- `PASS` embeds.hybrid_p@1_ge_0.75
- `PASS` embeds.pure_dense_p@1_ge_0.5
- `PASS` embeds.pure_dense_mrr_ge_0.6
- `PASS` embeds.pairwise_ge_0.9
- `PASS` llm.json_task_pass_rate_ge_0.6
- `PASS` llm.mean_latency_ms_lt_15000
- `PASS` llm.machines_refine_keeps_real_hosts
- `PASS` llm.vocab_refine_defines_term

**Overall: PASS** (14/14 gates)
