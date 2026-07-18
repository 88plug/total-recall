# Adversarial 10× eval

Generated: 2026-07-17 21:39:37 -0400

**n_pairs=432** seeds=84 docs=206 model=`qwen3-embedding:0.6b`

## Overall
```json
{
  "pure_dense": {
    "n": 432,
    "p@1": 0.5972,
    "p@5": 0.8171,
    "mrr": 0.6902,
    "miss_rate@1": 0.4028,
    "miss_samples": [
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: only search queries get Instruct/Query prefix; documents emb",
      "decision: only search queries get Instruct/Query prefix; documents emb"
    ]
  },
  "fts_only": {
    "n": 432,
    "p@1": 0.5532,
    "p@5": 0.7847,
    "mrr": 0.6588,
    "miss_rate@1": 0.4468,
    "miss_samples": [
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol"
    ]
  },
  "hybrid": {
    "n": 432,
    "p@1": 0.6296,
    "p@5": 0.8773,
    "mrr": 0.7365,
    "miss_rate@1": 0.3704,
    "miss_samples": [
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
      "decision: only search queries get Instruct/Query prefix; documents emb",
      "decision: only search queries get Instruct/Query prefix; documents emb"
    ]
  }
}
```

## Worst families (by hybrid miss@1)
```json
[
  {
    "family": "asymmetric_instruct",
    "miss_rate@1": 1.0,
    "p@1": 0.0
  },
  {
    "family": "num_ctx_llm",
    "miss_rate@1": 1.0,
    "p@1": 0.0
  },
  {
    "family": "searxng",
    "miss_rate@1": 1.0,
    "p@1": 0.0
  },
  {
    "family": "sources_10",
    "miss_rate@1": 1.0,
    "p@1": 0.0
  },
  {
    "family": "chat_2b",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "dim_1024",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "embed_model_tag",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "exactish",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "format_v2",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "null_def",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "vec_opt_out",
    "miss_rate@1": 0.8,
    "p@1": 0.2
  },
  {
    "family": "anti_echo",
    "miss_rate@1": 0.6,
    "p@1": 0.4
  },
  {
    "family": "argocd",
    "miss_rate@1": 0.6,
    "p@1": 0.4
  },
  {
    "family": "batch_cap",
    "miss_rate@1": 0.6,
    "p@1": 0.4
  },
  {
    "family": "dense_primary",
    "miss_rate@1": 0.6,
    "p@1": 0.4
  }
]
```

## Gates
- `PASS` n_pairs_ge_400
- `PASS` hybrid_p@1_ge_0.55
- `PASS` hybrid_p@5_ge_0.8
- `PASS` hybrid_mrr_ge_0.65
- `PASS` hybrid_ge_dense_p@1
- `PASS` hybrid_ge_fts_p@1
- `PASS` hybrid_best_of_three_p@1
- `PASS` hybrid_best_of_three_mrr
- `PASS` miss_rate_le_0.45
- `PASS` symbol_hybrid_p@1_ge_0.75
- `PASS` dense_beats_random

**Overall: PASS** (11/11)

<details><summary>Per-family metrics</summary>

```json
{
  "alembic": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: schema changes go through alembic revisions"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: schema changes go through alembic revisions",
        "decision: schema changes go through alembic revisions",
        "decision: schema changes go through alembic revisions"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: schema changes go through alembic revisions",
        "decision: schema changes go through alembic revisions"
      ]
    }
  },
  "anti_echo": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4222,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.3,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip",
        "decision: reject definitions that are near-verbatim copies of the snip"
      ]
    }
  },
  "argocd": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: argocd syncs manifests to the cluster on merge to main",
        "decision: argocd syncs manifests to the cluster on merge to main",
        "decision: argocd syncs manifests to the cluster on merge to main"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: argocd syncs manifests to the cluster on merge to main",
        "decision: argocd syncs manifests to the cluster on merge to main"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: argocd syncs manifests to the cluster on merge to main",
        "decision: argocd syncs manifests to the cluster on merge to main",
        "decision: argocd syncs manifests to the cluster on merge to main"
      ]
    }
  },
  "asymmetric_instruct": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.0,
      "mrr": 0.0333,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.54,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.4,
      "mrr": 0.225,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb",
        "decision: only search queries get Instruct/Query prefix; documents emb"
      ]
    }
  },
  "asyncpg": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: standardize on asyncpg for all postgres access not psycopg2"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: standardize on asyncpg for all postgres access not psycopg2",
        "decision: standardize on asyncpg for all postgres access not psycopg2",
        "decision: standardize on asyncpg for all postgres access not psycopg2"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: standardize on asyncpg for all postgres access not psycopg2"
      ]
    }
  },
  "author_88plug": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: author is 88plug with email andrew@88plug.com in plugin mani"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: author is 88plug with email andrew@88plug.com in plugin mani"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: author is 88plug with email andrew@88plug.com in plugin mani"
      ]
    }
  },
  "bash_ops": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: all ops scripts assume bash not zsh or fish"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: all ops scripts assume bash not zsh or fish",
        "decision: all ops scripts assume bash not zsh or fish"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: all ops scripts assume bash not zsh or fish",
        "decision: all ops scripts assume bash not zsh or fish"
      ]
    }
  },
  "batch_cap": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6333,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.8,
      "mrr": 0.4917,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho",
        "decision: refine batches cap around 25 entities so 2b list fidelity ho"
      ]
    }
  },
  "batch_embed": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7333,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed batch default and num_batch 512 for throughput on 0.6b",
        "decision: embed batch default and num_batch 512 for throughput on 0.6b"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed batch default and num_batch 512 for throughput on 0.6b",
        "decision: embed batch default and num_batch 512 for throughput on 0.6b"
      ]
    }
  },
  "bearer": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: all endpoints require a bearer token from the auth service"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: all endpoints require a bearer token from the auth service",
        "decision: all endpoints require a bearer token from the auth service"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: all endpoints require a bearer token from the auth service",
        "decision: all endpoints require a bearer token from the auth service"
      ]
    }
  },
  "celery": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.8667,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: use a task queue with celery workers not threads or asyncio "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: use a task queue with celery workers not threads or asyncio ",
        "decision: use a task queue with celery workers not threads or asyncio "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.85,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: use a task queue with celery workers not threads or asyncio "
      ]
    }
  },
  "chat_2b": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.8,
      "mrr": 0.3767,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.4,
      "mrr": 0.14,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.3952,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under thin"
      ]
    }
  },
  "chunk_size": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.8667,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: chunk_for_embedding defaults around 400 tokens with overlap "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8222,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: chunk_for_embedding defaults around 400 tokens with overlap "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.85,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: chunk_for_embedding defaults around 400 tokens with overlap "
      ]
    }
  },
  "definition_done": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: untested code is broken; prove with real check; persist"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: untested code is broken; prove with real check; persist",
        "standing rule: untested code is broken; prove with real check; persist"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: untested code is broken; prove with real check; persist",
        "standing rule: untested code is broken; prove with real check; persist"
      ]
    }
  },
  "dense_primary": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5333,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.2667,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.5067,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par",
        "decision: hybrid default is dense_primary so weak FTS cannot steal par"
      ]
    }
  },
  "dim_1024": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.6,
      "mrr": 0.2067,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.475,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.3786,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32"
      ]
    }
  },
  "domain_instruct": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.2667,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.65,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web",
        "decision: query instruct is session-memory domain task not generic web"
      ]
    }
  },
  "echo_filter": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.54,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: client rejects outputs that are near-verbatim of input after",
        "decision: client rejects outputs that are near-verbatim of input after",
        "decision: client rejects outputs that are near-verbatim of input after"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4286,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: client rejects outputs that are near-verbatim of input after",
        "decision: client rejects outputs that are near-verbatim of input after",
        "decision: client rejects outputs that are near-verbatim of input after"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.5667,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: client rejects outputs that are near-verbatim of input after",
        "decision: client rejects outputs that are near-verbatim of input after",
        "decision: client rejects outputs that are near-verbatim of input after"
      ]
    }
  },
  "embed_model_tag": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.2,
      "mrr": 0.1,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.42,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.3786,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB"
      ]
    }
  },
  "english_instruct": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.45,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: embed instruct task text is English even for non-English cor",
        "decision: embed instruct task text is English even for non-English cor",
        "decision: embed instruct task text is English even for non-English cor"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.64,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed instruct task text is English even for non-English cor",
        "decision: embed instruct task text is English even for non-English cor"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed instruct task text is English even for non-English cor",
        "decision: embed instruct task text is English even for non-English cor"
      ]
    }
  },
  "env_example_ban": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "ban: .env.example must contain placeholders only; real secrets live in"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "exactish": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.2,
      "mrr": 0.2,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.2917,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.2667,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense ",
        "decision: exactish FTS promote when FTS top is phrase match and dense "
      ]
    }
  },
  "force_push_ban": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "format_v2": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.3733,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 1.0,
      "mrr": 0.5,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 1.0,
      "mrr": 0.4833,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces r"
      ]
    }
  },
  "four_ds": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: Four Ds filter \u2014 Dumb Dangerous Difficult Different \u2014 a",
        "standing rule: Four Ds filter \u2014 Dumb Dangerous Difficult Different \u2014 a"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: Four Ds filter \u2014 Dumb Dangerous Difficult Different \u2014 a"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: Four Ds filter \u2014 Dumb Dangerous Difficult Different \u2014 a"
      ]
    }
  },
  "fsl_license": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: license is FSL-1.1-ALv2 on the plugin"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: license is FSL-1.1-ALv2 on the plugin"
      ]
    }
  },
  "gha": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.3,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.4,
      "mrr": 0.4,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push",
        "decision: github actions builds and tests every push"
      ]
    }
  },
  "ghcr": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: push containers to ghcr not docker hub"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.3333,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: push containers to ghcr not docker hub",
        "decision: push containers to ghcr not docker hub",
        "decision: push containers to ghcr not docker hub",
        "decision: push containers to ghcr not docker hub"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: push containers to ghcr not docker hub"
      ]
    }
  },
  "gpu_num": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.75,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: ollama options set num_gpu 999 to offload all layers when GP",
        "decision: ollama options set num_gpu 999 to offload all layers when GP"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: ollama options set num_gpu 999 to offload all layers when GP",
        "decision: ollama options set num_gpu 999 to offload all layers when GP"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.85,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: ollama options set num_gpu 999 to offload all layers when GP"
      ]
    }
  },
  "harness_def": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "in our setup harness means the Claude Code / Grok plugin runner not li"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "in our setup harness means the Claude Code / Grok plugin runner not li"
      ]
    }
  },
  "hooks_timeout": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.64,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: async re-index hooks keep timeout 60; fast hooks use 88plug ",
        "decision: async re-index hooks keep timeout 60; fast hooks use 88plug "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: async re-index hooks keep timeout 60; fast hooks use 88plug "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: async re-index hooks keep timeout 60; fast hooks use 88plug ",
        "decision: async re-index hooks keep timeout 60; fast hooks use 88plug "
      ]
    }
  },
  "json_retry": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.64,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: generate_json doubles num_predict once on JSONDecodeError fr",
        "decision: generate_json doubles num_predict once on JSONDecodeError fr"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: generate_json doubles num_predict once on JSONDecodeError fr",
        "decision: generate_json doubles num_predict once on JSONDecodeError fr"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: generate_json doubles num_predict once on JSONDecodeError fr"
      ]
    }
  },
  "k8s": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8286,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: we run everything on kubernetes in production; docker-compos"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: we run everything on kubernetes in production; docker-compos",
        "decision: we run everything on kubernetes in production; docker-compos"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: we run everything on kubernetes in production; docker-compos",
        "decision: we run everything on kubernetes in production; docker-compos"
      ]
    }
  },
  "keep_alive": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.6667,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.6,
      "mrr": 0.2686,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.6167,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whol"
      ]
    }
  },
  "kind_boost": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: dense re-rank boosts correction ban decision over domain_fac",
        "decision: dense re-rank boosts correction ban decision over domain_fac"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "kiss": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6286,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: KISS \u2014 if you cannot explain in one sentence simplify",
        "standing rule: KISS \u2014 if you cannot explain in one sentence simplify"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: KISS \u2014 if you cannot explain in one sentence simplify",
        "standing rule: KISS \u2014 if you cannot explain in one sentence simplify"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: KISS \u2014 if you cannot explain in one sentence simplify"
      ]
    }
  },
  "last_token_pool": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: qwen3-embedding uses last-token pool not mean pool; L2 norma",
        "decision: qwen3-embedding uses last-token pool not mean pool; L2 norma"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "launchdarkly": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: launchdarkly owns all runtime feature toggles; no ad-hoc env",
        "decision: launchdarkly owns all runtime feature toggles; no ad-hoc env"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "lexical_rerank": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: hybrid re-ranks candidates by cosine plus token coverage aft"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: hybrid re-ranks candidates by cosine plus token coverage aft"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "llm_opt_out": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.31,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.62,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.55,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not"
      ]
    }
  },
  "loki": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: ship structured logs to self-hosted loki via promtail never "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: ship structured logs to self-hosted loki via promtail never "
      ]
    }
  },
  "machines_fewshot": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5222,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: refine_machines few-shot drops Monday and asyncpg while keep",
        "decision: refine_machines few-shot drops Monday and asyncpg while keep",
        "decision: refine_machines few-shot drops Monday and asyncpg while keep"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.8,
      "mrr": 0.5,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: refine_machines few-shot drops Monday and asyncpg while keep",
        "decision: refine_machines few-shot drops Monday and asyncpg while keep",
        "decision: refine_machines few-shot drops Monday and asyncpg while keep",
        "decision: refine_machines few-shot drops Monday and asyncpg while keep"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: refine_machines few-shot drops Monday and asyncpg while keep",
        "decision: refine_machines few-shot drops Monday and asyncpg while keep"
      ]
    }
  },
  "match_scope": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.84,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: match scope to what was asked; no drive-by refactors wi"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: match scope to what was asked; no drive-by refactors wi"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8222,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: match scope to what was asked; no drive-by refactors wi"
      ]
    }
  },
  "mcp_live_enum": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "mcp_tools_count": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands",
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.7,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands",
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands",
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands",
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands"
      ]
    }
  },
  "modernbert_ban": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 1.0,
      "mrr": 0.4833,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.6167,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama"
      ]
    }
  },
  "mtp": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6333,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: qwen3.5:2b ships mtp.* tensors for multi-token prediction on",
        "decision: qwen3.5:2b ships mtp.* tensors for multi-token prediction on",
        "decision: qwen3.5:2b ships mtp.* tensors for multi-token prediction on"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "nats": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: services talk over nats jetstream not rabbitmq or kafka for "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5286,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: services talk over nats jetstream not rabbitmq or kafka for ",
        "decision: services talk over nats jetstream not rabbitmq or kafka for ",
        "decision: services talk over nats jetstream not rabbitmq or kafka for "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "null_def": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.4,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: vocab refine returns null definition when snippet is only th",
        "decision: vocab refine returns null definition when snippet is only th",
        "decision: vocab refine returns null definition when snippet is only th",
        "decision: vocab refine returns null definition when snippet is only th"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: vocab refine returns null definition when snippet is only th"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.8,
      "mrr": 0.5,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: vocab refine returns null definition when snippet is only th",
        "decision: vocab refine returns null definition when snippet is only th",
        "decision: vocab refine returns null definition when snippet is only th",
        "decision: vocab refine returns null definition when snippet is only th"
      ]
    }
  },
  "num_ctx_embed": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.665,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed options set num_ctx 8192 not the full 32k window",
        "decision: embed options set num_ctx 8192 not the full 32k window"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.62,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed options set num_ctx 8192 not the full 32k window",
        "decision: embed options set num_ctx 8192 not the full 32k window"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: embed options set num_ctx 8192 not the full 32k window",
        "decision: embed options set num_ctx 8192 not the full 32k window"
      ]
    }
  },
  "num_ctx_llm": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.0,
      "mrr": 0.0286,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.6667,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.8,
      "mrr": 0.375,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs no"
      ]
    }
  },
  "oauth_cookie": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "session note: OAuth callback state mismatch after SameSite cookie chan"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "session note: OAuth callback state mismatch after SameSite cookie chan"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "session note: OAuth callback state mismatch after SameSite cookie chan"
      ]
    }
  },
  "operator_voice": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: speak-like-operator skill matches lowercase terse we-framing",
        "decision: speak-like-operator skill matches lowercase terse we-framing"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "privacy_local": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.6,
      "mrr": 0.4,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: transcripts never leave the machine; refine prompts stay loc",
        "decision: transcripts never leave the machine; refine prompts stay loc",
        "decision: transcripts never leave the machine; refine prompts stay loc",
        "decision: transcripts never leave the machine; refine prompts stay loc"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7333,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: transcripts never leave the machine; refine prompts stay loc",
        "decision: transcripts never leave the machine; refine prompts stay loc"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.725,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: transcripts never leave the machine; refine prompts stay loc",
        "decision: transcripts never leave the machine; refine prompts stay loc"
      ]
    }
  },
  "product_ollama": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6833,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: managed ollama binary lives under plugin data bin; system PA",
        "decision: managed ollama binary lives under plugin data bin; system PA"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6333,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: managed ollama binary lives under plugin data bin; system PA",
        "decision: managed ollama binary lives under plugin data bin; system PA"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.69,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: managed ollama binary lives under plugin data bin; system PA",
        "decision: managed ollama binary lives under plugin data bin; system PA"
      ]
    }
  },
  "project_key": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "pytest": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: pytest is the only supported test runner"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.7,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: pytest is the only supported test runner",
        "decision: pytest is the only supported test runner",
        "decision: pytest is the only supported test runner"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.8667,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: pytest is the only supported test runner"
      ]
    }
  },
  "qwen_sampler": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.4667,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.8,
      "mrr": 0.4333,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5222,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 pr"
      ]
    }
  },
  "rebuild_identity": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.62,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: change of model backend or dim forces dense rebuild; query i",
        "decision: change of model backend or dim forces dense rebuild; query i"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.4667,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: change of model backend or dim forces dense rebuild; query i",
        "decision: change of model backend or dim forces dense rebuild; query i",
        "decision: change of model backend or dim forces dense rebuild; query i"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: change of model backend or dim forces dense rebuild; query i",
        "decision: change of model backend or dim forces dense rebuild; query i"
      ]
    }
  },
  "redis_cache": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: redis fronts the read-heavy queries"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: redis fronts the read-heavy queries",
        "decision: redis fronts the read-heavy queries"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.6667,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: redis fronts the read-heavy queries",
        "decision: redis fronts the read-heavy queries",
        "decision: redis fronts the read-heavy queries"
      ]
    }
  },
  "refute_first": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.725,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: try to refute a finding before trusting it; default to ",
        "standing rule: try to refute a finding before trusting it; default to "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: try to refute a finding before trusting it; default to "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.75,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: try to refute a finding before trusting it; default to ",
        "standing rule: try to refute a finding before trusting it; default to "
      ]
    }
  },
  "reuse_before_build": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "standing rule: reuse-before-build; do not parallel invent when existin",
        "standing rule: reuse-before-build; do not parallel invent when existin",
        "standing rule: reuse-before-build; do not parallel invent when existin"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.8667,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: reuse-before-build; do not parallel invent when existin"
      ]
    }
  },
  "rm_ban": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: never propose rm -rf on unbraced $VAR; empty expands to file"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.54,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: never propose rm -rf on unbraced $VAR; empty expands to file",
        "decision: never propose rm -rf on unbraced $VAR; empty expands to file",
        "decision: never propose rm -rf on unbraced $VAR; empty expands to file"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: never propose rm -rf on unbraced $VAR; empty expands to file"
      ]
    }
  },
  "rrf_k": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default",
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default",
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default",
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default"
      ]
    }
  },
  "ruff": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.6,
      "mrr": 0.5286,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: run ruff for linting and formatting drop black never reintro",
        "decision: run ruff for linting and formatting drop black never reintro",
        "decision: run ruff for linting and formatting drop black never reintro"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "schema_format": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6622,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: pass full JSON Schema as format for constrained decode not f",
        "decision: pass full JSON Schema as format for constrained decode not f"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: pass full JSON Schema as format for constrained decode not f"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7167,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: pass full JSON Schema as format for constrained decode not f",
        "decision: pass full JSON Schema as format for constrained decode not f"
      ]
    }
  },
  "screen_mcp": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 1.0,
      "mrr": 0.4833,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: screen-mcp is the only path for the operator real Firefox se",
        "decision: screen-mcp is the only path for the operator real Firefox se",
        "decision: screen-mcp is the only path for the operator real Firefox se",
        "decision: screen-mcp is the only path for the operator real Firefox se"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "searxng": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.8,
      "mrr": 0.2567,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.6,
      "mrr": 0.2917,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.8,
      "mrr": 0.3833,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then ",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then "
      ]
    }
  },
  "sentry": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.85,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: exceptions are reported to sentry in prod; do not email stac"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 0.8,
      "mrr": 0.8,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: exceptions are reported to sentry in prod; do not email stac"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.84,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: exceptions are reported to sentry in prod; do not email stac"
      ]
    }
  },
  "session_start": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7333,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: SessionStart emits operator context signpost for this cwd",
        "decision: SessionStart emits operator context signpost for this cwd"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6867,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: SessionStart emits operator context signpost for this cwd",
        "decision: SessionStart emits operator context signpost for this cwd"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: SessionStart emits operator context signpost for this cwd"
      ]
    }
  },
  "sources_10": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.6,
      "mrr": 0.2639,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 1.0,
      "mrr": 0.5667,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 1.0,
      "mrr": 0.3667,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code ",
        "decision: mines transcripts from 10 CLI clients including Claude Code "
      ]
    }
  },
  "subagent_hook": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g",
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 1.0,
      "mrr": 0.7,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g",
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g",
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g",
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects g"
      ]
    }
  },
  "subagent_review": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.8667,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: do not review your own work in the same context; spawn "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: do not review your own work in the same context; spawn ",
        "standing rule: do not review your own work in the same context; spawn "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: do not review your own work in the same context; spawn "
      ]
    }
  },
  "symbol": {
    "pure_dense": {
      "n": 12,
      "p@1": 0.5833,
      "p@5": 0.75,
      "mrr": 0.6681,
      "miss_rate@1": 0.4167,
      "miss_samples": [
        "ops: nginx restarted on web-01 after certificate rotation",
        "ops: web-02 still serves canary traffic on port 8443",
        "product embed model tag is qwen3-embedding:0.6b",
        "DEFAULT_MODEL for refine is qwen3.5:2b",
        "vec_meta format=2 is ollama-only dense"
      ]
    },
    "fts_only": {
      "n": 12,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 12,
      "p@1": 0.75,
      "p@5": 1.0,
      "mrr": 0.875,
      "miss_rate@1": 0.25,
      "miss_samples": [
        "ops: nginx restarted on web-01 after certificate rotation",
        "ops: web-02 still serves canary traffic on port 8443",
        "DEFAULT_MODEL for refine is qwen3.5:2b"
      ]
    }
  },
  "think_false": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.7,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: generate_json sets think false so qwen does not emit think b",
        "decision: generate_json sets think false so qwen does not emit think b"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.55,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: generate_json sets think false so qwen does not emit think b",
        "decision: generate_json sets think false so qwen does not emit think b",
        "decision: generate_json sets think false so qwen does not emit think b"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.75,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: generate_json sets think false so qwen does not emit think b",
        "decision: generate_json sets think false so qwen does not emit think b"
      ]
    }
  },
  "truncate": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "upgrade_4b": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.69,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy ",
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.4,
      "mrr": 0.3222,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy ",
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy ",
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy ",
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6786,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy ",
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy "
      ]
    }
  },
  "user_prompt_submit": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6733,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: UserPromptSubmit runs decide_and_format for on-demand memory",
        "decision: UserPromptSubmit runs decide_and_format for on-demand memory"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: UserPromptSubmit runs decide_and_format for on-demand memory"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.8,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: UserPromptSubmit runs decide_and_format for on-demand memory",
        "decision: UserPromptSubmit runs decide_and_format for on-demand memory"
      ]
    }
  },
  "uv": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.9,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "decision: use uv for installs and lockfiles not pip or poetry"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: use uv for installs and lockfiles not pip or poetry",
        "decision: use uv for installs and lockfiles not pip or poetry"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    }
  },
  "vault": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.6,
      "mrr": 0.2667,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i",
        "decision: store credentials in vault never in env files; vault agent i"
      ]
    }
  },
  "vec_opt_out": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.0,
      "p@5": 0.8,
      "mrr": 0.3952,
      "miss_rate@1": 1.0,
      "miss_samples": [
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 0.8,
      "mrr": 0.5,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.2,
      "p@5": 1.0,
      "mrr": 0.5,
      "miss_rate@1": 0.8,
      "miss_samples": [
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only rema"
      ]
    }
  },
  "verify_before": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.6333,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "standing rule: verify before announce; mark provisional when unconfirm",
        "standing rule: verify before announce; mark provisional when unconfirm",
        "standing rule: verify before announce; mark provisional when unconfirm"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6286,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: verify before announce; mark provisional when unconfirm",
        "standing rule: verify before announce; mark provisional when unconfirm"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.62,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "standing rule: verify before announce; mark provisional when unconfirm",
        "standing rule: verify before announce; mark provisional when unconfirm",
        "standing rule: verify before announce; mark provisional when unconfirm"
      ]
    }
  },
  "vite": {
    "pure_dense": {
      "n": 5,
      "p@1": 1.0,
      "p@5": 1.0,
      "mrr": 1.0,
      "miss_rate@1": 0.0,
      "miss_samples": []
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6222,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: migrated the web app bundler to vite from webpack",
        "decision: migrated the web app bundler to vite from webpack"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 1.0,
      "mrr": 0.7667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: migrated the web app bundler to vite from webpack",
        "decision: migrated the web app bundler to vite from webpack"
      ]
    }
  },
  "weighted_rrf": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.4,
      "p@5": 0.8,
      "mrr": 0.575,
      "miss_rate@1": 0.6,
      "miss_samples": [
        "decision: weighted_rrf mode available with dense weight default 3x FTS",
        "decision: weighted_rrf mode available with dense weight default 3x FTS",
        "decision: weighted_rrf mode available with dense weight default 3x FTS"
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.6,
      "mrr": 0.6,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: weighted_rrf mode available with dense weight default 3x FTS",
        "decision: weighted_rrf mode available with dense weight default 3x FTS"
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6722,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "decision: weighted_rrf mode available with dense weight default 3x FTS",
        "decision: weighted_rrf mode available with dense weight default 3x FTS"
      ]
    }
  },
  "white_hat": {
    "pure_dense": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.8667,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: white hat engineering; no shortcuts that bypass safety "
      ]
    },
    "fts_only": {
      "n": 5,
      "p@1": 0.6,
      "p@5": 0.8,
      "mrr": 0.6667,
      "miss_rate@1": 0.4,
      "miss_samples": [
        "standing rule: white hat engineering; no shortcuts that bypass safety ",
        "standing rule: white hat engineering; no shortcuts that bypass safety "
      ]
    },
    "hybrid": {
      "n": 5,
      "p@1": 0.8,
      "p@5": 1.0,
      "mrr": 0.85,
      "miss_rate@1": 0.2,
      "miss_samples": [
        "standing rule: white hat engineering; no shortcuts that bypass safety "
      ]
    }
  }
}
```

</details>
