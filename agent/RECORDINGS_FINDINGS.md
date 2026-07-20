# Recording-derived ground truth (real selectors from Playwright codegen)

Source: agent/recordings/*.py (recorded by the candidate; passwords redacted; folder gitignored).
These are the ACTUAL accessible names/roles clicked — build adapters from these, never from guesses.

## LinkedIn SEARCH  (flows/linkedin_search.py ✓)
- The jobs search UI is INSIDE `[data-testid="interop-iframe"]` (.content_frame). This is why raw DOM
  scraping saw "empty" — the app is in an iframe.
- Top keyword box (main page): textbox "Title, skill or Company".
- Filters (inside iframe): button "Date posted filter" -> text "Past month" -> button "Apply current
  filter"; "Experience level filter" -> "Mid-Senior level"; "Remote filter" -> "Remote".

## LinkedIn EASY APPLY  (flows/easyapply.py ✓)
- Entirely inside the interop-iframe. Open: button "Easy Apply to <job>".
- Resume: button "Upload resume" -> get_by_label("Upload resume", exact).set_input_files(...).
- Questions: textbox (e.g. "How many years... SAP Products?*"), radio group (Yes/No), native <select>
  (select_option "Yes"). -> answered by text-only LLM.
- Nav: button "Continue to next step" -> "Review your application" -> STOP at "Submit application".

## WORKDAY  (Accenture wd103 — flows/workday.py to be UPGRADED from this)
- LinkedIn "Apply to <job>" opens a POPUP (Accenture landing) -> "APPLY NOW" opens ANOTHER popup =
  Workday. Deep-link to `.../apply/applyManually` bypasses both.
- Cookies: button "Accept Cookies" (Workday) / "Allow all cookies" (landing).
- Account: button "Use My Last Application" (shortcut!) then **"Sign in with Google"** (SSO) — this
  tenant uses Google SSO, NOT email/password (Red Hat wd5 used email/password). Adapter must handle both.
- "How Did You Hear About Us?": textbox -> click -> text "Job Boards" -> text "LinkedIn" (prompt tree).
- Resume: button "Delete <file>.pdf" (remove existing FIRST) -> label "Resume/CV" button "Select files"
  -> group "Resume/CV" input[type=file].set_input_files(...).
- Questions: group "<question>" -> label "Select One Required" -> click -> text "Yes"/"No"; also
  button "Select One Required" -> role=listbox -> "Yes".
- Self-ID: button "What is your legal gender?" -> "Male"; button "Please Select Your Pronoun" ->
  option "He/Him".
- Consent: checkbox "I accept.". Nav: button "Save and Continue". Final: button "Submit".
- CONFIRMED real automation flow: cookie -> Use My Last Application/Sign in with Google -> How-did-you-
  hear -> resume(delete+upload) -> questions -> self-id -> consent -> Save and Continue -> Submit.

## GREENHOUSE  (GitLab — flows/greenhouse.py ✓)
- No account, single page. button "Apply".
- textbox "First Name" / "Last Name" / "Email"; phone: group "Phone" -> "Toggle flyout" -> combobox
  "Country" fill "ger" -> option "Germany +" -> textbox "Phone" fill national number.
- Resume: label "Resume/CV*" button "Attach" -> group "Resume/CV*" get_by_label("Attach").set_input_files.
- textbox "LinkedIn Profile"; textbox "What's the name you'd prefer" (preferred name).
- Country dropdowns: combobox "...country..." fill "ger" -> option "Germany".

## Cross-cutting truths (all match RESEARCH_ATS_AUTOMATION.md)
- Role/label/text selectors (get_by_role/get_by_label) are FAR more robust than data-automation-id and
  work across tenants. Prefer them; keep data-automation-id as fallback.
- Resume file inputs are hidden; set_input_files works on them. Delete existing first where offered.
- LLM = free-text/choice answers ONLY (years experience, Yes/No, dropdowns). Never drives navigation.
- STOP before the final Submit everywhere. Human submits.

## iCIMS  (Atlassian globalcareers-atlassian.icims.com — flows/icims.py ✓, human-assisted)
- Form is inside `iframe[name="icims_content_iframe"]`. Entry: link "Apply for this job online".
- Pre-gate: textbox "Email", a "Make a Selection" <select> (tenant-specific option ids — skip/LLM),
  checkbox "I accept", button "Next".
- ⚠️ HARD ANTI-BOT GATES (cannot/should-not be automated): **hCaptcha** (iframe[title="hCaptcha
  challenge"]) image challenge, and **email OTP** (6-digit code emailed; recorded flow fetched it from
  Gmail and pasted into textbox "Verification code" -> button "Submit verification code"). Also Google
  SSO ("Continue with Google").
- ADAPTER BEHAVIOUR: fill email + consent, click Next, then DETECT CAPTCHA/OTP and HAND OFF to the human
  (pause + Telegram; solve in noVNC or reply with the code). Never solves a CAPTCHA. This is the ceiling
  for compliant iCIMS automation — matches RESEARCH_ATS_AUTOMATION.md (CAPTCHAs are a hard stop for all
  agents). Future: Gmail-API OTP fetch could auto-handle the code, but the CAPTCHA stays human.
