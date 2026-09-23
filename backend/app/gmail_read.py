"""READING the mailbox: the same door the app already sends through, opened one notch wider.

HOW THIS WORKS, AND WHY IT IS THE RIGHT WAY FOR A GOOGLE WORKSPACE ACCOUNT.

`notify.email()` and the sign-in code already talk to the Gmail API with a SERVICE ACCOUNT that has
DOMAIN-WIDE DELEGATION, impersonating `GMAIL_SENDER`, scope `gmail.send`. That is the mechanism
Workspace provides for a server to act on a mailbox in the domain, and reading is the same mechanism
with one more scope. So:

  * NO second auth system, no OAuth consent screen, no refresh token to store and rotate, no
    "sign in with Google" round trip for a server that has no browser.
  * The ONE irreducible human step is in the Workspace admin console, because only a domain admin
    can grant it: Security -> Access and data control -> API controls -> Domain-wide delegation ->
    the existing client id -> add `https://www.googleapis.com/auth/gmail.readonly`.
  * READONLY, deliberately. `gmail.modify` would let this code label, archive and delete mail in his
    real mailbox; nothing here needs that, and a bug in a correlation engine must not be able to
    touch a message. If labelling is ever wanted it is a separate decision with its own scope.

WHY POLLING AND NOT PUSH. Gmail's watch/Pub-Sub delivers within seconds and needs a public endpoint,
a Pub/Sub topic, a subscription, and a verified push URL -- new infrastructure and new attack
surface for a product where a five-minute delay costs nothing. `users.messages.list` with a query
and a bounded window is one HTTPS call, has no moving parts, and cannot be replayed at us by anyone.
If he ever wants seconds, the push path plugs in behind the same `fetch()` seam.

EVERY NETWORK CALL GOES THROUGH ONE INJECTED `fetch`, so the suite exercises the real parsing and
the real bounds with no token and no socket.
"""
import base64
import json
import os
import re
import time

SENDER = os.environ.get("GMAIL_SENDER", "")
SA_B64 = os.environ.get("GMAIL_SA_B64", "")
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/%s"
# What we ask Gmail for. Narrow on purpose: this reads a job-hunt mailbox, not a life.
DEFAULT_QUERY = os.environ.get(
    "JHW_MAIL_QUERY",
    "-in:chats -in:drafts -from:me -category:promotions -category:social")
MAX_MESSAGES = int(os.environ.get("JHW_MAIL_MAX", "60"))
BODY_CHARS = int(os.environ.get("JHW_MAIL_BODY_CHARS", "4000"))
LOOKBACK_DAYS = int(os.environ.get("JHW_MAIL_LOOKBACK_DAYS", "14"))


def configured() -> bool:
    """Is the read side switched on? Reading is OPT-IN: `JHW_MAIL_READ=1` plus the credentials the
    send side already needs. A feature that reads somebody's mailbox does not default to on."""
    return bool(SENDER and SA_B64
                and os.environ.get("JHW_MAIL_READ", "").lower() in ("1", "true", "yes", "on"))


def status() -> dict:
    """What an operator needs to see on the console: which half is missing, in words."""
    return {
        "enabled": os.environ.get("JHW_MAIL_READ", "").lower() in ("1", "true", "yes", "on"),
        "sender": SENDER or None,
        "credentials": bool(SA_B64),
        "scope": SCOPE,
        "query": DEFAULT_QUERY,
        "note": ("" if configured() else
                 "reading is off: set JHW_MAIL_READ=1 and make sure the service account has "
                 "gmail.readonly in Workspace domain-wide delegation"),
    }


def _token():
    """A bearer token for the READ scope, impersonating the mailbox owner. Same pattern as the send
    path, one scope different -- so a token minted here can never send anything."""
    from google.auth.transport.requests import Request as GRequest
    from google.oauth2 import service_account
    info = json.loads(base64.b64decode(SA_B64))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[SCOPE], subject=SENDER)
    creds.refresh(GRequest())
    return creds.token


def _http(fetch=None):
    """The transport. Injected in tests; `requests` in production. One seam, so nothing else in
    this module knows whether a socket exists."""
    if fetch:
        return fetch

    def _real(url, headers=None, params=None, timeout=20):
        import requests
        r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
        return r.status_code, (r.json() if r.content else {})
    return _real


def _header(payload, name) -> str:
    # A PAYLOAD IS WHATEVER THE API SENT, and a test just proved it can be an int. Shapes from
    # outside are not ours to assume (defect class 35) -- coerce, never trust.
    if not isinstance(payload, dict):
        return ""
    for h in (payload.get("headers") or []):
        if not isinstance(h, dict):
            continue
        if str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value") or "")
    return ""


def _body_text(payload) -> str:
    """The plain text of a message, however the part tree is shaped. HTML is stripped rather than
    skipped: plenty of recruiters send HTML only, and dropping those would silently lose the half of
    the mailbox that matters most."""
    out, stack, seen = [], [payload if isinstance(payload, dict) else {}], 0
    while stack and seen < 40:
        p = stack.pop()
        seen += 1
        if not isinstance(p, dict):
            continue
        mime = str(p.get("mimeType") or "")
        data = ((p.get("body") or {}).get("data") or "")
        if data and mime.startswith("text/"):
            try:
                raw = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            except Exception:
                raw = ""
            if mime == "text/html":
                raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
                raw = re.sub(r"(?s)<[^>]+>", " ", raw)
                raw = re.sub(r"&nbsp;?", " ", raw)
            out.append(raw)
        stack.extend(p.get("parts") or [])
    text = re.sub(r"[ \t ]+", " ", "\n".join(out))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:BODY_CHARS]


def parse_message(d: dict) -> dict:
    """One Gmail message resource -> the flat shape mailmatch and the UI use. Never raises."""
    d = d if isinstance(d, dict) else {}
    payload = d.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    try:
        ts = int(d.get("internalDate") or 0) / 1000.0
    except Exception:
        ts = 0.0
    return {
        "id": str(d.get("id") or "")[:64],
        "thread_id": str(d.get("threadId") or "")[:64],
        "ts": ts,
        "from": _header(payload, "From")[:200],
        "to": _header(payload, "To")[:200],
        "reply_to": _header(payload, "Reply-To")[:200],
        "subject": _header(payload, "Subject")[:300],
        "snippet": str(d.get("snippet") or "")[:400],
        "body": _body_text(payload),
        "labels": [str(x)[:40] for x in (d.get("labelIds") or [])][:12],
    }


def fetch_recent(days=None, query=None, limit=None, fetch=None, token=None) -> tuple:
    """(messages, note). Reads the last `days` of the mailbox. NEVER raises: a mail fault must not
    touch the pipeline, and an empty list with a stated reason is an honest answer."""
    if not configured() and fetch is None:
        return [], (status()["note"] or "mail reading is not configured")
    days = LOOKBACK_DAYS if days is None else int(days)
    limit = MAX_MESSAGES if limit is None else max(1, min(200, int(limit)))
    q = "%s newer_than:%dd" % (query or DEFAULT_QUERY, max(1, days))
    http = _http(fetch)
    try:
        tok = token or (_token() if fetch is None else "test")
    except Exception as e:
        return [], "the mail credentials were refused: %r" % (e,)
    hdr = {"Authorization": "Bearer %s" % tok}
    base = API % (SENDER or "me")
    try:
        code, data = http(base + "/messages", headers=hdr,
                          params={"q": q, "maxResults": limit})
        if code != 200:
            return [], "Gmail list returned HTTP %s" % code
        ids = [m.get("id") for m in (data.get("messages") or []) if m.get("id")][:limit]
    except Exception as e:
        return [], "the mailbox could not be listed: %r" % (e,)
    out = []
    for mid in ids:
        try:
            code, d = http("%s/messages/%s" % (base, mid), headers=hdr,
                           params={"format": "full"})
            if code == 200:
                out.append(parse_message(d))
        except Exception:
            continue                      # one unreadable message must not lose the other 59
    return out, ("read %d of %d message(s)" % (len(out), len(ids)) if ids else "no new mail")


# --------------------------------------------------------------------------- self-test
def _selftest() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    ck("reading is OFF unless it is switched on", not configured())
    ck("and the status says which half is missing", "JHW_MAIL_READ=1" in status()["note"],
       status()["note"][:60])

    html = base64.urlsafe_b64encode(
        b"<html><style>p{}</style><body><p>Hi&nbsp;Evgeny</p>"
        b"<p>About <b>Senior Project Manager</b> at Fireblocks.</p></body></html>").decode()
    plain = base64.urlsafe_b64encode(b"plain part").decode()
    resource = {
        "id": "abc123", "threadId": "t1", "internalDate": "1790000000000",
        "snippet": "About Senior Project Manager",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": [{"name": "From", "value": "Talent <recruiter@fireblocks.com>"},
                                {"name": "Subject", "value": "Your application"},
                                {"name": "Reply-To", "value": "anna@fireblocks.com"},
                                {"name": "To", "value": "him@example.com"}],
                    "mimeType": "multipart/alternative",
                    "parts": [{"mimeType": "text/plain", "body": {"data": plain}},
                              {"mimeType": "text/html", "body": {"data": html}}]}}
    m = parse_message(resource)
    ck("the headers are read", m["from"].endswith("<recruiter@fireblocks.com>")
       and m["subject"] == "Your application" and m["reply_to"] == "anna@fireblocks.com")
    ck("the timestamp is seconds, not milliseconds", 1e9 < m["ts"] < 2e9, str(m["ts"]))
    ck("the plain part is read", "plain part" in m["body"])
    ck("an HTML-only recruiter mail is read too, tags stripped",
       "Senior Project Manager" in m["body"] and "<b>" not in m["body"], m["body"][:60])
    ck("scripts and styles do not leak into the text", "p{}" not in m["body"])
    ck("a malformed resource yields an empty shape rather than an exception",
       parse_message(None)["id"] == "" and parse_message({"payload": 7})["body"] == "")

    calls = []

    def fake(url, headers=None, params=None, timeout=20):
        calls.append((url, dict(params or {})))
        if url.endswith("/messages"):
            return 200, {"messages": [{"id": "abc123"}, {"id": "zzz"}]}
        if url.endswith("/abc123"):
            return 200, resource
        return 404, {}

    msgs, note = fetch_recent(days=7, fetch=fake, limit=10)
    ck("it lists then fetches each message", len(calls) == 3, str(len(calls)))
    ck("the query is bounded by a date window", "newer_than:7d" in calls[0][1].get("q", ""),
       calls[0][1].get("q", ""))
    ck("the default query excludes drafts, chats and promotions",
       all(x in calls[0][1]["q"] for x in ("-in:drafts", "-in:chats", "-category:promotions")))
    ck("one unreadable message does not lose the readable ones",
       len(msgs) == 1 and msgs[0]["id"] == "abc123", "%d message(s)" % len(msgs))
    ck("and the note says what was read", "read 1 of 2" in note, note)

    def boom(url, headers=None, params=None, timeout=20):
        raise RuntimeError("network down")

    msgs2, note2 = fetch_recent(fetch=boom)
    ck("a dead network returns an empty list and the reason, never an exception",
       msgs2 == [] and "could not be listed" in note2, note2)

    def refused(url, headers=None, params=None, timeout=20):
        return 403, {"error": {"message": "insufficient scope"}}

    msgs3, note3 = fetch_recent(fetch=refused)
    ck("a refused scope is reported as HTTP 403, not as an empty mailbox",
       msgs3 == [] and "403" in note3, note3)
    # STRIP COMMENTS AND DOCSTRINGS FIRST. The first version grepped the raw source and failed on
    # its own docstring, which EXPLAINS the send path -- a check matching its own prose, which this
    # estate has now done about twenty times. Measure the shipping slice only.
    import ast as _ast
    _src = open(__file__, encoding="utf-8").read()
    _tree = _ast.parse(_src)
    for _n in _ast.walk(_tree):
        if isinstance(_n, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            _d = _ast.get_docstring(_n, clean=False)
            if _d:
                _src = _src.replace(_d, " ")
    # AND CUT THE SELF-TEST OUT OF THE SLICE, because the assertion line below contains the very
    # literal it looks for -- the guard agreeing with itself, class 11. The needle is also built at
    # runtime so it cannot appear as a literal anywhere in this file.
    _lines = _src.splitlines()
    for _n in _ast.walk(_ast.parse(_src)):
        if isinstance(_n, _ast.FunctionDef) and _n.name == "_selftest":
            for _i in range(_n.lineno - 1, min(len(_lines), _n.end_lineno)):
                _lines[_i] = ""
    _code = "\n".join(re.sub(r"#.*$", "", ln) for ln in _lines)
    _needle = "gmail." + "send"
    ck("the READ scope is the only scope this module asks for",
       SCOPE.endswith("gmail.readonly") and _needle not in _code,
       "the send scope appears in the shipping slice" if _needle in _code else "")
    ck("...and the scope reaches the credential call",
       "scopes=[SCOPE]" in _code)

    print("gmail_read selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
