---
name: ats-workday
description: Ground truth for Workday (*.myworkdayjobs.com) apply flows - URL shape, step order, the data-automation-id / ARIA handles that matter, and the traps that have actually cost runs. Driven by flows/workday_wd.py + the generic flows/llm_driver.py. Auto-submits at Review (candidate authorised 2026-07-21); JHW_AUTOSUBMIT=0 stops at Review.
---

# ATS SKILL — Workday

## How to recognise it
* Candidate site: `https://<tenant>.wd<N>.myworkdayjobs.com/<SITE>/job/<LOCATION>/<TITLE-SLUG>_<REQID>`
* Apply URL:      the same, plus `/apply` (Workday also accepts `/apply/applyManually`)
* Mid-flow you also cross `identity.wd<N>.myworkday.com` (the auth hop) and sometimes
  `<tenant>.wd<N>.myworkday.com`. **All of those are Workday** — `workday_wd.is_workday()`
  accepts `myworkday.com` as well as `myworkdayjobs.com`, and the account lookup keys on the
  TENANT, because one login covers every host of that tenant.
* The live example this adapter was built against:
  `https://accenture.wd103.myworkdayjobs.com/AccentureCareers/job/Hamburg/Agentic-AI-MarTech-Strategist--all-genders-_R00329883/apply?source=jb_1`
  Keep the query string: `?source=jb_1` is Workday's own source tag and dropping it changes how
  the application is attributed.

## THE GATEWAY: a careers URL is NOT derivable into a Workday URL
`https://www.accenture.com/de-de/careers/jobdetails?id=R00329883_de&src=LINKEDINJP` cannot be
transformed into the Workday URL by any string rule. The Workday URL needs the SITE name
(`AccentureCareers`), the LOCATION slug (`Hamburg`) and the TITLE slug — none of which appear in
the req id. So do NOT invent one. `workday_wd.reach_workday()` opens the careers page and follows
**its own** Apply link, which is exactly what the real recording does:

    LinkedIn -> "Apply to <job>" (popup) -> accenture.com  -> "Allow all cookies"
             -> "APPLY NOW. A new tab will..." (SECOND popup) -> accenture.wd103.myworkdayjobs.com
             -> "Accept Cookies"

Both hops are POPUPS (`ctx.expect_page`), not same-tab navigations. After landing, the adapter
CLOSES the careers tab: `llm_driver._connect()` picks the LAST non-LinkedIn page, so leaving
accenture.com open makes it a coin flip which page the fallback driver would drive.

NOT YET IMPLEMENTED, AND NOT VERIFIED: Workday tenants expose a CXS search endpoint
(`/wday/cxs/<tenant>/<SITE>/jobs`) that would resolve a req id to its job path deterministically
and remove the click-through entirely. It is documented in the wild but **has not been verified
against this tenant from this repo**. Before relying on it, prove it with `python jhw.py inspect`
or a single curl and record the real response shape here. Do not code against it from memory.

## Is there an apply API? (researched — do not re-litigate)
No candidate-side one, same as Phenom / SuccessFactors / Oracle. Workday's APIs are tenant APIs
issued to the EMPLOYER. So: browser automation, deterministic wherever ground truth exists.

## THE WIDGET THAT MAKES WORKDAY DIFFERENT
Workday's screening controls are **not `<select>` and not a text `<input>`**. They are

    <button aria-haspopup="listbox" aria-expanded="false" aria-label="Select One Required">Select One</button>

which opens a **portal** of `[role=option]` rows. The recording proves the shape:
`get_by_role("button", ...)` then `get_by_role("listbox")` / `get_by_role("option", name="He/Him")`.

The generic `llm_driver` cannot safely drive that, for three specific reasons:

1. **The repeat-breaker collides.** Its key is `action|label|value|errors`. Several dropdowns on
   ONE Workday page share the accessible name `Select One Required` with an identical error set,
   so `click | Select One Required | |` reaches 3 strikes on the THIRD question and is BLOCKED
   before it has been opened once. That is the same page-scoping defect already documented for
   `click Next`, one level down.
2. **Workday REVERTS a selection when a re-render intervenes.** `llm_driver` re-enumerates the
   whole DOM between every single action, i.e. it guarantees that re-render.
3. **`ENUM_JS.missing` cannot see them.** It only inspects `input,select,textarea`, so an
   unanswered REQUIRED dropdown cannot hold back a `Next` click, and `blocked_next` never fires.

So `workday_wd.py` owns exactly this widget, as a COMPOUND turn with no enumeration in between:
**scroll into view -> click the button -> `llm_driver.PICK_JS` -> READ THE BUTTON'S LABEL BACK.**
Option matching itself is NOT reimplemented — it is `PICK_JS`, the routine proven on Phenom.

**A write is not done until it has been read back.** `_pick_in_portal()` returns
`verified = picked AND the label actually changed`. A control that reverts is recorded in
`unresolved`, never counted as filled. `wd_sweep()` is **idempotent**: an already-answered control
is left alone, because re-opening it is what reverts it.

## THE STEPS (order is fixed; a tenant may omit some)
1. **My Information** — Country FIRST if present: it RE-RENDERS the form, so anything set before
   it can be lost. Then legal first/last name, address line 1, postal code, city, phone
   (+ phone device type on some tenants), and *How Did You Hear About Us?*.
2. **My Experience** — Work Experience, Education, Skills, Websites/LinkedIn, and **Resume/CV,
   which is REQUIRED to leave the page.**
3. **Application Questions** — the button-dropdowns.
4. **Voluntary Disclosures** — gender / pronoun / veteran / disability + the consent checkbox.
5. **Self Identify** (US tenants only) — disability form.
6. **Review** -> **Submit**.

Only `Save and Continue` / `Next` moves forward. The step names across the top are a PROGRESS BAR:
clicking one navigates BACKWARDS and loses the page you just filled. `llm_driver` already refuses
`my information | my experience | voluntary disclosures | review | sign up | sign in | back`.

## How Did You Hear About Us? — a CASCADE and a MULTISELECT
Recorded, verbatim: click the textbox, click **"Job Boards"** (a parent node that expands), then
click **"LinkedIn"**. Two consequences:
* free text can never satisfy it — the value must come from the tree;
* the answer lands in a **CHIP** beside the input and **the input goes back to EMPTY**. So
  reading the input reports FAILURE on a SUCCESS. `WD_ENUM_JS` therefore reports
  `kind:"multi"` with a `chips` array, and a chip is the only proof it is answered.

## Files: attach by GROUP NAME, never by DOM order
`llm_driver._upload_files()` attaches by DOM order (correct on Phenom: resume then cover letter).
Workday renders Cover Letter ABOVE Resume/CV on some tenants, so DOM order would file the CV as
the cover letter. `WD_FILES_JS` maps each `input[type=file]` to its enclosing group's accessible
name and `wd_upload()` matches `resume|cv|curriculum` and `cover`.

Also: **Workday STACKS uploads.** A returning applicant already has a resume attached — the
recording literally begins with `Delete jev gen il cyber.pdf`. `WD_DELETE_JS` finds a
`Delete <file>.pdf` control and removes it first.

Use `set_input_files` on the input, never a click that opens the OS file dialog — there is no
desktop file chooser in the container. (`skills/ats-workday/workday_upload.py` and
`flows/workday_upload.py` are the older shims for that; `wd_upload()` supersedes both and neither
is on the live path.)

## Autofill with Resume — AVOID
Offered on some tenants. It parses the CV server-side and OVERWRITES fields, including ones we set
correctly, and the parse quality is worse than our own data. Fill explicitly. If a tenant forces
it, upload first and correct afterwards — never the other way round.

## The account gate
A Workday application REQUIRES an account. `wd_account()` tries, in this order:
1. **A stored password** — `out/ats_accounts.json`, written by `python jhw.py atspw <host> '<pw>'
   [email]` or `python jhw.py import-passwords <chrome-export.csv>`. Matched per exact host, then
   per TENANT. Secrets live ONLY in that file (chmod 600). Never in argv, never in .env, never
   logged.
2. **"Use My Last Application"** — a returning-candidate shortcut this tenant offers.
3. **SSO** (`Sign in with Google` / Microsoft). **We do NOT type a password into an identity
   provider.** The adapter detects SSO, stops with `stage="sso"`, and tells the human to sign this
   Chrome into that account ONCE in noVNC (`http://localhost:9090`) — the session persists in the
   sandbox profile, so it is a one-time cost. A blind attempt risks locking the account for every
   later application. **accenture.wd103 is a Google-SSO tenant in the recording**, so expect this
   on the first run.
4. **Create Account** — email + password + Verify New Password + the consent checkbox. The new
   password is stored immediately. If Workday emails a verification code, that is the ONE place
   the run waits for the human (asked on Telegram).

## Traps that have actually cost runs
1. **A control without its question is unactionable.** `WD_ENUM_JS` derives the question from the
   enclosing `[role=group]`'s accessible name (`aria-labelledby` -> the referenced nodes' text),
   then the legend, then the container's text minus the control's own text. Match answers by
   **SUBSTRING**: Workday reorders questions and injects conditional ones, so position is
   meaningless.
2. **Answering "Yes" to *immediate family in government* spawns an extra required question.** The
   truthful answer is No; answer it truthfully and the extra question never appears.
3. **Gender is labelled optional and is often REQUIRED** — validation blocks Save. Answer it.
4. **A checkbox is SET, never toggled.** A second click UNTICKS it and from that moment Save can
   never succeed. Routed through `llm_driver.CHECK_JS`, which leaves an already-ticked box alone.
   `wd_consent()` only ticks boxes whose label matches accept/agree/consent/acknowledge/certify/
   terms/privacy — a marketing opt-in is left alone.
5. **`aria-expanded` alone does not mean "dropdown".** Workday's *Add* button (another work-history
   block) carries it too. `WD_ENUM_JS` excludes navigation by text (`NAV_RE`).
6. **`innerText` is layout-dependent and can read empty.** Every text read in `workday_wd.py` is
   `innerText || textContent`. An empty read used to cost the control BOTH its value and its
   question. (`llm_driver`'s `FILL_JS`/`SELECT_JS`/`SWEEP_JS` still read `innerText` bare; that is
   fine in Chrome and is proven by `test_llm_driver_dom.py`, but do not copy the pattern.)
7. **Never `Escape` on a date box** — it reverts an uncommitted value. Escape is safe for closing a
   dropdown PORTAL, and that is the only place `workday_wd.py` uses it.
8. **A page mid-render looks exactly like a finished one.** `_read_page()` refuses to act on fewer
   than 6 controls and waits, up to 4 times.
9. **A stall must never end the run.** Two `Save and Continue` clicks with an unchanged page
   fingerprint hands the page to `llm_driver`, which can ask the candidate on Telegram. Standing
   order: sweep, then ask — never stop.

## Where the answers come from
**`agent/userdata/candidate.md` is the ONE source of facts.** `workday_wd.candidate_answers()`
parses every `- key: value` line from it; later sections win, so `## screening_defaults` (the
authoritative ALWAYS-answers block, and the last section in the file) takes precedence. `ASK` is
not an answer.

This skill folder also contains `candidate_profile.md`, which is an OLDER COPY of the same facts.
Nothing in the code reads it — `agent.py::_screening_block()`, `workday_wd.py` and `jhw_bot.py` all
use `userdata/candidate.md`. It is a second home for one value and should be deleted; until then,
**candidate.md wins.** Do not edit the copy.

The QUESTION WORDING lives in `workday_wd.WD_Q` (code), because it is knowledge about Workday, not
a fact about the candidate. Entries marked `(rec)` are verbatim prefixes from the real submission:

| question (substring)                | answer          | source |
|-------------------------------------|-----------------|--------|
| `all individuals employed by`        | Yes             | (rec)  |
| `at your current employer, are`      | No              | (rec)  |
| `legal gender`                       | Male            | (rec)  |
| `pronoun`                            | He/Him          | (rec)  |
| `legally authorized to work`         | Yes             | candidate.md |
| `sponsorship` / `require a visa`     | No              | candidate.md |
| `immediate family`                   | No              | candidate.md |
| `preferred method of communication`  | Email           | candidate.md |
| `relocat`                            | No              | candidate.md |
| `travel`                             | Yes             | candidate.md |
| `how did you hear`                   | Job Boards -> LinkedIn | (rec) |

An unmatched question returns `''` — the adapter then takes the FIRST real option **only if the
control is required**, and says so in the log and in `warnings`. Standing candidate instruction: a
site whose list lacks the answer must not block the application. An OPTIONAL unknown picker is
left alone.

## Submission
`AUTOSUBMIT` is on (candidate authorised end-to-end submission, 2026-07-21). Submission goes
through **`llm_driver._submit()`** — reused, never reimplemented — so `submitted` means the same
thing on Workday and Phenom: the SITE said so (`thank you` / `application submitted|received` / a
confirm|success URL). A click that throws no error is NOT evidence. `JHW_AUTOSUBMIT=0` stops at
Review.

## Regression guard
`flows/test_workday_dom.py` runs the REAL primitives against static replicas of these widgets and
is executed by `python jhw.py apply` (jhw.py::_dom_selftest), along
`test_llm_driver_dom.py` and `workday_wd.py --logic`. A guard the human has to remember to run
does not exist. When a tenant surprises you, get ground truth with `python jhw.py inspect`
(-> `out/dom_dump.json`) and add a replica here — never guess a selector.

## What NOT to do
* Do not add a tenant-specific `data-automation-id`/WID (e.g. `input[id='64cbff5f...']`) — they do
  not port between tenants or Workday UI versions. ARIA roles and text do.
* Do not click "Apply with LinkedIn" / Indeed / Dropbox — they start an OAuth dance.
* Do not click the progress bar, Back, or Sign Out.
* Do not re-navigate once inside the wizard. `workday_wd.py` hands off to `llm_driver.run(url="")`
  precisely so the driver cannot `goto` and throw away the signed-in session.
* Do not use `workday.py` or `workday_gevernova.py` for a new tenant. See the note in
  `workday_wd.py`'s header: the first cannot see the portal dropdowns at all and hard-stops at
  Review; the second is a linear script hardcoded to ONE GE Vernova posting.
