"""One `evt=llm_call` per model call, into the events log promtail already ships.

WHY THIS FILE EXISTS. On 2026-09-01 the DigitalOcean account's Serverless Inference bill jumped
hard, and two model ids accounted for >96% of the tokens. Answering "which project?" was impossible
because ONE model access key (sha256:9327f186) is shared by seven containers across five projects,
so DigitalOcean sees a single caller and its per-key usage page cannot separate them.

The obvious next move was to ask Loki, which holds the past. It could answer for the sibling project
-- cybergod's engine emits `evt=qwen` per call with the model and both token counts -- and it could
not answer for this one, because NOTHING HERE EMITTED A MODEL EVENT AT ALL:

    electronic.py:104   txt, _usage, _fin = await RC.call_model(...)      <- usage discarded
    llm.py::complete    return data["choices"][0]["message"]["content"]   <- usage never read

The API returns `usage` on every response. We were throwing it away and then had no way to prove
what this service had or had not spent. So Loki showed jobhuntwow's HTTP and security events beside
a silence that looked exactly like a quiet project -- and "I cannot see" rendering the same as
"there is nothing" is the defect that let the sibling's log shipper report success for a week while
shipping an empty archive.

TWO CHOKEPOINTS, ONE EMITTER. `llm.complete()` (backend tasks) and `resume_consensus.call_model()`
(the tailor chain) both post to DO_BASE_URL, so a meter on one of them measures half the spend and
would be worse than none: it would produce a confident number that is wrong. One function, called
from both.

IT MUST NEVER RAISE AND NEVER BLOCK. This is observation. A logging fault taking a user's tailoring
run down would be a far worse outcome than an unattributed dollar.

NOT A BUDGET. This file only records. The sibling project enforces a daily cap inside its own
`_call`; the equivalent for this service is a separate decision with its own blast radius, and
bolting enforcement onto an emitter is how a logging bug becomes an outage.
"""
import json
import os
import time

EVENTS_LOG = os.environ.get("EVENTS_LOG", "")
_EVENTS_LOG_FAILED = []      # first failed append prints once; never raises
SERVICE = os.environ.get("SERVICE", "electronic-backend")

# Per-million-token rates, input and output SEPARATELY. A single blended rate is wrong by a factor
# of three on this workload: DeepSeek 3.2 is $0.425 in and $1.36 out, and our calls are output-heavy
# by contract. The sibling project priced both directions the same for weeks and its "lifetime
# cost" could never be reconciled against a bank statement.
RATES = {
    "deepseek-3.2":      (0.425, 1.36),
    "deepseek-4-flash":  (0.112, 0.224),
    "llama-4-maverick":  (0.20, 0.60),
    "mistral-3-14B":     (0.10, 0.30),
    "gemma-4-31B-it":    (0.10, 0.30),
    "kimi-k2.5":         (0.30, 1.20),
    "kimi-k2.6":         (0.30, 1.20),
    "glm-5.2":           (0.30, 1.20),
    "qwen3-vl-30b":      (0.10, 0.30),
    "nemotron-3-nano-omni": (0.10, 0.30),
}
# AN UNKNOWN MODEL IS PRICED AT THE MOST EXPENSIVE RATE WE KNOW, never an average. An unpriced model
# must not look cheap: the whole point of this file is to notice a caller nobody configured, and
# rounding it down would hide exactly that.
_MAX_IN = max(r[0] for r in RATES.values())
_MAX_OUT = max(r[1] for r in RATES.values())


def rate_for(model):
    """(input, output) USD per million tokens, and whether the model was actually known."""
    m = str(model or "")
    if m in RATES:
        return RATES[m][0], RATES[m][1], True
    for k, v in RATES.items():          # tolerate a snapshot suffix like "-0813"
        if m.startswith(k):
            return v[0], v[1], True
    return _MAX_IN, _MAX_OUT, False


def cost_of(model, tokens_in, tokens_out):
    ri, ro, _known = rate_for(model)
    return round((int(tokens_in or 0) / 1e6) * ri + (int(tokens_out or 0) / 1e6) * ro, 6)


def _write(payload):
    line = json.dumps(payload)
    try:
        print(line, flush=True)
    except Exception:
        pass
    # STDOUT IS NOT ENOUGH. promtail tails the FILE; it does not read this container's stdout. The
    # sibling project lost every live assessment from Grafana to exactly this assumption -- it had
    # worked only by accident, because a different container's stdout happened to be scraped.
    if EVENTS_LOG:
        try:
            with open(EVENTS_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as e:
            # Same PermissionError class as telemetry.emit (UID 10001 vs a root-owned file). Say so
            # once on stdout; a metered call that silently never reaches the file is unmetered.
            if not _EVENTS_LOG_FAILED:
                _EVENTS_LOG_FAILED.append(1)
                print(json.dumps({"evt": "events_log_unwritable", "caller": "llm_events",
                                  "path": EVENTS_LOG, "err": repr(e)[:160]}), flush=True)


def record(model, usage, *, caller="", ms=0, status="ok", role="", user=""):
    """Emit one llm_call event. `usage` is the API's own dict; anything else is treated as absent.

    Returns the event dict (handy in tests); never raises.
    """
    try:
        u = usage if isinstance(usage, dict) else {}
        ti = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        to = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        # ZERO IS A MEASUREMENT; UNKNOWN IS NOT. A streamed response carries no `usage` block
        # unless the request asked for it, so recording 0 there would make a busy client look free
        # and would poison any sum built from these lines. The two must stay distinguishable --
        # the same rule as llm_meter.spent_today() returning None rather than 0.0, and the same one
        # logship broke when "I could not look" rendered identically to "there was nothing".
        known_tokens = bool(u)
        _ri, _ro, known = rate_for(model)
        ev = {"evt": "llm_call", "service": SERVICE, "ts": int(time.time()),
              "model": str(model or "")[:60], "caller": str(caller or "")[:40],
              "role": str(role or "")[:24], "tokens_in": ti, "tokens_out": to,
              "cost_usd": cost_of(model, ti, to) if known_tokens else 0.0,
              "tokens_known": known_tokens, "priced": bool(known),
              "ms": int(ms or 0), "status": str(status or "")[:24]}
        if user:
            ev["user"] = str(user)[:120]
        if not known:
            # A model we have no rate for is the shape of the 2026-09-01 incident: two ids that
            # appear in no configuration anywhere. Say so on the line itself, so a Loki query can
            # find it without knowing in advance what to look for.
            ev["unknown_model"] = True
        _write(ev)
        return ev
    except Exception:
        return None
