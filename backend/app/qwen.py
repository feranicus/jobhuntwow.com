"""Thin async client for DigitalOcean Serverless Inference (OpenAI-compatible)."""
import json
import httpx
from .settings import DO_BASE_URL, DO_KEY, QWEN_MODEL, HERMES_SYSTEM

def _headers():
    return {"Authorization": f"Bearer {DO_KEY}", "Content-Type": "application/json"}

def configured() -> bool:
    return bool(DO_KEY)

async def list_models():
    """GET /v1/models -> the models your access key can use."""
    if not DO_KEY:
        return {"error": "DO_INFERENCE_KEY is not set on the server"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{DO_BASE_URL}/models", headers=_headers())
        r.raise_for_status()
        data = r.json()
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return {"models": ids, "default": QWEN_MODEL or (ids[0] if ids else "")}

async def chat_stream(messages, model: str = "", temperature: float = 0.4):
    """Yield text chunks from a streaming chat completion."""
    mdl = model or QWEN_MODEL
    if not DO_KEY:
        yield "⚠️ Server has no DO_INFERENCE_KEY set. Add it in .env and restart the backend."
        return
    if not mdl:
        yield "⚠️ No model selected. Pick one in Connections (it calls /api/models)."
        return
    payload = {
        "model": mdl,
        "messages": [{"role": "system", "content": HERMES_SYSTEM}] + messages,
        "temperature": temperature,
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=None) as c:
        async with c.stream("POST", f"{DO_BASE_URL}/chat/completions",
                            headers=_headers(), json=payload) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode("utf-8", "ignore")
                yield f"⚠️ Inference error {r.status_code}: {body[:300]}"
                return
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0]["delta"].get("content")
                    if delta:
                        yield delta
                except Exception:
                    continue
