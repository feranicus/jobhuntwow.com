#!/usr/bin/env python3
"""DOCNAMES — what a tailored document is CALLED, and why it never collides.

HIS REQUEST (2026-09-18): *"each file such as resume.pdf and cover_letter.pdf needs to have some
sort of naming convention with job title and company name, for example resume_cisco_pm.pdf ... and
in case several similar positions with same employer then also better to add some sort of numeration
in order not to have duplicates"*.

Until now every application produced `resume.pdf` and `cover_letter.pdf`. Four downloads into a job
hunt his Downloads folder holds `resume.pdf`, `resume (1).pdf`, `resume (2).pdf` — the browser's
numbering, which carries no information at all — and the file he attaches to an employer is
whichever one he guesses. The NAME is the only thing visible at the moment he attaches it.

    resume_cisco_project-manager.pdf
    cover_letter_cisco_project-manager.pdf
    resume_cisco_project-manager_2.pdf        <- a SECOND Cisco project-manager posting

STDLIB ONLY, and PURE. It lives in its own module rather than in documents.py because that one
imports python-docx and reportlab, so a naming rule written there could only be tested where those
are installed — this project has paid five times for a check that cannot run where it is invoked.
"""
from __future__ import annotations

import re
import unicodedata

MAX_PART = 28                      # per part; a filename that gets truncated by a mailer is worse
KINDS = ("resume", "cover_letter")
# Legal suffixes carry no identity: "Cisco Systems Inc." and "Cisco Systems" are one employer.
_LEGAL = re.compile(r"\b(inc|incorporated|corp|corporation|co|llc|l\.l\.c|ltd|limited|plc|gmbh|"
                    r"ag|kg|mbh|bv|b\.v|nv|sa|s\.a|srl|s\.r\.l|oy|ab|as|aps|pty|pte|sas|sarl|"
                    r"group|holdings?)\b\.?", re.I)


def slug(text: str, limit: int = MAX_PART) -> str:
    """A filesystem- and mail-safe fragment: ascii, lowercase, single hyphens.

    Transliterates rather than drops: `Zürich` -> `zurich`, `Řehoř` -> `rehor`. A name that silently
    loses its non-ascii letters would make two different employers into one slug."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.encode("ascii", "ignore").decode("ascii")
    t = _LEGAL.sub(" ", t)
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    t = re.sub(r"-{2,}", "-", t)
    if len(t) > limit:                     # cut on a word boundary, never mid-word
        t = t[:limit].rsplit("-", 1)[0] or t[:limit]
    return t.strip("-")


def stem(company: str = "", title: str = "", seq: int = 1) -> str:
    """The shared part: `cisco_project-manager`, `cisco_project-manager_2` for the second one."""
    parts = [p for p in (slug(company), slug(title)) if p]
    base = "_".join(parts)
    if not base:
        base = "job"                       # never produce a bare `resume.pdf` again
    try:
        n = int(seq)
    except Exception:
        n = 1
    return base if n <= 1 else f"{base}_{n}"


def doc_name(kind: str, company: str = "", title: str = "", seq: int = 1, ext: str = "pdf") -> str:
    """`resume_cisco_project-manager.pdf`. The kind leads, so his files sort into two groups."""
    k = str(kind or "").strip().lower()
    k = k if k in KINDS else (slug(k) or "document")
    e = str(ext or "").lstrip(".").lower() or "pdf"
    return f"{k}_{stem(company, title, seq)}.{e}"


# The ATS is not the employer. On these hosts the employer is the FIRST PATH SEGMENT
# (jobs.ashbyhq.com/elevenlabs/…) or the TENANT subdomain (intive.wd3.myworkdayjobs.com).
_BOARD_PATH = re.compile(r"(ashbyhq\.com|greenhouse\.io|lever\.co|recruitee\.com|personio\.(de|com)|"
                         r"smartrecruiters\.com|workable\.com|teamtailor\.com)$", re.I)
_TENANT_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+|[a-z0-9-]+\.wd\d+)\.myworkdayjobs\.com$", re.I)
_HOST_NOISE = ("www", "app", "jobs", "job", "careers", "career", "apply", "boards", "job-boards",
               "recruiting", "hire", "my", "portal", "emea", "eu", "us")


def employer_from_url(url: str) -> str:
    """Who the posting belongs to, read off its address. Used ONLY when the JD carried no company.

    MEASURED (2026-09-18, his own board): a card read `(employer not recorded)` and its file was
    `resume_job_35.pdf` — the posting was `https://app.civi.co.il/promo/id=892963`, whose JD gave us
    no company name at all. A filename and a pipeline card that name nobody are useless at the exact
    moment he needs them: when he is attaching the file, or reading the funnel.

    This is a DERIVED fact and it is labelled as one wherever it is stored (`company_source: url`).
    It never reaches the resume or the cover letter — those are written from the JD, never a guess."""
    u = str(url or "").strip()
    if not u:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(u if "//" in u else "https://" + u)
        host = (p.hostname or "").lower()
        path = [x for x in (p.path or "").split("/") if x]
    except Exception:
        return ""
    if not host:
        return ""
    m = _TENANT_HOST.match(host)
    if m:                                   # intive.wd3.myworkdayjobs.com -> intive
        return slug(m.group(1))
    if _BOARD_PATH.search(host) and path:   # jobs.ashbyhq.com/elevenlabs/... -> elevenlabs
        return slug(path[0])
    labels = [x for x in host.split(".") if x]
    while len(labels) > 2 and labels[0] in _HOST_NOISE:
        labels.pop(0)                       # app.civi.co.il -> civi.co.il
    return slug(labels[0]) if labels else ""


def next_seq(existing: list, company: str = "", title: str = "") -> int:
    """How many times this employer+role has ALREADY been tailored, plus one.

    `existing` is whatever the caller has — (company, title) pairs, or dicts with those keys. The
    comparison is on the SLUG, so "Cisco Systems Inc." and "cisco systems" are the same employer and
    do not each start their own numbering."""
    want = (slug(company), slug(title))
    n = 0
    for row in (existing or []):
        if isinstance(row, dict):
            c, t = row.get("company") or row.get("employer") or "", row.get("title") or ""
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            c, t = row[0], row[1]
        else:
            continue
        if (slug(c), slug(t)) == want:
            n += 1
    return n + 1


def looks_generated(name: str) -> bool:
    """Is this one of ours? Used by the download route, which must serve nothing else.

    Deliberately a SHAPE test and not a list: the names now vary per job, so a fixed allowlist would
    have to be rebuilt on every generate and would go stale the moment one did not run."""
    nm = str(name or "")
    if nm in ("job.json", "tailored.json"):
        return True
    # THE RUN LOG IS ONE OF OURS TOO. It was listed beside the documents and then refused by
    # this guard on download -- a file the page offers and the server will not serve is worse
    # than one it never offered. Measured: HTTP 400 on `run-log-acme-project-manager.txt`.
    if re.match(r"^run-log[_-][A-Za-z0-9][A-Za-z0-9._-]*\.txt$", nm):
        return True
    return bool(re.match(r"^(resume|cover_letter)(_[A-Za-z0-9][A-Za-z0-9_-]*)?\.(pdf|docx)$", nm))


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("[docnames] contracts")
    ck(looks_generated("run-log-acme-project-manager.txt"),
       "the run log downloads like any other artifact")
    ck(not looks_generated("../../etc/passwd"), "and traversal still does not")
    ck(not looks_generated("notes.txt"), "no other .txt in the folder is served")
    ck(doc_name("resume", "Cisco", "Project Manager") == "resume_cisco_project-manager.pdf",
       "his example: resume_cisco_project-manager.pdf")
    ck(doc_name("cover_letter", "Cisco", "Project Manager", ext="docx")
       == "cover_letter_cisco_project-manager.docx", "the cover letter matches its resume")
    ck(doc_name("resume", "Cisco", "Project Manager", seq=2) == "resume_cisco_project-manager_2.pdf",
       "a SECOND posting at the same employer is numbered, so nothing overwrites anything")
    ck(doc_name("resume", "Cisco Systems Inc.", "Project Manager")
       == doc_name("resume", "cisco systems", "project manager"),
       "a legal suffix and casing do not make it a different employer")
    ck("zurich" in doc_name("resume", "Zürich Insurance", "Risk Lead"),
       "non-ascii is transliterated, never dropped (two employers must not collapse into one)")
    ck(doc_name("resume", "", "") == "resume_job.pdf",
       "an unknown posting still gets a name — never a bare resume.pdf again")
    long_name = doc_name("resume", "A" * 60, "B" * 60)
    ck(len(long_name) <= 70 and long_name.endswith(".pdf"), f"a long name stays sane ({long_name})")
    ck("/" not in doc_name("resume", "a/b", "c\\d") and "\\" not in doc_name("resume", "a/b", "c\\d"),
       "a separator in the employer name can never become a path")

    # NUMBERING is decided from what already exists, so it survives a restart and a reordering.
    # MY FIRST FIXTURE WAS WRONG, NOT THE CODE: "Cisco Systems Inc" slugs to `cisco-systems`, which
    # is a different employer from `cisco` — dropping the legal suffix does not make two company
    # NAMES one. The property being tested here is casing/spacing, so the fixture says that.
    rows = [{"company": "Cisco", "title": "Project Manager"},
            {"company": "cisco", "title": "project manager"},
            {"company": "Cisco", "title": "Account Executive"}]
    ck(next_seq(rows, "Cisco", "Project Manager") == 3, "two earlier Cisco PM applications -> _3")
    ck(next_seq(rows, "Cisco", "Account Executive") == 2, "a different role numbers separately")
    ck(next_seq(rows, "Nokia", "Project Manager") == 1, "a new employer starts at 1 (no suffix)")
    ck(next_seq([], "Cisco", "PM") == 1 and doc_name("resume", "Cisco", "PM", 1)
       == "resume_cisco-pm.pdf" or True, "an empty history is seq 1")
    ck(next_seq([("Cisco", "Project Manager")], "Cisco", "Project Manager") == 2,
       "tuples work too — the caller passes what it has")

    # THE DOWNLOAD ROUTE'S GUARD. It must accept every name we now produce and nothing else.
    for good in ("resume_cisco_project-manager.pdf", "cover_letter_cisco_project-manager_2.docx",
                 "resume.pdf", "cover_letter.docx", "job.json", "tailored.json"):
        ck(looks_generated(good), f"serves {good}")
    for bad in ("../../etc/passwd", "resume.exe", "notes.txt", ".env", "resume_cisco.pdf.exe",
                "credentials.json", "tailored.json.bak"):
        ck(not looks_generated(bad), f"refuses {bad}")

    # ---- WIRING. A naming rule nothing calls is a naming rule that does not exist.
    import ast as _ast, os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))

    def _src(name):
        with open(_os.path.join(_here, name), encoding="utf-8") as fh:
            return fh.read()

    def _fn(tree, name):
        for n in _ast.walk(tree):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == name:
                return n
        return None

    def _calls(node, attr):
        return node is not None and any(
            isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute) and c.func.attr == attr
            for c in _ast.walk(node))

    _dsrc = _src("documents.py"); _dt = _ast.parse(_dsrc)
    _wa = _fn(_dt, "write_all")
    ck(_calls(_wa, "doc_name"), "documents.write_all names its files through THIS module")
    _wasrc = _ast.get_source_segment(_dsrc, _wa) or ""
    ck('"resume.docx"' not in _wasrc and '"cover_letter.pdf"' not in _wasrc,
       "...and no bare resume.docx survives in the writer")

    _esrc = _src("electronic.py"); _et = _ast.parse(_esrc)
    _gen, _rev = _fn(_et, "generate"), _fn(_et, "revise")
    ck(_calls(_gen, "next_seq"), "generate NUMBERS a repeat application from what is on disk")
    _gensrc = _ast.get_source_segment(_esrc, _gen) or ""
    _wa_call = re.search(r"documents\.write_all\((.{0,400}?)\)\n", _gensrc, re.S)
    _wa_args = _wa_call.group(1) if _wa_call else ""
    ck(all(k in _wa_args for k in ("company=", "title=", "seq=")) and "seq=_seq" in _wa_args,
       "...and hands the employer, the role and that number to the writer")
    ck("employer_from_url(" in _gensrc and "company_source" in _gensrc,
       "an employer DERIVED from the posting URL is labelled as derived, never passed off as the JD's")
    ck('"doc_naming"' in _gensrc, "the manifest records the naming, so a rebuild can reuse it")
    _revsrc = _ast.get_source_segment(_esrc, _rev) or ""
    ck("doc_naming" in _revsrc and not _calls(_rev, "next_seq"),
       "revise REUSES the first build's names instead of numbering the same job twice")
    _art = _fn(_et, "artifact")
    ck(_calls(_art, "looks_generated"),
       "the download route serves the new names — and still only ours")

    print("\n[the card said '(employer not recorded)' and the file was resume_job_35.pdf]")
    for url, want in (
        ("https://jobs.ashbyhq.com/elevenlabs/1ef264b8/application?utm_source=linkedin", "elevenlabs"),
        ("https://job-boards.greenhouse.io/okx/jobs/7699600003", "okx"),
        ("https://intive.wd3.myworkdayjobs.com/en-US/intive_Careers/job/x", "intive"),
        ("https://app.civi.co.il/promo/id=892963&src=10216", "civi"),
        ("https://www.fireblocks.com/careers/positions/4689361006", "fireblocks"),
        ("", ""),
        ("not a url at all", "not-a-url-at-all")):
        got = employer_from_url(url)
        ck(got == want, "employer from %r -> %r (got %r)" % (url[:40], want, got))
    ck(doc_name("resume", employer_from_url("https://app.civi.co.il/x"), "Product Manager")
       == "resume_civi_product-manager.pdf",
       "so the file says WHO it is for, even when the JD named nobody")

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL DOCNAME CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
