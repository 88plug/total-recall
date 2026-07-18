# total-recall product model eval

Generated: 2026-07-17 22:36:03 -0400

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
Query instruct (truncated): `Instruct: Given a query, retrieve relevant past engineering session passages that answer the query
Query:`

### Easy / hard / instruct A/B
```json
{
  "easy": {
    "label": "easy",
    "n": 20,
    "pure_dense": {
      "n": 20,
      "p@1": 0.9,
      "p@5": 0.95,
      "mrr": 0.9180555555555555,
      "latency_ms_p50": 170.7130359718576,
      "latency_ms_p95": 224.29389803437516,
      "latency_ms_mean": 172.5700234004762
    },
    "fts_only": {
      "n": 20,
      "p@1": 0.2,
      "p@5": 0.25,
      "mrr": 0.23125,
      "latency_ms_p50": 0.4775490378960967,
      "latency_ms_p95": 0.8272590348497033,
      "latency_ms_mean": 0.4977263597538695
    },
    "hybrid": {
      "n": 20,
      "p@1": 0.9,
      "p@5": 0.95,
      "mrr": 0.9180555555555555,
      "latency_ms_p50": 159.9972689873539,
      "latency_ms_p95": 218.24196400120854,
      "latency_ms_mean": 167.59815319092013
    },
    "pairwise_target_beats_distractor": 1.0,
    "hybrid_miss_at_1": [
      "ci pipeline runner",
      "how do we deploy"
    ],
    "hybrid_miss_rate_at_1": 0.1
  },
  "hard": {
    "label": "hard",
    "n": 15,
    "pure_dense": {
      "n": 15,
      "p@1": 0.8666666666666667,
      "p@5": 1.0,
      "mrr": 0.9333333333333333,
      "latency_ms_p50": 147.93182304129004,
      "latency_ms_p95": 191.74257898703218,
      "latency_ms_mean": 157.17618520138785
    },
    "fts_only": {
      "n": 15,
      "p@1": 0.7333333333333333,
      "p@5": 0.8666666666666667,
      "mrr": 0.8,
      "latency_ms_p50": 0.5835409974679351,
      "latency_ms_p95": 1.0476669995114207,
      "latency_ms_mean": 0.6459032728647193
    },
    "hybrid": {
      "n": 15,
      "p@1": 0.9333333333333333,
      "p@5": 1.0,
      "mrr": 0.9666666666666667,
      "latency_ms_p50": 147.69869000883773,
      "latency_ms_p95": 181.5881030051969,
      "latency_ms_mean": 155.7400970098873
    },
    "pairwise_target_beats_distractor": 1.0,
    "hybrid_miss_at_1": [
      "what broke login after the oauth refactor"
    ],
    "hybrid_miss_rate_at_1": 0.06666666666666667
  },
  "instruct_ab": {
    "web_instruct": {
      "n": 20,
      "p@1": 0.8,
      "p@5": 0.95,
      "mrr": 0.86875,
      "latency_ms_p50": 155.41989699704573,
      "latency_ms_p95": 223.05179998511449,
      "latency_ms_mean": 167.9995365004288
    },
    "memory_instruct": {
      "n": 20,
      "p@1": 0.85,
      "p@5": 0.95,
      "mrr": 0.8899999999999999,
      "latency_ms_p50": 175.02151301596314,
      "latency_ms_p95": 210.8892630203627,
      "latency_ms_mean": 177.65051924798172
    },
    "memory_p@1_delta": 0.05,
    "memory_mrr_delta": 0.0212,
    "memory_wins_or_ties_p@1": true
  },
  "backfill": {
    "easy_embedded": 30,
    "easy_seconds": 0.47,
    "hard_embedded": 40,
    "hard_seconds": 0.551
  },
  "model": "qwen3-embedding:0.6b",
  "dim": 1024
}
```

## LLM refine (qwen3.5:2b)
```json
{
  "model": "qwen3.5:2b",
  "n": 6,
  "pass_rate": 1.0,
  "tasks": [
    {
      "name": "extract_decision",
      "pass": true,
      "latency_ms": 973.6,
      "output": {
        "decision": "asyncpg for postgres",
        "topic": "database connection"
      },
      "reasons": []
    },
    {
      "name": "extract_ban",
      "pass": true,
      "latency_ms": 1213.9,
      "output": {
        "banned": "commit .env files with secrets",
        "reason": "prevents accidental exposure of sensitive credentials in version control"
      },
      "reasons": []
    },
    {
      "name": "classify_correction",
      "pass": true,
      "latency_ms": 943.0,
      "output": {
        "is_correction": true,
        "summary": "prefer ruff over black"
      },
      "reasons": []
    },
    {
      "name": "machine_ner",
      "pass": true,
      "latency_ms": 1160.3,
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
      "latency_ms": 1099.8,
      "output": {
        "term": "harness",
        "definition": "Claude Code / Grok plugin runner"
      },
      "reasons": []
    },
    {
      "name": "null_when_missing",
      "pass": true,
      "latency_ms": 657.7,
      "output": {
        "hosts": []
      },
      "reasons": []
    }
  ],
  "production_refine": {
    "machines_ms": 1060.4,
    "machines": {
      "kept": [
        "cache-02",
        "web-01"
      ],
      "kept_real_hosts": true,
      "dropped_monday": true,
      "dropped_asyncpg": true
    },
    "machines_ok": true,
    "vocab_ms": 2604.5,
    "vocab_definitions": {
      "harness": "A plugin runner that manages Claude Code and Grok sessions by loading associated MCP servers and skills.",
      "project_key": "A mechanism used to collapse Git worktrees back to the repository root, which helps manage memory pooling."
    },
    "vocab_ok": true
  },
  "gates": {
    "json_task_pass_rate_ge_0.8": true,
    "mean_latency_ms_lt_15000": true,
    "machines_refine_keeps_real_hosts": true,
    "machines_refine_precision": true,
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
- `PASS` embeds.easy_hybrid_p@1_ge_0.75
- `PASS` embeds.easy_pure_dense_p@1_ge_0.7
- `PASS` embeds.easy_pure_dense_mrr_ge_0.75
- `PASS` embeds.easy_pairwise_ge_0.9
- `PASS` embeds.hard_hybrid_p@1_ge_0.6
- `PASS` embeds.hard_hybrid_p@5_ge_0.85
- `PASS` embeds.hard_pure_dense_p@1_ge_0.55
- `PASS` embeds.hard_miss_rate_at_1_le_0.4
- `PASS` embeds.memory_instruct_not_worse_than_web
- `PASS` llm.json_task_pass_rate_ge_0.8
- `PASS` llm.mean_latency_ms_lt_15000
- `PASS` llm.machines_refine_keeps_real_hosts
- `PASS` llm.machines_refine_precision
- `PASS` llm.vocab_refine_defines_term

**Overall: PASS** (20/20 gates)
