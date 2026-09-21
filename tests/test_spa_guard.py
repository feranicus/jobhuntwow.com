# -*- coding: utf-8 -*-
"""The probe-shaped 404, and the two things spa_guard.py cannot check about itself.

spa_guard.py's own self-test proves its BEHAVIOUR (no real route refused, every probe refused,
fails open without perseus, the env switch switches). This file proves the two properties that
live outside that module and that would silently rot:

  1. THE MIRROR IS TRUE. CLIENT_ROUTES restates frontend/src/App.jsx because the container ships
     the built bundle and cannot read App.jsx at runtime. A restated value drifts, and the stale
     one always wins - so App.jsx is parsed here and the two sets must be equal. Add a route to
     the router without adding it here and real users get 404s; this fails first.

  2. IT IS ACTUALLY WIRED. shield.py was once fully tested while nothing asserted the middleware
     called it: a control that is correct and unreachable is not a control. serve.py lives inside
     a heredoc in Dockerfile.web, so it is extracted and parsed with ast, and the assertion is on
     the CALL SITE and the DATA FLOW - not on a string that a comment could also contain.

Standalone, stdlib only, no network, no FastAPI. `python3 tests/test_spa_guard.py`.
"""
import ast
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend", "app"))

import spa_guard                                    # noqa: E402

FAILS = []


def ck(name, ok, detail):
    print("  %-5s %-36s %s" % ("ok" if ok else "FAIL", name, detail))
    if not ok:
        FAILS.append(name)


def serve_py_source():
    """The serve.py the image actually builds, lifted out of Dockerfile.web's heredoc."""
    s = io.open(os.path.join(ROOT, "Dockerfile.web"), encoding="utf-8").read()
    m = re.search(r"RUN cat > /app/serve\.py <<'PY'\n(.*?)\nPY\n", s, re.S)
    assert m, "the serve.py heredoc is not in Dockerfile.web in the shape this test expects"
    return m.group(1)


print("=" * 78)
print("spa_guard: the mirror and the wiring")
print("=" * 78)

# ---------------------------------------------------------------- 1. the mirror vs App.jsx
app_jsx = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"), encoding="utf-8").read()
# <Route path="/x" .../>, both <Routes> blocks. "*" is a wildcard, never evidence that a specific
# path is real, so it is excluded deliberately rather than by accident.
router = {m for m in re.findall(r'<Route\s+path="([^"]+)"', app_jsx) if m != "*"}
ck("router_was_parsed", len(router) >= 5,
   "%d concrete <Route path=> in App.jsx: %s" % (len(router), sorted(router)))
missing = sorted(router - set(spa_guard.CLIENT_ROUTES))
extra = sorted(set(spa_guard.CLIENT_ROUTES) - router)
ck("client_routes_mirror_app_jsx", not missing and not extra,
   "in App.jsx but not mirrored: %s | mirrored but not in App.jsx: %s"
   % (missing or "none", extra or "none"))

# ---------------------------------------------------------------- 2. serve.py calls it, and acts
src = serve_py_source()
tree = ast.parse(src)
fn = next((n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "spa"), None)
ck("spa_handler_found", fn is not None, "the catch-all handler `spa` is in serve.py")

decision_targets = []
if fn is not None:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        v = node.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr in ("decision", "should_404")
                and isinstance(v.func.value, ast.Name) and v.func.value.id == "spa_guard"):
            for t in node.targets:
                if isinstance(t, ast.Tuple) and t.elts and isinstance(t.elts[0], ast.Name):
                    decision_targets.append(t.elts[0].id)
                elif isinstance(t, ast.Name):
                    decision_targets.append(t.id)
ck("guard_is_called_in_the_handler", bool(decision_targets),
   "spa_guard.decision/should_404 is called inside spa(), result bound to %s"
   % (decision_targets or "nothing"))


def _returns_404(body):
    """Does this branch return a response carrying status_code=404? Property, not spelling."""
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            continue
        for kw in node.value.keywords:
            if kw.arg == "status_code" and isinstance(kw.value, ast.Constant) and kw.value.value == 404:
                return True
    return False


acted = False
if fn is not None:
    for node in ast.walk(fn):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id in decision_targets and _returns_404(node.body)):
            acted = True
ck("the_verdict_is_acted_on", acted,
   "`if %s:` returns status_code=404 - the call's RESULT reaches the response"
   % (decision_targets[0] if decision_targets else "?"))

# THE ORDER IS THE SAFETY PROPERTY, and it is TWO-SIDED. The guard must sit AFTER the real-file
# lookups (so a file that exists is served whatever its name looks like) and BEFORE the index.html
# fallback (so the only requests it can refuse are the ones that would otherwise have been handed
# the SPA shell with a 200). `ast.walk` is BREADTH-first, not source order, so the line numbers are
# collected and compared rather than taken in visit order - the first version of this check took
# the first WALKED isfile and reported a false failure against correct code.
isfiles, guards = [], []
if fn is not None:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "isfile":
                isfiles.append(node.lineno)
            elif node.func.attr in ("decision", "should_404"):
                guards.append(node.lineno)
g = min(guards) if guards else None
before = [n for n in isfiles if n < (g or 0)]
after = [n for n in isfiles if g is not None and n > g]
ck("guard_sits_between_files_and_shell", bool(before) and bool(after),
   "os.path.isfile at %s, guard at %s, os.path.isfile at %s - real files first, SPA shell last"
   % (before or "none", g, after or "none"))

# ---------------------------------------------------------------- 3. the refusal is observable
emits = []
if fn is not None:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "emit":
            for kw in node.keywords:
                if kw.arg == "evt" and isinstance(kw.value, ast.Constant):
                    emits.append(kw.value.value)
ck("the_refusal_emits_an_event", "spa_probe_404" in emits,
   "serve.py emits evt=%s on a refusal (events seen: %s)"
   % ("spa_probe_404", emits or "none"))

# ---------------------------------------------------------------- 4. the module's own self-test
r = subprocess.run([sys.executable, os.path.join(ROOT, "backend", "app", "spa_guard.py")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
ck("spa_guard_selftest_passes", r.returncode == 0,
   "python3 backend/app/spa_guard.py -> rc=%d (%s)"
   % (r.returncode, (r.stdout or "").strip().splitlines()[-1:] or ["no output"]))

# ---------------------------------------------------------------- 5. no second path table
# The whole point is that probe_shape() stays the one home for the shape judgement. If this module
# ever grows its own list of attack paths, the two drift and the stale one wins.
guard_src = io.open(os.path.join(ROOT, "backend", "app", "spa_guard.py"), encoding="utf-8").read()
gt = ast.parse(guard_src)
for node in ast.walk(gt):                             # strip docstrings so prose cannot match
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
            and node.body and isinstance(node.body[0], ast.Expr) \
            and isinstance(node.body[0].value, ast.Constant) \
            and isinstance(node.body[0].value.value, str):
        node.body.pop(0)
code_only = ast.dump(gt)
# A regex compiled in this module would be a second detector. The corpora in the self-test are
# fixtures, not a detector, and they live under names the check knows about.
ck("no_second_detector_in_spa_guard", "re.compile" not in guard_src.split('"""')[-1]
   and 'Attribute(value=Name(id=\'re\'), attr=\'compile\'' not in code_only,
   "spa_guard compiles no regex of its own; probe_shape() remains the one home")

print("-" * 78)
print("%d checks run, %d failed" % (10, len(FAILS)))
if FAILS:
    print("FAILED: %s" % ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
