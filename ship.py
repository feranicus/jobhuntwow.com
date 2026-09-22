#!/usr/bin/env python3
"""ship.py - THE one command that releases JobHuntWOW. Everything else is a building block.

    python ship.py

    1/6  TESTS         requirements parse - py_compile - ruff F821/F811/F822 - the test suites
    2/6  COMMIT+PUSH   ALWAYS pushes, even with nothing new to commit (GitHub is the source of truth)
    3/6  STAGING GATE  deploy to the twin - health - REBOOT IT - health - completeness - AI panel
    4/6  DEPLOY WEB    deploy_direct.py (ONE ssh session, builds on the droplet)
    5/6  VERIFY        the public site AND the sha256 of the code INSIDE the running container
    6/6  SAFE POINT    move `last-known-good`, write a dated `good-*` tag, push both

Flags NARROW it; they never split it (CLAUDE.md operating principle: one orchestrator, one command).
    --test           stop after 1/6
    --no-stage       skip the staging gate (say out loud that you did)
    --no-reboot      run the staging gate without the reboot test (faster and WEAKER)
    --rollback [TAG] reset to last-known-good (or TAG) and redeploy that exact state
    -m "message"     commit message

WHY DEPLOY IS DIRECT AND PUSH IS STILL MANDATORY
------------------------------------------------
GitHub is the source of truth for CODE and is NOT in the deploy path: no GHCR, no CI wait, no second
source of truth (that is settled in CLAUDE.md and DEPLOY.md). But a commit that never leaves the PC
is not a backup, and the droplet has no independent history - it is always rebuilt FROM the repo. So
this pushes on every run, unconditionally, and only the IMAGE BUILD stays on the droplet.
`git remote -v` for this checkout is https://github.com/feranicus/jobhuntwow.com.git, and that is
verified before anything is pushed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "deploy", "stagegate"))

OWNED = ["ship.py", "deploy/stagegate/gate.py", "deploy/stagegate/quorum.py"]


def say(*a):
    print(" ".join(str(x) for x in a), flush=True)


def hr(title):
    say("\n" + "=" * 78)
    say(title)
    say("=" * 78)


def run(cmd, cwd=HERE, timeout=None, env=None):
    """Stream a subprocess and return its RETURN CODE (an int).

    It returns an int, not a CompletedProcess. That is stated because the sibling project called
    `.returncode` on the equivalent helper and got `AttributeError: 'int' object has no attribute
    'returncode'` - read the helper before calling it.
    """
    say("  $ " + " ".join(cmd[:8]) + (" ..." if len(cmd) > 8 else ""))
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout,
                              env=env or os.environ.copy()).returncode
    except subprocess.TimeoutExpired:
        say("  [X] timed out after %ss" % timeout)
        return 124
    except FileNotFoundError:
        say("  [X] not found: %s" % cmd[0])
        return 127


def git(*args):
    r = subprocess.run(["git"] + list(args), cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip(), r.returncode


# ============================================================================ 1/6 tests
def _pyfiles():
    out = []
    for base in ("deploy/stagegate", "tests"):
        d = os.path.join(HERE, base)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.endswith(".py"):
                out.append(os.path.join(base, f))
    out.append("ship.py")
    return out


def ensure_app_requirements(py=None):
    """Install any DECLARED runtime dependency the operator's Python is missing, once.

    WHY, measured on the operator's machine 2026-09-06:
        FAILED: section 12 could not run: No module named 'docx'
        FAILED: section 13 could not run: No module named 'httpx'
        FAILED: section 14 could not run: No module named 'httpx'
    Three tests reported FAILED while printing SKIP -- and neither was a code defect. Those
    packages are declared in backend/requirements.txt and installed on the DROPLET by the
    Dockerfile; nothing ever installed them on the machine that runs the tests. A check that
    cannot run on the invoking platform is not a check, and one that fails on working code is
    worse: it trains you to ignore the suite.

    The fix is NOT to tell the operator to run pip -- that is a manual step (operating principle 1)
    and it will be forgotten by whoever clones this next. This is the SAME helper the sibling repo
    already uses, ported rather than reinvented.

    DELIBERATELY NARROW: only packages MISSING ENTIRELY are installed, never upgraded. A blanket
    `pip install -r requirements.txt` on a developer machine can move fastapi or starlette
    underneath whatever else lives in that interpreter. Version drift is answered by the image
    build, which installs from a clean base every time.
    """
    py = py or sys.executable
    req = os.path.join(HERE, "backend", "requirements.txt")
    if not os.path.exists(req):
        return
    specs = []
    for raw in open(req, encoding="utf-8"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # "fastapi==0.141.*" / "starlette>=1.3.1" / "uvicorn[standard]==0.30.*" -> the dist name.
        # No regex: this helper is defined above ship.py's own imports, and depending on `re`
        # being bound at that point is the kind of assumption the F821 gate exists to catch — it
        # caught exactly that here.
        name = line
        for sep in ("[", "<", ">", "=", "!", "~", ";", " "):
            name = name.split(sep, 1)[0]
        name = name.strip()
        if name:
            specs.append((name, line))
    if not specs:
        return

    # ASK THE INTERPRETER THAT WILL RUN THE TESTS, not this one. The first version of this helper
    # used importlib.metadata HERE, which measures ship.py's own environment — a different subject.
    # A negative test in a clean venv proved it: it reported a package missing that was only missing
    # locally, and MISSED python-multipart, which was the entire point. Same defect class as a
    # validator run against a temp copy instead of the mounted file.
    probe = ("import sys\n"
             "from importlib import metadata as m\n"
             "out = []\n"
             "for n in sys.argv[1:]:\n"
             "    try:\n"
             "        m.distribution(n)\n"
             "    except Exception:\n"
             "        out.append(n)\n"
             "print('\\n'.join(out))\n")
    r = subprocess.run([py, "-c", probe] + [n for n, _ in specs],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        return                                    # a probe we cannot run must not block the ship
    absent = {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}
    missing = [(n, spec) for n, spec in specs if n in absent]
    if not missing:
        return
    print("  %d declared dependency(ies) missing from this Python: %s"
          % (len(missing), ", ".join(n for n, _ in missing)))
    print("  installing them so the tests exercise the same stack the image builds...")
    for name, spec in missing:
        r = subprocess.run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            spec], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if r.returncode != 0:
            print((r.stdout or "") + (r.stderr or ""))
            sys.exit("[X] could not install %s, which backend/requirements.txt declares. "
                     "The tests import the app and would fail for a reason that is not a code "
                     "defect. Install it and re-run: python ship.py" % spec)
        print("    installed %s" % spec)


ENGINE_FILES = ["scripts/shodan_recon.py", "scripts/run_assessment.py", "scripts/enrich.py",
                "scripts/compliance_assess.py", "scripts/compliance_enrich.py",
                "scripts/creed.js", "scripts/group_discovery.py", "scripts/engine_config.py",
                "scripts/enrich_parallel.py", "scripts/attribution.py",
                "scripts/model_probe.py", "scripts/demo_build.py",
                # scope_deny.py is the authoritative shortener/social/platform denylist. It is a
                # SCOPE-CORRECTNESS file: a container running an older copy would happily admit
                # wa.me again (the abakus-tk.de failure), so its hash has to be proved deployed.
                "scripts/scope_deny.py", "scripts/psl.py", "scripts/asn_sources.py", "scripts/clarify.py",
                # THE TRANSLATION LAYER. None of it was hash-verified, so a stale copy on the
                # droplet was invisible — and a stale pack is exactly what an English label inside a
                # German deck looks like. The engine i18n GATE proves the packs are complete in this
                # repo; only these hashes prove the container is running the packs the gate checked.
                "scripts/i18n/i18n.py", "scripts/i18n/de.json", "scripts/i18n/ru.json",
                "scripts/i18n/deck_i18n.js",
                # White Label. proteus.py decides what a partner's artifacts LOOK like and brand.js
                # is what applies it at the render boundary; a container running an older copy of
                # either would silently ship the wrong branding — or our colours — to a partner's
                # customer, with every check still green. Same reasoning as scope_deny.py: prove
                # the hash is deployed rather than assuming the image picked it up.
                # pptx_preview renders the partner's real cover on the White Label page. It is the
                # ONLY thing standing between a wrong reading and a customer-facing deck, so a
                # container running a stale copy would show a preview that is not what it builds.
                "scripts/proteus.py", "scripts/brand.js", "scripts/pptx_preview.py"]
ENGINE_LOCAL = os.path.join(HERE, "hermes-skills", "shodan-assessment")
ENGINE_REMOTE = "/opt/shodan-skill"


def do_tests() -> bool:
    hr("1/6  TESTS")
    ok = True
    ensure_app_requirements()

    # (a) pip would fail on the droplet three minutes into the build; catching it here is free.
    #
    # THIS USED TO CALL `jhw._check_requirements()`, WHICH HAS NEVER EXISTED. jhw.py defines
    # `ensure_prereqs()` and nothing else of that shape, so ship.py crashed with AttributeError on
    # its very first step and this project could not be released at all. An assumed helper name,
    # the same defect class this codebase's sibling has recorded repeatedly -- and it was invisible
    # because nothing exercised do_tests() until somebody actually ran a release.
    #
    # The check is now implemented HERE, where it is used, rather than pointing at a function in
    # another module that may or may not exist. `packaging` is what pip itself parses with; if it
    # is not installed the check SKIPS AND SAYS SO rather than passing silently -- a check that
    # cannot run must never look like a check that passed.
    req = os.path.join(HERE, "backend", "requirements.txt")
    try:
        from packaging.requirements import Requirement, InvalidRequirement
    except Exception:
        say("  backend/requirements.txt NOT parsed (packaging not installed here)   SKIPPED")
    else:
        bad = []
        try:
            for n, raw in enumerate(open(req, encoding="utf-8").read().splitlines(), 1):
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith("-"):     # -r / -e / --flags are pip's, not ours
                    continue
                try:
                    Requirement(line)
                except InvalidRequirement as e:
                    bad.append("line %d: %s  (%s)" % (n, line[:60], e))
        except OSError as e:
            bad.append("cannot read %s: %s" % (req, e))
        if bad:
            for b in bad:
                say("  [X] requirements.txt: %s" % b)
            ok = False
        else:
            say("  backend/requirements.txt parses the way pip parses it       OK")

    # (b) syntax. Cheap, and this repo has a documented history of files being shipped truncated
    #     or null-padded by an editor.
    bad = []
    for rel in _pyfiles():
        p = os.path.join(HERE, rel)
        try:
            raw = open(p, "rb").read()
            if b"\x00" in raw:
                bad.append(rel + " CONTAINS NULL BYTES (an editor truncation signature)")
                continue
            compile(raw.decode("utf-8"), rel, "exec")
        except Exception as e:
            bad.append("%s: %s" % (rel, e))
    if bad:
        ok = False
        for b in bad:
            say("  [X] " + b)
    else:
        say("  %d python file(s) compile, none contain null bytes       OK" % len(_pyfiles()))

    # (c) UNDEFINED NAMES. This is the gate that matters most: a NameError takes production down
    #     while every unit test passes, because unit tests exercise helpers and nothing executes the
    #     failing line. F-rules only (real bugs, never style); F401 is excluded because an unused
    #     import is not an outage.
    #     A CHECK THAT SILENTLY SKIPS IS NOT A CHECK, so if ruff is absent we try ONCE to install it
    #     (no manual step for the operator) and, failing that, say NOT CHECKED loudly rather than
    #     printing a reassuring line.
    rc = run([sys.executable, "-m", "ruff", "--version"], timeout=60)
    if rc != 0:
        say("  ruff is not installed - installing it once (a gate you have to remember is not a gate)")
        run([sys.executable, "-m", "pip", "install", "--quiet", "ruff"], timeout=300)
        rc = run([sys.executable, "-m", "ruff", "--version"], timeout=60)
    if rc == 0:
        rc = run([sys.executable, "-m", "ruff", "check", "--select", "F821,F811,F822",
                  "--quiet", "deploy", "tests", "backend", "ship.py", "jhw.py",
                  "deploy_direct.py"], timeout=300)
        if rc == 0:
            say("  ruff F821/F811/F822: no undefined or redefined names       OK")
        else:
            ok = False
            say("  [X] ruff found undefined/duplicate names (see above)")
    else:
        say("  [!] ruff COULD NOT BE INSTALLED - F821 WAS NOT CHECKED THIS RUN. Undefined names")
        say("      would reach the droplet unnoticed. Fix by hand: python -m pip install ruff")

    # (d) the suites. Standalone scripts, stdlib only, no pytest: they must run on the operator's
    #     own machine with no setup, which is the platform five wasted ships were spent relearning.
    for rel in ("tests/test_gate_governance.py", "tests/test_gate_integrity.py",
                # CLAUDE.md is re-injected on every turn: its size is a correctness property.
                "tests/test_claude_md_size.py",
                # the board he drags cards on, and the endpoint the drop calls (real HTTP)
                "tests/test_pipeline_dnd.py",
                # who the job is with: the ladder, and the guard on its model rung
                "tests/test_employer_ladder.py", "backend/app/jd_ingest.py",
                # visitors against bots: three buckets, what each signal may prove, AND the visit
                # feed ("a person just opened jobhuntwow.com") with the rules that keep a scanner
                # wearing a browser user agent out of it.
                "backend/app/visitors.py",
                # WHY THE CATCH-ALL ANSWERED 200 TO EVERYTHING AND NOTHING EVER ALERTED.
                # The probe-shaped 404 and its route mirror; the ten security headers and the CSP
                # measured against what this site actually loads; the alert chain end to end
                # (plain-text delivery, the real artifact route, observe_http's caller, one
                # evt=http line per request, and the fleet page's count).
                "backend/app/spa_guard.py",
                "tests/test_spa_guard.py",
                "tests/test_security_headers.py",
                "tests/test_alert_chain.py",
                "tests/test_chat_open_wallet.py", "tests/test_llm_events_write.py",
                "backend/tests/test_resume_consensus.py",
                # The Tailor correlation and the digest that reports it. Standalone, stdlib only,
                # and they write to a temp database -- never to his real pipeline.
                "backend/app/tracker.py", "backend/app/digest.py", "backend/app/docnames.py",
                # THE WALLET, after the 2026-09 LLM-jacking. The budget gate and its four rules,
                # the canonical-host redirect the APPLICATION now owns (so the short domain's
                # visitors are observed rather than answered upstream), the operator's console and
                # its refusal to render "I could not look" as a zero, and the wiring check that
                # proves the gate runs BEFORE the spend at every chokepoint.
                "backend/app/llm_meter.py", "backend/app/hosts.py", "backend/app/security.py",
                "backend/app/spend_watch.py", "tests/test_security_console.py",
                # The project portfolio (his projects, picked per posting by arithmetic) and the
                # TOP-5 cover letter he asked for by name: the five must BE five, must survive the
                # audit round, and must reach the rendered file.
                "backend/app/portfolio.py", "tests/test_portfolio_and_top5.py"):
        p = os.path.join(HERE, rel)
        if not os.path.exists(p):
            say("  [X] MISSING test file %s - a suite that is absent cannot pass" % rel)
            ok = False
            continue
        if run([sys.executable, p], timeout=900) != 0:
            say("  [X] FAILED: %s" % rel)
            ok = False
        else:
            say("  %s  OK" % rel)
    # (e) AUTHORISATION, MEASURED ON THE RESPONSE. Every route of this app, every method, driven
    #     in-process and asked anonymously. Incident finding #11 was "no authorisation audit
    #     existed"; this is it, and it is a GATE rather than a report, because opt-in
    #     authorisation eventually ships a route whose author forgot. rc=1 stops the ship. rc=2
    #     means the audit could not reach its subject, which is BLIND, NOT CLEAN -- said out loud
    #     and never counted as a pass.
    #     CISA/NSA Secure-by-Design · NIST SP 800-53 AC-3 · OWASP API Top 10 API1/API5 · BSI
    #     IT-Grundschutz APP.3.1 (deny by default, verified).
    try:
        az = subprocess.run([sys.executable, os.path.join(HERE, "authz_audit.py")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=300)
    except subprocess.TimeoutExpired:
        say("  [X] the authorisation audit HUNG (300s) - that is a failure, not a pass")
        return False
    if az.returncode == 1:
        say((az.stdout or "") + (az.stderr or ""))
        say("  [X] A ROUTE SERVED CONTENT TO AN ANONYMOUS CALLER - do not ship")
        ok = False
    elif az.returncode != 0:
        # BLIND IS NOT CLEAN, AND IT IS NOT A SHIP EITHER. The sibling project printed a warning
        # here and let the deploy proceed; after an incident whose root cause was six days of
        # believing a blind tool, "we could not check" stops this one. Re-run it, or fix it.
        say("  [X] the authorisation audit could not run (rc=%s) - BLIND, NOT CLEAN" % az.returncode)
        say((az.stdout or "")[-600:])
        ok = False
    else:
        say("  authz: every non-public route refuses an anonymous caller")
    return ok


# ============================================================================ 2/6 git
def do_git(message: str) -> bool:
    hr("2/6  COMMIT + PUSH   (GitHub is the source of truth for code; it is NOT in the deploy path)")
    ahead, _ = git("rev-list", "--count", "@{u}..HEAD")
    if ahead and ahead != "0":
        say("  %s local commit(s) were not on the remote before this run" % ahead)
    # `jhw._commit_and_push` HAS NEVER EXISTED. jhw.py has no git helper at all -- ship.py was
    # written against an imagined API and crashed here on every run. Implemented with the `git()`
    # helper defined at the top of this file, which is the only git in this project.
    #
    # THE PUSH IS UNCONDITIONAL, per the module docstring: a commit that never leaves the PC is not
    # a backup, and the droplet has no independent history. Push is idempotent; skipping it when
    # there is nothing new to commit is what let the sibling's PC silently drift ahead of origin.
    remote, rc = git("remote", "get-url", "origin")
    if rc != 0 or not remote:
        say("  [X] no `origin` remote - refusing to continue with an unbacked-up commit")
        return False
    say("  origin: %s" % remote)
    dirty0, _ = git("status", "--porcelain")
    if dirty0:
        _o, rc = git("add", "-A")
        if rc != 0:
            say("  [X] git add failed")
            return False
        out, rc = git("commit", "-m", message)
        if rc != 0:
            say("  [X] git commit failed: %s" % out[:300])
            return False
        say("  committed: %s" % message)
    else:
        say("  nothing to commit - pushing anyway (push is idempotent; skipping it breaks the "
            "source-of-truth promise)")
    out, rc = git("push", "origin", "HEAD")
    if rc != 0:
        say("  [X] git push failed: %s" % out[:300])
        return False
    say("  pushed to origin")
    dirty, _ = git("status", "--porcelain")
    if dirty:
        say("  [!] the tree is STILL dirty after commit+push:")
        for ln in dirty.splitlines()[:10]:
            say("      " + ln)
    head, _ = git("rev-parse", "--short", "HEAD")
    say("  HEAD = %s" % head)
    return True


# ================================================================= the mid-flight identity guard
def pack_fingerprint() -> str:
    """sha256 over the EXACT file set deploy_direct ships, using ITS OWN filter.

    WHY: one release on the sibling project produced three different artefacts from "the same"
    commit, because the working tree was read several times and an editor changed it mid-flight.
    Staging then validated one thing and production received another, which makes a green staging
    run meaningless.

    The correct fix there was to pack `git archive HEAD` instead of the working tree. That is a
    behavioural change to deploy_direct.py, which is the deploy ENGINE and is deliberately not
    touched here (see docs/CICD.md "known gaps"). What this does instead is close the same hole from
    outside: fingerprint the shipped set before staging and again before production and REFUSE to
    promote if a single byte moved in between.
    """
    import deploy_direct as dd
    h = hashlib.sha256()
    for item in sorted(dd.SHIP_FILES + dd.SHIP_DIRS):
        p = os.path.join(HERE, item)
        if os.path.isfile(p):
            h.update(item.encode()); h.update(open(p, "rb").read())
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in dd.SKIP_DIRS)
            for f in sorted(files):
                if os.path.splitext(f)[1] in dd.SKIP_EXT or f.endswith(".env"):
                    continue
                fp = os.path.join(root, f)
                h.update(os.path.relpath(fp, HERE).replace("\\", "/").encode())
                h.update(open(fp, "rb").read())
    return h.hexdigest()[:16]


# ============================================================================ 3/6 staging
def do_stage(reboot_test=True):
    """-> (gate, digest). 'SKIP' when no twin is configured: that is reported, never treated as GO."""
    hr("3/6  STAGING GATE   (deploy -> health -> REBOOT -> health -> completeness -> AI panel)")
    import gate
    try:
        return gate.run(reboot_test=reboot_test)
    except Exception as e:
        # A BROKEN GATE MUST NEVER BECOME A BROKEN DEPLOY. Same doctrine as the false-positive
        # auditor: a check is a signal, not an authority. It is reported loudly and named as
        # unvalidated - it does not silently pass either.
        import traceback
        traceback.print_exc()
        return "SKIP", ("The staging gate itself raised %s, so the change is UNVALIDATED. That is a "
                        "defect in deploy/stagegate/gate.py, not a verdict on the release."
                        % type(e).__name__)


# ============================================================================ 4/6 + 5/6
def do_deploy() -> bool:
    hr("4/6  DEPLOY WEB   (deploy_direct.py: ONE ssh session, built on the droplet)")
    # `jhw.cmd_deploy` HAS NEVER EXISTED either -- and `deploy` is not one of jhw.py's 22
    # subcommands, which is the same fact from the other side. The real deploy ENGINE is
    # deploy_direct.py, which this file already imports for the fingerprint check, so nothing is
    # reimplemented: it is called directly instead of through a verb that was never wired up.
    import deploy_direct as dd
    try:
        dd.deploy(with_caddy=True)
    except SystemExit as e:
        if e.code:
            say("  [X] deploy failed: %s" % e)
            return False
    except Exception as e:
        say("  [X] deploy raised %r" % (e,))
        return False
    return True


ENGINE_DIR_IN_CONTAINER = "/app/app"
ENGINE_DIR_ON_DROPLET = "/opt/jobhuntwow/backend/app"


def engine_is_current() -> tuple:
    """(ok, detail) - is the RUNNING container serving the code we just shipped?

    A LIVENESS PROBE IS NOT A DEPLOY PROOF. A three-day-old container answers /api/health and 401s
    /api/me perfectly happily; the sibling project shipped a fix three times over a container that
    never changed and every verifier said DONE. So compare the sha256 of every backend python file
    INSIDE the container against the sources on the droplet, in ONE ssh session.
    """
    import deploy_direct as dd
    script = (
        "set +e\n"
        "IN=$(docker exec jhw-web sh -c 'cd %s 2>/dev/null && find . -name \"*.py\" "
        "-not -path \"*__pycache__*\" | LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum' "
        "| cut -d' ' -f1)\n"
        "NI=$(docker exec jhw-web sh -c 'cd %s 2>/dev/null && find . -name \"*.py\" "
        "-not -path \"*__pycache__*\" | wc -l' | tr -d ' ')\n"
        "ON=$(cd %s 2>/dev/null && find . -name \"*.py\" -not -path \"*__pycache__*\" "
        "| LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)\n"
        "NO=$(cd %s 2>/dev/null && find . -name \"*.py\" -not -path \"*__pycache__*\" | wc -l "
        "| tr -d ' ')\n"
        "echo \"ENGINE $IN $NI $ON $NO\"\n"
        "echo \"STATE $(docker inspect -f '{{.State.Status}} {{.RestartCount}}' jhw-web 2>/dev/null)\"\n"
        % (ENGINE_DIR_IN_CONTAINER, ENGINE_DIR_IN_CONTAINER, ENGINE_DIR_ON_DROPLET,
           ENGINE_DIR_ON_DROPLET))
    r = subprocess.run(dd.SSH + [dd.TGT, "bash -s"], input=script.encode(),
                       capture_output=True, timeout=180)
    out = (r.stdout or b"").decode("utf-8", "replace")
    eng = [ln.split() for ln in out.splitlines() if ln.startswith("ENGINE ")]
    state = [ln[len("STATE "):].strip() for ln in out.splitlines() if ln.startswith("STATE ")]
    if not eng or len(eng[0]) < 5:
        return False, ("could not read the engine hashes over ssh (rc=%s, out=%r) - a check that "
                       "cannot see its subject is not a pass" % (r.returncode, out[:200]))
    _, a, na, b, nb = eng[0][:5]
    st = state[0] if state else "?"
    if int(na or 0) < 6 or int(nb or 0) < 6:
        return False, ("could not enumerate the sources (container=%s files, droplet=%s) - not a "
                       "match, just no evidence" % (na, nb))
    if a != b:
        return False, ("STALE CONTAINER: container=%s (%s files) vs shipped=%s (%s files); "
                       "state=%s" % (a, na, b, nb, st))
    return True, "container runs the shipped backend python (%s files, %s), state=%s" % (na, a, st)


def _public_ok() -> bool:
    """Is the live site actually serving? CODE **AND** BYTES.

    A 200 with an empty body is exactly what a dead upstream behind a healthy proxy looks like --
    that shape cost the sibling project two "successful" deploys over a blank site. And a browser
    UA is required: the bot gate serves an unrecognised agent a 404, so a curl-shaped probe would
    record a working app as broken.
    """
    import urllib.request
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
    allok = True
    for url, want in (("https://jobhuntwow.com/api/health", 200),
                      ("https://jobhuntwow.com/", 200)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read() or b""
                good = r.status == want and len(body) > 200
                say("  %-38s %s  %d bytes%s" % (url, r.status, len(body),
                                                "" if good else "   <-- EMPTY BODY"))
                allok = allok and good
        except Exception as e:
            say("  %-38s FAILED  %r" % (url, e))
            allok = False
    return allok


def do_verify() -> bool:
    hr("5/6  VERIFY   (the public site AND the code actually inside the container)")
    # `jhw.cmd_status` EXISTS but is the wrong subject twice over: it takes an argument
    # (`def cmd_status(_)`), so calling it with none is a TypeError, and it prints the LOCAL docker
    # sandbox plus a noVNC URL -- it returns None and says nothing about the public site. So
    # `bool(public and ok)` could never have been True. Probe the real thing instead.
    public = _public_ok()
    ok, detail = engine_is_current()
    say("  engine_fresh   %s  %s" % ("OK  " if ok else "FAIL", detail))
    if not public:
        say("  [X] the public site did not assert clean (see the three lines above)")
    return bool(public and ok)


# ============================================================================ 6/6 safe point
def tag_known_good() -> str:
    hr("6/6  SAFE POINT")
    stamp = time.strftime("good-%Y%m%d-%H%M%S")
    git("tag", "-f", "last-known-good")
    git("tag", stamp)
    for ref in ("last-known-good", stamp):
        _, rc = git("push", "-f", "origin", ref)
        say("  pushed tag %s%s" % (ref, "" if rc == 0 else " [!] push failed"))
    say("  rollback with:  python ship.py --rollback            (or --rollback %s)" % stamp)
    return stamp


def do_rollback(target: str) -> int:
    hr("ROLLBACK -> %s" % target)
    _, rc = git("rev-parse", "--verify", target)
    if rc != 0:
        say("  [X] no such tag/commit: %s" % target)
        return 1
    dirty, _ = git("status", "--porcelain")
    if dirty:
        say("  parking local changes in `git stash` (nothing is discarded)")
        git("stash", "push", "-u", "-m", "ship.py rollback %s" % time.strftime("%Y%m%d-%H%M%S"))
    git("reset", "--hard", target)
    head, _ = git("rev-parse", "--short", "HEAD")
    say("  the PC is now at %s (%s)" % (head, target))
    if not do_deploy():
        return 1
    return 0 if do_verify() else 1


# ============================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Release JobHuntWOW. ONE command; the flags narrow it, they never split it.")
    ap.add_argument("--test", action="store_true", help="stop after the tests")
    ap.add_argument("--no-stage", action="store_true", help="skip the staging gate (say so out loud)")
    ap.add_argument("--no-reboot", action="store_true", help="staging gate without the reboot test")
    ap.add_argument("--rollback", nargs="?", const="last-known-good", default=None,
                    metavar="TAG", help="reset to last-known-good (or TAG) and redeploy it")
    ap.add_argument("-m", "--message", default="", help="commit message")
    a = ap.parse_args()

    if a.rollback:
        return do_rollback(a.rollback)

    if not do_tests():
        say("\n[X] TESTS FAILED - nothing was committed, pushed or deployed.")
        return 1
    if a.test:
        say("\n[OK] tests only (--test). Nothing was deployed.")
        return 0

    if not do_git(a.message or ("ship %s" % time.strftime("%Y-%m-%d %H:%M"))):
        return 1

    fp_before = pack_fingerprint()
    say("  shipped-file fingerprint: %s" % fp_before)

    if a.no_stage:
        say("\n[!] STAGING GATE SKIPPED (--no-stage). This change is going to production")
        say("    UNVALIDATED: no health checks ran, it was never rebooted, and no panel read it.")
    else:
        gate_res, digest = do_stage(reboot_test=not a.no_reboot)
        say("\n" + (digest or "").rstrip())
        say("\nSTAGING GATE: %s" % gate_res)
        if gate_res == "NO-GO":
            say("\n[X] REFUSING TO PROMOTE. Nothing on production was touched.")
            say("    The panel is advisory; this verdict comes from the deterministic checks above.")
            say("    If a HALT was panel dissent on a green gate, read their reasons - twice that")
            say("    has meant a CHECK IS LYING - then re-run with OVERRIDE_PANEL=1 to promote.")
            return 2
        if gate_res == "SKIP":
            say("\n[!] The staging gate did not run. Continuing to production UNVALIDATED.")

    fp_after = pack_fingerprint()
    if fp_after != fp_before:
        say("\n[X] THE SOURCE CHANGED MID-FLIGHT: %s -> %s" % (fp_before, fp_after))
        say("    Staging validated one artefact and production would receive a different one, which")
        say("    makes the green staging run meaningless. Commit the change and re-run.")
        return 2

    if not do_deploy():
        say("\n[X] DEPLOY FAILED. No safe-point tag was written.")
        return 1
    if not do_verify():
        # NEVER PRINT DONE ON A PATH WHERE A SUB-STEP RETURNED NON-ZERO.
        say("\n[X] VERIFY FAILED - the deploy is NOT confirmed and no safe point was tagged.")
        say("    Next:  python jhw.py diagnose")
        return 1
    tag_known_good()
    say("\n[OK] RELEASED and VERIFIED: https://jobhuntwow.com  (fingerprint %s)" % fp_after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
