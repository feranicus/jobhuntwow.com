# Application Playbook — how JobHuntWOW applies to jobs (for Hermes)

## The mission
Given a job (usually a LinkedIn URL), apply on the candidate's behalf: read the JD, use the tailored
resume + cover letter, drive the apply flow (LinkedIn Easy Apply OR an external ATS), fill every field
from the Candidate Profile, and **STOP at the final Review page — the human clicks Submit.**

## GOLDEN RULES (never break)
1. **Never click Submit / Send Application.** Fill everything, reach Review, then hand back to the human.
2. **Never invent data.** Use the Candidate Profile. If a required value is missing, ASK the candidate
   on Telegram, use the reply, and **save it to memory** so it's copy-pasted next time (ask once, reuse everywhere).
3. **Correct the country FIRST.** Forms default to "United States". Set Country/Territory = Germany before
   other fields — it re-derives the state dropdown and the phone country code (+49).
4. **Legal name on legal fields** (Evgeny Vainshtein), not the CV display name.
5. **Resume upload = give the absolute Windows path** to the file input; don't try to "attach" any other way.
6. **Handle the new-tab hop.** On company career sites (e.g. careers.<company>.com) the "Apply" button
   opens the real ATS (Workday/Greenhouse/etc.) in a NEW TAB/popup — follow it, then continue there.
7. **If the DOM snapshot is empty, take a vision screenshot** — the content is likely in a new tab or iframe.
8. **Email verification / OTP / CAPTCHA = pause and ask the human.** The agent can't read the inbox.
9. **Watch live** via a local visible browser: in the Hermes CLI run `/browser connect` (opens a real
   Chrome window, keeps it open across turns, reuses your sessions).

## ATS ground truth (verified from real recordings)

### Workday (e.g. GE Vernova: gevernova.wd5.myworkdayjobs.com)
Entry: careers.<company>.com job page → cookie "Accept" → **"Apply for …"** link (opens a NEW TAB) → Workday.
Flow: **Start Your Application** (Autofill with Resume / Apply Manually / Use My Last Application)
→ account (Create Account if new: Email + Password + Verify New Password + "Create Account"; else Sign In)
→ Resume/CV upload (often REQUIRED — "Select files" → file input) 
→ **Step 1 My Information** (How did you hear / former-employee Yes-No / Country / Name / Address / Phone)
→ **Step 2 My Experience** (Work Experience "Add", Education "Add", Skills, Resume/CV, LinkedIn URL)
→ **Step 3 Application Questions** (work-auth / export-control / agreements — dropdowns: open the
  "Select One Required" control inside each question group, then click the answer; some render in a listbox)
→ **Step 4 Voluntary Disclosures** (Gender, consent checkbox)
→ **Step 5 Review** → STOP.
Notes: "Save and Continue" advances each step; a required Resume upload disables it until a file is attached.
Screening answers → see Candidate Profile "Work authorization".

### LinkedIn Easy Apply
The apply UI lives inside `[data-testid="interop-iframe"]`. Fill contact + screening questions, click
Continue through to the Review step, then STOP before Submit.

### Greenhouse (job-boards.greenhouse.io)
Single-page form: First/Last/Email, phone (country combobox → Germany), Resume/CV attach (group
"Resume/CV*" → Attach → file input), country/location comboboxes, custom questions. Stop before Submit.

### iCIMS (careers-*.icims.com)
Form is inside `iframe[name="icims_content_iframe"]`. Fill email + consent, then it typically hits an
**hCaptcha + email OTP** wall — the agent must HAND OFF to the human (do not attempt to solve CAPTCHAs).

### Others (SuccessFactors, Taleo, Personio, HiBob)
Not yet recorded. When one appears, record the flow once (or let Hermes learn it) and add it here.

## The "ask once, remember" loop
When a field's value isn't in the Candidate Profile / memory:
1. Ask the candidate a single clear question (Telegram).
2. Use the reply to fill the field.
3. Save the answer to memory keyed to the candidate, so the next form is filled with no question.
This is the whole point: the FIRST application of each ATS teaches the answers; later ones are copy-paste.
