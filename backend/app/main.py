import os
import os as _os

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

from . import qwen, store, scout, llm
from .proxy import router as proxy_router
from .auth import router as auth_router, require_user, require_admin
from .electronic import router as electronic_router
from .settings import CORS_ORIGINS

# NO PUBLIC API DOCUMENTATION IN PRODUCTION (2026-09-06). `authz_audit.py` found
# https://jobhuntwow.com/openapi.json serving the complete route list, parameter names and schemas
# to anyone -- including, until today, `/api/chat`'s `model` field and `/api/electronic/*`'s
# `email` query parameter. That is the map an attacker would otherwise have to guess at, and it is
# how the two exposed doors were found by whoever found them. FastAPI enables /docs, /redoc and
# /openapi.json by DEFAULT; disabling them is one argument each. Set JHW_API_DOCS=1 in a dev
# environment to get them back. (Hiding docs is not a control -- the locks are the control, and
# they are asserted by tests/test_chat_open_wallet.py -- but publishing the map is a gift.)
_DOCS = os.getenv("JHW_API_DOCS", "") == "1"
app = FastAPI(title="JobHuntWOW API", version="0.1.0",
              docs_url="/docs" if _DOCS else None,
              redoc_url="/redoc" if _DOCS else None,
              openapi_url="/openapi.json" if _DOCS else None)

# PERSEUS SIDECAR — blocks what the hub published and reports every request to the
# shared event log, which is what makes this project visible in cybergod.ai ->
# Admin -> Fleet and what lets the ONE alerting brain page the operator about it.
# It holds no credentials and sends nothing itself. Wrapped because a defence that
# stops the site it protects is worse than no defence -- but the failure is PRINTED,
# because a swallowed import is how this ran unguarded while reporting success.
# ONE WRITER PER REQUEST, DECIDED IN CODE AND NOT IN A COMPOSE FILE. Both this sidecar and
# observability's middleware append evt=http to the same shared log; for weeks every count derived
# from it was doubled for this project, and the only thing stopping it was PERSEUS_OBSERVE_HTTP=0
# in docker-compose.web.yml -- so running the image any other way (local, a different compose, a
# bare uvicorn) silently double-counted again. The nomination is made HERE, and only if
# observability is actually importable: if it is not, the sidecar keeps writing, because one writer
# is better than none and a silent log is the failure this whole stack exists to prevent.
# IMPORTABLE IS NOT INSTALLED. The nomination is made here because perseus_client reads the env
# var at import time, but it is CONDITIONAL: main.py silences the sidecar's access line only if the
# observability middleware actually goes in (see the `_event_writer_installed` assertion further
# down). If that install fails, the nomination is UNDONE and the sidecar writes again, because one
# writer is better than none and a silent event log is the failure this whole stack exists to stop.
_event_writer = {"nominated": False, "installed": False}
try:
    from . import observability as _obs_probe       # noqa: F401  (probe: is the writer available?)
    __import__("os").environ["PERSEUS_OBSERVE_HTTP"] = "0"
    _event_writer["nominated"] = True
except Exception as _obs_missing:
    print('{"evt":"event_writer","writer":"perseus_client","why":"observability not importable: %s"}'
          % repr(_obs_missing)[:120], flush=True)

try:
    from . import perseus_client
    app.add_middleware(perseus_client.Middleware)
except Exception as _perseus_exc:  # never take the app down over telemetry
    print('PERSEUS SIDECAR NOT WIRED: %r' % (_perseus_exc,), flush=True)
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

# WHO IS ACTUALLY USING THIS — visitors against bots, three buckets, plus the browser probe.
# Its own middleware (see visitors.py: observability.py is read-only on the operator's machine) and
# two routes. Detection only: nothing here blocks a request.
try:
    from . import visitors as _visitors
    _visitors.install(app)
except Exception as _e:
    print('{"evt":"visitors_init","result":"error","err":"%s"}' % repr(_e)[:160], flush=True)


@app.post("/api/probe")
async def api_probe(request: Request):
    """The page reports what its own browser could do. PUBLIC, tiny, and it stores no address.

    `judge_probe` compares the WebRTC-visible address to the request address HERE and keeps only
    the boolean — the revealed address is never logged, emitted or stored (GDPR; the whole point of
    the technique is that it unmasks, so the unmasked value must not survive the comparison)."""
    # A PUBLIC endpoint reads a bounded body or nothing at all. 2 KB is far more than the dozen
    # booleans probe.js sends, and an oversized or unparsable body is simply an empty report.
    try:
        raw = await request.body()
        body = __import__("json").loads(raw[:2048]) if raw else {}
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    try:
        from . import observability as _obs
        ip = _obs.client_ip(request)
    except Exception:
        ip = ""
    # THE WEBRTC HALF IS OFF BY DEFAULT, DELIBERATELY. It unmasks an address the user chose to
    # hide, it cannot see a scripted client at all (no JavaScript runs in one), and it accuses
    # corporate networks that block UDP. The sibling estate evaluated the same technique and
    # declined it for those reasons. The HTTP/3, webdriver and headless signals cost nobody
    # anything and stay. JHW_PROBE_WEBRTC=1 turns the rest back on, as a deliberate act.
    if _os.environ.get("JHW_PROBE_WEBRTC", "") not in ("1", "true", "yes", "on"):
        for _k in ("ice", "srflx", "publicIp"):
            body.pop(_k, None)
    out = _visitors.judge_probe(body, ip)
    _visitors.remember_probe(ip, out["verdict"])
    try:
        from . import observability as _obs2
        _obs2.emit(evt="probe", verdict=out["verdict"], why=out["why"][:80], **out["signals"])
    except Exception:
        pass
    if out["verdict"] == "automation":
        try:
            from . import alerts as _al
            _al.fire("automation_probe", ip, "Automated browser on jobhuntwow.com",
                     ["Address: %s" % (ip or "-"), "Why    : %s" % out["why"],
                      "Signals: %s" % out["signals"],
                      "", "Detection only — nothing was blocked."], severity="INFO")
        except Exception:
            pass
    # The page gets nothing back it could use to tune itself.
    return {"ok": True}


@app.get("/api/visitors")
def api_visitors(hours: float = 24.0, user: str = Depends(require_user)):
    """Three buckets over the window: visitors, clients (bots), and NOT DETERMINABLE."""
    return _visitors.window(hours)


# The application tracker: /api/applications — one row per application, from the job description to
# the documents we sent. The Pipeline and the CRM read THIS, not a folder of manifests.
try:
    from .tracker import router as _tracker_router
    if _tracker_router is not None:
        app.include_router(_tracker_router)
except Exception as _e:      # a bookkeeping module must never stop the portal from booting
    print('{"evt":"tracker_init","result":"error","err":"%s"}' % repr(_e)[:160], flush=True)


# ---- observability: visitor telemetry + security alerting (observability.py, 1:1 from cybergod.ai)
# One JSON evt='http' per request (ip/country/device/bot/status/ms/user) -> EVENTS_LOG -> promtail ->
# Loki -> Grafana, and the SAME event feeds the alert rules (DDoS, scanners, IDOR, exfil, spray,
# OTP brute force). Detection only: never blocks a request, never touches the firewall.
# SERVICE + EVENTS_LOG come from the environment (docker-compose sets them for prod).
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
    # ADDED BEFORE observability's middleware ON PURPOSE. Starlette runs the last-added
    # middleware outermost, so adding the redirect here puts it INSIDE the observer: a visitor who
    # typed jobhw.org produces a real evt=http line carrying host=jobhw.org and status=301, and
    # then gets bounced. Added after, the redirect would answer above the only writer and the short
    # domain would be invisible again.
    try:
        from . import hosts as _hosts
        _hosts.install(app)
        print('{"evt":"canonical_host","canonical":"%s","redirected":%d}'
              % (_hosts.CANONICAL, len(_hosts.REDIRECT_HOSTS)), flush=True)
    except Exception as _he:
        print('{"evt":"canonical_host","result":"not_wired","err":"%s"}' % repr(_he)[:120],
              flush=True)

    obs.install_middleware(app, session_user_fn=_session_user)
    _event_writer["installed"] = True
    print('{"evt":"event_writer","writer":"observability","sidecar_http_write":"off"}', flush=True)

    # Daily "who used the platform and what did they run" report -> ALERT_EMAIL.
    # In-app asyncio task on purpose: no cron in the container, no systemd unit on the droplet
    # that would drift out of this repo.
    # WATCH TWO SOURCES, HOURLY. Our meter says who; the vendor's balance says whether. A
    # meter-only watcher reports a normal fortnight while the invoice triples - which is what
    # happened, and the bank statement found it six days late.
    from . import spend_watch as _spend

    @app.on_event("startup")
    async def _start_spend_watch():
        import asyncio as _aio
        _aio.create_task(_spend.scheduler())

    from . import daily_report as _daily

    @app.on_event("startup")
    async def _start_daily_report():
        import asyncio as _aio
        _aio.create_task(_daily.scheduler())

    # THE APPLICATION DIGEST — the evening of any day we actually sent resumes, and once a week
    # when we did not. Same reasoning as above: an asyncio task, so there is no cron on the droplet
    # that can drift out of this repo. The decision is recomputed from the tracker every time, so a
    # restart can neither double-send nor skip.
    from . import digest as _digest

    @app.on_event("startup")
    async def _start_application_digest():
        import asyncio as _aio
        _aio.create_task(_digest.scheduler())
except Exception as _e:      # observability must NEVER stop the app from booting
    print('{"evt":"telemetry_init","result":"error","err":"%s"}' % repr(_e)[:160], flush=True)


# ---- security headers. LAST middleware added == OUTERMOST, and that ordering is the point: it has
# to decorate the 404s the SPA probe guard returns as well as the pages the app serves, or every
# refused-scanner response would go out bare. Ten headers, one of which (CSP) is written from the
# origins this site demonstrably loads - read security_headers.py's header for what differs from
# the sibling's policy and why. Measured before this: jobhuntwow sent NONE of them while serving
# candidate CVs behind a login.
try:
    from . import security_headers as _sec
    _sec.install(app)
    # NAME THE FILE THE HASH CAME FROM. If the landing policy ever silently falls back to
    # 'unsafe-inline' because /app/landing.html could not be read, this line is how anyone finds
    # out - a feature nobody has seen working is off.
    print('{"evt":"security_headers","result":"installed","landing_csp":"%s","landing_src":"%s"}'
          % (_sec.LANDING_MODE, _sec.LANDING_SOURCE), flush=True)
except Exception as _e:      # a header is never worth refusing to boot over
    print('{"evt":"security_headers","result":"error","err":"%s"}' % repr(_e)[:160], flush=True)


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

# A REFUSED BUDGET IS A 429, NEVER A 500. resume_consensus and llm.complete raise
# llm_meter.BudgetExceeded from deep inside the tailor chain; without this handler the user would
# read "internal server error" for a deliberate, correct refusal, and the operator would go looking
# for a crash that never happened. Retry-After names when it is worth trying again.
try:
    from . import llm_meter as _llm_meter

    @app.exception_handler(_llm_meter.BudgetExceeded)
    async def _budget_exceeded(request: Request, exc):
        return JSONResponse({"error": "ai_budget", "detail": str(exc)},
                            status_code=429, headers={"Retry-After": "3600"})
except Exception as _e:                                   # a handler is not worth an outage
    print('{"evt":"budget_handler","result":"not_wired","err":"%s"}' % repr(_e)[:120], flush=True)


# THE UNDO. If the nominated writer was silenced but never installed, nobody writes the access
# line at all -- the worst of the three possible states, and invisible. The sidecar is already
# imported by now, so re-enabling means putting its flag back AND telling the module, since it read
# the env var once at import.
if _event_writer["nominated"] and not _event_writer["installed"]:
    try:
        __import__("os").environ["PERSEUS_OBSERVE_HTTP"] = "1"
        perseus_client.OBSERVE_HTTP = True
        print('{"evt":"event_writer","writer":"perseus_client","why":"observability imported but '
              'its middleware did not install - the sidecar keeps writing"}', flush=True)
    except Exception:
        print('{"evt":"event_writer","writer":"NONE","level":"error","why":"observability did not '
              'install and the sidecar could not be re-enabled - no request is being recorded"}',
              flush=True)


# ---------- the operator's security console ----------
# READ-ONLY, ADMIN-ONLY, and it reads the same event record everything else in the stack consumes.
# "I want to see every user trying to enter jobhw.org or jobhuntwow.com, and all the defences"
# is answered here: per-host traffic, three-bucket classification, attack classes by shape, what
# the shield did, whether anybody was actually told, and what the models cost.
@app.get("/api/security/overview")
def security_overview(request: Request, hours: float = 24.0,
                      admin: str = Depends(require_admin)):
    from . import security as _sec
    try:
        h = max(0.25, min(168.0, float(hours or 24.0)))
    except Exception:
        h = 24.0
    return _sec.overview(window_h=h)


# ---------- health / config ----------
def _build_stamp() -> str:
    """The build this container is running, written into the payload by deploy_direct at ship time.

    "unknown" when the file is absent (a local run, or an image built some other way) -- never a
    guess, and never today's date pretending to be the build date."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "BUILD_STAMP"), encoding="utf-8") as fh:
            return fh.read().strip()[:80] or "unknown"
    except Exception:
        return "unknown"


BUILD = _build_stamp()


@app.get("/api/health")
def health():
    # `build` is here so the page can show WHICH CODE IT IS. Without it, "I do not see the feature
    # you built" and "I am looking at an older build" are indistinguishable from the browser.
    return {"ok": True, "build": BUILD, "qwen_configured": qwen.configured(),
            "models": llm.routing_table()}

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
def connections(user: str = Depends(require_user)):
    return store.get_public()

@app.post("/api/connections")
def set_connection(req: ConnReq, user: str = Depends(require_user)):
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
def do_scout(req: ScoutReq, user: str = Depends(require_user)):
    return scout.search(req.query, req.location, req.remote)

@app.post("/api/apply")
def do_apply(req: ApplyReq, user: str = Depends(require_user)):
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
