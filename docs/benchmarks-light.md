# Local CPU benchmarks

Measured: 2026-10-12T21:10:30.733639+00:00

Physical cores: 6; RAM: 15.3 GiB.

4096 context, 160 output tokens, temperature 0, thinking off, GPU off. Each request inspects resident size then unloads the model. No response cache. Twenty short intent tasks with explicit action definitions; not an extraction, forecast-quality, or long-context evaluation.

| Model | First valid | After repair | Correct intent | Prompt tok/s | Generation tok/s | Peak Ollama RSS GiB | Resident estimate GiB | Peak system used GiB | Median call s |
|---|---|---|---|---|---|---|---|---|---|
| qwen3:1.7b | 20/20 | 20/20 | 20/20 | 108.1 | 11.9 | 1.93 | 1.75 | 11.38 | 8.6 |
| qwen3:4b | 20/20 | 20/20 | 20/20 | 42.6 | 6.1 | 3.13 | 2.97 | 12.35 | 16.2 |

RSS sums accessible Ollama processes and may double-count shared pages; system memory includes other desktop applications. Sampling interval: 100 ms. Throughput is median API-reported evaluation speed, excluding model loading.
Resident size is an Ollama estimate, not peak RSS. RSS below the resident estimate may reflect incomplete sampling.

The intent-v2 prompt explicitly defines all five actions. Do not compare accuracy directly with the earlier undefined-label benchmark.

Detailed model metadata:

```json
[
  {
    "model": "qwen3:1.7b",
    "suite_version": "intent-v2",
    "details": {
      "parent_model": "",
      "format": "gguf",
      "family": "qwen3",
      "families": [
        "qwen3"
      ],
      "parameter_size": "2.0B",
      "quantization_level": "Q4_K_M"
    },
    "license_apache": true,
    "cases": 20,
    "first_valid": 20,
    "valid": 20,
    "correct": 20,
    "prompt_tps": 108.11782346537365,
    "generation_tps": 11.889229284895059,
    "ollama_peak_rss_gb": 1.9265174865722656,
    "ollama_resident_estimate_gb": 1.7531445296481252,
    "peak_system_used_gb": 11.380714416503906,
    "median_wall_seconds": 8.611707250005566
  },
  {
    "model": "qwen3:4b",
    "suite_version": "intent-v2",
    "details": {
      "parent_model": "",
      "format": "gguf",
      "family": "qwen3",
      "families": [
        "qwen3"
      ],
      "parameter_size": "4.0B",
      "quantization_level": "Q4_K_M"
    },
    "license_apache": true,
    "cases": 20,
    "first_valid": 20,
    "valid": 20,
    "correct": 20,
    "prompt_tps": 42.56045124336855,
    "generation_tps": 6.138024535785936,
    "ollama_peak_rss_gb": 3.1292381286621094,
    "ollama_resident_estimate_gb": 2.965332029387355,
    "peak_system_used_gb": 12.348594665527344,
    "median_wall_seconds": 16.16059205000056
  }
]
```
