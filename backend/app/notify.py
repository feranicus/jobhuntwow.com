"""
notify.py — one place that can reach a human: Telegram + email.

Reuses what already works on this droplet:
  * Gmail API over HTTPS (service account + domain-wide delegation) — the droplet BLOCKS SMTP ports,
    which is why colt_auth sends OTPs this way. Same mechanism, same credentials.
  * The Telegram bot token the assess-bot already has (jhw-web loads assess-bot/.env).

Never raises: an alert that crashes the request it is warning about is worse than no alert.
"""
import json, os, time, urllib.parse, urllib.request

TG_TOKEN   = os.environ.get("BOT_TOKEN", "")
ALERT_CHAT = os.environ.get("ALERT_TG_CHAT", "")          # numeric chat id; auto-discovered if empty
# Comma-separated list -> both the S4BIZ and the Colt addresses get every alert + daily report.
ALERT_MAIL = os.environ.get("ALERT_EMAIL", "feranicus@s4biz.io")
ALERT_MAILS = [e.strip() for e in ALERT_MAIL.split(",") if e.strip()]
GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "")
GMAIL_SA_B64 = os.environ.get("GMAIL_SA_B64", "")
AUTH_STORE = os.environ.get("AUTH_STORE", "/data/web_authorized.json")


def _log(**k):
    k.setdefault("ts", time.time()); k.setdefault("service", os.environ.get("SERVICE", "jhw-web"))
    k.setdefault("bot", "webapp")
    line = json.dumps(k)
    try: print(line, flush=True)
    except Exception: pass
    try:
        p = os.environ.get("EVENTS_LOG", "")
        if p:
            with open(p, "a") as fh: fh.write(line + "\n")
    except Exception: pass


def _tg_chats():
    """Explicit ALERT_TG_CHAT wins. Otherwise alert every authenticated operator we know about —
    better to over-notify the owner than to have a silent security alert."""
    if ALERT_CHAT:
        return [c.strip() for c in ALERT_CHAT.split(",") if c.strip()]
    out = []
    for p in (AUTH_STORE, "/var/log/colt/authorized.json"):
        try:
            for uid in (json.load(open(p)) or {}):
                if str(uid).lstrip("-").isdigit() and str(uid) not in out:
                    out.append(str(uid))
        except Exception:
            pass
    return out


def telegram(text, markdown=False):
    """Send to every configured chat. True when at least one delivery succeeded.

    PLAIN TEXT BY DEFAULT, and that is the fix for a whole class of silent failures. Every message
    we send carries attacker- or employer-controlled strings: a probed path (`/wp-admin/_x`), a job
    title, a company name. With `parse_mode=Markdown` a single stray `_` or `*` makes Telegram
    reject the ENTIRE message as malformed entities — so the alert that matters most is exactly the
    one that never arrives. Nothing we send depends on bold, so the formatting buys nothing and
    costs the message. `markdown=True` remains for a caller that formats its own text and knows it
    is safe."""
    if not TG_TOKEN:
        return False
    ok = False
    for chat in _tg_chats():
        try:
            payload = {"chat_id": chat, "text": text[:3900],
                       "disable_web_page_preview": "true"}
            if markdown:
                payload["parse_mode"] = "Markdown"
            data = urllib.parse.urlencode(payload).encode()
            req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data)
            with urllib.request.urlopen(req, timeout=12) as r:
                ok = (r.status == 200) or ok
        except Exception as e:
            _log(evt="alert_delivery", channel="telegram", chat=str(chat), result="error", err=repr(e)[:160])
    return ok


def email(subject, body, to=None):
    """Gmail API (HTTPS). SMTP is blocked outbound on this droplet — do not 'fix' this to SMTP.
    `to` may be a comma list or a python list; every recipient gets it."""
    if to is None:
        to = ALERT_MAILS
    if isinstance(to, str):
        to = [x.strip() for x in to.split(",") if x.strip()]
    to = ", ".join(to)
    if not (GMAIL_SENDER and GMAIL_SA_B64):
        _log(evt="alert_delivery", channel="email", result="skipped", err="GMAIL_SENDER/GMAIL_SA_B64 not set")
        return False
    try:
        import base64
        from email.message import EmailMessage
        import requests
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GRequest
        msg = EmailMessage()
        msg["Subject"] = subject[:200]; msg["From"] = GMAIL_SENDER; msg["To"] = to
        msg.set_content(body)
        info = json.loads(base64.b64decode(GMAIL_SA_B64))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/gmail.send"], subject=GMAIL_SENDER)
        creds.refresh(GRequest())
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = requests.post("https://gmail.googleapis.com/gmail/v1/users/%s/messages/send" % GMAIL_SENDER,
                          headers={"Authorization": "Bearer " + creds.token,
                                   "Content-Type": "application/json"},
                          json={"raw": raw}, timeout=20)
        if r.status_code in (200, 202):
            return True
        _log(evt="alert_delivery", channel="email", result="error", status=r.status_code, err=r.text[:200])
    except Exception as e:
        _log(evt="alert_delivery", channel="email", result="error", err=repr(e)[:200])
    return False


def fire_and_forget(fn, *a, **k):
    """Send on a daemon thread and return immediately. Returns True if the send was STARTED.

    WHY THIS EXISTS. `telegram()` and `email()` are blocking HTTPS calls of a few hundred
    milliseconds. The visit feed runs inside the response path of an ordinary page view, so calling
    them directly would put a third party's latency -- and its outages -- in front of the person
    opening the site. An optional notification may make a page more informed; it may never make it
    slower than its own budget, and a nice-to-have that hangs is its own outage.
    """
    import threading
    try:
        threading.Thread(target=lambda: _quiet(fn, *a, **k), daemon=True).start()
        return True
    except Exception:
        return False


def _quiet(fn, *a, **k):
    try:
        fn(*a, **k)
    except Exception as e:
        _log(evt="notify_async_failed", fn=getattr(fn, "__name__", "?"), err=repr(e)[:160])


def both(subject, body):
    """Fire both channels. Independent: email failing must not silence Telegram."""
    # NO ASTERISKS. telegram() has defaulted to PLAIN TEXT since 2026-09 (see its docstring), so
    # these only ever rendered as literal `*` characters around the subject. Re-adding
    # markdown=True to make them bold would re-open the HTTP 400 that fix exists to prevent.
    t = telegram("🚨 %s\n\n%s" % (subject, body))
    e = email("[jobhuntwow.com] " + subject, body)
    _log(evt="alert_delivery", channel="both", telegram=bool(t), email=bool(e), subject=subject[:120])
    return t or e
