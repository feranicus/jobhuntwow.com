# Infrastructure — DigitalOcean, `.env`, Docker everywhere, Loki + Grafana

How JobHuntWOW runs, where the secrets live, and how observability is wired. Written from the
actual compose/config files in this repo, not from intent. If a file changes, update this doc in
the same commit (standing rule).

Everything material lives on **one DigitalOcean droplet** and runs in **Docker**. There is no
Kubernetes, no serverless functions, no managed database — deliberately. KISS is a hard rule here.


## 1. The DigitalOcean droplet

- **Region:** FRA1 (Frankfurt). All app data, sessions, generated documents and logs stay in the EU
  — that is a load-bearing privacy claim, not a nicety.
- **Public IP:** `64.225.108.200`. `jobhuntwow.com` A-records point here.
- **Deploy path (SETTLED):** direct from the PC — `python jhw.py deploy` → `deploy_direct.py`.
  It scp's the build context to `/opt/jobhuntwow`, the **droplet builds the image itself**, brings
  it up, wires the shared Caddy vhost, and verifies. **GitHub / GHCR / CI are NOT in the deploy
  path** — the round-trip added a stall and a second source of truth for zero benefit. `jhw.py push`
  is source control only and is not required to deploy.
- **One irreducible manual step:** cloud credentials / DNS can only be set by the account owner,
  once. After that everything is scripted and re-runnable.

### The droplet is shared — do not disturb the neighbours

The droplet also runs **VideoDead (jobhuntwow's streaming sibling), Amnezia VPN, Joplin, and the
`colt-stack`** (the Colt cyber pre-sales bots — a separate project). JobHuntWOW couples to that
environment through exactly two things and nothing else:

1. the **shared Caddy** vhost for `jobhuntwow.com`, and
2. the shared **`colt-stack_colt_events`** log volume (so app events reach the existing Grafana).

Rules that keep this safe (all enforced in code):
- `jhw-web` joins the **existing external network** `videodead_appnet` and is defined on **one
  network only**. A container on two networks makes Docker DNS hand Caddy an unreachable IP →
  intermittent 502. This was a real outage; it is now a hard rule.
- **Never `--remove-orphans`** on a subset compose in the shared project — it would delete
  colt-promtail / the bots.
- No firewall changes. Patch automation only refreshes our own images.


## 2. Docker, everywhere — the three stacks

Every process runs in a container. There are three compose stacks, each with a distinct job.

### a) Production web app — `docker-compose.web.yml` (on the droplet, project `jobhuntwow`)

One service, `jhw-web`: FastAPI backend + built React frontend in a single image
(`Dockerfile.web`), serving the SPA and `/api` from the same container.

- **No published ports.** Nothing on `0.0.0.0`. Public traffic arrives *only* through the shared
  Caddy, which reaches it as `http://jhw-web:8000` on `videodead_appnet`.
- Secrets from `/opt/jobhuntwow/.env` (chmod 600, gitignored, never baked into the image).
- Least privilege: `no-new-privileges:true`, `cap_drop: [ALL]`, `mem_limit 1g`, `cpus 1.0`,
  healthcheck on `/api/health`, JSON logs capped at 10m × 3.
- Writes events to the shared `colt_events` volume **and** stdout (so nothing depends on one path).

### b) Local dev — `docker-compose.yml` (on your PC)

`backend` + `frontend`. Ports bound **`127.0.0.1` only** (`8000` backend, `8090` frontend) — the
backend proxy holds the DO inference key, so it must never listen on `0.0.0.0`. Same hardening as
prod: `no-new-privileges`, mem/cpu limits, healthcheck, bounded logs.

### c) Local apply sandbox — `agent/docker-compose.local.yml`

The isolated browser stack the ATS driver runs in. **Security is the whole point:** the agent must
never have access to your real machine, so it lives in throwaway containers. Four services:

| service | role | notes |
|---|---|---|
| `jhw-browser` | Chromium + noVNC + CDP | only published port is `127.0.0.1:9090` (noVNC watch). CDP `9222`, VNC `5900` stay INTERNAL |
| `jhw-stagehand` | Stagehand automation svc | `network_mode: service:jhw-browser` — shares the browser's netns, reaches Chrome at `127.0.0.1:9222` |
| `jhw-agent` | the DOM/LLM apply driver | same shared netns; brains call out to DO via the backend proxy |
| `jhw-bot` | Telegram ask-bridge | relays the agent's questions to your phone and the answers back |

CDP on `9222` is deliberately **not** published — an exposed CDP is full remote browser control with
no auth, treated as a critical finding. Every service: `no-new-privileges`, `cap_drop: [ALL]` where
possible, `mem_limit`.

**Dockerfiles:** `Dockerfile.web` (prod), `backend/Dockerfile`, `frontend/Dockerfile`,
`agent/Dockerfile` (browser+agent), `agent/stagehand-svc/Dockerfile`, `agent/bot/Dockerfile`.


## 3. Secrets and `.env`

The one hard rule: **secrets never touch git, never enter an image, never appear in argv.**

- `.gitignore` blocks `.env` and `*.env`, and whitelists only `!.env.example`. `data/` is ignored
  too. `gitleaks` runs in CI as a backstop.
- Real values live **only** in `.env`: on your PC for local, and in `/opt/jobhuntwow/.env`
  (chmod 600) on the droplet. Compose reads them via `env_file:`. Never `COPY .env` into a layer.
- The user manages `.env` by hand — scripts do not rewrite it.
- When the agent container needs a secret it is passed **by name** (`-e KEY`, no value on the
  command line) after exporting into the process env, so the value never shows in `ps` or the echoed
  command.

### The keys (see `.env.example` for the full template)

| Variable | What it is |
|---|---|
| `DO_INFERENCE_BASE_URL` | DigitalOcean Serverless Inference endpoint (`https://inference.do-ai.run/v1`) |
| `DO_INFERENCE_KEY` | the DO inference key — **the** secret; only the backend proxy ever holds it |
| `QWEN_MODEL` | pin a model id, or leave empty to auto-resolve |
| `AGENT_PROXY_TOKEN` | shared token the `/v1` proxy checks; the local agent authenticates with THIS, never with the DO key |
| `JHW_MODEL_*` | optional per-role model overrides (driver / vision / content / extract / chat) |
| `DATA_DIR`, `CORS_ORIGINS` | storage path, CORS |
| `GRAFANA_PASSWORD` | Grafana admin password (obs stack) |

**Why the split matters:** the DO key lives on the server side only. Each candidate's local agent
gets the low-privilege `AGENT_PROXY_TOKEN` and reaches models through the backend's `/v1` proxy — so
a compromised sandbox can't exfiltrate the inference key.


## 4. Observability — Loki + Grafana + Promtail

The pattern is deliberately minimal: **services append one JSON object per line → Promtail tails the
files → Loki stores → Grafana renders.** No OpenTelemetry collector, no metrics server, no sidecars.

### The event stream

Every service emits structured events (`agent/obs.py` / backend), one JSON per line, with at least
`ts, service, evt` and often `job_id, stage`. No secrets or PII in events (a documented rule).
Events go to **both** stdout and a file, so the pipeline never depends on who owns stdout.

### Local — `docker-compose.obs.yml` (`python jhw.py obs`)

Profile-gated: it never runs during a normal apply. Three services:

- **Loki** (`grafana/loki:3.0.0`) — `obs/loki.yml`: filesystem storage, single-node ring, TSDB
  schema v13, **30-day retention** (`retention_period: 720h`), 8 MB/s ingest cap. Internal only.
- **Promtail** (`grafana/promtail:3.0.0`) — `obs/promtail.yml`: tails `/events/**/events.jsonl`
  **read-only**, parses the JSON, promotes `service` and `evt` to labels, uses the event `ts` as the
  timestamp. It gets **no docker socket** (that would be root-equivalent on the host) — least
  privilege by design.
- **Grafana** (`grafana/grafana:11.1.0`) — bound `127.0.0.1:3000` only, sign-up disabled, telemetry
  off, admin password from `${GRAFANA_PASSWORD}`.

### Grafana provisioning (all file-based, in git — no click-ops)

- `obs/grafana/provisioning/datasources/loki.yml` — the Loki datasource, set as default.
- `obs/grafana/provisioning/dashboards/dashboards.yml` — a file provider that loads everything in
  `/var/lib/grafana/dashboards` into a "JobHuntWOW" folder.
- Dashboards (committed JSON, auto-imported):
  - `jobhuntwow.json` — core app activity.
  - `electronic.json` — the Electronic agent: signups, verified logins, OTP ok/fail,
    `missing_dependency`, mailer self-check, live apply progress phase-by-phase.
  - `electronic-security.json` — visitor telemetry + the security alert rules (login failures,
    spraying, path probes, exfil bursts, delivery failures).

Dashboard queries are label-agnostic (`{job=~".+"} |= \`"service":"jhw-web"\``) because the shared
promtail's label scheme is not ours to depend on — every event we emit carries `"service"` so we
match on the content.

### Production — reuse, don't rebuild

On the droplet we do **not** stand up a second Loki/Grafana. `jhw-web` writes to
`EVENTS_LOG=/var/log/colt/jhw-web.log` on the shared **`colt-stack_colt_events`** volume, which
**colt-promtail already tails → Loki → Grafana**. `python jhw.py obs` installs our dashboards into
whichever Grafana provisioning dir exists and asks Loki whether our lines are actually indexed
(never assumes). One landmine handled automatically: `jhw-web` runs as a non-root uid and that
volume is root-owned, so the deploy runs a one-shot root container to create+chown the log file,
else writes fail silently.


## 5. The one command per job

Operations are single verbs on `python jhw.py` — never a list of commands to paste:

| command | does |
|---|---|
| `python jhw.py deploy` | ship source → droplet builds → wire Caddy → verify (direct from PC) |
| `python jhw.py obs` | wire + verify events in Grafana, install dashboards |
| `python jhw.py status` | is the public site actually our app (probe-tagged, not just "it answers") |
| `python jhw.py diagnose` | who serves `jobhuntwow.com` right now |
| `python jhw.py logs` | tail `jhw-web` logs |
| `python jhw.py local` / `local-down` | bring up / tear down the apply sandbox |
| `python jhw.py apply <url>` | run a real application in the sandbox |

Local dev without the orchestrator: `docker compose up --build` → app on `:8090`, `/api` proxied.


## 6. Network exposure at a glance

| port | where | exposure |
|---|---|---|
| 443 | shared Caddy → `jhw-web:8000` | public (the only public surface) |
| 8000 | backend | `127.0.0.1` (local) / internal-only (prod) |
| 8090 | frontend (local) | `127.0.0.1` |
| 3000 | Grafana | `127.0.0.1` |
| 3100 | Loki | internal (`expose`, never published) |
| 9090 | noVNC (sandbox watch) | `127.0.0.1` |
| 9222 / 5900 | Chrome CDP / VNC | **internal only — never published** |

The through-line: exactly one public port, everything else loopback or internal, the DO key server-side only, and every workload in a hardened container.
