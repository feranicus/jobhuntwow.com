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

**The wallet** (see `SECURITY.md`). Every one of these has a working default; set them only to move
a limit. The gate is asked BEFORE each model call and fails open on a storage fault, closed on the
budget.

| Var | Default | Meaning |
|-----|---------|---------|
| `JHW_DAILY_USD` | `3.00` | the whole service's AI spend per UTC day |
| `JHW_USER_DAILY_USD` | `0.75` | one account's share of it (self-signup is open) |
| `JHW_USER_CALLS_PER_HOUR` | `120` | per-account rate limit |
| `JHW_CALLS_PER_HOUR` | `600` | service-wide rate limit |
| `JHW_UNKNOWN_CALL_USD` | `0.01` | what a stream with no usage block costs against the caps |
| `JHW_METER_DB` | `$DATA_DIR/llm_meter.sqlite` | the ledger |
| `DO_API_TOKEN` | unset | lets `spend_watch` read the VENDOR's month-to-date usage. Without it that half honestly reports "unavailable" instead of 0 |
| `EXTRA_ADMIN_EMAILS` | unset | may only ADD administrators; `ADMIN_EMAILS` is committed in `auth.py` |
| `JHW_PROBE_WEBRTC` | off | the WebRTC half of the browser probe. Off at both ends on purpose |


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

## Who is using it, and who is a bot

**Telegram, as it happens** (`backend/app/alerts.py::usage`): every **sign-in** (user, address,
agent) and every **new job description** (employer, role, the posting link or the pasted size, the
files produced, how many that user has done today). This is a feed, not an alert, so it does NOT
use the alert cooldown — the second sign-in is exactly the event you want to see. Ceiling:
`JHW_USAGE_CAP_PER_HOUR` (40), and suppressions are logged. Off with `JHW_USAGE_FEED=0`.

**Three buckets, never two** (`backend/app/visitors.py`, `GET /api/visitors?hours=24`):

    VISITOR    the record carried evidence and nothing contradicted itself
    CLIENT     self-identified bot, or the record contradicted itself
    UNJUDGED   the record did not carry the fields to look at — a real answer, not a rounding error

Evidence, cheapest to most expensive: the UA table (labelling only), fetch-metadata **presence**
(`Sec-Fetch-*`, per-engine floors, never the values), and the protocol version — which only counts
when `X-Client-Proto` proves it is the CLIENT's version and not the proxy hop. Precedence runs one
way: an address seen once as a client stays one for the window.

**The browser probe** (`frontend/src/probe.js` → `POST /api/probe`) reports whether HTTP/3 and
WebRTC actually worked. It raises confidence for a real browser and flags headless/cloud ones —
it can never see a scraper (no JavaScript runs), so it is evidence, never a gate. The address
WebRTC reveals is compared on the server and dropped; only the boolean is kept. `no UDP` is
UNJUDGED, because that is what a corporate firewall looks like.

## Why nothing ever alerted, and what now does

Measured over 24 hours on 2026-09-21: **4002 requests, 320 attack-shaped, 0 alerts**, while the
sibling site fired 25 on a quarter of the traffic. The rules were fine. The server was not.

**The SPA catch-all answered HTTP 200 to every unknown path**, including `/.env`,
`/wp-login.php` and `/phpmyadmin/`. The two rules that catch scanners (`path_probe`,
`dir_bruteforce`) are gated on `status in (404, 403)`, so they were structurally unable to fire.
The same behaviour also told `perseus_client` that this app has a catch-all, which permanently
disarmed the local shield, and told every scanner that `/wp-login.php` exists.

**`backend/app/spa_guard.py`** now answers **404** to a probe-shaped path that is not a route we
serve. It is conservative by construction, because a wrong 404 here is a real person on a dead
page:

* the judgement is `perseus_client.probe_shape()`, the estate's one home for it, never a second
  path table;
* a declared client-side route is never refused, whatever its shape or its query string;
* a file that exists on disk is never refused;
* if `perseus_client` cannot be imported, or anything raises, the SPA is served;
* `JHW_PROBE_404=0` turns it off with a container restart and no deploy;
* every refusal emits `evt=spa_probe_404` with the path and the reason.

`CLIENT_ROUTES` mirrors `frontend/src/App.jsx` because the image ships the built bundle and cannot
read the router at runtime. `tests/test_spa_guard.py` parses App.jsx and fails if the two disagree,
so add a route to the router and the suite tells you before your users do.

**Security headers** (`backend/app/security_headers.py`) - the site had none while serving other
people's CVs behind a login. Ten now, installed outermost so they also decorate the refusals. The
CSP is written from what this site actually loads, not copied: the cabinet gets
`script-src 'self'` (the built shell has zero inline script, so this is free), and the public
landing one-pager, whose 162 KB of JavaScript is one inline block, is permitted by SHA-256 of that
exact block instead. `tests/test_security_headers.py` recomputes the hash from the served file and
reads both HTML shells and every `fetch()` in `frontend/src`, failing if the policy is missing an
origin the site uses **or** carries one it does not.

**Delivery is plain text.** Alert bodies carry attacker-controlled strings - the probed path, the
User-Agent - and one stray `_` or `*` under `parse_mode=Markdown` makes Telegram reject the whole
message with a 400. `notify.py` learned that in 2026-09; `observability.py`, the path actually
wired to the HTTP rules, had not.

**One line per request.** `perseus_client` and `observability` were both writing `evt=http` into
the same events log, doubling every count. `PERSEUS_OBSERVE_HTTP=0` silences the sidecar's write
only, and its evidence (`bot`, `sf`, `hv`, `hvs`, `av`) is merged into the surviving line rather
than dropped.

**The exfiltration rule points at the real payload.** `ALERT_DOWNLOAD_MARKER` defaulted to
`/deck/`, a route this site has never served. It is now `/api/electronic/artifacts/`, where the
candidate resumes, cover letters and photographs are.

**Proven, not assumed.** `tests/test_alert_chain.py` runs the rules in process - three probe-shaped
404s from one address must produce a `security_alert` tagged `jhw-web`, and the same three as 200s
must produce nothing. The staging gate then fires the real thing at the real container and asserts
the event appears there too (`probe_paths_are_refused`, `security_alert_fires`). The twin never
pages: the gate ships `ALERT_DELIVERY=0`, which suppresses the send and leaves a line saying so.

All of it runs inside `python ship.py`. There is no second command.

## Extra domains (jobhw.org)

`python domains.py` — read-only. It reads the hostnames out of the Caddy block we ship, prints the
exact DNS records to set at the registrar, then MEASURES whether each name points at the droplet and
whether the redirect actually answers. It says plainly when it cannot measure (no DNS on this
machine) instead of reporting a finding.

Order: **DNS first, then `python ship.py`.** Caddy asks Let's Encrypt for the certificate the moment
a hostname is in its config, and that only succeeds once the name resolves to the droplet.
`jobhw.org` and `www.jobhw.org` redirect to `https://jobhuntwow.com` in one hop, carrying the path
and the query — **and the redirect is issued by the application, not by Caddy**. A `redir` upstream
is one line and means the request never reaches the only process that writes an event, so nobody who
typed the short domain appeared anywhere in the record. They are proxied here, observed with
`host=jobhw.org`, and then bounced (`backend/app/hosts.py`). They show up as their own row on the
Security page.

## Security and observability (`/security`)

`SECURITY.md` is the whole picture. The short version: one JSON event per request (carrying the
hostname), 22 attack-shape classes, three visitor buckets (never two), a reversible shield, twelve
alert rules with a cooldown and a storm cap, ten security headers, a probe-shaped 404, a budget and
a rate limit in front of every paid model call, and an hourly watcher that compares our own ledger
against DigitalOcean's balance.

The operator's console is the **Security** page in the sidebar (administrators only, enforced
server-side on every request, not by hiding the menu entry). It shows who is knocking on each
hostname, what they asked for, what the defences did about it, whether anybody was actually told,
and what the models cost — and it renders an unreadable source as an em dash with a reason, never
as a zero.

```
python authz_audit.py     # every route, every method, asked anonymously. A gate inside ship.py
```

## Manuals (Russian)

`docs/manuals/` — **JobHuntWOW_QIG_RU.docx** (4 pages: sign in → tailor → pipeline → digest) and
**JobHuntWOW_Manual_RU.docx** (8 pages: every page and field, how the employer is resolved, what the
file names mean, what is still preview). PDF copies sit beside them.

## The Pipeline board

`/pipeline` reads the same rows. The columns are the real lifecycle:

    Tailored → Applied → HR screen → Technical → Task / presentation → Hiring manager
             → Final panel → Offer → Contract negotiation → Signed   (Rejected from any)

`Applied` and `Submitted` used to be two columns for one event. The engine's "submitted" means the
SITE confirmed the send — evidence about that event, not a next step — so it is a ✓ on the card and a
line in the digest. Old rows migrate themselves: `submitted` → Applied with the tick, `interview` →
HR screen. Every name we have ever used (including anything the local apply engine sends) still
resolves to a real column.

**Drag a card into another column to move it** — the card moves
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
