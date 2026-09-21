# -*- coding: utf-8 -*-
"""The security headers jobhuntwow.com was serving none of, while holding other people's CVs.

MEASURED, 2026-09-21: jobhuntwow sent NO Content-Security-Policy, NO Strict-Transport-Security, NO
X-Frame-Options, NO X-Content-Type-Options, NO Referrer-Policy and NO Permissions-Policy, on a site
that stores candidate resumes, cover letters and photographs behind a login. The sibling project
(cybergod.ai) has installed all ten since 2026-08 via webapp/backend/app/security_headers.py. This
is that file, ported - and the CSP is NOT that file's CSP, because a policy is written from what a
site actually loads, and these two sites load different things.

WHY IN THE APP AND NOT IN THE CADDYFILE. The proxy in front of us is SHARED with five other sites,
and one bad edit to it once took every domain on the box down together for six hours. A header
belongs to the application that knows what it serves: it ships inside the image, the engine-hash
deploy verify covers it, and it can be tested in a second without touching anyone else's vhost.

===============================================================================================
WHAT WAS CHANGED FROM CYBERGOD'S POLICY, AND WHY. Read this before editing anything below.
===============================================================================================
Every origin here was read off this repo, not recalled: frontend/index.html, the built
frontend/dist/index.html, every fetch() in frontend/src/, and the landing one-pager index.html.

1. script-src IS SPLIT BY PATH, and that is the whole of the difference.
   * The CABINET (every path except "/") gets `script-src 'self'` with no 'unsafe-inline' - the
     single largest XSS mitigation available. This is free here and was verified, not assumed:
     the Vite-built shell frontend/dist/index.html contains ZERO inline script, only
     <script type="module" src="/assets/index-*.js">. This is also the surface that matters,
     because it is the one behind the login with the CVs on it.
   * The PUBLIC LANDING PAGE ("/") is a single self-contained one-pager with ONE inline <script>
     block of 162 KB. Under `script-src 'self'` that block does not run and the page is dead.
     So "/" gets the SHA-256 of that exact block instead, computed at import time from the SAME
     file serve.py serves. A hash is strictly better than 'unsafe-inline': the page's own script
     runs and an INJECTED one still does not.
   * If the landing file cannot be read, the hash cannot be computed, and "/" falls back to
     'unsafe-inline'. That is deliberate: in CSP a script-src carrying a hash makes 'unsafe-inline'
     be IGNORED, so the two cannot both be listed as belt-and-braces - it is one or the other, and
     the fallback direction has to be the one that cannot break a public page.
   (The three `onclick=` hits a grep finds in the landing file are JS property assignments -
   `b.onclick=()=>render(k)` - inside that block, not HTML inline-handler attributes. CSP does not
   govern those. Checked, because getting it wrong would have meant a broken page.)

2. media-src stays 'self' though nothing uses it today (cybergod has a hero video, jobhuntwow has
   no <video>, <audio>, <object> or <embed> at all - grep says 0). 'self' costs nothing and a
   future screen-recording upload does not become an outage.

3. connect-src 'self' is CORRECT HERE AND WAS CHECKED. Every fetch in frontend/src is a relative
   same-origin path (/api/chat, /api/me, /api/probe, /api/electronic/*). The landing page contains
   no fetch, no XMLHttpRequest and no WebSocket; the `https://inference.do-ai.run/v1` string in it
   is documentation text inside a code block, not a request.

4. worker-src 'self' is kept although this repo currently ships no service worker (frontend/public
   does not exist). Same reasoning as media-src.

5. img-src keeps `data:` (the landing embeds one data:image) and `blob:` (a CV/photo preview is
   the obvious next thing to render from a File).

6. `Server: jobhuntwow`, not `cybergod`. Version disclosure is free reconnaissance, and the two
   sites are deliberately unrelated in public.

FAIL-OPEN, LIKE EVERY OTHER CONTROL HERE. Setting a header must never break a response. The whole
body is wrapped; on any error the response goes out exactly as it was.
"""
import base64
import hashlib
import os
import re

FONT_CSS = "https://fonts.googleapis.com"      # the Google Fonts STYLESHEET (a <link> in both shells)
FONT_FILES = "https://fonts.gstatic.com"       # the font FILES that stylesheet then @font-faces in

# WHERE THE LANDING ONE-PAGER IS. A hash computed from a file we do not serve is a broken page
# with a confident comment, so the resolution must agree with serve.py's
# `LANDING = os.environ.get("LANDING_HTML", "/app/landing.html")` wherever this runs.
#
# IN THE CONTAINER those are the same file: Dockerfile.web does `COPY index.html ./landing.html`,
# byte for byte. OUTSIDE it - the operator's PC, the test suite - /app does not exist, and the
# first version of this file therefore silently produced no hash and fell back to 'unsafe-inline'
# everywhere except production. That is the defect class where a check cannot see its subject:
# the test could not verify the policy that actually ships. So the repo's own index.html is the
# second candidate, and LANDING_SOURCE names which one was read, out loud, at boot.
def _landing_candidates():
    env = os.environ.get("LANDING_HTML")
    if env:
        return [env]                       # an explicit setting is never second-guessed
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return ["/app/landing.html", os.path.join(repo, "index.html")]


def _resolve_landing():
    for c in _landing_candidates():
        try:
            if os.path.isfile(c):
                return c
        except Exception:
            continue
    return _landing_candidates()[0]


LANDING_HTML = _resolve_landing()

_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S)


def landing_script_hashes(path=None):
    """-> ["'sha256-...'"] for every inline <script> in the landing page, or [] if it cannot be read.

    Inside a <script> element the HTML parser is in script-data state and the only terminator is
    `</script`, so the raw source between the tags IS the element's text content - which is exactly
    what the browser hashes. Byte-exact, verified against the real file.

    NEVER RAISES. An empty list means "no hash available", and the caller's fallback is what keeps
    the public page alive.
    """
    try:
        with open(path or LANDING_HTML, "r", encoding="utf-8") as fh:
            txt = fh.read()
    except Exception:
        return []
    out = []
    try:
        for body in _SCRIPT_RE.findall(txt):
            if not body.strip():
                continue                     # <script src=...></script> has nothing to hash
            digest = hashlib.sha256(body.encode("utf-8")).digest()
            out.append("'sha256-%s'" % base64.b64encode(digest).decode("ascii"))
    except Exception:
        return []
    return out


def _policy(script_src):
    return "; ".join([
        "default-src 'self'",
        "script-src " + script_src,
        # 'unsafe-inline' IS required for styles: React writes style="..." attributes on elements
        # and a nonce cannot be applied to an attribute, and the landing page carries one <style>
        # block. Inline CSS is a far smaller hazard than inline script - it cannot call an API or
        # read a cookie.
        "style-src 'self' 'unsafe-inline' " + FONT_CSS,
        "font-src 'self' data: " + FONT_FILES,
        "img-src 'self' data: blob:",
        "media-src 'self'",
        "connect-src 'self'",                  # every fetch in this app is a relative path
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",                   # no Flash/applets, ever
        "frame-src 'none'",
        "frame-ancestors 'none'",              # modern clickjacking defence
        "base-uri 'none'",                     # stops <base href> hijacking every relative URL
        "form-action 'self'",                  # a stolen form cannot POST credentials off-site
        "upgrade-insecure-requests",
    ])


# The cabinet: no inline script anywhere, so no exception for any.
CSP_APP = _policy("'self'")


def landing_policy():
    """The policy for "/" - hashes when we can read the file, 'unsafe-inline' when we cannot."""
    hashes = landing_script_hashes()
    if hashes:
        return _policy("'self' " + " ".join(hashes)), "hash"
    return _policy("'self' 'unsafe-inline'"), "unsafe-inline"


CSP_LANDING, LANDING_MODE = landing_policy()
LANDING_SOURCE = LANDING_HTML

# Exactly the paths serve.py answers with the landing one-pager. "/" and nothing else: the SPA
# shell owns every other HTML response and it has no inline script to excuse.
LANDING_PATHS = ("/",)

# Two years, subdomains included. NOT preloaded by default: submission to hstspreload.org is a
# one-way door that is slow and awkward to reverse, so it is a deliberate decision taken once,
# rather than a side effect of deploying. Set HSTS_PRELOAD=1 when that decision is made.
_HSTS = "max-age=63072000; includeSubDomains"
if os.environ.get("HSTS_PRELOAD") == "1":
    _HSTS += "; preload"

HEADERS = {
    "Strict-Transport-Security": _HSTS,
    "X-Content-Type-Options": "nosniff",          # stop MIME sniffing an upload into a script
    "X-Frame-Options": "DENY",                    # legacy twin of frame-ancestors
    # Cross-origin, send only the origin - so an outbound click to a job posting never tells the
    # employer which candidate, job or document page the user came from.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": ("accelerometer=(), autoplay=(self), camera=(), display-capture=(), "
                          "geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
                          "midi=(), payment=(), usb=(), xr-spatial-tracking=()"),
    "Cross-Origin-Opener-Policy": "same-origin",  # process isolation from any opener
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Server": "jobhuntwow",
}

# Anything under these prefixes is owner-scoped or live and must never sit in a shared cache -
# including caches we do not control: a corporate proxy, a CDN, a phone. /api/electronic/artifacts
# is inside /api/, and that is where the candidate documents are served from.
NO_STORE_PREFIXES = ("/api/",)


def csp_for(path):
    """The policy this path gets. Pure function, so the test can ask it directly."""
    p = (str(path or "/").split("?")[0] or "/")
    if p.rstrip("/") == "" or p in LANDING_PATHS:
        return CSP_LANDING
    return CSP_APP


def install(app):
    """Outermost middleware: it must also decorate the 404s the SPA guard returns.

    Starlette builds the stack so the LAST middleware added is the OUTERMOST, so this call has to
    come AFTER observability.install_middleware(app) in main.py. tests/test_security_headers.py
    asserts that ordering, because getting it wrong would silently leave every refused-scanner
    response bare.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class _SecurityHeaders(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            try:
                path = request.url.path
                response.headers["Content-Security-Policy"] = csp_for(path)
                for k, v in HEADERS.items():
                    response.headers[k] = v
                if any(path.startswith(p) for p in NO_STORE_PREFIXES):
                    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
                    response.headers["Pragma"] = "no-cache"
            except Exception:
                pass          # a header is never worth failing a response over
            return response

    app.add_middleware(_SecurityHeaders)
    return app
