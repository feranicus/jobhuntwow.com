---
name: ats-workday
description: Autonomous Workday job-application playbook for the Hermes browser agent. Covers account creation, the 5-step apply flow (My Information → My Experience → Application Questions → Voluntary Disclosures → Review), and the exact interaction recipes that survive Workday's React re-render quirks. Use for ANY Workday-hosted apply flow (gevernova.wd5.myworkdayjobs.com, *.myworkdayjobs.com, workday.com/.../apply). Target time per application AFTER first run: under 60s.
---

# ATS-Workday Apply Playbook

This skill turns a slow, exploratory browser session into a fast, pre-solved
execution. The agent MUST follow the recipes below verbatim — Workday's
widgets reject "smart" interaction. Every recipe was validated against a live
GE Vernova Workday instance (R5038183).

## GOLDEN RULES (violating these is what wastes hours)

1. **Use REAL CDP events only.** `browser_click` / `browser_press` only.
   Synthetic `element.click()` via `browser_console` CORRUPTS Workday's
   React state — it sets the value but a later re-render REVERTS it AND
   can flip sibling selections. NEVER use DOM `.click()` on dropdowns
   (exceptions: native `<input type=checkbox>` and the "stuck field" recovery below).
2. **Country = Germany FIRST** on My Information. If the candidate is in
   Germany/EU, set Country before anything else; it re-renders the form.
3. **Never invent data.** Every answer comes from `candidate_profile.md`.
   For self-disclosure fields with no profile value (e.g. Gender on the
   Voluntary Disclosures step), the field may still be REQUIRED — read the
   validation error, do not assume "optional" labels are skippable.
4. **Batch actions, verify once.** Do N interactions, then ONE snapshot to
   verify. Snapshot-after-every-click is the silent time-killer.
5. **Auto-submit at Review** (user override 2026-07-19): do NOT stop and
   wait. Click Submit. Still PAUSE on email-verification during account
   creation, and still ASK (once, first run) before filling any field the
   profile does not cover.

## PRE-FLIGHT (load before opening the page)

Read `candidate_profile.md`. Build an in-memory answer map keyed by question
substring → answer. For the standard GE Vernova / generic Workday screen:

```
non-compete    → "No"
agree          → "Yes, I agree."      (exact label varies: "Yes" or "Yes, I agree.")
authorized     → "Yes"
govt-employee  → "No"
family-govt    → "No"
comm-method    → "Email"
gender         → "Male" | "Female" | "Non-Binary" | "Wish not to disclose"
consent        → CHECK the terms checkbox
resume-path    → absolute path to tailored PDF on local disk
```

Match by SUBSTRING of the question text, not position — Workday reorders
and injects conditional questions.

## STEP 0 — ACCOUNT CREATION (only on first apply per ATS)

- Click Apply → "Apply manually" (not Easy Apply).
- Email + password from profile `credentials`. Reuse the SAME credentials on
  every ATS (Workday dedupes by email; "account exists" → use Sign In).
- If an **email-verification** step appears → PAUSE, tell the user, wait.
- After verify, you land on Step 1. The account + submitted data persist
  server-side per ATS, so later applications for this user REUSE Step 1/2.

## STEP 1 — MY INFORMATION

- Country: typeahead. `browser_click` the Country field, `browser_type` the
  country, ArrowDown/Enter or click the suggestion. Set GERMANY first.
- Name / Address / Phone: `browser_type` from profile. Do NOT invent.
- "How did you hear": typeahead cascade — select "LinkedIn" (under
  "Job Board" if a submenu appears). Free text fails; must pick from list.
- "Former GE Vernova employee?": "No" (per profile).
- Land/Bundesland: OPTIONAL, leave blank if the widget won't open.
- Verify once, Save & Continue.

## STEP 2 — MY EXPERIENCE (Resume is MANDATORY)

- Work Experience: `browser_type` Title + Company. Check "I currently work
  here" if applicable. From/To use Month/Year spinbuttons — if a
  spinbutton glitches (value resets), set it via the calendar or console
  `input.value` + native setter ONLY as last resort.
- Education: Add per institution. **Degree dropdown = the hard one.** Recipe:
  1. `browser_click` the Degree button (opens portal).
  2. Snapshot → find the `[role=option]` for the target degree
     (e.g. "Bachelor of Science (B.S.)", "Diploma").
  3. `browser_click` that option. Verify the button label changed.
  If the portal won't open via click, the DOM `.click()` on the button
  then real-click on the option works (Degree is the one exception where
  synthetic-open + real-option-click is reliable).
- LinkedIn URL: `browser_type` the profile URL.
- Skills: optional free-text chips; skip if suggestion list is unreliable
  (the uploaded CV covers it).
- **Resume/CV: REQUIRED to leave the step.** This is a NATIVE OS file
  dialog the browser agent CANNOT set via JS. In the Docker/local agent
  this MUST be solved with CDP `DOM.setFileInputFiles` (or Playwright
  `setInputFiles`) pointed at the absolute `resume-path`. The human must
  NOT pick the file — that defeats scale. If the supervisor exposes no
  file primitive, flag it as a blocker, do not fake the upload.
- Verify: Resume shows filename + size, no "Errors Found". Save & Continue.

## STEP 3 — APPLICATION QUESTIONS (the revert trap)

Workday REVERTS a dropdown's value when a DIFFERENT dropdown's portal
opens. Mitigation:

1. Set dropdowns ONE AT A TIME. After each `browser_click` open →
   `browser_click` option → verify that ONE label changed (spot-check,
   not full snapshot every time).
2. If a question is pre-filled WRONG and its portal won't reopen via
   real click (stuck at a value, `[expanded]` never appears):
   - `browser_console`: `btn.click()` to open, find `[role=option]`,
     `opt.click()`, wait 150ms, read `btn.textContent` (proves React state).
   - IMMEDIATELY `browser_click` Save & Continue — NO snapshot between
     (the snapshot re-render is what reverts it). The submit carries the
     synthetic-set value. This is the ONLY sanctioned synthetic-click use.
3. Conditional questions: answering "Yes" to family-government spawns an
   extra required question. Prefer "No" when truthful; if "Yes" is set,
   you MUST also answer the spawned conditional or Save stays disabled.
4. Verify all required answers, then Save & Continue.

## STEP 4 — VOLUNTARY DISCLOSURES

- Gender: labeled optional but is often REQUIRED (validation error blocks Save).
  Read the error; if required, self-disclose per profile identity
  (legitimate self-ID, not invented data).
- Terms checkbox: click the REAL `<input type=checkbox>` (id usually
  `termsAndConditions--...`), NOT the wrapper `div`/`label`. Real
  `browser_click` on the wrapper often fails to toggle;
  `document.querySelector('input[type=checkbox]').click()` is reliable here
  (checkbox is the other sanctioned synthetic-click exception).
- Verify consent `checked=true` and no "Errors Found", Save & Continue.

## STEP 5 — REVIEW (auto-submit)

- Snapshot once. Confirm: name/address/email/phone, resume filename,
  all screening answers, consent. If anything is wrong, Back and fix — do
  NOT submit bad data.
- Click **Submit**. Done. Report the application URL + status to the user
  (Telegram / web portal).

## ANTI-BOT / SCALE NOTES (for the JobHuntWOW architecture)

- Browser MUST run on the USER'S machine / residential IP (Docker per user),
  NOT a cloud datacenter IP (DigitalOcean etc.) — ATS bot-detection
  (HUMAN, PerimeterX, LinkedIn fingerprinting) burns datacenter IPs.
- LLM brains + Telegram + React profile-portal live in cloud (DO).
  They push `candidate_profile.md` to the local agent; they do NOT stream
  the browser (no noVNC-in-cloud).
- Pre-warm: one-time account creation per user per ATS; reuse the
  authenticated session/cookies for all that user's applications there.
- Template cache: first encounter of a Workday variant writes its selector
  map to disk; later users reuse it (compounding speedup).

## VERIFICATION CHECKLIST (end of every application)

- [ ] Step 1 Country=Germany, name/address/phone correct
- [ ] Step 2 resume uploaded (filename + KB shown), educations present
- [ ] Step 3 all screening answers correct (esp. family-government=No)
- [ ] Step 4 Gender set if required, consent checkbox checked
- [ ] Step 5 Review matches profile, Submit clicked, confirmation shown
- [ ] Status reported to user channel
