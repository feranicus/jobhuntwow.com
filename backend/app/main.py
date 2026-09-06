from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from . import qwen, store, scout, llm
from .proxy import router as proxy_router
from .auth import router as auth_router, require_user
from .electronic import router as electronic_router
from .settings import CORS_ORIGINS

app = FastAPI(title="JobHuntWOW API", version="0.1.0")
_DEFAULT_ORIGINS = ["https://jobhuntwow.com", "https://www.jobhuntwow.com",
                    "http://localhost:5173", "http://127.0.0.1:5173",
                    "http://localhost:8090", "http://127.0.0.1:8090"]
_CORS_ORIGINS = ([o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
                 if CORS_ORIGINS and CORS_ORIGINS != "*" else _DEFAULT_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    # Session cookies REQUIRE allow_credentials=True, and the spec forbids pairing that with "*",
    # so we always send an explicit origin list. In prod the SPA and API are the SAME origin
    # (one container behind Caddy), so this really only matters for local vite dev.
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)

# OpenAI-compatible proxy the local agent points at (keeps the DO key server-side).
app.include_router(proxy_router)

# Auth: open self-signup (email + own password) + 6-digit OTP 2FA, signed session cookie.
# Adds /api/auth/signup, /api/auth/login, /api/auth/verify, /api/auth/logout, /api/me.
app.include_router(auth_router)
app.include_router(electronic_router)


# ---- observability: visitor telemetry + security alerting (observability.py, 1:1 from cybergod.ai)
# One JSON evt='http' per request (ip/country/device/bot/status/ms/user) -> EVENTS_LOG -> promtail ->
# Loki -> Grafana, and the SAME event feeds the alert rules (DDoS, scanners, IDOR, exfil, spray,
# OTP brute force). Detection only: never blocks a request, never touches the firewall.
# SERVICE + EVENTS_LOG come from the environment (docker-compose sets them for prod).
import os as _os
try:
    from . import observability as obs

    def _session_user(request):
        try:
            from .auth import SESSION_COOKIE, read_session
            tok = request.cookies.get(SESSION_COOKIE)
            return read_session(tok) if tok else ""
        except Exception:
            return ""

    obs.configure(
        app_name="jobhuntwow",
        service=_os.environ.get("SERVICE", "jhw-web"),
        events_log=_os.environ.get("EVENTS_LOG", ""),
        grafana_hint=_os.environ.get("OBS_GRAFANA_HINT",
                                     "https://godeyes.ai/observe/d/jobhuntwow"),
    )
    obs.install_middleware(app, session_user_fn=_session_user)

    # Daily "who used the platform and what did they run" report -> ALERT_EMAIL.
    # In-app asyncio task on purpose: no cron in the container, no systemd unit on the droplet
    # that would drift out of this repo.
    from . import daily_report as _daily

    @app.on_event("startup")
    async def _start_daily_report():
        import asyncio as _aio
        _aio.create_task(_daily.scheduler())
except Exception as _e:      # observability must NEVER stop the app from booting
    print('{"evt":"telemetry_init","result":"error","err":"%s"}' % repr(_e)[:160], flush=True)


@app.on_event("startup")
async def _startup_selfcheck():
    """Say out loud, at boot, whether OTP email can actually work.

    A silent mailer is how 'Could not send the verification email.' reached a user: google-auth
    was present but its requests transport was not, and the ImportError was swallowed per-request.
    This emits evt=mailer_selfcheck so the failure is visible in logs/Grafana before anyone signs up.
    """
    try:
        from .auth import mailer_selfcheck
        mailer_selfcheck()
    except Exception as e:  # never block startup on a diagnostic
        import json as _j
        print(_j.dumps({"evt": "mailer_selfcheck", "result": "error", "err": repr(e)[:200]}),
              flush=True)

# ---------- models ----------
class Msg(BaseModel):
    role: str
    content: str

class ChatReq(BaseModel):
    messages: List[Msg]
    model: Optional[str] = ""

class ConnReq(BaseModel):
    section: str
    patch: dict

class ScoutReq(BaseModel):
    query: str = ""
    location: str = ""
    remote: bool = True

class ApplyReq(BaseModel):
    job_id: str
    confirm: bool = False

# ---------- health / config ----------
@app.get("/api/health")
def health():
    return {"ok": True, "qwen_configured": qwen.configured(), "models": llm.routing_table()}

@app.get("/api/models")
async def models(user: str = Depends(require_user)):
    # THE OPEN WALLET (2026-09-01/03, found 2026-09-06). This endpoint was PUBLIC and returned
    # DigitalOcean's ENTIRE catalogue -- 75 ids including deepseek-v4-pro-0813 and glm-5.3-flash --
    # and /api/chat below was public too and forwarded whatever model the caller named, on our
    # key, with no allowlist. Anyone who found the endpoint had free inference on every model DO
    # sells. Both ids that spent >96% of the account's tokens are exact catalogue slugs, which is
    # precisely what a caller gets from this listing. Now: session required, allowlist only.
    d = await qwen.list_models()
    allow = llm.allowed_models()
    if isinstance(d, dict) and d.get("models"):
        d["models"] = [m for m in d["models"] if m in allow]
    return d

@app.get("/api/connections")
def connections():
    return store.get_public()

@app.post("/api/connections")
def set_connection(req: ConnReq):
    patch = dict(req.patch)
    # derive a "connected" flag from meaningful fields, keep secrets server-side
    if req.section == "telegram" and patch.get("bot_token"):
        patch["connected"] = True
    if req.section == "linkedin" and patch.get("cookies"):
        patch = {"has_cookies": True, "connected": True}  # never echo the cookies back
    if req.section == "qwen" and patch.get("model"):
        patch["connected"] = True
    return store.update(req.section, patch)

# ---------- Hermes chat (real Qwen, streamed) ----------
@app.post("/api/chat")
async def chat(req: ChatReq, request: Request, user: str = Depends(require_user)):
    # Session required (see /api/models). And the model is checked against the ONE allowlist BEFORE
    # anything is forwarded; a refusal is recorded with who asked and from where, and pages, exactly
    # like the proxy's -- because this door and the proxy are the same door in different clothes.
    mdl = (req.model or "").strip()
    if mdl and mdl not in llm.allowed_models():
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))[:64]
        try:
            from . import llm_events
            llm_events.record(mdl, None, caller="chat.REFUSED", status="403", user=user or ip)
        except Exception:
            pass
        try:
            from . import notify
            notify.telegram("CABINET CHAT REFUSED A MODEL\n\nrequested : %s\nuser      : %s\n"
                            "from IP   : %s\nallowed   : %s\n\nBLOCKED, cost nothing."
                            % (mdl[:80], (user or "?")[:80], ip or "unknown",
                               ", ".join(sorted(llm.allowed_models()))[:200]))
        except Exception:
            pass
        raise HTTPException(403, {"error": "model not permitted", "requested": mdl[:80],
                                  "allowed": sorted(llm.allowed_models())})
    msgs = [m.model_dump() for m in req.messages]
    async def gen():
        async for chunk in qwen.chat_stream(msgs, model=mdl, user=user):
            yield chunk
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")

# ---------- job scout + apply (v0.1 stubs, real agent plugs in) ----------
@app.post("/api/scout")
def do_scout(req: ScoutReq):
    return scout.search(req.query, req.location, req.remote)

@app.post("/api/apply")
def do_apply(req: ApplyReq):
    if not req.confirm:
        return {"status": "needs_confirmation",
                "message": "Human gate: confirm before the Apply Driver submits.",
                "job_id": req.job_id}
    # v0.1: simulated step log. Real path = the LLM-driven agent driving the ATS (next phase).
    steps = [
        "read DOM - detected multi-step ATS form",
        "map fields to verified profile data",
        "fill name / email / experience (from evidence)",
        "attach truth-gated resume.pdf",
        "answer screening questions (grounded)",
        "SUBMITTED - captured confirmation number",
    ]
    return {"status": "submitted", "job_id": req.job_id, "steps": steps,
            "note": "v0.1 simulated. Real submit runs through the LLM-driven apply agent."}
