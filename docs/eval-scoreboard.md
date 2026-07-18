# Scoreboard: product qwen stack vs legacy / FTS

Live head-to-head. **qwen3-embedding:0.6b** + hybrid dense_primary + adaptive re-rank (lexical only for IDs/near-miss; paraphrases stay dense-led).

```json
{
  "easy": {
    "pure_dense": {
      "n": 20,
      "p@1": 0.85,
      "p@5": 0.9,
      "mrr": 0.8821,
      "miss@1": 0.15
    },
    "fts_only": {
      "n": 20,
      "p@1": 0.15,
      "p@5": 0.25,
      "mrr": 0.1988,
      "miss@1": 0.85
    },
    "hybrid": {
      "n": 20,
      "p@1": 0.85,
      "p@5": 0.9,
      "mrr": 0.8821,
      "miss@1": 0.15
    }
  },
  "hard40": {
    "pure_dense": {
      "n": 40,
      "p@1": 0.45,
      "p@5": 0.75,
      "mrr": 0.5597,
      "miss@1": 0.55
    },
    "fts_only": {
      "n": 40,
      "p@1": 0.5,
      "p@5": 0.675,
      "mrr": 0.5792,
      "miss@1": 0.5
    },
    "hybrid": {
      "n": 40,
      "p@1": 0.475,
      "p@5": 0.75,
      "mrr": 0.5844,
      "miss@1": 0.525
    }
  },
  "adversarial": {
    "legacy": {
      "n": 432,
      "p@1": 0.5231,
      "p@5": 0.831,
      "mrr": 0.6593,
      "miss@1": 0.4769
    },
    "product": {
      "n": 432,
      "p@1": 0.7037,
      "p@5": 0.8866,
      "mrr": 0.7887,
      "miss@1": 0.2963
    },
    "p@1_lift": 0.1806,
    "miss_cut": 0.3787
  },
  "model": "qwen3-embedding:0.6b",
  "dim": 1024
}
```

## Bottom line

- Easy paraphrases: FTS **0.15** → product hybrid **0.85** (~6× FTS; matches dense 0.85)
- Hard40: hybrid **0.475** best of FTS/dense
- Adversarial 432: legacy dense **0.5231** → product **0.7037** (+0.18 P@1, **38%** miss cut)
