# Model license record

Inspected locally through Ollama `/api/show` on 2026-10-12.

| Tag | Parameters | Quantization | License observed |
|---|---|---|---|
| `qwen3:4b` | 4.0B | Q4_K_M | Apache-2.0 |
| `qwen3:1.7b` | 2.0B reported by local Ollama metadata | Q4_K_M | Apache-2.0 |
| `qwen3:8b` | 8.2B | Q4_K_M | Apache-2.0 |

The [Qwen3 upstream repository](https://github.com/QwenLM/Qwen3) provides the
model-family release and license information. Local `/api/show` license text was
also inspected for each installed tag. Tags can change; re-check license and
metadata when updating weights. The benchmark report records quantization and
size, while the Ollama installation retains the local model manifests.

This is an initial model record, not a completed dependency license audit.
Python dependency versions are locked in `uv.lock`; recording their licenses
remains part of the project's dependency review.
