# Scoreboard: product qwen stack vs legacy / FTS

Live head-to-head after **2.3.6** (product ollama auto-update + ranking polish +
HARD40 enrichment). Model: **qwen3-embedding:0.6b** hybrid dense_primary.

```json
{
  "easy": {
    "hybrid": { "n": 20, "p@1": 0.9, "p@5": 0.95, "mrr": 0.918 },
    "fts_only": { "n": 20, "p@1": 0.2 },
    "pure_dense": { "n": 20, "p@1": 0.9 }
  },
  "product_hard15": {
    "hybrid": { "n": 15, "p@1": 0.933, "p@5": 1.0, "mrr": 0.967 }
  },
  "hard40": {
    "hybrid": { "n": 40, "p@1": 1.0, "p@5": 1.0, "mrr": 1.0 },
    "pure_dense": { "n": 40, "p@1": 1.0 }
  },
  "adversarial": {
    "n": 432,
    "product_hybrid": { "p@1": 0.7269, "p@5": 0.89, "mrr": 0.80, "miss@1": 0.273 },
    "product_pure": { "p@1": 0.6829 },
    "legacy_dense_baseline": { "p@1": 0.5231 },
    "p@1_lift_vs_legacy": 0.2038
  },
  "gates": {
    "eval_product_models": "20/20 PASS",
    "eval_adversarial_10x": "11/11 PASS",
    "eval_round3": "22/22 PASS"
  },
  "model": "qwen3-embedding:0.6b",
  "dim": 1024,
  "query_instruct": "Instruct: Given a query, retrieve relevant past engineering session passages that answer the query\\nQuery:"
}
```

## Bottom line

| Suite | Result |
|-------|--------|
| Easy hybrid P@1 | **0.90** (~4.5× FTS 0.20) |
| Product hard15 hybrid P@1 | **0.933** |
| Hard40 hybrid P@1 | **1.0** (enriched decision notes) |
| Adversarial 432 hybrid P@1 | **0.7269** (+20pp vs legacy dense 0.52) |
| Live prod index | format v2, full coverage, hybrid OK |

## 2.3.6 notes

- Product ollama **auto-updates** to latest (`.tar.zst`; needs `zstd`)
- Standing-ban ranking: don't penalize `decision: … legacy … rejected`
- HARD40 targets enriched as full decision notes (was empty-cosine lottery)
