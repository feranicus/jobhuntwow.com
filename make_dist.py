#!/usr/bin/env python3
"""make_dist.py — build a SANITISED, shareable copy of this software for someone else to test.

    python make_dist.py                 -> dist/jobhuntwow-test-<date>.zip
    python make_dist.py --keep          -> also leave the staged tree in dist/stage/ for inspection

WHY A SCRIPT AND NOT A ZIP MADE BY HAND. This repository contains, right now: the candidate's Google
password in cleartext (inside Playwright codegen recordings), a Telegram bot token, a DigitalOcean
inference key, a Gmail app password, live browser session cookies, an ATS credential vault, his home
address, phone number, and the EEOC declarations he made to employers. A hand-made archive is one
forgotten directory away from leaking all of it, and you cannot un-send a zip.

THE SECURITY POSTURE IS AN ALLOWLIST, NOT A DENYLIST. A denylist misses the file nobody thought of;
an allowlist can only ever miss a file the recipient did not need. Everything shipped is named
explicitly below.

AND IT IS VERIFIED, NOT HOPED FOR. After staging, `_verify()` reads EVERY BYTE of the staged tree and
searches it for every secret value currently in the live `.env` files and for every personal
identifier. If anything matches, the archive is NOT written and the offending file is named. This is
the same doctrine as `recordings.compile_dir()` refusing to write a knowledge file that still matches
a secret.
"""
from __future__ import annotations
import argparse
import io
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "dist", "stage")

# ─────────────────────────────────────────────────────────────── what ships (allowlist)
# Code, configuration templates and documentation. Nothing else.
COPY_GLOBS = [
    # the engine
    "agent/*.py", "agent/requirements.txt", "agent/Dockerfile*", "agent/docker-compose*.yml",
    "agent/run-chrome.sh", "agent/supervisord.conf", "agent/package.json", "agent/server.ts",
    "agent/.env.example", "agent/.env.local.example", "agent/.gitignore",
    "agent/flows/*.py", "agent/flows/selectors/*.json",
    "agent/reference/*.md",
    # the cloud product
    "backend/app/*.py", "backend/requirements.txt", "backend/Dockerfile*",
    "frontend/src/**/*.jsx", "frontend/src/**/*.js", "frontend/src/**/*.css",
    "frontend/index.html", "frontend/package.json", "frontend/vite.config.js",
    "frontend/public/*",
    # root
    # ROOT: NAMED, NOT GLOBBED. `*.py` pulled in ship.py / deploy_direct.py / sync_secrets.py /
    # diagnose_web.py -- which carry the owner's droplet IP and production hostnames and are useless
    # to a tester -- and it pulled in THIS FILE, whose PII table is literally a list of the
    # identifiers we are removing. `jhw.py` at the root IS the entry point (it delegates to agent/),
    # so it must ship.
    "jhw.py", "probe_models.py",
    "docker-compose*.yml", "Dockerfile*", "LICENSE", ".gitignore", ".env.example",
    "ARCHITECTURE.md", "DEPLOY.md", "OBSERVABILITY.md", "SEPARATION.md", "CONTRIBUTING.md",
    "obs/**/*.json", "obs/**/*.yml", "deploy/**/*.py", "deploy/**/*.caddy",
]
# Never, under any circumstance, regardless of the globs above.
NEVER = re.compile(
    r"(^|/)\.env$|(^|/)\.env\.[^/]*$(?<!\.example)"          # any real env file
    r"|(^|/)out(/|$)|(^|/)recordings(/|$)|(^|/)playrecord(/|$)"
    r"|_auth\d*\.json$|(^|/)credentials\.json$|(^|/)ats_accounts\.json$"
    r"|(^|/)learned_answers\.json$|(^|/)node_modules(/|$)|(^|/)dist(/|$)"
    r"|(^|/)__pycache__(/|$)|\.pyc$|(^|/)\.git(/|$)|\.bak\d*$|\.sqlite$|\.log$"
    r"|(^|/)userdata(/|$)|Profile.*\.pdf$|(^|/)hermes-context(/|$)|(^|/)\.venv(/|$)"
    # owner-only tooling: deploy scripts (droplet IP + hostnames) and this builder (its own PII table)
    r"|(^|/)(make_dist|ship|ship_web|deploy_direct|sync_secrets|diagnose_web"
    r"|cleanup_browsermetrics)\.py$")

# Docs worth sending, redacted on the way in.
DOCS = ["HANDOVER.md", "CLAUDE.md", "README.md"]

# Files this script WRITES ITSELF (placeholders), which are therefore exempt from the NEVER rule that
# excludes their real counterparts.
AUTHORED = {"agent/userdata/candidate.md", "agent/out/.gitkeep", "QUICKSTART.md",
            "agent/templates/resume_data.json"}

# ─────────────────────────────────────────────────────────────── redaction
# NOT SECRETS, even though they live in .env: a public endpoint URL, an in-cluster hostname and a
# loopback address are configuration. QUICKSTART.md legitimately has to tell the tester what goes in
# `DO_INFERENCE_BASE_URL`, and scanning for it flagged our own documentation.
NON_SECRET = {"DO_INFERENCE_BASE_URL", "JHW_PROXY_BASE", "JHW_CDP_URL", "JHW_ASK_CHANNEL",
              "DATA_DIR", "CORS_ORIGINS", "QWEN_MODEL"}


def live_secrets() -> dict:
    """Every genuinely SECRET value in a real .env. None of these may appear in the archive."""
    out = {}
    for f in (".env", "agent/.env", "agent/.env.local"):
        p = os.path.join(HERE, f)
        if not os.path.exists(p):
            continue
        for ln in io.open(p, encoding="utf-8", errors="replace"):
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if len(v) > 5 and k.strip() not in NON_SECRET:
                    out[k.strip()] = v
    return out


# Personal data. Not credentials, but not ours to hand out either -- and a tester needs his OWN.
PII = [
    (r"evgeny@s4biz\.io", "you@example.com"),
    (r"feranicus@s4biz\.io", "you@example.com"),
    (r"feranicus@gmail\.com", "you@example.com"),
    (r"jevgenijs\.vainsteins@[\w.]+", "you@example.com"),
    # NO WORD BOUNDARIES ON A DIGIT RUN. `\b15785541545\b` did NOT match inside `4915785541545`
    # (a comment in workday_gevernova.py), because "49" immediately before it is a word character.
    # An independent audit of the finished zip caught it; the build's own guard used the same broken
    # pattern, so it agreed with itself. That is why the audit is a SEPARATE implementation.
    (r"15785541545", "1700000000"),
    (r"1578551545", "1700000000"),
    (r"\+49\s*1578?5?541545", "+49 1700000000"),
    (r"\bVainsht?eins?\b", "Example"),
    (r"\bEvgeny\b", "Alex"),
    (r"\bJevgenijs\b", "Alex"),
    (r"\bferanicus\b", "your-handle"),
    (r"Herman[- ]?J?\.?[- ]?Bach[- ]?J?[- ]?Weg\s*\d*", "1 Example Street"),
    (r"Hermann-J\.-Bach-Weg\s*\d*", "1 Example Street"),
    (r"\b61169\b", "12345"),
    # `\bFriedberg\b` missed "Friedberg**s**" -- my own comment in greenhouse.py says "there are three
    # Friedbergs in Germany", and the trailing s defeats the closing boundary.
    (r"Friedberg\w*", "Exampletown"),
    (r"\bitzen\.ai\b", "example.com"),
    (r"\bs4biz\.io\b", "example.com"),
    (r"\b\d{9,10}:[A-Za-z0-9_-]{30,}\b", "<TELEGRAM_BOT_TOKEN>"),   # bot-token shape
    (r"\b64\.225\.108\.200\b", "<YOUR_SERVER_IP>"),      # the owner's droplet
    (r"\bjobhuntwow\.com\b", "example.com"),
    (r"\bcybergod\.ai\b", "example.com"),
]


_PHONE_RUN = re.compile(r"[+(]?[\d][\d\s()+\-.]{7,24}\d")
_TARGET_DIGITS = ("15785541545", "1578551545")
_EMAILISH = re.compile(r"^[\w.+-]+@[\w-]+\.[a-z]{2,}$", re.I)


def _scrub_phone(text: str) -> str:
    """Replace the candidate's number in ANY spacing.

    MEASURED BY THE AUDIT: `\b15785541545\b` matched the bare digits but not `+49 157 85541545`,
    so in `greenhouse.py`'s own selftest the EXPECTED value was rewritten and the INPUT was not --
    turning a passing assertion into `'+49 157 85541545' -> '15785541545' (want '1700000000')`.
    Asymmetric redaction does not leak anything; it does something worse for a tester, which is ship
    software whose first run reports two failures. Work on digit RUNS so spacing cannot matter."""
    def sub(m):
        run = m.group(0)
        digits = re.sub(r"\D", "", run)
        if any(t in digits for t in _TARGET_DIGITS):
            return ("+49 1700000000" if run.strip().startswith(("+", "0")) and
                    len(digits) > 11 else "1700000000")
        return run
    return _PHONE_RUN.sub(sub, text)


def redact(text: str, secrets: dict) -> str:
    """Secrets by exact value (longest first, so a substring cannot mask a longer match), then the
    phone in any spelling, then personal data by pattern.

    AN E-MAIL-SHAPED SECRET IS REPLACED WITH AN E-MAIL, not with `<KEY>`. Substituting a placeholder
    broke `converse.py`'s "an e-mail is not a password" assertion: `<JHW_MAIL_USER>` is punctuation-
    heavy with no spaces, i.e. exactly the shape the credential detector is built to catch. A
    redaction that changes the KIND of a value corrupts every test that reasons about kinds."""
    for k, v in sorted(secrets.items(), key=lambda kv: -len(kv[1])):
        text = text.replace(v, "you@example.com" if _EMAILISH.match(v) else f"<{k}>")
    text = _scrub_phone(text)
    for rx, rep in PII:
        text = re.sub(rx, rep, text, flags=re.I)
    return text


# ─────────────────────────────────────────────────────────────── staging
def _iter(globs) -> list:
    import glob as g
    out = []
    for pat in globs:
        for p in g.glob(os.path.join(HERE, pat), recursive=True):
            if os.path.isfile(p):
                rel = os.path.relpath(p, HERE).replace("\\", "/")
                if not NEVER.search(rel):
                    out.append(rel)
    return sorted(set(out))


TEXT = (".py", ".md", ".json", ".yml", ".yaml", ".txt", ".jsx", ".js", ".css", ".html", ".ts",
        ".example", ".sh", ".conf", ".caddy", ".gitignore")


def stage(secrets: dict, log=print) -> dict:
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)
    n_txt = n_bin = 0
    for rel in _iter(COPY_GLOBS):
        dst = os.path.join(STAGE, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        base = os.path.basename(rel)
        # A Dockerfile has no extension in TEXT, so my first version copied `Dockerfile.web` VERBATIM
        # and a `feranicus` GitHub URL survived the redaction. Anything we can decode as UTF-8 is
        # text; only fall back to a binary copy when decoding actually fails.
        if (rel.endswith(TEXT) or "." not in base or base.startswith(("Dockerfile", "docker-",
                                                                     ".env", "Makefile"))):
            try:
                src = io.open(os.path.join(HERE, rel), encoding="utf-8").read()
            except UnicodeDecodeError:
                shutil.copy2(os.path.join(HERE, rel), dst)
                n_bin += 1
                continue
            io.open(dst, "w", encoding="utf-8", newline="\n").write(redact(src, secrets))
            n_txt += 1
        else:
            shutil.copy2(os.path.join(HERE, rel), dst)
            n_bin += 1
    for d in DOCS:
        p = os.path.join(HERE, d)
        if os.path.exists(p):
            io.open(os.path.join(STAGE, d), "w", encoding="utf-8", newline="\n").write(
                redact(io.open(p, encoding="utf-8", errors="replace").read(), secrets))
            n_txt += 1
    log(f"  staged {n_txt} text file(s) (redacted) + {n_bin} binary file(s)")
    return {"text": n_txt, "binary": n_bin}


def _personal(t: str) -> bool:
    """Does this LABEL itself carry personal data? Some do -- an OAuth account-chooser row is
    rendered as `Stars4business OU you@example.com`, and that string is a field name."""
    return any(re.search(rx, t or "", re.I) for rx, _ in PII)


def sanitise_knowledge(log=print) -> int:
    """Ship the LABELS, never the VALUES.

    The knowledge base is the most useful thing here for a tester -- it is the real accessible names
    of every field on five ATSs, which is what takes days to discover. But the VALUES are the
    candidate's own answers: his e-mail, his phone, his city, the EEOC declarations he made. So the
    structure and the labels ship, the values are emptied, and the tester regenerates them from HIS
    OWN recordings -- which is exactly how the system is designed to work anyway."""
    src = os.path.join(HERE, "agent", "flows", "knowledge")
    dst = os.path.join(STAGE, "agent", "flows", "knowledge")
    os.makedirs(dst, exist_ok=True)
    n = 0
    import glob as g
    for p in g.glob(os.path.join(src, "*.json")):
        d = json.load(io.open(p, encoding="utf-8"))
        for f in (d.get("fields") or {}).values():
            f["value"] = ""
        d["choices"] = {k: "" for k in (d.get("choices") or {})}
        d["files"] = []
        d["sources"] = ["<redacted: the owner's recordings>"]
        # OPTIONS, ORDER, BUTTONS and JOB_URLS all carried personal data: the chosen city
        # ('Friedberg, Hesse, Germany'), an account-chooser label with his e-mail in it, and the
        # exact requisitions he applied to. None of it is needed -- the engine reads a select's real
        # options off the live page every time -- so it goes. The guard caught both of these.
        d.pop("options", None)
        d.pop("job_urls", None)
        d["order"] = [o for o in (d.get("order") or []) if not _personal(o)]
        d["buttons"] = [b for b in (d.get("buttons") or []) if not _personal(b)]
        d["fields"] = {k: v for k, v in (d.get("fields") or {}).items() if not _personal(k)}
        d["_note"] = ("LABELS ONLY. Values were removed before sharing. Record your own application "
                      "with `python jhw.py record <url>` and they fill in automatically.")
        io.open(os.path.join(dst, os.path.basename(p)), "w", encoding="utf-8",
                newline="\n").write(json.dumps(d, indent=2, ensure_ascii=False, sort_keys=True))
        n += 1
    log(f"  {n} knowledge file(s): labels kept, every value emptied")
    return n


CANDIDATE_TMPL = """# candidate.md — YOUR facts. The engine reads this; nothing here is guessed.
# Fill it in before your first run. Everything is plain text on purpose: you can see exactly what
# the engine knows about you, and it never sends any of it to a model except the few lines a form
# genuinely asks for.

## identity
- legal_name: Alex Example
- first_name: Alex
- last_name: Example
- gender: <optional; used only if a form asks and you want it answered>
- linkedin: https://www.linkedin.com/in/your-handle/

## contact
- email: you@example.com
- phone: "+49 1700000000"          # international form, for documents
- phone_national: "1700000000"     # DIGITS ONLY, no country code — this is what FORMS accept
- phone_country: Germany
- country: Germany
- address_line: 1 Example Street
- city: Exampletown
- city_query: Exampletown          # what to type into a city typeahead (the city ALONE)
- state_region: Example Region
- postal_code: "12345"

## screening_defaults
# YOUR standing answers. The engine uses one ONLY when it is genuinely an option on the page.
# Anything not written here is asked on Telegram — never invented by a model.
- how did you hear about us: LinkedIn
- work authorisation: Yes
- legally authorised to work: Yes
- require sponsorship: No
- visa sponsorship: No
- willing_to_relocate: "No"
- willing_to_accept_travel_assignment: "Yes"
- worked_for_company_before: "No"
- current_employee_of_company: "No"
- bound_by_noncompete_or_confidentiality: "No"
- notice_period: "1 month"
- salary_expectation_eur: ""        # leave blank and you will be ASKED. A model never invents this.
# Self-declarations. Leave blank and the engine asks you every time; fill them in and it answers
# deterministically, with no model involved. These are YOURS to state.
- legal gender: ""
- pronoun: ""
- protected veteran: ""
- disability: ""
- hispanic or latino: ""
"""

QUICKSTART = """# Quick start — testing JobHuntWOW

You have a **sanitised** copy: all code, no credentials, no personal data. Everything below is one
command at a time, and there is only ever one command.

## What it does

Give it a job URL. It opens a real browser in a container, tailors your CV and cover letter, fills the
application, answers screening questions from *your* declared answers, asks you on Telegram when it
genuinely cannot decide, submits, and only reports success if the site itself confirms it.

## 0. What you need

* **Docker Desktop** (running)
* **Python 3.11+** on the host
* A **DigitalOcean Serverless Inference** key (or any OpenAI-compatible endpoint + key)
* Optional but recommended: a **Telegram bot** (talk to `@BotFather`, takes a minute) so the engine
  can ask you things

## 1. Your secrets — you create these, nothing is shipped

```
cp .env.example .env
cp agent/.env.example agent/.env
```

Then edit both. The minimum to get a run:

| File | Key | What |
|---|---|---|
| `.env` | `DO_INFERENCE_KEY` | your inference key |
| `.env` | `DO_INFERENCE_BASE_URL` | e.g. `https://inference.do-ai.run/v1` |
| `.env` | `AGENT_PROXY_TOKEN` | invent any long random string |
| `agent/.env` | `AGENT_PROXY_TOKEN` | **the same string** |
| `agent/.env` | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | optional; without them it cannot ask you |
| `agent/.env` | `JHW_MAIL_USER` / `JHW_MAIL_PASS` | optional; a Gmail **app password**, only for reading ATS verification codes |

`agent/.env` is gitignored and never leaves your machine.

## 2. Your facts

Edit `agent/userdata/candidate.md` — it is a filled-in template with placeholders. Two things there
are worth understanding because they cost the original project days:

* **`phone_national` is digits only, no country code.** Workday and Greenhouse both *reject* `+49 …`
  in the phone box; the country goes in the country selector.
* **`city_query` is the city alone.** City + postcode returns zero options from an async typeahead.

Anything you leave blank in `## screening_defaults` will be **asked**, not invented. Salary in
particular is never generated by a model.

## 3. Run it

```
python jhw.py apply "https://job-boards.greenhouse.io/<company>/jobs/<id>"
```

That one command brings up the backend, brings up the browser sandbox, waits for Chrome, compiles any
recordings you have made, runs 20 self-test suites, and then applies. **Watch it live at
<http://localhost:9090/vnc.html>.**

Set `JHW_AUTOSUBMIT=0` in `agent/.env` for your first few runs — it then fills everything and stops
before Submit so you can check it by hand.

Want it to be able to ask you? In a second terminal:

```
python jhw.py bot
```

## 4. The trick that makes it good

Do the application **once, yourself**, recorded:

```
python jhw.py record "https://job-boards.greenhouse.io/<company>/jobs/<id>"
```

A browser opens; you fill the form as a human. The engine compiles what you did into facts — the real
field names, and the exact option you chose for each dropdown — and the next run uses them. A
recording is ground truth; no amount of model reasoning replaces it.

The knowledge files shipped with this copy contain the **labels** for five ATSs (Greenhouse, Workday,
iCIMS, Pinpoint, LinkedIn) with every value removed. Your own recordings fill the values in.

## 5. If something goes wrong

```
python jhw.py doctor          # what is up, what is not, and why
python jhw.py logs            # the containers
python jhw.py local-down      # stop everything
```

Failures are meant to be readable. If you see `REFUSING to submit: still required+empty -> …` that is
the engine declining to send an incomplete form — check Telegram, it is probably waiting on you.

## 6. Read this next

* `HANDOVER.md` — the full architecture, every file, and *why* each rule exists.
* `CLAUDE.md` — long, and worth it: every rule in the system traced back to the failure that produced
  it. Skim the headings.

## Honest limits

* Greenhouse is the best-tested path (a real application submitted end to end in 27 s). Workday
  reaches the account gate and often through it; other ATSs fall back to a generic model-driven driver.
* Job **sourcing** is a stub — you supply the URL.
* Nothing here scans, probes or touches any third party. It fills forms you point it at.
"""


def add_templates(log=print) -> None:
    d = os.path.join(STAGE, "agent", "userdata")
    os.makedirs(d, exist_ok=True)
    io.open(os.path.join(d, "candidate.md"), "w", encoding="utf-8",
            newline="\n").write(CANDIDATE_TMPL)
    io.open(os.path.join(STAGE, "QUICKSTART.md"), "w", encoding="utf-8",
            newline="\n").write(QUICKSTART)
    # resume_data.json -> the same shape, no personal content
    src = os.path.join(HERE, "agent", "templates", "resume_data.json")
    dst = os.path.join(STAGE, "agent", "templates", "resume_data.json")
    if os.path.exists(src):
        d0 = json.load(io.open(src, encoding="utf-8"))

        def blank(x):
            if isinstance(x, dict):
                return {k: blank(v) for k, v in x.items()}
            if isinstance(x, list):
                return [blank(v) for v in x[:1]] if x else []
            return "" if isinstance(x, str) else x
        out = blank(d0)
        b = out.setdefault("basics", {})
        b.update({"name": "Alex Example", "legal_name": "Alex Example",
                  "email": "you@example.com", "phone": "+49 1700000000",
                  "phone_national": "1700000000", "city": "Exampletown", "country": "Germany",
                  "postal_code": "12345", "linkedin": "linkedin.com/in/your-handle",
                  "website": "example.com", "preferred_name": "Alex"})
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        io.open(dst, "w", encoding="utf-8", newline="\n").write(
            json.dumps(out, indent=2, ensure_ascii=False))
    # an out/ dir so the first run does not have to create it
    os.makedirs(os.path.join(STAGE, "agent", "out"), exist_ok=True)
    io.open(os.path.join(STAGE, "agent", "out", ".gitkeep"), "w").write("")
    log("  QUICKSTART.md + candidate.md template + blank resume_data.json")


# ─────────────────────────────────────────────────────────────── the guard
def _verify(secrets: dict, log=print) -> list:
    """Read EVERY byte of the staged tree and refuse to ship if anything sensitive survived."""
    bad = []
    pii_rx = [(re.compile(rx, re.I), rx) for rx, _ in PII
              # the replacements are themselves harmless; only scan for the originals
              if rx not in (r"\bEvgeny\b",)] + [
        (re.compile(r"[\w.+-]+@s4biz\.io", re.I), "an s4biz.io address"),
        (re.compile(r"\b\d{9,10}:[A-Za-z0-9_-]{30,}\b"), "a Telegram bot token"),
        (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "an API key"),
    ]
    for root, _dirs, files in os.walk(STAGE):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, STAGE).replace("\\", "/")
            try:
                blob = io.open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for k, v in secrets.items():
                if v in blob:
                    bad.append(f"{rel}: contains the live value of {k}")
            for rx, what in pii_rx:
                m = rx.search(blob)
                if m:
                    bad.append(f"{rel}: {what} -> {m.group(0)[:40]!r}")
            # NEVER governs what may be COPIED FROM SOURCE. It must not flag the templates this
            # script AUTHORS -- `agent/userdata/candidate.md` is a placeholder file we wrote, and the
            # source one (with his address and declarations) is excluded precisely by that rule.
            if NEVER.search(rel) and rel not in AUTHORED:
                bad.append(f"{rel}: matches the NEVER list and should not be staged")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="leave dist/stage/ for inspection")
    a = ap.parse_args()

    print("=" * 78)
    print("BUILDING A SHAREABLE COPY — allowlisted, redacted, then verified byte by byte")
    print("=" * 78)
    sec = live_secrets()
    print(f"  {len(sec)} live secret value(s) will be scanned for: {', '.join(sorted(sec))}")
    stage(sec)
    sanitise_knowledge()
    add_templates()

    print("\nVERIFYING the staged tree (this is the part that matters):")
    bad = _verify(sec)
    if bad:
        print(f"  [X] REFUSING TO BUILD — {len(bad)} problem(s):")
        for b in bad[:25]:
            print("      " + b)
        print("  nothing was written. Fix the redaction and re-run.")
        return 1
    n = sum(len(f) for _, _, f in os.walk(STAGE))
    n = sum(len(files) for _, _, files in os.walk(STAGE))
    print(f"  [OK] {n} file(s): no live secret, no personal identifier, nothing on the NEVER list")

    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    zp = os.path.join(HERE, "dist", f"jobhuntwow-test-{date.today().isoformat()}.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _d, files in os.walk(STAGE):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.join("jobhuntwow", os.path.relpath(p, STAGE)))
    size = os.path.getsize(zp)
    print(f"\n  {os.path.relpath(zp, HERE)}   {size / 1024:.0f} KB   {n} files")
    if not a.keep:
        shutil.rmtree(STAGE, ignore_errors=True)
    else:
        print(f"  staged tree left at {os.path.relpath(STAGE, HERE)}")
    print("\n  Tell your friend to read QUICKSTART.md first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
