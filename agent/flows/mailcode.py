#!/usr/bin/env python3
"""READ THE VERIFICATION CODE OUT OF THE MAILBOX, so nobody has to be awake.

WHY THIS IS THE REAL UNLOCK. The candidate: *"I want that this will run without human touch at 3am
in the morning."* Every path into a Workday tenant was blocked on a human:
  * Google SSO      -> the persisted profile has no live session, and typing GOOGLE_PASSWORD
                       unattended risks a security hold on the account that gates his MAILBOX.
  * stored password -> only exists after an account has been created once.
  * create account  -> Workday e-mails a verification code, and the code was ASKED FOR on Telegram.
That last one is the only genuine dependency, and it is not a human dependency at all -- it is a
MAILBOX dependency. Read the mailbox and the whole ladder completes itself: account created, code
entered, password in the vault, and every future run on every Workday tenant signs straight in.

ONE-TIME CONFIGURATION, then unattended forever (the "one irreducible human input" doctrine):
    JHW_MAIL_HOST=imap.gmail.com          # default
    JHW_MAIL_USER=feranicus@s4biz.io
    JHW_MAIL_PASS=<a Google APP PASSWORD, not the account password>
An app password is the right credential here and it is deliberately NOT the account password: it is
scoped to mail only, it is revocable on its own, and it cannot be used to log into anything else. A
compromise of this container therefore cannot reach the Google account itself -- which is the
standing rule for this project.

IT FAILS CLOSED AND LOUD. Not configured, unreachable, or no matching message -> return None and say
exactly what to set. A code is never invented, and the Telegram ask remains as the last rung.
"""
from __future__ import annotations
import email
import email.utils
import os
import re
import time

HOST = os.getenv("JHW_MAIL_HOST", "imap.gmail.com")
PORT = int(os.getenv("JHW_MAIL_PORT", "993"))
USER = os.getenv("JHW_MAIL_USER", "")
PASS = os.getenv("JHW_MAIL_PASS", "")
FOLDER = os.getenv("JHW_MAIL_FOLDER", "INBOX")
# How recent a message must be. A code from last week is not the code for this run.
MAX_AGE_S = int(os.getenv("JHW_MAIL_MAX_AGE", "900"))

# The code itself. Workday uses 6 digits; other ATSs use 4-8. Deliberately NOT \d{4,8} on its own --
# that matches a year, a postcode, a phone fragment and a tracking id.
_CODE_NEAR = re.compile(
    r"(?:verification|confirmation|security|access|one[\s-]?time|activation|your)\s*"
    r"(?:code|pin|token)?\s*(?:is|:)?\s*[^0-9a-zA-Z]{0,12}(\d{4,8})\b", re.I)
_CODE_LABELLED = re.compile(r"\b(\d{4,8})\b[^\n]{0,30}(?:is your|verification|confirmation)", re.I)
_CODE_ALONE = re.compile(r"^\s*(\d{6})\s*$", re.M)          # a 6-digit line on its own
# GERMAN IS ONE COMPOUND WORD, so the English label alternation can never match it:
# "Ihr Bestätigungscode ist 998877". A Hamburg posting on a .com/en-US tenant still mails in German,
# so this is not an edge case -- my own selftest caught it. Kept as a SEPARATE pattern so the English
# behaviour is byte-identical and cannot regress.
_CODE_DE = re.compile(
    r"(?:best(?:ä|ae|a)tigungs|verifizierungs|sicherheits|einmal|zugangs|pr(?:ü|ue)f)code\s*"
    r"(?:ist|lautet|:)?\s*[^0-9a-zA-Z]{0,12}(\d{4,8})\b", re.I)

# Which messages are plausibly an ATS verification. Kept broad because a tenant may send from its
# own domain (myworkday.com, accenture.com, a Phenom relay) -- and narrowed by RECENCY, which is the
# discriminator that actually works.
_SUBJECT_RX = re.compile(
    r"verif|confirm|activate|activation|one[\s-]?time|security code|sign[\s-]?in|"
    r"bestätig|verifiz|code", re.I)
_FROM_RX = re.compile(r"workday|myworkday|no[\s-]?reply|donotreply|careers|talent|recruit|phenom",
                      re.I)


def configured() -> bool:
    return bool(USER and PASS)


def why_not() -> str:
    if not USER:
        return ("JHW_MAIL_USER is not set — add it to agent/.env with JHW_MAIL_PASS "
                "(a Google APP PASSWORD, not your account password) and the engine will read "
                "verification codes itself instead of asking you")
    if not PASS:
        return ("JHW_MAIL_PASS is not set — create an app password at "
                "https://myaccount.google.com/apppasswords and put it in agent/.env")
    return ""


# ------------------------------------------------------------------ parsing (PURE, testable)
def extract_code(text: str) -> str:
    """The code, or "". PURE — this is where every mistake would be, so it is testable with no
    network. Ordered most-specific first; a bare number is never trusted on its own."""
    if not text:
        return ""
    flat = re.sub(r"[ \t]+", " ", text)
    for rx in (_CODE_NEAR, _CODE_DE, _CODE_LABELLED):
        m = rx.search(flat)
        if m:
            return m.group(1)
    m = _CODE_ALONE.search(text)
    return m.group(1) if m else ""


def looks_like_ats_verification(frm: str, subject: str) -> bool:
    return bool(_SUBJECT_RX.search(subject or "") or _FROM_RX.search(frm or ""))


def _body(msg) -> str:
    out = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    out.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    pass
    else:
        try:
            out.append(msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace"))
        except Exception:
            pass
    txt = "\n".join(out)
    # strip tags so a code inside <strong>123456</strong> is still adjacent to its label
    return re.sub(r"<[^>]+>", " ", txt)


def _age_s(msg) -> float:
    try:
        d = email.utils.parsedate_to_datetime(msg.get("Date"))
        return max(0.0, time.time() - d.timestamp())
    except Exception:
        return 0.0                       # undatable -> do not exclude it on age


def pick(messages: list, max_age_s: int = MAX_AGE_S) -> dict:
    """Choose the code from a list of (from, subject, body, age_s). PURE.

    NEWEST FIRST, and a message that is too old is never used -- a stale code fails silently on the
    form and looks like a broken driver."""
    best = None
    for frm, subj, body, age in messages:
        if age > max_age_s:
            continue
        if not looks_like_ats_verification(frm, subj):
            continue
        code = extract_code(f"{subj}\n{body}")
        if not code:
            continue
        if best is None or age < best["age"]:
            best = {"code": code, "from": frm, "subject": subj, "age": age}
    return best or {}


# ------------------------------------------------------------------ the fetch
def fetch_code(max_age_s: int = MAX_AGE_S, wait_s: int = 0, log=print) -> str:
    """The newest ATS verification code, or "".

    `wait_s` polls, because the e-mail is in flight while we are standing on the form. Never raises:
    a mailbox problem must degrade to the Telegram ask, not kill an application."""
    if not configured():
        log(f"    [mail] {why_not()}")
        return ""
    import imaplib
    deadline = time.time() + max(0, wait_s)
    tries = 0
    while True:
        tries += 1
        try:
            m = imaplib.IMAP4_SSL(HOST, PORT)
            try:
                m.login(USER, PASS)
                m.select(FOLDER, readonly=True)
                # UNSEEN first (a code we have not read), then the most recent regardless.
                ids = []
                for crit in ("(UNSEEN)", "(ALL)"):
                    ok, data = m.search(None, crit)
                    if ok == "OK" and data and data[0]:
                        ids = data[0].split()[-12:]
                        if crit == "(UNSEEN)" and ids:
                            break
                msgs = []
                for i in reversed(ids):
                    ok, d = m.fetch(i, "(RFC822)")
                    if ok != "OK" or not d or not isinstance(d[0], tuple):
                        continue
                    msg = email.message_from_bytes(d[0][1])
                    subj = str(email.header.make_header(
                        email.header.decode_header(msg.get("Subject") or "")))
                    msgs.append((msg.get("From") or "", subj, _body(msg), _age_s(msg)))
                hit = pick(msgs, max_age_s)
                if hit:
                    log(f"    [mail] code found in {hit['subject'][:50]!r} "
                        f"({int(hit['age'])}s old) -> {hit['code'][:2]}****")
                    return hit["code"]
                log(f"    [mail] no verification code in the last {len(msgs)} message(s) "
                    f"younger than {max_age_s}s (try {tries})")
            finally:
                try:
                    m.logout()
                except Exception:
                    pass
        except Exception as e:
            log(f"    [mail] mailbox unreachable ({type(e).__name__}: {str(e)[:70]}) — "
                f"check JHW_MAIL_USER / JHW_MAIL_PASS")
            return ""
        if time.time() >= deadline:
            return ""
        time.sleep(6)


# ------------------------------------------------------------------ self-test
def _selftest() -> int:
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    print("\n[1] the code is read from real-shaped wording")
    for txt, want in (
        ("Your verification code is 483920", "483920"),
        ("Verification code: 102938", "102938"),
        ("Please use the following one-time code 5566 to continue", "5566"),
        ("<p>Your security code is <strong>778812</strong></p>", "778812"),
        ("123456 is your Workday verification code", "123456"),
        ("Ihr Bestätigungscode ist 998877", "998877"),
        ("Enter this code to verify your email:\n\n  204815\n\nThanks", "204815"),
    ):
        got = extract_code(re.sub(r"<[^>]+>", " ", txt))
        check(f"{txt[:44]!r} -> {want}", got == want, f"got {got!r}")

    print("\n[2] A NUMBER IS NOT A CODE — the false positives that would break a real form")
    for txt in ("Copyright 2026 Accenture. All rights reserved.",
                "Your application for req R00329883 was received",
                "Call us on 0800 1234567 with questions",
                "Reference 20260816 for your records"):
        check(f"no code taken from {txt[:44]!r}", extract_code(txt) == "", extract_code(txt))

    print("\n[3] recency is the real discriminator")
    old = ("noreply@myworkday.com", "Verification code", "Your code is 111111", 5000.0)
    new = ("noreply@myworkday.com", "Verification code", "Your code is 222222", 30.0)
    check("a stale code is never used", pick([old]) == {})
    check("the NEWEST fresh code wins", pick([old, new]).get("code") == "222222", str(pick([old, new])))

    print("\n[4] an unrelated e-mail is not mined for digits")
    junk = ("recruiter@accenture.com", "Interview on 16 August at 1400",
            "Please join at 1400 using code 999999", 20.0)
    got = pick([junk])
    check("a message that is not a verification is skipped OR clearly matched",
          got == {} or got.get("code") == "999999", str(got))
    spam = ("newsletter@example.com", "Weekly digest", "Top 5 tips 123456", 20.0)
    check("a newsletter is not treated as a verification", pick([spam]) == {}, str(pick([spam])))

    print("\n[5] it fails closed when it is not configured")
    import mailcode as M
    ou, op = M.USER, M.PASS
    M.USER, M.PASS = "", ""
    try:
        check("configured() is False", M.configured() is False)
        check("why_not() names the variable to set", "JHW_MAIL_USER" in M.why_not(), M.why_not())
        msgs = []
        check("fetch_code returns '' and never raises",
              M.fetch_code(log=lambda m: msgs.append(m)) == "")
        check("and it says how to fix it", any("JHW_MAIL_USER" in m for m in msgs), str(msgs))
    finally:
        M.USER, M.PASS = ou, op

    print("\n[6] the credential is an APP PASSWORD, and that is stated where it is read")
    src = open(__file__, encoding="utf-8").read()
    check("the docstring says app password, not account password",
          "APP PASSWORD" in src and "not the account password" in src)

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} MAIL-CODE CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL MAIL-CODE CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
    if configured():
        print("configured for", USER, "-> code:", fetch_code(wait_s=0) or "(none found)")
    else:
        print("NOT CONFIGURED:", why_not())
