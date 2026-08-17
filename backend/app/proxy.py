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

import re
import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .settings import DO_BASE_URL, DO_KEY
from .llm import verified_model_for, model_for, DEFAULT_MODELS

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
    """Map an alias to a real DO slug, tolerating a provider prefix.

    Stagehand (and any Vercel-AI-SDK client) must send a PROVIDER-QUALIFIED name such as
    "openai/jhw-fast" to route through a custom baseURL. Without stripping that prefix the alias
    lookup missed, the raw string went to DO, and DO answered 404 -> Stagehand reported
    "Failed to execute task: Not Found".
    """
    req = (requested or "").strip()
    bare = req.split("/", 1)[1] if "/" in req else req
    for cand in (req, bare):
        if cand in ROLE_ALIASES:
            return model_for(ROLE_ALIASES[cand])
    return bare or req      # a real DO slug (prefix stripped, DO does not know "openai/...")


async def _resolve_model_checked(requested: str) -> str:
    """Same, but a ROLE ALIAS is verified against the live catalog first.

    This is where the phantom-id class dies. `jhw-speak` resolved to `kimi-k2.6`, DO answered
    404 "model not found" on every single call, and the spokesperson was mute for an entire run
    while the log blamed a network error. A caller asking for a ROLE is asking us to choose, so
    choosing something that exists is our job. A caller naming a CONCRETE slug is not asking us to
    second-guess it, and is passed through untouched."""
    req = (requested or "").strip()
    bare = req.split("/", 1)[1] if "/" in req else req
    for cand in (req, bare):
        if cand in ROLE_ALIASES:
            return await verified_model_for(ROLE_ALIASES[cand])
    return bare or req


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
    payload["model"] = await _resolve_model_checked(payload.get("model", "jhw-driver"))
    stream = bool(payload.get("stream"))

    if not stream:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{DO_BASE_URL}/chat/completions",
                             headers=_do_headers(), json=payload)
            # A 4xx IS THE SERVER TELLING YOU WHAT IT WANTS. Retry ONCE, fixing only what it NAMED.
            # MEASURED (CLAUDE.md, the kimi bake-off): this endpoint answers
            #   400 {"message":"temperature must be 0.6 for this model"}
            #   400 "response_format type 'json_object' is not supported for this model"
            #   400 "max_tokens cannot be set when response_format type is 'json_object'"
            # Every one of those names the field. Blanket-stripping fields until something works is
            # how a safeguard gets disabled by accident -- so repair the named field and nothing else.
            if r.status_code in (400, 422):
                body = (r.text or "")[:400]
                fixed, why = dict(payload), []
                m = re.search(r"temperature must be ([0-9.]+)", body, re.I)
                if m:
                    fixed["temperature"] = float(m.group(1)); why.append(f"temperature={m.group(1)}")
                if re.search(r"response_format", body, re.I):
                    fixed.pop("response_format", None); why.append("dropped response_format")
                if re.search(r"max_tokens cannot be set", body, re.I):
                    fixed.pop("max_tokens", None); why.append("dropped max_tokens")
                if re.search(r"chat_template_kwargs", body, re.I):
                    fixed.pop("chat_template_kwargs", None); why.append("dropped chat_template_kwargs")
                if why:
                    print(f"[proxy] {payload.get('model')} refused the request ({body[:120]}) -> "
                          f"retrying with {', '.join(why)}", flush=True)
                    r = await c.post(f"{DO_BASE_URL}/chat/completions",
                                     headers=_do_headers(), json=fixed)
                else:
                    print(f"[proxy] {payload.get('model')} 400 and nothing named to fix: {body[:160]}",
                          flush=True)
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
