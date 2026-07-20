from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from . import qwen, store, scout, llm
from .proxy import router as proxy_router
from .auth import router as auth_router
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
async def models():
    return await qwen.list_models()

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
async def chat(req: ChatReq):
    msgs = [m.model_dump() for m in req.messages]
    async def gen():
        async for chunk in qwen.chat_stream(msgs, model=req.model):
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
