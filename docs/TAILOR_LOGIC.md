# Tailor — logic & interaction model (reusable spec)

This document describes how the **Tailor** feature works so it can be reused in another project.
It is written from the shipping implementation in `backend/app/electronic.py` +
`backend/app/resume_consensus.py` + `backend/app/documents.py`, not from intent.

> `tailor_core.py` in this folder is the PRE-CONSENSUS standalone extract (one call per document,
> one cross-vendor fallback). It is still a correct description of the truth-gating and the
> gap->answer loop, but it does NOT contain the 4-model consensus described in section 2b. Lift
> `backend/app/resume_consensus.py` instead; it is framework-free and takes an injected poster.

Tailor does ONE thing: **job description IN → tailored résumé + cover letter OUT (DOCX + PDF)**,
without ever inventing a fact about the candidate. Everything else (endpoints, storage, chat) is
scaffolding around that core.


## 1. The non-negotiable rule: truth-gating

The product is trust. Every design choice below exists to keep the model from lying on a document
that goes out under the candidate's name.

- The LLM may **re-angle, reorder, compress and rephrase** facts that are already in the supplied
  profile. It may **never** add an employer, title, date, degree, certification, tool, team size,
  revenue figure or percentage that is not in the profile.
- If the profile has no number for a claim, the bullet stays **qualitative** — no invented metric.
- Truth is enforced in **two independent layers**, because a prompt rule is a request, not a
  guarantee:
  1. **Prompt guardrails** (`_TRUTH`) — stated as hard rules in the system prompt.
  2. **Deterministic post-check** (`truth_check`) — after generation, every organisation named in
     the résumé is checked against the profile text. Anything not found is surfaced as a
     **warning to the human**. It is never silently "corrected" by rewriting prose, and absence is
     never treated as proof of fabrication (the profile may just be terse).

The corollary rule that bit us in production and is now enforced: **never delete an employer to fit
the page limit.** The model returns `all_employers` (its own inventory); `missing_employers()`
compares that against what actually got rendered and flags any drop. Detailed bullets are for recent
roles; every other employer still appears as a one-line "earlier experience" entry. A missing
employer reads as an unexplained gap and gets the candidate screened out — the worst failure this
document can have.


## 2a. The recruiter/headhunter rules (baked into the prompt)

Distilled from mainstream career-office + executive-search guidance (Harvard/Yale résumé guides,
LinkedIn Talent Solutions, Robert Half, Michael Page, Korn Ferry / Heidrick). Where sources
disagree (1 page vs 2) we take the **senior-hire** convention because this targets experienced
candidates.

Résumé (`RESUME_RULES`): 2 pages max for 10+ yrs / 1 page under 10; order = highlights → summary →
experience (reverse-chron) → skills → education; achievements not duties, strong past-tense verbs,
never "Responsible for"; quantify ≥ half the bullets; 3–5 bullets per recent role, ≤ 2 lines each;
ATS-safe (no tables/columns/graphics/headers); no pronouns, no "references available"; every
employer/title/date exactly as in the profile, in profile order.

Cover letter (`COVER_RULES`): 250–350 words, one page, 3–4 short paragraphs; open by naming the role
and leading with the strongest true proof point; middle maps the employer's top requirements to
concrete quantified evidence; confident close, no salary/apologies; never repeat the résumé
verbatim, no generic filler.


## 2b. The 4-model consensus: AUTHOR -> AUDITOR -> AUTHOR REVISES

`backend/app/resume_consensus.py`. The old path made ONE call per document with a single
cross-vendor fallback, and whatever came back WAS the document. Nothing measured whether the answer
was thin, nothing checked it against the JD, and a well-formed nothing (`{}` parses perfectly)
rendered as a finished resume. Quality was therefore whatever the first model happened to do.

**The chain** (measured; four vendors, so no shared failure domain - a 429 is provider-wide):

    deepseek-3.2  ->  llama-4-maverick  ->  gemma-4-31B-it  ->  kimi-k2.6
    deepseek          meta                  google             moonshot

`JHW_TAILOR_MODELS` overrides it, and a divergence from the committed order is PRINTED - a stale env
var silently beating committed code is a bug class this codebase has already paid for.
`model-selection-toolkit/MODEL_SELECTION.md` measured `deepseek-3.2` + `llama-4-maverick` as the
winners on this account, which is why they lead. No thinking/reasoning model is in the chain: they
emit chain-of-thought and cannot honour a strict-JSON contract.

**1. AUTHOR.** Walks the chain. A draft is accepted only if it passes the JSON contract **and** a
deterministic DEPTH FLOOR (`resume_depth` >= 900 chars of summary + highlights + bullets;
`cover_depth` >= 700). Depth, not presence: a model that "answers" every field with a third of the
prose reads as 100% coverage and renders as a template. A thin draft is a quality failure to REPORT
and a trigger to try the NEXT MODEL. Both documents are requested from one model concurrently; if
only one passes, the next model is asked for the one still missing, so a good resume is never
discarded because the cover letter came back empty.

**2. AUDITOR.** `pick_auditor()` returns a model that is NEVER an author and, where the chain
allows, never an author's VENDOR. An audit by the author is not a second opinion: it shares every
blind spot and failure mode. If no distinct model exists the audit is REFUSED and said so in the
manifest - faking independence is worse than not auditing. The auditor returns structured critique
only (`invented_facts`, `employers_dropped`, `weak_bullets`, `missed_keywords`, `cover_issues`,
per-section `scores` 0-10); it never returns document content.

**3. BOUND (`bound_audit`).** The auditor may FLAG; it may never rewrite, delete or empty anything.
Every flag is corroborated DETERMINISTICALLY before it is believed, and every refusal is RECORDED:

| flag | corroborated against |
|---|---|
| `invented_facts` | `claim_is_corroborated()` - numbers must match on DIGIT BOUNDARIES (so 22 never matches 220), words by a 5-char stem (so "Migrated" matches the profile's "migration", because rephrasing is explicitly permitted). A claim with nothing checkable KEEPS its flag - unverifiable means the human decides. |
| `employers_dropped` | `missing_employers()` is the AUTHORITY. The model can flag but not overrule it, and the deterministic list is UNIONED in, so an employer the reviewer missed is still caught. |
| `weak_bullets` | the quoted text must EXIST in the resume, and `bullet_is_weak()` (no digit AND no outcome verb) must agree. A quote from the COVER LETTER is refused by channel: a letter's close has no number by design. |
| `missed_keywords` | three conditions, all required - in the JD, in the PROFILE, and absent from the draft. A keyword the candidate cannot back up must never be added. |

**The gutting guard.** If the corroborated critique still condemns more than 60% of the bullets, or
calls more claims invented than there are roles (>50%), the WHOLE ROUND is refused: the draft
stands, nothing changes, and the reason is recorded (`audit_refused`). An automatic process may
narrow at the margin; it may never gut the artifact.

**4. REVISE.** Only the AUTHOR edits its own document, and `revision_ok()` verifies the result
before it can replace the draft. It is refused if it fails the contract or the floor, if it loses
more than 20% of the prose (fixing a weak bullet means making it concrete, never deleting it), or if
it drops an employer **the profile supports**. That last qualifier is load-bearing and was found by
an end-to-end run: an earlier version protected every employer in the draft, so when the author had
invented "Google" and the auditor caught it, the corrected revision was rejected for "dropping
Google" - the guard shipped a fabrication and kept the real missing employer missing. With no
profile to check against, everything is protected (fail closed).

Rounds are bounded at **2** (`JHW_TAILOR_ROUNDS`) for latency: worst case is 2 author calls + 2
audits + up to 4 revision calls. A round that changes nothing breaks the loop early.

## 3. Data flow

```
        (optional) profile upload           (optional) evidence / photo / links
                 │                                        │
   JD (paste/URL) ─► parse_jd ─► jd{title,company,location,text,status}
                 │                                        │
   profile (dict OR markdown OR extracted CV text) ───────┤
   gap answers {question -> answer}  ─────────────────────┤   (all appended to the profile text)
                                                          ▼
                     generate ──► resume_consensus.tailor()
                                    │
                                    ├─ AUTHOR   walk the 4-model chain; contract + DEPTH FLOOR
                                    ├─ AUDITOR  a different model AND vendor -> structured critique
                                    ├─ BOUND    corroborate every flag; refuse a gutting round
                                    └─ REVISE   author only; verified before it may replace the draft
                                              │        │
                     truth_check + missing_employers   │
                                              ▼        ▼
                        resume_struct / cover_struct  (persisted as tailored.json)
                                              │
                              documents.write_all ──► resume.docx/.pdf, cover_letter.docx/.pdf
                                              │
                                          manifest (job.json): files, gaps, warnings, models, timing
```

Key point for reuse: **the LLM returns structured JSON, not prose.** Rendering is deterministic
(python-docx → PDF). The model never touches layout, so a bad model can produce weak wording but
never a broken or unsafe document.


## 4. The interaction model (this is what you asked about)

Tailor is **conversational by design** because people iterate. The loop:

1. **Ingest JD** — `POST /jd {text?, url?}`. Pasted text always works. A URL is best-effort:
   LinkedIn/Indeed/Glassdoor usually block fetching, so the endpoint returns
   `status: "needs_paste"` and the UI asks the user to paste instead of failing.
2. **Provide the profile** — either a dict/markdown, or upload a real CV
   (`POST /profile/upload`, PDF/DOCX/txt). `extract_text()` pulls text server-side (the browser
   cannot read a PDF), and `contacts_from_text()` recovers name / email / phone / LinkedIn / website
   / location / headline from the top of a real CV that has no `key: value` labels.
3. **First generate** — `POST /generate {email, jd, profile, answers?, evidence?, links?, use_photo?}`.
   Returns a **manifest** with the file list, `keywords_matched`, `gaps`, `warnings`,
   `employers_missing`, which models answered, and elapsed time.
4. **Gaps drive the next turn.** `gaps` = JD requirements the profile does **not** evidence. The UI
   shows them as questions. The user answers; answers are fed back as
   *"ADDITIONAL FACTS CONFIRMED BY THE CANDIDATE"* — this is the one sanctioned way facts enter that
   weren't in the CV, and it keeps the no-invention rule intact (the candidate asserted them).
5. **Attach more** — portfolio/case-study files (`/evidence/upload`), publication links (`links`),
   and a passport **photo** (`/photo/upload`, résumé only, opt-in). Each artifact is capped so one
   long PDF can't crowd the JD out of the context window. A link that can't be fetched is reported
   (`link_notes`), never silently ignored.
6. **Revise in natural language** — `POST /revise {email, job_id, instruction}`:
   *"add Canonical back"*, *"shorter"*, *"more infra, less management"*, *"more formal"*,
   *"kill that bullet"*. Crucially, revise **edits the stored struct, it does not regenerate from the
   JD** — so the tailoring survives, the change is cheap, and the truth rules still apply
   (rearrange / trim / reword only). Re-render, re-check employers, return the updated manifest.

The rule that makes iteration safe: **state lives in `tailored.json`.** Generate writes it, revise
reads-edits-writes it. The JD is only re-consulted on a fresh `generate`.


## 5. What to surface to the user (don't hide the warnings)

The manifest is the contract. Show, don't bury:

- `gaps` → the follow-up questions (turns a one-shot into a conversation).
- `warnings` + `employers_missing` → truth-check output; the honesty layer. Hiding these would
  defeat the product. `warnings` now also carries the consensus record in human words: a claim the
  reviewer could not tie to the profile, a REJECTED revision, a REFUSED audit round, a review that
  never happened, and a thin document.
- `keywords_matched` → why this version fits the JD.
- `models` → `{resume, cover, auditor, auditor_vendor}`. The reader can see that the auditor was not
  the author without taking our word for it.
- `audit` → the whole consensus record: `auditor`, `auditor_vendor`, `authors`, `scores`
  (truthfulness / jd_fit / impact / structure / cover_letter, 0-10), `notes`, `skipped`, and per
  round `{round, verdict, counts, kept, refused, refused_all, refused_reason, revisions}`.
  **`kept`** is what we acted on; **`refused`** is what the reviewer said that we did NOT act on and
  why. Show both - a refusal is information, not noise.
- `audit_rounds`, `audit_refused` → how much review happened, and whether a round was thrown out.
- `quality` → `resume_chars`, `cover_chars`, the floors, `resume_thin`, `cover_thin`, `bullets`,
  `bullets_without_number_or_outcome`. This is the deterministic quality read, independent of any
  model's opinion.
- `attempts` → every draft attempt: `{model, doc, ok, why, depth, ms}`. A rejected model and its
  reason are visible, so "the tailoring is bad" becomes a readable number.
- `models` + `elapsed_ms` + `consensus_ms` → so a slow/failed model is visible, not a mystery spinner.
- `errors` → per-artifact (résumé vs cover vs a specific render) so a partial success is legible.
- on `/revise`: `revise_model` and `revise_rejected` (edits refused for failing the contract or for
  silently dropping an employer).


## 6. Failure behaviour (learned the hard way)

- **Tolerate any JSON SHAPE, reject only an EMPTY answer** — `_json()` unwraps ```code fences```,
  leading prose and `[ {...} ]`. A good answer thrown away by a strict parser costs the whole call.
- **Cross-vendor chain, not a pair** — four vendors, tried in measured order. A 429/outage is
  provider-wide, so a backup that shares a vendor with the head is not a backup.
- **Per-model constraints are ENCODED, never rediscovered** — `MODEL_PARAMS`: kimi requires
  `temperature=0.6`, refuses `response_format`, and needs thinking suppressed. On this gateway
  `max_tokens` and `response_format:json_object` are mutually exclusive, so JSON mode wins and the
  ceiling is dropped (a ceiling cut lands mid-JSON and yields garbage; a timeout is a clean failover).
- **A 400 is repaired by WHAT THE SERVER NAMED** (`repair_payload`) — the temperature is read out of
  the error body rather than trusted from our constant (the required value has changed under us
  before), the field the server names is the field that is dropped, and `chat_template_kwargs` is
  never stripped speculatively because that re-enables thinking. Blanket-stripping a payload is how
  you disable a safeguard you did not know you had.
- **Never raise into a 500 on a model hiccup** — `_ask` returns `{"_error": ...}`; the endpoint
  decides (partial success is allowed: one doc can render while the other reports an error).
- **`.doc` is refused** with a clear message (parsing legacy Word is unreliable) — DOCX/PDF/txt only.


## 7. Reuse checklist

To lift Tailor into another project you need exactly four things — everything else here is FastAPI
plumbing:

1. `backend/app/resume_consensus.py` — the chain, the prompts, the truth rules, the depth floors,
   the auditor selection, the corroboration, the guardrails and the whole consensus loop.
   **Framework-free** (stdlib only at import time); you inject a `poster`.
   (`tailor_core.py` is the older single-call extract; see the note at the top of this document.)
2. A **poster**: `async poster(payload: dict, timeout: float) -> dict`, where `payload` is an
   OpenAI-compatible chat-completions body and the return is the parsed response. Raise something
   carrying `.status` and `.body` on an HTTP error, so the 400 repair can read what the server said.
   `backend/app/llm.py::chat()` is that transport here.
3. A **profile** (dict, markdown, or extracted CV text) and a **JD dict** `{title, company,
   location, text}`.
4. A **renderer** if you want files (python-docx etc.); Tailor's core is renderer-agnostic and will
   hand you `resume_struct` / `cover_struct` to render however you like.

## 8. Tests, and what the rebuild actually fixed

    python3 backend/tests/test_resume_consensus.py     # 176 checks, no network, no key, no pytest

It stubs the transport, so nothing reaches the internet, and the EXIT GATE IS THE LAST STATEMENT in
the file on purpose - a gate in the middle silently stops enforcing everything appended after it.
Every guardrail was negative-tested: 18 mutations, each reintroducing a real defect, each verified to
FAIL the suite and then restored.

Four defects were MEASURED during the rebuild and are now guarded:

1. **`highlights` and `earlier` never reached the renderer.** `documents.py` renders both, and
   `resume_struct` set neither. So the 3-5 CAR highlights that are supposed to LEAD the resume were
   requested, returned and thrown away - and `earlier` is the ONLY mechanism that keeps a demoted
   employer in the document, so older employers were silently absent from the DOCX while
   `missing_employers()` inspected the model's ANSWER and reported clean. The manifest said the
   employers were there; the file did not have them.
2. **The employer-drop check ran on the wrong object.** It now runs on the RENDERED struct, which is
   what became the DOCX.
3. **The revision guard protected an INVENTED employer** and blocked the correction that removed it
   (see section 2b, step 4).
4. **`/revise` could write an EMPTY document.** Any dict was accepted, so `{"resume": {}}`
   overwrote a good resume with nothing and re-rendered a blank DOCX. It now must pass the contract,
   and it may not silently lose an employer - though an explicit "remove the X role" IS
   authorisation, because the candidate asked.

The truth-gating, the recruiter rules, the gap→answer loop and the struct-based revise are the
transferable IP. The storage layout (`users/<hash>/<job_id>/`), the tenancy-by-email and the file
endpoints are specific to this app and can be replaced wholesale.
