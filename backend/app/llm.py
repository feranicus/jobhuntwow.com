"""Role -> model routing for DigitalOcean Serverless Inference (OpenAI-compatible).

One place decides which DO model serves which task. Ids are exact DO slugs (see
https://docs.digitalocean.com/products/inference/details/models/) and are overridable via env,
so swapping a role's model is a one-line change with no code edits.
"""
import os
import httpx
from .settings import DO_BASE_URL, DO_KEY

# role -> DO model slug (env override: JHW_MODEL_<ROLE>)
DEFAULT_MODELS = {
    # CHOSEN FROM EVIDENCE (compare_models bake-off on our real action-JSON task, 2026-07):
    # all candidates passed the JSON contract, so latency + vendor-diversity decided (MODEL_SELECTION.md).
    # deepseek-3.2 805ms · mistral-3-14B 570ms · llama-4-maverick 1004ms  (3 vendors, all sub-1.1s)
    # REJECTED for the driver loop: glm-5.2 (27.8s), qwen3.5-397b (91.5s) — far too slow per action.
    # Env override per role: JHW_MODEL_<ROLE>. agent.llm_answer retries down answer/answer2/answer3.
    "answer":   "deepseek-3.2",      # screening answers (main)
    "answer2":  "mistral-3-14B",     # backup (diff vendor)
    "answer3":  "llama-4-maverick",  # 3rd (diff vendor)
    "driver":   "deepseek-3.2",      # generic DOM driver (main)
    "driver2":  "mistral-3-14B",
    "driver3":  "llama-4-maverick",
    "extract":  "deepseek-3.2",      # JD->JSON (reliable)
    "extract_fb": "mistral-3-14B",
    # DEMOTED 2026-08-16. The bot reported, verbatim:
    #   "Model unavailable (tried deepseek-v4-pro, deepseek-3.2, llama-4-maverick)"
    # so v4-pro was the HEAD of this chain and did not answer. CLAUDE.md already records the same
    # class of defect for `deepseek-v4-flash`: a marketing name is not an API model id, and every
    # call to a phantom id burns a round-trip before the fallback runs. deepseek-3.2 is measured
    # here (805ms, contract-valid), so it leads until `python jhw.py probe` proves v4-pro exists.
    "content":  "deepseek-3.2",      # resume / cover prose (measured, contract-valid)
    "content_fb": "mistral-3-14B",   # backup: DIFFERENT vendor, a 429 is provider-wide
    "chat":     "deepseek-3.2",
    # FAST role: one browser action per call, so wall-clock per call beats prose quality.
    # Measured on the real driver task: mistral-3-14B 570ms < deepseek-3.2 805ms < maverick 1004ms.
    # NOT openai-gpt-oss-120b — it returns HTTP 429 on this account (see CLAUDE.md bake-off).
    # MEASURED 2026-07 by `python jhw.py probe` against the real driver contract (66 models,
    # fastest-of-2, must return schema-valid JSON *and* pick the right element):
    #   mistral-3-14B 614ms · nemotron-nano-12b-v2-vl 722ms · deepseek-v4-pro 871ms ·
    #   nemotron-3-nano-omni 881ms · deepseek-3.2 905ms
    # Every anthropic-* and commercial openai-gpt-* returned 403 on this account (entitlement,
    # not availability). Backup is a DIFFERENT VENDOR on purpose: a 429/outage is provider-wide.
    "fast":     "mistral-3-14B",
    "fast2":    "nemotron-nano-12b-v2-vl",
    "vision":   "nemotron-nano-12b-v2-vl",
    # THE SPOKESPERSON. A FOURTH model whose only job is language: turn the machine state into one
    # human message, and turn the candidate's free-text reply back into a structured intent for the
    # three deciders. Requested explicitly ("add kimi 2.6 like we have in cybergod.ai") and it is the
    # right shape: the three deciders vote on ACTIONS, this one only ever writes and reads WORDS.
    # kimi-k2.6 is the fourth VENDOR (Moonshot), so a 429 on DeepSeek/Mistral/Meta cannot mute it.
    # MEASURED CONSTRAINTS, from CLAUDE.md's bake-off: kimi rejects `response_format` outright and
    # demands a specific temperature -- the API says which in the 400 body, so proxy.py reads it
    # rather than hardcoding a guess.
    "speak":    "kimi-k2.6",
}

# WHAT TO FALL BACK TO WHEN A ROLE'S CONFIGURED SLUG IS NOT IN THE LIVE CATALOG.
# MEASURED 2026-08-16 23:31, verbatim from the run:
#     HTTP 404: {"error":{"message":"model not found","type":"not_found_error"}}
# `kimi-k2.6` is not a slug this account can call, so the spokesperson was DEAD for the whole run and
# every ask it should have phrased fell back to the deterministic text. That is the THIRD time a
# phantom id has cost a run here -- `deepseek-v4-flash` came off a pricing page, `deepseek-v4-pro`
# off a chain head -- and each time the fix was to correct one constant, which fixes one instance and
# not the class. A model id is an EXTERNAL FACT that changes without telling us, so it has to be
# CHECKED, not remembered. `verified_model_for()` asks the catalog once, caches, and walks this
# ladder; every fallback is a DIFFERENT VENDOR from the role it is covering, because a 429 or a
# withdrawal is provider-wide.
FALLBACK_LADDER = {
    "speak":   ["kimi-k2.5", "glm-5.2", "mistral-3-14B", "deepseek-3.2"],
    "vision":  ["nemotron-3-nano-omni", "qwen3-vl-30b", "deepseek-3.2"],
    "fast2":   ["nemotron-3-nano-omni", "mistral-3-14B"],
}
_DEFAULT_LADDER = ["deepseek-3.2", "mistral-3-14B", "llama-4-maverick"]

_CATALOG: set = set()          # live ids, fetched once per process
_RESOLVED: dict = {}           # role -> the slug we PROVED answers
_CATALOG_TRIED = [False]


def model_for(role: str) -> str:
    """Resolve a role name to a concrete DO model slug (env can override each role)."""
    env = os.getenv(f"JHW_MODEL_{role.upper()}")
    if env:
        return env
    return DEFAULT_MODELS.get(role, DEFAULT_MODELS["content"])


async def catalog() -> set:
    """The ids this account can actually name, fetched once. An empty set means we could not ask."""
    if _CATALOG_TRIED[0]:
        return _CATALOG
    _CATALOG_TRIED[0] = True
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(f"{DO_BASE_URL}/models",
                            headers={"Authorization": f"Bearer {DO_KEY}"})
            r.raise_for_status()
            _CATALOG.update(str(m.get("id")) for m in (r.json().get("data") or []))
    except Exception as e:                                    # pragma: no cover - network
        print(f"[llm] could not read the model catalog ({type(e).__name__}) — "
              f"role slugs will be used as configured", flush=True)
    return _CATALOG


async def verified_model_for(role: str) -> str:
    """A slug for this role that the CATALOG SAYS EXISTS — or the honest configured one.

    FAILS OPEN, deliberately and in both directions: if the catalog cannot be read we return the
    configured slug unchanged (a network blip must never silently reroute every role to a different
    model), and if nothing on the ladder exists either we still return the configured slug so the
    caller gets the real 404 rather than a lie. What it removes is the case that actually bit us: a
    slug that has never existed, failing on every call, silently, for a whole run."""
    if role in _RESOLVED:
        return _RESOLVED[role]
    # AN EXPLICIT OVERRIDE IS NOT A GUESS. If the operator has set JHW_MODEL_<ROLE> they are naming
    # a model on purpose -- possibly one DO has just added and this cache has not seen -- and
    # silently substituting something else would make the override look broken. Same reasoning as
    # passing a concrete slug through untouched. (My own selftest caught this: I was overriding the
    # override.)
    env = os.getenv(f"JHW_MODEL_{role.upper()}")
    if env:
        _RESOLVED[role] = env
        return env
    want = model_for(role)
    ids = await catalog()
    if not ids or want in ids:
        _RESOLVED[role] = want
        return want
    for alt in FALLBACK_LADDER.get(role, []) + _DEFAULT_LADDER:
        if alt in ids:
            print(f"[llm] ROLE '{role}' is configured as '{want}', which is NOT in this account's "
                  f"catalog ({len(ids)} ids) — using '{alt}'. Fix DEFAULT_MODELS or set "
                  f"JHW_MODEL_{role.upper()}.", flush=True)
            _RESOLVED[role] = alt
            return alt
    print(f"[llm] ROLE '{role}' -> '{want}' is not in the catalog and NOTHING on its ladder is "
          f"either; passing it through so the real error is visible.", flush=True)
    _RESOLVED[role] = want
    return want


def routing_table() -> dict:
    return {r: model_for(r) for r in DEFAULT_MODELS}


def _headers():
    return {"Authorization": f"Bearer {DO_KEY}", "Content-Type": "application/json"}


class LLMHTTPError(Exception):
    """An HTTP error from the inference endpoint that CARRIES THE RESPONSE BODY.

    `raise_for_status()` throws the body away, and that body is the field which says
    "temperature must be 0.6 for this model" / "max_tokens cannot be set when response_format
    type is 'json_object'". Discarding it is how a healthy, entitled model gets written off as
    broken. A 4xx is the server telling you what it wants: keep it and act on what it named.
    """

    def __init__(self, status: int, body: str = ""):
        self.status = int(status)
        self.body = str(body or "")
        super().__init__("HTTP %d: %s" % (self.status, self.body[:300]))


async def chat(payload: dict, *, timeout: float = 120.0) -> dict:
    """POST an EXPLICIT OpenAI-compatible payload and return the parsed response.

    `complete()` resolves a ROLE to a model and hides the payload, which is right for single-shot
    tasks. The consensus tailor needs the opposite: it names an EXACT model per call (author vs
    auditor must be different models), it must send per-model required parameters, and it must SEE
    the error body to repair a 400 by what the server named. So the payload is built by the caller
    (`resume_consensus.build_payload`, which is pure and therefore testable) and this function is
    only the transport.
    """
    if not DO_KEY:
        raise RuntimeError("DO_INFERENCE_KEY is not set on the server")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{DO_BASE_URL}/chat/completions", headers=_headers(), json=payload)
    if r.status_code >= 400:
        raise LLMHTTPError(r.status_code, (r.text or "")[:800])
    return r.json()


async def complete(role: str, messages: list[dict], *, temperature: float = 0.3,
                   max_tokens: int | None = None, json_mode: bool = False) -> str:
    """Non-streaming completion for backend tasks (tailoring, extraction, field mapping)."""
    if not DO_KEY:
        raise RuntimeError("DO_INFERENCE_KEY is not set on the server")
    payload: dict = {
        "model": model_for(role),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{DO_BASE_URL}/chat/completions", headers=_headers(), json=payload)
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"]
