"""ONE canonical hostname, and every other name we own REDIRECTED BY THE APPLICATION.

WHY THE APP AND NOT THE PROXY. jobhw.org was redirected by a `redir` block in the shared Caddyfile,
which is one line and works -- and means the request never reaches this service, so nobody who
types the short domain appears anywhere in our event record. The operator's question is "show me
every person trying to enter jobhw.org OR jobhuntwow.com", and a redirect that happens upstream of
the only thing that writes events cannot answer it. You cannot protect what you cannot see, and you
cannot see what the proxy answered on your behalf.

So Caddy now PROXIES jobhw.org here like any other host, and this middleware issues the 301. The
request is observed first (this middleware is installed INSIDE observability's, so the 301 gets its
own evt=http line carrying host=jobhw.org), then answered in ONE hop to the canonical host, path
and query preserved.

NO OPEN REDIRECT, STRUCTURALLY. The destination host is a constant in this file; it is never read
from the request. Only hostnames on an explicit list are redirected at all -- an unknown Host that
somehow reaches us is served normally rather than bounced somewhere on its own say-so. A redirector
that takes its target from attacker-controlled input is a phishing endpoint with our certificate on
it (OWASP A01:2021, CWE-601).
"""
import os

CANONICAL = (os.environ.get("JHW_CANONICAL_HOST") or "jobhuntwow.com").strip().lower()
# Every name we own that is NOT the canonical one. Declared, never learned: a learned list is
# available only after the first request, and a Host header is attacker-controlled text.
REDIRECT_HOSTS = {h.strip().lower() for h in (
    os.environ.get("JHW_REDIRECT_HOSTS")
    or "jobhw.org,www.jobhw.org,www.jobhuntwow.com").split(",") if h.strip()}
# Never redirect these, whatever the Host says: the ACME challenge must answer on the name being
# validated or the certificate for that name cannot be issued, and a scanner bouncing the challenge
# would turn itself into a certificate outage for every domain on this host.
NEVER = ("/.well-known/",)


def host_of(raw):
    """The bare hostname from a Host header: no port, lowercased, bounded."""
    return (str(raw or "").split(",")[0].split(":")[0].strip().lower())[:80]


def target(raw_host, path="/", query=""):
    """The absolute URL this request should be sent to, or None to serve it normally.

    Pure, so the property that matters -- the destination is always our canonical host -- is
    testable without a server.
    """
    h = host_of(raw_host)
    if not h or h == CANONICAL or h not in REDIRECT_HOSTS:
        return None
    p = "/" + str(path or "/").lstrip("/")          # collapse "//evil.com" to "/evil.com"
    q = str(query or "")
    if any(p.startswith(n) for n in NEVER):
        return None
    return "https://%s%s%s" % (CANONICAL, p, ("?" + q) if q else "")


def install(app):
    """Install INSIDE the observability middleware, so the redirect is an observed request.

    Starlette runs the LAST-ADDED middleware outermost, so this must be added BEFORE
    observability.install_middleware(). If it were added after, the 301 would be answered above the
    only writer of evt=http and jobhw.org would be invisible again -- the exact failure this file
    exists to fix.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import RedirectResponse

    class _Canonical(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            try:
                url = target(request.headers.get("host"), request.url.path,
                             request.url.query)
            except Exception:
                url = None                      # a redirector must never be an outage
            if url:
                return RedirectResponse(url, status_code=301)
            return await call_next(request)

    app.add_middleware(_Canonical)
    return app


def _selftest():
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    ck("the canonical host is served, never redirected", target("jobhuntwow.com", "/tailor") is None)
    ck("an unknown host is served, never bounced on its own say-so",
       target("evil.example", "/") is None)
    ck("the short domain lands on the canonical host in ONE hop, path and query intact",
       target("jobhw.org", "/pipeline", "job=7") == "https://jobhuntwow.com/pipeline?job=7",
       str(target("jobhw.org", "/pipeline", "job=7")))
    ck("www of the short domain too", target("www.jobhw.org", "/") == "https://jobhuntwow.com/")
    ck("www of the canonical domain too",
       target("www.jobhuntwow.com", "/x") == "https://jobhuntwow.com/x")
    ck("a port on the Host header is ignored", target("jobhw.org:443", "/") is not None)
    ck("the Host is case-insensitive", target("JobHW.org", "/") is not None)
    # THE PROPERTY, not the spelling: whatever the input, the destination is OUR host.
    evil = ["//evil.example/x", "/\\evil.example", "/x?next=https://evil.example",
            "https://evil.example/x"]
    bad = [target("jobhw.org", e, "") for e in evil]
    ck("no input can move the destination off the canonical host",
       all(u is not None and u.startswith("https://" + CANONICAL + "/") for u in bad),
       str(bad))
    ck("the ACME challenge is never redirected (a bounced challenge is a certificate outage)",
       target("jobhw.org", "/.well-known/acme-challenge/tok") is None)
    print("hosts selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
