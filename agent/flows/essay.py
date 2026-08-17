#!/usr/bin/env python3
"""FREE-TEXT APPLICATION QUESTIONS — answered FROM THE CANDIDATE'S OWN DOCUMENTS.

WHY THIS EXISTS, from the Alpega run of 2026-08-17. Eleven fields filled, both documents attached,
Location finally resolved to the right Friedberg -- and then:

    6 question(s) the ladder could not answer: ['How does this job aligns with your career path?',
      'Have you led an engineering organisation through a significant transfo', ...]
    REFUSING to submit: still required+empty -> ...

The operator, fairly: *"why did it exit with extra questions? it should go to my resume and cover
letter and fetch from them, or ask the 3 LLMs, or ask me."* Correct on all three counts. What actually
happened is worse than any of them: **the model was never called.**

    greenhouse called : answer_fn(lab)                        -> 1 argument
    agent.llm_answer  : llm_answer(question, options, profile) -> 3 arguments

so every call raised `TypeError`, and my `except Exception: v = ""` swallowed it. Nineteenth assumed
signature in this project, and the bare except is what made it silent. A caught exception that
produces no log line is indistinguishable from "there was nothing to do".

THREE THINGS THIS FILE DOES, in order:

1. **DETERMINISTIC FIRST.** A start date, a salary figure, a notice period and a travel answer are
   FACTS the candidate has written down in `candidate.md`. They must never reach a model. The Alpega
   form asked for a start date and a salary and both were sitting in `## screening_defaults` unread.

2. **GROUND AN ESSAY IN HIS OWN DOCUMENTS.** "Have you led an engineering organisation through a
   significant transformation?" is a question about HIS career, and the answer is in his CV: seven
   experience entries with employers, titles, dates and outcomes, plus the cover letter already
   tailored to this posting. The model is given those and the job description, and asked to answer
   AS HIM from that material -- not to invent a career.

3. **REFUSE TO INVENT.** `verify()` is pure and rejects an answer that names an employer or a year
   the profile does not contain, or that reads like a refusal ("as an AI", "I don't have access").
   This project's oldest rule is that no unverifiable identifier reaches a customer-facing document;
   a job application is exactly that. A rejected answer becomes a question for the human, which is
   strictly better than a plausible fabrication on a real application.
"""
from __future__ import annotations
import json
import os
import re
from datetime import date, timedelta

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL = os.getenv("JHW_ESSAY_MODEL", "jhw-content")
TIMEOUT = float(os.getenv("JHW_ESSAY_TIMEOUT", "60"))
OUT = os.getenv("JHW_OUT", "/agent/out")

# ─────────────────────────────────────────────────────────── deterministic answers
_START_RX = re.compile(r"available|start date|notice|when (can|could) you (start|begin)|earliest", re.I)
_SALARY_RX = re.compile(r"salary|compensation|remuneration|expected pay|rate", re.I)
_TRAVEL_RX = re.compile(r"travel", re.I)
_YEARS_RX = re.compile(r"how many years|years of experience", re.I)


def deterministic(question: str, defaults: dict, profile: dict | None = None) -> str:
    """A written-down fact, or ''. PURE. No model, no network.

    The Alpega form asked for a start date and a salary; both were in candidate.md and neither was
    read, because the free-text path only ever looked at four profile keys (website, linkedin, email,
    github). A fact the candidate has stated is not a question."""
    q = re.sub(r"\s+", " ", (question or "")).strip().lower()
    if not q:
        return ""

    def d(*keys):
        for k in keys:
            for dk, dv in (defaults or {}).items():
                if k in dk.lower() and str(dv).strip():
                    return str(dv).strip().strip('"')
        return ""

    if _START_RX.search(q):
        v = d("available_start_date", "start date", "available start")
        # A LITERAL DATE IN A FILE GOES STALE. candidate.md's own note says "ALWAYS one month from the
        # application date", so recompute it rather than sending a date that has already passed.
        if v and re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            try:
                if date.fromisoformat(v) <= date.today():
                    v = ""
            except ValueError:
                v = ""
        if not v:
            notice = d("notice_period") or "1 month"
            weeks = 4 if "1 month" in notice.lower() or "one month" in notice.lower() else 4
            v = (date.today() + timedelta(weeks=weeks)).isoformat()
        return v
    if _SALARY_RX.search(q):
        v = d("salary_expectation_eur", "salary expectation", "salary")
        if v:
            digits = re.sub(r"[^\d]", "", v)
            return f"EUR {int(digits):,} gross per annum".replace(",", ".") if digits else v
        return ""
    if _TRAVEL_RX.search(q):
        return d("willing_to_accept_travel_assignment", "travel")
    if _YEARS_RX.search(q) and profile:
        yrs = _years(profile)
        return str(yrs) if yrs else ""
    return ""


def _years(profile: dict) -> int:
    """Total years of experience, from the EARLIEST dated role in his own CV. Never guessed."""
    ys = []
    for e in (profile or {}).get("experience", []) or []:
        for k in ("start", "from", "start_date"):
            m = re.search(r"(19|20)\d{2}", str(e.get(k) or ""))
            if m:
                ys.append(int(m.group(0)))
                break
    return (date.today().year - min(ys)) if ys else 0


# ─────────────────────────────────────────────────────────── his own documents
def _s(x, n: int = 0) -> str:
    """Anything -> a string, optionally truncated. NEVER raises, NEVER slices a non-sequence.

    THE ALPEGA CRASH, and it killed the whole run: `context()` did
        (profile["expertise"] or [])[:18]
    and `expertise` in resume_data.json is a **dict** of categories, not a list. `dict[:18]` raises
    `TypeError: unhashable type: 'slice'` (surfacing as `KeyError: slice(None, 18, None)`), the
    exception left `drive()` entirely, and the choice fields, the panel and the Telegram ask were
    NEVER REACHED. The engine did not decide against asking — it crashed before the ask existed.
    Every field read out of a profile now goes through here, because a profile is user-edited JSON and
    its shapes are not ours to assume."""
    if x is None:
        return ""
    if isinstance(x, str):
        t = x
    elif isinstance(x, (int, float, bool)):
        t = str(x)
    elif isinstance(x, dict):
        # a dict of categories -> "Category: a, b, c; Category: ..." which is what a reader wants
        parts = []
        for k, v in x.items():
            vv = ", ".join(_s(i) for i in v) if isinstance(v, (list, tuple)) else _s(v)
            parts.append(f"{_s(k)}: {vv}" if vv else _s(k))
        t = "; ".join(p for p in parts if p)
    elif isinstance(x, (list, tuple, set)):
        t = ", ".join(_s(i) for i in x if _s(i))
    else:
        t = str(x)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n] if n and n > 0 else t


def _rows(x) -> list:
    """A field that should be a list of records, whatever shape it arrived in. Never raises."""
    if isinstance(x, list):
        return [r for r in x if isinstance(r, dict)]
    if isinstance(x, dict):
        return [v for v in x.values() if isinstance(v, dict)] or [x]
    return []


def context(profile: dict, job: dict | None = None, cover: str = "", limit: int = 5200) -> str:
    """The material an answer must come FROM: the job, his roles, his summary, his cover letter."""
    profile = profile if isinstance(profile, dict) else {}
    job = job if isinstance(job, dict) else {}
    b = profile.get("basics") if isinstance(profile.get("basics"), dict) else {}
    out = []
    if job:
        out.append("THE ROLE HE IS APPLYING FOR:")
        out.append(f"  {_s(job.get('title'))} at {_s(job.get('company'))} "
                   f"({_s(job.get('location'))})")
        desc = _s(job.get("description"), 1400)
        if desc:
            out.append(f"  {desc}")
    summ = _s(profile.get("summary"), 900)
    if summ:
        out.append("\nHIS PROFESSIONAL SUMMARY (his own words):")
        out.append("  " + summ)
    exp = _rows(profile.get("experience"))
    if exp:
        out.append("\nHIS ACTUAL CAREER HISTORY — do not add to it, do not alter a date:")
        for e in exp[:7]:
            out.append(f"  - {_s(e.get('title'))} at {_s(e.get('company'))}, "
                       f"{_s(e.get('location'))}, {_s(e.get('start'))} to {_s(e.get('end'))}")
            es = _s(e.get("summary") or e.get("highlights"), 340)
            if es:
                out.append(f"      {es}")
    for key, label in (("expertise", "HIS EXPERTISE"), ("skills_flat", "HIS SKILLS"),
                       ("certifications", "HIS CERTIFICATIONS")):
        v = _s(profile.get(key), 700)
        if v:
            out.append(f"\n{label}: {v}")
    edu = _rows(profile.get("education"))
    if edu:
        out.append("\nHIS EDUCATION: " + "; ".join(
            f"{_s(e.get('credential') or e.get('degree'))} — {_s(e.get('org') or e.get('school'))}"
            for e in edu[:4]))
    cov = _s(cover, 1200)
    if cov:
        out.append("\nTHE COVER LETTER ALREADY TAILORED TO THIS ROLE (reuse its substance):")
        out.append("  " + cov)
    langs = _s(b.get("languages"), 200) if not isinstance(b.get("languages"), list) else ", ".join(
        _s(x.get("name") if isinstance(x, dict) else x) for x in (b.get("languages") or []))
    if langs:
        out.append("\nLANGUAGES: " + langs)
    return "\n".join(out)[:limit]


def load_job() -> dict:
    for p in (os.path.join(OUT, "job.json"), "/agent/out/job.json"):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            continue
    return {}


def load_cover() -> str:
    for p in ("cover_letter.html", "cover_letter.txt", "cover_letter.md"):
        try:
            with open(os.path.join(OUT, p), encoding="utf-8") as fh:
                t = fh.read()
            t = re.sub(r"<[^>]+>", " ", t)
            return re.sub(r"\s+", " ", t).strip()
        except Exception:
            continue
    return ""


# ─────────────────────────────────────────────────────────── the guard
_REFUSAL = re.compile(r"\bas an ai\b|language model|i (do not|don't) have access|cannot provide|"
                      r"i'?m unable to|insufficient information|\bN/?A\b$|^unknown$", re.I)
_COMPANYISH = re.compile(r"\b(?:at|with|for|joined)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})")


def verify(answer: str, profile: dict, question: str = "") -> tuple:
    """(ok, why). PURE. Rejects a fabrication, a refusal, or an answer that is not an answer.

    NO UNVERIFIABLE IDENTIFIER MAY REACH AN APPLICATION. An invented employer or a shifted date on a
    real job application is a misrepresentation, and it is exactly the class this project already
    guards against in customer documents. A rejected answer becomes a question for the human, which is
    strictly better than a plausible fabrication."""
    a = (answer or "").strip()
    if len(a) < 15:
        return False, f"too short to be an answer ({len(a)} chars)"
    if _REFUSAL.search(a):
        return False, "the model refused or hedged instead of answering"
    if re.search(r"\[|\]|\{|\}|<[a-z_]+>|TODO|XXX|lorem ipsum", a, re.I):
        return False, "it contains a placeholder"
    # SAME HARDENING HERE. This built its "known text" by iterating `expertise` as a sequence too, so
    # a dict would have crashed the GUARD as well -- and a guard that raises is a guard that lets
    # everything through once someone wraps it in a try/except.
    profile = profile if isinstance(profile, dict) else {}
    known = " ".join([
        _s(profile.get("summary")),
        " ".join(f"{_s(e.get('company'))} {_s(e.get('title'))} {_s(e.get('location'))} "
                 f"{_s(e.get('summary'))}" for e in _rows(profile.get("experience"))),
        _s(profile.get("expertise")), _s(profile.get("skills_flat")),
        _s(profile.get("certifications")), _s(profile.get("awards")),
        _s(profile.get("education")), _s(profile.get("basics")),
    ]).lower()
    for m in _COMPANYISH.finditer(a):
        # STRIP TRAILING PUNCTUATION. The capture took "Colt Technology Services." -- with the
        # sentence's full stop -- which is not in the CV text, so a REAL employer was refused. A guard
        # that rejects the truth is worse than one that is slightly loose: it turns every good answer
        # into a question for the human.
        name = m.group(1).strip().strip(".,;:!?)('\"")
        # a generic phrase after "at/with/for" is not an employer claim
        if len(name.split()) > 3 or name.lower() in ("the", "a", "an", "my", "this", "that"):
            continue
        if name.lower() not in known and name.lower() not in (question or "").lower():
            return False, f"names an employer the CV does not contain: {name!r}"
    for y in set(re.findall(r"\b(?:19|20)\d{2}\b", a)):
        if y not in known and y != str(date.today().year):
            return False, f"cites a year the CV does not contain: {y}"
    return True, ""


_SYS = (
    "You are helping a candidate answer ONE question on a real job application. You write AS HIM, in "
    "first person, in plain professional English.\n"
    "ABSOLUTE RULES:\n"
    "1. Use ONLY the career history, summary, expertise and cover letter you are given. Do NOT invent "
    "an employer, a job title, a date, a team size, a percentage or any other number.\n"
    "2. If the material genuinely does not answer the question, reply with exactly: CANNOT ANSWER\n"
    "3. No preamble, no sign-off, no bullet points unless the question asks for a list. Just the "
    "answer.\n"
    "4. Match the length to the question: a one-line question gets one or two sentences; an essay "
    "question gets 3 to 6 sentences.\n"
    "5. Never mention that you are an AI, and never describe your own limitations."
)


def _target_len(question: str) -> str:
    q = (question or "").lower()
    if re.search(r"describe|explain|why|how (do|does|would|did)|tell us|instance|example|"
                 r"experience with|walk us", q):
        return "3 to 6 sentences"
    if re.search(r"\?$", q.strip()) and len(q) < 90:
        return "1 to 2 sentences"
    return "2 to 4 sentences"


async def write(question: str, profile: dict, *, defaults: dict | None = None, job: dict | None = None,
                cover: str = "", log=print) -> dict:
    """One free-text answer. Returns {value, via, why}. `value` is '' when nothing may be said."""
    # NEVER RAISE OUT OF HERE. The caller's whole run died because an exception from this function
    # left `drive()` — so the choice fields, the panel and the human ask were never reached. A helper
    # that can abort the run it is helping is a defect regardless of what caused the exception.
    try:
        return await _write(question, profile, defaults=defaults, job=job, cover=cover, log=log)
    except Exception as e:
        log(f"    [essay] internal error ({type(e).__name__}: {str(e)[:80]}) — this question goes to "
            f"you instead; the run continues")
        return {"value": "", "via": "error", "why": f"{type(e).__name__}: {e}"}


async def _write(question: str, profile: dict, *, defaults: dict | None = None,
                 job: dict | None = None, cover: str = "", log=print) -> dict:
    det = deterministic(question, defaults or {}, profile)
    if det:
        log(f"    [essay] {question[:44]!r} <- WRITTEN FACT: {det[:40]!r}")
        return {"value": det, "via": "fact", "why": "from candidate.md"}
    ctx = context(profile, job, cover)
    if len(ctx) < 120:
        return {"value": "", "via": "none", "why": "no profile material to answer from"}
    body = {"model": MODEL, "temperature": 0.2, "max_tokens": 700,
            "messages": [{"role": "system", "content": _SYS},
                         {"role": "user", "content":
                          f"{ctx}\n\nQUESTION ON THE APPLICATION FORM:\n{question.strip()}\n\n"
                          f"Answer as him, in {_target_len(question)}, using only the material above."}]}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(f"{PROXY}/chat/completions",
                             headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        # NEVER SILENT. A swallowed exception here is what made the Alpega run look like "there was
        # nothing to do" when in fact the call had failed with a TypeError.
        log(f"    [essay] the model could not be reached ({type(e).__name__}: {str(e)[:70]})")
        return {"value": "", "via": "error", "why": type(e).__name__}
    txt = re.sub(r"^```.*?\n|```$", "", txt).strip().strip('"')
    if txt.upper().startswith("CANNOT ANSWER"):
        log(f"    [essay] {question[:44]!r}: the model says his CV does not answer it — asking you")
        return {"value": "", "via": "cannot", "why": "not derivable from his documents"}
    ok, why = verify(txt, profile, question)
    if not ok:
        log(f"    [essay] REJECTED an answer for {question[:38]!r}: {why}")
        return {"value": "", "via": "rejected", "why": why}
    log(f"    [essay] {question[:44]!r} <- {len(txt)} chars from his own CV/cover letter")
    return {"value": txt, "via": "documents", "why": ""}


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    PROF = {"summary": "Cyber security client partner and platform builder.",
            "experience": [
                {"company": "Colt Technology Services", "title": "Cyber Security Client Partner",
                 "location": "Frankfurt", "start": "Feb 2026", "end": "Present",
                 "summary": "Built cybergod.ai; grew meetings 300% per rep."},
                {"company": "Orange Business", "title": "Senior Project Manager",
                 "start": "2015", "end": "2019", "summary": "Delivered WAN programmes."}],
            "expertise": ["pre-sales", "cyber security", "AI platforms"],
            "basics": {"languages": [{"name": "English"}, {"name": "German"}]}}
    SD = {"salary_expectation_eur": "150000", "notice_period": "1 month",
          "available_start_date": "2020-01-01",          # deliberately STALE
          "willing_to_accept_travel_assignment": "Yes"}

    print("\n[1] a WRITTEN FACT never reaches a model — the two Alpega fields")
    v = deterministic("What are your salary expectations? (base salary+variable)", SD, PROF)
    ck("150.000" in v and "EUR" in v, f"salary from candidate.md: {v!r}")
    v = deterministic("What is your available start date?", SD, PROF)
    ck(re.match(r"^\d{4}-\d{2}-\d{2}$", v) and date.fromisoformat(v) > date.today(),
       f"start date recomputed because the file's date is in the past: {v!r}")
    v = deterministic("This role requires frequent travel to various company locations. Are you "
                      "willing?", SD, PROF)
    ck(v == "Yes", f"travel from candidate.md: {v!r}")
    ck(deterministic("How many years of experience do you have?", SD, PROF) == str(
        date.today().year - 2015), "years counted from the EARLIEST dated role in his own CV")
    ck(deterministic("How does this job align with your career path?", SD, PROF) == "",
       "a genuine essay question gets NO deterministic answer")

    print("\n[2] a stale literal date is not sent")
    ck(deterministic("Start date?", {"available_start_date": "2020-01-01",
                                     "notice_period": "1 month"}) >
       date.today().isoformat(), "a past date is replaced with today + notice")
    fut = (date.today() + timedelta(days=45)).isoformat()
    ck(deterministic("Start date?", {"available_start_date": fut}) == fut,
       "a FUTURE date the candidate wrote is respected verbatim")

    print("\n[3] the context is HIS documents, and nothing else")
    ctx = context(PROF, {"title": "VP Engineering", "company": "Alpega",
                         "description": "Lead a large org through transformation."},
                  cover="I have led cross-functional teams at Colt.")
    for must in ("Colt Technology Services", "Orange Business", "Feb 2026", "Alpega",
                 "cover letter"):
        ck(must.lower() in ctx.lower(), f"context carries {must!r}")
    ck("do not add to it" in ctx.lower(), "and it tells the model not to invent history")

    print("\n[4] verify() REFUSES a fabrication — the property that matters most")
    ok, why = verify("I led the transformation at Colt Technology Services, growing pipeline "
                     "substantially across the DACH region over several years.", PROF)
    ck(ok, f"a grounded answer is accepted ({why})")
    ok, why = verify("I spent four years at Deutsche Telekom leading a 200-person org through "
                     "a full cloud migration programme.", PROF)
    ck(not ok and "Deutsche" in why, f"an INVENTED employer is refused: {why}")
    ok, why = verify("Between 2003 and 2007 I ran the European delivery organisation and "
                     "rebuilt it end to end.", PROF)
    # EITHER invented year is a correct catch -- `set()` iteration decides which is reported first, so
    # asserting one specific year makes the test depend on hash order rather than on the property.
    ck(not ok and ("2003" in why or "2007" in why), f"an invented YEAR is refused: {why}")
    ok, why = verify("As an AI language model I do not have access to your CV.", PROF)
    ck(not ok, f"a refusal is not an answer: {why}")
    ok, why = verify("Yes.", PROF)
    ck(not ok, f"too short: {why}")
    ok, why = verify("I have delivered [PROJECT NAME] for several clients in the DACH region "
                     "and would bring the same approach here.", PROF)
    ck(not ok and "placeholder" in why, f"a placeholder is refused: {why}")
    ok, _ = verify("I have worked with cyber security and AI platforms for most of my career, "
                   "most recently at Colt Technology Services in Frankfurt.", PROF)
    ck(ok, "an answer naming only real employers is accepted")

    print("\n[5] the question's own words are not treated as an invented employer")
    ok, why = verify("I would relish the chance to do that at Alpega, building on the "
                     "transformation work I led at Colt Technology Services.", PROF,
                     question="Why do you want to work at Alpega?")
    ck(ok, f"the EMPLOYER BEING APPLIED TO is allowed ({why})")

    print("\n[6] length follows the question")
    ck("3 to 6" in _target_len("Describe a specific instance where you led a transformation."),
       "an essay question asks for 3-6 sentences")
    ck("1 to 2" in _target_len("Do you have a work permit?"), "a short question gets 1-2")

    print("\n[7] no credential, no browser, no page access in this module")
    src = open(__file__, encoding="utf-8").read()
    ship = src[:src.index("def _selftest(")]
    for banned in ("page.", "click(", "goto(", "data-jhw", "GOOGLE_PASSWORD", "input_value"):
        ck(banned not in ship, f"no {banned}")
    ck(len(ship) > 4000, f"the measured slice is the real module ({len(ship)} chars)")

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} ESSAY CONTRACT(S) BROKEN")
        return 1
    print("ALL ESSAY CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
