# JobHuntWOW — architecture

JobHuntWOW is deliberately split so each half does one thing well and can ship independently.

## The two halves

### 1) `jobhuntwow.com` — the tracker (zero-backend)
```
Gmail  ──►  Google Apps Script (CRON every 15 min)  ──►  Google Sheets  ──►  React dashboard (GitHub Pages)
            doPost() SSO + updates · doGet() tenant data · AutomationTask() Gmail parse
```
- Parses interview emails (company, role, stage, date, HR contact, sentiment, next step) into Sheets.
- The dashboard is a static React/Vite build **served from the droplet** by the shared videodead Caddy
  (`root * /srv/jobhuntwow; file_server`), deployed by **`deploy_jobhuntwow_caddy.py`**. DNS A-records
  `jobhuntwow.com` → the droplet (64.225.108.200, AS14061 DigitalOcean). The repo's GitHub-Pages
  workflow is **legacy/unused** now.
- No app server to run or patch — Google Apps Script is the backend. Source lives in the `jobhuntwow.com`
  repo (`apps-script/Code.gs`, `frontend/`); the built static files are staged to `/opt/jobhuntwow/site`.
- Observability: `deploy_jobhuntwow_obs.py` turns on Caddy JSON access logs for the site → promtail →
  Loki → the provisioned **"JobHuntWOW"** Grafana dashboard.

### 2) `jobhuntwow-app` — the interactive cabinet (this project)
```
Browser
   │  https://app.jobhuntwow.com
   ▼
videodead Caddy  ──►  frontend (nginx :80, serves the Vite build)
                        │  location /api/*  ──►  backend (FastAPI :8000)
                        │                          ├─ qwen.py   ──►  DO Serverless Inference (Qwen, OpenAI-compatible)
                        │                          ├─ scout.py  ──►  job search (v0.1 samples → real scraper)
                        │                          └─ store.py  ──►  JSON store in DATA_DIR (Docker volume jhw_data)
                        └─ static assets
```

## Request flows
- **Chat:** frontend `POST /api/chat` → backend `qwen.chat_stream()` → streams tokens back (SSE) from DO Inference.
- **Models:** `GET /api/models` → `qwen.list_models()` (auto-picks first model if `QWEN_MODEL` blank).
- **Scout:** `POST /api/scout {query,location,remote}` → `scout.search()` → `{query,count,jobs,note}`.
- **Apply:** `POST /api/apply` → stub; the ATS agent lands here (never auto-submits without user action).
- **Connections:** `GET|POST /api/connections` → per-user integration settings in the store.

## Containers (`docker-compose.yml`)
- `backend` — FastAPI, `expose: 8000`, env from `.env`, data on volume `jhw_data`.
- `frontend` — nginx serving the React build on `8090:80`, and proxying `/api` → `backend:8000`
  (`frontend/nginx.conf`). Only the frontend is published; the backend stays internal.

## Trust & data
- Only secret is `DO_INFERENCE_KEY` (DO Serverless Inference). No user passwords stored by the app itself.
- All persistent data is JSON under `DATA_DIR` (a named Docker volume) — easy to back up / wipe.
