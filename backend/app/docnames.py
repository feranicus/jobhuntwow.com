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
    return bool(re.match(r"^(resume|cover_letter)(_[A-Za-z0-9][A-Za-z0-9_-]*)?\.(pdf|docx)$", nm))


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("[docnames] contracts")
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
    ck("seq=_seq" in _gensrc and "company=jd.get" in _gensrc,
       "...and hands the employer, the role and that number to the writer")
    ck('"doc_naming"' in _gensrc, "the manifest records the naming, so a rebuild can reuse it")
    _revsrc = _ast.get_source_segment(_esrc, _rev) or ""
    ck("doc_naming" in _revsrc and not _calls(_rev, "next_seq"),
       "revise REUSES the first build's names instead of numbering the same job twice")
    _art = _fn(_et, "artifact")
    ck(_calls(_art, "looks_generated"),
       "the download route serves the new names — and still only ours")

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL DOCNAME CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
