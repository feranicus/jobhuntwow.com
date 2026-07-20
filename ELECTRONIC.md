# Electronic — the JobHuntWOW SaaS (DO, 24/7, multi-tenant)

Renames `jhw-bot` -> **Electronic**. It moves OFF the laptop and INTO DigitalOcean, with the exact
shape of cybergod.ai: web portal + Telegram, same DO LLMs, 24/7, many customers.

## Split (hybrid)
```
  CLOUD (DigitalOcean, 24/7, multi-tenant)          LOCAL (per candidate, only if they automate applying)
  ┌──────────────────────────────────────┐          ┌──────────────────────────────────┐
  │ www.jobhuntwow.com                   │          │ jhw-browser  (Chrome + noVNC)    │
  │  • portal: signup/login + 6-digit OTP│          │ jhw-stagehand (AI fallback)      │
  │  • private space per customer        │  artifacts│ Playwright adapters (per ATS)   │
  │  • chat GUI (Cassandra-style)        │ ────────▶│ reads candidate.md + PDFs        │
  │  • Telegram bot (Electronic)         │          │ residential IP, user's machine   │
  │  • JD in  -> resume+cover OUT (docx/pdf)        └──────────────────────────────────┘
  │  • DO serverless LLMs (our proxy)    │
  └──────────────────────────────────────┘
```
Applying stays LOCAL on purpose (residential IP + the candidate's own sessions). Everything a customer
touches — account, chat, documents — is cloud and always-on.

## Reuse map (do NOT rebuild what cybergod already proved)
| Need | Take from | Change required |
|---|---|---|
| 6-digit OTP by email (Gmail API; SMTP is blocked on the droplet) | `colt_auth.py` | keep as-is |
| Signed session cookies | `webapp/backend/app/auth.py` | keep as-is |
| Per-tenant SQLite store keyed by email | `webapp/backend/app/store.py` | new tables |
| Portal shell: Login / Cabinet / Assistant / History / Landing / Privacy | `webapp/frontend/src/pages/*` | rebrand + new pages |
| Chat assistant (LLM over the portal) | `webapp/backend/app/assistant.py` | new system prompt |
| Telegram bot | `cassandra-bot/cassandra_bot.py` | new commands, multi-tenant |
| Deploy: GHCR + Actions + Caddy vhost + TLS | `ship_web.py`, `deploy_web_direct.py`, `deploy/caddy/*` | new vhost |
| DO LLM routing + OpenAI-compatible proxy | `jobhuntwow-app/backend/app/{llm,proxy}.py` | already ours |
| Cabinet skeleton (Dashboard/Scout/Pipeline) | `jobhuntwow-app/frontend/src/pages/*` | already ours |

## What is genuinely NEW (not a copy)
1. **Open self-signup auth.** cybergod is an allowlist + ONE shared password. Electronic is
   *any email* + *a password the customer sets* + OTP. So we need a real `users` table
   (email, password_hash (scrypt/bcrypt), created_at, verified_at) and signup/reset flows —
   `colt_auth`'s allowlist logic is replaced, its OTP + lockout logic is kept.
2. **Multi-tenancy everywhere.** Every row, file and artifact is scoped by `user_id`.
   Per-user object storage: `candidate.md`, uploads, generated documents, application history.
3. **JD ingestion from a link** (LinkedIn / Glassdoor / Indeed / any).
4. **Artifact generation in DOCX *and* PDF** (not Markdown).
5. **Telegram ↔ portal account linking** (one bot, many tenants; a code links a chat to an account).

## ⚠ The one hard constraint to decide up front — JD links
LinkedIn, Indeed and Glassdoor are aggressively bot-protected and, from a **DigitalOcean datacenter
IP**, server-side fetching of a job URL will frequently be blocked, rate-limited or require login.
This is the same anti-bot reality that keeps the *applying* browser local. Practical tiers:
- **Always works:** customer pastes the JD text (or uploads the posting) -> 100% reliable, zero risk.
- **Usually works:** company career pages, Greenhouse/Lever/SmartRecruiters/Ashby public JSON feeds
  (documented, unauthenticated) -> parse cleanly.
- **Unreliable from the cloud:** LinkedIn / Indeed / Glassdoor URLs. Options if we want them:
  (a) the customer's LOCAL container fetches the JD (their IP, their session) and posts it up —
      recommended, reuses what we already have; (b) a paid scraping/proxy provider; (c) paste fallback.
Recommendation: ship paste + public feeds first, add (a) local-fetch for LinkedIn — it is free,
already built, and legal-safe.

## Stages (each independently shippable)
- **S1 — Auth + tenancy.** users table, signup + password + OTP, sessions, per-user store, Login/Signup
  pages. Definition of done: two different customers log in and see only their own space.
- **S2 — Electronic core.** JD in (paste/link/feed) + resume/LinkedIn/cover upload -> LLM tailoring on
  our DO models -> **resume.docx/.pdf + cover_letter.docx/.pdf** + per-user `candidate.md`.
- **S3 — Chat + Telegram.** Cassandra-style chat in the portal; Telegram bot with account linking.
- **S4 — Ship.** Dockerfile, GitHub Actions -> GHCR -> droplet, Caddy vhost + TLS, DNS for
  `www.jobhuntwow.com`, healthchecks, observability. 24/7.

## Ops notes carried over from cybergod (do not relearn the hard way)
- SMTP is blocked outbound on the droplet -> OTP email MUST go via the **Gmail API**.
- Deploy is **CI -> GHCR -> droplet**; never hand-edit the droplet, never `--remove-orphans` on a
  shared compose project.
- Secrets only as env/GitHub secrets; never in git.
- The one irreducible human step: DNS A-record for jobhuntwow.com -> the droplet.
