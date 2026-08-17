#!/usr/bin/env python3
"""PRE-SUBMIT AUDIT — the last gate before the one action that cannot be undone.

WHY HERE AND NOWHERE ELSE. FOUR_MODEL_CONSENSUS.md §7.6 rule 2 is "do not put a model in a request
path, ever", and the apply loop IS a request path: ~30 DOM actions per application at 570-1004ms
each. A panel per action would cost 4x the tokens, 2-4x the wall clock and 4x the 429 surface, and
buy nothing -- if the driver picks a wrong button the page simply does not advance and the
repeat-breaker catches it. That failure is self-correcting.
Submission is not. It happens ONCE per application and cannot be retracted. Today the only guard is
"no required field is blank", which does not catch a wrong postal code, a start date in the past, a
screening answer that contradicts candidate.md, or last week's PDF attached to this week's job. At
400 applications those are silent, expensive mistakes.

GOVERNANCE (carried verbatim from the doc's four invariants):
  * DETERMINISTIC CHECKS DECIDE. The panel only ever advises.
  * THE PANEL CANNOT BLOCK. A model hiccup must never cost a real application, so a concern is
    logged and sent to Telegram and the submit still goes ahead. Only a hard deterministic failure
    stops it.
  * THE AUDITOR IS NEVER THE AUTHOR AND NEVER THE SAME VENDOR.
  * FAIL OPEN. Any exception in here allows the submit, because the deterministic checks that
    matter have already run and passed.

MODELS: addressed by ROLE ALIAS so backend/app/llm.py stays the single home for model choice.
  jhw-answer  -> deepseek-3.2      (author,  805ms measured on our real action-JSON task)
  jhw-answer2 -> mistral-3-14B     (auditor, 570ms, DIFFERENT vendor)
  jhw-answer3 -> llama-4-maverick  (third,  1004ms, DIFFERENT vendor again)
Deliberately NOT the cybergod four: only two overlap, and kimi-k2.6 / gemma-4-31B-it carry
documented quirks here (kimi needs temperature 0.6 with response_format dropped; gemma measured
erratic on identical input). Probe before adding a fourth -- ids are measured, not assumed.
"""
from __future__ import annotations
import asyncio, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
# role aliases, resolved by backend/app/proxy.py -> llm.py::DEFAULT_MODELS
AUTHOR  = os.getenv("JHW_PRESUBMIT_AUTHOR",  "jhw-answer")
AUDITOR = os.getenv("JHW_PRESUBMIT_AUDITOR", "jhw-answer2")
THIRD   = os.getenv("JHW_PRESUBMIT_THIRD",   "jhw-answer3")
ENABLED = os.getenv("JHW_PRESUBMIT", "1").lower() not in ("0", "false", "no")
PANEL   = os.getenv("JHW_PRESUBMIT_PANEL", "1").lower() not in ("0", "false", "no")

# HARD checks stop a submission. SOFT checks are recorded and reported, never blocking.
HARD = {"req_empty", "no_honeypot", "docs_attached", "date_sane"}


def _log(m: str) -> None:
    print(f"[presubmit] {m}", flush=True)


# ---------------------------------------------------------------- deterministic checks
_DATE_RX = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{2})[-/](\d{2})[-/](\d{4})\b")


def _parse_date(v: str):
    m = _DATE_RX.search(v or "")
    if not m:
        return None
    try:
        if m.group(1):
            return time.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%Y-%m-%d")
        return time.strptime(f"{m.group(6)}-{m.group(5)}-{m.group(4)}", "%Y-%m-%d")
    except Exception:
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# label fragment -> candidate.md key. Only fields we can check OBJECTIVELY are listed; a screening
# question is prose and belongs to the panel, not to a string comparison.
_FIELD_MAP = (
    (r"first\s*name",                  "first_name"),
    (r"last\s*name|surname|family",    "last_name"),
    (r"address\s*line\s*1|street",     "address_line"),
    (r"^city|town",                    "city"),
    (r"zip|postal",                    "postal_code"),
    (r"e-?mail",                       "email"),
    (r"phone|mobile|telefon",          "phone"),
)


def checks(snap: dict, profile: dict, docs: dict) -> list:
    """Return [(name, ok, detail)] -- the evidence contract, one line per property.

    snap is llm_driver's ENUM_JS output, so the honeypot guard already applied when it was built."""
    out = []
    els = snap.get("els") or []
    filled = [e for e in els if str(e.get("val") or "").strip()]

    missing = snap.get("missing") or []
    out.append(("req_empty", not missing,
                "no required field left blank" if not missing else f"still blank: {missing[:6]}"))

    # a bot trap must be EMPTY. ENUM_JS should never have indexed it; this proves it end to end.
    trapped = [e for e in filled
               if re.search(r"robots? only|do not enter|leave (this|it) (blank|empty)",
                            (e.get("label") or ""), re.I)]
    out.append(("no_honeypot", not trapped,
                "no bot-trap field carries a value" if not trapped
                else f"HONEYPOT FILLED: {[e.get('label') for e in trapped]}"))

    bad = []
    for e in filled:
        lab, val = (e.get("label") or ""), str(e.get("val") or "").strip()
        for rx, key in _FIELD_MAP:
            if re.search(rx, lab, re.I):
                want = str(profile.get(key) or "").strip()
                if want and _norm(want) != _norm(val):
                    bad.append(f"{lab.strip()[:28]}={val[:22]!r} but candidate.md says {want[:22]!r}")
                break
    out.append(("matches_profile", not bad,
                f"{len(filled)} filled values agree with candidate.md" if not bad
                else "MISMATCH: " + "; ".join(bad[:4])))

    today = time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")
    past = []
    for e in filled:
        if not re.search(r"date|available|start|begin", (e.get("label") or ""), re.I):
            continue
        d = _parse_date(str(e.get("val")))
        if d and d < today:
            past.append(f"{(e.get('label') or '')[:30].strip()}={e.get('val')}")
    out.append(("date_sane", not past,
                "no date is in the past" if not past else f"DATE IN THE PAST: {past[:3]}"))

    miss_docs = [k for k, v in (docs or {}).items() if not v]
    out.append(("docs_attached", not miss_docs,
                f"documents present: {sorted(k for k, v in (docs or {}).items() if v)}"
                if not miss_docs else f"MISSING: {miss_docs}"))
    return out


def evidence(chk: list) -> str:
    """The doc's contract: one line per check, machine-readable, plus a terminal sentinel."""
    return "\n".join(f"CHECK|{n}|{'ok' if ok else 'FAIL'}|{d}" for n, ok, d in chk) + "\nEND_OF_EVIDENCE"


# ---------------------------------------------------------------- the panel (advisory only)
_SYS = ("You review a job application that is ONE CLICK from being submitted on the candidate's "
        "behalf. It cannot be retracted. You do NOT decide whether to submit; deterministic code "
        "does. Report only what you can see in the evidence. Never invent a field. "
        'Answer STRICT JSON: {"verdict":"ok"|"concern","issues":["..."]} and nothing else.')
_AUDIT = (" You are the AUDITOR and you are not the author. Ask what the deterministic checks CANNOT "
          "see: an answer that contradicts the profile, a value that is technically present but "
          "wrong for this posting, a field the checks do not cover at all.")


async def _ask_model(model: str, role: str, ev: str, form: str, prof: str, jd: str) -> dict:
    body = (f"EVIDENCE (deterministic, already computed):\n{ev}\n\n"
            f"THE FORM AS FILLED:\n{form}\n\nCANDIDATE PROFILE (authoritative):\n{prof}\n\n"
            f"JOB (first 600 chars):\n{jd[:600]}")
    msgs = [{"role": "system", "content": _SYS + (_AUDIT if role == "auditor" else "")},
            {"role": "user", "content": body}]
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}",
                                  "Content-Type": "application/json"},
                         json={"model": model, "messages": msgs, "temperature": 0.1,
                               "max_tokens": 700})
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    d = json.loads(m.group(0)) if m else {}
    iss = [str(x)[:180] for x in (d.get("issues") or []) if str(x).strip()][:5]
    return {"model": model, "role": role,
            "verdict": ("concern" if str(d.get("verdict", "")).lower().startswith("conc") else "ok"),
            "issues": iss}


async def panel(ev: str, form: str, prof: str, jd: str) -> list:
    """Two reviewers from DIFFERENT vendors, in parallel. A third only if they disagree.
    Every failure mode returns [] -- the panel can never stop an application."""
    if not PANEL:
        return []
    jobs = [_ask_model(AUTHOR, "author", ev, form, prof, jd),
            _ask_model(AUDITOR, "auditor", ev, form, prof, jd)]
    res = await asyncio.gather(*jobs, return_exceptions=True)
    revs = [x for x in res if isinstance(x, dict)]
    for x in res:
        if isinstance(x, Exception):
            _log(f"a reviewer did not answer ({type(x).__name__}) — continuing, it cannot block")
    if len(revs) == 2 and revs[0]["verdict"] != revs[1]["verdict"]:
        _log("reviewers disagree — asking the third vendor")
        try:
            revs.append(await _ask_model(THIRD, "auditor", ev, form, prof, jd))
        except Exception as e:
            _log(f"third vendor unavailable ({type(e).__name__})")
    return revs


# ---------------------------------------------------------------- the decision (PURE)
def decide(chk: list, revs: list) -> dict:
    """PURE so it is testable with no browser and no models -- the doc's `_decide_from_verdict`
    lesson. HARD deterministic failures block. Everything the panel says is advisory."""
    hard = [(n, d) for n, ok, d in chk if not ok and n in HARD]
    soft = [(n, d) for n, ok, d in chk if not ok and n not in HARD]
    flags, concerned = [], 0
    for r in revs or []:
        if r.get("verdict") == "concern":
            concerned += 1
        for i in r.get("issues") or []:
            flags.append(f"{r.get('model')}: {i}")
    return {"allow": not hard,
            "blocking": [f"{n}: {d}" for n, d in hard],
            "soft": [f"{n}: {d}" for n, d in soft],
            "flags": flags[:8],
            "reviewers": len(revs or []),
            "concerned": concerned}


# ---------------------------------------------------------------- entry point
async def audit(page, profile: dict, docs: dict, jd: str = "") -> dict:
    """Called immediately before Submit. Returns the decide() dict. FAILS OPEN."""
    if not ENABLED:
        _log("disabled (JHW_PRESUBMIT=0) — not auditing")
        return {"allow": True, "blocking": [], "soft": [], "flags": [], "reviewers": 0,
                "concerned": 0, "skipped": True}
    try:
        from llm_driver import ENUM_JS          # ONE home for the honeypot-guarded field index
        snap = await page.evaluate(ENUM_JS)
        chk = checks(snap, profile, docs)
        for n, ok, d in chk:
            _log(f"{'OK  ' if ok else 'FAIL'} {n:<16} {d}")
        ev = evidence(chk)
        form = "\n".join(
            f"{(e.get('label') or e.get('text') or '?')[:44]} = {str(e.get('val'))[:44]}"
            for e in (snap.get("els") or []) if str(e.get("val") or "").strip())[:2600]
        prof = "\n".join(f"{k}: {v}" for k, v in sorted((profile or {}).items()))[:2000]
        revs = await panel(ev, form, prof, jd)
        for r in revs:
            _log(f"panel {r['role']:<7} {r['model']:<12} {r['verdict']}"
                 + (f" :: {'; '.join(r['issues'])[:150]}" if r["issues"] else ""))
        d = decide(chk, revs)
        _log(("ALLOW submit" if d["allow"] else "BLOCK submit") +
             f" | {d['reviewers']} reviewer(s), {d['concerned']} concerned, {len(d['flags'])} flag(s)")
        return d
    except Exception as e:
        # FAIL OPEN, loudly. The deterministic checks that matter already ran inside the driver.
        _log(f"audit itself failed ({type(e).__name__}: {str(e)[:80]}) — ALLOWING the submit")
        return {"allow": True, "blocking": [], "soft": [], "flags": [],
                "reviewers": 0, "concerned": 0, "error": str(e)[:120]}


# ---------------------------------------------------------------- self-test (runs inside jhw.py apply)
def _selftest() -> int:
    """`python3 flows/presubmit.py --logic`. Pure: no browser, no models, no network. Wired into
       jhw.py's _SELFTESTS so it runs before every apply -- a guard you must remember to run does
       not exist."""
    fails = []

    def ck(name, ok, detail=""):
        print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  :: {str(detail)[:90]}" if not ok else ""))
        if not ok:
            fails.append(name)

    P = {"first_name": "Evgeny", "last_name": "Vainshtein", "city": "Frankfurt",
         "postal_code": "61169", "address_line": "Hermann-J.-Bach-Weg 16",
         "email": "evgeny@s4biz.io", "phone": "+49 15785541545"}
    D = {"resume.pdf": True, "cover_letter.pdf": True}

    def sn(els, missing=None):
        return {"els": els, "missing": missing or [], "errors": [], "url": "x", "review": True}

    def E(label, val, **kw):
        return dict(label=label, val=val, **kw)

    print("-- the real Accenture form, correctly filled")
    good = sn([E("First Name *", "Evgeny"), E("Last Name *", "Vainshtein"),
               E("Address Line 1 *", "Hermann-J.-Bach-Weg 16"), E("City *", "Frankfurt"),
               E("Postal Code *", "61169"),
               E("What date would you be available to begin?", "20-09-2026")])
    c = checks(good, P, D)
    ck("a correct application is ALLOWED", decide(c, [])["allow"])

    print("-- HARD failures must stop an irreversible action")
    ck("blank required field BLOCKS",
       not decide(checks(sn([E("City *", "Frankfurt")], missing=["Last Name *"]), P, D), [])["allow"])
    ck("a FILLED HONEYPOT blocks",
       not decide(checks(sn([E("Enter website. This input is for robots only, do not enter if "
                               "you're human.", "x")]), P, D), [])["allow"])
    ck("a start date IN THE PAST blocks",
       not decide(checks(sn([E("Available to begin", "20-07-2026")]), P, D), [])["allow"])
    ck("a missing document blocks",
       not decide(checks(sn([E("First Name *", "Evgeny")]), P,
                         {"resume.pdf": True, "cover_letter.pdf": False}), [])["allow"])

    print("-- a profile mismatch is REPORTED but must not cost an application")
    c3 = checks(sn([E("Postal Code *", "60311")]), P, D)
    d3 = decide(c3, [])
    ck("a wrong postal code is detected", any(n == "matches_profile" and not ok for n, ok, _ in c3))
    ck("...soft, not blocking, and names both values",
       d3["allow"] and any("60311" in x and "61169" in x for x in d3["soft"]), d3["soft"])

    print("-- THE PANEL ADVISES, IT NEVER DECIDES")
    angry = [{"model": "deepseek-3.2", "role": "author", "verdict": "concern", "issues": ["bad"]},
             {"model": "mistral-3-14B", "role": "auditor", "verdict": "concern", "issues": ["no"]}]
    dp = decide(c, angry)
    ck("two reviewers at 'concern' still ALLOW", dp["allow"], dp)
    ck("...but the flags are recorded", len(dp["flags"]) == 2 and dp["concerned"] == 2)
    ck("no reviewers at all (429s) still allows", decide(c, [])["allow"])
    ck("a happy panel cannot rescue a hard failure",
       not decide(checks(sn([], missing=["City *"]), P, D),
                  [{"model": "m", "role": "author", "verdict": "ok", "issues": []}])["allow"])

    print("-- the evidence contract + date parsing")
    ev = evidence(c)
    ck("one CHECK| line per check, plus the sentinel",
       ev.count("CHECK|") == len(c) and ev.endswith("END_OF_EVIDENCE"))
    ck("a failing check is marked FAIL", "|FAIL|" in evidence(c3))
    fut = time.strftime("%d-%m-%Y", time.localtime(time.time() + 86400 * 40))
    ck("a future date passes",
       all(ok for n, ok, _ in checks(sn([E("Available to begin", fut)]), P, D) if n == "date_sane"))
    ck("ISO yyyy-mm-dd is parsed too",
       not [1 for n, ok, _ in checks(sn([E("Start date", "2026-01-05")]), P, D)
            if n == "date_sane" and ok])
    ck("a non-date field containing digits is not date-checked",
       all(ok for n, ok, _ in checks(sn([E("Postal Code *", "61169")]), P, D) if n == "date_sane"))

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("ALL PRE-SUBMIT CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--logic" in sys.argv else 0)
