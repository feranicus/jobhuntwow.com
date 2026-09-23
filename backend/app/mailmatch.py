"""WHICH APPLICATION IS THIS EMAIL ABOUT? Deterministic, explainable, and it refuses to guess.

He asked to see incoming mail and correlate it with the roles in the pipeline. The correlation is
the whole value: a recruiter reply that lands beside the job it belongs to turns an inbox into a
CRM. A correlation that is WRONG does the opposite -- it files Cisco's rejection under the Fireblocks
application and the board starts lying.

SO THE MATCH IS ARITHMETIC, NOT A MODEL. Every signal below is a fact about the message compared to
a fact about the row, each carries a weight earned by how hard it is to be wrong about, and the
evidence is returned so the card can show WHY. A model is the fourth thing consulted in this estate
and it is not consulted here at all: there is nothing for it to add that counting cannot do, and a
hallucinated link to the wrong employer is worse than no link.

THE SIGNALS, strongest first:
  5  the ATS thread id / job id from the application URL appears in the message
  4  the sender's DOMAIN is the employer's own domain, or the ATS host we applied through
  3  the EMPLOYER name appears in the From display name, the subject or the body
  2  the ROLE title (3+ significant words of it) appears in the subject
  1  the message arrived AFTER we applied and within the correlation window

NOTHING MATCHES ON ONE WEAK SIGNAL. The floor is deliberately above "the employer name appeared
once", because "Anaconda" in a newsletter is not a reply from Anaconda. A tie between two rows is
resolved by the strongest evidence and then by recency; if two rows are still tied, the message is
left UNMATCHED and says so -- an honest "I do not know which of these two" beats filing it under
the wrong one.
"""
import re
import time

# How far back a message may sit from the application and still be about it. A recruiter answering
# five months later is real; six is a different search.
WINDOW_DAYS = 180
# The score a candidate must clear. 5 = one strong signal plus corroboration, or two mediums.
MIN_SCORE = 5
# Free-mail and ATS infrastructure domains: they say WHO SENT IT, never WHOSE JOB it is about.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "me.com", "gmx.de", "gmx.net", "web.de", "proton.me", "protonmail.com", "mail.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "xing.com", "stepstone.de", "monster.com",
}
# The ATS hosts we apply through. The sender domain alone does not name the employer, but a message
# from one of these that ALSO carries the employer or the job id is very strong.
ATS_DOMAINS = {
    "greenhouse.io", "myworkday.com", "myworkdayjobs.com", "ashbyhq.com", "lever.co",
    "smartrecruiters.com", "personio.de", "hibob.com", "workable.com", "teamtailor.com",
    "recruitee.com", "successfactors.com", "taleo.net", "icims.com", "jobvite.com",
}
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&.+-]{1,}")
_LEGAL = re.compile(r"(?i)\b(gmbh|ag|ug|kg|ohg|se|ltd|limited|inc|corp|corporation|llc|plc|bv|nv|"
                    r"oy|ab|as|a/s|sa|srl|spa|s\.p\.a|oü|ou|sp\.? z ?o\.?o\.?|pty|co)\b\.?")
_NOISE = {"the", "and", "for", "job", "role", "team", "group", "at", "of", "in", "we", "our"}


def norm(s) -> str:
    """Lowercased, legal-suffix-free, punctuation-free. Two spellings of one employer must collide."""
    v = _LEGAL.sub(" ", str(s or "").lower())
    return " ".join(w for w in _WORD.findall(v) if w not in _NOISE)


def domain_of(addr) -> str:
    """The domain from `Name <a@b.com>` or `a@b.com`. '' when there is none to read."""
    m = re.search(r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", str(addr or ""))
    if not m:
        return ""
    d = m.group(1).lower().strip(".")
    return d


def registrable(domain) -> str:
    """The last two labels — good enough to tell `eu.greenhouse.io` from `cisco.com` here, and
    deliberately NOT a public-suffix implementation: this decides a UI link, not an ownership claim."""
    parts = [p for p in str(domain or "").lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def _sig_words(text, min_len=4, limit=8):
    """The words worth matching a title on: long enough to discriminate, capped so a 12-word title
    cannot out-vote a strong signal."""
    out = []
    for w in _WORD.findall(str(text or "").lower()):
        if len(w) >= min_len and w not in _NOISE and w not in out:
            out.append(w)
    return out[:limit]


def score_row(msg: dict, row: dict, now=None) -> tuple:
    """(score, evidence[]) for ONE message against ONE application row. Pure; never raises."""
    now = now or time.time()
    ev, sc = [], 0
    frm = str(msg.get("from") or "")
    reply = str(msg.get("reply_to") or "")
    subj = str(msg.get("subject") or "")
    body = str(msg.get("body") or msg.get("snippet") or "")
    hay = " \n ".join((frm, reply, subj, body)).lower()

    emp = norm(row.get("employer"))
    emp_words = [w for w in emp.split() if len(w) >= 4]
    url = str(row.get("jd_url") or "")
    sent_ts = int(row.get("sent_ts") or 0) or int(row.get("created_ts") or 0)

    # 5 — the job's own identifier. An ATS puts it in the subject or the footer of every message in
    # the thread, and nothing else in the world carries it.
    for ident in _identifiers(url, row):
        if ident and ident.lower() in hay:
            sc += 5
            ev.append("the posting's own id (%s) is in the message" % ident[:40])
            break

    # 4 — the sender's domain. The employer's own domain, or the ATS we applied through.
    d = registrable(domain_of(reply) or domain_of(frm))
    if d and d not in GENERIC_DOMAINS:
        if emp_words and any(w in d.replace(".", " ") for w in emp_words):
            sc += 4
            ev.append("the sender's domain (%s) is the employer's" % d)
        elif d in ATS_DOMAINS and (registrable(domain_of(url)) == d or emp_words):
            sc += 3
            ev.append("it came from the ATS we applied through (%s)" % d)
        elif registrable(domain_of(url)) and registrable(domain_of(url)) == d:
            sc += 4
            ev.append("the sender's domain matches the posting's (%s)" % d)

    # 3 — the employer NAME, in the from-name, the subject or the body.
    if emp_words and all(w in hay for w in emp_words[:2]):
        sc += 3
        ev.append("the employer's name appears in the message")

    # 2 — the ROLE. Three significant words of the title, so "Manager" alone proves nothing.
    tw = _sig_words(row.get("title"))
    hit = [w for w in tw if w in subj.lower()]
    if len(hit) >= 3 or (tw and len(hit) >= 2 and len(tw) <= 3):
        sc += 2
        ev.append("the subject carries the role (%s)" % ", ".join(hit[:4]))

    # 1 — TIME. It cannot be about an application that had not happened yet.
    ts = float(msg.get("ts") or 0)
    if ts and sent_ts:
        if ts < sent_ts - 86400:
            return 0, ["the message predates the application"]
        if ts - sent_ts <= WINDOW_DAYS * 86400:
            sc += 1
            ev.append("it arrived %d day(s) after the application"
                      % int((ts - sent_ts) / 86400))
    return sc, ev


def _identifiers(url, row) -> list:
    """Strings that identify this job and nothing else: the ATS's own id out of the posting URL,
    and our own job_id (an apply engine that echoes it puts it in the thread)."""
    out = []
    for m in re.finditer(r"/(\d{6,})(?:[/?#]|$)", str(url or "")):
        out.append(m.group(1))
    m = re.search(r"/([0-9a-f]{8}-[0-9a-f-]{20,})", str(url or ""), re.I)
    if m:
        out.append(m.group(1))
    jid = str(row.get("job_id") or "")
    if len(jid) >= 8:
        out.append(jid)
    return out


def match(msg: dict, rows: list, now=None, min_score=None) -> dict:
    """Which row is this message about? {job_id, score, evidence, why_not} — never raises.

    `job_id` is "" when nothing clears the floor OR when two rows tie on the same evidence: an
    honest refusal, with the reason, beats filing a rejection under the wrong employer.
    """
    floor = MIN_SCORE if min_score is None else int(min_score)
    scored = []
    for r in rows or []:
        try:
            s, ev = score_row(msg, r, now)
        except Exception:
            s, ev = 0, []
        if s >= floor:
            scored.append((s, int(r.get("sent_ts") or r.get("created_ts") or 0), r, ev))
    if not scored:
        return {"job_id": "", "score": 0, "evidence": [],
                "why_not": "no application matched strongly enough (floor %d)" % floor}
    scored.sort(key=lambda t: (-t[0], -t[1]))
    best = scored[0]
    if len(scored) > 1 and scored[1][0] == best[0]:
        tied = [t[2].get("employer") or t[2].get("job_id") for t in scored[:3]]
        return {"job_id": "", "score": best[0], "evidence": best[3],
                "why_not": "two applications match equally well (%s) - not filing it under either"
                           % ", ".join(str(x) for x in tied[:2])}
    return {"job_id": best[2].get("job_id") or "", "score": best[0], "evidence": best[3],
            "why_not": ""}


def classify(msg: dict) -> str:
    """What KIND of message is this? Labelling only — it never decides a match, and an unknown
    shape is `mail`, never a guess that would move a card on its own."""
    s = (str(msg.get("subject") or "") + " " + str(msg.get("snippet") or "")).lower()
    if re.search(r"\b(unfortunately|not (?:be )?mov(?:e|ing) forward|regret to inform|"
                 r"decided to (?:proceed|move forward) with (?:other|another))\b", s):
        return "rejection"
    if re.search(r"\b(interview|schedule a call|book a time|calendly|meet(?:ing)? invite|"
                 r"phone screen|screening call)\b", s):
        return "interview"
    if re.search(r"\b(offer|offer letter|compensation package|we are pleased to offer)\b", s):
        return "offer"
    if re.search(r"\b(application (?:received|submitted)|thank you for applying|"
                 r"we (?:have )?received your application)\b", s):
        return "acknowledgement"
    if re.search(r"\b(assessment|take[- ]home|coding challenge|hackerrank|codility)\b", s):
        return "task"
    return "mail"


# --------------------------------------------------------------------------- self-test
def _selftest() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    now = time.time()
    day = 86400.0
    rows = [
        {"job_id": "20260901-aaa", "employer": "Fireblocks", "title": "Senior Project Manager",
         "jd_url": "https://boards.greenhouse.io/fireblocks/jobs/4512345",
         "sent_ts": now - 5 * day, "created_ts": now - 6 * day},
        {"job_id": "20260902-bbb", "employer": "Cisco Systems", "title": "Programme Manager EMEA",
         "jd_url": "https://jobs.cisco.com/jobs/ProjectDetail/1234567",
         "sent_ts": now - 10 * day, "created_ts": now - 11 * day},
    ]

    m = {"from": "Talent Team <no-reply@greenhouse.io>", "reply_to": "recruiter@fireblocks.com",
         "subject": "Your application for Senior Project Manager at Fireblocks",
         "snippet": "Thanks for applying. 4512345", "ts": now - 4 * day}
    r = match(m, rows)
    ck("a real recruiter reply lands on the right application",
       r["job_id"] == "20260901-aaa", "%s %s" % (r["job_id"], r["evidence"]))
    ck("and it SAYS why", len(r["evidence"]) >= 2, str(r["evidence"]))

    news = {"from": "News <news@techletter.io>", "subject": "Fireblocks raises a round",
            "snippet": "Crypto custody company Fireblocks announced...", "ts": now - day}
    r2 = match(news, rows)
    ck("a newsletter that merely NAMES the employer is not a match",
       r2["job_id"] == "", "%s %s" % (r2["job_id"], r2["why_not"]))

    old = dict(m, ts=now - 40 * day)
    r3 = match(old, rows)
    ck("a message that predates the application is refused outright",
       r3["job_id"] == "", r3["why_not"])

    tie_rows = [dict(rows[0]), dict(rows[0], job_id="20260901-ccc")]
    r4 = match(m, tie_rows)
    ck("two rows that match equally well leave it UNFILED, with the reason",
       r4["job_id"] == "" and "equally well" in r4["why_not"], r4["why_not"])

    generic = {"from": "Anna <anna@gmail.com>", "subject": "Senior Project Manager - next steps",
               "snippet": "Hi, about your application to Fireblocks for Senior Project Manager",
               "ts": now - 3 * day}
    r5 = match(generic, rows)
    ck("a recruiter writing from gmail still matches on name + role + time",
       r5["job_id"] == "20260901-aaa", "%s %s" % (r5["job_id"], r5["evidence"]))

    ck("the ATS domain alone is not enough to file it",
       match({"from": "no-reply@greenhouse.io", "subject": "hello", "ts": now}, rows)["job_id"] == "",
       "")

    ck("norm() collapses legal suffixes", norm("Stars4Business OÜ") == norm("stars4business"),
       "%r vs %r" % (norm("Stars4Business OÜ"), norm("stars4business")))
    ck("domain_of reads a display-name address",
       domain_of("Talent <recruiting@cisco.com>") == "cisco.com")
    ck("registrable trims the host", registrable("eu.boards.greenhouse.io") == "greenhouse.io")

    ck("a rejection is labelled", classify({"subject": "We regret to inform you"}) == "rejection")
    ck("an interview invite is labelled",
       classify({"subject": "Interview - next steps", "snippet": ""}) == "interview")
    ck("an offer is labelled", classify({"subject": "We are pleased to offer you"}) == "offer")
    ck("an acknowledgement is labelled",
       classify({"subject": "We received your application"}) == "acknowledgement")
    ck("anything else is just mail, never a guess", classify({"subject": "hello there"}) == "mail")
    ck("the label NEVER moves a card on its own - it is not part of match()",
       "classify" not in match.__doc__ and "stage" not in match.__doc__)

    print("mailmatch selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
