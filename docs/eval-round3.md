# total-recall eval round 3 — vectors live + 10x hard A/B

Generated: 2026-07-17 21:31:55 -0400

## Production index (real machine)
```json
{
  "path": "/home/andrew/.claude/plugins/data/total-recall-88plug/total-recall/index.db",
  "size_mb": 568.3,
  "extractions": 11358,
  "chunks": 11361,
  "vec_rows": 11361,
  "uncovered": 0,
  "coverage": 1.0,
  "vec_meta": {
    "dim": "1024",
    "format": "2",
    "model": "qwen3-embedding:0.6b",
    "backend": "ollama"
  },
  "gates": {
    "prod_db_exists": true,
    "prod_format_v2": true,
    "prod_model_qwen3_embed": true,
    "prod_backend_ollama": true,
    "prod_dim_1024": true,
    "prod_full_coverage": true,
    "prod_has_vectors": true,
    "prod_scale_ge_1k": true
  }
}
```

## Live smoke
```json
{
  "query": "ollama embed model product",
  "hybrid_n": 5,
  "dense_n": 5,
  "fts_n": 5,
  "hybrid_top": "no we always embed ollama look more closely",
  "dense_top": "no we always embed ollama look more closely",
  "model": "qwen3-embedding:0.6b",
  "backend": "ollama",
  "dim": 1024,
  "query_instruct": "Instruct: Retrieve relevant past engineering decisions, corrections, tool preferences, and session n",
  "gates": {
    "live_hybrid_ok": true,
    "live_dense_ok": true,
    "live_model_is_qwen3_embed": true,
    "live_memory_instruct": true
  }
}
```

## A/B legacy web/no-kind vs memory+kind
```json
{
  "legacy_web_no_kind": {
    "n": 30,
    "p@1": 0.5333333333333333,
    "p@5": 0.7666666666666667,
    "mrr": 0.6268386243386244,
    "miss_rate@1": 0.4666666666666667,
    "miss_samples": [
      "keep_alive=-1 pins qwen3-embedding for the whole backfill",
      "screen-mcp screenshots and clicks; chrome-devtools is unauth",
      "never rm -rf unbraced $VAR; empty expands to filesystem root",
      "LAN 192.168.1.211:8890 then local docker then tailscale",
      "documents embed raw; only search queries get Instruct/Query ",
      "few-shot examples show Monday and asyncpg dropped while web-"
    ]
  },
  "cranked_memory_plus_kind": {
    "n": 30,
    "p@1": 0.5,
    "p@5": 0.7666666666666667,
    "mrr": 0.6056878306878307,
    "miss_rate@1": 0.5,
    "miss_samples": [
      "keep_alive=-1 pins qwen3-embedding for the whole backfill",
      "never rm -rf unbraced $VAR; empty expands to filesystem root",
      "managed ollama binary lives under plugin data bin not system",
      "LAN 192.168.1.211:8890 then local docker then tailscale",
      "documents embed raw; only search queries get Instruct/Query ",
      "few-shot examples show Monday and asyncpg dropped while web-"
    ]
  },
  "p@1_delta": -0.0333,
  "mrr_delta": -0.0212,
  "miss_rate_delta": -0.0333,
  "cranked_wins_or_ties_p@1": false
}
```

## HARD40 hybrid suite (realistic noise)
```json
{
  "pure_dense": {
    "n": 40,
    "p@1": 0.45,
    "p@5": 0.75,
    "mrr": 0.5597222222222221,
    "miss_rate@1": 0.55,
    "miss_samples": [
      "keep_alive=-1 pins qwen3-embedding for the whole backfill",
      "documents embed raw; only search queries get Instruct/Query ",
      "dense_primary keeps vector order and appends FTS fill",
      "qwen3.5:2b won define coverage on CPU; 9b null-collapsed und",
      "non-thinking profile uses temperature 0.7 top_k 20 top_p 0.8",
      "vocab refine returns null definition when context is only th"
    ]
  },
  "fts_only": {
    "n": 40,
    "p@1": 0.475,
    "p@5": 0.675,
    "mrr": 0.56125,
    "miss_rate@1": 0.525,
    "miss_samples": [
      "keep_alive=-1 pins qwen3-embedding for the whole backfill",
      "dense_primary keeps vector order and appends FTS fill",
      "corrections bans and decisions get cosine distance boost ove",
      "qwen3.5:2b won define coverage on CPU; 9b null-collapsed und",
      "non-thinking profile uses temperature 0.7 top_k 20 top_p 0.8",
      "few-shot examples show Monday and asyncpg dropped while web-"
    ]
  },
  "hybrid": {
    "n": 40,
    "p@1": 0.525,
    "p@5": 0.8,
    "mrr": 0.6452380952380952,
    "miss_rate@1": 0.475,
    "miss_samples": [
      "keep_alive=-1 pins qwen3-embedding for the whole backfill",
      "documents embed raw; only search queries get Instruct/Query ",
      "dense_primary keeps vector order and appends FTS fill",
      "qwen3.5:2b won define coverage on CPU; 9b null-collapsed und",
      "non-thinking profile uses temperature 0.7 top_k 20 top_p 0.8",
      "vocab refine returns null definition when context is only th"
    ]
  },
  "backfill": {
    "embedded": 85,
    "chunks": 85,
    "seconds": 1.069
  }
}
```

## Adversarial8 (antonym near-miss twins)
```json
{
  "pure_dense": {
    "n": 8,
    "p@1": 0.5,
    "p@5": 1.0,
    "mrr": 0.6666666666666666,
    "miss_rate@1": 0.5,
    "miss_samples": [
      "dense_primary keeps vector order and appends FTS fill",
      "qwen3.5:2b won define coverage on CPU; 9b null-collapsed und",
      "documents embed raw; only search queries get Instruct/Query ",
      "non-thinking profile uses temperature 0.7 top_k 20 top_p 0.8"
    ]
  },
  "fts_only": {
    "n": 8,
    "p@1": 0.25,
    "p@5": 0.5,
    "mrr": 0.35416666666666663,
    "miss_rate@1": 0.75,
    "miss_samples": [
      "keep_alive=-1 pins qwen3-embedding for the whole backfill",
      "dense_primary keeps vector order and appends FTS fill",
      "qwen3.5:2b won define coverage on CPU; 9b null-collapsed und",
      "screen-mcp screenshots and clicks; chrome-devtools is unauth",
      "symbol queries like web-01 rely on exactish FTS promote over",
      "non-thinking profile uses temperature 0.7 top_k 20 top_p 0.8"
    ]
  },
  "hybrid": {
    "n": 8,
    "p@1": 0.625,
    "p@5": 1.0,
    "mrr": 0.7166666666666667,
    "miss_rate@1": 0.375,
    "miss_samples": [
      "dense_primary keeps vector order and appends FTS fill",
      "qwen3.5:2b won define coverage on CPU; 9b null-collapsed und",
      "non-thinking profile uses temperature 0.7 top_k 20 top_p 0.8"
    ]
  }
}
```

Hybrid miss reduction vs FTS (HARD40): **9.5%** (FTS miss 52.50% → hybrid miss 47.50%)

A/B miss reduction (legacy→crank): **-7.1%**

## Gates
- `PASS` prod_db_exists
- `PASS` prod_format_v2
- `PASS` prod_model_qwen3_embed
- `PASS` prod_backend_ollama
- `PASS` prod_dim_1024
- `PASS` prod_full_coverage
- `PASS` prod_has_vectors
- `PASS` prod_scale_ge_1k
- `PASS` live_hybrid_ok
- `PASS` live_dense_ok
- `PASS` live_model_is_qwen3_embed
- `PASS` live_memory_instruct
- `PASS` hard40_hybrid_p@1_ge_0.5
- `PASS` hard40_hybrid_p@5_ge_0.75
- `PASS` hard40_hybrid_ge_dense
- `PASS` hard40_hybrid_beats_fts_p@1
- `PASS` hard40_hybrid_best_of_three
- `PASS` adv8_hybrid_p@1_ge_0.5
- `PASS` adv8_hybrid_p@5_ge_0.75
- `PASS` adv8_hybrid_beats_dense
- `PASS` hybrid_not_worse_than_dense_mrr
- `PASS` hybrid_reduces_dense_misses

**Overall: PASS** (22/22)
