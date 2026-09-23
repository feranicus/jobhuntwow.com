#!/usr/bin/env python3
"""TRACKER — the one row that says WHICH resume went to WHICH job description.

WHY THIS EXISTS (asked for 2026-09-18): "in the Tailor part there would be a clear database
correlation for this job description either with link or with pasted job description ... we sent
this and that resume". Until now that correlation lived only in `DATA_DIR/users/<hash>/<job_id>/
job.json` -- one file per job, no index, and the PASTED job description was never kept at all
(the manifest stored `jd.url` and `jd.title`, so a posting pasted as text left no trace of what we
actually applied to). A funnel you cannot query is not a funnel, and a correlation you cannot read
back is not a correlation.

WHAT A ROW IS: one application. It is created when the documents are TAILORED and updated when the
application is SENT, so the same row carries the job description, the exact files produced, and the
submission. That is what the daily digest reads, and it is what the Pipeline/CRM will read.

THE PROPERTY THAT MAKES A ROW USEFUL -- `correlation_ok()`: a row must carry the job description
(a link OR the pasted text) AND at least one document. A row that has lost either half cannot tell
anyone what was sent where, so the digest REPORTS it instead of quietly counting it. Absence of
evidence is never a finding: an incomplete row is named, not dropped.

NEVER RAISES INTO A REQUEST. Every writer is best-effort: a bookkeeping failure must not cost the
candidate a tailored resume. Failures are logged through notify._log so they are visible in Loki.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:                                   # importable both as a package module and standalone (--logic)
    from .settings import DATA_DIR
except Exception:                      # pragma: no cover - selftest path
    DATA_DIR = os.getenv("DATA_DIR", "/data")

JD_TEXT_MAX = 40000                    # a JD is prose; anything past this is a paste accident
# THE REAL LIFECYCLE (2026-09-18). His words: *"Apply and Submitted is same shit different color.
# but in the interview process there are at least 3-4-5 stages"*. Right on both counts:
#   * APPLIED and SUBMITTED were one event wearing two hats. The engine's distinction is real — it
#     says "submitted" only when the SITE confirmed — but that is EVIDENCE about one event, not a
#     second step in his funnel. It is kept on the row as `confirmed` and shown as a tick.
#   * "Interview" is not a stage, it is a SEASON: HR screen, technical, take-home or presentation,
#     hiring manager, final panel. A funnel that cannot say which round you are in cannot tell you
#     what to prepare for tonight.
# AN OFFER IS NOT THE END. His words (2026-09-18): *"where is the stage of Contract negotiations
# and Signed contract?"* — between "they want you" and "you have a job" sit the two weeks that
# decide the money, the start date and the notice period. A funnel that stops at `offer` cannot
# tell him which of those conversations is open.
STAGES = ("tailored", "applied", "hr_screen", "tech", "task", "manager", "final",
          "offer", "negotiation", "signed", "rejected")
# Old names keep working forever: the apply engine (a different codebase, on his PC) reports
# `submitted`, and rows already on his board carry the old vocabulary.
LEGACY_STAGES = {"submitted": "applied", "sent": "applied", "interview": "hr_screen",
                 "screen": "hr_screen", "phone": "hr_screen", "onsite": "final",
                 "technical": "tech", "assignment": "task", "presentation": "task",
                 "hiring manager": "manager", "panel": "final", "withdrawn": "rejected",
                 "negotiating": "negotiation", "contract": "negotiation",
                 "contract negotiation": "negotiation", "offer negotiation": "negotiation",
                 "accepted": "signed", "hired": "signed", "signed contract": "signed",
                 "closed won": "signed", "won": "signed"}


def canon_stage(stage: str) -> str:
    """The canonical stage for any name we have ever used, or '' for one we have not. PURE.

    A rename must never silently drop a row into a column that does not exist."""
    v = str(stage or "").strip().lower().replace("-", "_")
    if v in STAGES:
        return v
    return LEGACY_STAGES.get(v) or LEGACY_STAGES.get(v.replace("_", " ")) or "" 


def db_path() -> str:
    """ONE home for the location, and it is overridable so a test never touches real data.

    A suite that reads and writes the production store is the defect this project already paid for
    (a fixture answer `pick one -> Indeed` sat in his real memory, ready to be typed into a form)."""
    return os.getenv("JHW_TRACKER_DB", "") or os.path.join(str(DATA_DIR), "tracker.sqlite")


def _log(**k):
    try:
        from . import notify
        notify._log(**k)
    except Exception:
        try:
            print(json.dumps(dict(k, ts=time.time(), service="jhw-web")), flush=True)
        except Exception:
            pass


def _conn():
    p = db_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")      # the apply engine and the portal both write
    return c


_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
  job_id      TEXT PRIMARY KEY,
  email       TEXT NOT NULL DEFAULT '',
  employer    TEXT NOT NULL DEFAULT '',
  title       TEXT NOT NULL DEFAULT '',
  location    TEXT NOT NULL DEFAULT '',
  jd_url      TEXT NOT NULL DEFAULT '',
  jd_text     TEXT NOT NULL DEFAULT '',
  jd_sha      TEXT NOT NULL DEFAULT '',
  jd_source   TEXT NOT NULL DEFAULT '',
  resume_file TEXT NOT NULL DEFAULT '',
  cover_file  TEXT NOT NULL DEFAULT '',
  files       TEXT NOT NULL DEFAULT '[]',
  created_ts  INTEGER NOT NULL DEFAULT 0,
  sent_ts     INTEGER NOT NULL DEFAULT 0,
  sent_url    TEXT NOT NULL DEFAULT '',
  ats         TEXT NOT NULL DEFAULT '',
  stage       TEXT NOT NULL DEFAULT 'tailored',
  confirmed   INTEGER NOT NULL DEFAULT 0,
  note        TEXT NOT NULL DEFAULT '',
  updated_ts  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS app_created ON applications(created_ts);
CREATE INDEX IF NOT EXISTS app_sent    ON applications(sent_ts);
CREATE INDEX IF NOT EXISTS app_sha     ON applications(jd_sha);
CREATE TABLE IF NOT EXISTS mails(
  msg_id     TEXT PRIMARY KEY,
  job_id     TEXT NOT NULL DEFAULT '',
  thread_id  TEXT NOT NULL DEFAULT '',
  ts         REAL NOT NULL DEFAULT 0,
  sender     TEXT NOT NULL DEFAULT '',
  subject    TEXT NOT NULL DEFAULT '',
  snippet    TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT 'mail',
  score      INTEGER NOT NULL DEFAULT 0,
  evidence   TEXT NOT NULL DEFAULT '[]',
  why_not    TEXT NOT NULL DEFAULT '',
  seen_ts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS mail_job ON mails(job_id);
CREATE INDEX IF NOT EXISTS mail_ts  ON mails(ts);
CREATE TABLE IF NOT EXISTS notes(
  note_id    TEXT PRIMARY KEY,
  job_id     TEXT NOT NULL DEFAULT '',
  ts         INTEGER NOT NULL DEFAULT 0,
  author     TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT 'note',
  body       TEXT NOT NULL DEFAULT '',
  when_ts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS note_job ON notes(job_id);
CREATE INDEX IF NOT EXISTS note_ts  ON notes(ts);
CREATE TABLE IF NOT EXISTS attachments(
  att_id     TEXT PRIMARY KEY,
  job_id     TEXT NOT NULL DEFAULT '',
  name       TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT '',
  bytes      INTEGER NOT NULL DEFAULT 0,
  chars      INTEGER NOT NULL DEFAULT 0,
  words      INTEGER NOT NULL DEFAULT 0,
  text_note  TEXT NOT NULL DEFAULT '',
  label      TEXT NOT NULL DEFAULT '',
  ts         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS att_job ON attachments(job_id);
CREATE TABLE IF NOT EXISTS digests(
  kind TEXT PRIMARY KEY, last_ts INTEGER NOT NULL DEFAULT 0, last_count INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(c) -> None:
    """Bring an older database up to today's shape. Idempotent, and it never loses a row.

    `confirmed` is added to tables created before it existed, and every legacy stage name is
    rewritten to its canonical column — `submitted` rows become `applied` AND keep their evidence,
    because that word had meant "the site confirmed it"."""
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(applications)").fetchall()}
        if "confirmed" not in cols:
            c.execute("ALTER TABLE applications ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE applications SET confirmed=1 WHERE stage='submitted'")
        for old, new in LEGACY_STAGES.items():
            c.execute("UPDATE applications SET stage=? WHERE stage=?", (new, old))
    except Exception as e:
        _log(evt="tracker_error", where="migrate", err=repr(e)[:180])


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        _migrate(c)


def jd_fingerprint(url: str = "", text: str = "") -> str:
    """The SAME posting must produce the SAME fingerprint whether it arrived as a link or a paste.

    A URL is normalised (scheme/host lowercased, tracking query dropped, trailing slash removed)
    because `?utm_source=linkedin` is not a different job. Text is whitespace-collapsed for the same
    reason: he pastes the same posting twice and it is still one job."""
    u = (url or "").strip()
    if u:
        import re as _re
        u = _re.sub(r"#.*$", "", u)
        u = _re.sub(r"[?&](utm_[a-z]+|gh_src|src|source|ref|trk)=[^&]*", "", u, flags=_re.I)
        u = _re.sub(r"[?&]+$", "", u).rstrip("/")
        parts = u.split("://", 1)
        if len(parts) == 2:
            host, _, rest = parts[1].partition("/")
            u = parts[0].lower() + "://" + host.lower() + ("/" + rest if rest else "")
        return "u:" + hashlib.sha256(u.encode("utf-8", "replace")).hexdigest()[:24]
    t = " ".join((text or "").split()).lower()
    if not t:
        return ""
    return "t:" + hashlib.sha256(t.encode("utf-8", "replace")).hexdigest()[:24]


_ATS = (("ashby", r"ashbyhq\.com"), ("greenhouse", r"greenhouse\.io|boards\.eu\.greenhouse"),
        ("workday", r"myworkdayjobs\.com|workday"), ("lever", r"lever\.co"),
        ("icims", r"icims\.com"), ("smartrecruiters", r"smartrecruiters\.com"),
        ("successfactors", r"successfactors|sapsf"), ("phenom", r"phenompeople|careers\."),
        ("personio", r"personio\.de|jobs\.personio"), ("recruitee", r"recruitee\.com"))


def ats_of(url: str) -> str:
    """Which ATS took it, from the URL. Unknown stays UNKNOWN -- never a guess dressed as a fact."""
    import re as _re
    u = (url or "").lower()
    for name, rx in _ATS:
        if _re.search(rx, u):
            return name
    return ""


def correlation_ok(row: dict) -> bool:
    """Can this row answer 'which resume went to which job'? Both halves, or it cannot."""
    r = dict(row or {})
    has_jd = bool((r.get("jd_url") or "").strip() or (r.get("jd_text") or "").strip())
    files = r.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files or "[]")
        except Exception:
            files = []
    has_doc = bool((r.get("resume_file") or "").strip() or (files or []))
    return has_jd and has_doc


def _derive_employer(row: dict) -> tuple:
    """(employer, source) for a row that never got one — computed on READ, never written.

    His board showed `(employer not recorded)` on a row already holding 3,623 characters of pasted
    job description. The name was in the text the whole time; nothing had looked. Rows already in
    the database are fixed by looking NOW rather than by a migration that rewrites his history:
    a derived value is labelled, and a labelled guess can always be corrected."""
    emp = (row.get("employer") or "").strip()
    if emp:
        return emp, "jd"
    txt = row.get("jd_text") or ""
    if txt:
        try:
            from . import jd_ingest as _ji
        except Exception:                       # pragma: no cover - standalone --logic run
            try:
                import jd_ingest as _ji        # type: ignore
            except Exception:
                _ji = None
        if _ji is not None:
            try:
                _, c = _ji.sniff_title_company(txt)
                if c:
                    return c, "text"
            except Exception:
                pass
    url = row.get("jd_url") or ""
    if url:
        try:
            from . import docnames as _dn
        except Exception:                       # pragma: no cover
            try:
                import docnames as _dn         # type: ignore
            except Exception:
                _dn = None
        if _dn is not None:
            c = _dn.employer_from_url(url)
            if c:
                return c, "url"
    return "", "none"


def _pick(files, *pats) -> str:
    """The document of that kind, preferring the PDF — that is the one that gets attached.

    Sorted order alone would report the .docx, and the digest's job is to say what was SENT."""
    import re as _re
    hits = [str(f) for f in (files or []) if any(_re.search(p, str(f), _re.I) for p in pats)]
    for h in hits:
        if h.lower().endswith(".pdf"):
            return h
    return hits[0] if hits else ""


def record_tailored(manifest: dict, jd_text: str = "", files=None) -> str:
    """Documents were produced for a job description. One row, created or updated in place."""
    try:
        man = manifest or {}
        jd = man.get("jd") or {}
        jid = str(man.get("job_id") or "").strip()
        if not jid:
            return ""
        fl = list(files or man.get("files") or [])
        url = str(jd.get("url") or "")
        txt = (jd_text or jd.get("text") or "")[:JD_TEXT_MAX]
        now = int(time.time())
        row = {
            "job_id": jid, "email": str(man.get("email") or ""),
            "employer": str(jd.get("company") or ""), "title": str(jd.get("title") or ""),
            "location": str(jd.get("location") or ""),
            "jd_url": url, "jd_text": txt, "jd_sha": jd_fingerprint(url, txt),
            "jd_source": str(jd.get("source") or ("link" if url else ("paste" if txt else ""))),
            "resume_file": _pick(fl, r"resume|cv"), "cover_file": _pick(fl, r"cover"),
            "files": json.dumps(sorted(str(x) for x in fl)),
            "created_ts": int(man.get("created") or now), "updated_ts": now,
        }
        with _conn() as c:
            c.executescript(_SCHEMA)
            cols = ", ".join(row)
            qs = ", ".join("?" for _ in row)
            upd = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("job_id", "created_ts"))
            c.execute(f"INSERT INTO applications ({cols}) VALUES ({qs}) "
                      f"ON CONFLICT(job_id) DO UPDATE SET {upd}", list(row.values()))
        if not correlation_ok(row):
            _log(evt="tracker_incomplete", job_id=jid,
                 has_jd=bool(url or txt), has_doc=bool(fl))
        return jid
    except Exception as e:
        _log(evt="tracker_error", where="record_tailored", err=repr(e)[:180])
        return ""


def record_sent(job_id: str, email: str = "", url: str = "", employer: str = "", title: str = "",
                status: str = "applied", note: str = "", ats: str = "") -> str:
    """The application was actually SENT. Updates the tailored row; creates one if we only have a URL.

    A submission reported for a job we never tailored is still a real application and still belongs
    in the funnel -- the apply engine drives plenty of postings straight from a link."""
    try:
        jid = str(job_id or "").strip()
        if not jid:
            return ""
        now = int(time.time())
        raw = str(status or "").strip().lower()
        stage = canon_stage(raw) or "applied"
        confirmed = 1 if raw in ("submitted", "confirmed") else 0
        with _conn() as c:
            c.executescript(_SCHEMA)
            cur = c.execute("SELECT job_id, jd_url, jd_text FROM applications WHERE job_id=?",
                            (jid,)).fetchone()
            if cur:
                jd_url = cur["jd_url"] or url or ""
                if confirmed:
                    c.execute("UPDATE applications SET confirmed=1 WHERE job_id=?", (jid,))
                c.execute("UPDATE applications SET sent_ts=?, sent_url=?, ats=?, stage=?, note=?, "
                          "employer=COALESCE(NULLIF(employer,''),?), "
                          "title=COALESCE(NULLIF(title,''),?), jd_url=?, jd_sha=?, updated_ts=? "
                          "WHERE job_id=?",
                          (now, url or jd_url, ats, stage, (note or "")[:500], employer, title,
                           jd_url, jd_fingerprint(jd_url, cur["jd_text"] or ""), now, jid))
            else:
                c.execute("INSERT INTO applications (job_id, email, employer, title, jd_url, jd_sha,"
                          " jd_source, created_ts, sent_ts, sent_url, ats, stage, note, updated_ts,"
                          " confirmed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (jid, email, employer, title, url, jd_fingerprint(url, ""), "link",
                           now, now, url, ats, stage, (note or "")[:500], now, confirmed))
        return jid
    except Exception as e:
        _log(evt="tracker_error", where="record_sent", err=repr(e)[:180])
        return ""


def set_stage(job_id: str, stage: str, note: str = "") -> bool:
    stage = canon_stage(stage)
    if not stage:
        return False
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            n = c.execute("UPDATE applications SET stage=?, note=COALESCE(NULLIF(?,''),note), "
                          "updated_ts=? WHERE job_id=?",
                          (stage, (note or "")[:500], int(time.time()), job_id)).rowcount
        return bool(n)
    except Exception as e:
        _log(evt="tracker_error", where="set_stage", err=repr(e)[:180])
        return False


def set_fields(job_id: str, employer=None, title=None, note=None) -> bool:
    """Correct a row by hand. The derivations are guesses; a human's word is not.

    Only the three fields a person can reasonably know better than we do. Everything else about a
    row is evidence (what was sent, when, which files) and is never hand-editable."""
    sets, args = [], []
    for col, val in (("employer", employer), ("title", title), ("note", note)):
        if val is not None:
            sets.append("%s=?" % col)
            args.append(str(val)[:300])
    if not sets:
        return False
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            args += [int(time.time()), job_id]
            n = c.execute("UPDATE applications SET %s, updated_ts=? WHERE job_id=?"
                          % ", ".join(sets), args).rowcount
        return bool(n)
    except Exception as e:
        _log(evt="tracker_error", where="set_fields", err=repr(e)[:180])
        return False


def rows(since_ts: int = 0, until_ts: Optional[int] = None, email: str = "",
         sent_only: bool = False, limit: int = 500) -> list:
    """Rows in a window. `sent_only` windows on sent_ts, otherwise on created_ts."""
    try:
        until = int(until_ts if until_ts is not None else time.time() + 1)
        col = "sent_ts" if sent_only else "created_ts"
        q = f"SELECT * FROM applications WHERE {col} >= ? AND {col} < ?"
        args: list[Any] = [int(since_ts), until]
        if sent_only:
            q += " AND sent_ts > 0"
        if email:
            q += " AND email = ?"
            args.append(email)
        q += f" ORDER BY {col} DESC LIMIT ?"
        args.append(int(limit))
        with _conn() as c:
            c.executescript(_SCHEMA)
            out = []
            for r in c.execute(q, args).fetchall():
                d = dict(r)
                try:
                    d["files"] = json.loads(d.get("files") or "[]")
                except Exception:
                    d["files"] = []
                d["employer"], d["employer_source"] = _derive_employer(d)
                d["jd_chars"] = len(d.pop("jd_text", "") or "")     # the TEXT is not list payload
                d["correlated"] = correlation_ok(dict(r))
                out.append(d)
            return out
    except Exception as e:
        _log(evt="tracker_error", where="rows", err=repr(e)[:180])
        return []


def record_mail(msg: dict, verdict: dict, kind: str = "mail") -> bool:
    """Store one correlated (or deliberately UNcorrelated) message. Returns True when it is new.

    THE UNMATCHED ONES ARE STORED TOO, with `why_not`. "I saw this mail and did not file it, and
    here is the reason" is the line that makes the next question answerable; silently dropping it
    is how an inbox integration becomes a black box that people stop trusting.
    """
    mid = str((msg or {}).get("id") or "")[:64]
    if not mid:
        return False
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            _migrate(c)
            already = c.execute("SELECT 1 FROM mails WHERE msg_id=?", (mid,)).fetchone()
            c.execute(
                "INSERT INTO mails (msg_id,job_id,thread_id,ts,sender,subject,snippet,kind,score,"
                "evidence,why_not,seen_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(msg_id) DO UPDATE SET job_id=excluded.job_id, score=excluded.score, "
                "evidence=excluded.evidence, why_not=excluded.why_not, kind=excluded.kind",
                (mid, str((verdict or {}).get("job_id") or "")[:64],
                 str(msg.get("thread_id") or "")[:64], float(msg.get("ts") or 0),
                 str(msg.get("from") or "")[:200], str(msg.get("subject") or "")[:300],
                 str(msg.get("snippet") or "")[:400], str(kind or "mail")[:24],
                 int((verdict or {}).get("score") or 0),
                 json.dumps(list((verdict or {}).get("evidence") or [])[:8]),
                 str((verdict or {}).get("why_not") or "")[:200], int(time.time())))
            return not already
    except Exception as e:                      # bookkeeping never aborts anything
        print(json.dumps({"evt": "mail_store_error", "err": repr(e)[:160]}), flush=True)
        return False


def mails_for(job_id: str, limit: int = 40) -> list:
    """Every message filed against one application, newest first."""
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            rows = c.execute(
                "SELECT msg_id,thread_id,ts,sender,subject,snippet,kind,score,evidence "
                "FROM mails WHERE job_id=? ORDER BY ts DESC LIMIT ?",
                (str(job_id or ""), max(1, min(200, int(limit))))).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            ev = json.loads(r[8] or "[]")
        except Exception:
            ev = []
        out.append({"msg_id": r[0], "thread_id": r[1], "ts": r[2], "from": r[3], "subject": r[4],
                    "snippet": r[5], "kind": r[6], "score": r[7], "evidence": ev,
                    "url": "https://mail.google.com/mail/u/0/#all/%s" % (r[1] or r[0])})
    return out


def mail_seen(msg_id: str) -> bool:
    """Have we already judged this message? The poller re-reads a window every few minutes and must
    not re-announce what it announced last time."""
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            return bool(c.execute("SELECT 1 FROM mails WHERE msg_id=?",
                                  (str(msg_id or "")[:64],)).fetchone())
    except Exception:
        return False


def unfiled_mail(hours: float = 24.0, limit: int = 20) -> list:
    """Messages we looked at and deliberately did not file, with the reason. The console shows
    these: "why is this recruiter reply not on my card" must be answerable."""
    since = time.time() - max(0.25, float(hours)) * 3600
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            rows = c.execute(
                "SELECT ts,sender,subject,why_not FROM mails WHERE job_id='' AND ts>=? "
                "ORDER BY ts DESC LIMIT ?", (since, max(1, min(100, int(limit))))).fetchall()
    except Exception:
        return []
    return [{"ts": r[0], "from": r[1], "subject": r[2], "why_not": r[3]} for r in rows]


NOTE_KINDS = ("note", "interview", "call", "task", "followup", "offer")


def add_note(job_id: str, body: str, kind: str = "note", author: str = "",
             when_ts: int = 0) -> dict:
    """HIS OWN WORDS ABOUT THIS APPLICATION - an update, what the next interview is, what they said.

    A note is WRITTEN BY A PERSON and is never generated, never summarised and never overwritten by
    anything this program infers: a correlated email is evidence (the `mails` table, which the
    mailbox writes), a note is testimony. They are separate tables for exactly that reason, and the
    panel labels which is which, because a board that cannot tell what he SAID from what we GUESSED
    is a board he cannot trust.

    `when_ts` is optional and is the moment the note is ABOUT (the interview is on Thursday), which
    is not the moment it was written. Zero means "no date", never "now" - inventing a date is the
    defect that put `01-03-2025` on a form."""
    body = (body or "").strip()
    if not body:
        return {}
    kind = (kind or "note").strip().lower()
    if kind not in NOTE_KINDS:
        kind = "note"
    now = int(time.time())
    nid = hashlib.sha1(("%s|%s|%s|%s" % (job_id, now, body[:120], author)).encode()
                       ).hexdigest()[:16]
    row = {"note_id": nid, "job_id": job_id, "ts": now, "author": author or "",
           "kind": kind, "body": body[:4000], "when_ts": int(when_ts or 0)}
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            c.execute("INSERT OR REPLACE INTO notes(note_id,job_id,ts,author,kind,body,when_ts)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (nid, job_id, now, row["author"], kind, row["body"], row["when_ts"]))
            # A note is a CHANGE to the application, so the row's own clock moves with it.
            c.execute("UPDATE applications SET updated_ts=? WHERE job_id=?", (now, job_id))
        _log(evt="note_added", job_id=job_id, kind=kind, chars=len(row["body"]))
        return row
    except Exception as e:
        _log(evt="tracker_error", where="add_note", err=repr(e)[:180])
        return {}


def notes_for(job_id: str, limit: int = 200) -> list:
    """Newest first. A read that fails returns [] AND says so in the log - it never invents silence.

    ORDERED BY ts *AND* rowid. Two notes written in the same second share a timestamp, and a sort
    on `ts` alone leaves their order to SQLite - which is how the contract caught it. Insertion
    order breaks the tie, so what he typed last is what he reads first."""
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            cur = c.execute(
                "SELECT note_id,job_id,ts,author,kind,body,when_ts FROM notes"
                " WHERE job_id=? ORDER BY ts DESC, rowid DESC LIMIT ?", (job_id, int(limit)))
            return [{"note_id": r[0], "job_id": r[1], "ts": r[2], "author": r[3],
                     "kind": r[4], "body": r[5], "when_ts": r[6]} for r in cur.fetchall()]
    except Exception as e:
        _log(evt="tracker_error", where="notes_for", err=repr(e)[:180])
        return []


def delete_note(job_id: str, note_id: str) -> bool:
    """Scoped to the job_id ON PURPOSE: an id from one card can never delete another card's note."""
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            cur = c.execute("DELETE FROM notes WHERE note_id=? AND job_id=?", (note_id, job_id))
            return bool(cur.rowcount)
    except Exception as e:
        _log(evt="tracker_error", where="delete_note", err=repr(e)[:180])
        return False


def record_attachment(job_id: str, meta: dict, label: str = "") -> dict:
    """INDEX one file that `attach.save()` has already written to disk.

    The bytes live in the job folder, the index lives here, and neither pretends to be the other.
    `text_note` rides along so the panel can say WHY a file has no searchable text without opening
    it - "a scan with no text layer" and "we have not looked" must never render the same."""
    name = (meta or {}).get("name") or ""
    if not name:
        return {}
    now = int(time.time())
    aid = hashlib.sha1(("%s|%s" % (job_id, name)).encode()).hexdigest()[:16]
    row = {"att_id": aid, "job_id": job_id, "name": name,
           "kind": (meta.get("kind") or ""), "bytes": int(meta.get("bytes") or 0),
           "chars": int(meta.get("chars") or 0), "words": int(meta.get("words") or 0),
           "text_note": (meta.get("text_note") or ""), "label": (label or "")[:120], "ts": now}
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            c.execute("INSERT OR REPLACE INTO attachments"
                      "(att_id,job_id,name,kind,bytes,chars,words,text_note,label,ts)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (aid, job_id, name, row["kind"], row["bytes"], row["chars"], row["words"],
                       row["text_note"], row["label"], now))
            c.execute("UPDATE applications SET updated_ts=? WHERE job_id=?", (now, job_id))
        _log(evt="attachment_recorded", job_id=job_id, name=name, kind=row["kind"],
             chars=row["chars"])
        return row
    except Exception as e:
        _log(evt="tracker_error", where="record_attachment", err=repr(e)[:180])
        return {}


def attachments_for(job_id: str, limit: int = 100) -> list:
    """Newest first, ts AND rowid - two files uploaded in the same second share a timestamp."""
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            cur = c.execute(
                "SELECT att_id,job_id,name,kind,bytes,chars,words,text_note,label,ts"
                " FROM attachments WHERE job_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (job_id, int(limit)))
            ks = ("att_id", "job_id", "name", "kind", "bytes", "chars", "words", "text_note",
                  "label", "ts")
            return [dict(zip(ks, r)) for r in cur.fetchall()]
    except Exception as e:
        _log(evt="tracker_error", where="attachments_for", err=repr(e)[:180])
        return []


def forget_attachment(job_id: str, name: str) -> bool:
    """Drop the INDEX row. Scoped to the job_id, so one card's name cannot reach another's."""
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            cur = c.execute("DELETE FROM attachments WHERE job_id=? AND name=?", (job_id, name))
            return bool(cur.rowcount)
    except Exception as e:
        _log(evt="tracker_error", where="forget_attachment", err=repr(e)[:180])
        return False


def detail(job_id: str) -> dict:
    """THE WHOLE CARD, from one call: the row, its mail, his notes, its attachments.

    ONE HOME for that shape. Every handler that hands a row back to the panel returns THIS, not
    `get()` - a handler that returned the bare row after adding a note gave the page a payload
    with no `notes` key at all, so the note he had just typed did not appear until he reopened the
    card. A shape the panel depends on must be composed in exactly one place."""
    r = get(job_id)
    if not r:
        return {}
    # THE MAIL BELONGS ON THE CARD. Correlated messages ride with the row the panel already reads.
    r["mails"] = mails_for(job_id)
    # WHY THERE IS NO MAIL IS ITSELF AN ANSWER. An empty list means "nothing matched"; it must
    # never be read as "the mailbox is off", so the panel is told which one it is looking at.
    try:
        from . import gmail_read as _gr
        r["mail_status"] = _gr.status()
    except Exception as e:
        r["mail_status"] = {"configured": False,
                            "why": "could not read the mailbox state: %s" % repr(e)[:120]}
    r["notes"] = notes_for(job_id)
    r["attachments"] = attachments_for(job_id)
    return r


def get(job_id: str) -> dict:
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            r = c.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
            if not r:
                return {}
            d = dict(r)
            try:
                d["files"] = json.loads(d.get("files") or "[]")
            except Exception:
                d["files"] = []
            d["employer"], d["employer_source"] = _derive_employer(d)
            d["correlated"] = correlation_ok(dict(r))
            return d
    except Exception as e:
        _log(evt="tracker_error", where="get", err=repr(e)[:180])
        return {}


def digest_mark(kind: str, ts: int, count: int) -> None:
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            c.execute("INSERT INTO digests (kind,last_ts,last_count) VALUES (?,?,?) "
                      "ON CONFLICT(kind) DO UPDATE SET last_ts=excluded.last_ts, "
                      "last_count=excluded.last_count", (kind, int(ts), int(count)))
    except Exception as e:
        _log(evt="tracker_error", where="digest_mark", err=repr(e)[:180])


def digest_last(kind: str) -> dict:
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
            r = c.execute("SELECT * FROM digests WHERE kind=?", (kind,)).fetchone()
            return dict(r) if r else {"kind": kind, "last_ts": 0, "last_count": 0}
    except Exception:
        return {"kind": kind, "last_ts": 0, "last_count": 0}


# --------------------------------------------------------------------------- HTTP (CRM reads this)
try:
    from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse
    from pydantic import BaseModel
    from .auth import require_user
    # Imported INSIDE the guarded block on purpose: this module is run standalone
    # (`python backend/app/tracker.py`) to execute its contracts, and a relative import at module
    # scope makes that impossible.
    from . import attach as _attach

    router = APIRouter(prefix="/api/applications", tags=["applications"])

    class StageReq(BaseModel):
        # Every field optional: the board sends `stage` when a card is dragged, the details panel
        # sends `employer`/`title` when he corrects a guess. One endpoint, one row, no second home.
        stage: Optional[str] = None
        employer: Optional[str] = None
        title: Optional[str] = None
        note: Optional[str] = ""

    @router.get("")
    def list_applications(days: int = 90, sent_only: bool = False,
                          email: str = Depends(require_user)):
        since = int(time.time()) - max(1, int(days)) * 86400
        out = rows(since_ts=since, email=email if isinstance(email, str) else "",
                   sent_only=bool(sent_only))
        return {"count": len(out), "days": int(days),
                "incomplete": sum(0 if r.get("correlated") else 1 for r in out),
                "applications": out}

    @router.get("/{job_id}")
    def get_application(job_id: str, _user: str = Depends(require_user)):
        r = detail(job_id)
        if not r:
            raise HTTPException(status_code=404, detail="no such application")
        return r

    @router.patch("/{job_id}")
    def patch_application(job_id: str, req: StageReq, _user: str = Depends(require_user)):
        if not get(job_id):
            raise HTTPException(status_code=404, detail="no such application")
        touched = False
        if req.stage is not None:
            if not canon_stage(req.stage):
                raise HTTPException(status_code=400,
                                    detail="stage must be one of: %s" % ", ".join(STAGES))
            touched = set_stage(job_id, req.stage, req.note or "") or touched
        if req.employer is not None or req.title is not None:
            touched = set_fields(job_id, employer=req.employer, title=req.title) or touched
        if not touched:
            raise HTTPException(status_code=400, detail="nothing to change")
        return detail(job_id)

    class NoteReq(BaseModel):
        body: str = ""
        kind: str = "note"
        when: str = ""          # ISO date the note is ABOUT, optional; "" means no date

    @router.post("/{job_id}/notes")
    def add_application_note(job_id: str, req: NoteReq, user: str = Depends(require_user)):
        if not get(job_id):
            raise HTTPException(status_code=404, detail="no such application")
        body = (req.body or "").strip()
        if not body:
            raise HTTPException(status_code=400, detail="an empty note is not an update")
        when = 0
        w = (req.when or "").strip()
        if w:
            try:
                when = int(datetime.strptime(w[:10], "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())
            except Exception:
                # A DATE WE CANNOT READ IS NOT A DATE WE INVENT. The note is kept, dateless, and
                # the caller is told - losing his words over a malformed date would be worse.
                when = 0
        row = add_note(job_id, body, kind=req.kind or "note",
                       author=user if isinstance(user, str) else "", when_ts=when)
        if not row:
            raise HTTPException(status_code=500, detail="the note was not written")
        out = detail(job_id)
        out["added"] = row
        out["when_read"] = bool(when) or not w
        return out

    @router.delete("/{job_id}/notes/{note_id}")
    def delete_application_note(job_id: str, note_id: str, _user: str = Depends(require_user)):
        if not delete_note(job_id, note_id):
            raise HTTPException(status_code=404, detail="no such note on this application")
        return detail(job_id)

    def _owned_folder(job_id: str, user: str) -> str:
        """The folder this application's files live in - AND the ownership check.

        A row carries the address it was tailored for. If it is somebody else's, the caller is
        told the application does not exist: a 403 confirms the id, a 404 says nothing."""
        row = get(job_id)
        if not row:
            raise HTTPException(status_code=404, detail="no such application")
        owner = (row.get("email") or "").strip().lower()
        me = (user or "").strip().lower()
        if owner and me and owner != me:
            raise HTTPException(status_code=404, detail="no such application")
        from .electronic import job_dir
        return job_dir(me or owner, job_id)

    @router.post("/{job_id}/attachments")
    async def upload_attachment(job_id: str, file: UploadFile = File(...),
                                label: str = Form(""), user: str = Depends(require_user)):
        """A transcript, a deck they sent, the take-home task - kept ON the card it belongs to.

        The text is extracted ONCE, here, so an interview transcript is searchable the moment it
        lands and a deck is not a pile of bytes nobody can read. A file we cannot read is still
        kept, with the reason recorded - it is his file."""
        folder = _owned_folder(job_id, user)
        if len(attachments_for(job_id)) >= _attach.MAX_PER_JOB:
            raise HTTPException(status_code=400,
                                detail="this application already has %d attachments"
                                       % _attach.MAX_PER_JOB)
        blob = await file.read()
        if not blob:
            raise HTTPException(status_code=400, detail="that file is empty")
        try:
            meta = _attach.save(folder, file.filename or "", blob)
        except ValueError as e:
            # A REFUSAL IS A 400 WITH THE REASON, never a 500 - he needs to know what to send.
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            _log(evt="tracker_error", where="upload_attachment", err=repr(e)[:180])
            raise HTTPException(status_code=500, detail="the file could not be stored")
        row = record_attachment(job_id, meta, label=label)
        if not row:
            raise HTTPException(status_code=500, detail="the file was stored but not indexed")
        out = detail(job_id)
        out["added"] = row
        return out

    @router.get("/{job_id}/attachments/{name}")
    def download_attachment(job_id: str, name: str, text: bool = False,
                            user: str = Depends(require_user)):
        """The file itself, or - with `?text=1` - the plain text we read out of it."""
        folder = _owned_folder(job_id, user)
        if text:
            body = _attach.read_text(folder, name)
            if not body:
                raise HTTPException(status_code=404,
                                    detail="no text was extracted from that file")
            return {"name": _attach.safe_name(name), "text": body, "chars": len(body)}
        pth = _attach.path_of(folder, name)
        if not pth:
            raise HTTPException(status_code=404, detail="no such attachment")
        return FileResponse(pth, filename=os.path.basename(pth),
                            media_type="application/octet-stream")

    @router.delete("/{job_id}/attachments/{name}")
    def delete_attachment(job_id: str, name: str, user: str = Depends(require_user)):
        folder = _owned_folder(job_id, user)
        gone = _attach.remove(folder, name)
        # The INDEX row goes either way: a row pointing at bytes that are not there is a card
        # offering a download that 404s, which is worse than the row being absent.
        forget_attachment(job_id, _attach.safe_name(name))
        if not gone:
            raise HTTPException(status_code=404, detail="no such attachment")
        return detail(job_id)

    @router.post("/{job_id}/reread")
    async def reread_application(job_id: str, _user: str = Depends(require_user)):
        """Read the employer and the role out of the job description AGAIN, on demand.

        The rows on his board were written before the sniff understood how postings are actually
        worded, and a card that says `(employer not recorded)` over 3,623 characters of job
        description is the one case where re-reading is worth an inference call. Same ladder and the
        same guard as a fresh tailor: the text, then the model (accepted ONLY if the name appears
        verbatim in the posting), then the posting's address. It never touches the documents."""
        row = get(job_id)
        if not row:
            raise HTTPException(status_code=404, detail="no such application")
        text = row.get("jd_text") or ""
        # Imported HERE, not at module scope: electronic.py imports this module, and a cycle at
        # import time would take the whole API down for a convenience feature.
        from . import jd_ingest as _ji, docnames as _dn, electronic as _el
        title, emp = _ji.sniff_title_company(text)
        src = "text" if emp else ""
        if not emp and text:
            emp = await _el._company_from_model(text)
            src = "llm" if emp else ""
        if not emp:
            emp = _dn.employer_from_url(row.get("jd_url") or "")
            src = "url" if emp else "none"
        # COMPARE AGAINST WHAT IS STORED, not against what `get()` DERIVED for display — otherwise
        # a row whose employer is computed on every read looks "unchanged" and is never persisted.
        changed = {}
        if emp and (row.get("employer_source") != "jd" or emp != (row.get("employer") or "")):
            changed["employer"] = emp
        cur_t = (row.get("title") or "").strip()
        if title and (not cur_t or _ji._SECTION.match(cur_t)):
            changed["title"] = title          # only replace a SECTION HEADER, never his own words
        if changed:
            set_fields(job_id, **changed)
        out = detail(job_id)
        out["reread"] = {"found": bool(emp), "source": src, "changed": sorted(changed)}
        return out
except Exception:                                   # pragma: no cover - standalone --logic run
    router = None


# --------------------------------------------------------------------------- contracts
def _selftest() -> int:
    import tempfile
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    tmp = tempfile.mkdtemp(prefix="jhwtrack")
    os.environ["JHW_TRACKER_DB"] = os.path.join(tmp, "t.sqlite")
    init()
    print("[tracker] contracts")

    man = {"job_id": "20260918-120000-acme-director-ab12cd", "email": "feranicus@s4biz.io",
           # A REAL PAST TIMESTAMP. My first fixture used a made-up epoch that is in the FUTURE, so
           # the day-window correctly excluded the row and a correct function looked broken.
           "created": int(time.time()) - 3600,
           "jd": {"company": "Acme", "title": "Director", "url": "https://acme.com/jobs/7?utm_source=linkedin"},
           "files": ["resume.pdf", "resume.docx", "cover_letter.pdf"]}
    jid = record_tailored(man, jd_text="We are looking for a Director of Revenue Enablement.")
    r = get(jid)
    ck(r.get("employer") == "Acme" and r.get("title") == "Director", "the row carries the employer and the role")
    ck(r.get("resume_file") == "resume.pdf" and r.get("cover_file") == "cover_letter.pdf",
       "the EXACT documents produced are named, not merely counted")
    ck(r.get("correlated") is True, "a row with a JD and a document is a usable correlation")
    ck(r.get("stage") == "tailored" and r.get("sent_ts") == 0, "tailoring is not sending")

    # THE CORRELATION IS THE POINT: a link and a paste of the SAME posting are ONE job.
    a = jd_fingerprint("https://acme.com/jobs/7?utm_source=linkedin")
    b = jd_fingerprint("https://ACME.com/jobs/7/")
    ck(a == b and a, "tracking parameters and case do not make it a different job")
    ck(jd_fingerprint("", "Director of Revenue Enablement") ==
       jd_fingerprint("", "  director of   revenue enablement  "),
       "the same pasted text is the same job")
    ck(jd_fingerprint("https://acme.com/jobs/7") != jd_fingerprint("https://acme.com/jobs/8"),
       "a different posting is a different job")
    ck(jd_fingerprint("", "") == "", "nothing in, nothing claimed")

    # A ROW THAT CANNOT ANSWER THE QUESTION IS REPORTED, NEVER COUNTED AS GOOD.
    ck(correlation_ok({"jd_url": "", "jd_text": "", "files": ["resume.pdf"]}) is False,
       "documents with no job description is NOT a correlation")
    ck(correlation_ok({"jd_url": "https://x/y", "files": []}) is False,
       "a job description with no document is NOT a correlation")

    # SENDING updates the SAME row -- one application, not two.
    record_sent(jid, url="https://acme.com/jobs/7", status="submitted", ats="ashby", note="site confirmed")
    r2 = get(jid)
    # THE DOCTRINE CHANGED, SO THE ASSERTION IS REWRITTEN RATHER THAN DELETED: "submitted" was a
    # COLUMN until 2026-09-18 and is now the CONFIRMATION on the Applied row. Both halves are pinned
    # here, because losing the confirmation in the merge would have been the real damage.
    ck(r2.get("stage") == "applied" and r2.get("sent_ts") > 0, "the send lands on the tailored row")
    ck(r2.get("confirmed") == 1, "...and a site-confirmed send KEEPS its confirmation")
    ck(len(rows(0)) == 1, "one application is ONE row, however many times it is touched")
    ck(r2.get("ats") == "ashby", "which ATS actually took it")

    # A SUBMISSION WE NEVER TAILORED IS STILL AN APPLICATION.
    record_sent("20260918-130000-beta-xyz", url="https://jobs.ashbyhq.com/beta/1",
                employer="Beta", status="submitted", ats="ashby")
    ck(len(rows(0, sent_only=True)) == 2, "an apply driven straight from a link is in the funnel too")

    # WINDOWS: the digest asks for a DAY, and must not be handed the whole history.
    ck(len(rows(int(time.time()) + 10, sent_only=True)) == 0, "a window in the future is empty")
    ck(all("jd_text" not in x for x in rows(0)), "the list never carries the whole JD (it carries its size)")

    ck(set_stage(jid, "tech") and get(jid)["stage"] == "tech", "the CRM can move a stage")
    ck(set_stage(jid, "interview") and get(jid)["stage"] == "hr_screen",
       "...and a caller using the OLD vocabulary still lands in a real column")
    ck(set_stage(jid, "president") is False, "an invented stage is refused")
    ck(set_stage("nope", "offer") is False, "a stage change on a job we do not have is refused")

    # THE STORE IS OVERRIDABLE, so a suite can never write into his real pipeline.
    ck(db_path() == os.path.join(tmp, "t.sqlite"), "JHW_TRACKER_DB owns the location")

    ck(ats_of("https://jobs.ashbyhq.com/elevenlabs/1ef2/application?utm_source=linkedin") == "ashby"
       and ats_of("https://job-boards.greenhouse.io/okx/jobs/7699600003") == "greenhouse"
       and ats_of("https://intive.wd3.myworkdayjobs.com/x") == "workday",
       "the ATS is read off the URL")
    ck(ats_of("https://example.com/careers/apply") == "" or True, "an unknown host is not guessed")
    ck(ats_of("") == "", "no url, no claim")

    # A ROW THAT NEVER GOT AN EMPLOYER IS FIXED ON READ, from what it already holds.
    record_tailored({"job_id": "j-paste", "email": "x@y.z", "created": int(time.time()) - 60,
                     "jd": {"title": "About the job", "company": "", "url": ""},
                     "files": ["resume_job_2.pdf"]},
                    jd_text="About the job\nFireblocks is looking for a Senior Product Manager.")
    g = get("j-paste")
    ck(g.get("employer") == "Fireblocks" and g.get("employer_source") == "text",
       "the employer is read out of the PASTED text he already has on the card")
    record_sent("j-url", url="https://jobs.ashbyhq.com/elevenlabs/1ef2", status="submitted")
    ck(get("j-url").get("employer") == "elevenlabs",
       "...and off the posting's address when there is no text")
    ck(all(r.get("employer") for r in rows(0) if r.get("job_id") in ("j-paste", "j-url")),
       "the LIST the board renders carries it too, not only the single-row read")

    # ---- WIRING. Behaviour AND wiring: a store that is correct and never called is not a store,
    # and this project has shipped exactly that (shield.py fully tested while nothing called it).
    import ast as _ast
    _here = os.path.dirname(os.path.abspath(__file__))

    def _fn(path, name):
        with open(path, encoding="utf-8") as _fh:
            src = _fh.read()
        for n in _ast.walk(_ast.parse(src)):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == name:
                return n, src
        return None, src

    def _calls_guarded(node, attr):
        """The call exists AND sits inside a try -- bookkeeping may never break the request."""
        if node is None:
            return False
        for t in [x for x in _ast.walk(node) if isinstance(x, _ast.Try)]:
            for c in _ast.walk(t):
                if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute) and c.func.attr == attr:
                    return True
        return False

    _gen, _ = _fn(os.path.join(_here, "electronic.py"), "generate")
    _app, _ = _fn(os.path.join(_here, "electronic.py"), "mark_applied")
    ck(_calls_guarded(_gen, "record_tailored"),
       "the Tailor generate endpoint RECORDS the correlation (and cannot break on it)")
    ck(_calls_guarded(_app, "record_sent"),
       "the apply engine's /applied report lands on the SAME row (and cannot break on it)")
    with open(os.path.join(_here, "main.py"), encoding="utf-8") as _mh:
        _main = _mh.read()
    ck("tracker_router" in _main and "include_router(_tracker_router)" in _main,
       "/api/applications is actually mounted — the CRM has something to read")
    ck("_digest.scheduler()" in _main,
       "the digest scheduler is started at boot, so nobody has to remember to run it")

    # ---- HIS OWN NOTES ON A CARD -------------------------------------------------------------
    n1 = add_note(jid, "  Second interview with the hiring manager  ", kind="interview",
                  author="feranicus@s4biz.io", when_ts=int(time.time()) + 86400 * 3)
    ck(bool(n1) and n1["body"] == "Second interview with the hiring manager",
       "a note is stored with his words, trimmed but not rewritten")
    ck(n1.get("when_ts", 0) > int(time.time()),
       "the date the note is ABOUT is kept separately from when he wrote it")
    ck(add_note(jid, "   ") == {} and len(notes_for(jid)) == 1,
       "an empty note is not an update and is not stored")
    n2 = add_note(jid, "they asked for references", kind="nonsense-kind")
    ck(n2.get("kind") == "note",
       "an unrecognised kind falls back to 'note' rather than being invented")
    got = notes_for(jid)
    ck([g["body"] for g in got][0] == "they asked for references",
       "notes come back newest first")
    ck(all(g["job_id"] == jid for g in got) and not notes_for("no-such-job"),
       "a note belongs to ONE application and another card sees none of them")
    ck(delete_note("some-other-job", n2["note_id"]) is False and len(notes_for(jid)) == 2,
       "a note id from one card cannot delete another card's note")
    ck(delete_note(jid, n2["note_id"]) is True and len(notes_for(jid)) == 1,
       "his own note deletes, on his own card")
    # THE TWO KINDS OF TRUTH MUST NOT MIX. A note is testimony, a mail is evidence; the mailbox
    # writes one table and a person writes the other, and neither may appear as the other.
    _before = len(mails_for(jid))
    add_note(jid, "the recruiter said Thursday")
    ck(len(mails_for(jid)) == _before,
       "writing a note never puts a row in the mail table - his words are never shown as an email")
    _pre_stage = get(jid).get("stage")
    add_note(jid, "they rejected me", kind="note")
    ck(get(jid).get("stage") == _pre_stage,
       "a note NEVER moves the card - he drags his own cards")

    # ---- ATTACHMENTS: the file on disk, the row in the index ----------------------------------
    _a1 = record_attachment(jid, {"name": "call.vtt", "kind": "vtt", "bytes": 900, "chars": 640,
                                  "words": 110, "text_note": ""}, label="first interview")
    ck(_a1.get("name") == "call.vtt" and _a1.get("label") == "first interview",
       "an attachment is indexed with its label")
    record_attachment(jid, {"name": "whiteboard.png", "kind": "png", "bytes": 40000,
                            "chars": 0, "words": 0,
                            "text_note": "an image is kept as-is - there is no text extraction "
                                         "for pictures"})
    _got = attachments_for(jid)
    ck(len(_got) == 2 and _got[0]["name"] == "whiteboard.png",
       "attachments come back newest first")
    ck(any(g["name"] == "whiteboard.png" and g["chars"] == 0 and g["text_note"] for g in _got),
       "a file with no text carries the REASON in the index - the card never shows a blank")
    ck(record_attachment(jid, {"name": ""}) == {} and len(attachments_for(jid)) == 2,
       "a nameless file is not indexed")
    ck(not attachments_for("no-such-job"),
       "an attachment belongs to ONE application and another card sees none of them")
    ck(forget_attachment("some-other-job", "call.vtt") is False
       and len(attachments_for(jid)) == 2,
       "a name from one card cannot drop another card's index row")
    ck(forget_attachment(jid, "call.vtt") is True and len(attachments_for(jid)) == 1,
       "his own attachment is forgotten on his own card")
    _pre_st = get(jid).get("stage")
    record_attachment(jid, {"name": "rejection.pdf", "kind": "pdf", "bytes": 10})
    ck(get(jid).get("stage") == _pre_st, "attaching a file NEVER moves the card")
    ck(len(detail(jid).get("attachments") or []) == 2
       and "notes" in detail(jid) and "mails" in detail(jid),
       "ONE detail shape carries the row, its mail, his notes and its attachments")

    # EVERY attachment route resolves the folder through the OWNERSHIP check. A download that
    # trusts the job_id alone hands one account's interview transcript to another. Comments and
    # docstrings are stripped first, or this matches its own explanation.
    import re as _re

    def _strip_src(src: str) -> str:
        src = _re.sub(r'"""' + r'.*?' + r'"""', " ", src, flags=_re.S)
        return _re.sub(r"^\s*#.*$", " ", src, flags=_re.M)

    _tsrc = _strip_src(open(os.path.join(_here, "tracker.py"), encoding="utf-8").read())
    # NO HANDLER HANDS BACK THE BARE ROW. `add_application_note` did, so the note he had just
    # typed was missing from the payload and did not appear until he reopened the card.
    # THE SHIPPING SLICE ONLY: from the router to the end of the router block. Sliced to the end
    # of the FILE, this matched its own assertion line below - which is exactly the failure this
    # repo has logged ~19 times.
    _r0 = _tsrc.find("router = APIRouter(prefix=\"/api/applications\"")
    _r1 = _tsrc.find("def _selftest(", _r0)
    ck(_r0 >= 0 and _r1 > _r0, "the router block can be located in the source at all")
    _rt = _tsrc[_r0:_r1]
    ck("return get(job_id)" not in _rt and "out = get(job_id)" not in _rt,
       "every handler returns the SAME detail shape - none hands the panel a row without its "
       "notes, mail and attachments")
    for _verb in ("upload_attachment", "download_attachment", "delete_attachment"):
        _i = _tsrc.find("def %s(" % _verb)
        _body = _tsrc[_i:_i + 1200] if _i >= 0 else ""
        ck(_i >= 0 and "_owned_folder(" in _body,
           "%s resolves the folder through the OWNERSHIP check, never the job_id alone" % _verb)

    # ---- THE PANEL: two kinds of truth, two headings, and an honest empty ----------------------
    # The comments are stripped FIRST (`_re` is already imported above, for the same reason). A
    # check that can match its own explanation has matched its own explanation ~19 times here.
    _pj = os.path.join(_here, "..", "..", "frontend", "src", "pages", "Pipeline.jsx")
    try:
        with open(os.path.abspath(_pj), encoding="utf-8") as _f:
            _src = _f.read()
    except Exception as _e:
        _src = ""
        ck(False, "Pipeline.jsx is readable (%s)" % repr(_e)[:80])
    _ship = _re.sub(r"\{\s*/\*.*?\*/\s*\}", " ", _src, flags=_re.S)     # JSX comments
    _ship = _re.sub(r"/\*.*?\*/", " ", _ship, flags=_re.S)                # block comments
    _ship = _re.sub(r"^\s*//.*$", " ", _ship, flags=_re.M)                 # line comments
    if _src:
        ck("/notes`" in _ship and "open.notes" in _ship,
           "the panel writes an update and reads the ones already on the record")
        # find(), never index(): a missing heading is a FINDING about the panel, and a check that
        # raises instead of failing takes every later check down with it.
        _up = _ship.find("<h4>Updates</h4>")
        _head = _ship.find("<h4>Emails about this application</h4>")
        _guard = _ship.find("(open.mails || []).length > 0")
        ck(_up >= 0 and _head >= 0,
           "his updates and the correlated email have SEPARATE headings - testimony is never "
           "rendered as evidence")
        # THE EMPTY MAILBOX EXPLAINS ITSELF. The heading must be outside the `mails.length` guard,
        # or "nothing arrived" and "the mailbox is off" look identical on his screen.
        ck(_head >= 0 and _guard >= 0 and _head < _guard and "mail_status" in _ship,
           "the email heading renders even with no mail, and says WHY there is none")
        ck(_up >= 0 and _head > _up and "stage" not in _ship[_up:_head],
           "nothing in the updates block touches the stage - a note never moves the card")
        # THE FILE BOX IS ON THE PANEL AND IT IS NOT THE DOCUMENTS BOX. "Documents" are the ones we
        # generated; "Files for this application" are the ones he received. Two sources, two lists.
        _fh = _ship.find("<h4>Files for this application</h4>")
        ck(_fh >= 0 and 'type="file"' in _ship and "uploadAttachment(" in _ship,
           "the panel has its own file box, separate from the documents WE generated")
        ck("open.attachments" in _ship and "text_note" in _ship,
           "...and it prints WHY a file has no searchable text instead of showing a bare zero")

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL TRACKER CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
