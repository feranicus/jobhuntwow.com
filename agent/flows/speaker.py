#!/usr/bin/env python3
"""THE FOURTH MODEL — the one that talks to the candidate, and only that.

His words: *"add to this another AI LLM that will communicate with me in natural language and will
translate everything back to the other 3 LLMs and they need to make decisions and fill stuff for the
4th LLM that will do the talking to me. please add kimi 2.6 like we have in cybergod.ai."*

THE SHAPE, and it is the right one:
    3 DECIDERS  (deepseek-3.2 · mistral-3-14B · llama-4-maverick)  vote on ACTIONS  -> escalate.py
    1 SPEAKER   (kimi-k2.6, a FOURTH vendor)                       handles WORDS    -> this module
The speaker never touches the browser, never votes, and never names a selector. It does exactly two
translations:
    OUT: machine state (stall, control index, what was tried, how the panel voted) -> ONE short
         human message with numbered options.
    IN:  his free-text reply -> one of converse.py's CLOSED intents.

GOVERNANCE, and this is the part that makes it safe:
  * `converse.parse()` IS AUTHORITATIVE. It is deterministic, it is unit-tested against every wording
    he has actually used, and it runs FIRST. The speaker is consulted ONLY when converse returns
    UNKNOWN -- i.e. only for wordings nobody anticipated.
  * The speaker's interpretation is CONSTRAINED to the same closed set and is re-validated by
    converse before it can act. A model cannot invent a new intent.
  * FAILS OPEN, DOWNWARD. No speaker, a 429, a timeout, an unparseable answer -> the deterministic
    text and the deterministic parse, which is exactly today's behaviour. The speaker can only ever
    make the conversation clearer, never change what the engine is allowed to do.
  * IT NEVER SEES A CREDENTIAL. `converse.parse()` recognises a password BY SHAPE before the speaker
    is reached, so a secret is routed to the vault and the speaker is never shown it.
KIMI'S CONSTRAINTS are handled in backend/app/proxy.py, which reads the 400 body and repairs the field
the server NAMES (temperature / response_format / max_tokens). Nothing is hardcoded here.
"""
from __future__ import annotations
import json
import os
import re

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL = os.getenv("JHW_SPEAK_MODEL", "jhw-speak")        # -> kimi-k2.6
TIMEOUT = float(os.getenv("JHW_SPEAK_TIMEOUT", "25"))
ENABLED = os.getenv("JHW_SPEAK", "1").lower() not in ("0", "false", "no")

_SYS_OUT = (
    "You are the voice of an automated job-application agent, speaking to the candidate on Telegram. "
    "You are given the machine's situation. Write ONE short message he can act on.\n"
    "RULES:\n"
    "1. Plain language. No jargon, no selectors, no code, no stack traces.\n"
    "2. Say WHAT IS BLOCKING in one sentence, then list the numbered options EXACTLY as given, "
    "keeping their numbers and meaning. Do not invent, merge, reorder or drop an option.\n"
    "3. Never ask for a password unless one of the given options is to supply one.\n"
    "4. Under 90 words. No greeting, no sign-off, no emoji.\n"
    "Reply with the message text only, nothing else.")

_SYS_IN = (
    "You translate a candidate's free-text reply into ONE machine intent. "
    "The ONLY permitted answers are: CREATE, RESET, PASSWORD, SSO, SKIP, STOP, RETRY, UNKNOWN.\n"
    "CREATE = register a new account. RESET = send a password reset. PASSWORD = he is supplying an "
    "existing password. SSO = use Google/Microsoft sign-in. SKIP = abandon this employer and move to "
    "the next job. STOP = stop the whole run. RETRY = try the same thing again. "
    "UNKNOWN = you genuinely cannot tell.\n"
    "If he is refusing, complaining or swearing without giving a direction, that is UNKNOWN.\n"
    'Answer STRICT JSON only: {"intent":"<one of the above>","confidence":0.0-1.0,'
    '"restated":"<his instruction in six words>"}')


def _log(m: str) -> None:
    print(f"[speaker] {m}", flush=True)


async def _ask(system: str, user: str, log=_log) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}",
                                  "Content-Type": "application/json"},
                         json={"model": MODEL,
                               "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": user}],
                               "max_tokens": 400})
        if r.status_code >= 400:
            # never discard the body: it names what the model wants (proxy.py repairs it)
            log(f"HTTP {r.status_code}: {(r.text or '')[:160]}")
            r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


# ------------------------------------------------------------------ OUT: state -> one human message
def brief(state: dict, options: list, labels: dict) -> str:
    """The DETERMINISTIC fallback message. Used when the speaker is unavailable, and given to the
       speaker as the thing it is rephrasing -- so the OPTIONS can never drift."""
    lines = [str(state.get("problem") or "I am blocked and need a decision.").strip(), ""]
    for n, intent in enumerate(options, 1):
        lines.append(f"{n}. {labels.get(intent, intent)}")
    lines.append("")
    lines.append("Reply with the number or say it in your own words.")
    return "\n".join(lines)


async def say(state: dict, options: list, labels: dict, log=_log) -> str:
    """One human message. Falls back to `brief()` on any failure, and VERIFIES that the speaker kept
    every option -- a rephrasing that loses an option would silently remove a choice he has."""
    fallback = brief(state, options, labels)
    if not ENABLED or not options:
        return fallback
    facts = json.dumps({
        "problem": state.get("problem"),
        "employer": state.get("employer"),
        "email_in_use": state.get("email"),
        "already_tried": (state.get("tried") or [])[:6],
        "what_the_page_shows": (state.get("controls") or [])[:8],
        "how_the_three_models_voted": (state.get("votes") or [])[:4],
        "numbered_options": {str(i + 1): labels.get(o, o) for i, o in enumerate(options)},
    }, ensure_ascii=False)[:2500]
    try:
        txt = (await _ask(_SYS_OUT, facts, log)).strip()
    except Exception as e:
        log(f"unavailable ({type(e).__name__}: {str(e)[:60]}) — sending the plain message")
        return fallback
    if not txt or len(txt) > 1200:
        log("answer was empty or absurdly long — sending the plain message")
        return fallback
    # EVERY option must survive the rephrasing, or he loses a choice without knowing.
    missing = [str(i + 1) for i in range(len(options)) if f"{i + 1}" not in txt]
    if missing:
        log(f"the rephrasing dropped option(s) {missing} — sending the plain message instead")
        return fallback
    log(f"spoke in {len(txt)} chars, all {len(options)} options intact")
    return txt


# ------------------------------------------------------------------ IN: his words -> one intent
INTENTS = ("CREATE", "RESET", "PASSWORD", "SSO", "SKIP", "STOP", "RETRY", "UNKNOWN")


async def interpret(reply: str, options: list, log=_log) -> dict:
    """{'intent','confidence','restated'} — consulted ONLY when the deterministic parse said UNKNOWN.

    The answer is constrained to the closed set and is re-checked here, so the speaker cannot widen
    what the engine may do. Anything odd -> UNKNOWN, which is exactly today's behaviour."""
    if not ENABLED or not (reply or "").strip():
        return {"intent": "UNKNOWN", "confidence": 0.0, "restated": "", "why": "speaker off"}
    try:
        txt = await _ask(_SYS_IN, f"His reply: {reply[:600]}\n"
                                  f"Options offered: {', '.join(options)}", log)
    except Exception as e:
        log(f"could not interpret ({type(e).__name__}) — leaving it UNKNOWN")
        return {"intent": "UNKNOWN", "confidence": 0.0, "restated": "", "why": "speaker error"}
    m = re.search(r"\{.*\}", txt or "", re.S)
    d = {}
    if m:
        try:
            d = json.loads(m.group(0))
        except Exception:
            d = {}
    intent = str(d.get("intent") or "").strip().upper()
    if intent not in INTENTS:
        log(f"proposed {intent!r}, which is not in the closed set — treating as UNKNOWN")
        return {"intent": "UNKNOWN", "confidence": 0.0, "restated": "", "why": "outside the set"}
    try:
        conf = float(d.get("confidence") or 0.0)
    except Exception:
        conf = 0.0
    # A CREDENTIAL IS NEVER ROUTED BY A MODEL. converse.parse() catches a password by shape before we
    # get here; if the speaker claims PASSWORD anyway it has no value to give, so it is useless.
    if intent == "PASSWORD":
        log("claimed PASSWORD but carries no value — the deterministic parser owns credentials")
        return {"intent": "UNKNOWN", "confidence": 0.0, "restated": "", "why": "no value"}
    return {"intent": intent, "confidence": max(0.0, min(1.0, conf)),
            "restated": str(d.get("restated") or "")[:60], "why": "speaker"}


async def read_reply(reply: str, options: list, log=_log) -> dict:
    """THE ONE ENTRY POINT for turning his words into an intent.

    Deterministic parse FIRST (it is tested and it owns credentials); the speaker only rescues an
    UNKNOWN, and its answer must be a member of the closed set. This ordering is the whole safety
    argument: the model makes the conversation nicer, it does not decide what may happen."""
    try:
        import converse as CV
    except Exception:
        return {"intent": "UNKNOWN", "value": "", "why": "converse missing"}
    d = CV.parse(reply, options)
    if d.get("intent") != "UNKNOWN":
        return d
    log(f"the deterministic parse could not read {str(reply)[:40]!r} — asking the speaker")
    sp = await interpret(reply, options, log=log)
    # DEFENCE IN DEPTH, and my own test exposed the need for it: the closed-set check lived only
    # inside `interpret`, so `read_reply` trusted whatever it was handed. The function that ACTS must
    # validate at its own boundary, not rely on a callee's contract.
    _i = str(sp.get("intent") or "").strip()
    if _i not in INTENTS or _i == "PASSWORD":
        log(f"{_i!r} is not an actionable intent — treating as UNKNOWN")
        return {"intent": "UNKNOWN", "value": "",
                "why": f"speaker proposed {_i[:20]!r}, which is outside the closed set"}
    if sp.get("intent") == "UNKNOWN" or sp.get("confidence", 0) < 0.5:
        return {"intent": "UNKNOWN", "value": "",
                "why": f"neither parser could read it ({sp.get('why')})"}
    log(f"the speaker reads it as {sp['intent']} ({sp['confidence']:.2f}): {sp['restated']!r}")
    return {"intent": sp["intent"], "value": "",
            "why": f"speaker {sp['confidence']:.2f}: {sp['restated']}"}


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    import asyncio
    import sys
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    OPTS = ["CREATE", "RESET", "PASSWORD", "SKIP"]
    LBL = {"CREATE": "Create a new account", "RESET": "Send a password reset",
           "PASSWORD": "I will paste the password", "SKIP": "Skip this employer"}
    me = sys.modules[__name__]
    old_ask = me._ask

    print("\n[1] the deterministic parse WINS — the speaker is never asked about a known wording")
    calls = []

    async def _spy(reply, options, log=None):
        calls.append(reply)
        return {"intent": "STOP", "confidence": 1.0, "restated": "hijack", "why": "spy"}
    old = me.interpret
    me.interpret = _spy
    try:
        for t, want in (("no lets create one", "CREATE"), ("Use google sso", "SSO"),
                        ("skip it", "SKIP"), ("2", "RESET"),
                        ("Password : @Q4sD9igY!mzW5u", "PASSWORD")):
            d = asyncio.run(read_reply(t, OPTS, log=lambda m: None))
            check(f"{t[:26]!r} -> {want} deterministically", d["intent"] == want, str(d))
        check("the speaker was NOT consulted for any of them", not calls, str(calls))
    finally:
        me.interpret = old

    print("\n[2] the speaker RESCUES only what the parser cannot read")
    async def _rescue(reply, options, log=None):
        return {"intent": "CREATE", "confidence": 0.9, "restated": "make a new account",
                "why": "speaker"}
    me.interpret = _rescue
    try:
        d = asyncio.run(read_reply("ugh just do the thing already", OPTS, log=lambda m: None))
        check("an unanticipated wording is rescued", d["intent"] == "CREATE", str(d))
        check("and it says the speaker did it", "speaker" in d["why"], str(d))
    finally:
        me.interpret = old

    print("\n[3] the speaker CANNOT widen what may happen — checked at BOTH layers")
    # (a) read_reply's own boundary check, with interpret stubbed to return junk
    for bad in ("DELETE_ACCOUNT", "run shell", "", "password", "PASSWORD"):
        async def _bad(reply, options, log=None, _b=bad):
            return {"intent": _b, "confidence": 1.0, "restated": "x", "why": "speaker"}
        me.interpret = _bad
        try:
            d = asyncio.run(read_reply("mmm", OPTS, log=lambda m: None))
            check(f"read_reply refuses a proposed {bad[:16]!r}", d["intent"] == "UNKNOWN", str(d))
        finally:
            me.interpret = old
    # (b) interpret's OWN check, driven through the real model boundary (_ask stubbed)
    for bad, why in (('{"intent":"DELETE_ACCOUNT","confidence":1.0}', "outside the set"),
                     ('{"intent":"PASSWORD","confidence":1.0}', "carries no value"),
                     ("not json at all", "unparseable"),
                     ('{"confidence":1.0}', "no intent")):
        async def _raw(system, user, log=None, _b=bad):
            return _b
        me._ask = _raw
        try:
            sp = asyncio.run(interpret("mmm", OPTS, log=lambda m: None))
            check(f"interpret refuses {why}", sp["intent"] == "UNKNOWN", str(sp))
        finally:
            me._ask = old_ask

    print("\n[4] low confidence is not acted on")
    async def _unsure(reply, options, log=None):
        return {"intent": "SKIP", "confidence": 0.3, "restated": "maybe skip", "why": "speaker"}
    me.interpret = _unsure
    try:
        d = asyncio.run(read_reply("dunno", OPTS, log=lambda m: None))
        check("confidence 0.3 -> UNKNOWN", d["intent"] == "UNKNOWN", str(d))
    finally:
        me.interpret = old

    print("\n[5] the OUT direction keeps every option, or falls back")
    st = {"problem": "I cannot sign in to Accenture.", "employer": "Accenture",
          "email": "evgeny@s4biz.io", "tried": ["vault password", "create account"]}
    fb = brief(st, OPTS, LBL)
    for n in ("1.", "2.", "3.", "4."):
        check(f"the plain message numbers {n}", n in fb)
    check("the plain message states the problem", "cannot sign in" in fb)

    async def _drops(system, user, log=None):
        return "Something went wrong. Just reply 1 and I will handle it."
    me._ask = _drops
    try:
        out = asyncio.run(say(st, OPTS, LBL, log=lambda m: None))
        check("a rephrasing that DROPS options is rejected", out == fb, out[:80])
    finally:
        me._ask = old_ask

    async def _good(system, user, log=None):
        return ("I can't get into Accenture with the stored password. Pick one:\n"
                "1. Create a new account\n2. Send a password reset\n"
                "3. I will paste the password\n4. Skip this employer")
    me._ask = _good
    try:
        out = asyncio.run(say(st, OPTS, LBL, log=lambda m: None))
        check("a rephrasing that KEEPS all four is used", out != fb and "Accenture" in out, out[:60])
    finally:
        me._ask = old_ask

    async def _dead(system, user, log=None):
        raise RuntimeError("429")
    me._ask = _dead
    try:
        out = asyncio.run(say(st, OPTS, LBL, log=lambda m: None))
        check("a 429 falls back to the plain message", out == fb)
    finally:
        me._ask = old_ask

    print("\n[6] it is a FOURTH VENDOR, and it never touches the browser")
    src = open(__file__, encoding="utf-8").read()
    ship = src[:src.index("def _selftest")]
    code = "\n".join(ln for ln in ship.splitlines()
                     if not ln.strip().startswith("#") and not ln.strip().startswith("//"))
    for banned in ("page.", "click(", "evaluate(", "data-jhw", "goto("):
        check(f"no {banned!r} in the shipping code", banned not in code)
    check("it addresses the model by ROLE ALIAS, not a hardcoded slug",
          'MODEL = os.getenv("JHW_SPEAK_MODEL", "jhw-speak")' in code)
    check("converse is authoritative", "import converse as CV" in code and "CV.parse(" in code)

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} SPEAKER CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL SPEAKER CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
