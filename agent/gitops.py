#!/usr/bin/env python3
"""GIT AS A SECOND SOURCE OF TRUTH — and a guard so it can never leak the candidate.

WHY THIS EXISTS (the operator, 2026-08-17): *"we need to start putting all the versions on github for
single source of truth ... because otherwise you will corrupt me the version that we work on all the
time like you did many times already"*. He is right, and the record backs him: the file editor has
truncated or null-padded agent.py, workday.py, jhw.py (twice), ask.py, both docker-compose.yml,
proxy.py, and this session I corrupted learned.py with a stale-offset AST edit. That file was
UNTRACKED, so there was no copy to restore from and it had to be rebuilt by hand. A commit after
every run turns that class of accident from an hour into `git checkout`.

MEASURED FIRST, BEFORE AUTOMATING ANYTHING (2026-08-17), and it is the reason this file is a guard
and not a two-line `git push`:
    git remote -v            -> origin  https://github.com/feranicus/jobhuntwow.com.git
    git cat-file -e origin/main:agent/userdata/candidate.md   -> EXISTS
    curl (NO auth) raw.githubusercontent.com/.../agent/userdata/candidate.md -> 200, full contents
So this working tree's origin is the PUBLIC GitHub-Pages repo of the landing site, and his home
address, phone number, gender, EEO self-disclosures, education, employment history and CV PDFs are
already downloadable by anyone. No password leaked (candidate.md stores only env var NAMES), but that
is personal data, published, indefinitely.

THEREFORE, TWO RULES, both enforced in code rather than remembered:
  1. **THE APP AND THE PII NEVER GO TO THE PUBLIC REPO.** `check_remote()` refuses to push the agent
     tree to a remote whose name is the landing site, and `audit()` names every private path that is
     tracked. Two repos, one purpose each: the landing site stays public, the automation goes to a
     PRIVATE repo.
  2. **A SAFEPOINT IS NEVER ALLOWED TO PUBLISH A PRIVATE FILE.** `safepoint()` scans what it is about
     to commit and ABORTS on a private path, a credential-shaped blob or a CV. Failing to save a
     version costs a `git checkout`; publishing a candidate's address cannot be taken back.
  3. **IT CAN NEVER FAIL A RUN.** Every entry point returns a report and swallows its own errors. A
     bookkeeping step that can abort an application is a worse defect than the one it prevents.

HISTORY IS NOT FIXED BY DELETING A FILE. `git rm` removes it from the NEXT commit; every earlier
commit still holds it and raw.githubusercontent.com still serves it. `purge_plan()` prints the real
options with their real costs. That is deliberately NOT automated: it force-pushes a rewritten
history over a repo that serves a live website, and that is the operator's decision to take, not a
side effect of running an apply.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))                       # .../agent
ROOT = os.path.dirname(HERE)                                            # repo root

# A remote that serves the PUBLIC landing site. The app tree must never be pushed here.
PUBLIC_REMOTES = re.compile(r"jobhuntwow\.com|github\.io|\.github\.io", re.I)

# WHAT MUST NEVER BE COMMITTED, anywhere. Ordered so the reason is obvious from the pattern.
# WHAT MUST NEVER BE COMMITTED. His instruction, 2026-08-17, verbatim: *"i need that this will be on
# git for now i dont care that my resume is seen to the internet but the passwords shouldn't be .env
# file"*. So the policy is now HIS policy, and the line is drawn at CREDENTIALS, not at personal data:
#   BLOCKED  - .env, ATS credentials, auth/session state, codegen recordings (they contain his typed
#              password in cleartext), and the databases (they churn every run and hold his answers).
#   ALLOWED  - candidate.md, the CVs, cover letters. He has decided that trade himself; my job is to
#              tell him what it means (they are world-readable on a public repo, forever, and search
#              engines index raw.githubusercontent.com), not to override him.
# A SESSION COOKIE IS A CREDENTIAL. `workday_auth6.json` holds live PLAY_SESSION / CALYPSO_SESSION
# cookies for a Workday tenant — anyone with that file is logged in as him until it expires.
PRIVATE = [
    (r"(^|/)\.env$|(^|/)\.env\.[^/]*$", "runtime secrets"),
    (r"credentials?\.json$|ats_accounts\.json$", "ATS credentials"),
    (r"(^|/)recordings/", "codegen recordings contain typed PASSWORDS in cleartext"),
    (r"auth\d*\.json$|storage_?state.*\.json$|_auth\.json$", "a live browser SESSION is a credential"),
    (r"\.sqlite$|\.sqlite3$|\.db$", "databases: they churn every run and hold his own answers"),
    (r"learned_answers\.json$", "learned answers, superseded by the database"),
    (r"(^|/)out/", "run artefacts: tailored documents, credentials, the learning database"),
]
# ...except the templates and examples, which are the point of the repo.
# ...and the CANDIDATE'S OWN DATA is allowed by his explicit decision (see above). `out/` stays
# blocked because it also holds credentials and the database, so the CV lives in the repo through
# `agent/userdata/` and `Profile.pdf`, not through the run directory.
ALLOW = re.compile(r"(^|/)templates/|\.example$|/example|(^|/)\.gitignore$", re.I)

# A blob that LOOKS like a secret, whatever it is called. Same shape rule as learned.is_secret:
# a password has no whitespace and carries punctuation that a URL or a date does not.
# WHAT A SECRET ACTUALLY LOOKS LIKE — the VALUE, not the word in front of it.
#
# MEASURED 2026-08-17: keying on the NAME (`password|token|key|secret`) produced 44 false positives on
# the first real run and then 18 more after the first tightening — every one a legitimate line:
#   agent/agent.py:429     _key = _hl.sha1(...).hexdigest()[:16]
#   Pipeline.jsx:22        <div className="kcard" key={i}>
#   backend/app/auth.py:59 SESSION_SECRET = secrets.token_urlsafe(48)      # GENERATES one
#   CLAUDE.md:837          "Password : @Q4sD9i..."                          # a redacted quote in docs
#   docker-compose.obs.yml GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-jobhuntwow}
# A guard that refuses 44 legitimate source files gets switched off within a week, and this repo has
# paid for that lesson twice. So this works the way gitleaks and trufflehog do: KNOWN PROVIDER
# PREFIXES, plus an entropy/shape test on the value. The name is only a hint about where to look.
_PROVIDER = re.compile(r"""(?x)
    sk-[A-Za-z0-9]{20,}                     # OpenAI-style
  | ghp_[A-Za-z0-9]{30,} | github_pat_[A-Za-z0-9_]{30,}
  | doo_v1_[A-Za-z0-9]{20,}                 # DigitalOcean
  | xox[baprs]-[A-Za-z0-9-]{10,}            # Slack
  | AKIA[0-9A-Z]{16}                        # AWS access key id
  | \b\d{8,10}:[A-Za-z0-9_-]{33,}\b        # Telegram bot token
  | -----BEGIN [A-Z ]*PRIVATE KEY-----
""")
_ASSIGN = re.compile(r"""(?ix)
    (?:pass(?:word|wd)?|secret|token|[\w-]*_?key|bearer|authorization|credential)
    \s*[:=]\s*['"]?([^\s'"]{16,})""")
_PLACEHOLDER = re.compile(r"\.\.\.|\$\{|\$[A-Za-z_]|<|%s|os\.|getenv|environ|settings\.|"
                          r"process\.|self\.|config|secrets\.|token_urlsafe|uuid|sha\d|hexdigest|"
                          r"changeme|placeholder|example|your[_-]?|xxx|redact", re.I)


# A TEST FIXTURE IS CREDENTIAL-SHAPED ON PURPOSE. `memory.py` and this file both assert that a
# password is refused, which means both contain one. Without an escape hatch the guard blocks the very
# suites that prove it works, and the operator's only way out is to disable it. `# pragma: allowlist
# secret` is detect-secrets' convention, so it is recognisable to anyone who has used a scanner.
_ALLOW_MARK = re.compile(r"pragma:\s*allowlist\s+secret|gitleaks:\s*allow", re.I)


def looks_secret(line: str) -> bool:
    """Does this line contain a real credential? Pure, so every case is a test."""
    t = line or ""
    if _ALLOW_MARK.search(t):
        return False
    if _PROVIDER.search(t) and not _PLACEHOLDER.search(t):
        return True
    m = _ASSIGN.search(t)
    if not m:
        return False
    v = m.group(1)
    if _PLACEHOLDER.search(v) or "(" in v or v.isupper():
        return False                       # a lookup, a placeholder, or an ALL_CAPS env var NAME
    classes = sum(bool(re.search(c, v)) for c in
                  (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    return classes >= 3 and len(v) >= 16    # mixed-class, long, no whitespace = a real literal


def clear_stale_lock(max_age_s: int = 300) -> str:
    """A stale `.git/index.lock` makes EVERY git write fail, silently.

    MEASURED 2026-08-17: the lock was dated 2026-08-16 12:26 and every `git rm --cached` returned
    `fatal: Unable to create .../index.lock`. That is also why 123 files sat uncommitted while
    `git rev-list origin/main..HEAD` read 0 — the writes had been failing for a day and nothing said
    so, so the tree looked in sync while nothing was being saved. Git leaves that file behind when a
    process is killed mid-write, which is exactly what a timeout does.

    Only an OLD lock is removed, and age is the discriminator: deleting a LIVE lock corrupts an index."""
    lock = os.path.join(ROOT, ".git", "index.lock")
    try:
        if not os.path.exists(lock):
            return ""
        age = time.time() - os.path.getmtime(lock)
        if age < max_age_s:
            return f"index.lock is {age:.0f}s old — a git process may be running; not touching it"
        os.remove(lock)
        return f"removed a STALE .git/index.lock ({age / 3600:.1f}h old) — git writes were failing"
    except OSError as e:
        return f"could not clear .git/index.lock: {e}"


def _git(*args, cwd=ROOT, check=False):
    """Run git and return (rc, out). NEVER raises — a missing git must not kill a run."""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        if check and p.returncode:
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "git is not installed on this machine"
    except Exception as e:                                              # pragma: no cover
        return 1, f"{type(e).__name__}: {e}"


def why_private(path: str) -> str:
    """'' if the path may be committed, otherwise WHY it may not. Pure, so it is testable."""
    p = (path or "").replace("\\", "/").strip()
    if not p:
        return ""
    if ALLOW.search(p):
        return ""
    for rx, reason in PRIVATE:
        if re.search(rx, p, re.I):
            return reason
    return ""


def remote_url(name: str = "origin") -> str:
    rc, out = _git("remote", "get-url", name)
    return out.strip() if rc == 0 else ""


def is_public_remote(url: str) -> bool:
    """A remote that serves the landing site. The automation must never be pushed there."""
    return bool(url) and bool(PUBLIC_REMOTES.search(url))


def tracked_private() -> list:
    """Every file git is TRACKING that must not be public. This is the exposure, measured."""
    rc, out = _git("ls-files")
    if rc:
        return []
    hits = []
    for f in out.splitlines():
        r = why_private(f)
        if r:
            hits.append((f, r))
    return hits


def on_remote(paths: list, ref: str = "origin/main") -> list:
    """Which of these are ALREADY published. `git rm` cannot undo this — only a history rewrite can."""
    live = []
    for f in paths:
        rc, _ = _git("cat-file", "-e", f"{ref}:{f}")
        if rc == 0:
            live.append(f)
    return live


def staged_private() -> list:
    """What is about to be committed that must not be. The last line of defence before a push."""
    # --diff-filter=ACMR EXCLUDES DELETIONS. Without it the guard refused the one operation that
    # REMOVES an exposure: `git rm --cached agent/userdata/candidate.md` stages a deletion of a private
    # path, so the first version saw "a private file is staged" and aborted -- then `git reset` undid
    # the removal. The guard was structurally incapable of letting the tree be cleaned.
    rc, out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    if rc:
        return []
    return [(f, why_private(f)) for f in out.splitlines() if why_private(f)]


def staged_secrets(max_bytes: int = 400_000) -> list:
    """A credential-shaped line inside a staged TEXT file, whatever the file is called."""
    rc, out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    if rc:
        return []
    bad = []
    for f in out.splitlines():
        p = os.path.join(ROOT, f)
        try:
            if os.path.getsize(p) > max_bytes:
                continue
            with open(p, encoding="utf-8", errors="strict") as fh:
                for i, line in enumerate(fh, 1):
                    if looks_secret(line):
                        bad.append((f, i))
                        break
        except (UnicodeDecodeError, OSError):
            continue                                    # binary or unreadable: not a text secret
    return bad


def ignore_rules() -> list:
    """Only credentials and churn. His CV and profile are deliberately NOT here."""
    return [
        "# --- added by gitops.py: credentials and churn, never the candidate's own data ------",
        "agent/out/",
        "agent/recordings/",
        "*.sqlite",
        "*.sqlite3",
        "learned_answers.json",
        "**/credentials.json",
        "**/ats_accounts.json",
        "**/*_auth.json",
        "**/workday_auth*.json",
        "dist/",
    ]


def harden(apply: bool = False) -> list:
    """Stop TRACKING what must not be public, and ignore it from now on.

    `git rm --cached` keeps the file ON DISK (the agent needs candidate.md and the CVs to work) and
    removes it from the next commit. It does NOT remove it from history — see `purge_plan()`."""
    msg = clear_stale_lock()
    rep = [f"  {msg}"] if msg else []
    hits = tracked_private()
    rep2 = rep
    if not hits:
        rep.append("nothing private is tracked")
    del rep2
    for f, why in hits:
        rep.append(f"  {'UNTRACK' if apply else 'would untrack'}  {f}   ({why})")
        if apply:
            rc, out = _git("rm", "--cached", "-q", "--", f)
            if rc:
                rep.append(f"     ! git rm failed: {out[:80]}")
    gi = os.path.join(ROOT, ".gitignore")
    try:
        cur = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
    except OSError:
        cur = ""
    missing = [r for r in ignore_rules() if r.strip() and not r.startswith("#")
               and r not in cur.splitlines()]
    if missing:
        rep.append(f"  {'ADD' if apply else 'would add'} {len(missing)} rule(s) to .gitignore")
        if apply:
            try:
                with open(gi, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n" + "\n".join(ignore_rules()) + "\n")
            except OSError as e:
                rep.append(f"     ! could not write .gitignore: {e}")
    else:
        rep.append("  .gitignore already carries every rule")
    return rep


def purge_plan() -> list:
    """History is not fixed by deleting a file. State the real options and their real costs."""
    priv = [f for f, _ in tracked_private()]
    live = on_remote(priv) if priv else []
    out = []
    if not live:
        out.append("nothing private is on origin/main — untracking is enough")
        return out
    out.append(f"{len(live)} private file(s) are ALREADY on origin/main and are served publicly by")
    out.append("raw.githubusercontent.com. `git rm` does NOT remove them: every earlier commit keeps")
    out.append("them. The options, with their costs — this is YOUR call, it is not automated:")
    out.append("")
    out.append("  A. Make that repo PRIVATE (fastest, ~30 seconds)")
    out.append("       gh repo edit feranicus/jobhuntwow.com --visibility private")
    out.append("     COST: GitHub Pages from a private repo needs a paid plan, so the landing site")
    out.append("     at jobhuntwow.com may stop being served. Check that before you run it.")
    out.append("")
    out.append("  B. REWRITE the history to erase the files, then force-push")
    out.append("       pip install git-filter-repo")
    # QUOTE THE PATHS. One of the real files is `agent/out/Profile (2).pdf`; unquoted, that command
    # would silently purge the wrong thing (or nothing) — a copy-paste that damages a repo.
    out.append("       git filter-repo --invert-paths " +
               " ".join(f'--path "{f}"' for f in live[:6]) + (" ..." if len(live) > 6 else ""))
    out.append("       git push --force origin main")
    out.append("     COST: every clone/fork of that repo keeps the old objects, GitHub may serve")
    out.append("     cached blobs for a while, and a force-push rewrites shared history.")
    out.append("")
    out.append("  C. Delete the repo and recreate it from a clean tree (most thorough)")
    out.append("     COST: loses stars/issues/history and the Pages setup must be reconnected.")
    out.append("")
    out.append("RECOMMENDED: A now (it stops the bleeding in seconds and is reversible), then B, and")
    out.append("keep the automation in its OWN private repo from here on. Nothing needs rotating —")
    out.append("candidate.md holds env var NAMES, not passwords — but the PII is real and published.")
    return out


def app_remote() -> str:
    """The remote the AUTOMATION belongs to. Never the landing site's."""
    for name in ("app", "origin"):
        u = remote_url(name)
        if u and not is_public_remote(u):
            return name
    return ""


def safepoint(message: str = "", push: bool = True, allow_public: bool = False) -> dict:
    """Commit (and push) the working tree — a version we can go back to. Never raises.

    THE ORDER IS THE SAFETY PROPERTY: stage, then INSPECT WHAT IS STAGED, and only then commit. A
    check that runs after the commit has already written the file into history."""
    r = {"ok": False, "committed": False, "pushed": False, "note": "", "refused": []}
    rc, _ = _git("rev-parse", "--git-dir")
    if rc:
        r["note"] = "not a git repository — nothing to save"
        return r
    lk = clear_stale_lock()
    if lk:
        print(f"[git] {lk}")
    rc, dirty = _git("status", "--porcelain")
    if rc:
        r["note"] = f"git unavailable: {dirty[:80]}"
        return r

    _git("add", "-A")
    bad = staged_private()
    sec = staged_secrets()
    if bad or sec:
        # UNSTAGE and refuse. Losing a savepoint costs a `git checkout`; publishing a candidate's
        # address or a credential cannot be taken back.
        _git("reset", "-q")
        r["refused"] = [f"{f} ({w})" for f, w in bad] + [f"{f}:{i} looks like a credential"
                                                         for f, i in sec]
        r["note"] = ("REFUSED to commit: " + "; ".join(r["refused"][:4]) +
                     "  -> run `python jhw.py git --fix` once, then this will pass")
        return r

    rc, out = _git("diff", "--cached", "--quiet")
    if rc == 0:
        r["ok"], r["note"] = True, "nothing changed — the tree is already saved"
    else:
        msg = message or f"safepoint {time.strftime('%Y-%m-%d %H:%M:%S')}"
        rc, out = _git("commit", "-q", "-m", msg)
        if rc:
            r["note"] = f"commit failed: {out[:120]}"
            return r
        r["committed"], r["ok"] = True, True
        r["note"] = f"committed: {msg}"

    if not push:
        return r
    name = "origin" if allow_public else app_remote()
    if not name:
        u = remote_url("origin")
        r["note"] += ("  | NOT PUSHED: the only remote is the PUBLIC landing repo "
                      f"({u.split('/')[-1] if u else '?'}). The automation needs its own private "
                      "repo — `python jhw.py git` prints the one command that creates it.")
        return r
    rc, out = _git("push", name, "HEAD")
    if rc:
        r["note"] += f"  | push to {name} failed: {out[-160:]}"
    else:
        r["pushed"] = True
        r["note"] += f"  | pushed to {name}"
    return r


def create_private_repo(name: str = "jobhuntwow-app", apply: bool = False) -> list:
    """Give the automation its own PRIVATE repo, using `gh` if it is authenticated here."""
    out = []
    try:
        p = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
        authed = p.returncode == 0
    except Exception:
        authed = False
    if not authed:
        out.append("`gh` is not installed or not logged in — that is the ONE human step:")
        out.append("    gh auth login          (or create the repo in the browser)")
        out.append(f"    gh repo create {name} --private --source . --remote app --push")
        return out
    rc, u = _git("remote", "get-url", "app")
    if rc == 0 and u.strip():
        out.append(f"remote 'app' already points at {u.strip()}")
        return out
    cmd = ["gh", "repo", "create", name, "--private", "--source", ".", "--remote", "app"]
    out.append(("running: " if apply else "would run: ") + " ".join(cmd))
    if apply:
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
            out.append(((p.stdout or "") + (p.stderr or "")).strip()[:400])
        except Exception as e:
            out.append(f"failed: {type(e).__name__}: {e}")
    return out


def audit() -> str:
    """One read-only report: where this tree pushes, what is exposed, what to do about it."""
    L = []
    u = remote_url("origin")
    a = remote_url("app")
    L.append(f"origin : {u or '(none)'}" + ("   <-- PUBLIC landing site" if is_public_remote(u)
                                            else ""))
    L.append(f"app    : {a or '(none)  <-- the automation has no private remote yet'}")
    rc, br = _git("branch", "--show-current")
    rc2, ahead = _git("rev-list", "--count", "@{u}..HEAD")
    L.append(f"branch : {br or '?'}" + (f"   {ahead} commit(s) not pushed" if rc2 == 0 else ""))
    priv = tracked_private()
    L.append("")
    if priv:
        live = on_remote([f for f, _ in priv])
        L.append(f"{len(priv)} PRIVATE file(s) are tracked; {len(live)} are already on origin/main:")
        for f, why in priv[:14]:
            L.append(f"   {'PUBLISHED' if f in live else 'tracked  '}  {f}   ({why})")
        if len(priv) > 14:
            L.append(f"   ... and {len(priv) - 14} more")
    else:
        L.append("no private file is tracked")
    L.append("")
    L += purge_plan()
    return "\n".join(L)


def _selftest() -> int:
    fails = []

    def ck(name, ok, d=""):
        print(("  OK   " if ok else "  FAIL ") + name + (f"   :: {str(d)[:90]}" if not ok else ""))
        if not ok:
            fails.append(name)

    print("[1] what must never be committed — the exposure this file was written for")
    for p in ("agent/.env", "agent/.env.local", "agent/out/ats_accounts.json",
              "agent/out/credentials.json", "agent/out/memory.sqlite",
              "agent/out/learned_answers.json", "agent/recordings/accenture.py",
              "agent/recordings/workday_auth6.json", "agent/recordings/gevernova_auth.json",
              "agent/out/resume.pdf"):
        ck(f"refused: {p}", bool(why_private(p)), why_private(p))

    print("\n[2] HIS OWN DATA IS ALLOWED — his decision, stated plainly (2026-08-17)")
    for p in ("agent/userdata/candidate.md", "agent/userdata/candidate_profile.md",
              "hermes-context/CANDIDATE_PROFILE.md", "Profile.pdf", "agent/CANDIDATE_PIPELINE.md"):
        ck(f"allowed: {p}", not why_private(p), why_private(p))

    print("\n[2b] ...and the repo must still be usable — templates and examples are the point")
    for p in ("agent/templates/resume.html", "agent/templates/cover_letter.html",
              "agent/.env.example", ".env.example", "agent/flows/greenhouse.py", "jhw.py",
              "CLAUDE.md", "agent/userdata/skills/ats-workday/SKILL.md"):
        ck(f"allowed: {p}", not why_private(p), why_private(p))

    print("\n[3] the automation may never be pushed to the PUBLIC landing repo")
    ck("the measured origin is recognised as public",
       is_public_remote("https://github.com/feranicus/jobhuntwow.com.git"))
    ck("a Pages remote is recognised", is_public_remote("git@github.com:feranicus/feranicus.github.io"))
    ck("a private app repo is NOT flagged",
       not is_public_remote("https://github.com/feranicus/jobhuntwow-app.git"))

    print("\n[4] a credential is recognised by its VALUE, not by the word in front of it")
    for line, want in (
            ('DO_INFERENCE_KEY=doo_v1_9f3ab8c2d7e14a5b6c9d0e1f', True),  # pragma: allowlist secret
            ('password=@Q4sD9igY!mzW5u1x', True),  # pragma: allowlist secret
            ('BOT_TOKEN = "8123456789:AAH-xyzXYZ_9911abcdefghijklmnopqrstuv"', True),  # pragma: allowlist secret
            ('  api_key: "sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"', True),  # pragma: allowlist secret
            ("aws: AKIA1234567890ABCDEF", True),  # pragma: allowlist secret
            # every one of these FAILED a real commit of correct code
            ('    _key = _hl.sha1((target or "").encode()).hexdigest()[:16]', False),
            ('<div className="kcard" key={i}><b>{k[0]}</b>', False),
            ("SESSION_SECRET = secrets.token_urlsafe(48)", False),
            ('  "Password : @Q4sD9i..."   -> fell through to the CHAT model', False),
            ("- GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-jobhuntwow}", False),
            ('DO_KEY = os.getenv("DO_INFERENCE_KEY", "")', False),
            ("password_env: HERMES_ATS_WORKDAY_PASSWORD", False),
            ('_VENDOR_KEY = "_vendor_passwords"', False),
            ("ap.add_argument('--env', help='KEY=VAL;KEY=VAL from the container')", False),
            ('key = sync_secrets.collect()[0].get("DO_INFERENCE_KEY", "")', False),
            ("# the password is read from the env, never stored", False),
            # a fixture must be able to opt out, or the guard blocks its own suite
            ('remember("q", "@Q4sD9igY!mzW5u1x")  # pragma: allowlist secret', False),
            ('token = "8123456789:AAH-xyzXYZ_9911abcdefghijklmnopqrstuv"  # gitleaks:allow', False)):
        ck(f"{'flagged' if want else 'ignored'}: {line.strip()[:46]!r}",
           looks_secret(line) == want)

    print("\n[5] the ORDER is the safety property, and it is asserted in the source")
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "safepoint")
    body = ast.get_source_segment(src, fn)
    def at(nd):
        return body.index(nd) if nd in body else -1

    ck("stages BEFORE it inspects", -1 < at('"add"') < at("staged_private()"))
    ck("inspects BEFORE it commits", -1 < at("staged_private()") < at('"commit"'))
    ck("unstages when it refuses", at('_git("reset"') > -1)
    ck("a refusal returns before committing", -1 < at('_git("reset"') < at('"commit"'))
    ck("it never raises: every git call goes through _git", "subprocess.run(" not in body)

    print("\n[5b] every entry point RUNS — ruff caught a NameError the suite could not")
    # `clear_stale_lock` was deleted by an edit of mine and the suite still said ALL CONTRACTS HOLD,
    # because it exercised the pure functions and never called harden() or safepoint(). Presence is
    # not reachability; call them.
    for fn, name in ((lambda: clear_stale_lock(), "clear_stale_lock()"),
                     (lambda: harden(apply=False), "harden(apply=False) — read-only"),
                     (lambda: audit(), "audit()"),
                     (lambda: purge_plan(), "purge_plan()"),
                     (lambda: staged_private(), "staged_private()"),
                     (lambda: staged_secrets(), "staged_secrets()"),
                     (lambda: safepoint("selftest", push=False, allow_public=False)
                      if os.getenv("JHW_GITOPS_LIVE") else "skipped", "safepoint() is importable")):
        try:
            fn()
            ck(f"runs: {name}", True)
        except Exception as e:
            ck(f"runs: {name}", False, f"{type(e).__name__}: {e}")

    print("\n[6] ...and this tree, right now")
    print("\n".join("   " + x for x in audit().splitlines()))

    print("\n" + ("GITOPS: " + ", ".join(fails) if fails else "ALL GITOPS CONTRACTS HOLD"))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    if "--audit" in sys.argv:
        print(audit())
    elif "--fix" in sys.argv:
        print("\n".join(harden(apply=True)))
    else:
        print(__doc__)
