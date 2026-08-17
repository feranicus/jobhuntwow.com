# ATS SKILL — Phenom People (careers.services.global.ntt, and most "careers.<brand>.com" sites)

## How to recognise it
* Job URL: `https://<host>/global/en/job/<JOB_SEQ_NO>/<slug>`
* Apply URL: `https://<host>/global/en/apply?jobSeqNo=<JOB_SEQ_NO>&step=1`
* Page shows an **Apply now** button; a chat widget "…Career Guide" may auto-open and cover the page.
* Powered-by / asset paths contain `phenom` or `cdn.phenompeople.com`.

## THE RULE THAT SAVES A STEP (measured on the NTT posting, 2026-07)
Do **NOT** hunt for the "Apply now" button. The apply form is a plain URL derived from the job URL:

    /global/en/job/<SEQ>/<slug>      ->      /global/en/apply?jobSeqNo=<SEQ>&step=1

Navigating straight there lands on the form. Clicking "Apply now" only performs that navigation,
and the click is fragile: a cookie banner, the "Career Guide" chat widget, or a "Restore pages?"
bubble can swallow it. `flows/phenom.py::apply_url()` implements the transform.

## Is there an API? (researched 2026-07 — do not re-litigate)
Phenom publishes a developer API (developer.phenom.com) including an **Apply API** and a
"has this candidate already applied" endpoint. It requires an **OAuth 2.0 token issued to the
EMPLOYER** (request via api-management@phenom.com). There is no candidate-side apply API, exactly
like Workday / SuccessFactors / Oracle. So: browser automation, deterministic where possible.
The Jobs API is readable for job data, which is useful for the SCOUT, not for applying.

## The form (step=1) — what is actually on it
Resume section (all optional paths to the same field):
  * `Apply With LinkedIn` button  — AVOID, it starts an OAuth dance
  * Dropbox / Indeed / Google Drive icons — AVOID
  * **`Upload Resume`** — a real `<input type="file">`; use Playwright `set_input_files`, never a
    native file-picker dialog (the sandbox has no desktop file chooser)
Cover letter section:
  * "Upload your Cover Letter using either DOC, DOCX, PDF, or TXT file types (1MB max)."
  * **`Select File`** — a SECOND `<input type="file">`. Both inputs exist on the same page; pick
    them in DOM order (resume first, cover letter second) or by the surrounding label text.
  * NOTE THE 1 MB LIMIT — our generated PDFs must stay under it.
Personal details: first name, last name, email, phone, address, city, **postal code**, country.
Screening: work authorisation ("eligible to work in Germany without sponsorship"), notice period,
salary expectation, how did you hear about us.
Validation: on submit it renders a red block **"Please enter all required fields:"** with one
"This field is required" line per missing field — that block is the reliable signal of what is
still missing, and it appears ABOVE the offending fields.

## Hard rules for this ATS
1. Go to the apply URL directly; do not click "Apply now".
2. Dismiss the cookie banner and close the "Career Guide" chat widget before enumerating —
   both overlay the form and steal clicks.
3. Two separate file inputs. Uploading the resume twice leaves the cover letter empty.
4. Never click the final Submit — stop at review, the human submits.
5. Emails on the NTT tenant: the page states applications from @ntt addresses are not considered.

## "should be array" — what it means (researched 2026-07, do not re-guess)
It is an **AJV JSON-schema** error: that field's value must be an **array**, and it is a string.
State/Province/Region is a TYPEAHEAD, not a text box - the component only writes into its array
when a SUGGESTION IS SELECTED. Typing "Hessen" fills the visible input, leaves the underlying
model a string, and the error stays. That is why re-typing it forever never cleared it.
FIX: type 3-6 characters, wait ~1.4s for the suggestion list, then CLICK the matching option
(`[role=option]`, `ul[role=listbox] li`, `li.ui-menu-item`, `[class*=autocomplete] li`) or press
ArrowDown+Enter. `llm_driver` exposes this as the **PICK** action. The same rule applies to ANY
field whose value must come from suggestions - never FILL those.

## The option list can be WRONG (measured on NTT, 2026-07)
NTT's State/Province/Region typeahead does not contain German states - "Hessen" is simply absent,
while Spanish provinces like "A Coruna" are. That is a bug in THEIR data, not in the driver.
RULE: exact match -> fuzzy match -> otherwise take the FIRST option, log it loudly and add a
warning to the run result. Never blind-press ArrowDown+Enter on an unfiltered list (that is how
"A Coruna" ended up on a German address), and never stall waiting for a value the site cannot
offer. The human sees the warning at the review gate before submitting.

## The State/Province widget: UNFILTERED list, non-standard markup (measured 2026-07)
Typing "H" does NOT filter it - the dropdown still starts at Aberdeenshire, Abu Dhabi, Aceh,
Ad Daqahliyah, Aichi... It is one long alphabetical list you scroll, and its rows are NOT
[role=option]. Two consequences:
1. Do not wait for [role=option] - it never appears. Search the DOM for a visible option-like
   element (li / [class*=option] / [class*=item] / [role=listitem]) whose TEXT matches, and
   click that. `llm_driver.PICK_JS` does this.
2. Typing the full word is pointless (and typing more can close the list). Type ~3 characters
   just to open it, then match by text.
Chrome's own ADDRESS AUTOFILL popup ("Hessen / Jev Vainsteins / Manage addresses...") renders on
top of this list and steals the click - run-chrome.sh now disables it
(--disable-features=AutofillServerCommunication, --disable-autofill-keyboard-accessory-view).

## "should be array" — what it means and the ONLY way to clear it (measured on NTT)
Phenom validates with AJV against a JSON schema. `should be array` means that field's value must be
an ARRAY of selected objects, not a string. It appears on **suggestion-backed (typeahead) fields** —
on the NTT DACH form: **State/Province/Region** — and on the attachment blocks.

- Typing the value and blurring does NOT clear it, ever. The component only writes its array when a
  suggestion row is CLICKED.
- The suggestion list is **not filtered the way you expect** and its rows are **not** `[role=option]`.
  Locate rows by visible text, geometrically below the input.
- **NTT's list has no "Hessen".** The German states are listed in English: type `Hess` and the single
  row offered is **`Hesse`**. Prefix ladder: full value -> 4 -> 3 -> 2 characters.
- If no row matches at any prefix, **click the FIRST row**. The candidate's standing instruction is
  that a missing option must never stall an application.
- The error text renders immediately AFTER its input inside the same `.form-group`, so the owning
  field is the last input preceding the message in document order.

Do not "fix" this error by touching Country/Region Code — that is a native `<select>`, it is already
valid, and re-selecting Germany changes nothing. That mistake cost 27 wasted steps in one run.
