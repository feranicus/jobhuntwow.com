#!/usr/bin/env python3
"""CHOOSING FROM A CLOSED LIST — and ASKING when we cannot.

WHY THIS EXISTS, from the OKX Greenhouse run of 2026-08-17. The adapter filled seven fields
correctly, attached both documents, then printed:

    REQUIRED+EMPTY: ['Country*', '', 'Location (City)*', '', 'How did you hear about us?*', '']
    REFUSING to submit: still required+empty -> ...

and RETURNED. The operator asked, fairly, why the models had invented answers and carried on. They had
not: **no model ran and no question was asked.** The adapter measured the gap, printed it, and gave up.
Nothing invented anything; nothing asked him either. That is worse than a wrong answer, because a
wrong answer at least moves and can be corrected.

WHAT THIS DOES, in order, cheapest and most certain first:
    1. the RECORDED human answer for this ATS         (flows/knowledge/*.json)
    2. the LEARNED answer from a previous application  (learned.py)
    3. a DETERMINISTIC RULE over the real option list  (screening defaults, EEOC)
    4. the 3-LLM PANEL, voting over the ACTUAL options (quorum of 2 of 3, different vendors)
    5. the HUMAN on Telegram, with the real options as numbered choices
A model is the FOURTH thing consulted, never the first.

**THE PROPERTY THAT MAKES A MODEL SAFE HERE: it can only return one of the strings we showed it.**
`_pick()` matches an answer against the option list and refuses anything that is not a member -- so a
hallucinated option is structurally unusable, exactly as a Set-of-Mark number outside 1..N is. The
caller therefore never has to trust the model's judgement about what exists, only about which of the
real choices is right.

IT NEVER GUESSES A LEGAL DECLARATION. Veteran status, disability status and race/ethnicity are
answered ONLY from an explicit rule the candidate wrote down, or by asking him. A model must not
decide what somebody declares about themselves to an employer, and `SENSITIVE` enforces that.
"""
from __future__ import annotations
import asyncio
import json
import os
import re

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
PANEL = [m.strip() for m in os.getenv(
    "JHW_PANEL", "jhw-answer,jhw-answer2,jhw-answer3").split(",") if m.strip()]
TIMEOUT = float(os.getenv("JHW_CHOOSE_TIMEOUT", "20"))
QUORUM = int(os.getenv("JHW_CHOOSE_QUORUM", "2"))

# A DECLARATION ABOUT ONESELF IS NOT A FORM FIELD TO OPTIMISE. These go to a written rule or to the
# human -- never to a model. Getting one wrong is a misrepresentation to an employer.
SENSITIVE = re.compile(
    # `gender identity` was here but plain `gender` was not, so "What is your legal gender?" was not
    # flagged -- and once the built-in "Male" was removed, that question would have gone to the PANEL.
    # Three models voting on somebody's gender is exactly what must never happen.
    r"veteran|disab|race|ethnic|\bgender\b|sexual orientation|transgender|pronoun|"
    r"hispanic|latino|criminal|conviction|visa status|sponsorship|security clearance|"
    # A SALARY IS A NEGOTIATING POSITION, not a form field to optimise. A model inventing a number
    # here costs real money and cannot be taken back, so it goes to a written figure or to him.
    r"salary|compensation|expected pay|rate expectation|desired (pay|salary)", re.I)


def norm(s: str) -> str:
    s = re.sub(r"[*·•:]+", " ", (s or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def _pick(answer: str, options: list) -> str:
    """An answer -> the EXACT option string it names, or ''. THE HALLUCINATION GUARD.

    Accepts an index ("3"), an exact string, a case/punctuation variant, or an unambiguous prefix.
    Refuses anything that names no option and anything ambiguous -- two candidates would mean the
    order of the list decides silently, which is the defect that put a footer link at index 0."""
    a = (answer or "").strip().strip(".").strip()
    if not a or not options:
        return ""
    m = re.match(r"^\[?(\d{1,2})\]?[).\s]*$", a)
    if m:
        i = int(m.group(1))
        return options[i - 1] if 1 <= i <= len(options) else ""
    for o in options:                                   # exact, then normalised
        if a == o:
            return o
    na = norm(a)
    hits = [o for o in options if norm(o) == na]
    if len(hits) == 1:
        return hits[0]
    hits = [o for o in options if na and norm(o).startswith(na)]
    if len(hits) == 1:
        return hits[0]
    # SUBSTRING MATCHING IS THE LAST RESORT AND IT NEARLY COST A REAL APPLICATION.
    # `_pick("ger", [...])` returned **'Algeria +213'**, because "ger" sits inside al-GER-ia and that
    # was the only hit in the truncated list it was shown. A three-letter fragment landing in the
    # middle of a word is not evidence of anything. So: at least 4 characters, and it must begin at a
    # WORD BOUNDARY -- which "Germany" satisfies and "Algeria" does not.
    if len(na) >= 4:
        hits = [o for o in options if re.search(r"(?:^|[^a-z0-9])" + re.escape(na), norm(o))]
        if len(hits) == 1:
            return hits[0]
    return ""


# ---------------------------------------------------------------- rule layer
# Written by the candidate, in candidate.md's `## screening_defaults`. A rule is only ever applied if
# its answer is ACTUALLY ONE OF THE OPTIONS on this page.
_RULES = (
    # The phone-country flyout offers entries like "Germany +49", so a prefix match is what resolves
    # it -- and `_pick` refuses the match if two options share that prefix.
    (r"^country|country code|phone country|country/territory",
                                                            ["Germany", "Deutschland"]),
    (r"^(state|region|province)|bundesland|^land$",           ["Hesse", "Hessen"]),
    (r"how did you hear|source|referral source",            ["LinkedIn", "Job Board", "Internet",
                                                             "Other"]),
    (r"authoris|authoriz|legally (be )?(able|entitled) to work|work permit|right to work",
                                                            ["Yes"]),
    (r"require (visa )?sponsorship|need sponsorship",        ["No"]),
    (r"willing to relocate|open to relocat",                 ["No"]),
    (r"willing to travel|able to travel",                    ["Yes"]),
    (r"worked (for|at) (us|this)|previously employed|former employee",
                                                            ["No"]),
    (r"currently employed by|current employee of",           ["No"]),
    (r"non-?compete|noncompete",                             ["No"]),
    # NO BUILT-IN PREFERENCE FOR A SELF-DECLARATION -- I wrote "Male" / "He/Him" here this morning,
    # which is the owner's answer and nobody else's. These resolve ONLY from `defaults`, i.e. from what
    # the user wrote in candidate.md; with nothing written, `rule()` returns "" and `decide()` asks
    # them. `SENSITIVE` already keeps the panel away; this keeps ME away too.
    (r"notice period",                                       ["1 month", "One month"]),
)

# THE RULE LAYER RUNS BEFORE THE SENSITIVITY CHECK ON PURPOSE. A declaration the candidate has WRITTEN
# DOWN (candidate.md `## screening_defaults`, including the EEOC answers he chose in his own
# recording) is his own answer and needs no model and no interruption. Sensitivity only removes the
# PANEL as an option; it never blocks his own written word.


def rule(question: str, options: list, defaults: dict | None = None) -> str:
    """A deterministic answer from what the candidate wrote down, or ''. PURE."""
    q = norm(question)
    for key, val in (defaults or {}).items():
        if norm(key) and norm(key) in q:
            got = _pick(str(val), options)
            if got:
                return got
    for rx, prefs in _RULES:
        if re.search(rx, q):
            for p in prefs:
                got = _pick(p, options)
                if got:
                    return got
    return ""


# ---------------------------------------------------------------- panel layer
_SYS = ("You are filling in one field of a real job application for a candidate. You will be given "
        "the question and the EXACT list of options the page offers. Reply with ONLY the number of "
        "the option to choose. No prose, no explanation, no other text. If none of the options is "
        "defensible from the candidate facts you were given, reply exactly: NONE")


async def _ask_one(model: str, prompt: str) -> str:
    body = {"model": model, "temperature": 0.0, "max_tokens": 8,
            "messages": [{"role": "system", "content": _SYS},
                         {"role": "user", "content": prompt}]}
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()


async def panel(question: str, options: list, facts: str = "", log=print) -> dict:
    """3 vendors vote; a QUORUM of 2 on the same real option wins. Anything not in the list is
    dropped BEFORE votes are counted, so a majority can never 'agree and be ignored'."""
    if not options or not PANEL:
        return {"value": "", "why": "no options or no panel configured"}
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    prompt = (f"CANDIDATE FACTS:\n{facts.strip()[:1500]}\n\n"
              f"QUESTION: {question.strip()}\n\nOPTIONS:\n{numbered}\n\n"
              f"Reply with one number from 1 to {len(options)}, or NONE.")
    got = await asyncio.gather(*[_ask_one(m, prompt) for m in PANEL], return_exceptions=True)
    votes: dict = {}
    for m, raw in zip(PANEL, got):
        if isinstance(raw, Exception):
            log(f"    [choose] {m} unavailable ({type(raw).__name__})")
            continue
        val = _pick(str(raw), options)
        if not val:
            log(f"    [choose] {m} -> {str(raw)[:24]!r} names no option — REFUSED")
            continue
        log(f"    [choose] {m} -> {val[:40]!r}")
        votes[val] = votes.get(val, 0) + 1
    if not votes:
        return {"value": "", "why": "nobody named a real option"}
    best, n = max(votes.items(), key=lambda kv: kv[1])
    if n >= QUORUM:
        log(f"    [choose] QUORUM {n}/{len(PANEL)} -> {best[:44]!r}")
        return {"value": best, "why": f"quorum {n}/{len(PANEL)}"}
    return {"value": "", "why": "no majority (" + ", ".join(f"{k}x{v}" for k, v in votes.items()) + ")"}


# ---------------------------------------------------------------- the ladder
def ask_text(question: str, options: list, why: str = "") -> str:
    """The message the human sees. The REAL options, numbered, so one tap is a complete answer."""
    head = "I need one answer to finish this application."
    if why:
        head += f" ({why})"
    body = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options[:12]))
    tail = ("\nReply with the number, or type the answer."
            if options else "\nReply with the answer.")
    return f"{head}\n\nQ: {question.strip()}\n\n{body}{tail}"


async def decide(question: str, options: list, *, ats: str = "", employer: str = "",
                 facts: str = "", defaults: dict | None = None, asker=None,
                 log=print) -> dict:
    """One field, one answer, or an honest ''. Returns {value, via, asked}.

    `value` is GUARANTEED to be a member of `options` when options were supplied -- every layer runs
    its answer through `_pick()`. The caller can therefore act on it without re-validating."""
    out = {"value": "", "via": "none", "asked": False}
    sensitive = bool(SENSITIVE.search(question or ""))

    # 1. what a human actually chose on this ATS
    try:
        import recordings as KB
        # A CHOICE FIELD'S RECORDED ANSWER IS THE OPTION HE CLICKED, never the text he typed to find
        # it. `known_value` returns the fill, which for a combobox is a SEARCH QUERY -- and using it
        # here is what put 'Algeria +213' on a German application.
        raw = KB.known_choice(ats, question) if ats else ""
        v = _pick(raw, options)
        if raw and not v:
            log(f"    [choose] {question[:34]!r}: recorded {raw[:26]!r} is not among the "
                f"{len(options)} option(s) on screen — not using it")
        if v:
            log(f"    [choose] {question[:40]!r} <- RECORDED human choice: {v[:34]!r}")
            return {"value": v, "via": "knowledge", "asked": False}
        if not options:                       # a free-text field: the recorded FILL is the answer
            fv = KB.known_value(ats, question) if ats else ""
            if fv and not fv.startswith("<redacted"):
                log(f"    [choose] {question[:40]!r} <- RECORDED value: {fv[:34]!r}")
                return {"value": fv, "via": "knowledge", "asked": False}
    except Exception:
        pass
    # 2. what we answered last time and got through with
    try:
        import learned as LN
        v = _pick(LN.recall(question, employer=employer) or "", options)
        if v:
            log(f"    [choose] {question[:40]!r} <- LEARNED: {v[:34]!r}")
            return {"value": v, "via": "learned", "asked": False}
    except Exception:
        pass
    # 3. a rule the candidate wrote down
    v = rule(question, options, defaults)
    if v:
        log(f"    [choose] {question[:40]!r} <- RULE: {v[:34]!r}")
        return {"value": v, "via": "rule", "asked": False}
    # 4. the panel — but NEVER for a self-declaration
    if sensitive:
        log(f"    [choose] {question[:44]!r} is a SELF-DECLARATION — no model may answer it")
    elif options:
        try:
            p = await panel(question, options, facts=facts, log=log)
            if p["value"]:
                return {"value": p["value"], "via": "panel", "asked": False}
            log(f"    [choose] the panel could not decide: {p['why']}")
        except Exception as e:
            log(f"    [choose] panel unavailable ({type(e).__name__})")
    # 5. ask the human, with the real options
    if asker is None:
        return out
    why = "it is a declaration about you, so I will not answer it for you" if sensitive else \
          "none of my own sources covers it"
    try:
        reply = await asker(ask_text(question, options, why))
    except Exception as e:
        log(f"    [choose] could not ask ({type(e).__name__})")
        return out
    out["asked"] = True
    v = _pick(reply or "", options) if options else (reply or "").strip()
    if v:
        log(f"    [choose] YOU said {str(reply)[:22]!r} -> {v[:34]!r}")
        out.update(value=v, via="human")
        try:
            import learned as LN
            LN.remember(question, v, employer=employer)
        except Exception:
            pass
    elif reply:
        log(f"    [choose] your reply {str(reply)[:30]!r} matches none of the {len(options)} options")
    return out


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    # A SUITE MUST NOT WRITE INTO HIS REAL MEMORY, AND MUST NOT READ FROM IT.
    # MEASURED 2026-08-17: with the learned rung finally working (it had been dead — every caller
    # passed `employer=` to a function that only declared `scope=`, and the TypeError was swallowed),
    # section [8] started FAILING: `decide()` answered 'Indeed' from the store before it ever reached
    # the human rung. The store also already contained `pick one -> Indeed`, a FIXTURE from this very
    # suite, sitting in his production answers. Both directions are defects: a test that reads real
    # state is not deterministic, and a test that writes it corrupts what the agent tells an employer.
    import tempfile as _tf
    _td = _tf.TemporaryDirectory()
    os.environ["JHW_MEMORY"] = os.path.join(_td.name, "memory.sqlite")
    os.environ["JHW_LEARNED"] = os.path.join(_td.name, "no-legacy.json")
    try:
        import importlib
        import memory as _MEM
        importlib.reload(_MEM)
    except Exception as _e:
        print(f"  (memory unavailable in this environment: {type(_e).__name__})")

    OPTS = ["LinkedIn", "Indeed", "Job Board", "Referral", "Other"]
    print("\n[1] _pick is the hallucination guard: only a REAL option can come out")
    ck(_pick("LinkedIn", OPTS) == "LinkedIn", "exact string")
    ck(_pick("linkedin", OPTS) == "LinkedIn", "case-insensitive")
    ck(_pick("1", OPTS) == "LinkedIn", "an index")
    ck(_pick("[3]", OPTS) == "Job Board", "a bracketed index")
    ck(_pick("6", OPTS) == "", "an index out of range is refused")
    ck(_pick("0", OPTS) == "", "index 0 is refused (the list is 1-based)")
    ck(_pick("Xing", OPTS) == "", "an option that does not exist is refused")
    ck(_pick("", OPTS) == "", "an empty answer is refused")
    ck(_pick("Job", OPTS) == "Job Board", "an unambiguous prefix resolves")
    ck(_pick("Referral", []) == "", "no options -> nothing can be picked")
    # A GENUINELY AMBIGUOUS PREFIX. My first version used "o" against this list, where only "Other"
    # starts with o -- so it was unambiguous and the check failed correct code. The real-world shape
    # is an option list that shares a stem, which is extremely common on these forms.
    YN = ["Yes", "Yes, with conditions", "No", "No, I do not have a disability"]
    ck(_pick("Ye", YN) == "", f"'Ye' matches two options -> REFUSED, not resolved by order")
    ck(_pick("Yes", YN) == "Yes", "an EXACT match still wins over its own longer neighbour")
    ck(_pick("No", YN) == "No", "and so does the short negative")
    ck(_pick("with conditions", YN) == "Yes, with conditions",
       "an unambiguous substring resolves")
    ck(_pick("N", YN) == "", "'N' matches two -> refused")

    print("\n[2] the rules only ever answer with an option the PAGE offers")
    ck(rule("How did you hear about us?", OPTS) == "LinkedIn", "source -> LinkedIn")
    ck(rule("How did you hear about us?", ["Indeed", "Other"]) == "Other",
       "LinkedIn absent -> the next preference that EXISTS")
    ck(rule("How did you hear about us?", ["Carrier pigeon"]) == "",
       "no preference present -> no answer, rather than a wrong one")
    ck(rule("Are you legally authorised to work in Germany?", ["Yes", "No"]) == "Yes",
       "work authorisation")
    ck(rule("Will you now or in the future require visa sponsorship?", ["Yes", "No"]) == "No",
       "sponsorship")
    ck(rule("Are you willing to relocate?", ["Yes", "No"]) == "No", "relocation")
    # A SELF-DECLARATION HAS NO BUILT-IN ANSWER. With nothing written down there is no rule answer at
    # all, so `decide()` asks the person. With the user's OWN candidate.md entry, it resolves silently.
    ck(rule("What is your legal gender?", ["Male", "Female", "Decline"]) == "",
       "gender: nothing is assumed when the user has not written it down")
    ck(rule("What is your legal gender?", ["Male", "Female", "Decline"],
            {"legal gender": "Female"}) == "Female",
       "gender: whatever the USER wrote is what is used")
    ck(rule("Preferred pronoun?", ["He/Him", "She/Her", "They/Them"]) == "",
       "pronoun: likewise nothing is assumed")
    ck(rule("Salary expectation?", ["Yes", "No"]) == "", "an unknown question gets no rule answer")

    print("\n[3] a candidate-written default outranks the built-in preference")
    ck(rule("How did you hear about us?", OPTS, {"how did you hear": "Referral"}) == "Referral",
       "candidate.md wins")
    ck(rule("How did you hear about us?", OPTS, {"how did you hear": "Carrier pigeon"}) == "LinkedIn",
       "a default that is not on the page falls through to the preference, never types junk")

    print("\n[4] a SELF-DECLARATION never reaches a model")
    for q in ("Are you a protected veteran?", "Do you have a disability?",
              "What is your race/ethnicity?", "Are you Hispanic or Latino?",
              "Do you identify as transgender?", "What are your salary expectations?"):
        ck(bool(SENSITIVE.search(q)), f"flagged sensitive: {q}")
    ck(not SENSITIVE.search("How did you hear about us?"), "an ordinary question is not flagged")
    for q in ("What is your legal gender?", "Gender", "Preferred pronoun?"):
        ck(bool(SENSITIVE.search(q)), f"flagged sensitive: {q}")
    ck(not SENSITIVE.search("Location (City)"), "a city is not a declaration")
    src = open(__file__, encoding="utf-8").read()
    ship = src[:src.index("def _selftest(")]
    dec = ship[ship.index("async def decide("):]
    ck(dec.index("sensitive") < dec.index("await panel("),
       "the sensitivity check happens BEFORE the panel is reached")
    ck("elif options:" in dec, "and it takes the panel branch away entirely")

    print("\n[5] the ladder order is knowledge -> learned -> rule -> panel -> human")
    for a, b in (("KB.known_value", "LN.recall"), ("LN.recall", "v = rule("),
                 ("v = rule(", "await panel("), ("await panel(", "await asker(")):
        ck(dec.index(a) < dec.index(b), f"{a} before {b}")

    print("\n[6] the panel votes over REAL options and cannot be ignored after agreeing")
    pan = ship[ship.index("async def panel("):ship.index("def ask_text(")]
    ck("_pick(str(raw), options)" in pan, "every vote is resolved to a real option first")
    ck(pan.index("_pick(str(raw), options)") < pan.index("votes[val]"),
       "unusable answers are dropped BEFORE the count, so a majority is never miscounted")
    ck("QUORUM" in pan, "a lone voice never decides")

    print("\n[7] the question the human sees carries the REAL options, numbered")
    t = ask_text("How did you hear about us?", OPTS, "none of my own sources covers it")
    ck("1. LinkedIn" in t and "5. Other" in t, "options are numbered from 1")
    ck("How did you hear about us?" in t, "the question is quoted verbatim")
    ck("number" in t.lower(), "it says a number is enough")

    print("\n[8] a human reply is validated the same way a model's is")
    async def _say(x):
        return x
    got = asyncio.run(decide("Pick one", OPTS, asker=lambda q: _say("2"), log=lambda *a: None))
    ck(got["value"] == "Indeed" and got["via"] == "human" and got["asked"],
       f"a number from the human resolves: {got}")
    # A DIFFERENT QUESTION, DELIBERATELY. Asking "Pick one" twice used to work only because the
    # learned rung was dead; now the first reply is REMEMBERED and the second call answers from
    # memory without asking — which is the whole point of the feature, so the fixture was wrong, not
    # the code. A test that depends on the agent forgetting is a test against the product.
    got = asyncio.run(decide("Pick another one", OPTS, asker=lambda q: _say("Mastodon"),
                             log=lambda *a: None))
    ck(got["value"] == "" and got["asked"], f"junk from the human is refused too: {got}")
    got = asyncio.run(decide("Pick one", OPTS, asker=lambda q: _say("Referral"),
                             log=lambda *a: None))
    ck(got["value"] == "Indeed" and got["via"] == "learned" and not got["asked"],
       f"and the question he already answered is NOT asked again: {got}")

    print("\n[9] with no asker configured it returns an honest empty, never a guess")
    got = asyncio.run(decide("Something nobody covers", ["A", "B"], log=lambda *a: None))
    ck(got["value"] == "" and got["via"] == "none" and not got["asked"], str(got))

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} CHOICE CONTRACT(S) BROKEN")
        return 1
    print("ALL CHOICE CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
