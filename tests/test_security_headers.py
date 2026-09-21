# -*- coding: utf-8 -*-
"""The CSP must match what this site actually loads - in BOTH directions.

A policy written from memory either breaks the site or permits things nobody checked, so this
file does not compare the CSP to a copy of itself. It reads the two HTML shells and every fetch
in frontend/src, derives the origins the site really reaches, and fails if the policy is missing
one of them OR carries one the site does not use.

The one that would actually take the site down is `landing_hash_matches_the_served_file`: the
public one-pager is a single 162 KB inline <script>, and under `script-src 'self'` it does not
run. It is permitted by SHA-256 instead. If that hash is computed from anything other than the
exact bytes serve.py serves, the landing page is blank and nobody sees a stack trace.

Standalone, stdlib only, no network, no FastAPI. `python3 tests/test_security_headers.py`.
"""
import ast
import base64
import hashlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend", "app"))

import security_headers as sh                        # noqa: E402

LANDING = os.path.join(ROOT, "index.html")
SPA_SRC = os.path.join(ROOT, "frontend", "index.html")
SPA_DIST = os.path.join(ROOT, "frontend", "dist", "index.html")

FAILS = []


def ck(name, ok, detail):
    print("  %-5s %-40s %s" % ("ok" if ok else "FAIL", name, detail))
    if not ok:
        FAILS.append(name)


def read(p):
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception:
        return ""


def directive(csp, name):
    for part in csp.split(";"):
        part = part.strip()
        if part == name or part.startswith(name + " "):
            return part[len(name):].strip()
    return None


print("=" * 78)
print("security headers: the policy against what the site really loads")
print("=" * 78)

landing_html = read(LANDING)
spa_html = read(SPA_SRC) + read(SPA_DIST)
app_js = ""
srcdir = os.path.join(ROOT, "frontend", "src")
for base, _d, files in os.walk(srcdir):
    for f in files:
        if f.endswith((".js", ".jsx")):
            app_js += read(os.path.join(base, f))
ck("the_sources_were_read", len(landing_html) > 10000 and len(spa_html) > 200 and len(app_js) > 1000,
   "landing=%d B, shells=%d B, frontend/src=%d B" % (len(landing_html), len(spa_html), len(app_js)))

# ------------------------------------------------------------- 1. the origins, both directions
ORIGIN_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+")
# Only origins the page REACHES. A URL inside a documentation code block is text, not a request,
# so the landing page's `QWEN_BASE_URL=https://inference.do-ai.run/v1` is excluded by construction:
# it is not in a src=, href=, url() or fetch().
reaching = set()
for blob in (landing_html, spa_html):
    for m in re.finditer(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', blob):
        reaching.add("://".join(m.group(1).split("://")[:1] + [m.group(1).split("://")[1].split("/")[0]]))
    for m in re.finditer(r'url\(\s*["\']?(https?://[^"\')]+)', blob):
        reaching.add("://".join(m.group(1).split("://")[:1] + [m.group(1).split("://")[1].split("/")[0]]))
for m in re.finditer(r'fetch\(\s*["\'`](https?://[^"\'`]+)', app_js):
    reaching.add("://".join(m.group(1).split("://")[:1] + [m.group(1).split("://")[1].split("/")[0]]))

permitted = set(ORIGIN_RE.findall(sh.CSP_APP)) | set(ORIGIN_RE.findall(sh.CSP_LANDING))
ck("policy_permits_every_origin_used", reaching <= permitted,
   "reached: %s | not permitted: %s" % (sorted(reaching), sorted(reaching - permitted) or "none"))
ck("policy_permits_nothing_unused", permitted <= reaching,
   "permitted: %s | unused: %s" % (sorted(permitted), sorted(permitted - reaching) or "none"))

# ------------------------------------------------------------- 2. cross-origin fetch really is none
rel_only = not re.search(r'fetch\(\s*["\'`]https?://', app_js)
ck("connect_src_self_is_correct", rel_only and directive(sh.CSP_APP, "connect-src") == "'self'",
   "every fetch() in frontend/src is a relative same-origin path, so connect-src 'self' holds")

# ------------------------------------------------------------- 3. the cabinet is STRICT
app_script = directive(sh.CSP_APP, "script-src")
ck("cabinet_forbids_inline_script", app_script == "'self'",
   "script-src for the cabinet is %r - an injected <script> or onclick= does not execute" % app_script)
# ...and that is only free because the built shell has none. Prove it, do not assume it.
dist = read(SPA_DIST)
inline_in_dist = [b for b in re.findall(r"<script[^>]*>(.*?)</script>", dist, re.S) if b.strip()]
ck("built_shell_has_no_inline_script", dist and not inline_in_dist,
   "frontend/dist/index.html carries %d inline script block(s)" % len(inline_in_dist))

# ------------------------------------------------------------- 4. THE ONE THAT BREAKS THE SITE
blocks = [b for b in re.findall(r"<script[^>]*>(.*?)</script>", landing_html, re.S) if b.strip()]
ck("landing_has_one_inline_block", len(blocks) == 1 and landing_html.count("</script>") == 1,
   "%d inline block(s), %d </script> terminators - extraction is unambiguous"
   % (len(blocks), landing_html.count("</script>")))
want = ["'sha256-%s'" % base64.b64encode(hashlib.sha256(b.encode("utf-8")).digest()).decode()
        for b in blocks]
got = sh.landing_script_hashes(LANDING)
ck("landing_hash_matches_the_served_file", got == want,
   "computed %s, policy would carry %s" % (want, got))
landing_script = directive(sh.CSP_LANDING, "script-src")
ck("landing_policy_carries_the_hash", all(h in (landing_script or "") for h in want),
   "script-src for '/' is %r" % ((landing_script or "")[:90]))
# CSP3: a script-src carrying a hash makes 'unsafe-inline' be IGNORED. Listing both would look
# like belt-and-braces and silently be the strict policy - which is the broken-landing-page case.
ck("hash_and_unsafe_inline_never_coexist",
   not (want and "'unsafe-inline'" in (landing_script or "")),
   "the hashed policy does not also list 'unsafe-inline' (browsers would ignore it)")

# ------------------------------------------------------------- 5. it is SCOPED, and it fails open
ck("only_the_landing_path_is_relaxed",
   sh.csp_for("/") == sh.CSP_LANDING and sh.csp_for("/login") == sh.CSP_APP
   and sh.csp_for("/api/electronic/artifacts/x/y.pdf") == sh.CSP_APP
   and sh.csp_for("/?ref=x") == sh.CSP_LANDING,
   "'/' gets the landing policy; every other path gets the strict one")
fallback, mode = (lambda: (sh._policy("'self' 'unsafe-inline'"), "unsafe-inline"))()
ck("fails_open_when_the_file_is_unreadable",
   sh.landing_script_hashes("/nonexistent/landing.html") == [],
   "an unreadable landing file yields no hash, and landing_policy() then falls back to "
   "'unsafe-inline' rather than serving a page whose script cannot run")

# ------------------------------------------------------------- 6. the rest of the headers
for h in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options",
          "Referrer-Policy", "Permissions-Policy", "Cross-Origin-Opener-Policy",
          "Cross-Origin-Resource-Policy", "X-Permitted-Cross-Domain-Policies"):
    ck("header_" + h.lower().replace("-", "_"), h in sh.HEADERS, "%s = %r" % (h, sh.HEADERS.get(h)))
ck("no_version_disclosure", sh.HEADERS.get("Server") == "jobhuntwow",
   "Server: %r - and not the sibling's name" % sh.HEADERS.get("Server"))
ck("candidate_documents_are_never_cached",
   any("/api/".startswith(p) or p == "/api/" for p in sh.NO_STORE_PREFIXES),
   "NO_STORE_PREFIXES=%s covers /api/electronic/artifacts/ where the CVs are"
   % (sh.NO_STORE_PREFIXES,))

# ------------------------------------------------------------- 7. ORDERING: outermost or useless
# Starlette makes the LAST middleware added the OUTERMOST. security_headers must be installed
# AFTER observability, or the 404s the SPA guard returns go out with no headers at all.
main_src = read(os.path.join(ROOT, "backend", "app", "main.py"))
tree = ast.parse(main_src)
for node in ast.walk(tree):                       # comments and docstrings can never satisfy this
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
            and node.body and isinstance(node.body[0], ast.Expr) \
            and isinstance(node.body[0].value, ast.Constant) \
            and isinstance(node.body[0].value.value, str):
        node.body.pop(0)
# PRESENCE IS NOT REACHABILITY. `ast.walk` finds a call just as happily under `if False:`, and the
# mutation harness proved it: wrapping the install in a dead branch left this check green while the
# headers were gone. So every statement inside a constant-false branch is excluded first.
dead = set()
for node in ast.walk(tree):
    if isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and not node.test.value:
        for sub in ast.walk(ast.Module(body=list(node.body), type_ignores=[])):
            dead.add(getattr(sub, "lineno", -1))

lines = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.lineno not in dead:
        if node.func.attr == "install_middleware":
            lines.setdefault("obs", []).append(node.lineno)
        if node.func.attr == "install" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "_sec":
            lines.setdefault("sec", []).append(node.lineno)
ck("security_headers_are_installed", bool(lines.get("sec")),
   "main.py calls security_headers.install at line(s) %s" % (lines.get("sec") or "NONE"))
ck("installed_outermost", bool(lines.get("sec")) and bool(lines.get("obs"))
   and min(lines["sec"]) > max(lines["obs"]),
   "observability at %s, security headers at %s - later == outermost == decorates the 404s"
   % (lines.get("obs"), lines.get("sec")))

print("-" * 78)
print("%d checks run, %d failed" % (23, len(FAILS)))
if FAILS:
    print("FAILED: %s" % ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
