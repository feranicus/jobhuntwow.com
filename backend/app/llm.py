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
    "content":  "deepseek-v4-pro",   # resume / cover prose (quality)
    "content_fb": "deepseek-3.2",
    "chat":     "deepseek-3.2",
    "vision":   "nemotron-nano-12b-v2-vl",
}


def model_for(role: str) -> str:
    """Resolve a role name to a concrete DO model slug (env can override each role)."""
    env = os.getenv(f"JHW_MODEL_{role.upper()}")
    if env:
        return env
    return DEFAULT_MODELS.get(role, DEFAULT_MODELS["content"])


def routing_table() -> dict:
    return {r: model_for(r) for r in DEFAULT_MODELS}


def _headers():
    return {"Authorization": f"Bearer {DO_KEY}", "Content-Type": "application/json"}


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
