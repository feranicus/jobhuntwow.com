#!/usr/bin/env python3
"""test_gate_integrity.py - the gate must not be able to LIE. These are checks about the CHECKS.

    python3 tests/test_gate_integrity.py

NO NETWORK, NO DROPLET, NO API KEY, NO pytest. Everything is either a pure function, a stubbed
subprocess, or a property of the RENDERED remote script.

Every rule here exists because a green gate once lied somewhere:
  1  the rendered shell PARSES (and `bash -n` too, when bash exists on this machine)
  2  no branch may score an UNKNOWN answer as a PASS - one `*) chk ... yes` wildcard took a real
     NO-GO to "33/33 green"
  3  EXPECTED and the script cannot drift apart, in either direction
  4  page routes are probed with a BROWSER user agent
  5  the run must reach a terminal SENTINEL, and the ssh return code is never discarded
  6  the CHECK| protocol has exactly ONE emitter, and its runaway guard sits far above real details
  7  the embedded verdict helper compiles, and its names are declared
  8  the PANEL CANNOT SET THE GATE: the gate is recomputed from the checks after the panel answers
  9  an unconfigured staging host SKIPS LOUDLY and touches nothing
 10  the reboot test cannot pass without a reboot
 11  ship.py refuses to promote on NO-GO, and calls the gate BEFORE the deploy
 12  ship.py does not reimplement the deploy, and never publishes a port or removes orphans
 13  engine_is_current fails CLOSED when it cannot read its subject

Source assertions strip COMMENTS first. A check that matches its own explanatory comment cannot
fail; that mistake has been made four separate times in the sibling project.

THE EXIT GATE IS THE LAST STATEMENT IN THIS FILE.
"""
import ast
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SG = os.path.join(ROOT, "deploy", "stagegate")
sys.path.insert(0, SG)
sys.path.insert(0, ROOT)

import gate      # noqa: E402
import quorum    # noqa: E402

RUN = [0]
FAILS = []


def _code_only(src):
    """Strip # comments before grepping. A check that matches its own explanatory comment cannot
    fail -- this repository family has been caught by that four separate times."""
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


def check(cond, what):
    RUN[0] += 1
    print("  %s  %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def nocomments(src, hash_comments=True):
    """Strip comments so a rule cannot be satisfied by the prose that explains it."""
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if hash_comments and s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def heredoc_balance(script):
    """Every heredoc opened must be closed. Returns a list of complaints.

    THIS IS DELIBERATELY THE ONLY STRUCTURAL PROPERTY CHECKED IN PURE PYTHON, and the first version
    of this function is why. It also counted quote parity per line and if/fi, case/esac, do/done
    keywords - and it FAILED SIX TIMES on a script `bash -n` calls clean:
      * an apostrophe inside a COMMENT ("docker's OWN healthcheck") is odd-but-fine;
      * a comment quoting a phrase across two lines is odd-but-fine;
      * `sed -n 's/..."\([^"]*\)".../\1/p'` is odd-but-fine;
      * a backslash line continuation splits a quoted string across lines, legally;
      * `if [ x ]; then a; else b; fi` puts `fi` at the END of the line, so a line-anchored `fi`
        counter reads +1 forever.
    Shell needs a real lexer, and a checker that false-positives on a correct file is worse than no
    checker: it trains you to ignore it. An UNTERMINATED HEREDOC, by contrast, is unambiguous and is
    the failure that silently swallows the rest of a script - so that is what is checked here, and
    `bash -n` below is the real syntax check whenever bash exists on this machine.
    """
    bad, heredoc = [], None
    for i, ln in enumerate(script.splitlines(), 1):
        if heredoc:
            if ln.strip() == heredoc:
                heredoc = None
            continue
        m = re.search(r"<<-?'([A-Za-z0-9_]+)'|<<-?\"([A-Za-z0-9_]+)\"|<<-?([A-Za-z0-9_]+)\s*$", ln)
        if m:
            heredoc = m.group(1) or m.group(2) or m.group(3)
    if heredoc:
        bad.append("unterminated heredoc: %s" % heredoc)
    return bad


HEALTH = gate.health_script()
PROV = gate.provision_script()
GATE_SRC = io.open(os.path.join(SG, "gate.py"), encoding="utf-8").read()
QUORUM_SRC = io.open(os.path.join(SG, "quorum.py"), encoding="utf-8").read()
SHIP_SRC = io.open(os.path.join(ROOT, "ship.py"), encoding="utf-8").read()

# ============================================================== 1  the rendered shell parses
print("\n[1] the rendered remote scripts are structurally sound")
for name, sc in (("health", HEALTH), ("provision", PROV),
                 ("verdict-shipper+health", gate.ship_verdict_py() + HEALTH)):
    bad = heredoc_balance(sc)
    check(not bad, "%s: every heredoc is closed (%s)" % (name, "; ".join(bad) or "balanced"))
check(bool(heredoc_balance("cat > f <<'EOF'\nline\n")),
      "NEGATIVE: an unterminated heredoc IS detected (the one structural fault that silently "
      "swallows the rest of a script)")

def _bash_n(script):
    """Syntax-check a script by piping it to `bash -n` on STDIN, never via a PATH.

    THE DEFECT THIS FIXES, seen on the operator's machine:
        /bin/bash: C:UsersferanAppDataLocalTemptmpv9srpgars.sh: No such file or directory
    The scripts were valid the whole time. The harness wrote a temp file and passed its PATH, and
    the bash on a Windows box cannot resolve a Windows drive path -- the backslashes are eaten and
    it gets `C:UsersferanAppData...`, exit 127. So the ONE real syntax check in this file has been
    reporting a false failure on the only machine that runs it, which is worse than not having it:
    a check that fails on working code trains you to ignore it.

    stdin has no path to mangle, so it behaves identically everywhere. BYTES, not text: Python's
    text mode on Windows rewrites every \\n into \\r\\n and bash then chokes on the \\r -- the same
    CRLF trap already recorded for the ssh payloads and for `git archive`.

    (The sibling repo hit and fixed this exact thing in tests/test_decommission.py. It was never
    carried across. Same rule, both projects: a check that cannot run on the invoking platform is
    not a check.)
    """
    r = subprocess.run(["bash", "-n"], input=script.encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stderr.decode("utf-8", "replace")


if shutil.which("bash"):
    for name, sc in (("health", gate.ship_verdict_py() + HEALTH), ("provision", PROV)):
        rc, err = _bash_n(sc)
        check(rc == 0, "%s: bash -n says the shell is valid (%s)"
              % (name, (err or "clean").strip()[:150]))
    rc, _err = _bash_n("if true; then\n  echo x\n")
    check(rc != 0,
          "NEGATIVE: bash -n rejects an unbalanced script, so the two passes above are real")
else:
    check(True, "[!] bash is NOT on this machine, so `bash -n` DID NOT RUN this time. Only the "
                "heredoc check above ran. Saying so beats implying a syntax check happened.")

# ============================================================== 2  no wildcard scores a pass
print("\n[2] no branch may score an UNKNOWN answer as a PASS")
wild = []
for i, ln in enumerate(HEALTH.splitlines(), 1):
    s = ln.strip()
    if s.startswith("*)") and re.search(r"\bchk\s+\S+\s+yes\b", s):
        wild.append("line %d: %s" % (i, s[:90]))
check(not wild, "no `*) chk <name> yes` catch-all in the health script (%s)"
      % ("; ".join(wild) or "none"))
cases = re.findall(r"^\s*\*\)\s*chk\s+(\S+)\s+(yes|no)", HEALTH, re.M)
check(cases and all(v == "no" for _, v in cases),
      "every case fallback arm scores NO (%d fallback arm(s): %s)"
      % (len(cases), ", ".join("%s=%s" % c for c in cases)))
mutant = HEALTH.replace("*) chk no_published_ports no", "*) chk no_published_ports yes", 1)
check(mutant != HEALTH and any(
    ln.strip().startswith("*)") and re.search(r"\bchk\s+\S+\s+yes\b", ln)
    for ln in mutant.splitlines()),
    "NEGATIVE: the wildcard detector finds a reintroduced `*) chk ... yes`")

# ============================================================== 3  EXPECTED cannot drift
print("\n[3] EXPECTED and the script cannot drift apart")


def emitted_names(health, verdict_py):
    shell = set(re.findall(r"\bchk\s+([a-z0-9_]+)\s+(?:yes|no)\b", health))
    py = set(re.findall(r'^\s*out\("([a-z0-9_]+)"', verdict_py, re.M))
    for grp in re.findall(r"for n in \(([^)]*)\)", verdict_py):
        py |= {x.strip().strip('"').strip("'") for x in grp.split(",") if x.strip()}
    return {x for x in (shell | py) if x}


emit = emitted_names(HEALTH, gate.VERDICT_PY)
exp = set(gate.EXPECTED)
check(emit == exp, "every emitted name is EXPECTED and every EXPECTED name is emitted "
                   "(%d names; missing=%s extra=%s)"
      % (len(exp), sorted(exp - emit) or "-", sorted(emit - exp) or "-"))
check(len(gate.EXPECTED) == len(set(gate.EXPECTED)), "EXPECTED has no duplicate names")
check(len(gate.EXPECTED) >= 15, "at least 15 deterministic checks (%d) - a panel with thin "
                                "evidence produces confident nonsense" % len(gate.EXPECTED))
drifted = HEALTH + '\nchk brand_new_check yes "added without declaring it"\n'
check(emitted_names(drifted, gate.VERDICT_PY) != exp,
      "NEGATIVE: adding a chk line without declaring it in EXPECTED is caught")
check(emitted_names(HEALTH, gate.VERDICT_PY) - {"disk"} != exp,
      "NEGATIVE: removing a name from the emitted set is caught")
for n in ("engine_fresh", "api_auth", "app_served", "chat_stream_delivers", "job_completes",
          "container_running", "restart_count"):
    check(n in exp, "the check that transfers directly from the sibling design is present: %s" % n)

# ============================================================== 4  browser user agent
print("\n[4] page routes are probed with a BROWSER user agent")
check("Mozilla/5.0" in HEALTH and "UA='Mozilla" in HEALTH, "a browser UA is defined")
PAGE_ROUTES = ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/login")
for route in PAGE_ROUTES:
    pat = re.compile(re.escape(route) + r"(?=[\s>)\'\"]|$)")
    hits = [ln for ln in HEALTH.splitlines() if pat.search(ln) and "curl" in ln]
    check(bool(hits) and all('-A "$UA"' in h for h in hits),
          "all %d curl(s) to the PAGE route %s carry -A \"$UA\" (a curl-shaped UA gets the 404 a bot "
          "gate exists to return, and the check would then record a broken app)"
          % (len(hits), route))
_api = [ln for ln in HEALTH.splitlines() if "127.0.0.1:8000/api/" in ln and "curl" in ln]
check(len(_api) >= 4, "the API routes are probed too (%d curl(s)) - they need no browser UA, which "
                      "is why the rule above is scoped to page routes" % len(_api))

# ============================================================== 5  sentinel + rc
print("\n[5] the run must reach a terminal sentinel, and the ssh rc is never discarded")
check(HEALTH.rstrip().endswith('echo "#### HEALTH_END"'),
      "the health script's LAST statement is the terminal sentinel")
g = nocomments(GATE_SRC)
check('"#### HEALTH_END" not in out' in g and '"#### HEALTH_END" not in out2' in g,
      "BOTH passes assert the sentinel - partial ssh output is never counted as a complete run")
check(g.count("out, err, rc = ssh_script(ship_verdict_py() + health_script()") >= 1
      and "rc=%s" in g,
      "the ssh return code is captured and printed into the failure detail")
check("health_run_complete" in g, "a cut-short run produces a NAMED failing check")

# ============================================================== 6  one emitter, sane guard
print("\n[6] the CHECK| protocol: one emitter, newline/pipe squashed, runaway guard far above real")
check(HEALTH.count("printf 'CHECK|") == 1,
      "exactly ONE emitter of the protocol in the whole script (%d)" % HEALTH.count("printf 'CHECK|"))
m = re.search(r"tr '\\n\|' '  '.*cut -c1-(\d+)", HEALTH)
check(bool(m), "the emitter squashes newlines AND pipes before printing")
check(m and int(m.group(1)) >= 1000,
      "the runaway guard is >= 1000 chars (%s) - the sibling had it at 200 and silently amputated "
      "every long detail AT SOURCE for weeks" % (m.group(1) if m else "absent"))
check("out(name, ok, detail)" in gate.VERDICT_PY and '.replace("|", " ")' in gate.VERDICT_PY,
      "the python helper squashes pipes too, because its text becomes a CHECK| field")

# ============================================================== 7  the embedded helper
print("\n[7] the embedded verdict helper")
try:
    compile(gate.VERDICT_PY, "jhw_verdict.py", "exec")
    check(True, "VERDICT_PY compiles")
except SyntaxError as e:
    check(False, "VERDICT_PY does not compile: %s" % e)
check('"""' not in gate.VERDICT_PY,
      "VERDICT_PY contains no triple quote (one there terminates gate.py's own r-string - it did)")
check("py_compile /tmp/jhw_verdict.py" in gate.ship_verdict_py(),
      "the droplet compiles the helper before use, so a broken helper says so instead of printing "
      "nothing and losing three checks to 'missing evidence'")

# ============================================================== 8  the panel cannot set the gate
print("\n[8] the PANEL CANNOT SET THE GATE")
run_src = nocomments(g[g.index("def run(reboot_test"):])
i_panel = run_src.index("ask_panel(")
i_gate = run_src.index("quorum.gate_from_checks(")
i_set = run_src.index('verdict["gate"] = gate')
check(i_panel < i_gate < i_set,
      "run() asks the panel, THEN recomputes the gate from the checks, THEN overwrites whatever the "
      "panel claimed (order: ask=%d recompute=%d overwrite=%d)" % (i_panel, i_gate, i_set))
check("advisory" in run_src.lower() or "advisory" in g.lower(),
      "the code says out loud that the panel is advisory")
q = nocomments(QUORUM_SRC)
check("def gate_from_checks" in q and "def decide_from_verdict" in q,
      "the rule lives in ONE place and is imported by the caller, not duplicated")
check(quorum.tuning_is_wired() is False,
      "the panel is wired to tune NOTHING (section 7.2 step 5 is not earned yet)")
check("BOUNDS = {}" in q, "BOUNDS is committed and empty; adding a key is a reviewable diff")

# ============================================================== 9  unconfigured host SKIPS loudly
print("\n[9] an unconfigured staging host SKIPS LOUDLY and touches nothing")
check(not re.search(r'JHW_STAGING_HOST"\s*,\s*"\d', GATE_SRC) and
      not re.search(r"^STAGING\s*=\s*[\"']\d", GATE_SRC, re.M),
      "there is NO hardcoded staging IP - the host is a parameter or it does not exist")
_old_ssh, _old_env = gate.ssh_script, os.environ.pop("JHW_STAGING_HOST", None)


def _boom(*a, **k):
    raise AssertionError("an unconfigured gate must not touch any droplet")


gate.ssh_script = _boom
try:
    res, digest = gate.run()
    check(res == "SKIP", "an unset JHW_STAGING_HOST returns SKIP, not GO and not NO-GO (%r)" % res)
    check("NOT CONFIGURED" in digest and "JHW_STAGING_HOST" in digest,
          "the SKIP prints the ONE line that enables it")
    check("SKIP, not a pass" in digest,
          "the SKIP states plainly that nothing was validated - a check that cannot run must say so")
except AssertionError as e:
    check(False, "the unconfigured gate reached out anyway: %s" % e)
finally:
    gate.ssh_script = _old_ssh
    if _old_env:
        os.environ["JHW_STAGING_HOST"] = _old_env

# ============================================================== 10  the reboot test needs a reboot
print("\n[10] the reboot test cannot pass without a reboot")
_ob = gate.boot_id
try:
    gate.boot_id = lambda host="", quiet=False: ["SAME-BOOT-ID", "6.8.0-1", "999"]
    import time as _t
    _os_sleep = _t.sleep
    _t.sleep = lambda *_a: None
    got = gate.wait_for_reboot("203.0.113.9", ["SAME-BOOT-ID", "6.8.0-1", "10"], timeout=1)
    check(got == (None, None),
          "ssh answering INSTANTLY with the SAME boot_id is NOT a reboot (an earlier version of this "
          "test reported a 1-second reboot and passed)")
    gate.boot_id = lambda host="", quiet=False: ["NEW-BOOT-ID", "6.8.0-2", "5"]
    secs, now = gate.wait_for_reboot("203.0.113.9", ["SAME-BOOT-ID", "6.8.0-1", "10"], timeout=120)
    check(secs is not None and now[0] == "NEW-BOOT-ID",
          "a CHANGED boot_id is what counts as a reboot")
finally:
    gate.boot_id = _ob
    _t.sleep = _os_sleep
check("boot_id" in nocomments(GATE_SRC) and "random/boot_id" in GATE_SRC,
      "the identity compared is /proc/sys/kernel/random/boot_id, regenerated by the kernel on boot")

# ============================================================== 11-12  ship.py governance
print("\n[11-12] ship.py: the gate gates, and the deploy is not reimplemented")
sh = nocomments(SHIP_SRC)
main_src = sh[sh.index("def main("):]
i_stage = main_src.index("do_stage(")
i_deploy = main_src.index("do_deploy()")
check(i_stage < i_deploy, "the staging gate runs BEFORE the production deploy")
check('return 2' in main_src[i_stage:i_deploy] and 'gate_res == "NO-GO"' in main_src,
      "a NO-GO returns exit 2 before the deploy is reached")
# ---- THE CLASS, not the instance -------------------------------------------------------------
# THE OLD ASSERTION HERE WAS `check("jhw.cmd_deploy" in sh, ...)` -- it grepped ship.py's SOURCE
# for a string. `jhw.cmd_deploy` HAS NEVER EXISTED, so that check passed for years while the line
# it "verified" crashed on every run. FOUR of the five `jhw.<attr>` references in ship.py were
# imaginary: _check_requirements, _commit_and_push, cmd_deploy, py. This project's ONE command had
# never once run to completion, and nothing noticed because the only tests of it were greps.
#
# So: RESOLVE every attribute ship.py reaches for against what jhw.py actually DEFINES. A grep
# proves a string is present; this proves the call can be made.
_jhw_src = io.open(os.path.join(ROOT, "jhw.py"), encoding="utf-8").read()
_jhw_tree = ast.parse(_jhw_src)
_defined = {n.name for n in _jhw_tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
_defined |= {t.id for n in _jhw_tree.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
# AST, NOT REGEX. The first version matched `jhw.py` inside the STRING LITERAL "jhw.py" (a
# filename in a file list) and reported it as a missing attribute -- a check aimed at the wrong
# subject, which is the very defect this block exists to catch. An AST walk sees attribute
# ACCESSES on the name `jhw` and nothing else.
def _attrs_on(src, varname):
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == varname:
            out.add(n.attr)
    return sorted(out)

_refs = _attrs_on(sh, "jhw")
_ghosts = [r for r in _refs if r not in _defined]
check(not _ghosts,
      "every jhw.<attr> ship.py calls EXISTS in jhw.py (refs=%s%s)"
      % (", ".join(_refs) or "-", "" if not _ghosts else "  MISSING: " + ", ".join(_ghosts)))

# ...and the same for deploy_direct, which is now the real deploy engine.
_dd_tree = ast.parse(io.open(os.path.join(ROOT, "deploy_direct.py"), encoding="utf-8").read())
_dd = {n.name for n in _dd_tree.body
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
_dd |= {t.id for n in _dd_tree.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)}
_ddrefs = _attrs_on(sh, "dd")
_ddghosts = [r for r in _ddrefs if r not in _dd]
check(not _ddghosts,
      "every deploy_direct.<attr> ship.py calls EXISTS (refs=%s%s)"
      % (", ".join(_ddrefs) or "-", "" if not _ddghosts else "  MISSING: " + ", ".join(_ddghosts)))

check("dd.deploy(" in sh, "the deploy is delegated to the deploy ENGINE (deploy_direct.deploy), "
                          "not reimplemented")
check("docker compose" not in sh and "tarfile" not in sh and "scp" not in sh,
      "ship.py does NOT reimplement any part of the deploy")
check("--remove-orphans" not in SHIP_SRC and "--remove-orphans" not in GATE_SRC,
      "nothing here ever passes --remove-orphans (it would delete the neighbours' containers on the "
      "shared host)")
check("-p 8000" not in HEALTH and "0.0.0.0:" not in HEALTH,
      "the gate never publishes a port to probe the app; it uses docker exec")
check("git(\"push\"" in _code_only(sh) and "git(\"commit\"" in _code_only(sh),
      "commit+push is implemented HERE with the one git() helper - jhw.py has NO git helper at "
      "all, and asserting it did is what let `jhw._commit_and_push` crash on every run")
check("nothing to commit - pushing anyway" in sh,
      "the push is UNCONDITIONAL: a commit that never leaves the PC is not a backup, and the "
      "droplet has no independent history")
check("tag_known_good" in sh and "last-known-good" in sh and "--rollback" in SHIP_SRC,
      "a verified release writes a safe point, and there is a rollback path to it")
check(sh.index("tag_known_good()") > sh.index("do_verify()"),
      "the safe point is tagged only AFTER verification (never tag an unverified state)")

# NO-GO must actually stop the release. Functional, with the expensive stages stubbed out.
sys.path.insert(0, ROOT)
import ship  # noqa: E402
calls = []
_saved = (ship.do_tests, ship.do_git, ship.do_stage, ship.do_deploy, ship.do_verify,
          ship.pack_fingerprint, ship.tag_known_good, sys.argv)
try:
    ship.do_tests = lambda: True
    ship.do_git = lambda m: True
    ship.pack_fingerprint = lambda: "fp"
    ship.do_deploy = lambda: calls.append("deploy") or True
    ship.do_verify = lambda: calls.append("verify") or True
    ship.tag_known_good = lambda: calls.append("tag") or "t"
    sys.argv = ["ship.py"]

    ship.do_stage = lambda reboot_test=True: ("NO-GO", "the twin failed 3 checks")
    rc = ship.main()
    check(rc == 2 and not calls,
          "NO-GO: ship.main() exits 2 and NOTHING was deployed, verified or tagged (calls=%r)" % calls)

    calls.clear()
    ship.do_stage = lambda reboot_test=True: ("SKIP", "no twin configured")
    rc = ship.main()
    check(rc == 0 and calls == ["deploy", "verify", "tag"],
          "SKIP: the release continues (loudly unvalidated) - an unprovisioned twin must not block "
          "the operator (calls=%r)" % calls)

    calls.clear()
    ship.do_stage = lambda reboot_test=True: ("GO", "clean")
    ship.do_verify = lambda: calls.append("verify") or False
    rc = ship.main()
    check(rc == 1 and "tag" not in calls,
          "a FAILED verify returns non-zero and writes NO safe point - never print DONE on a path "
          "where a sub-step failed (calls=%r)" % calls)

    calls.clear()
    ship.do_verify = lambda: calls.append("verify") or True
    fps = iter(["before", "AFTER-AN-EDIT"])
    ship.pack_fingerprint = lambda: next(fps)
    rc = ship.main()
    check(rc == 2 and "deploy" not in calls,
          "the source changing MID-FLIGHT stops the promotion: staging validated one artefact and "
          "production would receive another (calls=%r)" % calls)
finally:
    (ship.do_tests, ship.do_git, ship.do_stage, ship.do_deploy, ship.do_verify,
     ship.pack_fingerprint, ship.tag_known_good, sys.argv) = _saved

# ============================================================== 13  engine_is_current fails closed
print("\n[13] engine_is_current fails CLOSED when it cannot see its subject")


class _R:
    def __init__(self, out, rc=0):
        self.stdout, self.returncode, self.stderr = out.encode(), rc, b""


_orun = subprocess.run
try:
    subprocess.run = lambda *a, **k: _R("ENGINE aaa 21 aaa 21\nSTATE running 0\n")
    ok, detail = ship.engine_is_current()
    check(ok is True and "21 files" in detail, "matching hashes over 21 files -> current")
    subprocess.run = lambda *a, **k: _R("ENGINE aaa 21 bbb 21\nSTATE running 0\n")
    ok, detail = ship.engine_is_current()
    check(ok is False and "STALE CONTAINER" in detail,
          "different hashes -> STALE (a liveness probe is not a deploy proof)")
    subprocess.run = lambda *a, **k: _R("", 255)
    ok, detail = ship.engine_is_current()
    check(ok is False and "cannot see its subject" in detail,
          "an ssh failure is reported as NO EVIDENCE, never as a match")
    subprocess.run = lambda *a, **k: _R("ENGINE '' 0 '' 0\nSTATE absent\n")
    ok, detail = ship.engine_is_current()
    check(ok is False and "not a" in detail,
          "two EMPTY file lists are equal and must NOT be read as a match")
finally:
    subprocess.run = _orun

# ====================================== THE WEBSITE DEPLOY MUST NOT NEED DOCKER ON THIS MACHINE
# `python ship.py` ships jobhuntwow.com: the tarball travels in ONE ssh session and the DROPLET
# builds the image. The LOCAL apply sandbox (Chromium + noVNC, for Workday/Ashby) is a different
# product driven by `python jhw.py`, and on 2026-09-18 its verbs learned to demand — and start — a
# Docker daemon. deploy_direct.py was calling that orchestrator's `cmd_status` at the end, so a
# WEBSITE deploy would have finished by demanding Docker Desktop for no reason at all.
import ast as _ast2

with open(os.path.join(ROOT, "deploy_direct.py"), encoding="utf-8") as _fh:
    _ddsrc = _fh.read()
_ddt = _ast2.parse(_ddsrc)
_ddcalls = {c.func.id if isinstance(c.func, _ast2.Name) else c.func.attr
            for c in _ast2.walk(_ddt) if isinstance(c, _ast2.Call)
            and isinstance(c.func, (_ast2.Name, _ast2.Attribute))}
check("cmd_status" not in _ddcalls,
      "the web deploy does not call the apply sandbox's status verb")
check("spec_from_file_location" not in _ddcalls,
      "...and does not load agent/jhw.py at all")
# Every `docker` in this file must be part of the REMOTE script, never a local subprocess.
_ddlocal = [c for c in _ast2.walk(_ddt) if isinstance(c, _ast2.Call)
            and isinstance(c.func, _ast2.Attribute) and c.func.attr in ("run", "Popen", "call")
            and any(isinstance(a, _ast2.Constant) and "docker" in str(a.value) for a in c.args)]
check(not _ddlocal, "no local `docker ...` subprocess in the website deploy path")
_shipsrc = open(os.path.join(ROOT, "ship.py"), encoding="utf-8").read()
_shipt = _ast2.parse(_shipsrc)
_shiplocal = [c for c in _ast2.walk(_shipt) if isinstance(c, _ast2.Call)
              and isinstance(c.func, _ast2.Attribute) and c.func.attr in ("run", "Popen", "call")
              and any(isinstance(a, _ast2.Constant) and str(a.value).startswith("docker")
                      for a in ([c.args[0]] if c.args else []))]
check(not _shiplocal, "ship.py never runs docker on the operator's machine either")
# And the suites ship.py runs must be plain python, so the website can be tested with nothing
# installed but Python — that is what makes "no Docker for the web" true rather than hopeful.
check('[sys.executable, p]' in _shipsrc,
      "ship.py runs its suites with this interpreter, not inside a container")

# ================================ EVERY HOSTNAME WE SERVE MUST ALSO BE ONE WE CLAIM
# `deploy/caddy/jobhuntwow.caddy` says what Caddy serves; `fix_caddy.OURS` says which hostnames the
# deploy STRIPS from any other block on that droplet. A name in the first and not the second is
# half-wired: we serve it while somebody else's vhost can still claim it — which is exactly how the
# old one-pager kept jobhuntwow.com for six "successful" deploys.
import domains as _dom
import importlib.util as _ilu

_fc_spec = _ilu.spec_from_file_location("fix_caddy", os.path.join(ROOT, "deploy", "fix_caddy.py"))
_fc = _ilu.module_from_spec(_fc_spec)
_fc_spec.loader.exec_module(_fc)

_served = set(_dom.hostnames())
check(len(_served) >= 4, "the managed Caddy block names its hostnames (%s)" % ", ".join(sorted(_served)))
check(_served <= set(_fc.OURS),
      "every hostname we serve is one the deploy also claims (fix_caddy.OURS)")
check(_dom.CANON in _served, "the canonical host is one of them")

# THE DOCTRINE CHANGED ON 2026-09-21, AND THIS CHECK CHANGED WITH IT.
# It used to read: "every `redir` in the Caddy block points at the canonical host, one hop", because
# www.jobhuntwow.com and jobhw.org were redirected BY CADDY. That was correct about hops and wrong
# about visibility: a request answered by the proxy never reaches the only process that writes an
# event, so nobody who typed the short domain appeared anywhere in our record and the operator's
# question -- "show me everyone trying to enter jobhw.org" -- had no answer at all. The redirect now
# happens in the APPLICATION (backend/app/hosts.py), after the request has been observed.
# The property is unchanged and is now checked where it actually lives: every hostname we serve is
# either the canonical host or one the app redirects, in ONE hop, to the canonical host.
_blk = open(os.path.join(ROOT, "deploy", "caddy", "jobhuntwow.caddy"), encoding="utf-8").read()
_targets = re.findall(r"redir\s+(https://[^\s{]+)", _blk)
# `all([])` is True, so this line alone could no longer fail once the redirects left the proxy.
# It is kept only as the belt for a redirect somebody re-adds, and the braces are the two checks
# below it, which are falsifiable (proven by removing a hostname from _hosts.REDIRECT_HOSTS).
check(all(t == "https://" + _dom.CANON for t in _targets),
      "any redirect left in the proxy still points at the canonical host, one hop (%s)"
      % (", ".join(sorted(set(_targets))) or "none - the app owns the redirect now"))
check(bool(_targets) or "reverse_proxy" in _blk,
      "the block either redirects or proxies - an empty block would pass every check above it")

sys.path.insert(0, os.path.join(ROOT, "backend"))
from app import hosts as _hosts                                          # noqa: E402
check(_hosts.CANONICAL == _dom.CANON,
      "the app and the deploy agree on which host is canonical (%s)" % _hosts.CANONICAL)
_unclaimed = sorted(h for h in _served if h != _dom.CANON and h not in _hosts.REDIRECT_HOSTS)
check(not _unclaimed,
      "every hostname Caddy serves is either canonical or redirected by the app (%s)"
      % (", ".join(_unclaimed) or "none unclaimed"))
_hops = {h: _hosts.target(h, "/pipeline", "a=1") for h in _hosts.REDIRECT_HOSTS}
check(all(v == "https://" + _dom.CANON + "/pipeline?a=1" for v in _hops.values()),
      "and each of them lands on the canonical host in ONE hop, path and query intact")
check(_hosts.target(_dom.CANON, "/") is None,
      "the canonical host is never itself redirected (that is an infinite loop)")
check(_hosts.target("evil.example", "/") is None,
      "a hostname we do not own is served, never bounced on its own say-so (no open redirect)")

# ============================================================== THE GATE (LAST STATEMENT)
print("\n%s\n%d checks run, %d failed" % ("=" * 74, RUN[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)
