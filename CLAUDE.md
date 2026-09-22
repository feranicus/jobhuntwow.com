# JobHuntWOW app — project conventions for Claude / AI agents

## READ THIS FIRST — where the rest of this file went (2026-09-18)

This file used to be **219 KB / 2,719 lines / 126 sections**. It is re-injected into the model's
context on EVERY turn, so it was spending ~55,000 tokens before the operator typed anything — which
is exactly what makes a long session run out of room and start losing the thread.

Nothing was deleted. The full narrative — every incident, root cause, measured number and fixture,
verbatim — is at:

    docs/decisions/HISTORY-2026-07-to-2026-09.md

**This file now carries only what must be true on EVERY turn:** the standing rules, the settled
facts, and the defect classes as one-line rules. The history is the EVIDENCE for those rules and is
read on demand, not on every turn.

**HOW TO USE THE HISTORY.** When a rule below matters and you need the story, `grep` the history file
for a distinctive phrase from the rule. Every rule here was earned by an incident recorded there.

**HOW TO ADD TO IT.** A new incident goes in `docs/decisions/` in the same voice and detail, and
earns **at most one line here** — only if it must be obeyed on every turn. If a section here grows
past a few lines, the story belongs in the history file and the rule belongs here.
`tests/test_claude_md_size.py` FAILS THE BUILD when this file passes its budget, because a rule that
is only written down goes stale — and this one already did.

## What this project is
The interactive JobHuntWOW cabinet: a **React (Vite)** front end + **FastAPI** back end. Hermes chat
runs on **real Qwen** via **DigitalOcean Serverless Inference** (OpenAI-compatible). Job Scout and the
ATS Apply-Driver are wired as endpoints; the heavy agent logic plugs in behind them.

This is a SEPARATE project from the Colt cyber pre-sales monorepo it was extracted from. It also has a
sibling repo, `jobhuntwow.com` (GitHub-Pages landing + Google-Apps-Script Gmail→Sheets tracker).
Do NOT reintroduce any Colt / Shodan / cybergod code here.

## Standing rules
1. **KISS.** Zero manual data entry is the product promise. Prefer automation + sane defaults over knobs.
2. **Secrets never in git.** `DO_INFERENCE_KEY` and any tokens live ONLY in `.env` (gitignored) or as
   deploy secrets. `.env.example` carries placeholders only.
3. **One input where possible.** The user gives intent (a chat message, a job query); the app resolves
   the rest.
4. **The LLM assists, it does not decide side effects.** Qwen writes text; deterministic code decides
   what gets stored, applied, or sent.
5. **Frontend renders safely.** Never render an unknown backend value straight into JSX — coerce to text
   (objects → readable string) so a shape change can't white-screen the cabinet.
6. **Deliver operations as scripts + document — NO command blobs.** Never hand the user long ad-hoc
   shell/heredoc command sequences to paste ("talmud commands"). Every operational step (build, run,
   deploy, diagnose, fix) must be a re-runnable **Python script** committed to the repo, invoked as
   `python <script> ...`. Whenever anything changes (deps, Dockerfile, flags, config, architecture),
   update the relevant **README.md** in the SAME change so nothing is undocumented. KISS + full
   automation, always. (Applies to every project — jobhuntwow-app, Linkedin Scraper, oxford-*.)

## How it runs
- **Local:** `docker compose up --build` → app on `:8090`, backend proxied at `/api`.
- **Prod:** Docker on the droplet; the shared Caddy serves `app.jobhuntwow.com` → `frontend:80`; the
  frontend nginx proxies `/api` → `backend:8000`. See DEPLOY.md.

## API surface (keep stable)
`GET /api/health` · `GET /api/models` · `GET|POST /api/connections` · `POST /api/chat` (SSE stream) ·
`POST /api/scout` · `POST /api/apply`.

## Conventions
- Backend: Python 3.11+, FastAPI, `httpx` for the DO inference calls, JSON file store under `DATA_DIR`.
- Frontend: React + Vite, plain `fetch` in `src/api.js` with `credentials:'include'`.
- Adding a scout source = fill `backend/app/scout.py::search()`; keep the `{query,count,jobs,note}` shape.
- Adding an apply target = fill `/api/apply`. **The candidate has explicitly authorised end-to-end submission** (2026-07-21: "I already validated everything no need to bother the stakeholder again. please make it fully automated till the end"), so the driver clicks Submit itself. `JHW_AUTOSUBMIT=0` restores the stop-at-Review behaviour.

## What NOT to do
- No secrets in git, no Colt/Shodan code, no breaking the `/api/*` contract the frontend depends on.
- Don't couple to the droplet's videodead stack beyond the one Caddy vhost + shared network.

## STANDING RULE — containers: Docker + Google best practices, Secure by Design, KISS
Applies to EVERY containerized app in every project (jobhuntwow-app, Linkedin Scraper, oxford-*).
Implement these by default; never ship a container without them.

**Secure by Design (default-deny, least privilege)**
1. **Never publish a port on 0.0.0.0 unless the internet must reach it.** Bind local-only services to
   `127.0.0.1:PORT:PORT`. A published Chrome CDP (9222) = full remote browser control, no auth: treat
   it as a critical finding. Same for VNC (5900), noVNC (9090), and dev backends (8000).
2. **Least privilege:** `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]` (add back only what
   is proven necessary), non-root `USER` wherever the process allows it, read-only rootfs when feasible.
3. **Resource limits** on every service (`mem_limit`, `cpus`) — an unbounded browser/LLM loop must not
   take the host down.
4. **Secrets never in the image or git.** Runtime env only (`.env`, gitignored). Never `COPY .env`.
   Never bake tokens into layers. Never log a secret.
5. **Healthchecks** on every long-running service, so orchestration knows what "ready" means.

**DevSecOps (shift left) — `python jhw.py scan`**
6. **Trivy** (Aqua Security's OSS scanner) is the standard: image CVEs + filesystem/dependency CVEs +
   **secret scanning** + Dockerfile misconfiguration (IaC). Run it locally before deploy and in CI.
   Gate on HIGH/CRITICAL. Runs as a container — nothing to install.

**Observability — `python jhw.py obs`**
7. **Structured JSON events -> file -> Promtail -> Loki -> Grafana** (the same pattern as the colt-stack
   sibling project). Every service emits one JSON object per line with `ts, service, evt, ...`. No
   secrets/PII in events. Observability is profile-gated so it never slows the core flow.

**KISS (this overrides gold-plating)**
8. Prefer the smallest thing that satisfies the above: one `jhw.py` command per operation, no k8s, no
   service mesh, no agent sidecars. If a control adds a moving part without removing a real risk, skip
   it and write down why.

## HARD RULE — ONE command. Never hand the user a list of commands to run.
`python jhw.py <verb>` is THE entry point and it does EVERYTHING that verb needs, itself: bring up the
backend + proxy, bring up the sandbox, wait until Chrome's CDP actually answers, then do the work.
- **Never** tell the user to run 2-3 pythons, or a `docker compose ...` line, to make a verb work. If a
  verb needs a prerequisite, the verb does it. If a prerequisite can break (stale Chrome profile lock,
  container not recreated after an .env change), the verb detects and repairs it — that is what
  `jhw.py doctor` exists for, and `apply` should self-heal rather than ask the human.
- Optional/side operations (`scan`, `obs`) are their OWN verbs and are NEVER prerequisites of a run.
  Do not print them next to the run command as if they were steps.
- Applies to every project (jobhuntwow-app, Linkedin Scraper, oxford-*): one orchestrator, one verb,
  zero manual steps. This is the same KISS + full-automation rule as "no talmud command blobs".

## HARD RULE — write files with bash heredoc, then MACHINE-VERIFY. Never trust the file editor.
The Edit/Write file tools have repeatedly, silently CORRUPTED files in this repo: null bytes appended
(agent.py 757, flows/workday.py 2604 — Python cannot import a file containing null bytes), and hard
TRUNCATION mid-line (jhw.py cut at the same line twice, ask.py lost ask_human(), both docker-compose.yml
files cut mid-volume producing `- ../Pr`). Every one of these was shipped to the user as "fixed".
Therefore, for THIS repo:
1. **Author/repair files with `cat > file <<'EOF' ... EOF` in bash.** Bash writes have never corrupted.
   For surgical edits use a python heredoc (read -> replace -> write) run INSIDE bash.
2. **ALWAYS machine-verify before saying anything works** — and show the check:
   - Python:  `python3 -m py_compile f`  AND  `tr -cd '\000' < f | wc -c` must be 0
   - YAML:    `python3 -c "import yaml;yaml.safe_load(open('f'))"` + assert the structure
              (every volume contains ':', every port starts with 127.0.0.1, expected services present)
   - Shell:   `bash -n f`
   - JSON:    `python3 -c "import json;json.load(open('f'))"`
3. **Never report a fix from intent.** If the verification output isn't in front of you, the fix does
   not exist. A truncated file that "looks right" in the editor is the default failure mode here.

## HARD RULE — VERIFY EVERYTHING. Never assume. Never claim. Prove it, then speak.
This is the most-violated rule in this project and the root cause of most wasted hours. Applies to
EVERY project (jobhuntwow-app, Linkedin Scraper, oxford-*).

**The rule:** a statement to the user is only allowed if the evidence for it is on screen in this
session. "Should work", "that's fixed", "now it will", "I added X" — all forbidden unless a command's
output proves it. If it wasn't verified, say "not verified" out loud.

**Verify before claiming, always:**
1. **Files** — after EVERY write: `py_compile` / `yaml.safe_load` + structure asserts / `bash -n` /
   `json.load`, AND `tr -cd '\000' < f | wc -c` == 0, AND check the LAST line isn't truncated. The
   editor has silently truncated and null-padded files repeatedly in this repo (see the bash-heredoc
   rule). A file that "looks right" is not evidence.
2. **Code paths** — before saying a feature is wired, `grep` for the actual call site and show it.
   (e.g. claimed obs was wired -> only proved by `grep -c obs.event agent.py`.)
3. **Config** — before saying "ports are local-only" / "the volume is mounted", PARSE the file and
   assert it. Do not read it by eye.
4. **Root causes** — get the real error first (`jhw.py doctor`, `jhw.py logs`, the DOM dump). Do not
   theorise a cause and act on it. Chrome exit 21 was diagnosed only after reading the logs; every
   guess before that wasted a cycle.
5. **Selectors / external systems** — never invent a selector, model slug, API field, or automation-id
   from memory. Get ground truth (`jhw.py inspect` -> out/dom_dump.json, `jhw.py models`, the real
   repo's source) and cite where it came from. Guessed Workday selectors cost hours; the DOM dump
   settled it in one run.
6. **Before telling the user to run something** — re-verify the exact command exists and the files it
   touches compile/parse. Do not hand over a command you have not just proven is valid.

**When something is uncertain, say so plainly** ("I could not verify X; here is how we find out")
instead of asserting it confidently. Being wrong loudly is worse than being unsure honestly.

## LOCAL HYBRID STACK — ONE command (remember; do NOT hand the user multi-step sequences)
The isolated local stack (Chromium+noVNC browser container + Hermes agent container, brains->DO) is
brought up by the SINGLE command `python jhw.py local`. That verb does everything itself: `docker
compose down` the old single-container sandbox to FREE ports 9090/9222/5900 (the "port is already
allocated" failure), start the DO-proxy backend, then `docker compose -f docker-compose.local.yml up
-d --build`, and wait for noVNC. Stop with `python jhw.py local-down`. It reuses the EXISTING
`agent/.env` (env_file) — never create a second env file, never ask the user to re-enter keys.
NEVER tell the user to run `python jhw.py backend` + a `docker compose ...` line separately — the verb
does both. This is the same HARD RULE as "ONE command, no talmud command blobs".

## HARDEST RULE — NEVER use the Edit/Write file tools in this repo. Bash-heredoc ONLY. (memorized)
The Edit/Write tools have truncated or null-padded files REPEATEDLY here: agent.py, workday.py,
jhw.py (main() tail lost, twice), ask.py, both docker-compose.yml, workday_gevernova.py, and
backend/app/proxy.py (gen() cut mid-comment). Every one shipped as "fixed". The bash heredoc path has
NEVER corrupted a file. Therefore, with ZERO exceptions in this project:

1. **Do NOT call the Edit tool. Do NOT call the Write tool.** For ANY create or change, write the file
   with a bash heredoc (`cat > f <<'EOF' ... EOF`) or, for surgical edits, a `python3 - <<'PY'` block
   that reads → replaces (with an `assert` that the anchor was found and the replacement happened) →
   writes. Prefer rewriting the WHOLE file when it's small.
2. **MACHINE-VERIFY every write, in the same bash call, and show the output:**
   - Python: `python3 -m py_compile f` AND `tail -3 f` (last line intact) AND
     `tr -cd '\000' < f | wc -c` == 0 AND, for files with load-bearing functions, an `ast.parse`
     assert listing the expected defs.
   - YAML: `python3 -c "import yaml,sys; d=yaml.safe_load(open('f'))"` + structure asserts.
   - Shell: `bash -n f`.   JSON: `python3 -c "import json;json.load(open('f'))"`.
3. **If any check fails, REWRITE the whole file via heredoc — never try to patch a corrupted file.**
4. **A file that "looks right" in the editor is not evidence.** Only the verification output counts.
   If the verification isn't in front of me, the fix does not exist.
This supersedes any convenience of the editor. No exceptions, ever, in this repo.

## SETTLED — deploy is DIRECT from the PC. GitHub is NOT in the deploy path. (do not re-litigate)
**`python ship.py`** (engine: `deploy_direct.py`) = tarball the build context over ONE ssh session
to /opt/jobhuntwow -> **the droplet builds the image itself**
(`docker compose -p jobhuntwow -f docker-compose.web.yml build`) -> up -d --force-recreate -> wire
the marker-scoped Caddy block -> verify with a tagged probe. There is NO `gh workflow run`, NO GHCR
pull, NO CI wait. Reason: the GitHub round-trip added a 2-minute stall and a second source of truth
for zero benefit — this PC can already SSH the droplet.
**IT NEEDS NO DOCKER ON THIS MACHINE**: every docker command travels inside the ssh payload, which
`tests/test_gate_integrity.py` asserts. Never put a registry or a CI wait back into the deploy, and
never make the website depend on the local apply sandbox.
(`jhw.py deploy` and `jhw.py push` HAVE NEVER EXISTED — this section named both for weeks. `jhw.py`
drives the local apply sandbox; the website is `ship.py`.)

## HARD RULE — the engine must never ASK for a fact it already owns
`candidate_block()` was built ONLY from `templates/resume_data.json`, so the entire
`## screening_defaults` section of `userdata/candidate.md` (travel=Yes, relocate=No, worked-here=No,
notice=1 month, salary=150000) never reached the form driver. The agent therefore asked the human
on Telegram for answers that were sitting on disk, and invented `01-03-2025` as a start date.
`agent.py::_screening_block()` now appends those defaults verbatim to the facts and RECOMPUTES
`available_start_date` as today+30d on every run (the literal date in the file goes stale and was
already two months old). If a value is in candidate.md, asking for it is a defect.

## STANDING ORDER — never stop a run on a stall; sweep, then ASK on Telegram
"do not stop the process if the system is stuck with something even stupid things let it ask me in
telegram". So when the repeat-breaker trips:
1. `SWEEP_JS` first answers EVERY visible `<select>` still on its placeholder — `SELECT_DEFAULTS`
   (relocate=No, travel=Yes, worked-here=No, current-employee=No, noncompete=No, visa=No,
   how-did-you-hear=Internet) mirrors `## screening_defaults`; anything unlisted takes the FIRST
   real option, because "yes or no, it doesn't really matter" beats a blocked application.
2. Only then `ask.ask_human()` on Telegram with the exact refused action + the page's own errors.
   The reply resets that action's counter and the run CONTINUES. It exits only if the human replies
   `stop`, or if there is no Telegram channel at all.
A date fill now presses Escape afterwards: the calendar overlay stayed open and the model clicked a
day at random, which is how `20-08-2026` became `28/07/2026` on screen.

## STANDING RULE — BUILD IT. Do not tell the operator what you are not doing. (2026-08-16, memorized)
His words: *"stop deflecting, memorize do parallelism jobs and multi agents if need to be, stop
telling me you're not doing."* I ended a reply with "I'm not building that now" about work he had
just asked for. That is a refusal dressed as prudence and it wastes a whole round trip.
1. If he asks for something and it is buildable, BUILD IT in the same turn. State the risk in one
   line if there is one; do not substitute the risk for the work.
2. **USE SUBAGENTS FOR INDEPENDENT WORK, IN PARALLEL.** A read-only audit of the whole `flows/` tree
   runs perfectly well alongside a feature build. The first time I did this it returned 17 defect
   classes in one pass, including THREE that broke guards I had written that same day.
3. Never close a turn with a plan. Close it with a verified change and the one command.

---

# SETTLED FACTS — measured, do not re-derive

| Fact | Value |
|---|---|
| Two products, two commands | **the WEBSITE** (jobhuntwow.com · /tailor · /pipeline) ships with `python ship.py` and needs **no Docker on this PC** — the droplet builds it over ONE ssh session. **The LOCAL apply sandbox** (Chromium + noVNC driving Workday/Ashby/Greenhouse) is `python jhw.py`, and that one needs Docker Desktop. Never let one demand the other's prerequisites. |
| ONE orchestrator | `agent/jhw.py`. The repo-root `jhw.py` is a LAUNCHER that forwards argv + exit code. A copy of the entry point is the same defect as a value with four homes, except it is the thing the operator types. |
| The answer ladder | **recorded human answer → learned answer → written rule / profile → 3-LLM panel → the human.** A model is the FOURTH thing consulted, never the first. |
| Recordings are ground truth | `playrecord/` codegen → `flows/recordings.py` → `flows/knowledge/<ats>.json`, compiled automatically inside `apply`. The raw recording holds his typed password and is NEVER committed. |
| The phone | the NATIONAL number, digits only: **15785541545**. `+49 …` is rejected by Greenhouse AND Workday. The country code belongs in the country selector. |
| Workday | phone = THREE controls (country code FIRST, then device, then digits, each read back) · asks with a BUTTON whose name carries its current value · the date box is three spin inputs · confirmation is `Job_Application_ID=` in the URL or a modal that says "Succes(s)fully" (the site's own typo). |
| Greenhouse | Submit is **`Submit application`** · uploads go through `Attach` inside the `Resume/CV*` GROUP · a react-select has no `<select>` and VIRTUALISES long lists (type to narrow) · `First Name` needs `exact=True`. |
| Ashby | one long page · upload → location typeahead → salary/notice → radios → essays → `Submit Application` · answers come from his recording first. |
| Models by ROLE alias | `backend/app/llm.py` is the one home. jhw-answer deepseek-3.2 · jhw-answer2 mistral-3-14B · jhw-answer3 llama-4-maverick (three vendors, quorum 2 of 3) · jhw-speak kimi-k2.6 speaks and never decides. A model id is an EXTERNAL fact: `verified_model_for()` checks the live catalog, fails open. |
| One store per fact | credentials `flows/atscreds.py` · learning `flows/memory.py` (nothing is learned until the SITE confirms) · applications `backend/app/tracker.py` · document names `backend/app/docnames.py`. |
| Submission is authorised | end-to-end, by the candidate (2026-07-21). `JHW_AUTOSUBMIT=0` restores stop-at-Review. |

---

# THE RECURRING DEFECT CLASSES — the checklist that actually prevents repeats

Each line cost at least one run, most of them several. Read this before writing a check, a fixture or
a diagnostic. `grep` the history file for the phrase if you need the incident.

## Writes, reads and evidence
1. **A write is not done until it has been READ BACK.** `fill`/`set_input_files`/`click` returning
   without throwing proves only that Playwright acted — not that the widget kept it.
2. **A click that throws no error is not a submission.** Success is claimed ONLY on the site's own
   confirmation (or an application id the site minted).
3. **Truncated evidence produces confident false failures.** Read the WHOLE body / the whole group
   text; `[:400]` once turned a successful upload into `COULD NOT ATTACH`.
4. **Absence of evidence is never a finding.** An empty page, an empty index and a broken read look
   identical; every completion check must be POSITIVE evidence.
5. **Blur commits where Escape reverts.** Never press Escape on a date field or over a modal.
6. **A checkbox is SET, never toggled** — a second click unticks it. Any action that toggles is
   unsafe under retry.

## Checks and tests
7. **A check that cannot RUN is not a check**, and a skip is not a pass ("backend not importable",
   "catalog unavailable" printed on every run = it has never executed).
8. **A check that cannot FAIL is unproven.** Mutate the code it guards, confirm it fails, restore.
9. **Assert the PROPERTY, never a call's spelling** — a grep for `has_text=re.compile(label_rx` broke
   the moment the upload improved, against code that demonstrably works.
10. **Strip comments AND docstrings before grepping source, and measure only the shipping slice** —
    a check has matched its own assertion line or its own explanatory comment ~19 times here.
11. **Mutate the SHIPPING SLICE ONLY.** Replacing a marker in the check as well makes the guard agree
    with itself.
12. **Report by EXIT CODE.** `| head -1` and `grep | echo` exit 0 on empty input and report a mutation
    that changed nothing as "caught".
13. **A check must be at least as strict as the tool that consumes the artefact** — `yaml.safe_load`
    accepts a duplicate key that `docker compose` refuses.
14. **A test that depends on the agent forgetting is a test against the product**, and no suite may
    read or write his real memory/pipeline — point `JHW_MEMORY` / `JHW_TRACKER_DB` at a temp dir.
15. **A test that encodes a doctrine gets REWRITTEN when the doctrine is corrected**, with the old
    reasoning kept beside it.
16. **A self-test must never render into the browser a human is watching**, and never into a context
    holding real credentials.
17. **Validate EVERY anchor BEFORE editing anything**, then write, then verify — and never patch a
    corrupted file, rewrite the region whole. An anchor's INDENTATION is part of the anchor.

## Selectors, the DOM and the panel
18. **A role-name regex is a PREFIX match and `.first` resolves ties by DOM ORDER.** Anchor it, and
    test the anchor against the strings that must NOT match (`sign in` matched "Sign in with Google"
    and took the driver off the ATS).
19. **A three-letter label must be matched EXACTLY**; prefix matching below 6 characters is refused.
20. **Scope option lists to the widget's own listbox** — a page-wide `[role=option]` clicked a
    checkbox label. A selector built from page data must be QUOTED (`[id="…"]`), never interpolated.
21. **Never surface a validation error without the field it belongs to**, and attribute per-field
    messages by GEOMETRY; a SUMMARY banner has no owner.
22. **A hidden field is not `display:none`** — bot traps hide with clip/clip-path/zero area, and
    `aria-hidden` is an accessibility hint, NOT a visibility fact.
23. **When two readers look at the same page and disagree, that disagreement IS the bug report.**
24. **First-wins blinds everything; UNION the readers** and let vision only ADD what the DOM hides.
25. **One reader, one attribute namespace.** Every reader writing `data-jhw-esc` with its own
    numbering means the index the panel saw and the element the executor addressed were different.
26. **The INDEX is the safety contract:** a model may only name something we already hold a handle
    to, so a hallucinated control is structurally unclickable. Same for a closed option list.
27. **Every action loop needs a repeat-breaker, scoped to the PAGE** — "same action" on a new page is
    progress, not a repeat.
28. **A guard that blocks the only useful verb costs more than the risk it covers**, and when a guard
    and a real control share a label, the label cannot be the test — ask the DOM what it IS.
29. **When a model keeps making the same choice, READ THE PROMPT before blaming the model.**
30. **Models advise; deterministic code decides side effects.** The panel may only name a verb from a
    closed set, quorum 2 of 3, fail open DOWNWARD to asking the human. It never sees a credential.
31. **A self-declaration (gender, veteran, disability, race) and a SALARY are never answered by a
    model** — only from what he wrote down, otherwise ask.

## Code, config and one home
32. **ONE HOME.** A value with several homes drifts and the stale one wins — ENRICH_MODELS had four,
    the ATS store had two key schemes, the github rule had four copies, the orchestrator had two.
33. **READ THE SIGNATURE.** ~20 times in this project a helper was called with the wrong shape, and a
    bare `except` made it invisible. Never `except Exception: pass` — log the type and message.
34. **PRESENCE IS NOT REACHABILITY.** `"await asker(" in src` is true even under `if False:`.
35. **A profile is USER-EDITED JSON; its shapes are not ours to assume** (`expertise` is a dict, and
    `dict[:18]` killed a run before the ask rung existed).
36. **candidate.md is written for a human** — strip the inline `#` comment and the quotes, in EVERY
    parser that reads it.
37. **Never `sed -i` a bind-mounted FILE** (it changes the inode); compare host vs container hashes.
38. **An unqualified Playwright action inherits a 30-SECOND default.** Cap the page AND the context.
39. **A bookkeeping step must never abort an application** — tracker writes, safepoints and digests
    are best-effort by construction.
40. **Windows:** every `subprocess` that captures output needs `encoding="utf-8", errors="replace"`;
    one ssh session per deploy; no POSIX-only APIs.

## Diagnostics
41. **Name what you SAW, not what you wanted.** "0 controls indexed" / "no Save-and-Continue button"
    is unactionable; print the url, the stepper, every visible button, every required-empty field.
42. **A diagnostic that names a nonexistent command is worse than silence** — resolve a printed
    `python jhw.py <verb>` against the file that PRINTS it (`flows/test_docker.py` §8 enforces this).
43. **A tail shows whatever was noisiest, not whatever failed.** Print the FAIL lines.
44. **A futile attempt that prints a scary line every run trains you to read past the log.**
45. **An environment problem is ONE SENTENCE with an exit code, never a traceback.**

---

# WHAT LIVES WHERE

- `agent/jhw.py` — the orchestrator (every verb, `_SELFTESTS`, the safepoint, the docker precondition).
- `agent/flows/` — the drivers: `ashby.py` · `greenhouse.py` · `workday.py` · `llm_driver.py` (generic)
  · readers `vision.py` `seers.py` `som.py` · ladder `recordings.py` `learned.py` `memory.py`
  `choose.py` `essay.py` `escalate.py` `speaker.py` `converse.py` · guards `presubmit.py` `conduct.py`
  `atscreds.py` `mailcode.py` · suites `test_*.py` (all registered in `_SELFTESTS`).
- `agent/gitops.py` — the safepoint and the guard that keeps credentials out of git.
- `backend/app/` — the website: `electronic.py` (Tailor: jd → generate → artifacts) ·
  `resume_consensus.py` · `documents.py` + `docnames.py` (what the files are CALLED) ·
  `tracker.py` (one row per application) + `digest.py` (the end-of-day mail) · `auth.py` · `llm.py` ·
  `proxy.py` · `notify.py` · `daily_report.py`.
- `frontend/src/pages/` — `Tailor.jsx` · `Pipeline.jsx` (reads `/api/applications`) · `Electronic.jsx`.
- `ship.py` — the website orchestrator (tests → commit+push → staging gate → deploy → verify →
  safepoint). `deploy_direct.py` is its engine: ONE ssh session, built on the droplet.
- `docs/decisions/HISTORY-2026-07-to-2026-09.md` — the evidence for every rule above.

---

---

# SEPTEMBER 2026 — the rules earned this month
*(the incidents, verbatim, are in `docs/decisions/2026-09.md`)*

- **TWO PRODUCTS, TWO COMMANDS.** The WEBSITE is `python ship.py` and needs no Docker on his PC; the
  LOCAL APPLY SANDBOX is `python jhw.py` and needs Docker Desktop. **Name the product before naming
  the command** — a reply ending in the wrong one cost him a ten-minute Ubuntu pull.
  `deploy_direct.py` may never import anything from `agent/jhw.py`; `JHW_NO_DOCKER_AUTOSTART=1`
  stops the sandbox verbs from launching Docker Desktop.
- **The docs must name commands that exist.** `tests/test_claude_md_size.py` reads `agent/jhw.py`'s
  real verb list and checks every `python jhw.py <verb>` and `python <script>.py` in the
  operator-facing docs — resolved against the FILE THAT PRINTS IT. Five named commands had never
  existed, one of them in this file.
- **This file has a size budget (40 KB) and it is enforced.** A rule that is only written down goes
  stale; the history lives in `docs/decisions/`.
- **The board is the truth or it is nothing.** Drag-and-drop is optimistic and REVERTS on refusal;
  `preventDefault` on `dragOver` is the line whose absence makes drag look implemented and do
  nothing; a click must never be a drag (`didDrag`).
- **A derived fact is labelled as derived.** The employer ladder is JD text → model (accepted ONLY
  if the name appears VERBATIM in the posting) → posting URL → nothing, recorded in
  `company_source`. None of it reaches the resume or the cover letter.
- **A filename is read at the moment it is attached.** `resume_<employer>_<role>[_N].pdf`, numbered
  from what is on disk, never from a counter.
- **The lifecycle is his, not mine:** tailored → applied → hr_screen → tech → task → manager → final
  → offer → negotiation → signed (rejected from any). `canon_stage()` maps every legacy name so the
  apply engine keeps working; `_migrate()` moves old rows once; the board must render EVERY stage the
  store allows.
- **A hostname we SERVE must be one the deploy CLAIMS** (`fix_caddy.OURS`), and every `redir` lands
  on the canonical host in ONE hop. `python domains.py` prints the registrar records and measures —
  and refuses to report a finding when it cannot resolve DNS at all.

## THE WALLET, THE CONSOLE AND THE SHORT DOMAIN (2026-09-21)
*(the full stack and its boundaries: `SECURITY.md`; the incident that earned it: the LLM-jacking report)*

- **A paid model call is GATED BEFORE IT IS MADE, at all four chokepoints** (`llm.chat`,
  `llm.complete`, `qwen.chat_stream`, `proxy.chat_completions`). `backend/app/llm_meter.py` holds
  four rules — global daily USD, per-account daily USD, per-account calls/hour, service calls/hour —
  and **fails OPEN on a storage fault, CLOSED on the budget**. `None` (cannot read) and `0.0` (a
  quiet day) never collapse into each other. A refusal is a 429 with Retry-After, recorded as
  `evt=llm_budget_refused` and paged; it is never a 500.
- **UNKNOWN TOKENS ARE CHARGED.** A stream carries no `usage` block and the actor used the streaming
  endpoint, so an unpriced call costs `JHW_UNKNOWN_CALL_USD` against the caps and the row is marked
  `estimated`. A gate that only counts what it can price is one the attacker walks through.
- **A GATED FUNCTION THAT DOES NOT RECORD IS AN OPEN WALLET.** `llm.chat` — the transport for the
  whole consensus tailor — gated and never recorded for half a day, so the cap read a total that
  excluded the biggest spender, and a suite check asserted that hole was correct
  (`n_meter == 0` for resume_consensus). Both fixed; the suite now asserts per FUNCTION: if it
  gates, it records. And the meter write lives in its OWN `try`, never behind the ledger's.
- **The identity carrier needs a caller.** `llm_meter.set_current_user()` is called by
  `auth.require_user`; without it both per-account rules (`if user and ...`) are silently skipped
  on every path except `/api/chat`.
- **WATCH TWO SOURCES.** `spend_watch.py`: our ledger says *who*, DigitalOcean's month-to-date says
  *whether*. Median baseline with today excluded from it, a ratio AND a floor, and any model called
  today that was never called before is itself the finding. Needs `DO_API_TOKEN`.
- **EVERY HOSTNAME WE OWN IS REDIRECTED BY THE APP, NOT BY CADDY** (`backend/app/hosts.py`). A
  `redir` upstream is one line and means the request never reaches the only process that writes an
  event, so "show me everyone trying to enter jobhw.org" had no answer. The destination is a
  constant in that file (no open redirect), an unknown Host is served rather than bounced, and
  `/.well-known/` is never redirected — a bounced ACME challenge is a certificate outage.
- **THE ROUTE-TABLE GATE WAS SEEING 10 ROUTES OF 31.** FastAPI 0.139 stopped flattening
  `include_router()` into `app.routes`, so every auth, electronic, tracker and `/v1` route was
  skipped and the gate reported clean. `authz_audit.iter_api_routes()` is the one walker, both
  callers assert a floor on the count, and `python authz_audit.py` is a GATE inside `ship.py`.
- **ONE `evt=http` WRITER, NOMINATED IN CODE** (main.py), not by an env var in one compose file —
  and only if the nominated writer imports. The line now carries `host`.
- **THE CONSOLE NEVER RENDERS "I COULD NOT LOOK" AS ZERO.** `/api/security/overview` +
  `frontend/src/pages/Security.jsx`, admin-only server-side (`auth.require_admin`, fails closed).
  Unreadable source → `None` and a caveat; sidecar → active/stale/not installed/unverifiable;
  enforcement → unknown/none/armed/empty/active. Offenders are ranked by DISTINCT paths, not volume.
- **A SYNTHETIC AUDIT MUST NOT PAGE THE OPERATOR.** Probing every route anonymously trips
  `authz_probe` by construction; the audit and the wallet suite set `ALERTS_ENABLED=0` — detection
  runs, delivery does not, and the suppression is said out loud.
- **The WebRTC half of the browser probe is OPT-IN and OFF** at both ends (`JHW_PROBE_WEBRTC=1` and
  `VITE_JHW_PROBE_WEBRTC=1`). It cannot see a scripted client, it accuses corporate networks, and it
  unmasks an address the user chose to hide.
- **The deploy's `deploy_probe` is a GATE now**: it printed `EVENTS_LOG_UNWRITABLE` and exited 0, so
  a deploy with a dead event pipeline still said DONE.

## THE PORTFOLIO AND THE TOP-5 COVER LETTER (2026-09-22)
- **`portfolio.py` is the one home for his projects**, and the per-posting selection is ARITHMETIC,
  never a model: it counts the posting's own words against each project's stack, tags and title, so
  it cannot invent a project, it carries `matched` (a derived fact, labelled), and a posting that
  matches nothing selects NOTHING — an honest empty beats padding.
- **A PDF/WORD PORTFOLIO IMPORTS, AND THE IMPORT PROPOSES RATHER THAN SAVES.**
  `portfolio.parse_text` splits a file deterministically (every proposed value is a substring of
  his own file; `source` says which file), and only his Save writes to the store — a parser
  guessing at a two-column CV must not change stored facts. A scan with no text layer SAYS so.
- **TOP-5 IS A CONTRACT, NOT A PROMPT.** Exactly five reasons, each with proof; four or six is a
  rejected draft that walks the chain, and `revision_ok` refuses a revision that turns the five
  back into prose. **A floor must be measured against the document it guards** —
  `MIN_COVER_TOP5=500`, because the 700-char prose floor called every good five-reason letter THIN.

## PARSE IS NOT RUN — the white screen (2026-09-22)
- **One undefined identifier unmounted the whole cabinet**: `txt(...)` used in Tailor.jsx, defined
  only in the pages it was copied from. esbuild parsed it; the browser threw. "It parses" was never
  the question — `tests/test_frontend_symbols.py` asks whether every bare call a page makes is
  declared, imported or a global, and it strips TEMPLATES BEFORE JSX prose or it cannot see the
  `${txt(x)}` that caused it. **`ErrorBoundary` wraps both route trees**, so the next one shows the
  error instead of a blank page.

## READ THE DELIVERED ARTIFACT (2026-09-22)
- **A TOP-5 letter rendered EMPTY** — header, salutation, "Sincerely", nothing between — because
  `cover_struct` in generate() carried only `paragraphs` and dropped `reasons`/`opening`/`close`.
  Same defect, same function, as `highlights`/`earlier` on the resume. Every check stopped at the
  struct; the suite now unzips the DOCX and looks for the headlines. **`/revise` judges a document
  in ITS OWN format** (read from the manifest), or a top-5 edit is refused as "fewer than 2
  paragraphs" and the old file silently stands.
- **Page furniture is never a job title.** `jd_ingest.looks_like_title()` refuses logo alt text,
  "34 applicants", "2 weeks ago", Easy Apply/Remote/Full-time; `_is_employer_line()` refuses a name
  that reappears as a byline prefix (`Anaconda` / `Anaconda · Germany`) and uses it as the EMPLOYER.
  He got `cover_letter_anaconda_company-logo-for-anaconda.pdf` out of the old rule.

## THE RUN LOG (2026-09-22)
- **A progress bar is not a record.** `runlog.py` streams every step of a run to the page (polled,
  owner-checked, `known:false` for an unknown run) and writes the same lines beside the documents.
  A line is written when something HAPPENS: `[draft] cover deepseek-3.2 REJECTED depth=472 <- THIN`
  is the line that makes a bad run diagnosable. `RC.tailor(on_event=...)` carries the chain, every
  draft verdict with its depth, the auditor and its vendor, and each revision applied or refused.
- **A file the page LISTS must be one the server SERVES** — the log was listed beside the documents
  and refused by `docnames.looks_generated()` on download (HTTP 400). One home, both directions.

## THE VISIT FEED — "a person just opened jobhuntwow.com" (2026-09-21)
- **An anonymous VISIT is the only signal that says whether the site has traffic at all.**
  `visitors.note_visit()`: one plain-text Telegram message per visitor per 6h, naming the HOST, own
  hourly cap so a feed can never silence an alert, sent by `notify.fire_and_forget` (measured: 61 ms
  response while the sender slept 2 s). **Gate on the PATH, never the user agent.** A record with NO
  evidence still counts as a person — absence is unjudged, not a bot. **Every suppression writes its
  reason** and the console lists them, so "why did I get no message" is readable, not guessed.

## USAGE FEED AND BOT COUNTING (2026-09-21)
- **A feed is not an alert** (`alerts.usage()`: own hourly cap, no per-subject cooldown), and
  **`notify.telegram` is PLAIN TEXT by default** — one stray `_` made Telegram reject the whole
  message, so the alert that mattered most never arrived.
- **Three buckets, never two** (`visitors.py`): VISITOR · CLIENT · UNJUDGED, precedence one-way,
  fetch-metadata PRESENCE only, and the protocol check stays dormant until `X-Client-Proto` proves
  whose version it is. **The WebRTC probe is evidence, never a gate**, and off at both ends.
