# LLM refine bakeoff (total-recall chat model)

> **Card-aligned sampling (2026-07-18 research):**
> - **qwen3.5:** non-thinking structured `temp=0.7 top_p=0.8 top_k=20 presence_penalty=1.5 think=false`
> - **gemma4:** JSON refine `temp=0.1 top_p=0.95 top_k=64 think=false` (not free-form card 1.0)
> - **MTP:** stock Linux GGUF tags have **no** MTP meta; Gemma4 MTP is MLX/Apple-first in Ollama
> - **Ollama floor:** ≥0.31.2 for `think:false`+`format` on all thinking parsers (this host may be older)

Generated: 2026-07-18 00:47:04 -0400
Ollama daemon: `0.30.10`

## Recommendation

```json
{
  "default": "qwen3.5:2b",
  "cpu_default": "qwen3.5:2b",
  "gpu_default": "qwen3.5:2b",
  "reason": "top composite on primary device: qwen3.5:2b score=8.0",
  "embed_unchanged": "qwen3-embedding:0.6b"
}
```

## Ranking

### GPU

| rank | model | score | json | mach F1 | def_cov | echo | mach ms | vocab ms |
|------|-------|-------|------|---------|---------|------|---------|----------|
| 1 | `qwen3.5:2b` | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 1223.8 | 2717.3 |
| 2 | `gemma4:e4b-it-qat` | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 51923.1 | 68226.4 |
| 3 | `gemma4:12b-it-qat` | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 101239.0 | 168191.4 |
| 4 | `gemma4:e2b-it-qat` | 6.1666 | 1.0 | 1.0 | 0.3333 | 0.5 | 34691.3 | 40746.9 |

### CPU

| rank | model | score | json | mach F1 | def_cov | echo | mach ms | vocab ms |
|------|-------|-------|------|---------|---------|------|---------|----------|
| 1 | `qwen3.5:2b` | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 21019.4 | 29564.0 |
| 2 | `gemma4:12b-it-qat` | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 86776.5 | 120872.0 |
| 3 | `gemma4:e4b-it-qat` | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 28781.3 | 41687.4 |
| 4 | `gemma4:e2b-it-qat` | 6.1666 | 1.0 | 1.0 | 0.3333 | 0.5 | 13803.1 | 20788.5 |

## MTP / speculative

```json
{
  "qwen3.5:2b": {
    "mtp_present_in_gguf_meta": false,
    "mtp_metadata_keys": [],
    "note": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags."
  },
  "gemma4:e2b-it-qat": {
    "mtp_present_in_gguf_meta": false,
    "mtp_metadata_keys": [],
    "note": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags."
  },
  "gemma4:e4b-it-qat": {
    "mtp_present_in_gguf_meta": false,
    "mtp_metadata_keys": [],
    "note": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags."
  },
  "gemma4:12b-it-qat": {
    "mtp_present_in_gguf_meta": false,
    "mtp_metadata_keys": [],
    "note": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags."
  }
}
```

## Full rows

```json
[
  {
    "model": "qwen3.5:2b",
    "device": "gpu",
    "available": true,
    "meta": {
      "family": "qwen35",
      "parameter_size": "2.3B",
      "quantization": "Q8_0",
      "capabilities": [
        "completion",
        "vision",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.7,
        "top_k": 20,
        "top_p": 0.8,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 3476.1,
      "latency_ms_p50": 1001.0,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 15748.4,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1001.0,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 968.0,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 940.7,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 1199.9,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 998.4,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 1223.8,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A system that initializes and loads specific servers or skills for AI coding ses",
        "project_key": "A mechanism that reorganizes git worktrees to consolidate memory usage back into",
        "sharechain": "A sequence of cryptographic shares used in p2pool to verify miner payouts.",
        "xyzzy": null
      },
      "define_coverage": 1.0,
      "echo_rate": 0.0,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 2717.3
    },
    "composite_score": 8.0,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:e2b-it-qat",
    "device": "gpu",
    "available": true,
    "meta": {
      "family": "gemma4",
      "parameter_size": "4.6B",
      "quantization": "Q4_0",
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.1,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 19989.8,
      "latency_ms_p50": 21956.2,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 11469.3,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 22508.7,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 21288.5,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 21956.2,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 23274.8,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 19441.1,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 34691.3,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": null,
        "project_key": "A key that consolidates git worktree cwds back to the main repository root to al",
        "sharechain": "The connected sequence of miner shares within p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.3333,
      "echo_rate": 0.5,
      "non_null": 2,
      "xyzzy_null_ok": true,
      "ms": 40746.9
    },
    "composite_score": 6.1666,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:e4b-it-qat",
    "device": "gpu",
    "available": true,
    "meta": {
      "family": "gemma4",
      "parameter_size": "7.5B",
      "quantization": "Q4_0",
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.1,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 29265.0,
      "latency_ms_p50": 30020.0,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 27605.1,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 29101.0,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 30020.0,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 31715.9,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 31882.9,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 25264.9,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 51923.1,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component that loads necessary servers and skills for specific AI sessions.",
        "project_key": "A mechanism that consolidates git worktree changes back to the main repository r",
        "sharechain": "The connected sequence of shares belonging to a miner in p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.6667,
      "echo_rate": 0.3333,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 68226.4
    },
    "composite_score": 7.0001,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:12b-it-qat",
    "device": "gpu",
    "available": true,
    "meta": {
      "family": "gemma4",
      "parameter_size": "11.9B",
      "quantization": "Q4_0",
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.1,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 48634.8,
      "latency_ms_p50": 49913.1,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 65734.9,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 46556.9,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 46734.7,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 49986.6,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 49913.1,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 32882.8,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 101239.0,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component that initializes and provides MCP servers and skills for Claude Code",
        "project_key": "A mechanism that maps multiple git worktree directories back to a single reposit",
        "sharechain": "A sequence of miner shares linked together within a p2pool system.",
        "xyzzy": null
      },
      "define_coverage": 0.6667,
      "echo_rate": 0.3333,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 168191.4
    },
    "composite_score": 7.0001,
    "num_gpu_env": "999"
  },
  {
    "model": "qwen3.5:2b",
    "device": "cpu",
    "available": true,
    "meta": {
      "family": "qwen35",
      "parameter_size": "2.3B",
      "quantization": "Q8_0",
      "capabilities": [
        "completion",
        "vision",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.7,
        "top_k": 20,
        "top_p": 0.8,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 7774.6,
      "latency_ms_p50": 7558.5,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 15082.5,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 7558.5,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 5904.1,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 6436.2,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 8392.7,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 3273.6,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 21019.4,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component that manages the loading and configuration of specific servers or sk",
        "project_key": "A system variable used to consolidate git worktrees into a repository root, ther",
        "sharechain": "In p2pool networks, a sequence of miner shares linked together to facilitate ver",
        "xyzzy": null
      },
      "define_coverage": 1.0,
      "echo_rate": 0.0,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 29564.0
    },
    "composite_score": 8.0,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:e2b-it-qat",
    "device": "cpu",
    "available": true,
    "meta": {
      "family": "gemma4",
      "parameter_size": "4.6B",
      "quantization": "Q4_0",
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.1,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 6868.7,
      "latency_ms_p50": 5633.7,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 16348.4,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 6258.1,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 5633.7,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 5581.0,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 4500.6,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 2890.6,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 13803.1,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": null,
        "project_key": "A function that merges git worktree cwds back to the main repository root to poo",
        "sharechain": "The connected sequence of miner shares within p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.3333,
      "echo_rate": 0.5,
      "non_null": 2,
      "xyzzy_null_ok": true,
      "ms": 20788.5
    },
    "composite_score": 6.1666,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:e4b-it-qat",
    "device": "cpu",
    "available": true,
    "meta": {
      "family": "gemma4",
      "parameter_size": "7.5B",
      "quantization": "Q4_0",
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.1,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 11305.9,
      "latency_ms_p50": 9077.4,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 26614.5,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 8132.9,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 8229.0,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 9077.4,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 10601.5,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 5180.0,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 28781.3,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component that loads necessary servers and skills for specific AI sessions.",
        "project_key": "A mechanism that reverts git worktree changes to the main repository root to con",
        "sharechain": "The connected sequence of shares used in p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.6667,
      "echo_rate": 0.3333,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 41687.4
    },
    "composite_score": 7.0001,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:12b-it-qat",
    "device": "cpu",
    "available": true,
    "meta": {
      "family": "gemma4",
      "parameter_size": "11.9B",
      "quantization": "Q4_0",
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "mtp_metadata_keys": [],
      "mtp_present_in_gguf_meta": false,
      "thinking_cap": true,
      "tools_cap": true,
      "ollama_requires": null,
      "notes": "Linux GGUF tags rarely ship MTP heads; Gemma4 MTP in Ollama is primarily MLX/Apple (ollama \u22650.31). Qwen3.5 MTP needs special GGUF + llama.cpp draft-mtp \u2014 not stock library tags.",
      "sampling_profile": {
        "temperature": 0.1,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "seed": 42,
        "think": false
      }
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 23548.0,
      "latency_ms_p50": 22883.5,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 39141.1,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 20936.2,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 22535.3,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 23881.1,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 22883.5,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 11910.7,
          "reason": []
        }
      ]
    },
    "machines": {
      "kept": [
        "cache-02",
        "db-primary",
        "web-01"
      ],
      "precision": 1.0,
      "recall": 1.0,
      "f1": 1.0,
      "kept_real_hosts": true,
      "dropped_noise": true,
      "ms": 86776.5,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component responsible for loading the necessary servers and skills for specifi",
        "project_key": "A mechanism that maps multiple git worktree directories to a single repository r",
        "sharechain": "A sequence of miner shares that are linked together within a p2pool.",
        "xyzzy": null
      },
      "define_coverage": 1.0,
      "echo_rate": 0.0,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 120872.0
    },
    "composite_score": 8.0,
    "num_gpu_env": "0"
  }
]
```

