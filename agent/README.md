# jhw-agent — LLM-driven LinkedIn/ATS agent (secure-browser sandbox)

Runs on the **candidate's machine** (their residential IP + logged-in LinkedIn session). Reuses the
**cybergodai secure-browser sandbox**: a Docker container running *real* Google Chrome inside a
virtual display (Xvfb + x11vnc + noVNC), so you can **watch and take over** the browser in a web
page. browser-use drives that Chrome over CDP; a DigitalOcean model (via the droplet proxy) decides
each action. The DO key never lives here.

## One entry point: `jhw.py` (no shell blobs — see project CLAUDE.md)
    python jhw.py up                 # build + start the sandbox (Chrome + noVNC)
    python jhw.py watch              # print the noVNC URL to watch/drive the browser
    python jhw.py scrape 4435446898  # read a LinkedIn job  -> out/job.json
    python jhw.py apply  4435446898  # drive Apply -> ATS, fill through Review (human submits)
    python jhw.py status             # container + health + noVNC URL
    python jhw.py logs               # follow container logs
    python jhw.py down               # stop the sandbox

Watch the browser live at **http://localhost:9090/vnc.html** while scrape/apply run.

## Architecture
- Container "jhw-agent": supervisord runs Xvfb :0 + x11vnc + noVNC(:9090) + Google Chrome (headful,
  CDP :9222). `agent.py` (browser-use) connects to that Chrome, injects LinkedIn cookies
  (storage_state), and asks a DO model — via the droplet `/v1` proxy — for each action.
- Model routing (backend llm.py): driver=anthropic-claude-4.6-sonnet, content=deepseek-3.2,
  extract=llama3.3-70b-instruct. Agent uses alias `jhw-driver`; the proxy maps it and holds the DO key.

## Prerequisites
1. Backend + proxy running (holds the DO key): from the repo root `docker compose up backend`.
2. `.env` here — JHW_PROXY_BASE (http://host.docker.internal:8000/v1) and AGENT_PROXY_TOKEN
   (matches the backend). See `.env.example`. `jhw.py up` warns if missing.
3. LinkedIn cookies — `jhw.py` auto-copies `../../linkedin_cookies.json` on first run; else export
   your logged-in session (JSON) to `agent/linkedin_cookies.json`.

## Files
- jhw.py            — ops CLI (build/run/scrape/apply/status/logs/down)
- agent.py          — connects to the sandbox Chrome via CDP, injects cookies, runs browser-use
- Dockerfile        — cybergodai sandbox + Python venv + browser-use
- run-chrome.sh, supervisord.conf — reused from cybergodai (display + VNC + Chrome stack)
- docker-compose.yml — ports 9090 (noVNC) / 9222 (CDP); mounts cookies, out/, agent.py
- flows/linkedin.py — deterministic LinkedIn login + open job + click Apply (Playwright, no LLM)
- flows/workday.py  — deterministic Workday driver: cookie/Apply/account/My-Information/resume (no LLM)
- scrape_job.py     — legacy selector-only scraper (kept for reference; superseded by agent.py)

## Human gate
`apply` fills everything up to the final Review page and stops — the candidate clicks Submit in the
noVNC window. The agent never auto-submits (project standing rule).

## Change log
- 2026-07-15  ENTERPRISE HARDENING — observability + DevSecOps + Secure by Design (see the CLAUDE.md
  standing rule "containers: Docker + Google best practices, Secure by Design, KISS").
  * **CRITICAL FIX — port exposure.** Chrome CDP :9222 was published on 0.0.0.0. CDP is UNAUTHENTICATED
    remote control of a browser holding a live LinkedIn session + ATS accounts — anyone on the LAN could
    drive it. Now 9222/9090/5900 are bound to 127.0.0.1 ONLY. Backend :8000 (the LLM proxy holding the DO
    key) and frontend :8090 likewise.
  * **Least privilege:** `no-new-privileges:true` on every service, `cap_drop: ALL` where the process
    allows, resource limits (agent 4g/2cpu, backend 1g/1cpu), healthchecks, bounded json-file logs.
  * **DevSecOps — `python jhw.py scan`:** Trivy (Aqua Security's OSS scanner) in a container, nothing to
    install. Scans dependency/OS CVEs + hardcoded SECRETS + Dockerfile/compose misconfiguration, then the
    built images. Review HIGH/CRITICAL before deploy.
  * **Observability — `python jhw.py obs`:** JSON events (agent/obs.py) -> Promtail -> Loki -> Grafana at
    http://127.0.0.1:3000, auto-provisioned datasource + "JobHuntWOW — Apply Pipeline" dashboard
    (applies started / reached Review / blocked / errors + a live log stream). Profile-gated: it never
    runs during a normal apply. obs.py SCRUBS secret-ish keys and truncates long values — no secrets/PII
    in events. Promtail does NOT get the docker socket (root-equivalent); it reads the event files
    read-only.
  * events.jsonl + wd_*.json added to .gitignore.
  * KNOWN TRADE-OFFS (KISS rule 8 — write down what we skipped and why): (a) base images pinned by TAG
    not digest — digest pinning is a real maintenance burden for a local tool, and `jhw.py scan` catches
    the CVEs instead; (b) supervisord runs as root in the agent image because it must spawn Chrome as
    chrome-user — the container is the boundary, hence the strict port/caps posture; (c) Chrome runs
    --no-sandbox (unavoidable in-container) — same reasoning; (d) backend :8000 is bound to 127.0.0.1 and
    jhw-agent reaches it via host.docker.internal (Docker Desktop proxies to the host loopback) — if the
    LLM proxy ever fails to connect, that binding is the first thing to check.
- 2026-07-14  RESEARCH-DRIVEN REWRITE — see **RESEARCH_ATS_AUTOMATION.md** (verified against primary
  sources + 4 real Workday repos). All 5 researched fixes implemented in flows/workday.py:
  1. **RESUME PARSE WAIT (the bug that stalled every run)** — Workday parses the resume SERVER-SIDE.
     We now delete any existing attachment first (uploads stack otherwise), upload to the hidden input
     (`set_input_files` has zero actionability checks — the documented path), then WAIT until it's
     attached (`delete-file` appears) BEFORE clicking Save and Continue. Previously we clicked while
     Workday was still parsing, so the required-file validation never cleared -> 9 empty loops -> paused.
  2. **DEEP-LINK** to `{job}/apply/applyManually` — skips `adventureButton` (reference repos had to click
     it twice; flaky). Workday bounces to auth then returns.
  3. **SELECTOR MAP AS DATA** — flows/selectors/workday.json, FALLBACK CHAINS. Tenants run different
     Workday UI versions: two working repos use different sign-in submits (`signInSubmitButton` vs
     `click_filter`), and our own dump proves Red Hat runs a NEWER UI (`pageFooterNextButton`,
     `add-button`, `applyFlowMyExpPage`) than the repos (`bottom-navigation-next-button`, `Add`,
     `myExperiencePage`). Both kept. Never hardcode tenant WIDs.
  4. **SELECTOR = RUNTIME ASSERTION** — a repeating validation error or an unknown page HALTS and pings
     Telegram. We never guess. Kills "hallucinated success" (the #1 agent failure mode: the user thinks
     they applied and didn't).
  5. **LLM ONLY FOR FREE TEXT** — the LLM never drives the browser. Evidence: Skyvern 64.4% on
     write-heavy WebBench (best-in-class, still poor) + 4-5 min for 6 fields; browser-use's 89%
     WebVoyager invalid by their own admission ("removed 55 tasks", "not actually testing correct
     things"); OpenAI DISCONTINUED Operator ("computer-use models don't yet work reliably enough in
     production"). browser-use + page-agent REMOVED from the Workday path. page-agent is architecturally
     wrong for third-party ATS by its own docs ("apps you own, not external or locked-down sites").
  Also: CSP is a NON-ISSUE for Playwright/CDP (`page.evaluate` runs outside CSP — proven by Playwright's
  own test suite), Workday has NO shadow DOM (Canvas Kit = React+Emotion), and there is NO Workday apply
  API (browser automation is mandatory; but `POST /wday/cxs/{tenant}/{site}/jobs` is clean JSON for Scout).
  FIXED: null bytes were being appended to agent.py (757) and flows/workday.py (2604) by file writes —
  Python cannot import a file containing null bytes. All files stripped + compile-verified.
- 2026-07-14  ALIBABA PAGE-AGENT wired in + ONE command + consent-banner rule.
  * flows/page_agent.py injects Alibaba page-agent (https://github.com/alibaba/page-agent) — an
    in-page, text-DOM GUI agent (NO screenshots, NO vision) — into the ATS tab over CDP and drives it
    with OUR DO proxy (bring-your-own-LLM). It's now the fallback for NON-Workday ATS (Taleo,
    SuccessFactors, Personio, HiBob, Greenhouse, Lever, iCIMS). Backend CORS is already `*`, so the
    in-page LLM fetch to the proxy works. Config via JHW_PAGEAGENT_CDN / JHW_PAGEAGENT_MODEL.
  * ONE command: `python jhw.py apply <job>` now brings the WHOLE stack up itself (backend + /v1 proxy
    AND the browser sandbox, then waits healthy) via _ensure_stack() — no separate `jhw.py backend`
    step. (`jhw.py backend` still exists to force a rebuild after editing llm.py.)
  * CONSENT BANNER RULE (the "By clicking the checkbox I consent…" thing the old LLM kept fumbling):
    now deterministic. flows/workday.py `_check_consent()` ticks the consent/terms/privacy checkbox
    (Workday id first, else any visible checkbox whose label matches consent|agree|terms|privacy|
    acknowledge|certify) BEFORE Create Account / Submit; flows/autofill.py does the same on every page.
    For page-agent (non-Workday) the task prompt tells it to tick consent checkboxes.
  * Workday sign-in HARDENED: switch to the Sign-In view and click Sign In / Create Account by ARIA
    role+text (get_by_role) when the per-tenant data-automation-id doesn't match — this was why Red
    Hat's sign-in never actually submitted. NOTE: the account must exist with the STORED password;
    for a company whose account was created in an earlier run with a lost password, reset it once via
    "Forgot your password?" then `python jhw.py atspw <host> '<newpass>'`.
- 2026-07-14  V2 — BROWSER-USE REMOVED FROM WORKDAY (no screenshots / no vision / no agent loop).
  Root cause of the slowness + failures: browser-use captured a screenshot every step, needed vision
  models, and its stuck LLM emitted malformed JSON and NEVER called ask_human (so no Telegram). New
  path is fully deterministic: flows/autofill.py is a generic "password-manager" engine that maps the
  candidate profile onto any form's fields by name/id/autocomplete/label heuristics (Workday/Taleo/
  SuccessFactors/Personio/HiBob/Greenhouse/Lever). flows/workday.py now drives the ENTIRE flow
  (cookie -> Apply -> account -> page loop: autofill + resume + Next) to the Review page with ZERO
  browser-use; only genuine free-text/choice questions go to a TEXT-ONLY LLM (agent.llm_answer, role
  jhw-answer) — no browser driving by the LLM at all. ask.notify() now pings Telegram on every
  stop/pause/stuck so the human always hears about it. Models switched to reliable instruct models
  (answer/extract/chat=deepseek-3.2, content=deepseek-v4-pro; browser-use fallback chain, only for
  non-Workday ATS = qwen3.5-397b -> glm-5.2 -> deepseek-3.2). This is the Alibaba page-agent
  philosophy (in-page text DOM, no screenshots) but deterministic, so it's faster: the structured
  fields cost ZERO LLM calls. NOTE: Workday's per-tenant question selectors may need one live tuning
  pass. Requires `python jhw.py backend` (llm.py is baked into the backend image) before apply.
- 2026-07-14  ACCOUNT-GATE FIX (this is what made the LLM "go berserk"): apply_context() minted a NEW
  random password every run, so a second apply to a company whose account already existed did
  Create Account -> "email exists" -> Sign In with the wrong (fresh) password -> stuck on Create
  Account -> the LLM inherited that and thrashed. Now flows/workday.py keeps STABLE per-host
  credentials in out/ats_accounts.json (create-once, sign-in-forever); if the account exists but the
  stored password is wrong/absent it asks the candidate ONCE via Telegram (ask_human) for the
  password (or to reset it via "Forgot your password?"). The deterministic driver now OWNS the whole
  account gate and, if it cannot get past it, STOPS and returns needs_llm=False — agent.py no longer
  hands a broken login page to the LLM. out/ats_accounts.json is gitignored (holds secrets).
- 2026-07-14  FAST HYBRID — deterministic Workday driver (flows/workday.py). Workday tags every field
  with a stable `data-automation-id`, so the cookie banner, Apply -> Apply Manually, per-company
  account (create, else sign-in), My-Information (legal name/address/phone) and resume upload are now
  driven by Playwright with NO LLM and NO vision — killing the slow browser-use wandering (the old
  run burned ~12 LLM steps, each up to 75s, just to reach sign-in). agent.py `apply` detects Workday
  from the ATS url and runs workday.drive() first; the LLM (Stage 2) then only finishes the leftover
  screening questions / EEO / consent on the current page and STOPS at Review. Non-Workday ATS still
  use the LLM chain; if Stage-1 can't find the Apply button it falls back to a full LLM apply. Also
  broadened LinkedIn Apply detection (role/text locators) in flows/linkedin.py. Pattern follows AIHawk
  (deterministic steps, LLM only for free-text) — next candidate for speed: Alibaba page-agent
  (in-page text-DOM agent) for arbitrary non-Workday ATS.
- 2026-07-14  ONE orchestrator + auto-login: `python jhw.py run <job>` does A->Z (backend + sandbox
  + health wait + login + scrape + tailor + apply). flows/linkedin.py.ensure_login() auto-signs in
  (LinkedIn-native creds, or best-effort Google), and if that fails pings via Telegram and waits
  for a one-time manual noVNC login (persists after). `jhw.py login` runs just the login step.
- 2026-07-14  DETERMINISTIC-FIRST pivot (+ AGENT_GUIDE.md A2Z doc): LinkedIn navigation is now
  Playwright (flows/linkedin.py) — login check, open job, click Apply, detect Easy-Apply vs
  external ATS — NO LLM, so it can't create a LinkedIn account. LLM (browser-use) only fills the
  ATS/Easy-Apply form (Stage 2). ATS creds used ONLY on the ATS. Switched to FAST models
  (driver=deepseek-4-flash). Added playwright to the image; mounted flows/.
- 2026-07-14  Model research: driver chain = Kimi K2.6 (VLM, best agentic tool-call stability) ->
  Qwen3.5-397B (top OSWorld GUI agent, multimodal) -> GLM-5.2 (best BFCL tool-calling, text last
  resort). content=deepseek-v4-pro, extract=deepseek-4-flash. Slugs verified against `jhw.py models`.
- 2026-07-14  Session persistence + optional auto-login: logged-in Chrome profile now persists in
  a Docker volume (jhw_chrome_data) — log in ONCE in noVNC, stays logged in across restarts.
  Optional LINKEDIN_EMAIL/PASSWORD (preferred) or GOOGLE_EMAIL/PASSWORD enable agent re-login on a
  login wall; 2FA/CAPTCHA is routed to the candidate via ask_human. (Google discouraged: master
  account, blocks automation, and creds reach the LLM API when a login page is read.)
- 2026-07-14  Human-in-the-loop