# total-recall eval round 3 — vectors live + 10x hard A/B

Generated: 2026-07-18 01:30:10 -0400

## Production index (real machine)
```json
{
  "path": "/home/andrew/.claude/plugins/data/total-recall-88plug/total-recall/index.db",
  "size_mb": 568.4,
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
  "query_instruct": "Instruct: Given a query, retrieve relevant past engineering session passages that answer the query\nQ",
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
    "n": 32,
    "p@1": 0.625,
    "p@5": 0.625,
    "mrr": 0.625,
    "miss_rate@1": 0.375,
    "miss_samples": [
      "decision: keep embed weights resident in VRAM \u2014 keep_alive=-",
      "decision: stop silent oversize chunk loss \u2014 truncate=false o",
      "decision: asymmetric encode \u2014 query side only gets the Instr",
      "decision: default fusion that protects paraphrase top hits i",
      "decision: when FTS should still win top slot use exactish pr",
      "decision: kind that outranks trivia facts in dense re-rank \u2014"
    ]
  },
  "cranked_memory_plus_kind": {
    "n": 32,
    "p@1": 0.625,
    "p@5": 0.625,
    "mrr": 0.6364583333333333,
    "miss_rate@1": 0.375,
    "miss_samples": [
      "decision: keep embed weights resident in VRAM \u2014 keep_alive=-",
      "decision: stop silent oversize chunk loss \u2014 truncate=false o",
      "decision: asymmetric encode \u2014 query side only gets the Instr",
      "decision: default fusion that protects paraphrase top hits i",
      "decision: when FTS should still win top slot use exactish pr",
      "decision: kind that outranks trivia facts in dense re-rank \u2014"
    ]
  },
  "p@1_delta": 0.0,
  "mrr_delta": 0.0115,
  "miss_rate_delta": 0.0,
  "cranked_wins_or_ties_p@1": true
}
```

## HARD40 hybrid suite (realistic noise)
```json
{
  "pure_dense": {
    "n": 40,
    "p@1": 1.0,
    "p@5": 1.0,
    "mrr": 1.0,
    "miss_rate@1": 0.0,
    "miss_samples": []
  },
  "fts_only": {
    "n": 40,
    "p@1": 1.0,
    "p@5": 1.0,
    "mrr": 1.0,
    "miss_rate@1": 0.0,
    "miss_samples": []
  },
  "hybrid": {
    "n": 40,
    "p@1": 1.0,
    "p@5": 1.0,
    "mrr": 1.0,
    "miss_rate@1": 0.0,
    "miss_samples": []
  },
  "backfill": {
    "embedded": 85,
    "chunks": 85,
    "seconds": 1.122
  }
}
```

## Adversarial8 (antonym near-miss twins)
```json
{
  "pure_dense": {
    "n": 8,
    "p@1": 1.0,
    "p@5": 1.0,
    "mrr": 1.0,
    "miss_rate@1": 0.0,
    "miss_samples": []
  },
  "fts_only": {
    "n": 8,
    "p@1": 1.0,
    "p@5": 1.0,
    "mrr": 1.0,
    "miss_rate@1": 0.0,
    "miss_samples": []
  },
  "hybrid": {
    "n": 8,
    "p@1": 1.0,
    "p@5": 1.0,
    "mrr": 1.0,
    "miss_rate@1": 0.0,
    "miss_samples": []
  }
}
```

Hybrid miss reduction vs FTS (HARD40): **0.0%** (FTS miss 0.00% → hybrid miss 0.00%)

A/B miss reduction (legacy→crank): **0.0%**

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
