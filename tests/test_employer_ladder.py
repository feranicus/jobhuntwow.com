#!/usr/bin/env python3
"""WHO IS THIS JOB WITH? — the ladder, and the guard on its model rung.

His words (2026-09-18), looking at a card that said `(employer not recorded)` above 3,623 characters
of pasted job description: *"why Employer not recorded? in every job description there is a name of
the company so this suppose to be there"*.

THE LADDER, most certain first — the same shape, and the same reasoning, as the apply engine's:
    1. the ATS / JSON-LD parser, or the pasted text's own header   (jd_ingest.sniff_title_company)
    2. THE MODEL, reading the posting — accepted ONLY if the name is IN the posting
    3. the posting's address                                        (docnames.employer_from_url)
    4. nothing, said plainly

THE PROPERTY THAT MAKES RUNG 2 SAFE is the one Set-of-Mark and the closed option list use: the model
may only name something the source already contains, so a hallucinated employer is structurally
impossible rather than merely unlikely. This suite runs that rung with a STUBBED model — a real one
would make the test a coin flip, and the guard is what is being tested, not the vendor.

Stdlib + the app's own deps. Touches no network: the stub replaces the only call that would.
"""
import asyncio
import ast
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="jhwemp"))

FAILS = []


def ck(cond, msg, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + msg + (("   [%s]" % detail) if detail else ""))
    if not cond:
        FAILS.append(msg)


def main() -> int:
    print("[employer] the ladder that answers 'who is this job with?'")
    from app import electronic as E, jd_ingest as J, docnames as D

    JD = ("About the job\n"
          "Fireblocks is looking for a Senior Product Manager to join our platform team. "
          "You will own the roadmap for custody.")

    # ---- rung 1: deterministic, and it already answers the case he hit
    t, c = J.sniff_title_company(JD)
    ck(c == "Fireblocks", "rung 1 reads the employer straight out of the pasted text", "%r/%r" % (t, c))

    # ---- rung 2: the model, with the guard
    real = E._ask
    try:
        async def ok_model(*a, **k):
            return {"company": "Fireblocks"}

        async def liar(*a, **k):
            return {"company": "Coinbase"}          # never appears in the posting

        async def empty(*a, **k):
            return {"company": ""}

        async def boom(*a, **k):
            raise RuntimeError("upstream 429")

        E._ask = ok_model
        ck(asyncio.run(E._company_from_model(JD)) == "Fireblocks",
           "rung 2 accepts a name the posting actually contains")
        E._ask = liar
        ck(asyncio.run(E._company_from_model(JD)) == "",
           "rung 2 REFUSES an employer the posting never mentions (no invented employer, ever)")
        E._ask = empty
        ck(asyncio.run(E._company_from_model(JD)) == "", "an empty answer is not an employer")
        E._ask = boom
        ck(asyncio.run(E._company_from_model(JD)) == "",
           "a model failure costs nothing — the ladder simply moves on")
        E._ask = ok_model
        ck(asyncio.run(E._company_from_model("too short")) == "",
           "a stub of a posting is never sent to a model at all")
    finally:
        E._ask = real

    # ---- rung 3
    ck(D.employer_from_url("https://app.civi.co.il/promo/id=892963") == "civi",
       "rung 3 falls back to the posting's address")

    # ---- THE ORDER IS THE POINT. A guess must never outrank a fact.
    src = open(os.path.join(ROOT, "backend", "app", "electronic.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    gen = next((n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "generate"), None)
    gsrc = ast.get_source_segment(src, gen) or ""
    i_jd = gsrc.find('jd.get("company")')
    i_llm = gsrc.find("_company_from_model(")
    i_url = gsrc.find("employer_from_url(")
    ck(-1 < i_jd < i_llm < i_url,
       "the JD's own word, then the model, then the URL — in that order", "%d<%d<%d" % (i_jd, i_llm, i_url))
    ck('"company_source"' in gsrc or "company_source=" in gsrc,
       "and which rung answered is RECORDED, so a guess is never passed off as the JD's word")
    cfm = next((n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_company_from_model"), None)
    csrc = ast.get_source_segment(src, cfm) or ""
    ck("company_in_text(" in csrc,
       "the model's answer goes through the verbatim check — the guard cannot be skipped")

    # ---- AND NOTHING DERIVED MAY REACH THE DOCUMENTS THEMSELVES.
    # The resume and the cover letter are written from the JD by the consensus chain. A derived
    # employer is for the FILENAME, the card and the record — never for the prose. Measured: every
    # line that mentions `_emp` must come AFTER the consensus ran, and none of them may be a call
    # into it. (My first two attempts at this check were a tautology and an empty message — the
    # vacuous-check defect this repo records ~20 times. This one names its subject.)
    lines = gsrc.splitlines()
    emp_lines = [i for i, ln in enumerate(lines) if "_emp" in ln]
    con_line = next((i for i, ln in enumerate(lines) if ln.strip().startswith("con = ")), -1)
    ck(con_line >= 0, "the consensus call was found in generate()", "line %d" % con_line)
    ck(bool(emp_lines) and min(emp_lines) > con_line,
       "the derived employer appears only AFTER the documents have been written")
    ck(not any("RC." in lines[i] or "_ask(" in lines[i] for i in emp_lines),
       "...and is never handed to the chain that writes them")

    print("=" * 66)
    if FAILS:
        print("[X] %d EMPLOYER-LADDER CONTRACT(S) BROKEN" % len(FAILS))
        for f in FAILS:
            print("    " + f)
        return 1
    print("ALL EMPLOYER-LADDER CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
