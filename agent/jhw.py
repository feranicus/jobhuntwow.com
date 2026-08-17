#!/usr/bin/env python3
"""jhw.py — the ONE ops entry point for JobHuntWOW (no shell blobs; see CLAUDE.md).

    python jhw.py run 4435446898        # A->Z: backend+sandbox+login+scrape+tailor+apply (ONE command)
    python jhw.py login                 # ensure LinkedIn login (auto, else one-time manual in noVNC)
    python jhw.py backend       # (re)start the backend + /v1 proxy (repo-root compose)
    python jhw.py up            # build + start the secure-browser sandbox (Chrome + noVNC)
    python jhw.py watch         # print the noVNC URL to watch/drive the browser
    python jhw.py scrape 4435446898     # read a LinkedIn job -> out/job.json
    python jhw.py tailor                # scraped JD + Profile.pdf -> tailored resume + cover letter
    python jhw.py apply  4435446898     # drive Apply -> ATS, fill through Review (human submits)
    python jhw.py status        # container + health + noVNC URL
    python jhw.py logs          # follow sandbox logs
    python jhw.py down          # stop the sandbox

Wraps Docker so operations are re-runnable and documented. The sandbox compose lives in agent/;
the backend compose lives in the repo root (agent/..). Run from anywhere.
"""
from __future__ import annotations
import argparse, glob, io, os, re, shutil, subprocess, sys

try:
    _SELF = os.path.abspath(__file__)
except OSError:
    # WSL+Docker can replace the CWD inode, killing getcwd(); recover via $PWD (same path, new inode)
    os.chdir(os.environ.get("PWD", "/"))
    _SELF = os.path.abspath(__file__)
HERE = os.path.dirname(_SELF)                              # .../agent
ROOT = os.path.abspath(os.path.join(HERE, ".."))            # .../jobhuntwow-app (backend compose)
NOVNC = "http://localhost:9090/vnc.html"
COOKIES = os.path.join(HERE, "linkedin_cookies.json")
COOKIES_SRC = os.path.abspath(os.path.join(HERE, "..", "..", "linkedin_cookies.json"))


def sh(*args, cwd=HERE, check=True):
    print("···", " ".join(args), f"(in {os.path.basename(cwd)})")
    return subprocess.run(args, cwd=cwd, check=check)


def dc(*args, cwd=HERE, check=True):
    return sh("docker", "compose", *args, cwd=cwd, check=check)


def ensure_prereqs():
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    if not os.path.exists(COOKIES):
        if os.path.exists(COOKIES_SRC):
            shutil.copy(COOKIES_SRC, COOKIES); print(f"[OK] copied LinkedIn cookies from {COOKIES_SRC}")
        else:
            print(f"[WARN] {COOKIES} missing and none at {COOKIES_SRC}. Export your LinkedIn cookies (JSON) there.")
    if not os.path.exists(os.path.join(HERE, ".env")):
        print("[WARN] agent/.env missing — set JHW_PROXY_BASE + AGENT_PROXY_TOKEN (see .env.example).")


def cmd_backend(_):
    # repo-root compose holds the backend + /v1 proxy; --force-recreate reloads .env (model routing).
    dc("up", "-d", "--build", "--force-recreate", "backend", cwd=ROOT)
    print("[OK] backend restarted (reloaded model routing from repo-root .env).")


def cmd_up(_):
    ensure_prereqs(); dc("up", "-d", "--build")
    print(f"\n[OK] Sandbox starting. Watch the browser at: {NOVNC}\n     ~30s, then: python jhw.py status")


def cmd_down(_): dc("down")
def cmd_watch(_): print(f"Open the browser view here: {NOVNC}")
def cmd_status(_): dc("ps", check=False); print(f"\nnoVNC (watch/drive): {NOVNC}")
def cmd_logs(_): dc("logs", "-f", check=False)


def _exec_agent(goal, job, out=None):
    ensure_prereqs()
    # Forward creds from the HOST .env into the exec so the container always has the current values
    # (its own env can be stale if .env changed after start). Secrets are passed by NAME ONLY
    # (`-e KEY`, no value) after exporting them into this process env — so the VALUE never appears in
    # argv or the echoed command line. Add new secrets to this list; they stay out of the logs.
    env = []
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "JHW_ASK_CHANNEL", "JHW_WORKDAY_PASSWORD", "JHW_WORKDAY_EMAIL"):
        v = _envval(k)
        if v:
            os.environ[k] = v          # docker inherits it; `-e KEY` forwards by name, no value leak
            env += ["-e", k]
    args = (["-f", "docker-compose.local.yml", "exec", "-T"] + env
            + ["jhw-agent", "python3", "/agent/agent.py", goal, job])
    if out: args += ["--out", out]
    print(f"[i] {goal} job {job} — watch live at {NOVNC}")
    dc(*args, check=False)


def cmd_scrape(a):
    _exec_agent("scrape", a.job, "/agent/out/job.json")
    print(f"[OK] result (if any) -> {os.path.join(HERE, 'out', 'job.json')}")


# (script args, the marker that proves it PASSED, what it protects)
# ONE list so adding a guard is one line here and never a second command for the human.
_SELFTESTS = [
    (["/agent/flows/test_llm_driver_dom.py"], "ALL DOM CONTRACTS HOLD",
     "generic driver: error->field attribution, typeahead pick, date, submit"),
    (["/agent/flows/workday_wd.py", "--logic"], "ALL WORKDAY LOGIC CONTRACTS HOLD",
     "Workday: URL derivation, routing precedence, answers from candidate.md"),
    (["/agent/flows/learned.py", "--logic"], "ALL LEARNED-ANSWER CONTRACTS HOLD",
     "learned answers: recalled across employers, secrets refused"),
    (["/agent/flows/memory.py", "--logic"], "ALL MEMORY CONTRACTS HOLD",
     "the one store: nothing is learned until the SITE confirms the submission"),
    (["/agent/flows/ashby.py", "--logic"], "ALL ASHBY CONTRACTS HOLD",
     "Ashby from his recording: plain-digit salary, 'one month', required ticks block Submit"),
    (["/agent/flows/workday_questions.py", "--logic"], "ALL WORKDAY QUESTION CONTRACTS HOLD",
     "the questions page: the date in the format the BOX asks for, his salary, never 'ASK'"),
    (["/agent/flows/presubmit.py", "--logic"], "ALL PRE-SUBMIT CONTRACTS HOLD",
     "pre-submit audit: hard checks block, the panel only advises"),
    (["/agent/flows/test_workday_dom.py"], "ALL WORKDAY DOM CONTRACTS HOLD",
     "Workday: portal dropdowns, cascade+chips, files by group, consent"),
    (["/agent/flows/escalate.py", "--logic"], "ALL ESCALATION CONTRACTS HOLD",
     "3-LLM stall panel: quorum of 2, invented targets and credentials refused"),
    (["/agent/flows/test_escalate_dom.py"], "ALL ESCALATION DOM CONTRACTS HOLD",
     "a MODAL owns the page: its controls indexed, the page behind it excluded"),
    (["/agent/flows/test_selectors.py"], "ALL SELECTOR CONTRACTS HOLD",
     "repo-wide: no name regex in flows/ can resolve to an adjacent control"),
    (["/agent/flows/workday.py", "--logic"], "ALL WORKDAY SELECTOR CONTRACTS HOLD",
     "selectors: no click regex can reach a third-party SSO button, actions are capped"),
    (["/agent/flows/speaker.py", "--logic"], "ALL SPEAKER CONTRACTS HOLD",
     "4th model (kimi-k2.6) talks to you; it cannot widen what the engine may do"),
    (["/agent/flows/converse.py", "--logic"], "ALL CONVERSATION CONTRACTS HOLD",
     "an instruction is an INTENT with an action, never typed into a password box"),
    (["/agent/flows/seers.py", "--logic"], "ALL SEER CONTRACTS HOLD",
     "5 more senses: a11y tree, hit grid, shadow walk, tab order, self-healing click"),
    (["/agent/flows/vision.py", "--logic"], "ALL VISION CONTRACTS HOLD",
     "vision reader: labels never coordinates, a hallucinated control cannot resolve"),
    (["/agent/flows/mailcode.py", "--logic"], "ALL MAIL-CODE CONTRACTS HOLD",
     "verification codes: read from the mailbox, a bare number is never a code"),
    (["/agent/flows/som.py", "--logic"], "ALL SET-OF-MARK CONTRACTS HOLD",
     "Set-of-Mark: the eye answers with a NUMBER, so a hallucinated control cannot exist"),
    (["/agent/flows/test_models.py"], "ALL MODEL-ROUTING CONTRACTS HOLD",
     "no phantom model id: every role is checked against the live catalog, fails open"),
    (["/agent/flows/recordings.py", "--logic"], "ALL RECORDING-KNOWLEDGE CONTRACTS HOLD",
     "human recordings -> facts: a credential never reaches the knowledge file"),
    (["/agent/flows/conduct.py", "--logic"], "ALL CONDUCT CONTRACTS HOLD",
     "politeness: per-employer rate limits, and a bot challenge is HANDED OVER, never solved"),
    (["/agent/flows/test_docker.py"], "ALL DOCKER CONTRACTS HOLD",
     "containers: no port on 0.0.0.0, least privilege, resource limits, no baked secret"),
    (["/agent/flows/essay.py", "--logic"], "ALL ESSAY CONTRACTS HOLD",
     "free text: answered from HIS CV + cover letter, and an invented employer is refused"),
    (["/agent/flows/choose.py", "--logic"], "ALL CHOICE CONTRACTS HOLD",
     "a closed list: only a REAL option can be chosen, and a self-declaration is never generated"),
    (["/agent/flows/greenhouse.py", "--logic"], "ALL GREENHOUSE CONTRACTS HOLD",
     "Greenhouse from the recording: national phone, city-only, no invented field name"),
    (["/app/jhw_bot.py", "--logic"], "ALL BOT CONTRACTS HOLD",
     "Telegram: an answer in pieces reaches the engine; a credential never reaches a model",
     "jhw-bot"),
]


def _compile_knowledge():
    """Recordings -> flows/knowledge/*.json, EVERY run, before anything reads them.

    WHY THIS IS THE VERB'S JOB AND NOT THE OPERATOR'S. On 2026-08-17 I compiled this by hand three
    times with `python flows/recordings.py --compile ../recordings`. That is a SECOND COMMAND the
    operator has to remember, which is the rule this project breaks most often -- and the failure mode
    is SILENT: a freshly recorded ATS has no effect at all until somebody remembers, so the run uses
    stale facts and looks like a code bug. He records; the engine compiles. That is the whole point.
    Pure parsing: stdlib only, no network, no container, ~50ms. It writes to the HOST directory, which
    is bind-mounted read-only into jhw-agent, so the container sees it live with no rebuild.
    NON-BLOCKING by design -- a malformed recording must never stop an application."""
    rec = os.path.join(HERE, "recordings")
    kbdir = os.path.join(HERE, "flows", "knowledge")
    if not os.path.isdir(rec):
        return
    try:
        sys.path.insert(0, os.path.join(HERE, "flows"))
        import recordings as KB
        before = {}
        for f in glob.glob(os.path.join(kbdir, "*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    before[os.path.basename(f)] = fh.read()
            except Exception:
                pass
        out = KB.compile_dir(rec, kbdir, log=lambda *a: None)
        changed = []
        for ats in sorted(out):
            try:
                with open(os.path.join(kbdir, ats + ".json"), encoding="utf-8") as fh:
                    now = fh.read()
            except Exception:
                now = ""
            if now != before.get(ats + ".json"):
                changed.append(ats)
        nf = sum(len(k.get("fields") or {}) for k in out.values())
        nc = sum(len(k.get("choices") or {}) for k in out.values())
        print(f"[OK] knowledge compiled from your recordings: {len(out)} ATS, {nf} field name(s), "
              f"{nc} recorded choice(s)"
              + (f"  — UPDATED: {', '.join(changed)}" if changed else ""))
    except Exception as e:
        print(f"[warn] could not compile the recordings ({type(e).__name__}: {str(e)[:70]}) — "
              f"the knowledge files already on disk are still used")


def _dom_selftest():
    """~5s regression guard, inside the ONE command. The DOM contracts these drivers depend on
    (which field owns a validation error; a suggestion list that is not the page navigation; a
    Workday dropdown whose value silently reverts) broke silently four times and each break cost a
    full application run. Never a separate command the human has to remember - it runs here or it
    does not exist."""
    for suite in _SELFTESTS:
        # A suite may name the CONTAINER it belongs to. jhw_bot.py is only present in the jhw-bot
        # image (its Dockerfile COPYs that one file), so running it in jhw-agent would report
        # "could not run" forever -- a check that cannot execute is not a check.
        args, marker, what = suite[0], suite[1], suite[2]
        svc = suite[3] if len(suite) > 3 else "jhw-agent"
        name = os.path.basename(args[0]) + ("".join(" " + a for a in args[1:]))
        try:
            p = subprocess.run(
                ["docker", "compose", "-f", "docker-compose.local.yml", "exec", "-T", svc,
                 "python3"] + args,
                cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120)
            out = (p.stdout or "") + (p.stderr or "")
            # BOTH conditions: a marker without exit 0 means checks failed after it printed, and
            # exit 0 without the marker means the script died before it ever asserted anything.
            if p.returncode == 0 and marker in out:
                print(f"[OK] {what}")
            else:
                # SHOW THE FAILURES, NOT THE TAIL. A 14-line tail was eaten whole by
                # playwright's "please run playwright install" ASCII box plus an asyncio cleanup
                # traceback, so a real assertion failure was invisible and I could not diagnose my
                # own regression. A diagnostic that does not name its subject sends the next
                # investigation down the wrong road.
                print(f"[warn] {name} did not pass — {what}:")
                lines = out.strip().splitlines()
                hits = [ln for ln in lines
                        if ln.lstrip().startswith(("FAIL", "[X]", "FAILED:"))
                        or "Traceback" in ln or "Error:" in ln]
                for line in (hits[:12] or lines[-14:]):
                    print("      " + line.rstrip())
                if hits:
                    print(f"      ({len(lines)} lines total — run it directly for the rest: "
                          f"docker compose -f docker-compose.local.yml exec {svc} python3 {args[0]})")
        except Exception as e:
            print(f"[warn] {name} could not run: {str(e)[:90]}")


def _safepoint(when: str, note: str = ""):
    """Commit the tree so a corrupted file is one `git checkout` away, not an hour of rebuilding.

    THE OPERATOR ASKED FOR THIS, and the record earns it: the editor has truncated or null-padded
    agent.py, workday.py, jhw.py (twice), ask.py, both compose files and proxy.py, and on 2026-08-17 a
    stale-offset AST edit of mine destroyed learned.py -- which was UNTRACKED, so there was nothing to
    restore from and it had to be rebuilt by hand.

    NON-BLOCKING, ALWAYS. A bookkeeping step that can abort an application is a worse defect than the
    one it prevents, so every failure is printed and swallowed. And it REFUSES to commit the
    candidate's PII, a CV or anything credential-shaped -- see gitops.safepoint()."""
    try:
        sys.path.insert(0, HERE)
        import gitops as G
        r = G.safepoint(f"safepoint ({when}) {note}".strip())
        print(f"[git] {r['note']}")
        if r.get("refused"):
            print("[git] run `python jhw.py git --fix` once and this stops happening")
    except Exception as e:
        print(f"[git] safepoint skipped ({type(e).__name__}: {str(e)[:70]})")


def cmd_apply(a):
    if not _ensure_stack():               # ONE command: backend + sandbox + BRAIN, then apply
        return 2                          # brain unreachable -> do NOT burn a run (and the documents)
    _compile_knowledge()                  # his recordings ARE the first rung of the answer ladder
    _dom_selftest()
    # BEFORE the run: whatever we are about to execute is what gets saved, so a crash mid-run leaves
    # a commit describing the code that produced it, not the code as it was afterwards.
    _safepoint("before apply", a.job)
    rc = _exec_agent("apply", a.job)
    # AFTER the run: the learning database, the compiled knowledge and any adapter change are all on
    # disk now. `out/` is gitignored, so what lands here is CODE and KNOWLEDGE, never his data.
    _safepoint("after apply", a.job)
    return rc


def cmd_tailor(_):
    # reads out/job.json + Profile.pdf -> resume.pdf, cover_letter.pdf, fields.json (in out/)
    ensure_prereqs()
    print(f"[i] tailor: extract + content LLMs -> out/  (needs a prior `scrape`)")
    dc("exec", "-T", "jhw-agent", "python3", "/agent/tailor.py", check=False)
    print(f"[OK] resume/cover-letter (if any) -> {os.path.join(HERE, 'out')}")




def _envval(key):
    f = os.path.join(HERE, ".env")
    if not os.path.exists(f): return ""
    for line in open(f, encoding="utf-8"):
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def cmd_telegram(_):
    """Discover your Telegram chat id (message the bot first), and send a test message."""
    import json as _j, urllib.request as _u
    tok = _envval("TELEGRAM_BOT_TOKEN")
    if not tok:
        print("Set TELEGRAM_BOT_TOKEN in agent/.env first.\n"
              "1) In Telegram, message @BotFather -> /newbot -> copy the token.\n"
              "2) Put it in agent/.env as TELEGRAM_BOT_TOKEN=...\n"
              "3) Send any message to your new bot, then run `python jhw.py telegram` again.")
        return
    base = f"https://api.telegram.org/bot{tok}"
    try:
        data = _j.loads(_u.urlopen(f"{base}/getUpdates", timeout=20).read())
    except Exception as e:
        print(f"[ERR] Telegram API: {e}"); return
    chats = {}
    for upd in data.get("result", []):
        m = upd.get("message", {}); ch = m.get("chat", {})
        if ch.get("id"): chats[ch["id"]] = ch.get("first_name") or ch.get("title") or ""
    if not chats:
        print("No messages yet. Send any message to your bot in Telegram, then re-run `python jhw.py telegram`.")
        return
    print("Found chats (put the id in agent/.env as TELEGRAM_CHAT_ID):")
    for cid, nm in chats.items(): print(f"  {cid}   {nm}")
    cur = _envval("TELEGRAM_CHAT_ID")
    if cur:
        try:
            _u.urlopen(f"{base}/sendMessage?chat_id={cur}&text=%E2%9C%85%20JobHuntWOW%20is%20connected.", timeout=20)
            print(f"[OK] sent a test message to chat {cur}")
        except Exception as e:
            print(f"[warn] test send failed: {e}")


def cmd_models(_):
    """List the model ids your DO key can actually use (via the backend). Run backend first."""
    import json as _j, urllib.request as _u
    try:
        data = _j.loads(_u.urlopen("http://127.0.0.1:8000/api/models", timeout=30).read())
    except Exception as e:
        print(f"[ERR] backend not reachable on :8000 ({e}). Run `python jhw.py backend` first."); return
    models = data.get("models") or []
    if not models:
        print("[WARN] backend returned no models:", data); return
    print(f"Models available to your DO key ({len(models)}):")
    for m in sorted(models): print("  ", m)
    print("\nTell Claude which to use for driver(vision)/content/extract, or paste this list.")



def _accounts_path():
    p = os.path.join(HERE, "out", "ats_accounts.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def cmd_git(a):
    """SECOND SOURCE OF TRUTH — and the guard that keeps the candidate out of a public repo.

    Read-only by default (it prints what is exposed and what to do). `--fix` untracks the private
    files and writes the ignore rules; `--repo` creates the PRIVATE repo the automation belongs in."""
    sys.path.insert(0, HERE)
    import gitops as G
    if getattr(a, "fix", False):
        print("\n".join(G.harden(apply=True)))
        print()
        r = G.safepoint("gitops: stop tracking private files (PII, CVs, credentials, databases)")
        print("  " + r["note"])
        print()
    if getattr(a, "repo", False):
        print("\n".join(G.create_private_repo(apply=True)))
        print()
    # PUSH. He asked for git to be a real second source of truth, so this verb SAVES AND PUSHES —
    # not merely reports. Credentials are still refused; that guard is the whole point.
    r = G.safepoint("manual safepoint", push=True)
    print("  " + r["note"])
    print()
    print(G.audit())
    return 0


def cmd_memory(a):
    """WHAT THE AGENT HAS LEARNED — one verb, read-only.

    The database lives in the agent container's `out/` volume, which is bind-mounted from `agent/out`,
    so the same file is readable from either side. Tries the container first (that is where a run
    wrote it) and falls back to this machine when the stack is down: a verb that only works while a
    container happens to be running is not a verb."""
    extra = ["--forget", a.forget] if getattr(a, "forget", "") else []
    try:
        p = subprocess.run(["docker", "compose", "-f", "docker-compose.local.yml", "exec", "-T",
                            "jhw-agent", "python3", "/agent/flows/memory.py", "--report"] + extra,
                           cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0 and (p.stdout or "").strip():
            print(p.stdout)
            return 0
    except FileNotFoundError:
        # no docker on this machine -> read the same file directly. A verb that traceback's because a
        # tool it PREFERS is absent is the "a check that cannot run where it is invoked" defect.
        pass
    p = subprocess.run([sys.executable, os.path.join(HERE, "flows", "memory.py"), "--report"] + extra,
                       cwd=os.path.join(HERE, "flows"), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print((p.stdout or "") + (p.stderr or ""))
    return p.returncode


def cmd_atspw(a):
    """Store STABLE ATS account credentials for a host. The login email can differ from the résumé
       contact email (e.g. a Workday account under your gmail):
         python jhw.py atspw redhat.wd5.myworkdayjobs.com 'cLR|6eKZ-9c\\E4yU,afX' feranicus@gmail.com"""
    import json as _j
    email = getattr(a, "email", "") or ""
    if not email:
        try:
            email = _j.load(open(os.path.join(HERE, "templates", "resume_data.json"), encoding="utf-8")).get("basics", {}).get("email", "")
        except Exception:
            pass
    path = _accounts_path()
    try:
        acc = _j.load(open(path, encoding="utf-8"))
    except Exception:
        acc = {}
    acc[a.host] = {"email": email, "password": a.password}
    _j.dump(acc, open(path, "w"), indent=2)
    print(f"[OK] stored ATS login for {a.host} (user={email}). `python jhw.py apply <job>` will sign in.")


def cmd_import_passwords(a):
    """Bulk-load ATS logins from a Chrome/Google Password Manager CSV export (chrome://password-manager
       -> Settings -> Export passwords). One export populates every company's login at once."""
    import csv, json as _j, re as _re
    path = _accounts_path()
    try:
        acc = _j.load(open(path, encoding="utf-8"))
    except Exception:
        acc = {}
    n = 0
    with open(a.csv, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            row = { (k or "").lower(): v for k, v in row.items() }
            url, user, pwd = row.get("url", ""), row.get("username", ""), row.get("password", "")
            if not url or not pwd:
                continue
            host = _re.sub(r"^https?://", "", url).split("/")[0].lower()
            acc[host] = {"email": user, "password": pwd}
            n += 1
    _j.dump(acc, open(path, "w"), indent=2)
    print(f"[OK] imported {n} logins into out/ats_accounts.json — apply will now sign in to any of them.")


def cmd_inspect(_):
    """Dump the REAL DOM (fields, buttons, data-automation-ids) of the ATS tab currently open in the
       sandbox — so selectors are written from ground truth, not guesses. Open the ATS page first."""
    _ensure_stack()
    print("[i] inspecting the front ATS tab in the sandbox (open the ATS page in noVNC first) …")
    dc("exec", "-T", "jhw-agent", "python3", "/agent/flows/probe.py", check=False)
    print(f"[OK] full dump -> {os.path.join(HERE, 'out', 'dom_dump.json')}")


def _rec_python() -> str:
    """WHICH python inside jhw-browser owns playwright. The Dockerfile creates /opt/venv AND
       pip-installs requirements, and which interpreter ended up with playwright depends on PATH
       at build time. DETECT it; do not guess -- an invented interpreter path is how this repo has
       burned a dozen cycles."""
    for py in ("/opt/venv/bin/python", "python3", "/usr/bin/python3"):
        r = subprocess.run(["docker", "compose", "-f", "docker-compose.local.yml", "exec", "-T",
                            "jhw-browser", py, "-c", "import playwright,sys;print(sys.executable)"],
                           cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            print(f"[i] playwright lives at {r.stdout.strip()} inside jhw-browser")
            return py
    print("[X] no interpreter inside jhw-browser can import playwright.")
    return ""


def cmd_record(a):
    """Record the COMPLETE apply lifecycle with Playwright codegen, INSIDE the sandbox, reusing the
       browser profile that already holds your Google/SSO session. Watch and drive it in noVNC.

    WHY INSIDE jhw-browser and not on the host: that container is the only one with DISPLAY=:0,
    Xvfb, noVNC and the jhw_browser_profile volume. jhw-agent shares its NETWORK namespace but has
    neither a display nor the profile, so codegen cannot run there.

    WHY A COPY OF THE PROFILE: Chrome refuses to open a user-data-dir that another process already
    holds, and jhw-browser's own Chrome is live on /home/chrome-user/chrome-data. Recording against
    a copy means the sandbox keeps running AND a codegen crash can never corrupt the real profile
    (which is where your Google session lives). The copy carries the cookies, so you stay signed in.
    """
    _ensure_stack()
    url = a.url
    m = re.search(r"https?://([^./]+)", url or "")
    name = a.name or (re.sub(r"[^a-z0-9]+", "_", m.group(1).lower()).strip("_") if m else "recording")
    py = _rec_python()
    if not py:
        return

    # VERIFY THE FLAGS EXIST IN THIS PLAYWRIGHT BUILD instead of asserting them from the docs.
    h = subprocess.run(["docker", "compose", "-f", "docker-compose.local.yml", "exec", "-T",
                        "jhw-browser", py, "-m", "playwright", "codegen", "--help"],
                       cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace")
    help_txt = (h.stdout or "") + (h.stderr or "")
    need = ["--save-storage", "--user-data-dir", "--target"]
    missing = [f for f in need if f not in help_txt]
    out_flag = "--output" if "--output" in help_txt else ("-o" if "-o," in help_txt or " -o " in help_txt else "")
    if missing or not out_flag:
        print(f"[X] this playwright codegen does not expose {missing or ['an output flag']}.")
        print("--- real `codegen --help` from inside the container ---")
        print(help_txt.strip()[:1800])
        return
    target = "python-async" if "python-async" in help_txt else "python"
    print(f"[OK] codegen flags verified; target={target}, output flag={out_flag}")

    prof, rec, auth = "/tmp/rec-profile", f"/tmp/{name}.py", f"/tmp/{name}_auth.json"
    # Fresh copy each run so a half-finished previous recording cannot leak in.
    dc("-f", "docker-compose.local.yml", "exec", "-T", "-u", "root", "jhw-browser", "bash", "-lc",
       f"rm -rf {prof} && cp -a /home/chrome-user/chrome-data {prof} "
       f"&& rm -f {prof}/SingletonLock {prof}/SingletonCookie {prof}/SingletonSocket "
       f"&& chown -R chrome-user:chrome-user {prof}", check=False)

    print()
    print("=" * 78)
    print(f"  RECORDING -> agent/recordings/{name}.py")
    print(f"  1. open {NOVNC}")
    print("  2. drive the WHOLE lifecycle: choose the apply path, sign in with Google if it asks,")
    print("     fill every step, and stop at Review WITHOUT submitting.")
    print("  3. close the browser window when done -- that is what ends the recording and")
    print("     writes the storage state.")
    print()
    print("  SECRET: codegen writes EVERY VALUE YOU TYPE into the .py, passwords included, and the")
    print("  storage state holds live session cookies. agent/recordings/ is gitignored wholesale")
    print("  for exactly this reason. The committable artefact is the ADAPTER it produces, never")
    print("  the recording.")
    print("=" * 78)
    print()
    dc("-f", "docker-compose.local.yml", "exec", "-u", "chrome-user",
       "-e", "DISPLAY=:0", "jhw-browser", py, "-m", "playwright", "codegen",
       f"--user-data-dir={prof}", f"--save-storage={auth}", "--target", target,
       out_flag, rec, url, check=False)

    # recordings/ is mounted into NO container, so the artefacts must be copied out.
    os.makedirs(os.path.join(HERE, "recordings"), exist_ok=True)
    ok = True
    for src, dst in ((rec, f"recordings/{name}.py"), (auth, f"recordings/{name}_auth.json")):
        r = sh("docker", "cp", f"jhw-browser:{src}", os.path.join(HERE, dst), check=False)
        if r.returncode != 0:
            print(f"[WARN] nothing at {src} -- the recording may have been closed before it started")
            ok = False
    apath = os.path.join(HERE, f"recordings/{name}_auth.json")
    if os.path.exists(apath):
        try:
            os.chmod(apath, 0o600)
        except OSError:
            pass
        print(f"[OK] storage state -> recordings/{name}_auth.json (chmod 600, gitignored, holds "
              "live session cookies -- never commit it)")

    # A FILE EXISTING IS NOT A FILE BEING RIGHT. Prove the recording actually captured a session.
    rpath = os.path.join(HERE, f"recordings/{name}.py")
    if os.path.exists(rpath):
        body = io.open(rpath, encoding="utf-8", errors="replace").read()
        acts = len(re.findall(r"\.(click|fill|select_option|set_input_files|press|check)\(", body))
        print(f"[i] recording: {len(body.splitlines())} lines, {acts} recorded actions")
        if acts < 5:
            print("[WARN] fewer than 5 actions captured -- that is not a lifecycle. Re-run and")
            print("       drive the form to Review before closing the window.")
            ok = False
        r = subprocess.run([sys.executable, "-m", "py_compile", rpath], capture_output=True)
        print("[OK] recording compiles" if r.returncode == 0 else "[X] recording does NOT compile")
    if ok:
        print()
        # `adapt` was never built as a verb, so this told the operator to run something that does not
        # exist. What actually happens next is automatic: `apply` compiles every recording into
        # flows/knowledge/*.json before it runs, so the facts from this recording are already in force.
        print(f"[NEXT] nothing — `python jhw.py apply <url>` already compiled {name} into "
              f"flows/knowledge/. Record another tenant to widen it.")
    # HE RECORDS, THE FACTS APPEAR. Compiling is not a separate step the human performs.
    _compile_knowledge()



def _doctor_brain() -> bool:
    """Can the agent REACH THE LLM? This is the check that did not exist, and its absence turned a
       one-line network fault into a guessing game.

    MEASURED 2026-08-16: an NTT run navigated, uploaded resume.pdf AND cover_letter.pdf, correctly
    detected `State/Province/Region -> should be array`, and then died with
        [llm_driver] EXIT step 1: LLM call failed: All connection attempts failed
    while Stagehand independently reported `Cannot connect`. Both talk to the SAME address
    (JHW_PROXY_BASE, default http://host.docker.internal:8000/v1), so the browser half was fine and
    only the brain was unreachable. `doctor` checked Chrome and said nothing about this.

    It probes from INSIDE jhw-agent, because that is the only vantage point that matters -- the host
    being able to curl :8000 proves nothing about the container."""
    print("[i] === can jhw-agent reach the LLM proxy? ===")
    probe = (
        "import os,socket,urllib.request,urllib.error,sys\n"
        "base=os.getenv('JHW_PROXY_BASE','http://backend:8000/v1')\n"
        "print('  JHW_PROXY_BASE =',base)\n"
        "from urllib.parse import urlparse\n"
        "u=urlparse(base); host=u.hostname; port=u.port or 80\n"
        "try:\n"
        "    ips=sorted({ai[4][0] for ai in socket.getaddrinfo(host,port)})\n"
        "    print(f'  DNS  {host} -> {ips}')\n"
        "except Exception as e:\n"
        "    print(f'  DNS  {host} -> FAILED: {type(e).__name__}: {e}'); sys.exit(1)\n"
        "s=socket.socket(); s.settimeout(4)\n"
        "try:\n"
        "    s.connect((host,port)); print(f'  TCP  {host}:{port} -> OPEN')\n"
        "except Exception as e:\n"
        "    print(f'  TCP  {host}:{port} -> REFUSED/TIMEOUT: {type(e).__name__}: {e}')\n"
        "    print('  => the backend is not reachable from the container. It publishes')\n"
        "    print(\"     127.0.0.1:8000 on the HOST; check `docker compose ps backend`.\")\n"
        "    sys.exit(1)\n"
        "finally:\n"
        "    s.close()\n"
        "root=base[:-3] if base.endswith('/v1') else base\n"
        "for path in ('/api/health','/v1/models'):\n"
        "    try:\n"
        "        r=urllib.request.urlopen(root.rstrip('/')+path,timeout=6)\n"
        "        print(f'  HTTP {path} -> {r.status} {r.read(90)!r}')\n"
        "    except urllib.error.HTTPError as e:\n"
        "        print(f'  HTTP {path} -> {e.code} {e.read(120)!r}')\n"
        "    except Exception as e:\n"
        "        print(f'  HTTP {path} -> FAILED: {type(e).__name__}: {e}')\n"
    )
    r = subprocess.run(["docker", "compose", "-f", "docker-compose.local.yml", "exec", "-T",
                        "jhw-agent", "python3", "-c", probe],
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").rstrip()
    print(out or "  [!] could not run the probe inside jhw-agent")
    if r.stderr.strip():
        print("  stderr:", r.stderr.strip()[:300])
    if r.returncode != 0:
        # ConnectionRefusedError means NOTHING IS LISTENING -- so the question is why the backend
        # process is down, not whether the port mapping is right. `127.0.0.1:8000:8000` is the
        # committed binding and it worked on 21 Jul, so do not go looking at the mapping: read the
        # container's own state and logs. A container can report Running while uvicorn died on
        # import, and a bind-mounted EVENTS_LOG dir that the app cannot write is a known cause.
        print("\n[i] === why is the backend down? (state + its own logs) ===")
        sh("docker", "compose", "ps", "backend", cwd=ROOT, check=False)
        sh("docker", "compose", "logs", "--tail", "40", "backend", cwd=ROOT, check=False)
        print("\n[i] === can the HOST reach it? (proves listener vs mapping) ===")
        hp = subprocess.run([sys.executable, "-c",
                             "import urllib.request as u;"
                             "print(u.urlopen('http://127.0.0.1:8000/api/health',timeout=5).read()[:120])"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("  host -> 127.0.0.1:8000/api/health :",
              (hp.stdout or hp.stderr or "").strip().splitlines()[-1][:160] if (hp.stdout or hp.stderr) else "no output")
        print("  (host OK + container REFUSED = port-mapping/firewall. BOTH refused = the app is down.)")
    return r.returncode == 0


def cmd_doctor(_):
    """Diagnose + repair the sandbox. Checks BOTH halves: the browser (Chrome/CDP) and the brain
       (the LLM proxy). Clears a stale Chrome profile lock -- a hard container recreate leaves
       SingletonLock behind and Chrome then refuses to start -- restarts, and verifies.

    FIXED 2026-08-16: every docker call here targeted the DEFAULT compose file, i.e. the OLD
    single-container sandbox's `jhw-agent`, not `jhw-agent-local` from docker-compose.local.yml
    which is what `apply` actually runs. So doctor was diagnosing a container that was not the one
    failing, and on a machine running the hybrid stack it reported on nothing at all."""
    LOCAL = ("-f", "docker-compose.local.yml")
    print("[i] === sandbox logs (last 30) ===")
    dc(*LOCAL, "logs", "--tail", "30", "jhw-agent", check=False)
    _doctor_brain()
    print("\n[i] clearing any stale Chrome profile lock …")
    for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        subprocess.run(["docker", "compose", *LOCAL, "exec", "-T", "jhw-browser",
                        "rm", "-f", f"/home/chrome-user/chrome-data/{f}"], cwd=HERE, capture_output=True)
    print("[i] restarting the browser sandbox …")
    dc(*LOCAL, "restart", "jhw-browser", check=False)
    # jhw-agent and jhw-stagehand run with `network_mode: service:jhw-browser`. Restarting the
    # NETNS OWNER destroys their network stack, so a plain restart leaves them attached to a
    # namespace that no longer exists -- every `exec ... curl 127.0.0.1:9222` then fails and the
    # wait reports "Chrome still down" about a Chrome that is running perfectly. They must be
    # RECREATED, not restarted. (Introduced by me on 2026-08-16; the old doctor restarted the
    # single-container sandbox where Chrome and the agent shared one container, so it never hit this.)
    print("[i] recreating the containers that share its network namespace …")
    dc(*LOCAL, "up", "-d", "--force-recreate", "jhw-agent", "jhw-stagehand", check=False)
    if _wait_chrome(45):
        print("[OK] Chrome is back.")
        _doctor_brain()
        print("\nNow run:  python jhw.py apply <job>")
        return
    print("\n[ERR] Chrome still down — logs again so we can see why:")
    dc(*LOCAL, "logs", "--tail", "40", "jhw-browser", check=False)


def cmd_scan(_):
    """DevSecOps gate (CLAUDE.md standing rule): Trivy = Aqua Security's OSS scanner.
       Scans: dependency/OS CVEs + hardcoded SECRETS + Dockerfile/compose misconfiguration + images.
       Runs as a container — nothing to install. Review HIGH/CRITICAL before deploying."""
    sev = "HIGH,CRITICAL"
    print("[i] 1/2 repo scan (deps + secrets + Dockerfile/compose misconfig) …")
    sh("docker", "run", "--rm", "-v", f"{ROOT}:/src:ro", "-v", "jhw_trivy:/root/.cache/trivy",
       "aquasec/trivy:latest", "fs", "--scanners", "vuln,secret,misconfig",
       "--severity", sev, "--exit-code", "0", "/src", cwd=ROOT, check=False)
    print("\n[i] 2/2 image scan (built images) …")
    for img in ("jobhuntwow-app-backend:latest", "agent-jhw-agent:latest"):
        sh("docker", "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock",
           "-v", "jhw_trivy:/root/.cache/trivy", "aquasec/trivy:latest", "image",
           "--severity", sev, "--exit-code", "0", "--ignore-unfixed", img, cwd=ROOT, check=False)
    print("\n[OK] scan done. Fix HIGH/CRITICAL, or record why it is accepted (KISS rule 8).")


def cmd_obs(_):
    """Start observability: JSON events -> Promtail -> Loki -> Grafana. Profile-gated: never runs
       during a normal apply."""
    dc("-f", "docker-compose.obs.yml", "up", "-d", cwd=ROOT, check=False)
    print("\n[OK] Grafana:  http://127.0.0.1:3000   (admin / jobhuntwow — override GRAFANA_PASSWORD)")
    print("     Dashboard: JobHuntWOW -> Apply Pipeline")
    print(f"     Events:    {os.path.join(HERE, 'out', 'events.jsonl')}")


def cmd_login(_):
    ensure_prereqs()
    print("[i] ensuring LinkedIn login (auto, else one-time manual in noVNC) …")
    dc("exec", "-T", "jhw-agent", "python3", "/agent/agent.py", "login", check=False)


def _healthy(t=2):
    import urllib.request as _u
    for url in ("http://127.0.0.1:9222/json/version", "http://127.0.0.1:8000/api/health"):
        try:
            _u.urlopen(url, timeout=t)
        except Exception:
            return False
    return True


def _wait_chrome(tries=30):
    """Wait until Chrome's CDP answers INSIDE the sandbox. Checked from inside the container on purpose:
       WSL cannot reliably reach Docker's published ports, so a host-side probe always times out.
       `up -d` returns as soon as the container starts, but Chrome (Xvfb + browser) needs ~10-25s more."""
    import time as _t
    for i in range(tries):
        p = subprocess.run(["docker", "compose", "exec", "-T", "jhw-agent",
                            "curl", "-fsS", "http://127.0.0.1:9222/json/version"],
                           cwd=HERE, capture_output=True)
        if p.returncode == 0:
            if i:
                print()
            print("[OK] sandbox Chrome ready.")
            return True
        print("[i] waiting for Chrome in the sandbox …" if i == 0 else ".", end="", flush=True)
        _t.sleep(2)
    print("\n[warn] Chrome not ready after ~60s — continuing anyway (check: python jhw.py logs).")
    return False


def _container_running(name: str) -> bool:
    r = subprocess.run(["docker", "ps", "-q", "-f", f"name=^{name}$"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return bool((r.stdout or "").strip())


def _drop_hermes() -> None:
    """Remove the retired jhw-hermes container if it is still lying around.

    It was replaced by jhw_bot.py + Stagehand. Compose no longer defines it, so `compose down`
    will NOT clean it up - it survives as an orphan holding ports and RAM.
    """
    r = subprocess.run(["docker", "ps", "-aq", "-f", "name=^jhw-hermes$"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if (r.stdout or "").strip():
        print("[i] removing the retired jhw-hermes container (freeing its ports + RAM)")
        subprocess.run(["docker", "rm", "-f", "jhw-hermes"], capture_output=True)


def _port_busy(port: int) -> bool:
    """Is something already listening on this host port?"""
    import socket
    s_ = socket.socket()
    s_.settimeout(0.6)
    try:
        s_.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s_.close()


def _ensure_stack(build=False):
    """One command: ensure backend + /v1 proxy + browser sandbox are up AND Chrome is actually
       listening on CDP, then return."""
    ensure_prereqs()
    _drop_hermes()
    # PORT CONFLICT SELF-HEAL: the hybrid local stack (docker-compose.local.yml -> jhw-browser,
    # jhw-stagehand, jhw-bot) and this single-container sandbox BOTH bind 9090/9222/5900. If the
    # hybrid stack is up, `docker compose up` fails with "port is already allocated", jhw-agent
    # never starts, and the exec then reports "service is not running". The verb repairs it
    # instead of asking the human to guess (CLAUDE.md: a verb does everything it needs itself).
    # ONE sandbox: jhw-browser (Chrome+noVNC) + jhw-stagehand + jhw-agent + jhw-bot all share the
    # browser's network namespace. Previously `apply` used the OLD single-container stack, which
    # binds the same 9090/9222/5900 — so the two fought over ports AND Stagehand was unreachable
    # from the agent (different netns), which is why it was never actually used.
    if _port_busy(9090) and not _container_running("jhw-browser"):
        print("[i] freeing 9090/9222/5900 — the old single-container sandbox is holding them")
        dc("down", check=False)

    _ensure_shared_net()          # both compose projects join it; must exist before either `up`
    flags = ["up", "-d"] + (["--build", "--force-recreate"] if build else [])
    dc(*(flags + ["backend"]), cwd=ROOT, check=False)   # backend holds the DO key + model routing
    # --build every time: the stagehand service COMPILES server.ts into its image, so without it
    # code changes silently never reach the container. Docker layer cache makes this ~1s no-op.
    lflags = flags if "--build" in flags else flags + ["--build"]
    dc("-f", "docker-compose.local.yml", *lflags, check=False)   # unified sandbox
    # OBSERVABILITY comes up with the stack — one command, no separate step. Idempotent + instant once
    # running (restart: unless-stopped). Loki + Promtail + Grafana on 127.0.0.1:3000.
    dc("-f", "docker-compose.obs.yml", "up", "-d", cwd=ROOT, check=False)
    _wait_chrome()                                       # <- the sandbox was 'Started', Chrome wasn't up yet
    # RETURN IT. The first version called _ensure_brain(), printed "NOT starting the apply", and then
    # ran the apply anyway because nobody read the result -- the message was simply false. A guard
    # whose verdict is discarded is worse than no guard: it teaches you to distrust the output.
    return _ensure_brain()


def _ensure_shared_net(name: str = "jhw_shared") -> None:
    """`docker network create` is idempotent-by-check; compose declares the net EXTERNAL so it must
       exist first. One command, no manual setup step (CLAUDE.md: a verb does what it needs itself)."""
    r = subprocess.run(["docker", "network", "inspect", name],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"[i] creating the shared docker network {name} …")
        subprocess.run(["docker", "network", "create", name],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")


def _backend_publishes_8000() -> bool:
    """Is the backend's port actually PUBLISHED to the host?

    MEASURED 2026-08-16, and this is the whole bug: `docker compose ps backend` reported
        STATUS "Up About an hour (healthy)"   PORTS "8000/tcp"
    `8000/tcp` with no `127.0.0.1:8000->` prefix means EXPOSED BUT NOT PUBLISHED. The container was
    CREATED 3 WEEKS AGO, and **port mappings are fixed at container creation time**. `up -d` finds an
    existing container, prints "Running", and leaves the old mapping alone -- so the compose file can
    say `127.0.0.1:8000:8000` forever and the running container will never gain it.

    Symptoms this produced, all consistent: the backend's own logs showed /api/health 200 from
    127.0.0.1 (its internal healthcheck talking to itself), the HOST got WinError 10061 refused, the
    container got Errno 111 refused, and every apply died at the first LLM call with
    "All connection attempts failed" while looking like a code regression."""
    r = subprocess.run(["docker", "compose", "ps", "--format", "{{.Ports}}", "backend"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ports = (r.stdout or "").strip()
    return "->8000" in ports or ":8000->" in ports


def _brain_reachable() -> bool:
    """Can jhw-agent actually open a TCP connection to the LLM proxy? Checked from INSIDE the
       container, because the host being able to reach it proves nothing about the sandbox."""
    probe = ("import os,socket,sys\n"
             "from urllib.parse import urlparse\n"
             "u=urlparse(os.getenv('JHW_PROXY_BASE','http://backend:8000/v1'))\n"
             "s=socket.socket(); s.settimeout(4)\n"
             "try: s.connect((u.hostname,u.port or 80)); sys.exit(0)\n"
             "except Exception: sys.exit(1)\n"
             "finally: s.close()\n")
    r = subprocess.run(["docker", "compose", "-f", "docker-compose.local.yml", "exec", "-T",
                        "jhw-agent", "python3", "-c", probe],
                       cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def _ensure_brain() -> bool:
    """A verb must repair its own prerequisites (CLAUDE.md HARD RULE). The LLM proxy is the one
       nothing was checking, and `apply` without it uploads your documents and then dies on the
       first model call, which looks like a code bug and is not one.

    MEASURED 2026-08-16, and my first attempt at this made it WORSE:
      * `docker compose ps backend` showed PORTS=8000/tcp -- exposed, never published.
      * so I ran `up -d --force-recreate backend`, which DESTROYED a healthy running container, and
        the replacement could not start:
            ports are not available: exposing port TCP 127.0.0.1:8000 -> 127.0.0.1:0:
            listen tcp4 127.0.0.1:8000: bind: An attempt was made to access a socket in a way
            forbidden by its access permissions.
        That is Windows WSAEACCES: the OS refuses the bind because 8000 sits inside a Hyper-V
        reserved range. Those ranges are re-picked on reboot, which is exactly why the 21 Jul run
        worked and today's did not. NO compose setting wins that argument, and recreating the
        container can only ever fail again.
    THE FIX IS TO STOP NEEDING A HOST PORT: backend and the sandbox share the external docker
    network `jhw_shared`, and the agent addresses it as `backend:8000` -- the same pattern this repo
    already uses for frontend nginx -> backend:8000 and, on the droplet, Caddy -> jhw-web:8000.
    Verify a candidate host port before ever re-adding a publish:
        netsh interface ipv4 show excludedportrange protocol=tcp
    """
    if _brain_reachable():
        print("[OK] LLM proxy reachable from the sandbox.")
        return True
    print("[!] the sandbox cannot reach the LLM proxy — repairing before we waste an apply run")
    _ensure_shared_net()
    # Attach the RUNNING backend to the shared network without recreating it. `network connect` is
    # additive and non-destructive: it cannot fail on a port bind, and it cannot take the container
    # down. Recreating is what broke the stack, so it is deliberately NOT done here.
    # --alias backend IS LOAD-BEARING. `docker network connect` grants the CONTAINER NAME as a DNS
    # alias (jobhuntwow-app-backend-1), NOT the compose SERVICE name. Without the alias
    # `http://backend:8000` would not resolve and this repair would silently accomplish nothing.
    r = subprocess.run(["docker", "network", "connect", "--alias", "backend",
                        "jhw_shared", "jobhuntwow-app-backend-1"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    msg = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0:
        print("    attached the running backend to jhw_shared (no recreate, no port bind)")
    elif "already exists" in msg:
        print("    backend is already on jhw_shared")
    else:
        print(f"    could not attach the backend: {msg[:160]}")
        print("    is it running?  docker compose ps backend   (in the repo root)")
    import time as _t
    for _ in range(10):
        if _brain_reachable():
            print("[OK] LLM proxy reachable as backend:8000 — no host port needed.")
            return True
        _t.sleep(2)
    print("[X] STILL unreachable. Refusing to start the apply — it would upload your documents")
    print("    and then die on the first model call.")
    # NAME A VERB THAT EXISTS FROM WHERE HE IS STANDING. This said `python jhw.py doctor`, and the
    # operator runs the ROOT jhw.py, whose verbs are deploy/diagnose/logs/... -- so the one line meant
    # to help him produced `invalid choice: 'doctor'`. `doctor` is a verb of agent/jhw.py.
    print("    Evidence:  python agent/jhw.py doctor")
    print("               python agent/jhw.py logs jhw-agent")
    return False


def _wait_health():
    import time as _t
    for i in range(20):                                 # ~40s cap, then continue anyway
        if _healthy():
            print("[OK] stack healthy."); return True
        print("[i] waiting for backend (:8000) + sandbox (:9222) …" if i == 0 else ".",
              end="", flush=True)
        _t.sleep(2)
    print("\n[warn] health wait timed out — continuing anyway.")
    return False


def cmd_run(a):
    """A->Z (one command): backend + /v1 proxy + browser sandbox + observability up and Chrome ready,
       LinkedIn login, then scrape -> tailor -> apply. Uses the same self-healing startup as `apply`."""
    _ensure_stack()
    cmd_login(None)
    print(f"\n[i] === scrape {a.job} ==="); _exec_agent("scrape", a.job, "/agent/out/job.json")
    print("\n[i] === tailor (tailor resume + cover letter to THIS job) ==="); dc("exec", "-T", "jhw-agent", "python3", "/agent/tailor.py", check=False)
    print(f"\n[i] === apply {a.job} ==="); _exec_agent("apply", a.job)
    print(f"\n[OK] pipeline done. Review in noVNC ({NOVNC}) and click Submit yourself.")
    print("[i] Chat/answer the robot on Telegram (send /start). If it needs a field it will ask you there.")


def cmd_bot(a):
    """Full LLM Telegram assistant (Cassandra-style) + human-in-the-loop relay. It is the ONLY
       Telegram poller: the apply engine hands it questions via out/asks/ and it relays your reply.
       Comes up automatically with the stack; this (re)creates it from the current .env and tails logs."""
    ensure_prereqs()
    dc("up", "-d", "--build", "--force-recreate", "jhw-bot", check=False)
    print("[OK] jhw-bot running (LLM assistant + ask-relay). Message it on Telegram: /start")
    if getattr(a, "logs", False):
        dc("logs", "-f", "jhw-bot", check=False)


def _wait_local():
    import time as _t, urllib.request as _u
    for i in range(40):                                   # ~2min cap
        try:
            _u.urlopen("http://127.0.0.1:9090/", timeout=3); print("\n[OK] browser (noVNC) ready."); return True
        except Exception:
            print("[i] waiting for the browser container ..." if i == 0 else ".", end="", flush=True)
            _t.sleep(3)
    print("\n[warn] browser not ready — check: docker compose -f docker-compose.local.yml logs -f")
    return False


def cmd_local(a):
    """ONE command for the ISOLATED local hybrid stack. Does EVERYTHING itself (no manual steps):
       frees ports held by the older single-container sandbox, starts the DO-proxy backend (brains),
       then brings up the isolated containers (Chromium+noVNC browser + Stagehand + candidate bot)."""
    ensure_prereqs()
    _drop_hermes()                                        # retired container: compose can't clean it
    print("[i] freeing ports (stopping the old single-container sandbox if running) ...")
    dc("down", check=False)                               # agent/docker-compose.yml -> frees 9090/9222/5900
    print("[i] starting brains (DO-proxy backend on :8000) ...")
    dc("up", "-d", "--build", "--force-recreate", "backend", cwd=ROOT, check=False)  # rebuild proxy + reload token
    print("[i] bringing up the isolated local stack (browser + stagehand + bot) ...")
    dc("-f", "docker-compose.local.yml", "up", "-d", "--build", check=False)
    _wait_local()
    print(f"[OK] Local stack up. Watch: {NOVNC}  ·  Talk to the agent on Telegram (send /start).")
    print("     Logs: docker compose -f docker-compose.local.yml logs -f   ·   Stop: python jhw.py local-down")


def cmd_local_down(a):
    _drop_hermes()
    """Stop the isolated local hybrid stack."""
    dc("-f", "docker-compose.local.yml", "down", check=False)
    print("[OK] local stack stopped.")


def main():
    p = argparse.ArgumentParser(description="jhw-agent ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backend").set_defaults(fn=cmd_backend)
    sub.add_parser("up").set_defaults(fn=cmd_up)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("logs").set_defaults(fn=cmd_logs)
    s = sub.add_parser("scrape"); s.add_argument("job"); s.set_defaults(fn=cmd_scrape)
    a = sub.add_parser("apply"); a.add_argument("job"); a.set_defaults(fn=cmd_apply)
    sub.add_parser("tailor").set_defaults(fn=cmd_tailor)
    sub.add_parser("telegram").set_defaults(fn=cmd_telegram)
    sub.add_parser("models").set_defaults(fn=cmd_models)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    ap = sub.add_parser("atspw"); ap.add_argument("host"); ap.add_argument("password")
    ap.add_argument("email", nargs="?", default=""); ap.set_defaults(fn=cmd_atspw)
    ip = sub.add_parser("import-passwords"); ip.add_argument("csv"); ip.set_defaults(fn=cmd_import_passwords)
    g = sub.add_parser("git")
    g.add_argument("--fix", action="store_true",
                   help="untrack the private files and write the ignore rules")
    g.add_argument("--repo", action="store_true",
                   help="create the PRIVATE repo for the automation (needs `gh auth login` once)")
    g.set_defaults(fn=cmd_git)
    mm = sub.add_parser("memory"); mm.add_argument("--forget", default="")
    mm.set_defaults(fn=cmd_memory)
    sub.add_parser("inspect").set_defaults(fn=cmd_inspect)
    rc = sub.add_parser("record"); rc.add_argument("url")
    rc.add_argument("--name", default=""); rc.set_defaults(fn=cmd_record)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    sub.add_parser("obs").set_defaults(fn=cmd_obs)
    r = sub.add_parser("run"); r.add_argument("job"); r.set_defaults(fn=cmd_run)
    b = sub.add_parser("bot"); b.add_argument("--logs", action="store_true"); b.set_defaults(fn=cmd_bot)
    sub.add_parser("local").set_defaults(fn=cmd_local)
    sub.add_parser("local-down").set_defaults(fn=cmd_local_down)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
  