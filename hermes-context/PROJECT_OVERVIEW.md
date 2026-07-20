# JobHuntWOW — Project Overview (A→Z, for Hermes)

## What it is
An automated job-application system. One input (a LinkedIn job) → it scrapes the JD, tailors the
resume + cover letter to it, then drives the apply flow (LinkedIn Easy Apply or an external ATS) and
fills every field, stopping at the Review page for the human to submit. Product promise: **zero manual
data entry**; the LLM writes text, deterministic code (or a careful agent) does the clicking; nothing
is auto-submitted.

## Architecture
- **Frontend:** React (Vite) cabinet.
- **Backend:** FastAPI. Holds the DigitalOcean Serverless Inference key and exposes an OpenAI-compatible
  proxy at `/v1` (`backend/app/proxy.py`) — transparent passthrough to DO, only rewrites the model name;
  auth is a bearer token `AGENT_PROXY_TOKEN` (NOT the DO key). Role→model routing in `backend/app/llm.py`.
- **Agent sandbox (`agent/`):** a Docker container (`jhw-agent`) running Chromium + noVNC + Chrome CDP,
  secure-by-design (all ports bound to 127.0.0.1). Playwright adapters drive the browser.
- **Telegram assistant (`agent/jhw_bot.py`):** a Cassandra-style LLM bot; also the human-in-the-loop
  channel via a file "ask-bridge" (`agent/out/asks/`) so exactly one process polls Telegram.
- **Observability:** JSON events → Promtail → Loki → Grafana (profile-gated). **DevSecOps:** Trivy scans.

## How it runs — ONE command each
`jhw.py` is the single entry point (no shell blobs). Key verbs:
- `python jhw.py run <jobid>`   — A→Z: backend + sandbox + login + scrape + tailor + apply.
- `python jhw.py backend`       — (re)start backend + `/v1` proxy on 127.0.0.1:8000.
- `python jhw.py up | status | logs | down` — sandbox lifecycle; noVNC at http://localhost:9090.
- `python jhw.py bot [--logs]`  — the Telegram assistant.
- `python jhw.py scrape|tailor|apply <jobid>` — individual stages.
Local ports (127.0.0.1 only): backend 8000, frontend 8090, noVNC 9090, VNC 5900, Chrome CDP 9222, Grafana 3000.

## The apply pipeline
`agent/agent.py::run_apply` →
1. `flows/linkedin.py::prepare_application` (deterministic Playwright) opens the job, clicks Apply,
   follows the external-site new-tab hop, returns the ATS url + apply type.
2. Router dispatches by ATS to a recorded adapter:
   - `easy_apply` → `flows/easyapply.py`
   - Workday (GE Vernova) → `flows/workday_gevernova.py`  (generic Workday → `flows/workday.py`)
   - greenhouse.io → `flows/greenhouse.py`  ·  icims.com → `flows/icims.py`
   - unknown → `flows/llm_driver.py` (generic text-DOM LLM driver — last resort, unreliable).
3. Each adapter fills from the Candidate Profile, asks the human (ask-bridge) for anything missing,
   and STOPS at Review. Recordings (real selectors) live in `agent/recordings/`.

## Models (chosen by evidence — `model-selection-toolkit/`)
Bake-off on the real action-JSON task: all candidates produced valid JSON, so latency + vendor-diversity
decided. Chain in `backend/app/llm.py`: **deepseek-3.2 → mistral-3-14B → llama-4-maverick** (all sub-1.1s,
three vendors). Rejected for the driver loop: glm-5.2 (27.8s), qwen3.5-397b (91.5s). DO tier serves
open-weight models only (Anthropic/OpenAI = 403); those are fine for prose but weak at agentic tool-calling.

## Hermes integration (current direction)
We are testing **Hermes Agent** (Nous Research) as the browser-driving brain because it self-improves,
has memory + a user profile, drives a real browser, and talks over Telegram — matching the "ask once,
remember, copy-paste everywhere" goal better than the hand-built adapters.
- **Model:** started on Nous Portal free tier (`tencent/hy3:free`) — it DOES call tools. Fallback ladder:
  our DO proxy (`provider: custom`, `base_url http://127.0.0.1:8000/v1`, model `deepseek-3.2`,
  `agent.tool_use_enforcement: true`), then OpenRouter, then Nous Plus ($20/mo).
- **Browser:** `/browser connect` in the Hermes CLI = a visible local Chrome you watch live (best for
  test/inspect). Cloud browser (Browser Use) is paid and has no logged-in session.
- **Telegram:** Hermes's own gateway becomes the single bot (retire `jhw_bot` to avoid two pollers).
- **Config lives at** `~/.hermes/config.yaml` + `~/.hermes/.env` (Windows: `%USERPROFILE%\.hermes`).
- Hermes auto-created a `fill-job-application` skill during the GE Vernova run (self-learning working).

## Current state (as of this handoff)
- GE Vernova (Workday, careers.gevernova.com → gevernova.wd5.myworkdayjobs.com) is the live test.
- Hermes on the free model handled the careers→Workday new-tab hop, created the `evgeny@s4biz.io`
  account (no email verification needed), completed Step 1 (My Information) and Step 2 (My Experience),
  and reached the required Resume/CV upload. Next: upload CV → Application Questions → Review (STOP).

## Standing rules (from CLAUDE.md)
- KISS + full automation; every operation is a re-runnable script, documented in the same change.
- **Verify everything** — never claim a fix without proof (compile/parse/grep the evidence).
- **Never auto-submit**; stop at Review. **Never invent data.** Absence of evidence is not a finding.
- **Secrets never in git or logs** — DO key, `AGENT_PROXY_TOKEN`, ATS passwords live only in env/memory.

## Repo map (essentials)
```
jobhuntwow-app/
  jhw.py? (agent/jhw.py)         # ops entry point
  backend/app/{proxy.py,llm.py,main.py,qwen.py}
  agent/agent.py                 # apply orchestrator + router
  agent/jhw_bot.py, agent/ask.py # Telegram assistant + ask-bridge
  agent/flows/                   # linkedin, workday, workday_gevernova, greenhouse, icims, easyapply, llm_driver
  agent/recordings/              # real Playwright recordings (selectors ground truth; gitignored)
  agent/templates/resume_data.json  # candidate structured data
  model-selection-toolkit/       # probe_models.py, compare_models.py, MODEL_SELECTION.md
  hermes-context/                # THESE docs
```
