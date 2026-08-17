# THE BIBLE — machines, IPs, directories, SSH, deploy. Read this first.

Purpose: a fresh Claude (or human) should understand the whole estate from THIS file alone, in
minutes, without spelunking. Every value below was read from the actual files, not remembered.
If any of it changes, update this file in the same commit (standing rule).

> TL;DR — One DigitalOcean droplet at **64.225.108.200** (Frankfurt) runs **four independent Docker
> stacks**. Deploys go **directly from this PC over SSH** (no GitHub in the path). Secrets live only
> in gitignored `.env` files. The three projects on this PC map to three GitHub repos and three
> droplet compose projects. Do not confuse them.


## 0. The three projects on this PC (do not mix them)

| PC folder | What it is | GitHub repo | Deploy command |
|---|---|---|---|
| `C:\Python SW\Linkedin Scraper\jobhuntwow-app` | **JobHuntWOW app** ("Electronic" + Tailor). React+FastAPI + local apply sandbox. THE main project. | `github.com/feranicus/jobhuntwow.com` | `python jhw.py deploy` |
| `C:\Python SW\Linkedin Scraper\jobhuntwow.com` | JobHuntWOW **landing page** (GitHub-Pages static + Apps-Script Gmail→Sheets tracker). Sibling. | `github.com/feranicus/jobhuntwow.com` | git push / Pages |
| `C:\Python SW\Linkedin Scraper` (root) | **Colt cyber pre-sales automation** (`electronic` repo): assess bots, cassandra, `cybergod.ai`. SEPARATE product. | `github.com/feranicus/electronic` | `python ship.py` |
| `C:\Python SW\oxford-dictionary-scraper` | Unrelated scraper. Ignore for infra. | — | — |

**CRITICAL:** jobhuntwow-app was extracted FROM the Colt monorepo. Never reintroduce Colt / Shodan /
cybergod code into jobhuntwow-app. They only share the physical droplet.


## 1. The one droplet — 64.225.108.200

- **Provider:** DigitalOcean · **Region:** FRA1 (Frankfurt) · **User:** `root` · **SSH:** port 22, open to the internet.
- **Public IP:** `64.225.108.200` (hard-coded default in `deploy_direct.py`; override with `DROPLET_HOST`).
- Hosts **four unrelated stacks side by side**. Nothing here may disturb the others:

| Stack | Compose project | Serves | Owner |
|---|---|---|---|
| **jobhuntwow** | `-p jobhuntwow` at `/opt/jobhuntwow` | `jobhuntwow.com` | this app |
| **colt-stack** | `-p colt-stack` at `/opt/colt-stack` | `cybergod.ai`, assess bots, `/opt/shodan-skill` | the Colt project |
| **videodead** | `videodead_*` | streaming; **owns the shared Caddy (:443)** and network `videodead_appnet` | sibling |
| **Amnezia VPN / Joplin** | own containers | VPN (UDP) / notes | untouched |

The shared **videodead-caddy** terminates TLS for every site and reverse-proxies to each app's
container over the external network `videodead_appnet`. Observability (Loki/Grafana) is owned by
colt-stack/videodead; we reuse it (see §5).


## 2. Domains & DNS

| Domain | Points at | Serves | DNS registrar |
|---|---|---|---|
| `jobhuntwow.com` (+ `www` → apex redirect) | droplet `64.225.108.200` | the app, via shared Caddy → `jhw-web:8000` | (apex A-record → droplet) |
| `cybergod.ai` | droplet | Colt web cabinet | **GoDaddy** (ns07/ns08.domaincontrol.com) |
| `godeyes.ai/observe` | droplet | shared Grafana | — |

The landing sibling repo carries a `CNAME` file (`jobhuntwow.com`) for GitHub Pages, but the **live
app is served from the droplet, not Pages** — do not let Pages re-claim the domain.


## 3. Directory map — this PC

Working root: `C:\Python SW\Linkedin Scraper\jobhuntwow-app`

```
jobhuntwow-app/
├── jhw.py                     # THE orchestrator. One verb per operation. Start here.
├── deploy_direct.py           # SSH deploy engine (called by `jhw.py deploy`; you never run it directly)
├── docker-compose.yml         # LOCAL dev: backend(:8000) + frontend(:8090), 127.0.0.1 only
├── docker-compose.web.yml     # PROD: jhw-web (built ON the droplet), joins videodead_appnet
├── docker-compose.obs.yml     # LOCAL obs: Loki+Grafana+Promtail (`jhw.py obs`)
├── Dockerfile.web             # prod image (FastAPI + built React in one)
├── index.html                 # public landing served at /
├── backend/                   # FastAPI
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/                   # electronic.py (Tailor core), proxy.py (/v1 DO proxy), auth, settings…
├── frontend/                  # React + Vite  (src/pages/: Tailor.jsx, Electronic.jsx, Pipeline.jsx…)
│   ├── Dockerfile · nginx.conf · vite.config.js · package.json
├── agent/                     # the LOCAL APPLY SANDBOX (isolated browser + driver + bot)
│   ├── jhw.py                 # sandbox orchestrator (backend+sandbox up, then apply)
│   ├── agent.py               # apply entrypoint: route → tailor → drive → submit
│   ├── ask.py                 # Telegram ask-bridge (agent side)
│   ├── jhw_bot.py             # Telegram bot (the ONLY getUpdates poller)
│   ├── docker-compose.local.yml   # 4 services: jhw-browser, jhw-stagehand, jhw-agent, jhw-bot
│   ├── Dockerfile · bot/Dockerfile · stagehand-svc/Dockerfile
│   ├── flows/                 # ATS adapters: llm_driver.py, phenom.py, workday*.py, + test_*.py
│   └── userdata/              # candidate.md (screening answers), skills/ats-phenom/SKILL.md
├── deploy/
│   ├── caddy/jobhuntwow.caddy # the vhost block (marker-scoped; deploy re-appends it)
│   ├── fix_caddy.py           # runs ON the droplet: parses & repairs the shared Caddyfile
│   └── whoserves.py           # runs ON the droplet: proves who owns the vhost
├── obs/
│   ├── loki.yml · promtail.yml
│   └── grafana/provisioning/{datasources,dashboards}/ · grafana/dashboards/*.json
└── docs/
    ├── BIBLE.md               # ← this file
    ├── INFRASTRUCTURE.md      # DO + Docker + Loki/Grafana deep dive
    ├── TAILOR_LOGIC.md        # Tailor logic + interaction model
    └── tailor_core.py         # reusable, framework-free Tailor core
```

Sibling on this PC: `C:\Python SW\Linkedin Scraper` root = the **Colt** project (`ship.py`,
`deploy.py`, `assess-bot/`, `cassandra-bot/`, its own `CLAUDE.md`). Resumes live in
`C:\Users\feran\Downloads\Resumes and positions`.


## 4. Directory map — the droplet

```
/opt/jobhuntwow/              # THIS app's build context + runtime (project `jobhuntwow`)
├── docker-compose.web.yml    # scp'd from PC each deploy
├── Dockerfile.web · backend/ · frontend/ · index.html · deploy/caddy/…
└── .env                      # chmod 600, gitignored, written over SSH stdin — the ONLY secret store
/opt/colt-stack/              # the Colt project (separate) · /opt/shodan-skill inside its images
/opt/videodead/               # streaming stack; its Caddyfile is the SHARED one (bind-mounted)
```

Shared Docker objects jobhuntwow attaches to (never redefines):
- network `videodead_appnet` (external) — how Caddy reaches `jhw-web:8000`
- volume `colt-stack_colt_events` (external) — the log volume colt-promtail already tails


## 5. SSH & deploy — exactly how it works

**You never SSH by hand for changes.** Every droplet change is a committed artifact applied by ONE
command. SSH is for read-only diagnostics only.

### Connection (from `deploy_direct.py`)
- Target: `root@64.225.108.200`. Override via env `DROPLET_HOST`, `DROPLET_USER`, `SSH_KEY`.
- Key auto-detected from `~/.ssh/{id_ed25519,id_rsa,id_ecdsa}` (Windows OpenSSH needs `-i` explicit).
- The key is **passphrase-protected**, so `BatchMode` is deliberately OFF; on non-Windows a single
  `ControlMaster` connection is reused so you type the passphrase once.
- SSH opts always carry `ConnectTimeout=10`, `ServerAliveInterval=15`, `StrictHostKeyChecking=accept-new`.

### `python jhw.py deploy` does, in order:
1. tar the build context (`SHIP_FILES` + `backend/`, `frontend/`; skipping `node_modules, dist,
   __pycache__, .git, .vite, ssrtmp, out`) → scp to `/opt/jobhuntwow` in **ONE** ssh payload
   (secrets/`.env` sent separately over **stdin**, never argv, never a temp file).
2. droplet builds the image itself: `docker compose -p jobhuntwow -f docker-compose.web.yml build`.
3. `up -d --force-recreate` (never `--remove-orphans` — that would kill the neighbours).
4. wire the marker-scoped Caddy block via `deploy/fix_caddy.py` (rewrites the bind-mounted file
   in place — never `sed -i`, which breaks the mount inode).
5. verify by tagging a request and grepping jhw-web's access log (content match is NOT proof of
   routing).

**No GitHub, no GHCR, no CI.** `python jhw.py push` is source control only and is not required to deploy.

### Read-only diagnostics (safe):
`python jhw.py status` (is the public site our app), `python jhw.py diagnose` (who owns the vhost),
`python jhw.py logs`.


## 6. Ports & network exposure (the whole surface)

| Port | Host | Exposure | What |
|---|---|---|---|
| 443 | droplet | **public** | shared Caddy → `jhw-web:8000` (the ONLY public app surface) |
| 22 | droplet | public | SSH (deploy + diagnostics) |
| 8000 | local / droplet | 127.0.0.1 local · internal-only prod | FastAPI + the `/v1` DO proxy (holds the DO key) |
| 8090 | local | 127.0.0.1 | frontend (dev) |
| 3000 | local | 127.0.0.1 | Grafana (local obs) |
| 3100 | local | internal | Loki |
| 9090 | local | 127.0.0.1 | noVNC — watch the apply sandbox live |
| 9222 / 5900 | local | **internal only, never published** | Chrome CDP / VNC (exposed CDP = full remote control, no auth → critical) |
| 8081 | local | internal (shared netns) | Stagehand service |


## 7. Secrets — where they are, and are NOT

- **Only** in `.env` files: on this PC (per project) and at `/opt/jobhuntwow/.env` (chmod 600).
- `.gitignore` blocks `.env` and `*.env`, whitelists `!.env.example`, ignores `data/`. `gitleaks`
  runs in CI as a backstop.
- Never in an image (`COPY .env` forbidden), never in argv (agent gets `-e KEY` by name only), never
  logged.
- Key `.env` values (template in `.env.example`): `DO_INFERENCE_KEY` (the big one — server-side
  only), `DO_INFERENCE_BASE_URL` (`https://inference.do-ai.run/v1`), `AGENT_PROXY_TOKEN` (what the
  sandbox authenticates with — NOT the DO key), `QWEN_MODEL`, `JHW_MODEL_*`, `GRAFANA_PASSWORD`,
  Telegram `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.


## 8. Observability in one paragraph

Services emit one JSON line per event (`ts, service, evt, …`) to stdout **and** a file. LOCAL:
`jhw.py obs` runs Loki+Promtail+Grafana (`docker-compose.obs.yml`); Promtail tails
`agent/out/**/events.jsonl` read-only (no docker socket), Loki keeps 30 days, Grafana on
`127.0.0.1:3000` (admin / `$GRAFANA_PASSWORD`). PROD: `jhw-web` writes to
`/var/log/colt/jhw-web.log` on the shared `colt-stack_colt_events` volume, which colt-promtail
already ships to Loki→Grafana — we add only our dashboards. Full detail: `docs/INFRASTRUCTURE.md`.


## 9. First-five-minutes checklist for a new session

1. Read this file, then `CLAUDE.md` (hard rules — especially: bash-heredoc file writes + machine
   verify; ONE command per op; verify before claiming).
2. Working dir is `jobhuntwow-app`. Orchestrator is `python jhw.py <verb>`.
3. To change the droplet: edit the committed file, run `python jhw.py deploy`. Never hand-SSH a fix.
4. Local apply test: `python jhw.py apply "<job url>"` → watch at `http://localhost:9090/vnc.html`.
5. Secrets are in `.env` (not git). Droplet is `root@64.225.108.200`, app at `/opt/jobhuntwow`.
6. The Colt project (repo `electronic`, `ship.py`) is a DIFFERENT product on the same droplet — do
   not cross the streams.
