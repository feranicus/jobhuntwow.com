# JobHuntWOW agent — A2Z guide (read this first)

## What this is
A self-service job-application robot. Given a LinkedIn job, it: (1) **scrapes** the JD, (2) **tailors**
the candidate's resume + cover letter to it, (3) **applies** — driving LinkedIn's Apply into the
company ATS (Workday etc.), filling every field from the candidate's real data, uploading the tailored
resume, and STOPPING at the Review page for a human to submit. It runs on the candidate's own machine
(their residential IP + logged-in LinkedIn), so it looks like them.

## Golden rule: deterministic first, LLM for governance
Do as much as possible with **deterministic Playwright** (fast, reliable, cheap, can't hallucinate).
Use the **LLM only** for: understanding a JD, writing prose, deciding an ambiguous field, or when a
deterministic step fails. Never drive the whole browser with an LLM — it's slow and it invents things
(e.g. it once tried to *create a LinkedIn account*). If a step can be a selector + a click, it must be.

## The pipeline
```
scrape (LLM, 1 pass)  ->  out/job.json
tailor (extract+content LLMs) -> renders templates/{resume,cover_letter}.html -> out/{resume,cover_letter}.pdf + fields.json
apply:
  STAGE 1  flows/linkedin.py  (PLAYWRIGHT, deterministic, NO LLM)
           - check LinkedIn is logged in (else: tell human to log in at noVNC, stop)
           - open the job, click Apply
           - detect Easy-Apply (stay on LinkedIn) vs external (company ATS opens; bring that tab to front)
  STAGE 2  agent.py + browser-use  (LLM governance, on the ATS/Easy-Apply form ONLY)
           - fill My Info / Experience / Education / screening from resume_data.json
           - ATS account uses a MINTED per-company password (out/credentials.json) — only on the ATS
           - unknown value -> ask_human (Telegram) ; upload resume via available_file_paths
           - STOP at Review; a human submits in noVNC. Never auto-submit.
```

## Runtime architecture
- One Docker container (the **cybergodai secure-browser sandbox**): supervisord runs Xvfb + x11vnc +
  **noVNC on :9090** + real **Google Chrome** (headful, CDP on :9222). Watch/*take over* at
  http://localhost:9090/vnc.html . The logged-in Chrome profile persists in the `jhw_chrome_data`
  volume — **log in to LinkedIn once**, it sticks.
- `flows/*.py` connect to that Chrome with Playwright `connect_over_cdp(:9222)` — deterministic layer.
- `agent.py` (browser-use, also over CDP :9222) — the LLM layer, used only for form fill.
- Backend on the droplet exposes an **OpenAI-compatible /v1 proxy** that injects the DO key and routes
  by role. The DO key never leaves the droplet; the agent uses a shared AGENT_PROXY_TOKEN.

## Models (roles -> DO slug, backend/app/llm.py; verify with `python jhw.py models`)
- `driver` deepseek-4-flash (fast form governance) · `driver2` nemotron-nano-12b-v2-vl (fast vision
  fallback) · `driver3` glm-5.2 (strong tool-caller, last resort). Apply runs the chain, restarting on
  the SAME live page if one stalls (max_failures=3 + step cap).
- `content` deepseek-v4-pro (writing) · `extract` deepseek-4-flash (JD->fields) · fallbacks deepseek-3.2.
- Slugs are env-overridable (JHW_MODEL_<ROLE>). Anthropic/OpenAI models exist but are premium tier.

## Files
- `jhw.py`      one ops CLI: backend | up | down | status | logs | scrape | tailor | apply | telegram | models
- `flows/linkedin.py`  deterministic LinkedIn (login check, open job, click Apply, detect ATS)
- `agent.py`    Stage-2 LLM form fill (browser-use), ask_human tool, model chain, human gate
- `tailor.py`   JD + resume_data.json -> tailored resume/cover-letter PDFs (extract+content LLMs)
- `ask.py`      human-in-the-loop channel (Telegram now; `web` seam for the V2 React cabinet)
- `templates/`  resume_data.json (master facts) + resume.html + cover_letter.html
- `Dockerfile`, `run-chrome.sh`, `supervisord.conf`, `docker-compose.yml`  the sandbox

## Adding a new ATS (the pattern — like the Oxford scraper)
1. Add `flows/<ats>.py`: a deterministic Playwright flow with a selector map + waits + retries for that
   ATS's steps (account, My-Info, Experience, Education, questions, resume upload, Review).
2. Fill known fields deterministically from `resume_data.json`. For a field with no known selector or an
   ambiguous screening question, call `ask.ask_human(...)` or hand that single step to the LLM.
3. Keep the human gate: never click final Submit.
Deterministic coverage should grow over time; the LLM is the fallback that shrinks as adapters mature.

## Guardrails (standing rules)
- Never auto-submit an application. Never create a LinkedIn account. ATS credentials are used only on
  the ATS domain, never on linkedin.com.
- LLM assists; deterministic code decides side effects. Secrets only in .env / out (gitignored), never git.
- Every operation is a `python jhw.py <cmd>` script; every change updates README + this guide (no shell blobs).
