# JobHuntWOW

**Turn a chaotic job search into a WOW dashboard.** Zero manual data entry, KISS by design.

JobHuntWOW is two cooperating pieces:

| Component | What it is | Where it lives | Hosting |
|-----------|-----------|----------------|---------|
| **`jobhuntwow.com`** (landing + tracker) | Marketing site + the automated interview tracker: **Gmail → Google Apps Script → Google Sheets → React dashboard**. Zero app backend (Apps Script IS the backend). | repo `github.com/feranicus/jobhuntwow.com` | **Droplet** — the shared videodead Caddy serves the static build from `/srv/jobhuntwow`; deployed by `deploy_jobhuntwow_caddy.py`. (DNS A → 64.225.108.200; the repo's GitHub-Pages workflow is legacy/unused.) Data + SSO via Google Apps Script. |
| **`jobhuntwow-app`** (this folder) | The interactive cabinet: **React (Vite)** front end + **FastAPI** back end with **Hermes** chat on real **Qwen** via DigitalOcean Serverless Inference, plus Job Scout / Apply-Driver. | **this project** | Docker on the droplet at `app.jobhuntwow.com` |

This README documents **`jobhuntwow-app`**. For the tracker/landing see the `jobhuntwow.com` repo.
For the plan to split this out of the Colt monorepo into its own repo, see **[SEPARATION.md](SEPARATION.md)**.

## What it does (app)

- **Hermes chat** — streaming assistant on real Qwen (DO Serverless Inference, OpenAI-compatible).
- **Job Scout** — search jobs by query/location/remote (v0.1 returns realistic samples; the real
  LinkedIn + company-site scout plugs into `backend/app/scout.py`).
- **Pipeline** — interview pipeline board.
- **Connections** — store per-user integration settings.
- **Apply-Driver** — `/api/apply` stub where the ATS auto-apply agent lands next.

## Architecture (one breath)

```
Browser ── app.jobhuntwow.com ──> Caddy (videodead) ──> frontend (nginx :80)
                                                          │  /api/*  ──> backend (FastAPI :8000)
                                                          │                 └─> Qwen (DO Serverless Inference)
                                                          └─ static React (Vite build)
```

Full detail in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Quick start (Docker)

```bash
cp .env.example .env         # put your doo_v1_... key in DO_INFERENCE_KEY
docker compose up --build
# app:  http://localhost:8090        api: proxied at /api
```

## Quick start (dev, hot reload)

```bash
# backend
cd backend && pip install -r requirements.txt && \
  DO_INFERENCE_KEY=doo_v1_... uvicorn app.main:app --reload   # :8000

# frontend (new terminal)
cd frontend && npm install && npm run dev                     # :5173, proxies /api
```

## Configuration (`.env`)

| Var | Meaning |
|-----|---------|
| `DO_INFERENCE_BASE_URL` | DO Serverless Inference base (`https://inference.do-ai.run/v1`) |
| `DO_INFERENCE_KEY` | your `doo_v1_...` key (**secret** — never commit) |
| `QWEN_MODEL` | exact model id, or blank to auto-pick the first `/api/models` result |
| `DATA_DIR` | backend data dir (default `/data`, a Docker volume) |
| `CORS_ORIGINS` | allowed origins (`*` in dev; lock down in prod) |

## Release  (`python ship.py`)

**ONE command releases the app.** It runs the test gates, pushes to GitHub, validates the change on a
staging droplet that it **reboots**, deploys, verifies the code actually inside the running container,
and tags a rollback point:

```
python ship.py                  # the whole release
python ship.py --test           # gates only
python ship.py --rollback       # back to last-known-good, redeployed
```

`ship.py` calls `deploy_direct.py` directly — nothing is reimplemented. (NOTE: there is no
`jhw.py deploy` subcommand; this line used to claim there was, and that sent one operator to a
command that does not exist. `python ship.py` is the deploy verb.) The check
inventory, the four-model review panel, the governance rules and how to enable the staging droplet
(one variable: `JHW_STAGING_HOST`) are documented in **[docs/CICD.md](docs/CICD.md)**.

## Deploy

Runs as Docker on the droplet behind the shared Caddy at **`app.jobhuntwow.com`**.
Step-by-step (DNS, Caddy vhost, compose) in **[DEPLOY.md](DEPLOY.md)**.

## Applying: one warm stack, many applications

`python jhw.py apply "<url>"` does everything itself, as always. What changed (2026-09-18) is that
it no longer pays for a docker build it does not need:

```powershell
cd "C:\Python SW\Linkedin Scraper\jobhuntwow-app"
python jhw.py up                      # once per session (or after a reboot)
python jhw.py apply "https://jobs.ashbyhq.com/..."
python jhw.py apply "https://jobs.ashbyhq.com/..."      # warm: no rebuild, straight to the form
```

| Switch | What it does |
|--------|--------------|
| (default) | If Chrome's CDP already answers, the build is skipped and the running stack is reused. |
| `--rebuild` / `JHW_FORCE_BUILD=1` | Force `docker compose --build` — after editing a Dockerfile or stagehand's `server.ts`. |
| `JHW_FAST=1` | Also skip the three browser self-tests. The pure-logic contracts still run, and the skip is printed. |

`agent/flows/*.py` is bind-mounted, so editing an adapter needs no rebuild at all.

**Docker Desktop is the verb's problem, not yours.** If the engine is not running, `jhw.py` starts
Docker Desktop and waits for it; if it cannot, you get one sentence and exit code 2 — never a
`CalledProcessError` traceback naming an npipe.

**`jhw.py` at the repo root is a LAUNCHER for `agent/jhw.py`.** It used to be a second copy, the two
drifted, and the copy he actually runs was the stale one (no `git`/`memory` verb, five self-test
suites unregistered, and it printed a verb that has never existed). One file, invoked from either
place.

## What the files are called

`resume.pdf` is what every application used to produce, so four downloads later the folder holds
`resume (1).pdf` … and the file you attach to an employer is a guess. Now:

```
resume_cisco_project-manager.pdf
cover_letter_cisco_project-manager.pdf
resume_cisco_project-manager_2.pdf      # a SECOND Cisco project-manager posting
```

The rule is `backend/app/docnames.py` (stdlib only, so it is testable anywhere). The number comes
from how many times that employer **and** role have already been tailored — counted from the
manifests on disk, so it survives a restart — and "Cisco Systems Inc." and "cisco systems" are the
same employer. Asking Electronic to revise a draft rewrites the SAME filenames; it never numbers the
same job twice. Older jobs keep their old names and still download.

## Manuals (Russian)

`docs/manuals/` — **JobHuntWOW_QIG_RU.docx** (4 pages: sign in → tailor → pipeline → digest) and
**JobHuntWOW_Manual_RU.docx** (8 pages: every page and field, how the employer is resolved, what the
file names mean, what is still preview). PDF copies sit beside them.

## The Pipeline board

`/pipeline` reads the same rows. **Drag a card into another column to move it** — the card moves
immediately, the change is saved, and if the server refuses it goes back where it was and says why.
A `move to …` dropdown on each card does the same thing for keyboard and touch. (The one-click
`rejected` link is gone: one mis-click is how a live application ended up in Rejected.)

**Click a card to open it**: the whole job description, the exact creation and send times (full date,
time and timezone), the stage, the ATS, the documents as download links, and where the employer came
from. Two fields are editable — the employer and the role — because a person can know those better
than a derivation; everything else on a row is evidence and stays read-only. **Re-read the posting**
runs the employer ladder again for cards written before the parser understood how postings are
worded.

**Who the job is with** is answered by a ladder, most certain first:

1. the ATS/JSON-LD parser, or the pasted text's own header — `Fireblocks is looking for …`,
   `Senior PM at Cisco Systems`, `About Acme Robotics`, `At Monzo, we …`, `Company: Zalando SE`;
2. the model, reading the posting — and its answer is accepted **only if the name appears verbatim
   in the posting**, so an invented employer cannot reach a card, a filename or the record;
3. the posting's own address — `app.civi.co.il` becomes `civi`, so a file is
   `resume_civi_product-manager.pdf` rather than `resume_job_35.pdf`.

Which rung answered is stored as `company_source` (`jd` / `llm` / `url` / `none`), and none of it
ever reaches the resume or the cover letter — those are written from the job description. Cards
already in your pipeline are fixed on read, from the text they already hold.

## Your applications: the correlation, and the digest

Every tailored document set is recorded as ONE row that says **which job description** (the link you
gave, or the text you pasted) produced **which resume and cover letter** — and the same row is
updated when the application is actually sent, by the portal or by the local apply engine.

* `backend/app/tracker.py` — the store (`DATA_DIR/tracker.sqlite`) and `GET /api/applications`,
  `PATCH /api/applications/{job_id}` (stage moves). The **Pipeline** page reads it; it used to render
  invented companies.
* `backend/app/digest.py` — the e-mail. The evening of **any day an application was sent** you get a
  digest of what went out; a week with **nothing** sent gets one message saying exactly that, and
  nothing in between. `JHW_DIGEST_HOUR` (UTC, default 19), `DIGEST_EMAIL`, `JHW_DIGEST=0` to disable.
* `python jhw.py digest --now` prints and sends the same mail the scheduler would, so it can be
  proven rather than trusted. `--force` sends even when nothing is due.

A row that cannot prove the correlation — a job description with no document, or documents with no
job description — is **named** in the digest and flagged on the Pipeline card. It is never counted
as a success.

## Repo map

| Path | Purpose |
|------|---------|
| `backend/app/main.py` | FastAPI routes: `/api/health,/models,/connections,/chat,/scout,/apply` |
| `backend/app/qwen.py` | DO Serverless Inference client (model list + streaming chat) |
| `backend/app/scout.py` | Job scout (v0.1 samples; real scraper plugs in here) |
| `backend/app/store.py` · `settings.py` | JSON store + env config |
| `frontend/src/pages/` | `Dashboard` · `Hermes` · `Pipeline` · `Scout` · `Connections` |
| `docker-compose.yml` | backend (`:8000`) + frontend nginx (`8090:80`, proxies `/api`) |
| `Caddy-snippet.txt` | the `app.jobhuntwow.com` reverse-proxy block for the droplet Caddy |

## License

See the `jobhuntwow.com` repo `LICENSE` (same project, open source).
