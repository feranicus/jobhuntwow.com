# JobHuntWOW / "Electronic" — Complete Engineering Handover

**What this document is.** Everything needed to pick this system up cold, or to port its ideas into
another project: the architecture, every significant file and its purpose, the doctrines that govern
how decisions are made, and — most importantly — **why** each rule exists, with the incident that
produced it. Nothing here is aspirational. Every "why" is traceable to a measured failure.

**Proven end to end, 2026-08-17 12:13 UTC+1:** a real application submitted to OKX via Greenhouse in
**27 seconds**, ten fields plus two documents, every answer from the candidate's own recorded choices,
success claimed only on the site's own confirmation page
(`job-boards.greenhouse.io/okx/jobs/7699600003/confirmation` — "Thank you for applying.").

**Status at time of writing:** 2026-08-17. ~14,500 lines in `agent/flows`, ~6,300 in `backend/app`,
20 self-test suites gating every run, 5 compiled ATS knowledge files.

---

## 1. What the system does

A candidate gives one input — a job URL. The system:

1. resolves the ATS behind it,
2. tailors a CV and cover letter to that job description,
3. drives the real application form in a real browser,
4. answers screening questions from the candidate's own recorded/declared answers,
5. asks the candidate on Telegram when it genuinely cannot decide,
6. submits, and only claims success on the site's own confirmation.

**Product promise: zero manual data entry.** Every design decision below follows from that.

### The one command

```
python jhw.py apply "<job url>"
```

That verb does *everything* itself: brings up the backend, brings up the browser sandbox, waits for
Chrome's CDP to answer, runs all 20 self-tests, then applies. **Never hand an operator a list of
commands.** If a verb needs a prerequisite, the verb does it. This is a hard rule and the most
frequently violated one.

Other verbs: `local`, `local-down`, `bot`, `doctor`, `logs`, `record`, `models`, `scan`, `obs`,
`status`, `tailor`, `scrape`, `atspw`, `inspect`.

`apply` does, in order: bring up the backend + sandbox and wait for CDP → **compile the operator's
recordings into knowledge** → run all 20 self-test suites → apply. Any of the first three failing stops
the run before a document or a Shodan/inference credit is spent.

---

## 2. Architecture

### 2.1 Two deployments, one codebase

| | Local (the engine) | Cloud (the product) |
|---|---|---|
| What | Drives real ATS forms | Web cabinet at `app.jobhuntwow.com` |
| Why local | A browser that logs into real accounts must not run on a shared host | Users need a UI |
| Containers | `jhw-browser`, `jhw-stagehand`, `jhw-agent`, `jhw-bot` | `jhw-web` (frontend nginx + FastAPI) |
| Brains | DigitalOcean Serverless Inference, via the backend proxy | same |

### 2.2 The local stack (`agent/docker-compose.local.yml`)

```
┌─────────────────────────────────────────────────────────────────┐
│ jhw-browser    Chromium + Xvfb + x11vnc + noVNC                 │
│                headful, persistent profile volume               │
│                CDP on 127.0.0.1:9222  (NEVER 0.0.0.0)           │
│                noVNC on 127.0.0.1:9090  ← the operator watches  │
└───────────────▲─────────────────────────────────────────────────┘
                │ shares the browser's NETWORK namespace
┌───────────────┴──────────────┐  ┌──────────────────────────────┐
│ jhw-agent                    │  │ jhw-stagehand                │
│ the driver: Playwright over  │  │ Stagehand observe(), used    │
│ CDP, all of flows/           │  │ only as a 4th-rung reader    │
└───────────────┬──────────────┘  └──────────────────────────────┘
                │
┌───────────────▼──────────────┐  ┌──────────────────────────────┐
│ backend (FastAPI)            │  │ jhw-bot                      │
│ /v1/* LLM proxy + role       │  │ the ONLY Telegram poller     │
│ aliases; owns the DO key     │  │ relays asks, records answers │
└──────────────────────────────┘  └──────────────────────────────┘
```

**Why the browser is a separate container.** Threat model, stated by the operator: *"I do not want
hackers to break through the agent and get access to local full PC stuff."* A browser that holds a
live Google session and types real credentials is the highest-risk process in the estate. It runs
isolated, with its own user, its own profile volume, and no host mount beyond `out/`.

**Why the agent shares the browser's network namespace.** CDP must be reachable at `127.0.0.1:9222`
without publishing it. **A published CDP port is full remote browser control with no authentication**
— treat any `0.0.0.0:9222` as a critical finding.

**Why jhw-bot is a separate container and the only Telegram poller.** Telegram's `getUpdates` has a
single consumer per token; two pollers steal each other's messages. The bot RECORDS answers to a
shared volume; the agent ENFORCES them. Authorisation and enforcement in separate processes means a
bug in the bot cannot act on the candidate's behalf.

### 2.3 Request flow for one application

```
jhw.py apply <url>
  └─ 20 self-tests (fail ⇒ no run)
  └─ agent.py
       ├─ prep: resolve ATS from the URL, tailor documents for THIS posting
       ├─ route to an adapter:
       │    greenhouse.io   → flows/greenhouse.py   (recorded, deterministic)
       │    myworkdayjobs   → flows/workday.py      (recorded, deterministic)
       │    icims.com       → flows/icims.py
       │    anything else   → flows/llm_driver.py   (generic, model-driven per action)
       └─ report: structured event + Telegram notification
```

---

## 3. Repository map

### 3.1 `agent/` — the engine

| File | Lines | Purpose |
|---|---|---|
| `agent/jhw.py` | 931 | **The single orchestrator.** Every verb. Owns the self-test gate. |
| `agent.py` | 588 | Per-application flow: prep → route → report. Owns `_ats_email()`. |
| `jhw_bot.py` | 567 | Telegram bot: relays asks, parses replies, refuses credentials. |
| `ask.py` | 233 | The ask bridge (file-based, bot-relayed). Windows, heartbeat, `waited()`. |
| `tailor.py` | 209 | CV + cover letter generation for a specific JD. |
| `stealth.py` | 181 | Browser fingerprint hardening. |
| `scrape_job.py` | 194 | JD extraction from a posting URL. |

### 3.2 `agent/flows/` — adapters, perception, decision

**ATS adapters (deterministic, built from human recordings)**

| File | Lines | Notes |
|---|---|---|
| `workday.py` | 2615 | Account gate, wizard, panel escalation, Google SSO, submit. |
| `greenhouse.py` | 1055 | Single-page; react-select handling; the choice ladder. |
| `workday_wd.py` | 1525 | Experimental Workday variant (`JHW_WD_EXPERIMENTAL=1`). |
| `llm_driver.py` | 1331 | **Generic** driver for unknown ATSs: index → model picks → execute. |
| `icims.py`, `easyapply.py`, `phenom.py`, `workday_gevernova.py` | | Per-vendor. |

**Perception — six independent readers, unioned**

| File | Lines | What it is |
|---|---|---|
| `seers.py` | 660 | Four senses that share nothing with the DOM predicate: **a11y tree** (CDP `Accessibility.getFullAXTree`), **hit grid** (`elementFromPoint` over a 40×25 grid), **deep DOM** (shadow roots + same-origin iframes), **tab walk** (focus order — a correct modal traps focus). Plus `merge()` (union) and `stamp()` (one canonical index). |
| `vision.py` | 717 | Screenshot → VLM. Returns **labels, never coordinates**. Label mode + bbox mode. |
| `som.py` | 468 | **Set-of-Mark**: numbered boxes painted over elements we already hold handles to; the model answers with a **number**. |
| `stagehand.py` | 57 | Bridge to Stagehand `observe()`. |

**Decision**

| File | Lines | What it is |
|---|---|---|
| `escalate.py` | 718 | The 3-LLM **stall panel**. Closed verb set, pure `validate()`, pure `quorum()`. |
| `choose.py` | 396 | The **closed-list chooser**: knowledge → learned → rule → panel → human. |
| `speaker.py` | 345 | The 4th model. Words only — no page access at all. |
| `converse.py` | 204 | Free-text reply → an INTENT from a closed set, each with an action. |
| `presubmit.py` | 333 | Pre-submit audit. Hard checks block; the panel only advises. |

**Knowledge**

| File | Lines | What it is |
|---|---|---|
| `recordings.py` | 484 | **Compiles human Playwright recordings into facts.** Redacts secrets on the way in. |
| `knowledge/*.json` | — | The committed, sanitised output. 5 ATSs. |
| `learned.py` | 225 | Answers that worked before, recalled across employers. |
| `atscreds.py` | 196 | Per-vendor password vault, per-tenant accounts. |
| `mailcode.py` | 284 | Reads a verification code over IMAP. Pure `extract_code()`. |

**Guards (run before every apply)**

| File | What it asserts |
|---|---|
| `test_selectors.py` | No name-resolving regex anywhere in `flows/` can reach an adjacent control. |
| `test_models.py` | Every role resolves to a model the live catalog actually has. |
| `test_workday_dom.py`, `test_escalate_dom.py`, `test_llm_driver_dom.py` | DOM contracts in a real Chromium. |
| `testbrowser.py` | How a self-test gets a browser: private, banner-marked, never the operator's profile. |

### 3.3 `backend/app/` — the cloud product

| File | Lines | Purpose |
|---|---|---|
| `resume_consensus.py` | 1256 | Multi-model CV generation/critique. |
| `backend/app/electronic.py` | 805 | The chat agent + `/api/electronic/{jd,generate,jobs,artifacts}`. |
| `observability.py` | 768 | Structured events → file → Promtail → Loki → Grafana. |
| `documents.py` | 518 | DOCX/PDF rendering. |
| `auth.py` | 508 | Signup, OTP by Gmail API, sessions. |
| `jd_ingest.py` | 483 | JD paste/URL → structured. |
| `llm.py` | 203 | **Role → model. One home.** `verified_model_for()` checks the live catalog. |
| `proxy.py` | 140 | `/v1/*` OpenAI-compatible proxy; role aliases; targeted 400 repair. |
| `main.py` | 170 | App wiring, startup checks. |

> **Name collisions to know about.** There are two `jhw.py` (root = the *web* orchestrator for the
> sibling cloud repo; `agent/jhw.py` = the engine orchestrator) and two `electronic.py`
> (`backend/app/electronic.py` = the chat agent + document endpoints; `agent/flows/electronic.py` = a
> thin client the engine uses). Always qualify the path.

### 3.4 Elsewhere

- `agent/recordings/` — **raw human recordings. GITIGNORED.** They contain cleartext passwords.
- `agent/userdata/candidate.md` — the candidate's facts and `## screening_defaults` (his declarations).
- `agent/templates/resume_data.json` — profile data the adapters read.
- `frontend/src/pages/` — the cabinet: Electronic, Tailor, Scout, Pipeline, Dashboard, Login.
- `CLAUDE.md` — **152 KB of measured defect post-mortems.** The single most valuable file in the repo.
- `obs/grafana/dashboards/` — provisioned dashboards.

---

## 4. The core doctrine: the answer ladder

Every value that goes into a form is resolved in this order. **A model is the fourth thing consulted,
never the first.**

```
1. RECORDED     what a human actually entered on this ATS   flows/knowledge/*.json
2. LEARNED      what we answered before and got through     learned.py
3. RULE         what the candidate wrote down               candidate.md ## screening_defaults
4. PANEL        3 models voting over the page's REAL options choose.py / escalate.py
5. HUMAN        Telegram, with those options numbered       ask.py
```

**Why this order.** Cost and certainty both increase downwards. A recorded human answer is not
inferred, not generated, and not a default — it is *what worked*. A model asked to choose between real
options is doing something it is good at; a model asked to invent a value is a liability.

**The safety property that makes rungs 4 and 5 usable: the answer must be a member of a set we
supplied.** `choose._pick()` resolves any answer — model's or human's — against the actual option list
and refuses a non-member, an out-of-range index, and anything ambiguous. A hallucinated option is
therefore *structurally unusable*. Set-of-Mark applies the identical trick to elements: the model
returns an integer indexing marks we drew, so it cannot name a control that does not exist.

### 4.1 What a model is never allowed to decide

`choose.SENSITIVE` removes the panel entirely for: veteran status, disability, race/ethnicity, gender
identity, sexual orientation, criminal history, security clearance, **and salary**.

- A declaration about oneself to an employer is a misrepresentation if wrong.
- A salary is a negotiating position; an invented number costs real money and cannot be retracted.

These come from a rule the candidate wrote down, or they are asked. **A recording IS a written
declaration** — the EEOC answers he chose in his own recording live in `candidate.md`, traceable to
that file, and therefore resolve deterministically with no model and no interruption.

---

## 5. Perception: why there are six readers

**The history.** Over one evening, four consecutive runs reported `0 control(s) indexed` on a page that
visibly showed two sign-in buttons. Each fix changed the *same* mechanism — a DOM visibility predicate
— and failed the same way. The operator's diagnosis was correct: *"if we get the same issue 3-5 times,
we must change the vision method, not iterate on the same one."*

**The readers, and what each is immune to:**

| Reader | Immune to |
|---|---|
| custom JS (`_CTRL_JS`) | — (the baseline; ~10 ms) |
| Playwright `is_visible()` | our own predicate bugs (industry definition, maintained) |
| a11y tree (CDP) | CSS tricks; sees what a screen reader announces |
| hit grid (`elementFromPoint`) | `aria-hidden`, clip-hiding, wrong `offsetParent` — pure geometry |
| deep DOM | shadow boundaries and iframes (`querySelectorAll` does not cross them) |
| tab walk | the page *behind* a modal (a correct modal traps focus) |
| vision (screenshot → VLM) | every DOM assumption, because it shares none of them |
| Set-of-Mark | label resolution failure — the answer is a number, not a name |

### 5.1 Two rules that were paid for dearly

**UNION, NEVER FIRST-WINS.** `_readable_index` used to return the first reader with ≥2 controls.
Vision reported five controls correctly — `['evgeny@s4biz.io', '************', 'Sign In',
'Create Account', 'Forgot your password?']` — three were discarded as unresolvable, the surviving two
cleared the threshold, and the union of four other readers *was never consulted*. The two fields that
mattered were deleted from the index before four vendors ever saw it, and the models were then blamed
for guessing. **Vision may only ADD what the DOM hides; it may never REMOVE what the DOM can see.**

**AN EMPTY INDEX IS A BROKEN READ, NOT AN EMPTY PAGE.** The floor is not a count. `_suspect()` asks the
DOM a *different* question — how many visible `input`s exist — and an index holding fewer fields than
the page has is a broken read, retried, never handed to a panel.

### 5.2 A filled input cannot be resolved by label

`'evgeny@s4biz.io'` is a box's **value**; its accessible **name** is "Email Address", and no Playwright
locator matches an input by value. `'************'` is the browser's own password mask — that string
exists in no attribute anywhere in the DOM. Label mode is therefore *structurally* incapable of naming
the only two fields on a sign-in form. `vision._by_value()` handles both, refusing ambiguity.

### 5.3 One canonical index

Every reader used to stamp `data-jhw-esc` with its own 0-based numbering, and `tab_walk` wiped the
others first. So after a union read, the DOM carried only the last reader's numbering while the panel
was handed union indices. **The index the panel saw and the attribute the executor clicked were two
different numbering schemes.** Now: one reader, one namespace (`data-jhw-grid|tab|deep`), and
`seers.stamp()` writes the canonical attribute once, from the union's numbering, after the union. A
control whose element vanished is marked `stale` rather than silently renumbered.

---

## 6. Decision: 3 deciders + 1 speaker

```
3 DECIDERS  deepseek-3.2 · mistral-3-14B · llama-4-maverick   vote on ACTIONS
1 SPEAKER   kimi-k2.6 (Moonshot)                             handles WORDS only
```

**Four different vendors, deliberately.** A 429 or an outage is provider-wide; a backup from the same
vendor is not a backup.

### 6.1 Governance (non-negotiable)

- **Deterministic code executes; models advise.** The panel may only name a verb from a closed set.
- **Quorum of 2 of 3**, and unusable proposals are dropped *before* votes are counted — so a majority
  can never "agree and be ignored".
- **Fail open downward**: any 429/timeout/exception ⇒ ask the human, i.e. exactly what happened before
  the panel existed. The panel can only ever *reduce* how often a human is woken.
- **Never in a request path.** The fill loop is ~30 DOM actions per application at 570–1000 ms each; a
  panel per action costs 4× tokens, 2–4× wall clock and 4× the 429 surface, and buys nothing — a wrong
  click just fails to advance the page and the repeat-breaker catches it. **That failure is
  self-correcting; a stall and a submission are not.** So the panel runs on the stall path and before
  the irreversible action, never per action.
- **The auditor is never the author and never the same vendor.**
- **The speaker touches nothing.** Asserted by test: `speaker.py` contains no `page.`, `click(`,
  `evaluate(`, `goto(` or `data-jhw`.

### 6.2 A phantom model id must never cost a run

`kimi-k2.6` returned HTTP 404 "model not found" for an entire run; the speaker was mute while the log
blamed a network error. Third occurrence (`deepseek-v4-flash` came off a pricing page,
`deepseek-v4-pro` sat at a chain head). Each previous fix corrected one constant, which fixes one
instance and never the class.

**A model id is an external fact that changes without telling us, so it must be checked, not
remembered.** `llm.verified_model_for()` reads the live catalog once, caches, and walks a per-role
ladder whose every entry is a different vendor. It **fails open in both directions**: an unreadable
catalog never reroutes a role, and an exhausted ladder passes the configured slug through so the *real*
error is visible instead of a lie. An explicit `JHW_MODEL_<ROLE>` override is honoured verbatim — that
is the operator naming a model on purpose.

---

## 7. Recordings → knowledge (the highest-leverage subsystem)

**The operator, and he was right:** *"we were working two and a half days on something that it took me
a minute to record."*

A Playwright codegen recording is ground truth about three things no amount of model reasoning
recovers:

1. **The real accessible names.** `Resume/CV*`, `Location (City)`, `Submit application`, `Land Select
   One`. Hours were spent inventing names the recording simply contains.
2. **The value a field will actually accept.** The Accenture recording shows the phone box fought four
   times — `+49 157 8551545`, `+49 1578551545`, `1578551545` — each producing `Error Phone Number`,
   until `15785541545` was accepted. The stored profile held one of the values that *failed*.
3. **The entry path and order.** `Accept Cookies → Autofill with Resume → Sign in with Google`. The
   driver had defaulted to Apply Manually and refused Google outright.

### 7.1 The pipeline

```
agent/recordings/*.py        raw codegen — GITIGNORED (contains cleartext passwords)
        │  jhw.py::_compile_knowledge()  ← runs inside `apply` AND `record`, never by hand
        │  recordings.compile_dir()      ← stdlib only, ~50 ms, no network, non-blocking
        ▼
flows/knowledge/<ats>.json   sanitised, committed:  fields · choices · options · buttons · files
        │  known_value()  = a recorded FILL   (free-text fields)
        │  known_choice() = a recorded OPTION (choice fields — never the query typed to find it)
        ▼
adapters + choose.py         rung 1 of the answer ladder
```

**The compile is a verb's job, not the operator's.** It was compiled by hand three times in one
afternoon before this was wired, and `record` recorded a lifecycle without compiling it — so a newly
recorded ATS had *no effect at all* and the run silently used stale facts. It now runs inside `apply`
**before** the self-tests, so a recording made minutes earlier is in force for the very next run, and it
prints `— UPDATED: <ats>` only when something actually changed. Verified idempotent, and verified to
pick up a brand-new recording (fields, label→option choice, buttons) with no second command.

### 7.2 Four rules the pipeline enforces

- **Secrets never survive.** `redact()` runs on the way in: by label, by matching the live env, and by
  *shape* for a password nobody has seen. `compile_dir()` refuses to write a file that still matches a
  secret. *A password needs punctuation that does not occur in a URL* — `(){}~!#$%^&`, never
  `. - / @ : + _`. (An earlier version redacted `itzen.ai`, the candidate's own website.)
- **Only human recordings are evidence.** The first run swept up our own adapters from the same folder
  and would have committed *our invented selectors* as if a human had proved them. `_is_codegen()`
  requires one of codegen's entry signatures, which nothing else in the project has.
- **Last write wins.** The human's failed attempts are not facts; only the value a field finally
  accepted is.
- **A search query is not an answer.** For a combobox, codegen records the typed *filter* and then the
  clicked *option*, one line apart. Storing only the fill put **`Algeria +213` on a German
  application**: the recorded value was `ger`, Germany was not in the (virtualised) visible option
  list, and `ger` matched inside al-GER-ia. `known_choice()` pairs a label with the option that
  followed it; `known_value()` is used only for fields with no options at all.

---

## 8. Security model

| Rule | Why |
|---|---|
| Secrets only in `.env` (gitignored) or deploy secrets | Never in git, never in an image layer, never logged. |
| Values over **stdin**, never argv | argv is visible in `ps` and shell history. |
| No port on `0.0.0.0` unless the internet must reach it | A published CDP (9222) is unauthenticated full browser control. Same for VNC 5900, noVNC 9090, dev backends 8000. |
| `no-new-privileges`, `cap_drop: ALL`, non-root `USER`, resource limits, healthchecks | Default-deny. An unbounded browser/LLM loop must not take the host down. |
| The password is typed **once per run** | Repeated attempts lock accounts — and that Google account gates the mailbox every ATS verification email arrives in. |
| A challenge is a **stop**, not a retry | 2-step, passkey, "browser may not be secure" ⇒ hand back to the human, who signs in once in noVNC; the persistent profile remembers it. |
| A credential never reaches a model | `jhw_bot` refuses by *shape* before the chat model sees it; `converse.parse()` owns credentials deterministically. |
| Self-tests never render into the operator's browser | `testbrowser.py`: a private throwaway browser, every fixture carrying a red **"SELF-TEST FIXTURE — NOT A REAL APPLICATION"** banner. A fixture once painted *"Thank you for your application."* into the live profile — manufacturing exactly the evidence the submit rule exists to protect. |

### 8.1 What is deliberately not done

**No offensive action, ever.** No scanning a third party, no connecting back. Criminal under German
StGB §202a/§202b/§303b, EU Directive 2013/40, US CFAA §1030, Canada CC s.342.1 — and §202c
criminalises building the tooling with that intent. It is also pointless: the address is usually a
compromised third party.

---

## 9. Verification discipline

**20 suites run before every single apply.** A guard the operator has to remember to run does not
exist.

### 9.1 Rules that produced most of the value

**A write is not done until it has been READ BACK.** `fill()` returning without throwing proves only
that Playwright typed. React keeps a value *tracker* on a node; a direct assignment leaves it stale, so
what you *see* is set and what the site *validates* is empty. Use the native setter
(`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set`) — **called on the real
field, or you get `TypeError: Illegal invocation`** — then read the value back.

**Absence of evidence is never a finding.** A failed lookup is `UNKNOWN`, not `none`. This is the
oldest rule here and the most often re-broken.

**Every completion check must be positive evidence.** An empty page is indistinguishable from a
finished one. `done` requires a Review page or a Submit button, never the *absence* of problems.

**A repeat-breaker must be scoped to its context.** "Same action" on a different page is progress, not
a repeat.

**Any action that toggles is unsafe under retry.** Clicking a ticked checkbox unticks it. Express it as
"set to X" and make it idempotent.

**An unanchored Playwright role-name regex is a bet on layout.** `name=` is a *prefix* match and
`.first` resolves ties by DOM order. `r"sign\s*in"` matched **"Sign in with Google"** and took the
driver off the ATS entirely for 70 seconds. `test_selectors.py` now compiles all 85 name-resolving
regexes in `flows/` against a corpus of real adjacent controls.

**A check that cannot run is not a check.** Occurrences: a ruff gate that silently skipped for weeks;
an esbuild path "missing" on a machine where esbuild worked; `page.accessibility` removed from
Playwright; `os.uname()` on Windows; a test importing `httpx`, which the app does not declare.

**A check must name its subject.** Recurring failure modes, each seen multiple times:
- a grep matching **its own assertion line** (measure only the slice above `def _selftest`);
- a grep matching **a comment or docstring** explaining the forbidden thing (strip both first);
- a substring aimed at characters when the property is about **code** (`"del "` matched inside
  "mo**del did** not answer" — use the AST);
- a check aimed at the **wrong element** (an attribute instead of the element; the first rule of a
  multi-rule selector);
- a check that inspects **a seventh of its subject** and passes vacuously.

**A check that has never failed is unproven.** Every guard here has been negative-tested: reintroduce
the defect, watch it fail, restore. **And a negative test that passes because of defence in depth is
measuring the other guard** — defeat every guard on the path before believing it.

**A harness that mutates real files must self-heal on import.** A `finally` cannot survive SIGKILL, and
a killed run once left a marker in a source file that broke the *next* run for an unrelated reason.

---

## 10. Deploy

**Direct from the PC. GitHub is not in the deploy path.**

```
python jhw.py deploy        (or ship.py in the sibling repo)
```

scp the build context → **the droplet builds the image itself** → `up -d --force-recreate` → wire the
marker-scoped Caddy block → verify. No registry, no CI wait. The GitHub round-trip added a two-minute
stall and a second source of truth for zero benefit.

**Hard-won deploy rules:**

- **ONE ssh session per deploy.** Windows OpenSSH has no `ControlMaster`, and OpenSSH ≥9.8 enables
  `PerSourcePenalties` by default — a burst of short-lived sessions from one IP gets throttled. The
  payload travels base64 in one `ssh host bash -s`; secrets on stdin.
- **Pack the COMMIT (`git archive HEAD`), not the working tree.** A mid-flight edit otherwise ships
  untested. Pass `-c core.autocrlf=false -c core.eol=lf` or the artefact is platform-dependent.
- **Never `sed -i` a bind-mounted file.** It writes a temp file and renames it, producing a new inode;
  the mount still points at the old one, so every later edit is invisible to the container. Compare
  `sha256sum` host-vs-container and restart when they differ.
- **Config has three hops** — host file → container file (the *mount*) → running config (the *reload*)
   → what is actually *served*. A check must say which hop it measured. `caddy validate` proves nothing
  about hops 1–2.
- **Never `--remove-orphans`** on a subset compose in a shared project.
- **Content matching is not proof of routing.** Tag the request and grep the upstream's access log.

---

## 11. Environment

Set once in `agent/.env` (gitignored). **The operator edits this file; scripts must not.**

```
JHW_PROXY_BASE        http://backend:8000/v1
AGENT_PROXY_TOKEN     shared secret for the LLM proxy
JHW_CDP_URL           http://127.0.0.1:9222
JHW_ASK_CHANNEL       bridge
TELEGRAM_BOT_TOKEN    the bot
TELEGRAM_CHAT_ID      the operator
JHW_MAIL_USER         the mailbox we can READ (verification codes land here)
JHW_MAIL_PASS         a Google APP PASSWORD — mail-scoped, separately revocable
GOOGLE_EMAIL/PASSWORD used only for ATS SSO, once per run, never logged
LINKEDIN_*            LinkedIn session
```

Root `.env` (backend): `DO_INFERENCE_BASE_URL`, `DO_INFERENCE_KEY`, `DATA_DIR`, `CORS_ORIGINS`.

**Why `JHW_MAIL_PASS` is an app password, not the account password.** Mail-scoped and separately
revocable, so a compromise of the container still cannot reach the account that gates the mailbox.
That is the same reasoning that keeps `GOOGLE_PASSWORD` off the automated path where possible: reading
mail is safe where full sign-in is not.

Useful switches: `JHW_AUTOSUBMIT=0` (stop at review), `JHW_ATS_ALLOW_SSO=0`, `JHW_VISION_FIRST=0`,
`JHW_ASK_WINDOW`, `JHW_ACTION_TIMEOUT`, `JHW_WD_ENTRY=manual|autofill|last`.

---

## 11a. One rule, one home — the defect class that survives longest

The most persistent failure mode in this project is not a wrong algorithm. It is **the same rule
existing in two places, with the older copy winning.**

Measured instances:
- `ENRICH_MODELS` had **four** homes (code default, compose `environment:`, `.env` plural, `.env`
  singular legacy) and the stale one beat the committed chain for weeks.
- The ATS login address had **four** homes, so the engine would have registered with an address whose
  mailbox it cannot read and waited forever for a code.
- `atscreds` keys credentials by vendor/tenant while `workday.py` keyed by hostname — one fact, two
  stores, so one reported "no account recorded" while the other held one. **Still open.**
- The recorded-option preference ("this Friedberg, not that one") lived in the choice loop and not in
  the location block that runs earlier — **and a wrong city went out on a submitted application.**

The fix is never "remember to update both". It is: **one function owns the rule, every path calls it,
and a test asserts via the AST that no caller re-implements it.** `_pick_place()` is the worked example.

## 12. Known open items

- `backend/app/scout.py` is a 4-sample stub — real job sourcing is unwired.
- `flows/linkedin_search.py` is dead code.
- `atscreds` keys by vendor/tenant while `workday.py::_load_accounts()` keys by hostname — **one fact,
  two stores.** Needs one owner.
- CRM Kanban for applied jobs: not built.
- DO cloud side: portal profile builder, per-user Telegram, bge matching: not built.
- App-layer checks (fetching a customer page) are deliberately **not** implemented: "not one packet"
  is a product promise, and changing it requires changing the ToU wording in the same edit.

---

## 13. Porting this to another project

The transferable parts, in order of value. None of them is job-application specific.

### 13.1 The answer ladder (`choose.py`) — the single most portable idea

Any agent that fills forms, answers questions, or picks from options should resolve values as
*recorded → learned → written rule → model consensus → human*, with **every answer validated against a
set you supplied**. ~400 lines. Works unchanged for any closed-choice problem.

### 13.2 Recordings → knowledge (`recordings.py`)

If a human can demonstrate the task once, compile that demonstration into facts instead of reasoning
about it. Applies to any UI automation. The three rules that matter: redact on the way *in*, accept only
genuine recordings, and never confuse a search query with a chosen answer.

### 13.3 Set-of-Mark perception (`som.py` + `seers.py`)

Numbered marks over elements you already hold handles to; the model answers with an integer. Resolution
is by construction, so hallucination is structurally impossible. Then **union every reader** — never
first-wins. Directly reusable for any browser or GUI agent.

### 13.4 Panel governance (`escalate.py`, §6.1 above)

Closed verb set, pure validate, quorum, fail open downward, never in a request path, put the consensus
on the *irreversible* action rather than the hot path. Vendor diversity is the point.

### 13.5 The verification discipline (§9)

This is the part that actually made the system work, and it is entirely portable:
- read every write back;
- absence of evidence is never a finding;
- completion requires positive evidence;
- negative-test every guard, and defeat defence in depth before believing the test;
- a check must be able to run where it is invoked, and must name its subject;
- **keep a `CLAUDE.md`-style post-mortem file.** 152 KB of "here is the defect, here is the measured
  evidence, here is the rule" is worth more than any amount of clean code, because it stops the same
  class of bug recurring. Roughly half the incidents in this project were *repeats* of a rule that had
  been written down but not enforced — so where possible, **turn the rule into a test.**

### 13.6 Operational shape

- ONE orchestrator, ONE command per verb, prerequisites handled by the verb.
- Deliver operations as committed scripts, never as command blobs to paste.
- One ssh session per deploy; pack the commit, not the tree.
- Isolate the risky process (the browser) in its own container with no host reach.

---

## 14. Reading order for someone new

1. This file.
2. `CLAUDE.md` — long, but it is the *why* behind every guard. Skim the headings; each is an incident.
3. `agent/jhw.py` — the orchestrator and the self-test list.
4. `agent/flows/choose.py` — the ladder, in 400 readable lines.
5. `agent/flows/greenhouse.py` — the cleanest complete adapter, with its recording provenance inline.
6. `agent/flows/seers.py` + `som.py` — perception.
7. `agent/flows/escalate.py` — panel governance.
8. Then run `python jhw.py apply <a real greenhouse url>` and watch noVNC at `localhost:9090`.

---

*Every "why" in this document is traceable to a measured failure recorded in `CLAUDE.md`. If something
here reads like an opinion, it is a scar.*
