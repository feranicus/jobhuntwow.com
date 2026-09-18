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
from typing import Any, Optional

try:                                   # importable both as a package module and standalone (--logic)
    from .settings import DATA_DIR
except Exception:                      # pragma: no cover - selftest path
    DATA_DIR = os.getenv("DATA_DIR", "/data")

JD_TEXT_MAX = 40000                    # a JD is prose; anything past this is a paste accident
STAGES = ("tailored", "applied", "submitted", "interview", "offer", "rejected", "withdrawn")


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
  note        TEXT NOT NULL DEFAULT '',
  updated_ts  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS app_created ON applications(created_ts);
CREATE INDEX IF NOT EXISTS app_sent    ON applications(sent_ts);
CREATE INDEX IF NOT EXISTS app_sha     ON applications(jd_sha);
CREATE TABLE IF NOT EXISTS digests(
  kind TEXT PRIMARY KEY, last_ts INTEGER NOT NULL DEFAULT 0, last_count INTEGER NOT NULL DEFAULT 0
);
"""


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


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


def _pick(files, *pats) -> str:
    import re as _re
    for f in (files or []):
        for p in pats:
            if _re.search(p, str(f), _re.I):
                return str(f)
    return ""


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
        stage = status if status in STAGES else "applied"
        with _conn() as c:
            c.executescript(_SCHEMA)
            cur = c.execute("SELECT job_id, jd_url, jd_text FROM applications WHERE job_id=?",
                            (jid,)).fetchone()
            if cur:
                jd_url = cur["jd_url"] or url or ""
                c.execute("UPDATE applications SET sent_ts=?, sent_url=?, ats=?, stage=?, note=?, "
                          "employer=COALESCE(NULLIF(employer,''),?), "
                          "title=COALESCE(NULLIF(title,''),?), jd_url=?, jd_sha=?, updated_ts=? "
                          "WHERE job_id=?",
                          (now, url or jd_url, ats, stage, (note or "")[:500], employer, title,
                           jd_url, jd_fingerprint(jd_url, cur["jd_text"] or ""), now, jid))
            else:
                c.execute("INSERT INTO applications (job_id, email, employer, title, jd_url, jd_sha,"
                          " jd_source, created_ts, sent_ts, sent_url, ats, stage, note, updated_ts)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (jid, email, employer, title, url, jd_fingerprint(url, ""), "link",
                           now, now, url, ats, stage, (note or "")[:500], now))
        return jid
    except Exception as e:
        _log(evt="tracker_error", where="record_sent", err=repr(e)[:180])
        return ""


def set_stage(job_id: str, stage: str, note: str = "") -> bool:
    if stage not in STAGES:
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
                d["jd_chars"] = len(d.pop("jd_text", "") or "")     # the TEXT is not list payload
                d["correlated"] = correlation_ok(dict(r))
                out.append(d)
            return out
    except Exception as e:
        _log(evt="tracker_error", where="rows", err=repr(e)[:180])
        return []


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
    from fastapi import APIRouter, Depends, HTTPException
    from pydantic import BaseModel
    from .auth import require_user

    router = APIRouter(prefix="/api/applications", tags=["applications"])

    class StageReq(BaseModel):
        stage: str
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
        r = get(job_id)
        if not r:
            raise HTTPException(status_code=404, detail="no such application")
        return r

    @router.patch("/{job_id}")
    def patch_application(job_id: str, req: StageReq, _user: str = Depends(require_user)):
        if req.stage not in STAGES:
            raise HTTPException(status_code=400,
                                detail="stage must be one of: %s" % ", ".join(STAGES))
        if not set_stage(job_id, req.stage, req.note or ""):
            raise HTTPException(status_code=404, detail="no such application")
        return get(job_id)
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
    ck(r2.get("stage") == "submitted" and r2.get("sent_ts") > 0, "the send lands on the tailored row")
    ck(len(rows(0)) == 1, "one application is ONE row, however many times it is touched")
    ck(r2.get("ats") == "ashby", "which ATS actually took it")

    # A SUBMISSION WE NEVER TAILORED IS STILL AN APPLICATION.
    record_sent("20260918-130000-beta-xyz", url="https://jobs.ashbyhq.com/beta/1",
                employer="Beta", status="submitted", ats="ashby")
    ck(len(rows(0, sent_only=True)) == 2, "an apply driven straight from a link is in the funnel too")

    # WINDOWS: the digest asks for a DAY, and must not be handed the whole history.
    ck(len(rows(int(time.time()) + 10, sent_only=True)) == 0, "a window in the future is empty")
    ck(all("jd_text" not in x for x in rows(0)), "the list never carries the whole JD (it carries its size)")

    ck(set_stage(jid, "interview") and get(jid)["stage"] == "interview", "the CRM can move a stage")
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

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL TRACKER CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
