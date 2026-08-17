"""tailor_core.py — the reusable core of the Tailor feature, framework-free.

Job description IN -> tailored resume + cover-letter STRUCTS out, truth-gated.

This is a distilled, dependency-free lift of backend/app/electronic.py. It contains the
transferable IP — truth-gating, the recruiter/headhunter rules, JSON tolerance, the
employer-drop guard, and the generate + natural-language-revise logic — with NO web framework,
NO storage layout and NO vendor lock-in. See TAILOR_LOGIC.md for the full model.

YOU INJECT:
  * an async LLM callable:
        async def llm_complete(messages, *, temperature, max_tokens, json_mode) -> str
    (messages = [{"role": "...", "content": "..."}]; must return the model's text)

USAGE:
    core = TailorCore(llm_complete)
    result = await core.generate(profile=<dict|markdown|CV text>,
                                 jd={"title": .., "company": .., "location": .., "text": ..},
                                 answers={gap_question: candidate_answer},   # optional
                                 evidence=[{"name": .., "text": ..}],         # optional
                                 links_text={url: fetched_text})              # optional
    # result.resume / result.cover  -> dicts you render however you like
    # result.warnings / result.gaps / result.employers_missing / result.errors -> surface these

    revised = await core.revise(result.resume, result.cover, "add Canonical back; make it shorter")

No file I/O, no network. Rendering (DOCX/PDF) and LLM transport are the caller's job.
Python 3.9+. Standard library only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

LLMComplete = Callable[..., Awaitable[str]]


# --------------------------------------------------------------------------- prompts
_TRUTH = (
    "HARD RULES - these override everything else:\n"
    "1. NEVER invent, embellish or infer a fact about the candidate. Employers, job titles, dates, "
    "degrees, certifications, tools, team sizes, revenue and percentages may ONLY appear if they are "
    "present in the CANDIDATE PROFILE below. If the profile has no number for a claim, write the "
    "bullet WITHOUT a number rather than inventing one.\n"
    "2. You may re-angle, reorder, compress and rephrase what IS there so it speaks to this job.\n"
    "3. Mirror the job description's real vocabulary ONLY where it is truthfully the same thing the "
    "candidate has done. Do not adopt a JD keyword the candidate cannot back up.\n"
    "4. ATS-safe output: plain text, no tables, no columns, no graphics, no emoji, no markdown "
    "syntax inside field values.\n"
    "5. Every bullet must carry a FACT, a NUMBER or an OUTCOME. No filler, no adjectives-only lines.\n"
    "6. Output ONLY the requested JSON object. No prose before or after."
)

_RESUME_SYS = ("You tailor an existing resume to a specific job. You are a truth-gated editor, not a "
               "copywriter with imagination.\n" + _TRUTH)

_COVER_SYS = ("You write a concise, senior cover letter that a hiring manager will actually finish "
              "reading. Specific, no cliches, no fabrication.\n" + _TRUTH)

RESUME_RULES = (
    "RULES (follow ALL - these come from mainstream recruiter and executive-search guidance):\n"
    "1. LENGTH: 2 pages maximum for 10+ years, 1 page under 10 years. Be ruthless: the last 10-15\n"
    "   years carry detail, older roles collapse to one line each.\n"
    "2. ORDER: highlights, then summary, then experience (reverse-chronological), then skills,\n"
    "   then education. A recruiter's first screen is ~7 seconds - the top third must sell.\n"
    "3. ACHIEVEMENTS, NOT DUTIES: every bullet is an accomplishment with evidence, never a job\n"
    "   description. Start with a strong past-tense verb (Led, Built, Won, Cut, Scaled). Never use\n"
    "   'Responsible for'.\n"
    "4. QUANTIFY: at least half the bullets carry a number - EUR, %, headcount, time saved, deal\n"
    "   size. If the profile has no number for a point, keep the point but do NOT invent one.\n"
    "5. BULLETS: 3-5 per recent role, max 2 lines each, no paragraphs.\n"
    "6. ATS: mirror the JD's exact terminology where the candidate genuinely has it. No tables,\n"
    "   columns, headers/footers, graphics or text boxes - they break ATS parsers.\n"
    "7. NO PRONOUNS, no 'References available on request'.\n"
    "8. TRUTH: every employer, title and date exactly as in the profile, in profile order. Never\n"
    "   merge, drop, re-date or promote a role. Re-angle emphasis only.\n"
    "9. NEVER DELETE AN EMPLOYER TO FIT THE PAGE LIMIT. Detailed bullets are for the recent and\n"
    "   most relevant roles; EVERY remaining employer still appears in \"earlier\" as a single\n"
    "   line (title, company, dates). A missing employer reads as an unexplained gap and gets\n"
    "   the candidate screened out - it is the worst possible failure of this document.\n"
)

COVER_RULES = (
    "RULES (mainstream recruiter guidance for cover letters):\n"
    "1. LENGTH: 250-350 words, ONE page, 3-4 short paragraphs. Never longer.\n"
    "2. OPENING: name the role and lead with the single most relevant proof point.\n"
    "3. MIDDLE: 2 paragraphs mapping the employer's top requirements to concrete, quantified\n"
    "   evidence from the profile. Show you understand THEIR problem, not your career story.\n"
    "4. CLOSE: brief, confident, a clear next step. No pleading, no salary, no apologies.\n"
    "5. NEVER repeat the resume verbatim, never invent, no generic filler.\n"
)


def _resume_user(profile_txt: str, jd: dict) -> str:
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n%s\n\n"
        "TARGET JOB\ntitle: %s\ncompany: %s\nlocation: %s\ndescription:\n%s\n\n"
        "Return JSON with EXACTLY this shape:\n"
        "{\n"
        '  "summary": "3-4 sentence professional summary aimed at THIS job, true to the profile",\n'
        '  "skills": ["12-20 skills taken from the profile, ordered by relevance to the JD"],\n'
        '  "experience": [\n'
        '    {"title":"<exact title>", "company":"<exact company>", "dates":"<exact dates or empty>",\n'
        '     "location":"<from profile or empty>",\n'
        '     "bullets":["3-5 bullets re-angled to the JD. Same facts. Each carries a fact/number/outcome."]}\n'
        "  ],\n"
        '  "earlier": [ {"title":"<exact>", "company":"<exact>", "dates":"<exact>"} ],\n'
        '  "all_employers": ["EVERY employer name in the profile, in profile order"],\n'
        '  "keywords_matched": ["JD terms the candidate genuinely covers"],\n'
        '  "highlights": ["EXACTLY 3-5 career achievements, strongest first, CAR form with a number"],\n'
        '  "gaps": ["JD requirements the profile does NOT evidence - honest, for the candidate only"]\n'
        "}\n"
        % (profile_txt[:14000], jd.get("title", ""), jd.get("company", ""),
           jd.get("location", ""), (jd.get("text") or "")[:9000])) + RESUME_RULES


def _cover_user(profile_txt: str, jd: dict) -> str:
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n%s\n\n"
        "TARGET JOB\ntitle: %s\ncompany: %s\nlocation: %s\ndescription:\n%s\n\n"
        "Return JSON with EXACTLY this shape:\n"
        "{\n"
        '  "salutation": "Hiring Team, or a named person if the JD gives one",\n'
        '  "paragraphs": ["Opening hook", "Proof mapped to top requirement", "Fit / first months", "Close"],\n'
        '  "closing": "Sincerely,"\n'
        "}\n"
        "Max ~320 words total. No sentence may contain a fact absent from the profile."
        % (profile_txt[:9000], jd.get("title", ""), jd.get("company", ""),
           jd.get("location", ""), (jd.get("text") or "")[:6000])) + COVER_RULES


# --------------------------------------------------------------------------- helpers
def parse_json_loose(txt: Any) -> dict:
    """Tolerate every shape a model actually emits: dict, fenced, prefixed prose, [ {...} ].

    A GOOD answer thrown away by a strict parser costs the whole call. Tolerate any SHAPE;
    reject only an EMPTY answer (the caller treats {} as 'no answer').
    """
    if isinstance(txt, dict):
        return txt
    s = (txt or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s)
        if isinstance(d, list):
            d = next((x for x in d if isinstance(x, dict)), {})
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def profile_text(profile: Any) -> str:
    if isinstance(profile, str):
        return profile
    try:
        return json.dumps(profile, ensure_ascii=False, indent=1)
    except Exception:
        return str(profile)


def _pget(profile: Any, key: str, default):
    return profile.get(key, default) if isinstance(profile, dict) else default


_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_PHONE = re.compile(r"(?:\+\d{1,3}[\s./-]?)?(?:\(?\d{2,4}\)?[\s./-]?){2,5}\d{2,4}")
_RE_LI = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_RE_SITE = re.compile(r"(?:https?://)?(?:www\.)?[\w-]+\.[a-z]{2,}(?:/[\w%./-]*)?", re.I)
_RE_LABEL = re.compile(r"^\s*-?\s*([a-z_]+)\s*:\s*(.+?)\s*$", re.I)


def contacts_from_text(text: str) -> dict:
    """Recover the contact block from a REAL CV (PDF/DOCX text has no 'email:' labels).

    Looks only at the first ~12 non-empty lines - the header. Returns any of:
    name, headline, email, phone, linkedin, website, location.
    """
    out: dict = {}
    head = [l.strip() for l in (text or "").splitlines() if l.strip()][:12]
    blob = "\n".join(head)
    m = _RE_EMAIL.search(blob)
    if m:
        out["email"] = m.group(0)
    m = _RE_LI.search(blob)
    if m:
        out["linkedin"] = m.group(0).rstrip("/")
    for line in head:
        cand = line
        for k in ("email", "linkedin"):
            if out.get(k):
                cand = cand.replace(out[k], " ")
        m = _RE_PHONE.search(cand)
        if m and len(re.sub(r"\D", "", m.group(0))) >= 9:
            out.setdefault("phone", m.group(0).strip(" |,;"))
    for line in head:
        for w in _RE_SITE.findall(line):
            lw = w.lower()
            if ("linkedin.com" in lw or "@" in w or lw.endswith((".jpg", ".png"))
                    or (out.get("email") and w in out["email"])):
                continue
            if "." in w and " " not in w and len(w) > 4:
                out.setdefault("website", w.rstrip("/.,;|"))
                break
        if out.get("website"):
            break
    for line in head:
        if any(t in line for t in ("@", "http", "linkedin.com")) or _RE_PHONE.search(line):
            continue
        words = line.replace(",", " ").split()
        if 1 < len(words) <= 5 and sum(w[:1].isupper() for w in words) >= 2 and len(line) < 60:
            out.setdefault("name", line.strip())
            break
    if out.get("name") and out["name"] in head:
        i = head.index(out["name"])
        if i + 1 < len(head):
            nxt = head[i + 1]
            if "@" not in nxt and not _RE_PHONE.search(nxt) and 3 < len(nxt) < 140:
                out.setdefault("headline", nxt.strip())
    return out


def basics(profile: Any) -> dict:
    """Contact block from a dict profile, or from markdown 'key: value' lines, or a raw CV."""
    keys = ("name", "full_name", "headline", "title", "email", "phone", "location", "city",
            "country", "linkedin", "website", "portfolio")
    out: dict = {}
    if isinstance(profile, dict):
        src = profile.get("basics") if isinstance(profile.get("basics"), dict) else profile
        for k in keys:
            v = src.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                out[k] = str(v).strip()
    else:
        txt = str(profile or "")
        for line in txt.split("\n"):
            m = _RE_LABEL.match(line)
            if m and m.group(1).lower() in keys:
                out.setdefault(m.group(1).lower(), m.group(2).strip().strip('"'))
        for k, v in contacts_from_text(txt).items():
            out.setdefault(k, v)
    if "name" not in out and "full_name" in out:
        out["name"] = out["full_name"]
    if "headline" not in out and "title" in out:
        out["headline"] = out["title"]
    if "location" not in out:
        loc = " ".join(x for x in (out.get("city"), out.get("country")) if x)
        if loc:
            out["location"] = loc
    return out


def _norm(x: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(x or "").lower())


def missing_employers(tailored: dict) -> list:
    """Which employers did the model SEE (all_employers) but leave out of the rendered doc?

    Containment, not equality: 'Stars4Business' vs 'Stars4Business OU' is the same employer.
    """
    seen = [str(x).strip() for x in (tailored.get("all_employers") or []) if str(x).strip()]
    if not seen:
        return []
    rendered = set()
    for key in ("experience", "earlier"):
        for e in (tailored.get(key) or []):
            if isinstance(e, dict):
                rendered.add(_norm(e.get("company") or e.get("employer")))
    out = []
    for c in seen:
        n = _norm(c)
        if n and not any(n in r or r in n for r in rendered if r):
            out.append(c)
    return out


def truth_check(tailored: dict, profile: Any) -> list:
    """Flag any organisation in the resume that the profile text does not contain.

    A WARNING for the human, never a silent correction, never proof of fabrication.
    """
    hay = re.sub(r"\s+", " ", profile_text(profile).lower())
    warns, seen = [], set()
    for e in (tailored.get("experience") or []):
        if not isinstance(e, dict):
            continue
        v = str(e.get("company") or "").strip()
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        probe = re.sub(r"[^a-z0-9 ]+", " ", v.lower())
        probe = re.sub(r"\b(gmbh|ag|inc|ltd|llc|ou|se|bv|plc|kg|co)\b", " ", probe).strip()
        head = probe.split(" ")[0] if probe else ""
        if head and len(head) > 2 and head not in hay:
            warns.append("Employer '%s' is not present in the supplied profile - verify it before "
                         "sending." % v)
    return warns


# --------------------------------------------------------------------------- result
@dataclass
class TailorResult:
    resume: dict
    cover: dict
    keywords_matched: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    employers_missing: list = field(default_factory=list)
    models: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- core
class TailorCore:
    """Framework-free Tailor. Inject an async LLM callable; get structs + honesty metadata back."""

    def __init__(self, llm_complete: LLMComplete):
        self._llm = llm_complete

    async def _ask(self, system: str, user: str, max_tokens: int) -> dict:
        """One LLM call. Returns the parsed dict, or {'_error': ...} - never raises."""
        try:
            out = await self._llm(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.3, max_tokens=max_tokens, json_mode=True)
            d = parse_json_loose(out)
            if d:
                return d
            return {"_error": "empty/unparseable answer"}
        except Exception as e:  # noqa: BLE001 - transport failures become data, not exceptions
            return {"_error": "%s: %s" % (type(e).__name__, e)}

    @staticmethod
    def build_profile_text(profile: Any, answers: Any = None, evidence: Any = None,
                           links_text: Any = None) -> str:
        """Assemble the single permitted source of facts.

        Gap ANSWERS are facts the candidate CONFIRMED (true, but not on the CV). Appending them
        here is the only sanctioned way new facts enter - and they stay inside the truth boundary
        because the candidate asserted them. Evidence/links are capped so one long PDF cannot
        crowd out the JD.
        """
        ptxt = profile_text(profile)
        answered = []
        if answers:
            items = answers.items() if isinstance(answers, dict) else [
                (a.get("q", ""), a.get("a", "")) for a in answers if isinstance(a, dict)]
            for q, a in items:
                if str(a or "").strip():
                    answered.append("- %s -> %s" % (str(q).strip(), str(a).strip()))
        if answered:
            ptxt += ("\n\nADDITIONAL FACTS CONFIRMED BY THE CANDIDATE (treat as true, use where "
                     "relevant to the target job):\n" + "\n".join(answered))
        for e in (evidence or []):
            if isinstance(e, dict) and str(e.get("text") or "").strip():
                ptxt += ("\n\nSUPPORTING DOCUMENT PROVIDED BY THE CANDIDATE (%s):\n%s"
                         % (str(e.get("name") or "attachment")[:120], str(e["text"])[:6000]))
        if isinstance(links_text, dict):
            for url, text in links_text.items():
                if str(text or "").strip():
                    ptxt += "\n\nLINK PROVIDED BY THE CANDIDATE (%s):\n%s" % (url, str(text)[:6000])
        return ptxt

    async def generate(self, profile: Any, jd: dict, answers: Any = None,
                       evidence: Any = None, links_text: Any = None) -> TailorResult:
        """JD + profile -> tailored resume & cover STRUCTS, plus honesty metadata.

        Raises ValueError only on missing INPUT (no profile / empty JD). A model failure is
        reported in .errors, never raised - one document can succeed while the other fails.
        """
        if not str((jd or {}).get("text") or "").strip():
            raise ValueError("jd.text is empty - supply the job description")
        if not profile:
            raise ValueError("profile is required - we never invent candidate facts")

        ptxt = self.build_profile_text(profile, answers, evidence, links_text)
        if len(ptxt.strip()) < 60:
            raise ValueError("profile is too thin to tailor from")

        import asyncio
        tailored, cover = await asyncio.gather(
            self._ask(_RESUME_SYS, _resume_user(ptxt, jd), 6000),
            self._ask(_COVER_SYS, _cover_user(ptxt, jd), 2000),
        )
        errors = {}
        if tailored.get("_error"):
            errors["resume"] = tailored["_error"]
        if cover.get("_error"):
            errors["cover"] = cover["_error"]

        b = basics(profile)
        resume_struct = {
            "basics": b,
            "summary": tailored.get("summary") or _pget(profile, "summary", ""),
            "skills": tailored.get("skills") or _pget(profile, "skills", []),
            "highlights": tailored.get("highlights") or [],
            "experience": tailored.get("experience") or _pget(profile, "experience", []),
            "earlier": tailored.get("earlier") or [],
            "education": _pget(profile, "education", []),
            "certifications": _pget(profile, "certifications", []),
            "languages": _pget(profile, "languages", []),
        }
        cover_struct = {
            "basics": b,
            "company": jd.get("company", ""),
            "job_title": jd.get("title", ""),
            "salutation": cover.get("salutation") or "Hiring Team",
            "paragraphs": cover.get("paragraphs") or [],
            "closing": cover.get("closing") or "Sincerely,",
        }
        miss = missing_employers(tailored)
        return TailorResult(
            resume=resume_struct, cover=cover_struct,
            keywords_matched=tailored.get("keywords_matched") or [],
            gaps=tailored.get("gaps") or [],
            warnings=truth_check(resume_struct, profile)
            + ["Employer missing from the resume: %s" % m for m in miss],
            employers_missing=miss,
            models={"resume": tailored.get("_model_role"), "cover": cover.get("_model_role")},
            errors=errors,
        )

    async def revise(self, resume_struct: dict, cover_struct: dict, instruction: str) -> TailorResult:
        """Iterate on already-built structs in natural language.

        Edits the STRUCT, does not regenerate from the JD - so the tailoring survives and the
        change is cheap. Truth rules still apply: rearrange / trim / reword only.
        """
        instruction = (instruction or "").strip()
        if not instruction:
            raise ValueError("say what you want changed")
        cur = {"resume": resume_struct, "cover": cover_struct}
        system = ("You edit an existing resume/cover-letter JSON. Apply ONLY the requested change. "
                  "Never invent facts, never delete an employer, keep every key that exists. "
                  "Return the SAME JSON shape, nothing else.")
        user = ("CURRENT DOCUMENT JSON:\n%s\n\nREQUESTED CHANGE:\n%s\n\n"
                "Return the complete updated JSON with keys 'resume' and 'cover'."
                % (json.dumps(cur)[:24000], instruction[:2000]))
        got = await self._ask(system, user, 6500)
        if got.get("_error"):
            return TailorResult(resume=resume_struct, cover=cover_struct,
                                errors={"revise": got["_error"]})
        new_resume = got.get("resume") if isinstance(got.get("resume"), dict) else resume_struct
        new_cover = got.get("cover") if isinstance(got.get("cover"), dict) else cover_struct
        miss = missing_employers(new_resume)
        return TailorResult(
            resume=new_resume, cover=new_cover,
            employers_missing=miss,
            warnings=["Employer missing from the resume: %s" % m for m in miss],
        )


if __name__ == "__main__":
    import asyncio

    async def _fake_llm(messages, *, temperature, max_tokens, json_mode):
        # A stub that echoes the shape, to prove the plumbing without a real model.
        if "cover letter" in messages[0]["content"].lower():
            return json.dumps({"salutation": "Hiring Team",
                               "paragraphs": ["Opening.", "Proof.", "Fit.", "Close."],
                               "closing": "Sincerely,"})
        return json.dumps({
            "summary": "Senior infra engineer.",
            "skills": ["BGP", "Python", "Docker"],
            "experience": [{"title": "Network Architect", "company": "Colt", "dates": "2019-2024",
                            "bullets": ["Cut peering costs 18%."]}],
            "earlier": [{"title": "NOC Engineer", "company": "Verint", "dates": "2015-2019"}],
            "all_employers": ["Colt", "Verint", "Canonical"],   # Canonical is intentionally dropped
            "keywords_matched": ["BGP"], "highlights": ["Cut costs 18%."],
            "gaps": ["No Kubernetes cert evidenced."]})

    async def _demo():
        core = TailorCore(_fake_llm)
        res = await core.generate(
            profile="Evgeny\nengineer@example.com\n+49 157 8554 1545\nWorked at Colt, Verint, Canonical.",
            jd={"title": "Principal SA", "company": "NTT", "location": "DACH",
                "text": "BGP, Python, automation."})
        print("basics parsed :", res.resume["basics"])
        print("gaps          :", res.gaps)
        print("missing empls :", res.employers_missing, "(Canonical should be flagged)")
        print("warnings      :", res.warnings)
        assert "Canonical" in res.employers_missing, "employer-drop guard failed"
        print("OK - core plumbing verified")

    asyncio.run(_demo())
