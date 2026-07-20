# JobHuntWOW app — project conventions for Claude / AI agents

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
- Adding an apply target = fill `/api/apply`; the agent must never auto-submit without an explicit user action.

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

## REMINDER — the Edit/Write tool truncated jhw.py again (line 376) losing main()'s tail + __main__.
Symptom: `python jhw.py <verb>` silently does nothing / a verb "disappears". Cause: the file editor
truncated jhw.py mid-`main()` (run/bot/obs parsers + `args.fn(args)` + `if __name__` gone). ALWAYS
author/repair jhw.py with a bash heredoc or python read/replace-in-bash, then machine-verify:
`python3 -m py_compile jhw.py` AND `tail -4 jhw.py` shows `if __name__ == "__main__": main()` AND
`python3 jhw.py --help` lists every verb. Never trust the editor for this file.

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
