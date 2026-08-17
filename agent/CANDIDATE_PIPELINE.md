# Candidate pipeline — Electronic talks, the stack applies

Electronic is kept for what it is genuinely good at: **talking to the candidate and writing prose**.
The Playwright/Stagehand stack does the clicking. One universal profile feeds every ATS.

```
  CANDIDATE (Telegram / portal)
        |  Electronic interviews once  (asks only what's missing)
        v
   userdata/candidate.md          <-- UNIVERSAL, ATS-agnostic single source of truth
        |
        +--> per JOB: Electronic + DO models write tailored resume.pdf + cover_letter.pdf
        |
        v
   APPLY ENGINE   (detect ATS -> per-ATS skill + recorded Playwright adapter)
        |            unknown/broken selector -> Stagehand AI -> learn -> persist
        v
   submitted, reported back to the candidate on Telegram
```

## 1. Electronic's job (candidate-facing)
- **Intake once:** interview the candidate, read their uploaded resume + LinkedIn, and WRITE
  `userdata/candidate.md`. Ask only for fields it cannot derive. Never invent.
- **Per job:** given the JD, produce a **tailored resume + cover letter** (DO models), saved as PDFs
  and registered in `candidate.md` under `resume.path` / `cover_letter.path`.
- **During apply:** answer the one-off questions the profile doesn't cover (salary, notice period),
  then **write the answer back into `candidate.md`** so it is never asked again.

## 2. `candidate.md` — the universal map (ATS-agnostic)
Seeded from the validated Workday profile. To serve every ATS it must carry the superset:

| Block | Fields |
|---|---|
| identity | full/first/last name, gender, LinkedIn, portfolio/GitHub |
| contact | email, phone (+ type), full address, city, region, postal, country |
| work_auth | authorized-in (per country), needs sponsorship, visa/permit type, export-control |
| education | school, degree, field, from/to, location (repeatable) |
| work_experience | title, company, from/to, current?, location, summary (repeatable) |
| skills / languages | list + proficiency |
| screening | how-heard, former-employee, non-compete, agreements, contact-method |
| eeo (US ATS) | veteran status, disability, race/ethnicity (all "decline" allowed) |
| logistics | salary expectation, notice period, earliest start, relocation, travel % |
| documents | resume.path, cover_letter.path (per job), certificates |
| ats_accounts | per-vendor email + password_env (reused per ATS, created once) |

Rule: **the file is authoritative.** Agents read it; they never ask for a field it contains.

## 3. ATS coverage roadmap (by market share — see the HCM chart)
Build one skill+adapter per vendor, highest share first. Cumulative reach:

| # | Vendor | Share | Cumulative | Status |
|---|---|---|---|---|
| 1 | Workday | 15.7% | 15.7% | **DONE** (skill + adapter + upload shim) |
| 2 | SAP SuccessFactors | 11.0% | 26.7% | next |
| 3 | Oracle HCM Cloud | 8.5% | 35.2% | next |
| 4 | ADP | 7.0% | 42.2% | |
| 5 | UKG | 6.5% | 48.7% | |
| 6 | Personio | 4.0% | 52.7% | |
| 7 | Cornerstone OnDemand | 4.0% | 56.7% | |
| 8 | HiBob | 3.8% | 60.5% | |
| 9 | Ceridian (Dayforce) | 3.6% | 64.1% | |
| 10 | iCIMS | 3.5% | 67.6% | **DONE** (adapter; hCaptcha/OTP hands off) |
| 11-15 | MS Dynamics, Paychex, Paycom, Paylocity, Rippling | 14.2% | **81.8%** | |

Plus (not in the HCM chart but common for tech roles): **LinkedIn Easy Apply** (done),
**Greenhouse** (done), Lever, Ashby, SmartRecruiters, Taleo, BrassRing.

**Top 5 vendors = ~49% of the market; top 10 = ~68%; top 15 = ~82%.** That is the build order.

## 4. Per-ATS skill folder (the repeatable unit)
```
userdata/skills/ats-<vendor>/
  SKILL.md              # lock-step execution script + hard prohibitions (see ats-workday)
  selectors.json        # recorded selectors; grows as Stagehand learns
  <vendor>_upload.py    # file-upload shim if the vendor needs one
```
`candidate.md` stays at `userdata/` — ONE profile, shared by all skills.

## 5. What makes it fast at scale
- Known ATS -> recorded Playwright, **zero LLM**.
- New/broken -> Stagehand `observe()` returns a concrete selector -> use it AND save it to
  `selectors.json` -> that ATS is deterministic for every candidate afterwards.
- Resume upload -> `workday_upload.py` pattern (filechooser intercept / CDP setFileInputFiles):
  **no human, no OS dialog**.
