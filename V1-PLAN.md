# JobHuntWOW — V1 build plan (LinkedIn → Workday apply)

**Scope of V1:** one vertical slice, proven end-to-end against a real target:
Red Hat *EMEA AI Architect (m/f/d)* — LinkedIn job `4435446898` → Workday portal
`redhat.wd5.myworkdayjobs.com`. Ship the loop, then add ATS adapters one at a time.

Reference target (the V1 test fixture):
- LinkedIn: https://www.linkedin.com/jobs/view/4435446898/
- Workday: https://redhat.wd5.myworkdayjobs.com/jobs/job/Remote-Germany/EMEA-AI-Architect--m-f-d-_R-057546-2

---

## 1. The deciding constraint (why V1 is hybrid, not all-online)

The browser drives the candidate's **own authenticated LinkedIn session** and creates/uses
**their own Workday account**. LinkedIn and Workday both flag automation from datacenter IPs.
Running that browser in DigitalOcean = the candidate's LinkedIn gets restricted. So:

> The browser runs on the candidate's machine (residential IP, real session).
> The cloud does the thinking (LLM, tailoring, CRM, orchestration) but never touches the browser.

This is the same standing rule the repos already enforce — *"the LLM assists, it does not decide
side effects"* — applied to a new product.

```
CANDIDATE PC (Docker: jhw-agent)                     DO DROPLET (cloud, reuse cybergod stack)
┌────────────────────────────────────┐               ┌──────────────────────────────────────┐
│ Chromium + Playwright (stealth)     │  outbound WSS │ FastAPI backend (existing)           │
│  ├─ LinkedIn session (real cookies) │◄─────────────►│  ├─ /api/chat   Qwen (existing)      │
│  ├─ Workday adapter (fills form)    │   task/result │  ├─ /api/scout  → real LinkedIn scout │
│  └─ reports DOM + confirmations     │   messages    │  ├─ /api/apply  → dispatch to agent  │
│                                     │               │  ├─ tailor.py   resume/CL (LLM)      │
│ NO secrets decided here — executes  │               │  └─ store/CRM   Kanban + history     │
│ instructions, asks user to confirm  │               │ DeepSeek/Qwen via DO Serverless      │
└────────────────────────────────────┘               └──────────────────────────────────────┘
        residential IP, candidate's real browser              datacenter IP — text only
```

**Local agent packaging:** Docker only (`docker compose up` on the candidate PC). No native/venv
path — one container ships Chromium + Playwright + the agent.

**AI approach — LLM-driven, not selector-driven.** The agent does NOT rely on hardcoded CSS
selectors (LinkedIn/ATS mutate their DOM to break exactly that — our first selector scrape came
back empty for this reason). Instead, at every step the agent serialises the page (accessibility
tree + interactive elements, plus a screenshot when a vision model is needed) and asks an LLM for
the next action; deterministic code executes it and gates the final submit. We reuse
**`browser-use`** (Python + Playwright agent, OpenAI-compatible) rather than writing our own loop.
Because DO Serverless is OpenAI-compatible, the DO models drive it directly.

**The DO key never leaves the droplet.** The agent runs on the candidate PC but points at a thin
**OpenAI-compatible proxy on the droplet** (`/v1/chat/completions`, `/v1/models`) that injects the
DO key, routes each request to the right model by role, and logs every call. So the agent needs
only a per-candidate agent token, not your billing key.

---

## 2. The V1 flow, walked through Red Hat

1. **Candidate logs into the cloud portal** and starts the local `jhw-agent` (Docker) once. The
   agent imports their real logged-in LinkedIn session (cookie file, like `linkedin_verifier.py`).
2. **Scout** — candidate pastes/searches the LinkedIn job. Agent opens `linkedin.com/jobs/view/4435446898`,
   scrapes the JD (title, company, description, requirements). Cloud stores it.
3. **Tailor** — cloud LLM reads the JD + the candidate's profile (the LinkedIn "Save to PDF"
   export, e.g. `Profile.pdf`) and produces a tailored `resume.pdf` + `cover_letter.pdf` that
   surface the stakeholder value the JD asks for. Grounded only in real profile facts.
4. **Apply click** — agent clicks *Apply* on LinkedIn. Red Hat routes to the Workday portal.
5. **Workday adapter** takes over (see §4): creates/uses the candidate's Red Hat Workday account,
   uploads the tailored resume, autofills My Information / Experience / Education / screening
   questions from the profile. Missing-but-required fields → agent pauses and asks the candidate.
6. **Human gate** — agent fills everything, then **stops at Review**. The candidate clicks Submit
   (existing rule: *never auto-submit without an explicit user action*). Agent captures the
   confirmation number.
7. **CRM** — the application lands as a card in the Kanban ("Applied" column) with company, role,
   date, confirmation #, and links.

---

## 3. Components — new vs. reused

| Component | Status | Source to reuse |
|---|---|---|
| Stealth Chromium + cookie-session login | **reuse** | `Linkedin Scraper/linkedin_verifier.py` (`create_stealth_context`, `load_cookies`, `human_*`, `detect_challenge`) |
| DO/OpenAI-compatible LLM client (+fallback/retry) | **reuse/extend** | existing `backend/app/qwen.py`; add `backend/app/llm.py` role-routing |
| OpenAI-compatible proxy (`/v1/*`, key stays server-side) | **new** | `backend/app/proxy.py` in front of DO Serverless |
| `browser-use` LLM-driven page agent | **new (reuse lib)** | pip `browser-use`, pointed at the proxy — replaces hardcoded selectors |
| FastAPI + SSE + store skeleton | **reuse (exists)** | `jobhuntwow-app/backend/app/{main,store,scout}.py` |
| Zero-trust auth (email+pw+OTP), signed sessions | **reuse (V1.1)** | `Linkedin Scraper/colt_auth.py` + `webapp/backend/app/auth.py` (generalize `@colt.net` regex) |
| Retry/backoff state machine, per-item checkpoint | **reuse** | `oxford-dictionary-scraper/src/scraper.py` (`fetch_url`, checkpoint), `database.py` |
| **jhw-agent** local runner (Docker) | **new** | browser-use + Playwright + stealth context; points at cloud proxy |
| Per-ATS **hints** (task prompts + known quirks) | **new** | injected into the agent goal, not selectors |
| Resume/cover-letter tailoring (`tailor.py`) | **new** | uses LLM client + `pdf` skill for output |
| CRM Kanban board | **reuse/adapt** | `jobhuntwow.com/docker-jobhuntwow/frontend` Kanban components; existing `Pipeline.jsx` |
| Docker/Caddy deploy, GHCR CI, obs | **reuse** | `webapp/Dockerfile`, `docker-compose.web.yml`, `ship_web.py`, `obs/` |

---

## 3.5. Model routing (DO Serverless)

One proxy, one routing table (`backend/app/llm.py`). Each task gets the cheapest model that's
reliable for it; the agent driver gets the best tool-caller because a wrong click costs a real
application. All ids are exact DO Serverless slugs.

| Role | Model (DO slug) | Why |
|---|---|---|
| `driver` — browser navigation (LinkedIn + every ATS) | `anthropic-claude-4.6-sonnet` | Best tool-calling + vision + 200K ctx; reliability is the whole point |
| `vision` — screenshot fallback on hard pages | `nemotron-nano-12b-v2-vl` | Cheap 12B vision-language for routine "what's on screen" |
| `content` — resume + cover-letter writing | `deepseek-3.2` | Strong writer, far cheaper than Claude for long-form |
| `extract` — JD→requirements, profile→fields (JSON) | `llama3.3-70b-instruct` | Fast, cheap, solid JSON-mode structured output |
| `chat` — Hermes assistant | `deepseek-3.2` | Good general chat at low cost |

Cost note: the driver is the expensive path (many calls per application). V1 = correctness first
with Claude Sonnet; a later optimisation runs a cheap model (`nemotron-nano`/`llama3.3`) for
routine steps and escalates to Sonnet only when it stalls. Model ids live in env, so swapping
`driver` to Opus 4.8 or DeepSeek V4 is a one-line change.

## 4. The apply agent (LinkedIn handoff → Workday, LLM-driven)

Built on `browser-use` with the `driver` model. Not a per-tenant selector script — a goal
("apply to this job as this candidate") the agent pursues by reading each page. Because it reads
the page rather than matching selectors, the SAME agent handles LinkedIn, Workday, SuccessFactors,
Personio, etc.; per-ATS knowledge is injected as *hints* (task instructions + known quirks), not
brittle selectors. Optional recorded selector maps become a fast-path cache later.

Canonical Workday steps the agent must handle:
1. **Job page → Apply** (`Autofill with Resume` vs `Apply Manually`). Prefer Autofill with the
   tailored resume, then correct fields.
2. **Account** — sign in or create account (email + generated password). *Credential is generated
   per-company and stored in the vault — see §5.*
3. **My Information** — name, address, phone, contact prefs, "how did you hear about us".
4. **Experience** — work history, education, skills, languages, resume/CV upload, LinkedIn URL.
5. **Application Questions** — work authorization, relocation, notice period, sponsorship — the
   screening questions. Deterministic from profile where known; LLM-drafted then user-confirmed
   where not.
6. **Voluntary Disclosures / Self-Identify** — leave to candidate (sensitive; do not auto-fill).
7. **Review → STOP.** Human gate. Candidate submits.

Real selectors get captured during build against the Red Hat URL and stored as
`agent/adapters/workday.yml` (a versioned selector map, so a Workday UI change is a config edit,
not a code change).

---

## 5. Credentials & secrets (design note)

Each candidate gets a **generated per-company Workday login**. These are real secrets, so:
- Never in git, never in the JSON store (existing rule).
- V1: encrypted-at-rest vault on the cloud, per-user key; the agent pulls the one credential it
  needs at apply time over the authenticated channel. (Simplest secure option; can move to
  local-only vault later if preferred.)
- The email-fetch CRM automation (auto-linking interview emails to applications) is **V2**.

---

## 6. Build sequence

1. **`agent/` local runner** — lift `linkedin_verifier.py` stealth core into a standalone
   `jhw-agent` Docker service that connects out to the backend and runs tasks. Prove it opens
   LinkedIn with the candidate session and scrapes job `4435446898`.
2. **Real scout** — replace `scout.py` sample data with the agent-driven LinkedIn scrape,
   keeping the `{query,count,jobs,note}` shape.
3. **`tailor.py`** — JD + profile PDF → tailored resume + cover letter PDF (LLM + `pdf` skill).
4. **Workday adapter** — build `agent/adapters/workday.yml` + runner against the Red Hat URL,
   through fill; stop at Review.
5. **Human-gate wiring** — `/api/apply` dispatches to the agent, streams steps over SSE, waits
   for the candidate's explicit Submit.
6. **CRM Kanban** — applied jobs → cards; adapt the existing Pipeline page.
7. **Package & deploy** — agent Docker image + cloud via existing `ship_web.py`/CI.

Multi-user auth (colt_auth) folds in at step 5/7 as V1.1.

---

## 7. Guardrails (carried from existing CLAUDE.md rules)

- Never auto-submit an application without an explicit user click.
- The LLM writes text; deterministic code decides what gets filled, stored, or submitted.
- Secrets only in the vault / env, never in git or the JSON store.
- Frontend coerces unknown backend values to text (no white-screen on a shape change).
