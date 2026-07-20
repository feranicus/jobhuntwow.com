# Picking a main / backup / 3rd LLM — methodology + tooling

Two scripts, two questions:

- **`probe_models.py`** — *which models can this key even reach, and which return valid output fast?*
  Lists the catalog, live-probes a shortlist, and prints a recommended **vendor-diverse chain**.
- **`compare_models.py`** — *which model writes the better answer on MY real task?*
  Runs the SAME prompt through several models and prints the outputs side by side with latency,
  tokens and cost.

Both are standalone: any OpenAI-compatible endpoint (DigitalOcean serverless, OpenAI, OpenRouter,
Together, Groq, Fireworks, local vLLM/Ollama), no SSH, no Docker, no project imports.

```bash
export LLM_BASE_URL="https://inference.do-ai.run/v1"   # any OpenAI-compatible /v1
export LLM_API_KEY="sk-..."
python probe_models.py --list                          # catalog only, ~1s
python probe_models.py                                 # probe + recommend a chain
python compare_models.py --task prompt.txt --models a,b,c --json-contract --require summary,items
```

---

## The 8 rules this tooling encodes (learned the hard way in production)

**1. Don't pick "the best model" — pick a chain, and make the backup a different VENDOR.**
A 429 or an outage is almost always provider-wide. `deepseek → another deepseek` is the same failure
twice. Three vendors = three independent failure domains. `probe_models.py` recommends the fastest
*passing* model **per vendor** for exactly this reason.

**2. Visibility ≠ entitlement.** A model showing up in `/v1/models` does **not** mean your key/tier
can call it. On the account this was built for, 74 models were *visible* but every `anthropic-*` and
commercial `openai-*` id returned **HTTP 403** — only the open-weight ids (and `gpt-oss-*`) actually
answered. The only truth is a live call. That is the entire point of probing instead of reading a
spec sheet.

**3. A 429 is an ACCOUNT problem, not a model problem.** Rate-limit / empty-balance errors can't be
fixed by retrying the same model — only by a different vendor, or a quota/balance change. If *every*
candidate is 429, stop swapping models and go check your billing/limits.

**4. Never put a reasoning / "thinking" / distill model in a strict-JSON chain.** They emit
chain-of-thought first, then run out of tokens and truncate the JSON. `*-distill`, `*-thinking`,
`*-reasoner`, `o1`, `o3` are skipped by default. Use **instruct** models for structured output.

**5. Latency ranking INVERTS between a toy probe and your real prompt.** In one measured case a model
was 3.3s on a one-line probe but **44.6s** on the real 10k-char prompt — slower than the "slow" head
model at 25s. **Never choose on a synthetic probe.** Use `probe_models.py` to find what's *reachable
and valid*, then `compare_models.py --task <your real prompt>` to choose the winner.

**6. Parsing ≠ answering.** A model returned `{}` in 4s — it parsed as valid JSON, looked like
"success", and shipped an empty deliverable. Always validate the *content*, not just that JSON
parsed: require the fields you actually need (`--require summary,items`) and reject answers with a
trivially small completion-token count.

**7. Models don't always return the shape you asked for.** Expect: a bare array instead of an object,
a `[{...}]` wrapper around your object, trailing prose after the JSON, ```json fences. Your
production parser must tolerate all of these — `compare_models.py`'s `parse_json()` shows the minimum
(find first `{`…last `}`, fall back to `[`…`]`).

**8. Cheapest + fastest is NOT the win condition.** For anything a human reads, a credible answer
wins. Read the full outputs the way your end user will. In the original bake-off, the cheap fast
model produced factually-accurate-but-generic prose, while the "slow" model named the specific facts
— and *also* hallucinated an identifier. Neither was strictly "better"; you only learn that by
reading the artifact, which is why `compare_models.py` prints it in full.

---

## `probe_models.py` — reference

| flag | meaning |
|---|---|
| `--list` | print the catalog only (1 API call, instant) |
| `--models a,b,c` | probe exactly these ids (default: an auto open-weight shortlist) |
| `--task FILE` | probe with YOUR prompt instead of the built-in demo |
| `--lang de` | also score whether the output actually came back in German |
| `--timeout 90` | per-call seconds — slow models legitimately need 60–90s |
| `--top 3` | how many models to recommend in the chain |
| `--json` | machine-readable results |
| `--base` / `--key` | override endpoint/key without env vars |

Output = catalog grouped by vendor → a probe table (status / ms / $ / note) → a recommended chain:

```
  MAIN    deepseek-3.2              deepseek     12400ms  ...
  BACKUP  llama-4-maverick         llama         3300ms  ...
  3RD     openai-gpt-oss-120b      openai(oss)   ...
  ENRICH_MODELS="deepseek-3.2,llama-4-maverick,openai-gpt-oss-120b"
```

## `compare_models.py` — reference

| flag | meaning |
|---|---|
| `--task FILE` | **required** — your real prompt |
| `--models a,b,c` | **required** — the models to compare |
| `--json-contract` | request + validate JSON output |
| `--require k1,k2` | JSON keys that MUST be present (catches empty/partial answers) |
| `--out DIR` | save every raw answer for inspection |
| `--timeout 180` | per-call seconds |

Output = each model's full answer, then a scoreboard (ms / tokens / cost / JSON-ok / missing-fields).

## Pricing

Both scripts show an approximate `$/1M` blended cost from a built-in hint table. To get real numbers,
drop a `prices.json` next to the scripts:

```json
{ "deepseek-3.2": 0.28, "llama-4-maverick": 0.20, "openai-gpt-oss-120b": 0.30 }
```

Keys are matched as substrings against the model id; unknown models show `0`.

## Wiring the chosen chain into an app

The pattern the originals use: read `ENRICH_MODELS="a,b,c"` from env, try each in order, and on
failure fail over to the next. Give the **head** the biggest slice of your time budget (it's the one
you want to win), floor each attempt so a backup always has runtime, and validate the *content* of
each answer before accepting it. See `enrich.colt-original` references for a battle-tested version.

## Files in this folder

- `probe_models.py`, `compare_models.py` — **portable, use these in any project.**
- `*.colt-original.py` — the originals from the Colt project (SSH + docker exec into a specific
  droplet, import `enrich.py`). Kept only as reference for the failover/budget/validation logic.
