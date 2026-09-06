"""/api/models and /api/chat were PUBLIC and /api/chat forwarded any caller-chosen model id to
DigitalOcean on our key (2026-09-06). Verified from the internet with no login: /api/models
returned DO's whole catalogue, deepseek-v4-pro-0813 and glm-5.3-flash included. That is the door
the 2026-09-01/03 spend walked through. These tests keep it shut."""
import asyncio, json, os, sys, tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-in-prod")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.main import app                      # noqa: E402
from app import auth, users, llm              # noqa: E402


def _call(method, path, body=None, cookie=None):
    """Drive the ASGI app with ~20 lines of stdlib. No httpx, no TestClient (not in requirements)."""
    hdrs = [(b"host", b"jobhuntwow.com"), (b"content-type", b"application/json")]
    if cookie:
        hdrs.append((b"cookie", cookie.encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": hdrs, "client": ("203.0.113.9", 1234), "server": ("jobhuntwow.com", 443)}
    out = {"status": None, "body": b""}
    sent = [False]
    async def receive():
        if sent[0]:
            await asyncio.sleep(3600)
        sent[0] = True
        return {"type": "http.request", "body": raw, "more_body": False}
    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(app(scope, receive, send))
    return out


def _session():
    email = "wallet-test@example.com"
    try:
        users.create_user(email, "Correct-Horse-9!")
    except Exception:
        pass
    return "%s=%s" % (auth.SESSION_COOKIE, auth.make_session(email))


def test_models_is_no_longer_public():
    assert _call("GET", "/api/models")["status"] == 401


def test_chat_is_no_longer_public():
    r = _call("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}],
                                    "model": "deepseek-v4-pro-0813"})
    assert r["status"] == 401, "an anonymous caller must never reach the model"


def test_a_logged_in_user_cannot_name_a_model_outside_the_allowlist(monkeypatch):
    calls = []
    async def fake_stream(messages, model="", temperature=0.4, user=""):
        calls.append(model); yield "x"
    from app import qwen
    monkeypatch.setattr(qwen, "chat_stream", fake_stream)
    r = _call("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}],
                                    "model": "deepseek-v4-pro-0813"}, cookie=_session())
    assert r["status"] == 403 and calls == [], (r["status"], r["body"][:200])
    assert "deepseek-v4-pro-0813" not in llm.allowed_models()


def test_an_allowed_model_still_streams_and_carries_the_user(monkeypatch):
    seen = {}
    async def fake_stream(messages, model="", temperature=0.4, user=""):
        seen["model"], seen["user"] = model, user; yield "ok"
    from app import qwen
    monkeypatch.setattr(qwen, "chat_stream", fake_stream)
    r = _call("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}],
                                    "model": "deepseek-3.2"}, cookie=_session())
    assert r["status"] == 200 and r["body"] == b"ok"
    assert seen["model"] == "deepseek-3.2" and seen["user"] == "wallet-test@example.com"


def test_one_allowlist_for_both_doors():
    """The proxy had an allowlist since 2026-09-06 morning; the cabinet chat had none. Two lists
    would drift, so the proxy must delegate to llm.allowed_models()."""
    from app import proxy
    assert proxy._allowed_models() == llm.allowed_models()
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend", "app", "proxy.py"), encoding="utf-8").read()
    assert "JHW_PROXY_ALLOW" not in src.split("def _allowed_models")[1].split("\n\n")[0]


if __name__ == "__main__":
    from _mini import run_module
    raise SystemExit(1 if run_module(dict(globals())) else 0)
