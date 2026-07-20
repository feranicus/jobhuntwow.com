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

## Deploy

Runs as Docker on the droplet behind the shared Caddy at **`app.jobhuntwow.com`**.
Step-by-step (DNS, Caddy vhost, compose) in **[DEPLOY.md](DEPLOY.md)**.

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
