# LLM refine bakeoff (total-recall chat model)

Generated: 2026-07-18 01:25:57 -0400
Ollama daemon: `0.32.1` (product-embedded binary, not system PATH)

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

| rank | model | mtp | score | json | mach F1 | def_cov | echo | mach ms | vocab ms |
|------|-------|-----|-------|------|---------|---------|------|---------|----------|
| 1 | `qwen3.5:2b` | on | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 1496.9 | 3601.7 |
| 2 | `gemma4:e4b-it-qat` | on | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 1697.9 | 3533.4 |
| 3 | `gemma4:12b-it-qat` | on | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 2504.6 | 8013.4 |
| 4 | `gemma4:e2b-it-qat` | on | 6.1666 | 1.0 | 1.0 | 0.3333 | 0.5 | 1277.6 | 2703.7 |

### CPU

| rank | model | mtp | score | json | mach F1 | def_cov | echo | mach ms | vocab ms |
|------|-------|-----|-------|------|---------|---------|------|---------|----------|
| 1 | `qwen3.5:2b` | on | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 16820.7 | 28870.5 |
| 2 | `gemma4:12b-it-qat` | on | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 72460.5 | 100095.7 |
| 3 | `gemma4:e4b-it-qat` | on | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 26082.0 | 35853.3 |
| 4 | `gemma4:e2b-it-qat` | on | 6.1666 | 1.0 | 1.0 | 0.3333 | 0.5 | 14235.1 | 19619.2 |

## Gemma4 without MTP

Same harness with `OLLAMA_MLX_MTP_*_DRAFT_TOKENS=0`. Stock Linux GGUF tags lack MTP heads.

| device | model | score | json | mach F1 | def_cov | echo | mach ms | vocab ms |
|--------|-------|-------|------|---------|---------|------|---------|----------|
| cpu | `gemma4:12b-it-qat` | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 9673.4 | 41044.4 |
| cpu | `gemma4:e4b-it-qat` | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 4477.4 | 17143.6 |
| cpu | `gemma4:e2b-it-qat` | 5.0 | 1.0 | 1.0 | 0.0 | 1.0 | 2538.0 | 10158.6 |
| gpu | `gemma4:12b-it-qat` | 8.0 | 1.0 | 1.0 | 1.0 | 0.0 | 2478.9 | 7487.3 |
| gpu | `gemma4:e4b-it-qat` | 7.0001 | 1.0 | 1.0 | 0.6667 | 0.3333 | 1124.4 | 2893.1 |
| gpu | `gemma4:e2b-it-qat` | 6.1666 | 1.0 | 1.0 | 0.3333 | 0.5 | 1017.1 | 2578.5 |
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
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 2439.1,
      "latency_ms_p50": 1640.9,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 7246.4,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 2091.0,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 1487.3,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 1640.9,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 1323.3,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 845.9,
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
      "ms": 1496.9,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A system that initializes and loads specific servers or skills for a coding envi",
        "project_key": "A mechanism that reorganizes git worktrees to consolidate memory usage by return",
        "sharechain": "A linked sequence of cryptographic shares representing miner transactions within",
        "xyzzy": null
      },
      "define_coverage": 1.0,
      "echo_rate": 0.0,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 3601.7
    },
    "composite_score": 8.0,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:e2b-it-qat",
    "device": "gpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 13965.0,
      "latency_ms_p50": 1134.0,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 78789.9,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1154.6,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 991.3,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 1134.0,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 995.8,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 724.1,
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
      "ms": 1277.6,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": null,
        "project_key": "A mechanism that consolidates git worktree cwds back to the main repository root",
        "sharechain": "The connected sequence of miner shares within p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.3333,
      "echo_rate": 0.5,
      "non_null": 2,
      "xyzzy_null_ok": true,
      "ms": 2703.7
    },
    "composite_score": 6.1666,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:e4b-it-qat",
    "device": "gpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 3022.2,
      "latency_ms_p50": 1223.7,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 12578.3,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1186.3,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 1104.5,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 1223.7,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 1226.3,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 814.1,
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
      "ms": 1697.9,
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
      "ms": 3533.4
    },
    "composite_score": 7.0001,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:12b-it-qat",
    "device": "gpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 2972.6,
      "latency_ms_p50": 2128.8,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 8089.9,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1985.4,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 2020.5,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 2491.7,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 2128.8,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 1119.3,
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
      "ms": 2504.6,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component that initializes and provides MCP servers and skills for Claude Code",
        "project_key": "A mechanism that maps various git worktree directories to a single repository ro",
        "sharechain": "A sequence of miner shares linked together within a p2pool system.",
        "xyzzy": null
      },
      "define_coverage": 0.6667,
      "echo_rate": 0.3333,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 8013.4
    },
    "composite_score": 7.0001,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:e2b-it-qat",
    "device": "gpu",
    "mtp": false,
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
      },
      "mtp_requested": false
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 1017.3,
      "latency_ms_p50": 1064.3,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 898.6,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1323.2,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 1064.3,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 1117.1,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 915.5,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 785.5,
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
      "ms": 1017.1,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": null,
        "project_key": "A feature that merges git worktree and cwds back to the main repository root to ",
        "sharechain": "The connected sequence of miner shares within p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.3333,
      "echo_rate": 0.5,
      "non_null": 2,
      "xyzzy_null_ok": true,
      "ms": 2578.5
    },
    "composite_score": 6.1666,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:e4b-it-qat",
    "device": "gpu",
    "mtp": false,
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
      },
      "mtp_requested": false
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 1156.5,
      "latency_ms_p50": 1241.7,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 1086.2,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1192.9,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 1265.0,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 1241.7,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 1270.7,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 882.8,
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
      "ms": 1124.4,
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
      "ms": 2893.1
    },
    "composite_score": 7.0001,
    "num_gpu_env": "999"
  },
  {
    "model": "gemma4:12b-it-qat",
    "device": "gpu",
    "mtp": false,
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
      },
      "mtp_requested": false
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 2102.5,
      "latency_ms_p50": 2174.3,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 2407.5,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 1970.7,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 2062.6,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 2641.3,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 2174.3,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 1358.6,
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
      "ms": 2478.9,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component responsible for loading the necessary servers and skills for specifi",
        "project_key": "A mechanism that maps multiple git worktree directories to a single repository r",
        "sharechain": "A sequence of miner shares linked together within a p2pool system.",
        "xyzzy": null
      },
      "define_coverage": 1.0,
      "echo_rate": 0.0,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 7487.3
    },
    "composite_score": 8.0,
    "num_gpu_env": "999"
  },
  {
    "model": "qwen3.5:2b",
    "device": "cpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 5842.4,
      "latency_ms_p50": 5937.5,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 8265.4,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 7018.3,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 5685.9,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 4795.5,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 5937.5,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 3351.6,
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
      "ms": 16820.7,
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
      "ms": 28870.5
    },
    "composite_score": 8.0,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:e2b-it-qat",
    "device": "cpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 6212.5,
      "latency_ms_p50": 4981.9,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 15510.5,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 4358.0,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 4659.4,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 5318.4,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 4981.9,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 2446.7,
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
      "ms": 14235.1,
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
      "ms": 19619.2
    },
    "composite_score": 6.1666,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:e4b-it-qat",
    "device": "cpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 9974.5,
      "latency_ms_p50": 8690.3,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 21485.9,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 6939.2,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 8132.0,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 9861.2,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 8690.3,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 4738.5,
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
      "ms": 26082.0,
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
      "ms": 35853.3
    },
    "composite_score": 7.0001,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:12b-it-qat",
    "device": "cpu",
    "mtp": true,
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
      },
      "mtp_requested": true
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 23149.6,
      "latency_ms_p50": 22572.8,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 42202.3,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 18359.6,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 23158.1,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 22572.8,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 21238.9,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 11365.8,
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
      "ms": 72460.5,
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
      "ms": 100095.7
    },
    "composite_score": 8.0,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:e2b-it-qat",
    "device": "cpu",
    "mtp": false,
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
      },
      "mtp_requested": false
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 2727.8,
      "latency_ms_p50": 3177.3,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 2572.6,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 3229.6,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 3177.3,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 3295.3,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 2797.8,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 1293.9,
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
      "ms": 2538.0,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": null,
        "project_key": "A key used to consolidate git worktree and cwds back to the main repository root",
        "sharechain": "The connected sequence of miner shares within p2pool.",
        "xyzzy": null
      },
      "define_coverage": 0.0,
      "echo_rate": 1.0,
      "non_null": 2,
      "xyzzy_null_ok": true,
      "ms": 10158.6
    },
    "composite_score": 5.0,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:e4b-it-qat",
    "device": "cpu",
    "mtp": false,
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
      },
      "mtp_requested": false
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 4039.7,
      "latency_ms_p50": 4566.1,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 4179.8,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 4566.1,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 3947.1,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 4950.0,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 5011.1,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 1584.2,
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
      "ms": 4477.4,
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
      "ms": 17143.6
    },
    "composite_score": 7.0001,
    "num_gpu_env": "0"
  },
  {
    "model": "gemma4:12b-it-qat",
    "device": "cpu",
    "mtp": false,
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
      },
      "mtp_requested": false
    },
    "json_tasks": {
      "pass_rate": 1.0,
      "n": 6,
      "passed": 6,
      "latency_ms_mean": 8113.7,
      "latency_ms_p50": 8877.9,
      "tasks": [
        {
          "name": "extract_decision",
          "ok": true,
          "ms": 8958.9,
          "reason": []
        },
        {
          "name": "extract_ban",
          "ok": true,
          "ms": 8877.9,
          "reason": []
        },
        {
          "name": "classify_correction",
          "ok": true,
          "ms": 8378.8,
          "reason": []
        },
        {
          "name": "machine_ner",
          "ok": true,
          "ms": 10263.0,
          "reason": []
        },
        {
          "name": "vocab_def",
          "ok": true,
          "ms": 8389.1,
          "reason": []
        },
        {
          "name": "null_when_missing",
          "ok": true,
          "ms": 3814.6,
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
      "ms": 9673.4,
      "gate_precision_ge_0.7": true
    },
    "vocab": {
      "definitions": {
        "harness": "A component responsible for loading the necessary servers and skills for specifi",
        "project_key": "A mechanism that maps multiple git worktree directories to a single repository r",
        "sharechain": "A sequence of miner shares within a p2pool system.",
        "xyzzy": null
      },
      "define_coverage": 1.0,
      "echo_rate": 0.0,
      "non_null": 3,
      "xyzzy_null_ok": true,
      "ms": 41044.4
    },
    "composite_score": 8.0,
    "num_gpu_env": "0"
  }
]
```

