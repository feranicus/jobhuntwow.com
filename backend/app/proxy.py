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
import time
import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .settings import DO_BASE_URL, DO_KEY
from .llm import verified_model_for, model_for, DEFAULT_MODELS, allowed_models

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


# THE ALLOWLIST. A model the proxy may forward is one WE chose, measured, and priced. Anything
# else is refused with the list, so a legitimate client learns what to ask for in one round trip.
#
# WHY THIS EXISTS (2026-09-01/03). `_resolve_model` passed any CONCRETE slug straight through to
# DigitalOcean on OUR key. Two ids that appear in no configuration in any project we own --
# `deepseek-v4-pro-0813` and `glm-5.3-flash`, both exact catalog slugs with snapshot suffixes, the
# shape a client sends after listing /v1/models -- then accounted for >96% of the account's tokens
# across two multi-hour bursts. Whoever held the proxy token could spend on any model in the
# catalog and did. A proxy on a shared key with no model policy is an open wallet.
# Env override JHW_PROXY_ALLOW (comma list) for a deliberate, named exception.
def _allowed_models() -> set:
    return allowed_models()          # ONE list, shared with /api/chat (llm.allowed_models)


def _client_ip(request: Request) -> str:
    # ONE proxy (caddy) sits in front; the first X-Forwarded-For entry is the client. Attacker-
    # controlled text, so it is logged, never trusted for authorisation.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    payload = await request.json()
    requested = payload.get("model", "jhw-driver")
    payload["model"] = await _resolve_model_checked(requested)
    ip = _client_ip(request)
    if payload["model"] not in _allowed_models():
        # REFUSED, RECORDED, AND ALERTED. The refusal line is the evidence -- who asked, from
        # where, for what -- and it is the single event this whole investigation has been waiting
        # for. Somebody spent >96% of the account's tokens on two models that appear in no
        # configuration we own, across two multi-hour sessions. The moment they try again they are
        # named. A log line nobody is watching would waste that; this pages immediately.
        #
        # NO MARKDOWN: the model id and the address are attacker-controlled text, and one stray
        # underscore makes Telegram reject the whole message as malformed entities -- which would
        # silently lose the one alert that matters most.
        try:
            from . import llm_events
            llm_events.record(payload["model"], None, caller="proxy.REFUSED", status="403",
                              user=ip)
        except Exception:
            pass
        try:
            from . import notify
            notify.telegram(
                "AI PROXY REFUSED A MODEL\n\n"
                "requested : %s\nresolved  : %s\nfrom IP   : %s\nallowed   : %s\n\n"
                "This is the caller that has been spending on the shared DigitalOcean key. The "
                "request was BLOCKED and cost nothing. The address above is the lead."
                % (str(requested)[:80], payload["model"][:80], ip or "unknown",
                   ", ".join(sorted(_allowed_models()))[:200]))
        except Exception:
            pass
        raise HTTPException(403, {"error": "model not permitted through this proxy",
                                  "requested": str(requested)[:80],
                                  "allowed": sorted(_allowed_models())})
    stream = bool(payload.get("stream"))
    _t0 = time.time()

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
        # METER IT. THIS ENDPOINT IS THE ONE THAT MOST NEEDS IT: it forwards to DigitalOcean using
        # OUR key, so any client holding the proxy token spends on this account without a key of
        # its own. That is exactly the shape of the 2026-09-01 incident, where two model ids that
        # appear in no configuration anywhere accounted for >96% of the account's tokens and no
        # amount of reading our own code could attribute them. An external caller is invisible
        # until the thing it comes through writes a line.
        try:
            _body = r.json()
        except Exception:
            # PRESERVE THE ORIGINAL FAILURE MODE. This line used to be `JSONResponse(r.json(), ...)`,
            # so a non-JSON body raised. Swallowing that now to return an empty 200-shaped object
            # would be a behaviour change smuggled in under a logging patch.
            raise
        try:
            from . import llm_events
            llm_events.record(payload.get("model"), (_body or {}).get("usage"),
                              caller="proxy.chat_completions",
                              ms=int((time.time() - _t0) * 1000),
                              status=str(r.status_code), user=ip)
        except Exception:
            pass
        return JSONResponse(_body, status_code=r.status_code)

    async def gen():
        # RAW passthrough: preserve DO's exact SSE bytes and framing. Reframing line-by-line and
        # dropping blank lines breaks the SSE event separators, so OpenAI-SSE clients (Hermes) read
        # an empty stream with no finish_reason. Never reframe SSE by hand.
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{DO_BASE_URL}/chat/completions",
                                headers=_do_headers(), json=payload) as r:
                async for chunk in r.aiter_raw():
                    yield chunk
        # RECORD THE CALL, AND DO NOT INVENT THE TOKENS. A streamed response carries no `usage`
        # block unless the request asked for `stream_options.include_usage`, and adding that here
        # would change the bytes a client receives -- on an endpoint whose comment three lines up
        # says never to reframe SSE by hand, because doing so already broke Hermes once.
        #
        # So the honest record is: this model was called, by this caller, for this long, and the
        # token count is NOT KNOWN. Logging zero would be a confident wrong number and would make
        # a busy external client look free -- the same defect as pricing an unknown model cheaply.
        # "Which model, how often, from where" is most of the attribution question anyway.
        try:
            from . import llm_events
            llm_events.record(payload.get("model"), None, caller="proxy.chat_completions.stream",
                              ms=int((time.time() - _t0) * 1000), status="stream", user=ip)
        except Exception:
            pass
    return StreamingResponse(gen(), media_type="text/event-stream")
