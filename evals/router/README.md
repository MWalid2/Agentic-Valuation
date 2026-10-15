# Router evaluation set

`prompts.jsonl` holds 50 first messages with the expected answer for each, written
on 2026-10-15 before the Router was implemented. "Today" is fixed at 2026-10-15
so relative dates are reproducible.

| Field | Meaning |
|---|---|
| `status` | `resolved` (one company to confirm) or `ambiguous` (the user must choose) |
| `ticker` | The resolved ticker; for `ambiguous`, a ticker that must be among the candidates |
| `date` | Expected valuation date, or `reject` (future date) |
| `intent` | I1-I11, or `null` when the message gives only a company and a date |
| `scope` | `out` for banks, insurers and REITs; checked only for resolved companies |

A prompt passes only when every field matches. Coverage: tickers, full and partial
names, misspellings, several share classes, preferred-stock listings sharing a
company ID, a renamed successor registrant, a spin-off with a similar name,
relative and written dates, future dates, all eleven intents, out-of-scope
companies, and free-form sentences.

```powershell
uv run python scripts/evaluate_router.py                 # rules + local model
uv run python scripts/evaluate_router.py --no-model      # rules only
uv run python scripts/evaluate_router.py --model-intent  # model labels every question
```

SEC listings and company profiles are fetched (or replayed from the cache); the
model is `qwen3:1.7b` through Ollama.
