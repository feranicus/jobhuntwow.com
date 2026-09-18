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

## TWO PRODUCTS, TWO COMMANDS — do not confuse them again (2026-09-18, he lost time to this)

His words: *"I need you to make changes to https://jobhuntwow.com/tailor ... on our Digital Ocean
droplet not on docker desktop on my pc!!!"* — after a reply of mine ended with `python jhw.py up`,
which is the LOCAL APPLY SANDBOX. That verb builds `jhw-browser` FROM ubuntu:24.04, so Docker Desktop
started and began pulling Ubuntu layers for work that had nothing to do with what he asked for.

    THE WEBSITE   jobhuntwow.com · /tailor · /pipeline · the digest · the tracker
                  -> cd jobhuntwow-app && python ship.py
                  -> NO Docker on this PC. One ssh session; the DROPLET builds the image.

    THE SANDBOX   Chromium + noVNC filling Workday / Ashby / Greenhouse forms
                  -> python jhw.py up|apply   -> needs Docker Desktop, by design.

RULES:
1. **Name the product before naming the command.** A reply that ends in the wrong one costs him a
   ten-minute image pull and the trust that the last answer was read carefully.
2. `deploy_direct.py` must never import or call anything from `agent/jhw.py` — a website deploy that
   ends by demanding Docker Desktop is the same defect from the other side. Asserted by
   `tests/test_gate_integrity.py` (no `cmd_status`, no `spec_from_file_location`, no local `docker`
   subprocess in either `deploy_direct.py` or `ship.py`), negative-tested.
3. `JHW_NO_DOCKER_AUTOSTART=1` stops `jhw.py` from starting Docker Desktop for him, and its refusal
   message NAMES the website path so the two cannot be confused at the moment it matters.

## THE DOCS NAMED FIVE COMMANDS THAT HAVE NEVER EXISTED (2026-09-18)
`CLAUDE.md`'s own SETTLED-deploy section described **`jhw.py deploy`** (no such verb), and DEPLOY.md offered
`push`, `chat`, `diagnose` and `mailcheck` — none of which are verbs of anything. `flows/test_docker.py`
§8 had been enforcing exactly this property for commands we PRINT AT RUNTIME since 2026-08-17, and
the markdown had drifted the same way with nobody checking. Now `tests/test_claude_md_size.py` reads
`agent/jhw.py`'s real verb list and asserts every `python jhw.py <verb>` and every `python <script>.py`
named in the operator-facing docs exists — resolving each against THE FILE THAT PRINTS IT
(`agent/README.md` says `python apply_all.py` and the reader is standing in `agent/`; resolving only
against the repo root flagged a correct line, which is the identical false positive §8 once had).
Both halves negative-tested. `docs/decisions/` is excluded on purpose: it is a record of what was
said at the time, not instructions.

## THE BOARD IS DRAGGABLE, AND THE MIS-CLICK THAT PUT A LIVE APPLICATION IN "REJECTED" (2026-09-18)
He asked for one thing: *"I need to be able to move myself the jobs in pipeline but just moving them
with my mouse"*. Native HTML5 drag-and-drop in `Pipeline.jsx` — no library. THE MOVE IS OPTIMISTIC
AND REVERSIBLE: the card lands immediately, the PATCH follows, and a refusal puts it BACK and prints
why, because a board showing a state the database does not hold is the same defect as a log claiming
a submit the site never confirmed. The one-click `rejected` link is REMOVED (his screenshot shows a
real application sitting in Rejected; that link was one mis-click away on every card), and a
`move to …` select remains for keyboard and touch.
`tests/test_pipeline_dnd.py` checks BOTH halves: the endpoint over REAL HTTP through the app (legal
move lands · invented stage 400 · unknown job 404 · **a refused move leaves the stored row
unchanged**), and the drag contract in the JSX. The contract that matters most is
`preventDefault` on `dragOver` — without it the browser silently refuses every drop, so the feature
looks implemented and does nothing. Three mutations, all caught.
HONEST LIMIT: there are no node_modules in my sandbox, so I did not RENDER the page — the JSX is
parsed by esbuild and the wiring is asserted, not clicked.

## `(employer not recorded)` AND `resume_job_35.pdf` — the posting's address is a fact (2026-09-18)
His board showed a card with no employer and a file called `resume_job_35.pdf`, because that JD
(`https://app.civi.co.il/promo/id=892963`) carried no company name at all. A filename that names
nobody is useless at the one moment it is read: when he attaches it.
`docnames.employer_from_url()` reads the employer off the address, and it knows the ATS is not the
employer: `jobs.ashbyhq.com/elevenlabs/…` → elevenlabs (first path segment on a board host),
`intive.wd3.myworkdayjobs.com` → intive (the tenant), `app.civi.co.il` → civi (noise labels
stripped). It is used ONLY when the JD named nobody, it is recorded as `company_source: "url"` so a
guess is never mistaken for the JD's own word, and **it never reaches the resume or the cover
letter** — those are written from the JD.
MY OWN WIRING CHECK BROKE ON THE FIX: it asserted `"company=jd.get" in generate`, the OLD spelling,
which is precisely the defect that file exists to prevent. Re-pinned to the property (the writer is
handed company/title/seq).

## "IN EVERY JOB DESCRIPTION THERE IS A NAME OF THE COMPANY" — he is right (2026-09-18)
His card read `(employer not recorded)`, title **"About the job"**, above 3,623 characters of pasted
job description. `jd_ingest._guess_title_company` understood a literal `Company:` label — which
almost no posting uses — and took the FIRST LINE as the title, which on a LinkedIn paste is a
section header. The name was in the text the whole time; nothing had looked.
`sniff_title_company()` now reads the shapes postings actually use (`<Company> is looking for`,
`<Title> at <Company>`, `About <Company>`, `At <Company>, we…`, `Join us at <Company>`, the labels),
and `_looks_like_name()` refuses a section header, a sentence, a generic noun and anything over six
words — **a bad guess on a card and in a filename is worse than none**.
THE LADDER, most certain first, recorded in `company_source`: the JD's own word → **the model,
reading the posting** → the posting's address → nothing, said plainly. Rung 2 is safe for the same
structural reason Set-of-Mark and the closed option list are: `jd_ingest.company_in_text()` accepts
the model's answer ONLY if it appears VERBATIM in the posting, so a hallucinated employer is
impossible rather than unlikely. None of it reaches the resume or the cover letter — asserted by
measuring that `_emp` appears only AFTER the consensus wrote them.
ROWS ALREADY IN THE DATABASE ARE FIXED ON READ (`tracker._derive_employer`), from the text they
already hold — a labelled derivation beats a migration that rewrites his history.
`tests/test_employer_ladder.py` runs rung 2 with a STUBBED model (a real one would make the test a
coin flip; the GUARD is the subject): a name in the posting is accepted, `Coinbase` against a
Fireblocks posting is REFUSED, an exception costs nothing. Two mutations, both caught — and my first
two attempts at the last check were a tautology and an empty message, which is the vacuous-check
defect this file records over and over.

## CLICK A CARD, SEE THE JOB (2026-09-18)
*"if I click on this job in the pipeline it needs to give me its details such as the job description
and when exactly It was created time and full date"*. A drawer on `Pipeline.jsx`: the WHOLE pasted
job description, `created` / `sent` / `last change` as full date + time + TIMEZONE (a relative "2
days ago" is not an answer to "when exactly"), stage, ATS, the documents as download links, the
record id, and `employer from` so a derivation is never mistaken for the JD's own word.
A CLICK MUST NOT BE A DRAG: `didDrag` is set on dragStart and cleared one tick after dragEnd, so
finishing a drag never also opens the panel. Negative-tested.
TWO FIELDS ARE EDITABLE — employer and role — because a person knows those better than a guess;
everything else on a row is EVIDENCE (what was sent, when, which files) and stays read-only.
`POST /api/applications/{id}/reread` runs the ladder again on demand for cards written before the
sniff understood postings, with the same verbatim guard on the model rung, and PERSISTS the result.
MY OWN CHECK MISSED THE MISSING WRITE: "the card still shows Atera" is true even when nothing was
saved, because `get()` also DERIVES the employer on every read — defence in depth hiding the thing
under test, the fourth time in this project. Re-pinned to `employer_source == "jd"`, which is true
only when the value is STORED; the mutation is caught now.

## "APPLY AND SUBMITTED IS SAME SHIT DIFFERENT COLOR" — the lifecycle is nine columns now (2026-09-18)
His words, and both halves were right. **Applied and Submitted were one event in two colours:** the
apply engine says `submitted` only when the SITE confirmed the send, which is EVIDENCE about that
event, not a second step in his funnel. It is now `confirmed` on the row — a ✓ on the card and a line
in the digest — and the column is gone. **"Interview" was a season, not a stage:** HR screen ·
Technical · Task/presentation · Hiring manager · Final panel. A board that cannot say which round he
is in cannot tell him what to prepare tonight.
    tailored → applied → hr_screen → tech → task → manager → final → offer → negotiation
             → signed                                                     (rejected from any)
AND AN OFFER IS NOT THE END (same day, his follow-up): *"where is the stage of Contract negotiations
and Signed contract?"* — between "they want you" and "you have a job" sit the two weeks that decide
the money, the start date and the notice period. `negotiation` and `signed` are columns of their own,
and `contract` / `negotiating` / `hired` / `accepted` / `closed won` all canonicalise into them.
NOTHING BREAKS AND NOTHING IS LOST: `canon_stage()` is pure and maps every name we have ever used
(`submitted`→applied, `interview`→hr_screen, `technical`→tech, `panel`→final, …), so the apply engine
on his PC keeps working WITHOUT being redeployed; `_migrate()` adds the `confirmed` column to an
older database and rewrites legacy stage names once, idempotently — proven against a database built
in the OLD shape. The suite also asserts the board renders EVERY stage the store allows, so a stage
can never exist with no column to hold it. Three mutations, all caught.
A STALE ASSERTION OF MINE BROKE ON THIS: it demanded `stage == "tailored"` after an unrelated edit,
which an earlier section legitimately moved. Re-pinned to the property (editing the employer must not
move the card). The Russian manuals were regenerated in the same change — a manual that describes six
columns the day nine ship is worse than no manual.
