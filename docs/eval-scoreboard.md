# Scoreboard: product qwen stack vs legacy / FTS

Live head-to-head. **qwen3-embedding:0.6b** + hybrid dense_primary + adaptive
re-rank + **card-shaped domain instruct** (MTEB template, session-memory task).

Re-eval after model-card expert pass (2026-07-18, v2.3.5).

```json
{
  "easy": {
    "pure_dense": {
      "n": 20,
      "p@1": 0.9,
      "p@5": 0.95,
      "mrr": 0.9181,
      "miss@1": 0.1
    },
    "fts_only": {
      "n": 20,
      "p@1": 0.2,
      "p@5": 0.25,
      "mrr": 0.2313,
      "miss@1": 0.8
    },
    "hybrid": {
      "n": 20,
      "p@1": 0.9,
      "p@5": 0.95,
      "mrr": 0.9181,
      "miss@1": 0.1
    }
  },
  "hard40": {
    "pure_dense": {
      "n": 40,
      "p@1": 0.475,
      "p@5": 0.75,
      "mrr": 0.6002,
      "miss@1": 0.525
    },
    "fts_only": {
      "n": 40,
      "p@1": 0.475,
      "p@5": 0.675,
      "mrr": 0.5613,
      "miss@1": 0.525
    },
    "hybrid": {
      "n": 40,
      "p@1": 0.525,
      "p@5": 0.775,
      "mrr": 0.6346,
      "miss@1": 0.475
    }
  },
  "adversarial": {
    "legacy_dense_baseline": {
      "n": 432,
      "p@1": 0.5231,
      "p@5": 0.831,
      "mrr": 0.6593,
      "miss@1": 0.4769
    },
    "product_prior_2.3.4": {
      "n": 432,
      "p@1": 0.7037,
      "p@5": 0.8866,
      "mrr": 0.7887,
      "miss@1": 0.2963
    },
    "product_2.3.5_card_instruct": {
      "n": 432,
      "p@1": 0.7153,
      "p@5": 0.8843,
      "mrr": 0.7888,
      "miss@1": 0.2847
    },
    "p@1_lift_vs_legacy": 0.1922,
    "miss_cut_vs_legacy": 0.4026,
    "p@1_lift_vs_prior": 0.0116
  },
  "model": "qwen3-embedding:0.6b",
  "dim": 1024,
  "query_instruct": "Instruct: Given a query, retrieve relevant past engineering session passages that answer the query\\nQuery:"
}
```

## Bottom line

| Suite | Prior (2.3.4) | Card instruct (2.3.5) | Δ |
|-------|---------------|----------------------|---|
| Easy hybrid P@1 | 0.85 | **0.90** | +5pp |
| Easy vs FTS | 0.15 → 0.85 (~6×) | 0.20 → **0.90** (~4.5× FTS; higher abs) | |
| Hard40 hybrid P@1 | 0.475 | **0.525** | +5pp |
| Adversarial 432 hybrid P@1 | 0.7037 | **0.7153** | +1.2pp |
| Adversarial vs legacy dense | +18pp | **+19pp** (38%→**40%** miss cut) | |

**No re-embed** — query instruct only. Docs stay raw (card rule).

## Card alignment

| Model | Card rule | Product |
|-------|-----------|---------|
| qwen3-embedding:0.6b | `Instruct:…\nQuery:{q}` no space; docs raw; L2; domain task | shipped |
| qwen3.5:2b | non-thinking structured: temp 0.7 / top_p 0.8 / pp 1.5; think off | JSON refine + schema |
