#!/usr/bin/env python3
"""A REAL CONVERSATION WITH CLEAR ACTIONS — not a password prompt with nowhere to go.

THE OPERATOR, verbatim: *"if we have this interaction they need to act upon it this needs to be a
real conversation with clear actions like we have on Jobhuntwow with .../electronic and not just
throw me out to terminal."*

MEASURED, why he is right. Twice tonight the engine asked an open question, he answered with a clear
INSTRUCTION, and the code typed the instruction into a password box:
    ask: "Reply with its PASSWORD"                  he said: "Use google sso"
    ask: "Does the candidate have an account?"      he said: "no lets create one"
    -> [wd 695.0s] tried your password against ['evgeny@s4biz.io', 'evgeny@s4biz.io']
An ask that can only consume a secret cannot hold a conversation. So:

  1. EVERY ask OFFERS CLOSED OPTIONS, numbered, with what each one will DO.
  2. The reply is parsed into an INTENT from a CLOSED SET, by number OR by natural wording.
  3. Each intent maps to ONE deterministic action the engine actually performs.
  4. An unparseable reply is not guessed at -- it is asked again ONCE with the options repeated.

`parse()` is PURE, so every wording he has actually used is a test case rather than a hope.
NOTHING here types a credential: a PASSWORD intent carries the value separately and the caller
hands it to the vault, never to the chat model and never to `learned.py`.
"""
from __future__ import annotations
import re

# The closed set. Adding one is a deliberate change with a test and a handler in the caller.
INTENTS = ("CREATE", "RESET", "PASSWORD", "SSO", "SKIP", "STOP", "RETRY", "UNKNOWN")

_RX = (
    # order matters: the most specific wording first
    ("STOP",     r"\b(stop|abort|cancel|quit|forget it|leave it)\b"),
    ("SKIP",     r"\b(skip|next job|move on|another job|give up on this one)\b"),
    ("RESET",    r"\b(reset|forgot|forgotten|recover|new password|change (the )?password|"
                 r"password reset|send.*reset)\b"),
    ("CREATE",   r"\b(create|register|sign ?up|make (an? )?account|new account|no account|"
                 r"don'?t have|dont have|hasn'?t got|no,? ?lets? create|create one)\b"),
    ("SSO",      r"\b(google|sso|oauth|microsoft|linkedin)\b"),
    ("RETRY",    r"\b(retry|try again|again|repeat)\b"),
)

# A credential-shaped reply. Deliberately conservative: a false positive only costs one re-ask,
# a false negative types a secret into the wrong place.
_SECRET_WORD = re.compile(r"\b(pass ?word|passwort|pwd|pin)\b\s*[:=]?\s*(\S+)", re.I)


def _looks_like_password(t: str) -> bool:
    x = (t or "").strip()
    if not x or " " in x:
        return False
    if re.match(r"^[a-z][a-z0-9+.-]*://", x, re.I) or x.lower().startswith("www."):
        return False                                   # a URL
    if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", x, re.I):
        return False                                   # an e-mail
    if re.match(r"^[\w.-]+\.[a-z]{2,24}(/\S*)?$", x, re.I):
        return False                                   # a bare host
    return (8 <= len(x) <= 120
            and bool(re.search(r"[A-Za-z]", x))
            and bool(re.search(r"\d", x) or re.search(r"[^A-Za-z0-9]", x)))


def parse(reply: str, options: list | None = None) -> dict:
    """{'intent': ..., 'value': ...}. PURE.

    Accepts a NUMBER ("2"), a keyword ("create"), or the way a human actually types
    ("no lets create one"). A password is recognised BY SHAPE or by an explicit label, and its
    value is carried separately so it can go to the vault instead of into a sentence."""
    t = re.sub(r"\s+", " ", str(reply or "")).strip()
    if not t:
        return {"intent": "UNKNOWN", "value": "", "why": "empty reply"}
    if t.upper().startswith("NO_ANSWER"):
        return {"intent": "UNKNOWN", "value": "", "why": "no answer"}

    # 1. a bare number picks one of the offered options
    m = re.match(r"^\(?(\d{1,2})[).\s]*$", t)
    if m and options:
        n = int(m.group(1))
        if 1 <= n <= len(options):
            return {"intent": options[n - 1], "value": "", "why": f"chose option {n}"}

    # 2. an explicitly labelled password -- take the VALUE, not the sentence
    m = _SECRET_WORD.search(t)
    if m and _looks_like_password(m.group(2)):
        return {"intent": "PASSWORD", "value": m.group(2), "why": "labelled password"}

    # 3. natural wording, most specific first
    for intent, rx in _RX:
        if re.search(rx, t, re.I):
            return {"intent": intent, "value": "", "why": f"matched {intent.lower()} wording"}

    # 4. a bare high-entropy token is a password
    if _looks_like_password(t):
        return {"intent": "PASSWORD", "value": t, "why": "credential-shaped"}

    return {"intent": "UNKNOWN", "value": "", "why": f"could not read {t[:40]!r}"}


def ask_text(question: str, options: list, labels: dict | None = None) -> str:
    """The message he actually receives: the question, then numbered choices that say what each
    one WILL DO. No open-ended 'reply with the password' -- that is what produced two dead runs."""
    labels = labels or DEFAULT_LABELS
    lines = [question.strip(), ""]
    for n, intent in enumerate(options, 1):
        lines.append(f"{n}. {labels.get(intent, intent)}")
    lines.append("")
    lines.append("Reply with the number, or just say it in your own words. "
                 "You can also paste a password directly.")
    return "\n".join(lines)


DEFAULT_LABELS = {
    "CREATE":   "Create a new account with evgeny@s4biz.io and the vault password "
                "(I read the verification e-mail myself)",
    "RESET":    "Send a password reset, then read the e-mail and set a new one",
    "PASSWORD": "I will paste the existing password",
    "SSO":      "Use Google sign-in (needs a live session in the sandbox profile)",
    "SKIP":     "Skip this employer and carry on with the next job",
    "STOP":     "Stop the run",
    "RETRY":    "Try the same thing again",
}


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    OPTS = ["CREATE", "RESET", "PASSWORD", "SKIP"]

    print("\n[1] THE TWO REPLIES HE ACTUALLY SENT, which the old code typed into a password box")
    r = parse("no lets create one", OPTS)
    check("'no lets create one' -> CREATE", r["intent"] == "CREATE", str(r))
    r = parse("Use google sso", OPTS)
    check("'Use google sso' -> SSO", r["intent"] == "SSO", str(r))
    for t in ("no lets create one", "Use google sso"):
        check(f"{t!r} is NOT read as a password", parse(t, OPTS)["intent"] != "PASSWORD")

    print("\n[2] numbers pick an option")
    for n, want in ((1, "CREATE"), (2, "RESET"), (4, "SKIP")):
        check(f"'{n}' -> {want}", parse(str(n), OPTS)["intent"] == want, str(parse(str(n), OPTS)))
    check("'9' is out of range, not option 9", parse("9", OPTS)["intent"] == "UNKNOWN")

    print("\n[3] natural wording, the way a human types it")
    for t, want in (("create an account", "CREATE"), ("register please", "CREATE"),
                    ("I don't have an account", "CREATE"), ("sign up", "CREATE"),
                    ("reset the password", "RESET"), ("forgot password", "RESET"),
                    ("send a reset link", "RESET"),
                    ("skip this one", "SKIP"), ("move on to the next job", "SKIP"),
                    ("stop", "STOP"), ("abort", "STOP"),
                    ("try again", "RETRY")):
        got = parse(t, OPTS)["intent"]
        check(f"{t!r} -> {want}", got == want, f"got {got}")

    print("\n[4] a password is taken as a VALUE, never as prose")
    r = parse("Password : @Q4sD9igY!mzW5u", OPTS)
    check("a labelled password yields the value only",
          r["intent"] == "PASSWORD" and r["value"] == "@Q4sD9igY!mzW5u", str(r))
    r = parse("@Q4sD9igY!mzW5u", OPTS)
    check("a bare credential is recognised", r["intent"] == "PASSWORD", str(r))
    check("and the sentence is NOT stored as the value", "Password" not in r["value"])

    print("\n[5] the things that must NOT be mistaken for a credential")
    for t in ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers",
              "evgeny@s4biz.io", "jobhuntwow.com", "yes", "ok", "8"):
        check(f"{t[:38]!r} is not a password", parse(t, OPTS)["intent"] != "PASSWORD",
              str(parse(t, OPTS)))

    print("\n[6] an unreadable reply is admitted, not guessed")
    r = parse("hmmmm what", OPTS)
    check("UNKNOWN with a reason", r["intent"] == "UNKNOWN" and r["why"], str(r))
    check("empty is UNKNOWN", parse("", OPTS)["intent"] == "UNKNOWN")
    check("NO_ANSWER (timeout) is UNKNOWN", parse("NO_ANSWER", OPTS)["intent"] == "UNKNOWN")

    print("\n[7] the message he receives names the ACTION, not just the option")
    msg = ask_text("I cannot sign in to Accenture. How do you want me to get in?", OPTS)
    check("the question is first", msg.startswith("I cannot sign in"))
    for n in ("1.", "2.", "3.", "4."):
        check(f"option {n} is numbered", n in msg)
    check("CREATE says it reads the e-mail itself", "verification e-mail" in msg)
    check("it invites plain words too", "your own words" in msg)
    check("every offered intent is described", all(
        DEFAULT_LABELS[o][:18] in msg for o in OPTS))

    print("\n[8] intents are a CLOSED set")
    for t in ("no lets create one", "reset it", "skip", "stop", "@Q4sD9igY!mzW5u", "gibberish"):
        check(f"{t[:26]!r} maps inside the closed set", parse(t, OPTS)["intent"] in INTENTS)

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} CONVERSATION CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL CONVERSATION CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
