---
name: ats-workday
description: STRICT step-by-step execution script for autonomous Workday job applications. Follow orders literally — do NOT discover, improvise, or "be smart". Validated against live GE Vernova Workday (R5038183). Any deviation from the recipes below BREAKS the submission (synthetic clicks revert, dropdowns un-set, Save disables). Use for all *.myworkdayjobs.com / workday.com apply flows. Auto-submits at Review (user override 2026-07-19).
---

# ATS-WORKDAY — LOCK-STEP EXECUTION SCRIPT

You are one of potentially MANY agents running this. Another agent WILL break
the application if it improvises. This file is the source of truth. Obey it
exactly. If a step's described widget is missing, STOP and report — do NOT
guess an alternative interaction.

## ⛔ HARD PROHIBITIONS (violating any of these breaks the apply)

1. **NEVER call `browser_console` to `.click()` a dropdown/typeselect button**
   to SET a value (except the two sanctioned recoveries below). Synthetic
   `.click()` updates React state but the NEXT snapshot re-render REVERTS it
   and can corrupt sibling selections. Use `browser_click` (real CDP) only.
2. **NEVER snapshot after every click.** One snapshot per logical block
   (max ~6 actions). Snapshot-per-click is what turns minutes into hours AND
   triggers the revert bug.
3. **NEVER set a dropdown, then open a DIFFERENT dropdown, expecting the first
   to stick** without re-verifying. Workday reverts prior selections when a new
   portal opens. Set ONE, verify THAT one changed, move on.
4. **NEVER invent an answer.** Pull from `candidate_profile.md`. If absent,
   use the CONFIRMED DEFAULTS block below (these are real, user-authorized).
5. **NEVER leave a field "Select One" if the profile/confirm block has a value.**
6. **NEVER pause at Review.** Click Submit (auto-submit rule). Only PAUSE for
   email-verification during account creation.

## ✅ THE ONLY SANCTIONED SYNTHETIC-CLICK USES (read carefully)

- **Native `<input type=checkbox>`** (Terms consent): real `browser_click` on
  the wrapper often fails to toggle. Use `browser_console`:
  `document.querySelector('input[type=checkbox]').click()` — checkboxes are
  reliable this way (unlike portals).
- **Stuck dropdown that won't reopen** (pre-filled wrong, `[expanded]` never
  appears on real click): synthetic-open + synthetic-option-click to set React
  state, then IMMEDIATELY `browser_click` Save & Continue with NO snapshot
  between (the snapshot re-render is the revert). This is the ONLY way to fix a
  corrupted pre-set field.

## PRE-FLIGHT (load BEFORE opening the page)

**Read the co-located `candidate_profile.md` in this skill's folder.** It holds
ALL of Evgeny Vainshtein's real data — name, address, education, work history,
skills, screening answers, and the resume path. Pull EVERY answer from it. DO
NOT ask the user for any field present in that file. If the file is missing,
STOP and report; do NOT fall back to asking the user or to guessing.

## CONFIRMED DEFAULTS (user-authorized — use when profile is silent)

```
Country                 → Germany
First/Last name         → Evgeny Vainshtein
Address                 → Hermann-J.-Bach-Weg 16, 61169 Friedberg, Germany
Phone                   → +49 15785541545 (Mobile)
How did you hear        → LinkedIn  (under "Job Board" cascade)
Former GE Vernova emp   → No
non-compete             → No
agree confidential      → Yes, I agree.
authorized to work      → Yes
govt employee           → No
family govt employee    → No          ← TRUTHFUL. Do NOT set Yes.
comm method             → Email
Gender                  → Male        ← self-disclosure; required despite "optional" label
consent terms           → CHECKED
LinkedIn URL            → https://www.linkedin.com/in/feranicus/
resume-path             → absolute path to the tailored PDF on local disk
```

Match questions by SUBSTRING, not position. Workday reorders and injects
conditional questions (e.g. family-govt=Yes spawns an extra required question —
so always answer No when truthful).

═══════════════════════════════════════════════
ORDERED EXECUTION
═══════════════════════════════════════════════

## STEP 0 — ACCOUNT (first apply per ATS only)

1. `browser_click` Apply → "Apply manually" (NOT Easy Apply).
2. Fill email + password (reuse same creds on every ATS; if "account exists"
   → Sign In instead).
3. IF email-verification appears → PAUSE, message user, wait. Do not proceed.
4. After verify you land on Step 1. Account + data persist server-side, so
   later applications for this user REUSE Step 1/2.

## STEP 1 — MY INFORMATION (set Country FIRST)

1. `browser_click` the Country field → `browser_type` "Germany" → pick the
   suggestion (ArrowDown+Enter or click it). **Country must be set before
   other fields; it re-renders the form.**
2. `browser_type` First/Last name, Address, Phone from profile/defaults.
3. "How did you hear": typeahead cascade → select "LinkedIn" (under "Job Board"
   if a submenu). Free text fails — must pick from list.
4. "Former employee?": No.
5. Land/Bundesland: leave BLANK (optional).
6. ONE snapshot → confirm labels filled. `browser_click` Save and Continue.

## STEP 2 — MY EXPERIENCE (Resume is MANDATORY to leave)

1. Work Experience: `browser_type` Job Title + Company. Check "I currently work
   here" if applicable. From/To via Month/Year spinbuttons. If a spinbutton
   glitches (value resets on blur), set via calendar picker or, last resort,
   console `input.value=` + native setter — but prefer the visible control.
2. Education (one group per institution):
   - School/University: `browser_type`.
   - **Degree dropdown (the hard one):**
     a. `browser_click` the Degree button → it opens a portal (`[role=listbox]`).
     b. `browser_snapshot` → find `[role=option]` whose text is the target
        (e.g. "Bachelor of Science (B.S.)", "Diploma").
     c. `browser_click` that option. The button label must change.
     d. If the portal will NOT open on real click: synthetic-open
        (`btn.click()`) then real `browser_click` the option — Degree is the
        one dropdown where synthetic-open + real-option-click is reliable.
   - Field of Study / GPA: `browser_type` or leave blank.
   - From/To years: spinbuttons.
3. LinkedIn URL: `browser_type` the profile URL.
4. Skills: optional free-text chips; SKIP if the suggestion list is unreliable
   (the uploaded CV covers it).
5. **Resume/CV — REQUIRED. Upload with NO human click:**
   - Import the shim bundled with this skill: `workday_upload.py`.
   - Playwright agent: `upload_via_playwright(page, resume_path)` — it clicks
     "Select files" and feeds the file into the intercepted chooser.
   - Raw CDP agent: `await upload_via_cdp(session, resume_path)` — sets the
     hidden `<input type=file>` directly and dispatches input/change so React
     registers it (no OS dialog at all).
   - The human MUST NOT pick the file. If neither primitive is available in the
     host agent → flag as blocker, do NOT fake the upload.
6. ONE snapshot → Resume shows filename + KB, no "Errors Found".
   `browser_click` Save and Continue.

## STEP 3 — APPLICATION QUESTIONS (revert trap — follow exactly)

For EACH question, in order:
  a. `browser_click` the question's button (opens `[role=listbox]`).
  b. `browser_snapshot` → `browser_click` the option matching the answer map.
  c. Confirm that ONE button's label changed. Move to next. (Do not snap after
     every single one, but if a later open seems to have unset an earlier one,
     re-open and re-set that one, then continue.)
Targets (substring → option text):
  - "non-competition" → No
  - "confidential or proprietary" → Yes, I agree.
  - "legally authorized to work" → Yes
  - "ever been a Government employee" → No
  - "immediate family member" → No   (answering Yes spawns a required extra
                                      question — avoid by answering No)
  - "communication method" → Email
If a question is pre-filled WRONG and won't reopen (corrupted field):
  - `browser_console`: `btn.click()` → find option → `opt.click()` → wait 150ms
    → read `btn.textContent` (proves React state set).
  - IMMEDIATELY `browser_click` Save and Continue — NO snapshot between.
One final snapshot → all six correct. `browser_click` Save and Continue.

## STEP 4 — VOLUNTARY DISCLOSURES

1. Gender: label says optional but is OFTEN REQUIRED (validation blocks Save).
   If "Errors Found → Gender is required" appears, `browser_click` Gender →
   `browser_snapshot` → `browser_click` the self-disclosure option (Male per
   defaults). Do not assume optional = skippable.
2. Terms checkbox: `browser_console`
   `document.querySelector('input[type=checkbox]').click()`. Verify
   `checked=true` via one snapshot.
3. ONE snapshot → no "Errors Found", consent checked. `browser_click` Save
   and Continue. (If Save is `disabled`, a required field is still empty —
   re-snapshot, find the error, fix it, retry. Do NOT force-submit.)

## STEP 5 — REVIEW (auto-submit)

1. ONE snapshot. Confirm: name/address/email/phone, resume filename + size,
   all screening answers (esp. family-government = No), Gender set if required,
   consent checked.
2. If anything wrong → `browser_click` Back, fix, return. Do NOT submit bad data.
3. `browser_click` **Submit**. Application complete.
4. Report URL + status to the user channel (Telegram / web portal).

═══════════════════════════════════════════════
ANTI-BOT / SCALE (JobHuntWOW architecture)
═══════════════════════════════════════════════
- Browser on USER'S machine / residential IP (Docker per user). NEVER a cloud
  datacenter IP — ATS bot-detection (HUMAN, PerimeterX, LinkedIn fingerprint)
  burns DO/IPs. The agent failed this session partly because the human had to
  pick the file; at scale the CDP upload shim removes that.
- Brains + Telegram + React profile-portal in cloud (DO); they push
  `candidate_profile.md` to the local agent. NO noVNC-in-cloud (datacenter IP).
- Pre-warm: one-time account per user per ATS; reuse auth session/cookies.
- Template cache: first Workday variant writes its selector map; reuse later.

═══════════════════════════════════════════════
END-OF-APPLICATION CHECKLIST
═══════════════════════════════════════════════
- [ ] Step 1 Country=Germany, name/address/phone correct
- [ ] Step 2 resume uploaded (filename + KB), educations present, LinkedIn URL
- [ ] Step 3 all 6 screening answers correct (family-government = No)
- [ ] Step 4 Gender set if required, consent checkbox checked=true
- [ ] Step 5 Review matches profile, Submit clicked, confirmation shown
- [ ] Status reported to user channel
