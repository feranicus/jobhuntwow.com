#!/usr/bin/env python3
"""DIGEST — "what went out today", by e-mail, and a weekly one when nothing went out.

HIS REQUEST (2026-09-18): "this digest needs to be sent to me end of each day that we actively sent
resumes and if we don't sent resumes once a week."

So the cadence is a FUNCTION OF ACTIVITY, not a fixed daily mail:
  * a day on which at least one application was SENT  -> the daily digest, that evening;
  * a quiet stretch                                   -> ONE weekly digest saying so, and nothing
                                                         in between.
`due()` is PURE, which is why every one of those cases is a test rather than a hope, and why a
restart cannot double-send: the decision is recomputed from what is already recorded, never from a
timer's memory.

WHAT IT READS: tracker.py, the same rows the Tailor page writes and the apply engine updates. The
digest therefore reports the CORRELATION -- which resume went to which posting -- and it NAMES a row
that cannot answer that question instead of counting it as a success.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from . import notify, tracker
except Exception:                                   # pragma: no cover - standalone --logic run
    import notify, tracker                          # type: ignore

DAY = 86400
WEEK = 7 * DAY
# Local evening by default: 19:00 UTC is 20:00 Berlin in winter, 21:00 in summer. One knob.
DIGEST_HOUR = int(os.getenv("JHW_DIGEST_HOUR", "19"))
ENABLED = os.getenv("JHW_DIGEST", "1").strip().lower() not in ("0", "false", "no")
TO = os.getenv("DIGEST_EMAIL", "") or os.getenv("ALERT_EMAIL", "feranicus@s4biz.io")


def day_bounds(ts: float) -> tuple:
    """The UTC day containing ts. One definition, used by the decision AND by the window it reads."""
    d = datetime.fromtimestamp(ts, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def due(now: float, sent_today: int, last_daily_ts: int, last_weekly_ts: int) -> str:
    """WHICH digest is owed right now: 'daily', 'weekly', or '' (nothing).

    PURE. The caller supplies the facts; this decides. A daily digest is owed at most once per day
    and only when something actually went out; a weekly one is owed only after a full week with no
    digest of either kind -- so a quiet fortnight produces two mails, not fourteen and not zero."""
    start, _ = day_bounds(now)
    if sent_today > 0:
        return "daily" if int(last_daily_ts or 0) < start else ""
    last_any = max(int(last_daily_ts or 0), int(last_weekly_ts or 0))
    if now - last_any >= WEEK:
        return "weekly"
    return ""


def _jd_ref(r: dict) -> str:
    """How this row identifies the posting: the link if we have one, otherwise the pasted text."""
    u = (r.get("jd_url") or "").strip()
    if u:
        return u
    n = int(r.get("jd_chars") or 0)
    sha = (r.get("jd_sha") or "")[:14]
    return f"pasted job description ({n} chars, {sha})" if n else "NO JOB DESCRIPTION ON RECORD"


def render(sent: list, tailored: list, window: str, since_ts: int, now: float) -> tuple:
    """(subject, body). Deterministic text: no model writes this, so it cannot invent an employer."""
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    n = len(sent)
    if window == "weekly" and n == 0:
        subject = "JobHuntWOW — no applications went out this week"
    else:
        subject = "JobHuntWOW — %d application%s sent %s" % (n, "" if n == 1 else "s",
                                                             "today" if window == "daily" else
                                                             "in the last 7 days")
    L = [subject, "=" * len(subject), ""]
    if n:
        L.append("SENT — job description -> the documents we sent")
        for r in sent:
            when = datetime.fromtimestamp(r.get("sent_ts") or now, timezone.utc).strftime("%H:%M UTC")
            L.append("")
            L.append("  %s — %s" % (r.get("employer") or "(employer not recorded)",
                                    r.get("title") or "(role not recorded)"))
            L.append("    job        : %s" % _jd_ref(r))
            L.append("    documents  : %s" % (", ".join(r.get("files") or []) or "NONE ON RECORD"))
            L.append("    resume sent: %s" % (r.get("resume_file") or "NOT RECORDED"))
            L.append("    stage      : %s via %s at %s" % (r.get("stage") or "?",
                                                           r.get("ats") or "portal", when))
            if not r.get("correlated"):
                # A row that cannot say what went where is REPORTED, never quietly counted.
                L.append("    ** INCOMPLETE: this row cannot prove which resume went to which "
                         "posting — open it in the Pipeline **")
            L.append("    id         : %s" % r.get("job_id"))
    else:
        L.append("Nothing was sent in this window.")
        L.append("")
        L.append("That is the whole point of this message: a week with no applications is a fact "
                 "you should hear, not silence.")
    if tailored:
        L.append("")
        L.append("TAILORED BUT NOT YET SENT (%d):" % len(tailored))
        for r in tailored[:20]:
            L.append("  %s — %s   [%s]" % (r.get("employer") or "?", r.get("title") or "?",
                                           r.get("job_id")))
    bad = [r for r in sent if not r.get("correlated")]
    L += ["", "-" * 60,
          "window : %s -> %s" % (datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                                 datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M")),
          "sent   : %d    tailored-not-sent: %d    incomplete records: %d" % (n, len(tailored), len(bad)),
          "source : the application tracker (the same rows the Pipeline and the CRM read)",
          "day    : %s" % day]
    return subject, "\n".join(L)


def build(window: str = "daily", now: float = 0.0) -> tuple:
    now = float(now or time.time())
    since = day_bounds(now)[0] if window == "daily" else int(now - WEEK)
    sent = tracker.rows(since_ts=since, until_ts=int(now) + 1, sent_only=True)
    pend = [r for r in tracker.rows(since_ts=int(now - WEEK), until_ts=int(now) + 1)
            if not r.get("sent_ts")]
    subject, body = render(sent, pend, window, since, now)
    return subject, body, len(sent)


def run_once(now: float = 0.0, force: str = "") -> dict:
    """Decide, render, send, record. Returns what it did, so a verb can PRINT it."""
    now = float(now or time.time())
    start, _ = day_bounds(now)
    sent_today = len(tracker.rows(since_ts=start, until_ts=int(now) + 1, sent_only=True))
    last_d = tracker.digest_last("daily").get("last_ts", 0)
    last_w = tracker.digest_last("weekly").get("last_ts", 0)
    if not force and not sent_today and not (last_d or last_w) and not tracker.rows(0, limit=1):
        # A BRAND-NEW STORE HAS NO QUIET WEEK TO REPORT. Without this, the first evening after a
        # deploy mails "nothing went out this week" about a week that never existed. Record the
        # baseline instead, so the weekly clock starts now and means something when it fires.
        tracker.digest_mark("weekly", int(now), 0)
        return {"sent": False, "kind": "", "why": "first run — weekly clock started, nothing to report"}
    kind = force or due(now, sent_today, last_d, last_w)
    if not kind:
        return {"sent": False, "kind": "", "why": "nothing due (%d sent today)" % sent_today}
    subject, body, n = build(kind, now)
    ok = notify.email(subject, body, to=TO)
    if ok:
        tracker.digest_mark(kind, int(now), n)
    notify._log(evt="digest", kind=kind, count=n, result="sent" if ok else "error", to=TO)
    return {"sent": bool(ok), "kind": kind, "count": n, "subject": subject, "body": body}


async def scheduler():
    """Wake at DIGEST_HOUR and ask run_once. The decision is in the data, so a restart is harmless."""
    if not ENABLED:
        return
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=DIGEST_HOUR, minute=5, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep(max(60, (nxt - now).total_seconds()))
        try:
            run_once()
        except Exception as e:
            notify._log(evt="digest", result="error", err=repr(e)[:180])


# --------------------------------------------------------------------------- contracts
def _selftest() -> int:
    import tempfile
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("[digest] contracts")
    t0 = 1_787_000_000.0                       # a fixed instant, so nothing here depends on today
    start = day_bounds(t0)[0]

    ck(due(t0, 3, 0, 0) == "daily", "a day we actively sent resumes gets the digest")
    ck(due(t0, 3, start + 10, 0) == "", "and only ONCE — a restart cannot double-send it")
    ck(due(t0, 3, start - 10, 0) == "daily", "yesterday's digest does not satisfy today")
    ck(due(t0, 0, start - 10, 0) == "", "a quiet day is NOT mailed")
    ck(due(t0, 0, t0 - WEEK - 1, 0) == "weekly", "a whole week with nothing sent IS mailed")
    ck(due(t0, 0, 0, t0 - WEEK + 100) == "", "...and the weekly one is not repeated daily after that")
    ck(due(t0, 0, t0 - 2 * DAY, 0) == "", "two quiet days is not yet a week")
    ck(due(t0, 1, 0, t0 - 10) == "daily", "a send always wins over the weekly clock")

    # RENDER: the correlation must be legible, and a broken row must be impossible to miss.
    row = {"employer": "Acme", "title": "Director", "jd_url": "https://acme.com/jobs/7",
           "files": ["resume.pdf", "cover_letter.pdf"], "resume_file": "resume.pdf",
           "sent_ts": t0, "stage": "submitted", "ats": "ashby", "correlated": True,
           "job_id": "20260918-acme"}
    s, b = render([row], [], "daily", start, t0)
    ck("https://acme.com/jobs/7" in b and "resume.pdf" in b,
       "the digest says WHICH posting and WHICH resume")
    ck("Acme" in b and "Director" in b, "the employer and the role are named")
    ck("1 application sent today" in s, "the subject line is the answer")
    pasted = dict(row, jd_url="", jd_chars=1840, jd_sha="t:abc123def4567", correlated=True)
    s2, b2 = render([pasted], [], "daily", start, t0)
    ck("pasted job description (1840 chars" in b2,
       "a posting he PASTED is identified too — that was the half with no record at all")
    broken = dict(row, correlated=False)
    _, b3 = render([broken], [], "daily", start, t0)
    ck("INCOMPLETE" in b3, "a row that cannot prove the correlation is NAMED, never counted as fine")
    s4, b4 = render([], [], "weekly", int(t0 - WEEK), t0)
    ck("no applications went out this week" in s4 and "Nothing was sent" in b4,
       "the weekly message states the quiet week plainly")
    _, b5 = render([row], [{"employer": "Beta", "title": "VP", "job_id": "x"}], "daily", start, t0)
    ck("TAILORED BUT NOT YET SENT (1)" in b5 and "Beta" in b5,
       "documents built and not sent are surfaced, so nothing is silently dropped")

    # THE MAIL IS NEVER WRITTEN BY A MODEL.
    import inspect
    src = inspect.getsource(render) + inspect.getsource(build)
    ck(not any(w in src for w in ("llm", "_ask(", "qwen", "openai")),
       "the digest is deterministic text — a model can never invent an employer into it")

    # run_once must not mark a digest as sent when the mail did not go.
    tmp = tempfile.mkdtemp(prefix="jhwdig")
    os.environ["JHW_TRACKER_DB"] = os.path.join(tmp, "t.sqlite")
    tracker.init()
    tracker.record_sent("j1", url="https://acme.com/jobs/7", employer="Acme", status="submitted")
    real = notify.email
    try:
        notify.email = lambda *a, **k: False                     # the mailer is down
        r = run_once()
        ck(r["kind"] == "daily" and r["sent"] is False, "a failed send is reported as a failure")
        ck(tracker.digest_last("daily").get("last_ts", 0) == 0,
           "...and is NOT recorded as sent, so the next run tries again")
        notify.email = lambda *a, **k: True
        r2 = run_once()
        ck(r2["sent"] is True and tracker.digest_last("daily").get("last_ts", 0) > 0,
           "a successful send is recorded once")
        ck(run_once()["sent"] is False, "and the same day does not send a second time")
    finally:
        notify.email = real

    # A BRAND NEW INSTALL MUST NOT MAIL ABOUT A WEEK THAT NEVER HAPPENED.
    os.environ["JHW_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(prefix="jhwfresh"), "t.sqlite")
    tracker.init()
    real2 = notify.email
    _mails = []
    try:
        notify.email = lambda *a, **k: (_mails.append(a[0]), True)[1]
        r0 = run_once()
        ck(r0["sent"] is False and not _mails, "the first ever run sends nothing")
        ck(tracker.digest_last("weekly").get("last_ts", 0) > 0,
           "...it starts the weekly clock, so the NEXT quiet week is real")
    finally:
        notify.email = real2

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL DIGEST CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if "--now" in _sys.argv:
        # ONE command must be able to prove this works. `--now` decides and sends exactly as the
        # scheduler would; `--force daily|weekly` sends regardless of what is due (for a first run).
        _force = ""
        if "--force" in _sys.argv:
            _i = _sys.argv.index("--force")
            _force = _sys.argv[_i + 1] if len(_sys.argv) > _i + 1 else "daily"
        _r = run_once(force=_force)
        print("digest: kind=%s sent=%s %s" % (_r.get("kind") or "-", _r.get("sent"),
                                              _r.get("why") or ""))
        if _r.get("body"):
            print("-" * 60)
            print(_r["body"])
        raise SystemExit(0 if (_r.get("sent") or not _r.get("kind")) else 1)
    raise SystemExit(_selftest())
