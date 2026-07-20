# JobHuntWOW apply stack — Playwright (deterministic) + Stagehand (AI)

The 80/20 architecture the benchmarks recommend: DOM-driven Playwright for everything predictable,
AI only for the parts that vary. Reuses what we already built — the real recordings and the skills.

```
              apply(job_url)
                    |
          detect ATS from the URL
                    |
    +---------------+-----------------+
    | KNOWN ATS (recorded)            | UNKNOWN ATS / a selector broke
    v                                 v
 Playwright adapter  (flows/*.py)   Stagehand sidecar (flows/stagehand.py -> :8081)
 built from agent/recordings/*      observe() -> concrete selector -> act()
 ZERO LLM, ~seconds                 or agent() for a whole form, guided by SKILL.md
    |                                 |
    +--------> persist the learned selector into the ATS map <-------+
                    |
        next application on that ATS is deterministic again (no LLM)
```

## Layers
| Layer | What | Where |
|---|---|---|
| Deterministic | Recorded Playwright adapters — Workday, Greenhouse, iCIMS, Easy Apply | `flows/workday_gevernova.py`, `flows/greenhouse.py`, `flows/icims.py`, `flows/easyapply.py` (from `recordings/`) |
| AI fallback | Stagehand `observe`/`act`/`extract`/`agent` over HTTP | `stagehand-svc/server.ts` + client `flows/stagehand.py` |
| Knowledge | The playbook + the answer DB | `userdata/skills/ats-workday/SKILL.md`, `.../candidate_profile.md` |
| Browser | Real Chrome + noVNC, residential IP | `jhw-browser` container, watch at http://localhost:9090 |
| Chat | Telegram (ask-once, notify, trigger) | `jhw-hermes` container |

## Why this is fast
- Known ATS = pure Playwright: **no LLM call at all**.
- Unknown/broken = one `observe()` returns a **concrete selector**; we use it AND save it, so the next
  candidate on that ATS runs deterministically. First encounter pays the LLM cost, everyone after is free.
- Resume upload is Playwright `setInputFiles` (`/upload`) — no LLM, no native file picker, no human.

## Services (all local, isolated)
- `jhw-browser` — Chrome + noVNC (owns the network namespace; CDP stays on loopback).
- `jhw-stagehand` — AI sidecar, shares that netns so it drives the SAME Chrome at `127.0.0.1:9222`.
  LLM = our DO proxy (`gpt-oss-120b`), only for the fuzzy bits.
- `jhw-hermes` — Telegram chat / ask-once / notifications.

## Run
```bash
python jhw.py local          # ONE command: frees ports, backend, browser + stagehand + chat
```
Health check the AI layer: `curl 127.0.0.1:8081/health` (from inside the browser netns) or
`docker exec jhw-stagehand wget -qO- 127.0.0.1:8081/health`.

## Sidecar API (what Python calls)
`POST /goto {url}` · `POST /observe {instruction}` -> actions with selectors ·
`POST /act {instruction|action}` · `POST /extract {instruction, schema}` ·
`POST /agent {instruction, system, maxSteps}` · `POST /upload {path, selector}`

## Not yet verified (first-run items)
- Stagehand's exact local-CDP attach option (`localBrowserLaunchOptions.cdpUrl`) — confirm on first boot.
- `node --experimental-strip-types` runs `server.ts` directly (Node 22); if it complains, precompile.
- Wiring the router to call `flows/stagehand.py` on adapter failure is the next step.
