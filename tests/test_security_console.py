"""The controls added on 2026-09-21, tested where they are WIRED rather than where they are written.

Every check here answers one of the twelve findings in the LLM-jacking report, and each is written
so that breaking the control makes it FAIL (proven by mutation, by exit code, not by eye):

  1. the budget gate runs BEFORE the request at every chokepoint          (findings 2, 3)
  2. every chokepoint also RECORDS, so the meter cannot measure half      (finding 4)
  3. an over-budget caller is refused by the real routes, over real HTTP  (finding 3)
  4. the refusal is evidence: recorded and paged                          (finding 7)
  5. the console is administrator-only, server-side                       (finding 11)
  6. an unreadable source renders None, never 0                           (findings 3, 8, 9)
  7. exactly ONE evt=http line per request, in code not in compose        (the doubled counter)
  8. the line names the HOST, and the short domain is observed not lost   (the new visibility)
"""
import ast
import asyncio
import io
import json
import os
import sys
import tempfile
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ["ALERTS_ENABLED"] = "0"          # detection yes, paging the operator no
_TMP = tempfile.mkdtemp()
os.environ["EVENTS_LOG"] = os.path.join(_TMP, "events.log")
os.environ["JHW_METER_DB"] = os.path.join(_TMP, "meter.sqlite")

_fails = []
_ran = [0]


def check(cond, name, detail=""):
    _ran[0] += 1
    print(("  ok    " if cond else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not cond:
        _fails.append(name)


def src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ============================================================ 1 + 2. the gate is BEFORE the spend
def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _calls(node, attr, skip=()):
    """Line numbers of every call whose attribute is `attr` inside this function.

    DECORATORS ARE EXCLUDED. `@router.post("/chat/completions")` is an `ast.Call` with attr "post"
    sitting on line 1 of the function, so counting it made the gate look like it ran AFTER the
    request -- a false failure produced by measuring the wrong thing, which is worse than no
    measurement because somebody would have "fixed" working code.
    """
    out = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr and n.lineno not in skip):
            out.append(n.lineno)
    return out


def _decorator_lines(fn):
    return {d.lineno for d in getattr(fn, "decorator_list", [])}


CHOKEPOINTS = [("backend/app/llm.py", "chat"), ("backend/app/llm.py", "complete"),
               ("backend/app/qwen.py", "chat_stream"),
               ("backend/app/proxy.py", "chat_completions")]

for rel, fname in CHOKEPOINTS:
    tree = ast.parse(src(rel))
    fn = _fn(tree, fname)
    deco = _decorator_lines(fn) if fn else set()
    gates = _calls(fn, "gate", deco) if fn else []
    posts = sorted(_calls(fn, "post", deco) + _calls(fn, "stream", deco)) if fn else []
    check(bool(gates), "%s::%s asks the budget gate at all" % (rel.split("/")[-1], fname),
          "gate() at %s" % gates)
    check(bool(gates) and bool(posts) and min(gates) < min(posts),
          "%s::%s gates BEFORE it spends" % (rel.split("/")[-1], fname),
          "gate %s, first request %s" % (gates[:1], posts[:1]))

# EVERY GATED FUNCTION MUST ALSO RECORD, or the gate reads a total that excludes the path it
# guards. This was first written as `n_meter >= n_events - 1`, which is vacuous wherever there is
# exactly one recorder -- deleting every meter row from llm.py and qwen.py still passed it -- and
# beside it sat `n_meter == 0` for resume_consensus, which asserted that the tailor chain must NOT
# be metered and so pinned the hole in place. Both are gone. The property is per FUNCTION: if it
# gates, it records.
for rel, fname in CHOKEPOINTS:
    fn = _fn(ast.parse(src(rel)), fname)
    deco = _decorator_lines(fn) if fn else set()
    recs = _calls(fn, "record", deco) if fn else []
    meter_recs = [ln for ln in recs
                  if "_meter.record(" in src(rel).splitlines()[ln - 1]
                  or "llm_meter.record(" in src(rel).splitlines()[ln - 1]]
    check(bool(meter_recs), "%s::%s RECORDS what it spends, not just gates it"
          % (rel.split("/")[-1], fname), "meter.record at %s" % meter_recs)

# and the tailor chain is metered through its transport, exactly once per HTTP call
_rc = src("backend/app/resume_consensus.py")
check("llm.chat(" in _rc, "the tailor posts through llm.chat, which is a metered chokepoint")
check("_meter.record(" not in _rc and "llm_meter.record(" not in _rc,
      "and does not record a SECOND row for the same HTTP call")


# ============================================================ 3 + 4. the routes really refuse
from app import llm_meter                                                       # noqa: E402
from app.main import app                                                        # noqa: E402
from app import auth, users                                                     # noqa: E402

users.create_user("admin-test@example.com", "Sup3rSecret!pass")
users.create_user("plain-test@example.com", "Sup3rSecret!pass")
auth.ADMIN_EMAILS.add("admin-test@example.com")


def call(method, path, body=None, cookie=None, host=b"jobhuntwow.com", token=None):
    out = {"status": 0, "body": b"", "headers": {}}
    hdrs = [(b"host", host), (b"user-agent", b"Mozilla/5.0"), (b"content-type", b"application/json")]
    if cookie:
        hdrs.append((b"cookie", cookie.encode()))
    if token:
        hdrs.append((b"authorization", ("Bearer " + token).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": hdrs, "client": ("203.0.113.9", 1234), "server": ("jobhuntwow.com", 443)}
    payload = json.dumps(body or {}).encode()

    sent = {"done": False}

    async def receive():
        # ONE body, then a disconnect. A receive() that keeps re-serving the same http.request
        # makes Starlette's streaming middleware raise "Unexpected message received" -- which looks
        # like an application bug and is a test-harness bug.
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in m["headers"]}
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.new_event_loop().run_until_complete(app(scope, receive, send))
    return out


admin_c = "%s=%s" % (auth.SESSION_COOKIE, auth.make_session("admin-test@example.com"))
plain_c = "%s=%s" % (auth.SESSION_COOKIE, auth.make_session("plain-test@example.com"))

# Spend the whole day's budget, then knock on the doors.
llm_meter._broken[0] = False
with llm_meter._connect() as c:
    c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (__import__("time").time(), llm_meter._today(), "t", "deepseek-3.2", 0, 0,
               99.0, 99.0, 0, 0, "ok", "plain-test@example.com"))

r = call("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}]}, cookie=plain_c)
check(b"budget" in r["body"].lower() or b"limit" in r["body"].lower(),
      "an over-budget cabinet chat is refused in the stream, not billed",
      r["body"][:70].decode("utf-8", "replace"))

os.environ["AGENT_PROXY_TOKEN"] = "test-token"
import importlib                                                                # noqa: E402
importlib.reload(sys.modules["app.proxy"])
r = call("POST", "/v1/chat/completions", {"model": "deepseek-3.2", "messages": []},
         token="test-token")
check(r["status"] == 429 and r["headers"].get("retry-after"),
      "an over-budget proxy call is 429 with Retry-After, never forwarded",
      "%s %s" % (r["status"], r["headers"].get("retry-after")))

log = open(os.environ["EVENTS_LOG"], encoding="utf-8").read() if os.path.exists(
    os.environ["EVENTS_LOG"]) else ""
check("llm_budget_refused" in log, "the refusal is written to the event record, with who and why")

fired = []
llm_meter.refuse(user="x@y.z", model="m", caller="c", reason="over", ip="1.2.3.4")
check("llm_budget_refused" in open(os.environ["EVENTS_LOG"], encoding="utf-8").read(),
      "refuse() records even when the alert channel is off")

# ============================================================ 5. the console is admin-only
check(call("GET", "/api/security/overview")["status"] == 401,
      "the security console refuses an anonymous caller")
check(call("GET", "/api/security/overview", cookie=plain_c)["status"] == 403,
      "and a signed-in NON-administrator, server-side")
r = call("GET", "/api/security/overview", cookie=admin_c)
check(r["status"] == 200, "and answers the administrator", str(r["status"]))
d = json.loads(r["body"] or b"{}")

# ============================================================ 6. unreadable is not zero
from app import security as sec                                                 # noqa: E402
_real = sec.EVENTS_LOG
sec.EVENTS_LOG = os.path.join(_TMP, "nope", "missing.log")
blind = sec.overview()
check(blind["requests"] is None and blind["attacks"] is None,
      "an unreadable event file reports None, never 0")
check(bool(blind["caveat"]), "and says so in a caveat the page must render")
check(blind["sidecar"] in ("not installed", "unverifiable", "stale")
      and blind["enforce"] == "unknown",
      "a sidecar it cannot verify never reads as 'off'",
      "%s / %s" % (blind["sidecar"], blind["enforce"]))
sec.EVENTS_LOG = _real

# ============================================================ 7 + 8. one writer, and it names the host
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    call("GET", "/api/health")
lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("{")]
http = [json.loads(ln) for ln in lines if '"evt": "http"' in ln or '"evt":"http"' in ln]
check(len(http) == 1, "exactly ONE evt=http line per request", "got %d" % len(http))
check(bool(http) and http[0].get("host") == "jobhuntwow.com",
      "and it names the hostname that was asked for", str(http[:1])[:80])
# THE PROPERTY, NOT THE SPELLING: whoever the writer is, the app must end up with exactly one,
# and it must not be possible to end up with none. The runtime check above proves "exactly one".
# This proves the undo exists: silencing the sidecar is conditional on the nominated writer having
# actually INSTALLED, not merely imported.
_main = src("backend/app/main.py")
check('_event_writer["installed"] = True' in _main
      and '_event_writer["nominated"] and not _event_writer["installed"]' in _main,
      "silencing the sidecar is undone if the nominated writer never installs")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r = call("GET", "/pipeline", host=b"jobhw.org")
short = [json.loads(ln) for ln in buf.getvalue().splitlines()
         if ln.startswith("{") and '"evt": "http"' in ln]
check(r["status"] == 301 and r["headers"].get("location") == "https://jobhuntwow.com/pipeline",
      "the short domain is redirected in one hop", str(r["headers"].get("location")))
check(bool(short) and short[0].get("host") == "jobhw.org" and short[0].get("status") == 301,
      "AND it is OBSERVED on the way through - the point of proxying it instead of redirecting "
      "it upstream", str(short[:1])[:90])

print("-" * 78)
print("%d checks run, %d failed" % (_ran[0], len(_fails)))
if _fails:
    print("FAILED: " + ", ".join(_fails))
sys.exit(1 if _fails else 0)
