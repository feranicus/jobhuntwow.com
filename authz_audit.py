"""DENY BY DEFAULT, AND PROVE IT. Every route of this app must refuse an anonymous caller.

WHY. The 2026-09 incident's finding #11, verbatim: "No authorisation audit existed. Nothing ever
asked whether every route refuses a stranger, until after the money was gone." In a framework where
a route is PUBLIC unless somebody remembers to add a dependency, an open endpoint is a matter of
time rather than of care -- `/api/chat` and the whole `/api/electronic/*` tree were exactly that.

MEASURE THE RESPONSE, NEVER THE SOURCE. The sibling project's first version scanned function
signatures and declared everything public, because the real guard is a statement inside the handler.
This drives the real ASGI app and reads the real status code.

BLIND IS NOT CLEAN. If nothing could be reached, this says so and exits 2. A tool that cannot see
its subject must never report a clean result -- that is the defect that let a failed Loki query read
as innocence three runs in a row.

    python authz_audit.py            # in-process, every route, every method
    python authz_audit.py --live     # the public site, GET/HEAD only, safe methods
    python authz_audit.py --json
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

# THE PUBLIC SET, NAMED. Anything not on this list must refuse. Adding to it is a deliberate act
# that shows up in review; forgetting a dependency is not. One home: the same list the regression
# test in tests/test_chat_open_wallet.py pins, kept here because this file must run standalone.
PUBLIC_OK = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/signup"), ("POST", "/api/auth/login"),
    ("POST", "/api/auth/verify"), ("POST", "/api/auth/logout"),
    # The browser probe. Public on purpose: it is posted by an anonymous page load, it carries no
    # identity, the body is capped, and it can only ever ADD evidence about the caller's own
    # client. Justified at the call site in main.py and pinned by the wallet test.
    ("POST", "/api/probe"),
}
OK_STATUS = (401, 403)
# The floor the walk must clear before any verdict is trusted. One home: the same number
# tests/test_chat_open_wallet.py asserts, and it is imported from here rather than restated.
MIN_ROUTES = 25
LIVE_PATHS = ["/api/me", "/api/models", "/api/connections", "/api/applications",
              "/api/electronic/jobs", "/api/electronic/jobs?email=victim@example.com",
              "/api/security/overview", "/api/visitors", "/v1/models", "/openapi.json", "/docs"]


def iter_api_routes(app):
    """EVERY APIRoute, including the ones inside an included router.

    FastAPI 0.139 stopped flattening `include_router()` into `app.routes`: an included router now
    appears as a single lazy `_IncludedRouter` object. Walking `app.routes` and filtering for
    APIRoute therefore sees ONLY the routes declared directly on the app -- 10 of 27 here -- and
    silently skips every auth, electronic, tracker and /v1 route. That is a check that cannot see
    its subject reporting a clean result, which is the failure this whole file exists to prevent,
    so the walk descends instead of assuming a shape.
    """
    from fastapi.routing import APIRoute
    out, seen = [], set()

    def walk(container, depth=0):
        if depth > 6:
            return
        for r in (getattr(container, "routes", None) or []):
            if isinstance(r, APIRoute):
                if id(r) not in seen:
                    seen.add(id(r))
                    out.append(r)
            elif hasattr(r, "original_router"):
                walk(r.original_router, depth + 1)
            elif hasattr(r, "routes"):
                walk(r, depth + 1)

    walk(app)
    return out


def classify(path, status, body, public, method="GET"):
    """`public` is a set of (METHOD, path) pairs and the METHOD is part of the key.

    Matching on the path alone made every method of a public path public: a hypothetical leaking
    GET /api/auth/login would have been reported as public-by-design rather than as a finding.
    """
    if status == 0:
        return "unreachable", "no response"
    if (method, path) in public:
        return "public-by-design", ""
    if status in OK_STATUS:
        return "refused", ""
    if status == 404:
        return "not-present", ""
    if status in (301, 302, 303, 307, 308):
        return "redirect", ""
    if status == 422:
        # FastAPI validates the body BEFORE the dependency runs, so a 422 says nothing either way.
        return "inconclusive-422", "body validation ran before the guard"
    if 200 <= status < 300:
        head = (body or b"")[:120]
        if b"<!doctype html" in head.lower() or b"<html" in head.lower():
            return "spa-shell", "an API path answered with HTML"
        return "SERVED", (body or b"")[:160].decode("utf-8", "replace")
    return "other-%d" % status, (body or b"")[:120].decode("utf-8", "replace")


def audit_local():
    """Every route, every method, in-process. No network, and a throwaway data directory."""
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
    os.environ.setdefault("SESSION_SECRET", "audit-only")
    os.environ.setdefault("JHW_NO_DOCKER_AUTOSTART", "1")
    # A SYNTHETIC AUDIT MUST NOT PAGE THE OPERATOR. Probing every route anonymously trips the
    # authz_probe rule by construction, and an alert that fires on every release is exactly the
    # benign-every-time noise that trains people to read past the one that matters. Detection still
    # runs; only DELIVERY is suppressed, and this line says so out loud.
    os.environ["ALERTS_ENABLED"] = "0"
    print("  (alert delivery suppressed for this audit; detection still runs)")
    sys.path.insert(0, os.path.join(HERE, "backend"))
    import asyncio
    from fastapi.routing import APIRoute
    from app.main import app

    def call(method, path):
        out = {"status": 0, "body": b""}
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "client": ("203.0.113.9", 1234),
                 "server": ("jobhuntwow.com", 443),
                 "headers": [(b"host", b"jobhuntwow.com"), (b"user-agent", UA.encode()),
                             (b"content-type", b"application/json")]}

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
            elif m["type"] == "http.response.body":
                out["body"] += m.get("body", b"")
        try:
            asyncio.new_event_loop().run_until_complete(app(scope, receive, send))
        except Exception as e:
            out["body"] = ("EXC %r" % (e,)).encode()
        return out["status"], out["body"]

    rows, findings = [], []
    for r in iter_api_routes(app):
        if not r.path.startswith(("/api/", "/v1/")):
            continue
        path = (r.path.replace("{job_id}", "1").replace("{filename}", "x.pdf")
                .replace("{app_id}", "1").replace("{id}", "1"))
        for m in sorted(set(r.methods) - {"HEAD", "OPTIONS"}):
            if (m, r.path) in PUBLIC_OK:
                rows.append(("%s %s" % (m, r.path), 0, "public-by-design", "named in PUBLIC_OK"))
                continue
            st, body = call(m, path)
            verdict, extra = classify(r.path, st, body, PUBLIC_OK, m)
            # /v1/* answers 503 when no proxy token is configured: a refusal, not a leak.
            if r.path.startswith("/v1/") and st == 503:
                verdict = "refused"
            rows.append(("%s %s" % (m, r.path), st, verdict, extra))
            if verdict in ("SERVED", "spa-shell"):
                findings.append(("%s %s" % (m, r.path), st, extra))
    return rows, findings


def audit_live(host):
    import urllib.error
    import urllib.request
    rows, findings = [], []
    for p in LIVE_PATHS:
        url = "https://%s%s" % (host, p)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        st, body = 0, b""
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                st, body = r.status, r.read(400)
        except urllib.error.HTTPError as e:
            st, body = e.code, e.read(400)
        except Exception:
            st, body = 0, b""
        verdict, extra = classify(p, st, body, {("GET", "/api/health")}, "GET")
        rows.append(("GET %s" % p, st, verdict, extra))
        if verdict in ("SERVED", "spa-shell") and p not in ("/",):
            findings.append(("GET %s" % p, st, extra))
    return rows, findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="probe the public site instead")
    ap.add_argument("--host", default="jobhuntwow.com")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows, findings = audit_live(a.host) if a.live else audit_local()
    reached = [r for r in rows if r[2] != "unreachable"]
    # A FLOOR, BECAUSE BLIND IS NOT CLEAN. `reached` counts public-by-design rows too, so a walk
    # that regressed to seeing only the five named public routes would have printed [OK] while
    # measuring nothing. This app has ~31 api/v1 routes; anything under 25 means the walk broke,
    # not that the routes went away.
    if not a.live and len(rows) < MIN_ROUTES:
        print("[!] BLIND, NOT CLEAN: the route walk found %d routes, expected at least %d. "
              "Nothing here is a statement about authorisation." % (len(rows), MIN_ROUTES))
        return 2

    if a.json:
        print(json.dumps({"rows": rows, "findings": findings, "reached": len(reached),
                          "total": len(rows)}, indent=2))
        return 1 if findings else 0

    for name, st, verdict, extra in rows:
        print("  %-46s %-4s %-18s %s" % (name[:46], st or "-", verdict, (extra or "")[:60]))
    print("-" * 96)
    if findings:
        print("[X] %d ROUTE(S) SERVED CONTENT TO AN ANONYMOUS CALLER:" % len(findings))
        for name, st, extra in findings:
            print("    %s -> %s  %s" % (name, st, (extra or "")[:100]))
        return 1
    if not reached:
        print("[!] BLIND, NOT CLEAN: nothing answered. This run says NOTHING about authorisation.")
        return 2
    print("[OK] no route served content to an anonymous caller (%d probed, %d reached)."
          % (len(rows), len(reached)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
