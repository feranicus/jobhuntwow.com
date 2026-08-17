# Release pipeline — `python ship.py`

How a change reaches jobhuntwow.com: deterministic test gates, a staging twin that gets **rebooted**,
and a four-model review panel that explains the evidence and **decides nothing**.

Written from the code in this repo. Every check named here was read out of
`deploy/stagegate/gate.py`; every rule was proven by breaking it and watching the suite fail.

---

## 0. The one command

```
python ship.py
```

That is the whole interface. Flags **narrow** it, they never split it:

| flag | effect |
|---|---|
| `--test` | stop after stage 1 |
| `--no-stage` | skip the staging gate (it says out loud that the release is unvalidated) |
| `--no-reboot` | run the staging gate without the reboot test (faster, and **weaker**) |
| `--rollback [TAG]` | reset to `last-known-good` (or `TAG`) and redeploy that exact state |
| `-m "msg"` | commit message |
| `OVERRIDE_PANEL=1` | promote through a panel halt (see §4) |

Everything else — `jhw.py deploy`, `deploy_direct.py`, `deploy/stagegate/gate.py`,
`deploy/stagegate/quorum.py` — is a **building block** that `ship.py` calls. They stay individually
runnable for debugging and you should never need them.

```
1/6  TESTS        requirements parse · compile + null-byte scan · ruff F821/F811/F822 · 3 suites
2/6  COMMIT+PUSH  ALWAYS pushes, even with nothing new to commit
3/6  STAGING GATE deploy to the twin → health → REBOOT IT → health → completeness → AI panel
4/6  DEPLOY WEB   jhw.py deploy → deploy_direct.py (ONE ssh session, image built on the droplet)
5/6  VERIFY       the public site AND the sha256 of the code inside the running container
6/6  SAFE POINT   move `last-known-good`, write a dated `good-*` tag, push both
```

**GitHub is the source of truth for code and is NOT in the deploy path.** No GHCR, no CI wait, no
second source of truth (settled in `CLAUDE.md` and `DEPLOY.md`). But a commit that never leaves the
PC is not a backup and the droplet has no independent history, so stage 2 pushes unconditionally.
Both this checkout and the landing checkout point at `https://github.com/feranicus/jobhuntwow.com.git`
— verified, and they share history (`cd603d5` is an ancestor of this tree), so pushing from here is
safe. `jhw.py`'s own guard refuses to push if `origin` is not that repo.

---

## 1. The check inventory

21 deterministic checks run **inside** the twin, plus up to 4 more added by the PC side, and they run
**twice** — before and after the reboot. Each measures exactly one property and emits one line:

```
CHECK|<name>|yes|no|<detail>
```

`tr` squashes newlines and pipes at the source (a newline inside a detail truncates the record and
silently loses the rest); `cut -c1-1000` is only a runaway guard for a crashing command, and it sits
far above the longest real detail.

### Inside the twin

| check | what it measures |
|---|---|
| `container_running` | `docker inspect .State.Status` of `jhw-web` is exactly `running` |
| `container_healthy` | docker's **own** healthcheck went `healthy` (not stuck in `starting`) |
| `restart_count` | `.RestartCount <= 1` — a crash loop reports "running" between restarts |
| `no_published_ports` | nothing is bound on the host. A stray `ports:` entry is a remote-access finding |
| `runs_as_nonroot` | `id -u` inside the container is `10001` |
| `secret_not_baked_into_image` | the **image**'s env layer carries no secret names (the container legitimately gets them at runtime) |
| `api_health` | `GET /api/health` → 200 with `"ok":true` |
| `api_auth` | `GET /api/me` with **no cookie** → 401. Probing `/` alone passes on a cached SPA shell |
| `app_served` | `GET /` with a **browser UA** returns bytes **and** the body links to `/login` |
| `spa_shell` | `GET /login` returns the built SPA (`id="root"`) — i.e. `npm run build` really landed |
| `html_nocache` | `/` sends `Cache-Control: no-cache`. Without it browsers heuristically cached a stale landing for a day |
| `api_404_is_json` | an unknown `/api/...` path is a JSON 404, not the SPA catch-all swallowing API routes |
| `datadir_writable` | uid 10001 can write `/data` — where the JSON store and every artifact go |
| `chat_stream_delivers` | `POST /api/chat` and count the **bytes the stream emitted**. A stream that opens and emits nothing is the empty-200 of streaming |
| `job_completes` | `POST /api/electronic/generate` with a small JD + profile and **read the manifest**: HTTP 200 and ≥ 4 rendered files |
| `job_artifacts_rendered` | those files exist **on disk** and each is ≥ 6 KB. A well-formed nothing renders too |
| `auditor_not_author` | from the same manifest: the auditing model ≠ the authoring model. The product claim, measured |
| `engine_fresh` | sha256 of every `backend/app/*.py` **inside the container** vs the shipped sources. **The deploy proof** |
| `events_written` | the app appended at least one `jhw-web` line to `EVENTS_LOG` |
| `disk` | `/` is under 90 % used — a docker build fails otherwise |
| `memory` | ≥ 250 MB available |

### Added by the PC side

| check | what it measures |
|---|---|
| `reboot_recovery` | `/proc/sys/kernel/random/boot_id` **CHANGED**. Waiting for ssh to answer is not a reboot test |
| `evidence_complete` | every name in the **committed** `EXPECTED` list reported in this pass |
| `post_reboot_*` | the same 21 checks again, compared **by name** |
| `health_run_complete` | the run reached its terminal sentinel `#### HEALTH_END`, with the ssh return code |

### Two properties worth calling out

* **`app_served` uses a browser User-Agent.** A `curl`-shaped UA is exactly what a bot gate is built
  to 404, and the check would then record a broken app. There is **no bot-404 gate in this backend
  today** — verified: no `BOT_404` branch anywhere in `backend/app/` — so this is armour against the
  day it gains one, and it costs nothing.
* **The gate knows what it EXPECTS.** `EXPECTED` is a committed list and *both* passes are compared
  to it. Deriving "expected" from whatever the first pass produced cannot see a first pass that was
  also cut short: both halves then agree with each other and the gate reports "35/35 passed" over six
  checks that never ran. A shrinking denominator is **missing evidence, not a smaller pass**.

---

## 2. What the staging gate does NOT prove

Stated here so nobody has to infer it, and repeated verbatim in the panel's briefing:

* **The vhost, TLS and SSE buffering through Caddy.** The twin is deployed with `--no-caddy` and has
  no shared proxy; a twin with a *different* proxy topology would validate something that never
  ships. Production proves this separately in `jhw.py status`, which tags a request and greps
  `jhw-web`'s own access log for the tag (content matching alone once passed while a byte-identical
  static copy owned the domain).
* **The ATS apply drivers.** They run in a local sandbox on the operator's PC, never on a droplet.
* **The frontend bundle by hash.** `engine_fresh` covers `backend/app/*.py` only. `spa_shell` proves
  the SPA is *present*; nothing proves it is *this commit's* SPA.
* **The observability pipeline.** `events_written` proves the write. There is no promtail or Loki on
  the twin, so nothing there proves the pipeline.

---

## 3. The four-model panel

`deploy/stagegate/quorum.py`, shipped into `jhw-web` on the twin and run there — because
`DO_INFERENCE_KEY` lives in `/opt/jobhuntwow/.env` on the droplet and never on the operator's PC. A
check that cannot see its subject is not a check.

```
soldiers  deepseek-3.2 · llama-4-maverick     "did this work?"        operational reading
auditors  gemma-4-31B-it · kimi-k2.6          "what did we miss?"     adversarial reading
```

Four **vendors**, not four prompts: a 429 is provider-wide and a blind spot is model-family-wide, so
four models on one provider is one model wearing four hats. This is the same doctrine
`resume_consensus.pick_auditor` already enforces for the Tailor.

* Per-model quirks are **not re-implemented**. `quorum.ask_one` calls
  `resume_consensus.build_payload` / `repair_payload`, which is the one home in this repo for them
  (kimi requires a specific temperature and refuses `response_format`; this gateway rejects
  `max_tokens` together with `json_object`; chain-of-thought must stay suppressed).
* **A 4xx body is never discarded.** It is the server telling you what it wants, and a payload is
  repaired by **what the server named** — never by stripping fields until something works.
* A reviewer that 429s, times out or returns nothing is **recorded as a non-answer**, not a failure
  of the release.
* Cost: four reviewers × ~1,800 output tokens on open-weight models — a few tenths of a cent per run.

### The briefing is the highest-leverage part

`quorum.ARCH` is a factual map: how a code change reaches production (the container is **replaced**;
there is no hot reload), the shared-proxy facts (single-file bind mount pins an inode, Caddy reads
config only at start, the three hops), that `/api/chat` is a plain streaming response and **not**
SSE-with-reconnect, that `generate` runs the whole tailor **inside the request** — and, explicitly,
what does **not** exist: no Kubernetes, no config-map, no hot-reload watcher, **no queue, no broker,
no worker**, no CI in the deploy path, no promtail on the twin. Plus one line per check saying what
it already measures.

Without that last section a panel proposes plausible architecture it cannot see, and the same wrong
call costs a review slot every single run.

---

## 4. Governance — who decides what

```
                        DETERMINISTIC CHECKS      MODELS
verdict (GO / NO-GO)          DECIDE              never
reasoning / root cause          --                WRITE
risk list                       --                WRITE
any runtime value           not tunable yet       nothing (BOUNDS is empty)
release halt                    --                2+ dissent ⇒ ask a human
```

`quorum.decide_from_verdict()` is pure, has no I/O, and is exercised by 14 assertions:

| panel | gate | outcome |
|---|---|---|
| four happy models | **NO-GO** | **NO-GO** — an agreeable model cannot wave through a dead container |
| one hard NO-GO | GO | **GO** — one rate-limited or grumpy model cannot hold a release hostage |
| one 429 / timeout | GO | **GO** — an errored reviewer is a non-answer, not dissent |
| two UNSUREs, nobody objecting | GO | **GO** — hesitation is not a finding |
| ≥ 2 dissenters **incl. ≥ 1 NO-GO** | GO | **HALT**, escapable with `OVERRIDE_PANEL=1` |
| no reviews at all | GO | **GO** |

The halt exists because a panel contradicting a green gate has twice meant **a check is lying**
rather than the system being fine. A guard an informed operator cannot override is a bug; a guard
that presents its decision as a malfunction is a different bug — so it prints the reason, the
comparison to make (the check *count* against the previous run), and the override.

**Nothing is tunable.** `quorum.consensus()` (quorum on the direction → **median** → clamp **on
read** → 25 % step cap) and `BOUNDS = {}` are ported and tested, and connected to nothing:
step 5 of the porting order is "**only then** consider letting it tune anything", and a test asserts
`tuning_is_wired() is False` so it cannot be switched on quietly.

### Checks about the checks

| property | why |
|---|---|
| no `*) chk <name> yes` wildcard | one such fallback took a real NO-GO to "33/33 green" |
| a PASS whose detail describes a failure is **demoted automatically** | a check reported PASS while quoting "STALE MOUNT" in its own detail |
| details are **wrapped, never truncated** | truncation destroys the evidence the check exists to provide, on the passing path where nobody looks |
| a run must reach a terminal **sentinel** | partial ssh output was counted as a complete run |
| the ssh **return code** is never discarded | a dropped connection returning partial output was invisible |
| `EXPECTED` ⟷ the script cannot drift | adding a `chk` line without declaring it fails the build |
| `engine_is_current` fails **closed** | two empty file lists are equal; that is no evidence, not a match |
| the source cannot change **mid-flight** | one release produced three artefacts from "the same" commit |

---

## 5. Running the tests

`python ship.py --test`, or individually (no pytest, no pip install, stdlib only — they must run on
the operator's own machine, which is the platform five wasted ships were spent relearning):

```
python tests/test_gate_governance.py        51 checks - the rules that decide a release
python tests/test_gate_integrity.py         66 checks - the gate cannot lie
python backend/tests/test_resume_consensus.py   176 checks - the tailor consensus
```

Every rule is negative-tested. Most mutate a **copy** of the module in a temp directory (a `finally`
cannot survive SIGKILL, and a killed harness that leaves a marker in a real file fails the next run
for an unrelated reason). Verified by hand, with the tree restored byte-for-byte afterwards:

| break this | and this fails |
|---|---|
| let the panel set the gate | "a RED gate with four happy reviewers stays NO-GO" |
| let ONE reviewer veto | "ONE grumpy reviewer does NOT block the release" |
| count errored reviewers as dissent | "a 429 is a non-answer, not dissent" |
| drop the missing-evidence rule | "a check that VANISHED is NO-GO and is named" |
| reintroduce a `*) chk ... yes` | "no wildcard catch-all in the health script" |
| truncate instead of wrap | "the LAST characters of a 274-char detail survive" |
| deploy before the gate | "NO-GO: nothing was deployed, verified or tagged" |
| disable the contradiction demoter | "a PASS whose detail says STALE is demoted" |

---

## 6. Enabling the staging droplet

There is **no default host**: a hardcoded IP would either be someone else's box or silently absent.
Without it the gate returns **SKIP**, prints the line below, and `ship.py` continues while saying the
release is unvalidated. A check that cannot run must say so — never silently pass, and never fail a
release for a facility that has not been provisioned.

1. Create a droplet that **matches production**: same size, same image, same region (FRA1). A twin
   that differs in those is not a twin — latency to the inference endpoint, host hardware generation
   and kernel line are exactly the variables that make "it worked in staging" untrue. Production is
   `64.225.108.200`, FRA1.
2. Put your ssh key on it (`ssh root@<ip> echo ok` must work — the same key `deploy_direct.py` uses).
3. Set one variable:

```powershell
$env:JHW_STAGING_HOST = "<ip>"          # PowerShell
```
```bash
export JHW_STAGING_HOST=<ip>            # bash
```

Then `python ship.py` does the rest by itself: installs docker, creates the external network
(`videodead_appnet`) and volume (`colt-stack_colt_events`) that `docker-compose.web.yml` declares
`external:` — on a fresh twin they do not exist and `up` refuses to start — deploys with
`deploy_direct.deploy()` (**the same function production uses**, with `--no-caddy`), health-checks,
reboots the box, health-checks again, and asks the panel.

**Synthetic fixtures only.** The twin never receives production personal data; the job check drives a
committed fictional JD and profile. The runtime `.env` (the same secrets production gets) is written
by the existing `deploy_direct.runtime_env()`.

Wall clock: roughly 8–20 minutes, dominated by the docker build (~2–4 min) and by `job_completes`
running a real tailor twice (bounded by `JHW_GATE_JOB_TIMEOUT`, default 420 s each). It runs in both
passes deliberately: "a job still completes after a reboot" is what the reboot test is *for*.

---

## 7. Known gaps, stated plainly

1. **`deploy_direct.py` packs the WORKING TREE, not `git archive HEAD`.** The correct fix is
   `git -c core.autocrlf=false -c core.eol=lf archive HEAD` (`core.eol` alone does nothing — measured
   on the sibling project), so a Windows packer and a Linux packer ship identical bytes from one
   commit. That is a behavioural change to the deploy **engine** and is deliberately not made here.
   `ship.py::pack_fingerprint()` closes the same hole from outside: it fingerprints the shipped file
   set before staging and again before production and **refuses to promote if a byte moved**.
2. **The frontend bundle is not hash-verified** against the commit (see §2).
3. **`Dockerfile.web`'s header comment is stale** — it says the image is built in GitHub Actions and
   pushed to GHCR and "NEVER built on the droplet", which is the opposite of what
   `docker-compose.web.yml`, `deploy_direct.py` and `CLAUDE.md` all say. Harmless but misleading;
   not changed here because the image build is out of this change's scope.
4. **Two checkouts share one git remote** (this repo and the landing repo both push
   `feranicus/jobhuntwow.com`). They share history today; pushing from the landing repo after this
   one has moved ahead would need a merge.
5. **The panel needs `DO_INFERENCE_KEY` in the container.** Without it every reviewer records
   "did not answer" and the digest says `0 of 4 answered`. The deterministic gate is unaffected —
   which is the point.
6. **No release-notes application.** The third use of the four-model pattern (four independent
   write-ups mailed via the Gmail API + Telegram) is not ported; SMTP is blocked outbound on the
   droplet, so it would have to go through the Gmail API path that `backend/app/notify.py` already
   owns.
7. **Nothing here has run against a droplet yet.** The pipeline was authored and verified offline:
   suites green, rendered shell `bash -n` clean, embedded helper exercised in all three directions,
   and every governance rule negative-tested. The first real `python ship.py` needs the operator's
   machine and its ssh key.
