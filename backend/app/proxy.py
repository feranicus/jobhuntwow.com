"""OpenAI-compatible proxy in front of DO Serverless Inference.

The local jhw-agent (browser-use) points its base_url here instead of at DO directly, so:
  - your DO billing key NEVER leaves the droplet,
  - every agent LLM call is logged/observable server-side,
  - the agent can address models by ROLE alias (jhw-driver, jhw-vision, ...) and we route them.

Endpoints (mounted at /v1):
  POST /v1/chat/completions   -> forwards to DO, streaming or not
  GET  /v1/models             -> pass-through list

Auth: the agent must send `Authorization: Bearer <AGENT_PROXY_TOKEN>` (a per-deployment shared
token, NOT the DO key). Set AGENT_PROXY_TOKEN in the droplet env.
"""
import json
import os

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .settings import DO_BASE_URL, DO_KEY
from .llm import model_for, DEFAULT_MODELS

router = APIRouter(prefix="/v1", tags=["proxy"])

AGENT_PROXY_TOKEN = os.getenv("AGENT_PROXY_TOKEN", "")

# Agents may ask for a role alias; we map "jhw-<role>" -> concrete DO slug.
ROLE_ALIASES = {f"jhw-{r}": r for r in DEFAULT_MODELS}


def _check_auth(authorization: str | None):
    if not AGENT_PROXY_TOKEN:
        raise HTTPException(503, "AGENT_PROXY_TOKEN not configured on server")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != AGENT_PROXY_TOKEN:
        raise HTTPException(401, "bad agent token")


def _resolve_model(requested: str) -> str:
    if requested in ROLE_ALIASES:
        return model_for(ROLE_ALIASES[requested])
    return requested  # already a real DO slug


def _do_headers():
    return {"Authorization": f"Bearer {DO_KEY}", "Content-Type": "application/json"}


@router.get("/models")
async def models(authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{DO_BASE_URL}/models", headers=_do_headers())
    # advertise our role aliases alongside the real ids
    body = r.json()
    body.setdefault("data", [])
    for alias in ROLE_ALIASES:
        body["data"].append({"id": alias, "object": "model", "owned_by": "jobhuntwow"})
    return JSONResponse(body, status_code=r.status_code)


@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    payload = await request.json()
    payload["model"] = _resolve_model(payload.get("model", "jhw-driver"))
    stream = bool(payload.get("stream"))

    if not stream:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{DO_BASE_URL}/chat/completions",
                             headers=_do_headers(), json=payload)
        return JSONResponse(r.json(), status_code=r.status_code)

    async def gen():
        # RAW passthrough: preserve DO's exact SSE bytes and framing. Reframing line-by-line and
        # dropping blank lines breaks the SSE event separators, so OpenAI-SSE clients (Hermes) read
        # an empty stream with no finish_reason. Never reframe SSE by hand.
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{DO_BASE_URL}/chat/completions",
                                headers=_do_headers(), json=payload) as r:
                async for chunk in r.aiter_raw():
                    yield chunk
    return StreamingResponse(gen(), media_type="text/event-stream")
