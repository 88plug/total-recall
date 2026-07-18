# total-recall eval round 2 (brand-new suites)

Generated: 2026-07-17 20:59:42 -0400

Post model-card crank learnings. New corpora — not the 2.3.3/2.3.4 easy set.

## Macro retrieval
```json
{
  "mean_hybrid_p@1": 0.869047619047619,
  "mean_hybrid_p@5": 1.0,
  "mean_miss_rate@1": 0.13095238095238096
}
```

## Suites
```json
{
  "R1_session": {
    "label": "R1_session",
    "n": 8,
    "pure_dense": {
      "n": 8,
      "p@1": 0.75,
      "p@5": 1.0,
      "mrr": 0.875,
      "miss_rate@1": 0.25,
      "latency_ms_p50": 279.5279219863005,
      "miss_samples": [
        "session 2026-07-16: ollama keep_alive was 5m so qwen3-embedding unload",
        "product-owned path: ensure_product_ollama downloads bin under plugin d"
      ]
    },
    "fts_only": {
      "n": 8,
      "p@1": 0.875,
      "p@5": 1.0,
      "mrr": 0.9166666666666666,
      "miss_rate@1": 0.125,
      "latency_ms_p50": 1.2141530169174075,
      "miss_samples": [
        "standing rule: always set a hard max_tokens and stream-cancel on clien"
      ]
    },
    "hybrid": {
      "n": 8,
      "p@1": 0.75,
      "p@5": 1.0,
      "mrr": 0.875,
      "miss_rate@1": 0.25,
      "latency_ms_p50": 268.4465119964443,
      "miss_samples": [
        "session 2026-07-16: ollama keep_alive was 5m so qwen3-embedding unload",
        "product-owned path: ensure_product_ollama downloads bin under plugin d"
      ]
    },
    "backfill_s": 0.427,
    "embedded": 13
  },
  "R2_paraphrase": {
    "label": "R2_paraphrase",
    "n": 6,
    "pure_dense": {
      "n": 6,
      "p@1": 0.6666666666666666,
      "p@5": 1.0,
      "mrr": 0.7777777777777778,
      "miss_rate@1": 0.33333333333333337,
      "latency_ms_p50": 252.75649200193584,
      "miss_samples": [
        "refine_machines must drop hallucinated keys not present in the input c",
        "Instruct line describes session memory retrieval then Query: plus the "
      ]
    },
    "fts_only": {
      "n": 6,
      "p@1": 0.5,
      "p@5": 0.6666666666666666,
      "mrr": 0.5833333333333334,
      "miss_rate@1": 0.5,
      "latency_ms_p50": 1.023473043460399,
      "miss_samples": [
        "TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 stops the embed model from unloading ",
        "searxng MCP prefers 192.168.1.211:8890 then docker then tailscale back",
        "refine_machines must drop hallucinated keys not present in the input c"
      ]
    },
    "hybrid": {
      "n": 6,
      "p@1": 0.6666666666666666,
      "p@5": 1.0,
      "mrr": 0.7777777777777778,
      "miss_rate@1": 0.33333333333333337,
      "latency_ms_p50": 243.09924995759502,
      "miss_samples": [
        "refine_machines must drop hallucinated keys not present in the input c",
        "Instruct line describes session memory retrieval then Query: plus the "
      ]
    }
  },
  "R3_twins": {
    "label": "R3_twins",
    "n": 4,
    "pure_dense": {
      "n": 4,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 305.6818959885277,
      "miss_samples": []
    },
    "fts_only": {
      "n": 4,
      "p@1": 0.75,
      "p@5": 1.0,
      "mrr": 0.8333333333333334,
      "miss_rate@1": 0.25,
      "latency_ms_p50": 1.299456984270364,
      "miss_samples": [
        "decision: ollama qwen3-embedding:0.6b is the only dense path; fastembe"
      ]
    },
    "hybrid": {
      "n": 4,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 332.19196303980425,
      "miss_samples": []
    }
  },
  "R4_corrections": {
    "label": "R4_corrections",
    "n": 4,
    "pure_dense": {
      "n": 4,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 269.54150799429044,
      "miss_samples": []
    },
    "fts_only": {
      "n": 4,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 0.8360280189663172,
      "miss_samples": []
    },
    "hybrid": {
      "n": 4,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 262.6560729695484,
      "miss_samples": []
    }
  },
  "R5_symbols": {
    "label": "R5_symbols",
    "n": 5,
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "latency_ms_p50": 232.5014439993538,
      "miss_samples": [
        "product embed model tag is qwen3-embedding:0.6b (1024-d MRL, Q8_0 ~639",
        "ops: nginx restarted on web-01 after certificate rotation"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 0.6300330278463662,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 227.379969030153,
      "miss_samples": []
    }
  },
  "R6_kind": {
    "label": "R6_kind",
    "n": 3,
    "pure_dense": {
      "n": 3,
      "p@1": 0.6666666666666666,
      "p@5": 1.0,
      "mrr": 0.8333333333333334,
      "miss_rate@1": 0.33333333333333337,
      "latency_ms_p50": 249.50545001775026,
      "miss_samples": [
        "decision: ship qwen3-embedding:0.6b; upgrade to 4b only if instruction"
      ]
    },
    "fts_only": {
      "n": 3,
      "p@1": 0.6666666666666666,
      "p@5": 0.6666666666666666,
      "mrr": 0.6666666666666666,
      "miss_rate@1": 0.33333333333333337,
      "latency_ms_p50": 0.6084099877625704,
      "miss_samples": [
        "decision: ship qwen3-embedding:0.6b; upgrade to 4b only if instruction"
      ]
    },
    "hybrid": {
      "n": 3,
      "p@1": 0.6666666666666666,
      "p@5": 1.0,
      "mrr": 0.8333333333333334,
      "miss_rate@1": 0.33333333333333337,
      "latency_ms_p50": 231.82308598188683,
      "miss_samples": [
        "decision: ship qwen3-embedding:0.6b; upgrade to 4b only if instruction"
      ]
    }
  },
  "R7_reject": {
    "label": "R7_reject",
    "n": 3,
    "pure_dense": {
      "n": 3,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 235.75610300758854,
      "miss_samples": []
    },
    "fts_only": {
      "n": 3,
      "p@1": 0.6666666666666666,
      "p@5": 0.6666666666666666,
      "mrr": 0.6666666666666666,
      "miss_rate@1": 0.33333333333333337,
      "latency_ms_p50": 0.6423550075851381,
      "miss_samples": [
        "rejected: publishing total-recall as a pure pip wheel without the plug"
      ]
    },
    "hybrid": {
      "n": 3,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 237.2454369906336,
      "miss_samples": []
    }
  },
  "R8_cwd": {
    "label": "R8_cwd",
    "n": 2,
    "isolation_rate": 1.0,
    "leaks": [],
    "hybrid": {
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "n": 2,
      "miss_rate@1": 0.0,
      "latency_ms_p50": 0,
      "miss_samples": []
    },
    "pure_dense": {
      "p@1": 1.0
    },
    "fts_only": {
      "p@1": null
    }
  },
  "R9_instruct_ab": {
    "web": {
      "n": 14,
      "p@1": 0.5714285714285714,
      "p@5": 0.9285714285714286,
      "mrr": 0.7363945578231291,
      "miss_rate@1": 0.4285714285714286,
      "latency_ms_p50": 0.0,
      "miss_samples": [
        "session 2026-07-16: ollama keep_alive was 5m so qwen3-embedding unload",
        "product-owned path: ensure_product_ollama downloads bin under plugin d",
        "TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 stops the embed model from unloading ",
        "searxng MCP prefers 192.168.1.211:8890 then docker then tailscale back",
        "refine_machines must drop hallucinated keys not present in the input c",
        "Instruct line describes session memory retrieval then Query: plus the "
      ]
    },
    "memory": {
      "n": 14,
      "p@1": 0.5714285714285714,
      "p@5": 0.9285714285714286,
      "mrr": 0.7321428571428571,
      "miss_rate@1": 0.4285714285714286,
      "latency_ms_p50": 0.0,
      "miss_samples": [
        "session 2026-07-16: ollama keep_alive was 5m so qwen3-embedding unload",
        "product-owned path: ensure_product_ollama downloads bin under plugin d",
        "TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 stops the embed model from unloading ",
        "searxng MCP prefers 192.168.1.211:8890 then docker then tailscale back",
        "refine_machines must drop hallucinated keys not present in the input c",
        "Instruct line describes session memory retrieval then Query: plus the "
      ]
    },
    "memory_p@1_delta": 0.0,
    "memory_mrr_delta": -0.0043,
    "memory_wins_or_ties": true
  }
}
```

## LLM
```json
{
  "model": "qwen3.5:2b",
  "n": 6,
  "pass_rate": 1.0,
  "tasks": [
    {
      "name": "extract_standing_ban",
      "pass": true,
      "latency_ms": 1352.2,
      "output": {
        "banned": "rm -rf on an unbraced shell variable",
        "scope": "shell"
      },
      "reasons": []
    },
    {
      "name": "extract_preference_voice",
      "pass": true,
      "latency_ms": 1502.4,
      "output": {
        "preference": "lowercase terse no preamble we framing zero emoji",
        "evidence": "talk like me"
      },
      "reasons": []
    },
    {
      "name": "classify_not_correction",
      "pass": true,
      "latency_ms": 1792.0,
      "output": {
        "is_correction": false,
        "summary": "The user's message is a standard project management instruction regarding testing outcomes and deployment, not a request for correction or verification of facts."
      },
      "reasons": []
    },
    {
      "name": "multi_host_evidence",
      "pass": true,
      "latency_ms": 2598.6,
      "output": {
        "hosts": [
          {
            "name": "gpu-box-3",
            "evidence": "Pulled metrics from"
          },
          {
            "name": "edge-relay",
            "evidence": "restarted caddy on"
          }
        ]
      },
      "reasons": []
    },
    {
      "name": "grounded_null_def",
      "pass": true,
      "latency_ms": 1268.9,
      "output": {
        "term": "blorptree",
        "definition": null
      },
      "reasons": []
    },
    {
      "name": "decision_with_reject",
      "pass": true,
      "latency_ms": 1009.9,
      "output": {
        "chosen": "screen-mcp",
        "rejected": "playwright"
      },
      "reasons": []
    }
  ],
  "production_refine": {
    "machines_ms": 1266.4,
    "kept": [
      "edge-relay",
      "gpu-box-3"
    ],
    "machines_ok": true,
    "precision": true,
    "vocab_ms": 3018.4,
    "definitions": {
      "dense_primary": "A retrieval strategy that prioritizes vector ranking while selectively adding fuzzy text search results to prevent weaker keywords from displacing top-ranked matches.",
      "product ollama": "An Ollama instance that is owned and managed as a separate plugin component within the application's data directory, responsible for serving both embedding and chat models."
    },
    "vocab_ok": true
  },
  "gates": {
    "llm_available": true,
    "json_pass_rate_ge_0.83": true,
    "machines_hosts_kept": true,
    "machines_precision": true,
    "vocab_ok": true,
    "mean_latency_ms_lt_20000": true
  }
}
```

## Gates
- `PASS` retrieval.R1_session_p@1_ge_0.75
- `PASS` retrieval.R1_session_p@5_ge_0.9
- `PASS` retrieval.R2_paraphrase_p@1_ge_0.66
- `PASS` retrieval.R2_paraphrase_p@5_ge_0.85
- `PASS` retrieval.R3_twins_p@1_ge_0.75
- `PASS` retrieval.R4_corrections_p@1_ge_0.75
- `PASS` retrieval.R5_symbols_p@1_ge_0.8
- `PASS` retrieval.R5_hybrid_ge_fts_p@1
- `PASS` retrieval.R6_kind_p@1_ge_0.66
- `PASS` retrieval.R7_reject_p@1_ge_0.66
- `PASS` retrieval.R8_cwd_isolation_ge_1.0
- `PASS` retrieval.R9_memory_instruct_not_worse
- `PASS` retrieval.macro_hybrid_p@1_ge_0.75
- `PASS` llm.llm_available
- `PASS` llm.json_pass_rate_ge_0.83
- `PASS` llm.machines_hosts_kept
- `PASS` llm.machines_precision
- `PASS` llm.vocab_ok
- `PASS` llm.mean_latency_ms_lt_20000

**Overall: PASS** (19/19 gates)
