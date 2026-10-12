# 10. What Intent Means and How We Benchmark It

An **intent** is the action the user wants the assistant to perform. It is not
the company name, financial metric, or topic mentioned in the message. The Router
uses intent to decide which components must run.

## The five labels in this benchmark

| Intent | Criterion | Example | Project behavior |
|---|---|---|---|
| `data` | Retrieve a reported/historical financial figure | "Show the latest cash balance" | Fetch eligible data and cite it |
| `wacc` | Estimate or choose cost of capital, discount rate, or beta method | "Which beta should we use?" | Run cost-of-capital components |
| `valuation` | Calculate intrinsic/fair value or build a DCF model | "What is fair value per share?" | Run the valuation pipeline |
| `audit` | Trace the source of an existing value or assumption | "Which filing supports this lease liability?" | Retrieve provenance/evidence |
| `modify` | Explicitly change an assumption | "Set operating margin to 25%" | Validate an edit, invalidate dependent results, and rerun |

Classify the requested action rather than a keyword. Mentioning a tax rate does
not automatically mean WACC. Asking for its source is `audit`; explicitly setting
it is `modify`. Asking about beta selection is `wacc`.

Some messages have several possible intentions, such as "Check this tax rate."
A production Router should use context or ask for clarification. This sample
avoids such deliberately ambiguous requests; multi-intent and follow-up behavior
are not tested.

## Relationship to the project's eleven intents

The architecture defines I1–I11. This benchmark uses a smaller diagnostic subset:

| Benchmark label | Closest architecture intents |
|---|---|
| `data` | I1 data snapshot; some I2 historical requests |
| `wacc` | I3 cost of capital |
| `valuation` | I6 quick valuation and I9 full model |
| `audit` | I10 audit/explain provenance |
| `modify` | I11 modification |

It does not separately classify I4 assumption rationale, I5 scenarios, I7
sensitivities, or I8 comps. It also does not distinguish quick/full outputs or
test entity/date resolution. This is not the roadmap's full Router acceptance set.

## Fair comparison procedure

`scripts/benchmark_llm.py` now uses the versioned `intent-v2` suite. Both
`qwen3:1.7b` and `qwen3:4b` receive the same explicit label definitions, twenty
questions, JSON schema, and generation settings. There are four questions for
each label. Expected labels were defined before running either model.

The original baseline did not explain the label meanings adequately. Its
semantic scores cannot fairly be compared with this revised prompt. The original
report remains in `docs/benchmarks.md`; revised results go to
`docs/benchmarks-light.md`. No 8B inference is used in this comparison.

```powershell
uv run python scripts/benchmark_llm.py qwen3:1.7b qwen3:4b
```

Inference is CPU-only with six physical threads, 4096 context, 160 output tokens,
temperature zero, and thinking disabled. Response caching is bypassed. Each
request temporarily retains its model for a resident-size inspection, then
explicitly unloads it before the next call. Models run sequentially.

## What the scores mean

| Metric | Criterion |
|---|---|
| First-pass validity | Initial output parses as JSON and satisfies the exact Pydantic schema |
| Valid after repair | Output is valid after at most one schema repair retry |
| Correct intent | A validated output's label exactly matches the preassigned expected label |
| Intent accuracy | Correct labels divided by all twenty questions, including failures |
| Prompt speed | Median API-reported input tokens divided by prompt-evaluation seconds |
| Generation speed | Median API-reported output tokens divided by generation seconds |
| Median call time | Median wall time including generation, repair if needed, resident inspection, and unloading |
| Process RSS | Sampled total accessible Ollama/llama working set; may omit processes or double-count shared pages |
| Resident estimate | Largest size reported by Ollama for the tested resident model; not a measured process peak |
| System memory | Peak measured system usage, including the user's other applications |

Schema-valid JSON can contain the wrong label. Conversely, the right word in
malformed JSON is unusable until validation succeeds. Semantic mistakes do not
trigger repair, since the expected answer is evaluation ground truth, not
information available to the application during real requests.

The schema requires an allowed intent and a nonempty rationale of at most 180
characters, with no extra fields. The rationale is not independently scored for
evidence quality; intent accuracy measures only label agreement.

Raw case records include question, expected label, predicted label, validity,
attempts, and latency. Summaries and cases are under the ignored directory
`data/benchmark-runs/intent-v2/data/benchmarks/`. Different suite versions retain
separate results. The revised script regenerates the lighter-model report.

## What this can and cannot tell us

Good results support using a model for these clearly defined selection tasks.
They do not establish filing-number extraction, accounting judgment, forecast
quality, rationale grounding, or production accuracy. Twenty questions are a
small development set used to refine the prompt, not a held-out generalization
test. Each question contributes five percentage points to the overall score.

Future role selection should include grounded filing snippets and unseen prompts,
plus end-to-end memory with the full app. Running the benchmark does not change
role assignments. After reviewing these results, the configuration was separately
updated to use only 1.7B and 4B; see [the configuration guide](07-configuration.md).

## Measured results: 2026-10-12

| Model | First-pass JSON | Correct intent | Generation tokens/s | Peak process RSS GiB | Peak system usage GiB | Median call seconds |
|---|---|---|---|---|---|---|
| `qwen3:1.7b` | 20/20 | 20/20 | 11.9 | 1.93 | 11.38 | 8.6 |
| `qwen3:4b` | 20/20 | 20/20 | 6.1 | 3.13 | 12.35 | 16.2 |

Both models classified all four examples of each action correctly. Neither needed
a schema repair. Both were unloaded afterward. RSS covers sampled accessible
Ollama/llama processes; system usage includes the desktop and other applications.
The machine has 15.3 GiB physical RAM. Ollama's separate resident-size estimates
were 1.75 GiB and 2.97 GiB respectively.

For this small set, 1.7B achieved the same observed correctness at approximately
half the call latency and lower memory use. It is a provisional lightweight
Router candidate. This does not establish accuracy across all eleven project
intents or show which model is better at financial interpretation. The revised
4B memory sample includes resident inspection before unloading.

See [benchmarks-light.md](benchmarks-light.md) for full metrics and metadata.
