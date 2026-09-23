"""THE LOOP: read the mailbox, correlate each message to an application, file it, say so once.

This is the join between `gmail_read` (what arrived) and `mailmatch` (which job it is about), plus
the two things that make an integration trustworthy rather than magic:

  * IT ANNOUNCES ONCE. A message is judged the first time it is seen and never re-announced, even
    though the poller re-reads an overlapping window every few minutes. `tracker.mail_seen()` is the
    memory, so a restart cannot re-page him for yesterday's rejection.
  * IT NEVER MOVES A CARD BY ITSELF. `mailmatch.classify()` labels a message `rejection`,
    `interview`, `offer` — and that label is shown, not acted on. A subject line that reads like a
    rejection is not a rejection, and an inbox heuristic that silently drags a live application into
    the Rejected column would destroy the one thing the board is for. He drags his own cards.

Reading is OPT-IN (`JHW_MAIL_READ=1`) and READ-ONLY. Everything here is best-effort: a mail fault
writes a line and the product carries on.
"""
import json
import os
import time

EVERY_S = int(os.environ.get("JHW_MAIL_EVERY_S", "600"))
LOOKBACK_DAYS = int(os.environ.get("JHW_MAIL_LOOKBACK_DAYS", "14"))
SERVICE = os.environ.get("SERVICE", "jhw-web")
# A feed, not an alert: its own cap, so a noisy morning can never eat the security storm cap.
MAX_TELL_PER_RUN = int(os.environ.get("JHW_MAIL_TELL_MAX", "6"))


def _sib(name):
    from importlib import import_module
    if __package__:
        return import_module("." + name, __package__)
    return import_module(name)


def _log(**k):
    k.setdefault("ts", int(time.time()))
    k.setdefault("service", SERVICE)
    try:
        print(json.dumps(k, default=str), flush=True)
    except Exception:
        pass
    p = os.environ.get("EVENTS_LOG", "")
    if p:
        try:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(k, default=str) + "\n")
        except Exception:
            pass


def run_once(fetch=None, rows=None, messages=None, tell=None, now=None) -> dict:
    """One pass. Every dependency is injectable, so the whole loop is testable with no network,
    no database of his, and no Telegram.

    Returns a report: what was read, what was filed, what was deliberately not, and why.
    """
    out = {"read": 0, "filed": 0, "unfiled": 0, "new": 0, "note": "", "told": 0, "items": []}
    gm = _sib("gmail_read")
    mm = _sib("mailmatch")
    tr = _sib("tracker")

    if messages is None:
        if not gm.configured() and fetch is None:
            out["note"] = gm.status()["note"]
            return out
        messages, out["note"] = gm.fetch_recent(days=LOOKBACK_DAYS, fetch=fetch)
    out["read"] = len(messages or [])
    if rows is None:
        try:
            rows = tr.recent(days=365, limit=400)
        except Exception as e:
            out["note"] = "the pipeline could not be read (%r)" % (e,)
            return out

    told = 0
    for msg in (messages or []):
        mid = str(msg.get("id") or "")
        if not mid:
            continue
        try:
            already = tr.mail_seen(mid)
        except Exception:
            already = False
        verdict = mm.match(msg, rows, now=now)
        kind = mm.classify(msg)
        try:
            tr.record_mail(msg, verdict, kind)
        except Exception:
            pass
        if verdict.get("job_id"):
            out["filed"] += 1
        else:
            out["unfiled"] += 1
        if already:
            continue
        out["new"] += 1
        out["items"].append({"id": mid, "subject": msg.get("subject", "")[:120],
                             "kind": kind, "job_id": verdict.get("job_id", ""),
                             "score": verdict.get("score", 0),
                             "evidence": verdict.get("evidence", [])[:3],
                             "why_not": verdict.get("why_not", "")})
        if verdict.get("job_id") and told < MAX_TELL_PER_RUN:
            told += 1
            row = next((r for r in rows if r.get("job_id") == verdict["job_id"]), {})
            _announce(row, msg, kind, verdict, tell)
    out["told"] = told
    _log(evt="mail_pass", read=out["read"], new=out["new"], filed=out["filed"],
         unfiled=out["unfiled"], told=told, note=out["note"])
    return out


def _announce(row, msg, kind, verdict, tell=None):
    """One plain-text Telegram line for a message that landed on a real application.

    PLAIN TEXT, because a subject line is somebody else's text and one stray underscore makes
    Telegram reject the whole message -- the rule this estate learned by losing the alert that
    mattered most.
    """
    label = {"rejection": "Rejection", "interview": "Interview", "offer": "OFFER",
             "task": "Task / assessment", "acknowledgement": "Application received"}.get(
                 kind, "Reply")
    body = "\n".join([
        "%s - %s" % (label, row.get("employer") or "(employer not recorded)"),
        "",
        "role    : %s" % (row.get("title") or "-"),
        "from    : %s" % str(msg.get("from") or "-")[:120],
        "subject : %s" % str(msg.get("subject") or "-")[:160],
        "when    : %s" % time.strftime("%Y-%m-%d %H:%M UTC",
                                       time.gmtime(float(msg.get("ts") or time.time()))),
        "",
        "matched because: %s" % "; ".join(verdict.get("evidence", [])[:3]),
        "",
        "Nothing was moved on the board. Labels are a reading of the subject line, never a decision.",
    ])
    _log(evt="mail_notice", kind=kind, job_id=verdict.get("job_id"),
         employer=row.get("employer"), score=verdict.get("score"))
    try:
        if tell:
            tell("%s - %s" % (label, row.get("employer") or "?"), body)
            return
        notify = _sib("notify")
        notify.fire_and_forget(notify.both,
                               "%s - %s" % (label, row.get("employer") or "?"), body)
    except Exception:
        pass


async def scheduler():
    import asyncio
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_once)
        except Exception as e:
            _log(evt="mail_watch_error", err=repr(e)[:160])
        await asyncio.sleep(max(120, EVERY_S))


# --------------------------------------------------------------------------- self-test
def _selftest() -> int:
    import tempfile
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    os.environ["JHW_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(), "t.sqlite")
    tr = _sib("tracker")
    now = time.time()
    day = 86400.0
    rows = [{"job_id": "j-1", "employer": "Fireblocks", "title": "Senior Project Manager",
             "jd_url": "https://boards.greenhouse.io/fireblocks/jobs/4512345",
             "sent_ts": now - 5 * day, "created_ts": now - 6 * day}]
    msgs = [
        {"id": "m1", "thread_id": "t1", "ts": now - day,
         "from": "Anna <anna@fireblocks.com>", "reply_to": "",
         "subject": "Senior Project Manager - interview next week",
         "snippet": "Could we schedule a call about your application to Fireblocks?"},
        {"id": "m2", "thread_id": "t2", "ts": now - day,
         "from": "News <news@techletter.io>", "subject": "Fireblocks raises a round",
         "snippet": "Crypto custody company Fireblocks announced..."},
    ]
    sent = []
    r1 = run_once(rows=rows, messages=msgs, tell=lambda s, b: sent.append((s, b)), now=now)
    ck("both messages were read", r1["read"] == 2, str(r1))
    ck("the recruiter reply is FILED against the application", r1["filed"] == 1, str(r1))
    ck("the newsletter is NOT", r1["unfiled"] == 1)
    ck("he is told once, and only about the filed one", len(sent) == 1, str(len(sent)))
    ck("the message names the employer, the role and WHY it matched",
       sent and "Fireblocks" in sent[0][1] and "matched because" in sent[0][1])
    ck("...and says plainly that nothing was moved on the board",
       sent and "Nothing was moved on the board" in sent[0][1])
    ck("an interview invite is labelled as one", sent and sent[0][0].startswith("Interview"),
       sent[0][0] if sent else "")

    filed = tr.mails_for("j-1")
    ck("the card can read it back", len(filed) == 1 and filed[0]["subject"].startswith("Senior"),
       str(filed)[:80])
    ck("with a link into Gmail", filed and filed[0]["url"].startswith("https://mail.google.com/"))
    unf = tr.unfiled_mail(hours=48)
    ck("the unfiled one is kept WITH ITS REASON, not dropped",
       len(unf) == 1 and "matched" in (unf[0]["why_not"] or ""), str(unf)[:90])

    sent2 = []
    r2 = run_once(rows=rows, messages=msgs, tell=lambda s, b: sent2.append(s), now=now)
    ck("a second pass over the same window announces NOTHING again",
       r2["new"] == 0 and sent2 == [], "new=%d told=%d" % (r2["new"], len(sent2)))
    ck("...while still counting what it saw", r2["read"] == 2 and r2["filed"] == 1)

    r3 = run_once(rows=rows, messages=[], tell=lambda s, b: None)
    ck("an empty mailbox is not an error", r3["read"] == 0 and r3["new"] == 0)

    # THE LABEL MUST NOT BE ABLE TO MOVE A CARD. Prove it by reading the row after a rejection.
    rej = [{"id": "m3", "thread_id": "t3", "ts": now - day,
            "from": "Anna <anna@fireblocks.com>",
            "subject": "Senior Project Manager - we regret to inform you",
            "snippet": "Unfortunately we will not be moving forward with your application."}]
    tr.record_tailored({"job_id": "j-1", "email": "x@y.z",
                        "jd": {"company": "Fireblocks", "title": "Senior Project Manager"}},
                       jd_text="a posting", files=["resume_x.pdf"])
    before = (tr.get("j-1") or {}).get("stage")
    run_once(rows=rows, messages=rej, tell=lambda s, b: None, now=now)
    after = (tr.get("j-1") or {}).get("stage")
    ck("a REJECTION email does not move the card - he drags his own cards",
       before == after, "%s -> %s" % (before, after))
    ck("but it IS on the card, labelled",
       any(m["kind"] == "rejection" for m in tr.mails_for("j-1")),
       str([m["kind"] for m in tr.mails_for("j-1")]))

    print("mailwatch selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
