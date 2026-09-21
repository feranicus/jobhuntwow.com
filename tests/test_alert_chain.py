# -*- coding: utf-8 -*-
"""Can jobhuntwow produce an alert, and can it be delivered? Nothing has ever proven either.

MEASURED, 24h to 2026-09-21: 4002 requests, 320 attack-shaped, 0 alerts. The 404 fix
(backend/app/spa_guard.py) is what lets the rules fire at all. This file guards the rest of the
chain, every link of which was broken and none of which had a test:

  * DELIVERY. observability.notify_telegram sent parse_mode=Markdown while alert bodies carry
    attacker-controlled probed paths. One stray `_` or `*` and Telegram rejects the WHOLE message
    with HTTP 400 - so the very first alert this site ever produced would likely have vanished.
  * THE RULE'S SUBJECT. ALERT_DOWNLOAD_MARKER defaulted to "/deck/", a cybergod route that does
    not exist here, so the exfiltration rule guarded nothing while candidate CVs sat elsewhere.
  * REACHABILITY. observe_http() is where six of the seven HTTP rules live. If nothing on the
    request path calls it, every rule in it is dead code that passes every unit test.
  * ONE LINE PER REQUEST. Two middlewares wrote evt=http into the same log, doubling every count.
  * THE FLEET PAGE. perseus_client emits `perseus_shield_block`; fleet.py counted `shield_block`.

Standalone, stdlib only, no network. `python3 tests/test_alert_chain.py`.
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "backend", "app")

FAILS = []


def ck(name, ok, detail):
    print("  %-5s %-38s %s" % ("ok" if ok else "FAIL", name, detail))
    if not ok:
        FAILS.append(name)


def src(rel):
    return io.open(os.path.join(APP, rel), encoding="utf-8").read()


def code_only(text, path="<src>"):
    """The SHIPPING SLICE: comments and docstrings removed, so prose can never satisfy a check.
    A check in this estate has matched its own explanatory comment more times than any other bug.
    """
    tree = ast.parse(text, path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
    return tree


print("=" * 78)
print("the alert chain: delivery, subject, reachability, one line, one count")
print("=" * 78)

# ============================================================== 1. DELIVERY: no parse mode, ever
obs_src = src("observability.py")
obs_tree = code_only(obs_src, "observability.py")

tg = next((n for n in ast.walk(obs_tree)
           if isinstance(n, ast.FunctionDef) and n.name == "notify_telegram"), None)
ck("notify_telegram_found", tg is not None, "observability.notify_telegram exists")

# THE PROPERTY: parse_mode must never be an UNCONDITIONAL part of the payload. It may appear only
# inside a branch guarded by the caller's own opt-in, because the caller is then asserting that it
# composed the text itself. Asserted structurally, not by grepping for the word: the word is in
# the docstring, and a docstring is not behaviour.
unconditional = []
if tg is not None:
    guarded_lines = set()
    for node in ast.walk(tg):
        if isinstance(node, ast.If):
            for sub in ast.walk(ast.Module(body=list(node.body), type_ignores=[])):
                guarded_lines.add(getattr(sub, "lineno", -1))
    for node in ast.walk(tg):
        hit = False
        if isinstance(node, ast.Constant) and node.value == "parse_mode":
            hit = True
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "parse_mode":
            hit = True
        if hit and node.lineno not in guarded_lines:
            unconditional.append(node.lineno)
ck("no_unconditional_parse_mode", not unconditional,
   "parse_mode appears only inside a branch (unconditional at lines: %s)" % (unconditional or "none"))

# And the default must BE off: a parameter that defaults to markdown re-creates the bug.
default_off = False
if tg is not None:
    args = tg.args
    names = [a.arg for a in args.args]
    if "markdown" in names:
        i = names.index("markdown") - (len(names) - len(args.defaults))
        default_off = (0 <= i < len(args.defaults)
                       and isinstance(args.defaults[i], ast.Constant)
                       and args.defaults[i].value is False)
ck("plain_text_is_the_default", default_off,
   "notify_telegram(text, markdown=False) - a caller must OPT IN to a parse mode")

# The rule bodies really do carry attacker-controlled text; this is why the above matters. Prove
# it rather than assert it, so the reason cannot quietly stop being true.
oh = obs_src[obs_src.index("def observe_http"):]
oh = oh[:oh.index("\n    def ")] if "\n    def " in oh else oh
ck("alert_bodies_carry_the_probed_path", '"Paths: %s"' in oh or "'Paths: %s'" in oh,
   "path_probe/authz_probe put the probed PATHS in the message body - that is the attacker's text")

# notify_both must not hand it markdown syntax either.
nb = next((n for n in ast.walk(obs_tree) if isinstance(n, ast.FunctionDef) and n.name == "notify_both"), None)
stars = []
if nb is not None:
    for node in ast.walk(nb):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "*%s*" in node.value:
            stars.append(node.lineno)
ck("notify_both_sends_no_markdown_syntax", not stars,
   "no '*%%s*' subject wrapper in notify_both (found at: %s)" % (stars or "none"))

# The sibling path (notify.py) must stay fixed too - it was fixed first and is the precedent.
nsrc = src("notify.py")
ntree = code_only(nsrc, "notify.py")
nboth = next((n for n in ast.walk(ntree) if isinstance(n, ast.FunctionDef) and n.name == "both"), None)
nstars = [n.lineno for n in ast.walk(nboth or ast.Module(body=[], type_ignores=[]))
          if isinstance(n, ast.Constant) and isinstance(n.value, str) and "*%s*" in n.value]
ck("notify_py_stays_plain_text", not nstars,
   "notify.both sends no markdown syntax either (found at: %s)" % (nstars or "none"))

# ============================================================== 2. THE RULE'S SUBJECT
ART = "/api/electronic/artifacts/"
el = src("electronic.py")
# The route must actually exist, or we have just pointed the rule at a second fiction.
has_route = bool(re.search(r'@router\.get\(\s*["\']/artifacts/\{job_id\}/\{filename\}["\']', el)) \
    and 'APIRouter(prefix="/api/electronic"' in el
ck("the_artifact_route_exists", has_route,
   "electronic.py serves /artifacts/{job_id}/{filename} under prefix /api/electronic")

markers = set(re.findall(r'ALERT_DOWNLOAD_MARKER["\']\s*,\s*["\']([^"\']+)["\']',
                         obs_src + src("alerts.py")))
ck("download_marker_points_at_it", markers == {ART},
   "every ALERT_DOWNLOAD_MARKER default is %s (found: %s)" % (ART, sorted(markers)))

# ONE HOME: the rule in alerts.py must read the constant, not a literal of its own.
al_tree = code_only(src("alerts.py"), "alerts.py")
al_oh = next((n for n in ast.walk(al_tree) if isinstance(n, ast.FunctionDef) and n.name == "observe_http"), None)
deck_literals = [n.lineno for n in ast.walk(al_oh or ast.Module(body=[], type_ignores=[]))
                 if isinstance(n, ast.Constant) and n.value == "/deck/"]
ck("no_hardcoded_deck_route_left", not deck_literals,
   "alerts.observe_http holds no '/deck/' literal (found at: %s)" % (deck_literals or "none"))

# ============================================================== 3. REACHABILITY
# A CHECK THAT CANNOT FAIL IS NOT A CHECK, and a RULE THAT IS NEVER CALLED IS NOT A RULE.
# observe_http holds six of the seven HTTP rules. Find a caller ON THE REQUEST PATH: inside the
# middleware that main.py installs, not merely somewhere in the repo (a self-test calls it too).
mw = next((n for n in ast.walk(obs_tree)
           if isinstance(n, ast.FunctionDef) and n.name == "install_middleware"), None)
callers = []
if mw is not None:
    for node in ast.walk(mw):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "observe_http":
            callers.append(node.lineno)
ck("observe_http_has_a_caller_on_the_request_path", bool(callers),
   "install_middleware calls observe_http at line(s) %s" % (callers or "NONE - every HTTP rule is dead code"))

# ...and main.py must install that middleware, or the caller itself is unreachable.
main_src = src("main.py")
main_tree = code_only(main_src, "main.py")
installs = [n.lineno for n in ast.walk(main_tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "install_middleware"]
ck("main_installs_that_middleware", bool(installs),
   "main.py calls install_middleware at line(s) %s" % (installs or "NONE"))

# ============================================================== 4. ONE LINE PER REQUEST
# Two writers of evt=http into one log doubles every count derived from it. The surviving writer
# must be observability's, and the sidecar's must be switchable off - AND jobhuntwow must have
# switched it, or the duplication is still shipping.
pc_src = src("perseus_client.py")
pc_tree = code_only(pc_src, "perseus_client.py")
pc_obs = next((n for n in ast.walk(pc_tree) if isinstance(n, ast.FunctionDef) and n.name == "observe"), None)
gated = False
if pc_obs is not None:
    for node in ast.walk(pc_obs):
        if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp):
            names = {x.operand.id for x in ast.walk(node.test)
                     if isinstance(x, ast.UnaryOp) and isinstance(x.operand, ast.Name)}
            if "OBSERVE_HTTP" in names and any(isinstance(b, ast.Return) for b in node.body):
                gated = True
ck("sidecar_write_is_switchable", gated,
   "perseus_client.observe() returns early on `not OBSERVE_HTTP`")

compose = io.open(os.path.join(ROOT, "docker-compose.web.yml"), encoding="utf-8").read()
ck("jobhuntwow_nominated_one_writer", "PERSEUS_OBSERVE_HTTP=0" in compose,
   "docker-compose.web.yml sets PERSEUS_OBSERVE_HTTP=0 for jhw-web")

# EVIDENCE MUST NOT VANISH WITH THE LINE. The surviving writer has to carry the four fields that
# existed only on the sidecar's line, or this de-duplication silently became a deletion.
safe_emit = None
for node in ast.walk(mw or ast.Module(body=[], type_ignores=[])):
    if isinstance(node, ast.FunctionDef) and node.name == "_safe_emit":
        safe_emit = node
merged = set()
for node in ast.walk(safe_emit or ast.Module(body=[], type_ignores=[])):
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
            and isinstance(node.slice.value, str):
        merged.add(node.slice.value)
    if isinstance(node, ast.keyword) and node.arg:
        merged.add(node.arg)
want = {"sf", "hv", "hvs", "av"}
ck("the_sidecars_evidence_survives", want <= merged,
   "the surviving evt=http line carries %s (missing: %s)"
   % (sorted(want & merged), sorted(want - merged) or "none"))

# ============================================================== 5. THE FLEET PAGE COUNTS IT
fleet = os.path.join(os.path.dirname(ROOT), "webapp", "backend", "app", "fleet.py")
if os.path.exists(fleet):
    ftree = code_only(io.open(fleet, encoding="utf-8").read(), "fleet.py")
    counted = set()
    for node in ast.walk(ftree):
        if isinstance(node, ast.Compare) and node.ops and isinstance(node.ops[0], ast.In):
            for c in node.comparators:
                if isinstance(c, ast.Tuple):
                    vals = {e.value for e in c.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                    if "security_alert" in vals:
                        counted |= vals
    emitted = {n.args[0].value for n in ast.walk(pc_tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_emit"
               and n.args and isinstance(n.args[0], ast.Constant)
               and isinstance(n.args[0].value, str) and "block" in n.args[0].value}
    # A BLOCK THAT HAPPENED, and only that. The first version of this check asked whether every
    # event whose name contains "block" was counted, and correctly failed: the sidecar also emits
    # perseus_shield_would_block (shadow mode - enforcement that did NOT happen) and
    # perseus_shield_unblock (the opposite of a block). Counting either would inflate the alert
    # column with non-events, which is the benign-every-time failure that trains an operator to
    # read past the alert that matters. So the property is two-sided.
    REAL = "perseus_shield_block"
    NOT_ALERTS = {"perseus_shield_would_block", "perseus_shield_unblock"}
    ck("sidecar_emits_the_block_event", REAL in emitted,
       "perseus_client emits %s (all block-ish events: %s)" % (REAL, sorted(emitted)))
    ck("fleet_counts_a_sidecar_block", REAL in counted,
       "fleet's alert branch counts %s" % sorted(counted))
    ck("fleet_ignores_non_events", not (NOT_ALERTS & counted),
       "shadow-mode and unblock are NOT counted as alerts (wrongly counted: %s)"
       % (sorted(NOT_ALERTS & counted) or "none"))
else:
    # A CHECK THAT CANNOT SEE ITS SUBJECT IS NOT A PASS. fleet.py lives in the sibling repo; if the
    # checkout is not beside this one the honest answer is "not measured", and it FAILS rather than
    # reporting the estate as healthy from an absence.
    for n in ("sidecar_emits_the_block_event", "fleet_counts_a_sidecar_block",
              "fleet_ignores_non_events"):
        ck(n, False, "webapp/backend/app/fleet.py not found at %s - not measured" % fleet)

# ============================================================== 6. FIRE IT. In process. For real.
# Every check above reads source. This one RUNS the rules: three probe-shaped 404s from one address
# through the same observe_http() the middleware calls, against a temp events log, and the
# security_alert line has to appear. It is the cheap half of the staging gate's ALERTCHAIN section
# (which fires real HTTP at the real container); this half needs no droplet, so it runs on every
# `python ship.py` and catches the regression before anything is deployed.
# NOTHING REAL IS TOUCHED: a temp events log, ALERT_DELIVERY=0 so no message is sent, and the
# config is restored afterwards.
import json
import tempfile

sys.path.insert(0, os.path.join(ROOT, "backend", "app"))
_old_delivery = os.environ.get("ALERT_DELIVERY")
os.environ["ALERT_DELIVERY"] = "0"          # detection only; never page from a test suite
try:
    import observability as obs
    _keep = (obs.CFG.events_log, obs.CFG.service, obs.CFG.tg_token, obs.CFG.gmail_sender)
    tmpdir = tempfile.mkdtemp(prefix="jhw-alertchain-")
    log = os.path.join(tmpdir, "events.log")
    obs.configure(events_log=log, service="jhw-web", tg_token="", gmail_sender="")
    try:
        # THE CONDITION UNDER TEST IS THE 404. With the old catch-all these same three requests
        # arrived as 200 and rule 3 could not fire - that is the whole defect, expressed as a
        # fixture. The fixture is proved first: a 200 run must produce NOTHING.
        for pth in ("/.env", "/wp-login.php", "/phpmyadmin/"):
            obs.alerts.observe_http({"ip": "203.0.113.77", "path": pth, "status": 200,
                                     "ua": "curl/8.4.0", "bot": True, "bot_name": "curl"})
        lines = io.open(log, encoding="utf-8").read().splitlines() if os.path.exists(log) else []
        quiet = [l for l in lines if '"security_alert"' in l]
        ck("catchall_200s_produce_no_alert", not quiet,
           "3 probe paths answered 200 -> %d alert(s). This IS the measured defect: the rules are "
           "gated on 404/403, so a catch-all silences them." % len(quiet))

        for pth in ("/.env", "/wp-login.php", "/phpmyadmin/"):
            obs.alerts.observe_http({"ip": "203.0.113.77", "path": pth, "status": 404,
                                     "ua": "curl/8.4.0", "bot": True, "bot_name": "curl"})
        lines = io.open(log, encoding="utf-8").read().splitlines() if os.path.exists(log) else []
        alerts_fired = [json.loads(l) for l in lines if '"security_alert"' in l]
        ck("three_probe_404s_fire_an_alert", bool(alerts_fired),
           "3 probe paths answered 404 -> %d security_alert event(s), rules: %s"
           % (len(alerts_fired), [a.get("rule") for a in alerts_fired] or "NONE"))
        ck("the_alert_is_tagged_jhw_web",
           any(a.get("service") == "jhw-web" for a in alerts_fired),
           "service field on the alert(s): %s"
           % ([a.get("service") for a in alerts_fired] or "no alert at all"))
        ck("the_alert_names_the_probed_paths",
           any(".env" in (a.get("detail") or "") for a in alerts_fired),
           "the body carries the attacker-controlled paths - which is why delivery must be plain "
           "text (detail seen: %r)"
           % ((alerts_fired[0].get("detail") or "")[:70] if alerts_fired else "none"))
        supp = [json.loads(l) for l in lines if '"alert_delivery"' in l]
        ck("suppressed_delivery_still_leaves_a_line", any(
            d.get("result") == "suppressed" for d in supp),
           "ALERT_DELIVERY=0 emitted %d alert_delivery line(s) - an alert nobody received must "
           "never look like one that was delivered" % len(supp))
    finally:
        obs.configure(events_log=_keep[0], service=_keep[1], tg_token=_keep[2],
                      gmail_sender=_keep[3])
        try:
            os.remove(log)
            os.rmdir(tmpdir)
        except Exception:
            pass
except Exception as exc:
    ck("the_alert_chain_could_be_exercised", False,
       "could not run the rules in process: %r - not measured, which is not a pass" % (exc,))
finally:
    if _old_delivery is None:
        os.environ.pop("ALERT_DELIVERY", None)
    else:
        os.environ["ALERT_DELIVERY"] = _old_delivery

print("-" * 78)
print("%d checks run, %d failed" % (21, len(FAILS)))
if FAILS:
    print("FAILED: %s" % ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
