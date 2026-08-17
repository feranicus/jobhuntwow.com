#!/usr/bin/env python3
"""MEMORY — one SQLite database: the audit trail, and everything the engine learns from it.

The operator, after the first fully autonomous submission: *"our SW needs to put this in a DB and learn
from it and our LLMs as well. each new job submission and new questions are a learning curve and
experience."* Exactly right, and the run that prompted it is the proof: six free-text questions, one
answered by him on Telegram, one by a model, one from his CV, three from written facts. **Without a
record, the next employer asking the same question starts from zero and wakes him again.**

THREE TABLES, ONE FILE (`out/memory.sqlite`):

  runs      one row per application: url, ats, employer, stage, ok, ms, confirmation url.
  fields    every value that went into a form, and WHERE THE ANSWER CAME FROM
            (recorded / learned / rule / documents / llm / human). This is IMPROVEMENTS.md §4.4.
  answers   the LEARNING store: question -> answer, with the source, how often it has been used, and
            whether it has ever been part of a CONFIRMED submission.

**THE LEARNING SIGNAL IS THE SITE'S OWN CONFIRMATION, NOTHING WEAKER.** An answer is only promoted to
`answers` when the run it belonged to reached `stage=submitted` — i.e. the employer's own page said
"thank you". Remembering an answer from a run that crashed would teach the engine from a form nobody
accepted, which is how a wrong answer becomes permanent. Same doctrine as `_submit()` refusing to claim
success without the confirmation page.

**IT IS THE STORAGE FOR `learned.py`, NOT A SECOND HOME.** `learned.recall/remember/forget/stats`
delegate here, so there is one place where "what did we answer last time" lives. The existing
`out/learned_answers.json` is imported once, automatically, and then left alone. This is the same
pattern that closed the credential dual-keying defect: one owner, every caller delegating, and a test
asserting nobody else touches the store.

**NO CREDENTIAL EVER ENTERS.** `learned.is_secret()` already decides that by SHAPE and by label, and
`remember()` refuses anything it flags — checked here as well, because a store that trusts its callers
is one bad caller away from persisting a password.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import time

OUT = os.getenv("JHW_OUT", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out"))
DB = os.getenv("JHW_MEMORY", os.path.join(OUT, "memory.sqlite"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, url TEXT, ats TEXT, employer TEXT,
  stage TEXT, ok INTEGER DEFAULT 0, ms INTEGER, confirm_url TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL, ts REAL NOT NULL,
  label TEXT NOT NULL, value TEXT, source TEXT NOT NULL, ok INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS answers (
  key TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
  question TEXT NOT NULL, answer TEXT NOT NULL, source TEXT,
  uses INTEGER DEFAULT 0, confirmed INTEGER DEFAULT 0,
  first_ts REAL, last_ts REAL,
  PRIMARY KEY (key, scope));
CREATE INDEX IF NOT EXISTS fields_run ON fields(run_id);
CREATE INDEX IF NOT EXISTS runs_ats ON runs(ats, ok);
"""


def _norm(q: str) -> str:
    """The lookup key — DELEGATED to `learned._norm`, which is its one owner.

    MEASURED 2026-08-17: this function used to have its own rules, and they were WEAKER in two ways
    that matter. It kept digits, and it did not normalise British/American spelling — so
    "legally authorised" (Accenture) and "legally authorized" (Siemens) were two different memories
    and the candidate would be asked the same question at every tenant. `learned._norm` already
    encodes both rules, each added after a real run. Two normalisers means two definitions of "the
    same question", and the looser one silently wins wherever it is called.

    Falls back to a local rule only if that import fails, so a broken import degrades the KEY rather
    than the store."""
    try:
        import learned as LN
        return LN._norm(q)
    except Exception:
        s = re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\(.*?\)", " ", (q or "").lower()))
        return re.sub(r"\s+", " ", s).strip()[:180]


_CONN = [None]


def conn():
    """One connection per process. Never raises: a broken store must not stop an application."""
    if _CONN[0] is not None:
        return _CONN[0]
    try:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        c = sqlite3.connect(DB, timeout=10)
        c.row_factory = sqlite3.Row
        c.executescript(_SCHEMA)
        c.commit()
        _CONN[0] = c
        _import_legacy(c)
        return c
    except Exception as e:                                    # pragma: no cover - disk
        print(f"[memory] unavailable ({type(e).__name__}: {str(e)[:60]}) — running without it",
              flush=True)
        return None


def _import_legacy(c) -> int:
    """Bring `out/learned_answers.json` in ONCE, so nothing already learned is lost.

    Nothing is deleted: the JSON stays where it is. Destroying a store to tidy a format is the worse
    trade, already recorded in this project for a quarantined credential."""
    try:
        if c.execute("SELECT COUNT(*) FROM answers").fetchone()[0]:
            return 0
        p = os.getenv("JHW_LEARNED", os.path.join(OUT, "learned_answers.json"))
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        n = 0
        for scope, items in (d.items() if isinstance(d, dict) else []):
            if not isinstance(items, dict):
                continue
            for q, rec in items.items():
                a = rec.get("answer") if isinstance(rec, dict) else rec
                if not a:
                    continue
                remember(q, str(a), scope=scope,
                         source=(rec.get("source") if isinstance(rec, dict) else "legacy"),
                         confirmed=False)
                n += 1
        if n:
            print(f"[memory] imported {n} previously learned answer(s) from learned_answers.json",
                  flush=True)
        return n
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────── the audit trail
def start_run(url: str, ats: str = "", employer: str = "") -> int:
    c = conn()
    if c is None:
        return 0
    try:
        cur = c.execute("INSERT INTO runs (ts,url,ats,employer,stage) VALUES (?,?,?,?,?)",
                        (time.time(), url, ats, employer, "start"))
        c.commit()
        return int(cur.lastrowid)
    except Exception:
        return 0


def field(run_id: int, label: str, value, source: str, ok: bool = True) -> None:
    """One field, and WHERE THE ANSWER CAME FROM. The source is the point of this table: without it a
    trail says what was sent and not whether a human, a rule or a model decided it."""
    c = conn()
    if c is None or not run_id:
        return
    try:
        v = str(value)
        c.execute("INSERT INTO fields (run_id,ts,label,value,source,ok) VALUES (?,?,?,?,?,?)",
                  (run_id, time.time(), str(label)[:200], v[:4000], str(source)[:24], int(ok)))
        c.commit()
    except Exception:
        pass


def finish(run_id: int, *, stage: str, ok: bool, ms: int = 0, confirm_url: str = "",
           note: str = "", log=print) -> None:
    """Close the run — and, ONLY on a confirmed submission, promote what we learned.

    THE SIGNAL IS THE SITE'S OWN CONFIRMATION. Promoting from a crashed or refused run would teach the
    engine from a form nobody accepted."""
    c = conn()
    if c is None or not run_id:
        return
    try:
        c.execute("UPDATE runs SET stage=?, ok=?, ms=?, confirm_url=?, note=? WHERE id=?",
                  (stage, int(ok), int(ms), confirm_url, note[:500], run_id))
        c.commit()
    except Exception:
        return
    if not (ok and stage == "submitted"):
        return
    try:
        rows = c.execute("SELECT label,value,source FROM fields WHERE run_id=? AND ok=1",
                         (run_id,)).fetchall()
        emp = (c.execute("SELECT employer FROM runs WHERE id=?", (run_id,)).fetchone()
               or {"employer": ""})["employer"]
        n = 0
        for r in rows:
            # Only the ANSWERS are worth learning. A name or an e-mail comes from the profile every
            # time and does not need remembering; a screening question does.
            if r["source"] in ("human", "documents", "llm", "llm_answer", "panel"):
                if remember(r["label"], r["value"], scope=emp or "global", source=r["source"],
                            confirmed=True):
                    n += 1
        if n:
            log(f"    [memory] learned {n} answer(s) from a CONFIRMED submission — the next employer "
                f"asking these will not need you")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────── the learning store
def remember(question: str, answer: str, scope: str = "global", source: str = "human",
             confirmed: bool = False) -> bool:
    """Record an answer. Returns False when it was refused (a credential, or nothing to store)."""
    q, a = (question or "").strip(), (answer or "").strip()
    if not q or not a:
        return False
    try:
        import learned as LN
        if LN.is_secret(q, a):
            return False
    except Exception:
        # FAIL CLOSED on the credential test. If we cannot ask, do not store.
        if re.search(r"password|passwort|secret|token|api[_ -]?key|\bpin\b", q, re.I):
            return False
    c = conn()
    if c is None:
        return False
    k, now = _norm(q), time.time()
    try:
        cur = c.execute("SELECT uses, confirmed FROM answers WHERE key=? AND scope=?", (k, scope))
        row = cur.fetchone()
        if row:
            c.execute("UPDATE answers SET answer=?, source=?, uses=uses+1, "
                      "confirmed=MAX(confirmed,?), last_ts=? WHERE key=? AND scope=?",
                      (a[:4000], source, int(confirmed), now, k, scope))
        else:
            c.execute("INSERT INTO answers (key,scope,question,answer,source,uses,confirmed,"
                      "first_ts,last_ts) VALUES (?,?,?,?,?,1,?,?,?)",
                      (k, scope, q[:300], a[:4000], source, int(confirmed), now, now))
        c.commit()
        return True
    except Exception:
        return False


def recall(question: str, scope: str = "global") -> str:
    """The best answer we have. A CONFIRMED answer for this employer beats a confirmed global one,
    which beats anything unconfirmed — evidence outranks recency."""
    c = conn()
    if c is None:
        return ""
    k = _norm(question)
    if not k:
        return ""
    try:
        rows = c.execute("SELECT answer, scope, confirmed, uses FROM answers WHERE key=?",
                         (k,)).fetchall()
    except Exception:
        return ""
    if not rows:
        k2 = _fuzzy_key(c, k)
        if not k2:
            return ""
        try:
            rows = c.execute("SELECT answer, scope, confirmed, uses FROM answers WHERE key=?",
                             (k2,)).fetchall()
        except Exception:
            return ""
    if not rows:
        return ""
    # A CONFIRMED ANSWER IS REUSABLE ACROSS EMPLOYERS — that is the entire point of learning. His
    # answer about his own career does not change because a different company is asking.
    # EXCEPT when the question is ABOUT the employer ("why do you want to work here", "what do you
    # know about us"): reusing that would send OKX's answer to Alpega, which is worse than asking.
    same = scope or "global"
    usable = [r for r in rows if r["scope"] in (same, "global")]
    if not _about_employer(question):
        usable = usable or [r for r in rows if r["confirmed"]]
    if not usable:
        return ""
    # exact scope first, then confirmed, then most used. Evidence outranks recency.
    usable.sort(key=lambda r: (r["scope"] == same, r["confirmed"], r["uses"]), reverse=True)
    return usable[0]["answer"]


_ABOUT_EMPLOYER = re.compile(
    r"why (do|would) you want to (work|join)|why (us|our company|this company)|"
    r"what (do you know|interests you) about|about (us|our)|"
    r"why are you interested in (us|this|our)", re.I)


def _fuzzy_key(c, k: str) -> str:
    """A near-miss must still hit: an ATS rewords the same question slightly per tenant.

    The threshold lives in `learned.FUZZ` (0.92) — tight enough that unrelated questions do not
    collide, and a hit is LOGGED so a wrong reuse can be traced instead of guessed at. This is a
    LOOKUP, never a write: the stored key is unchanged, so nothing is merged behind our back."""
    try:
        import difflib
        import learned as LN
        best, score = "", 0.0
        for (kk,) in c.execute("SELECT DISTINCT key FROM answers").fetchall():
            # CONTAINMENT IS A HIT, and a ratio cannot see it. Accenture's real question is stored in
            # full ("All individuals employed by Accenture must comply with its Code of Business
            # Ethics. Do you agree?") and asked again as the clause alone — a 53/90-character overlap
            # scores 0.74, well under the threshold, so a ratio ALONE would ask him again forever.
            # The 25-character floor is what stops a short generic key ("yes", "n a") swallowing
            # every question that happens to contain it.
            short, long_ = sorted((k, kk), key=len)
            if len(short) >= 25 and short in long_:
                return kk
            r = difflib.SequenceMatcher(None, k, kk).ratio()
            if r > score:
                best, score = kk, r
        if best and score >= float(getattr(LN, "FUZZ", 0.92)):
            print(f"[memory] fuzzy hit ({score:.2f}): {k[:48]!r} -> {best[:48]!r}", flush=True)
            return best
    except Exception:
        pass
    return ""


def _about_employer(question: str) -> bool:
    """Is this question ABOUT the employer? Then a previous employer's answer must not be reused."""
    return bool(_ABOUT_EMPLOYER.search(question or ""))


def scopes() -> dict:
    """{scope: answers} — what we know, per employer. Never raises."""
    c = conn()
    if c is None:
        return {}
    try:
        return {r["scope"]: r["n"] for r in c.execute(
            "SELECT scope, COUNT(*) AS n FROM answers GROUP BY scope").fetchall()}
    except Exception:
        return {}


def forget(question: str, scope: str = "global") -> bool:
    c = conn()
    if c is None:
        return False
    try:
        cur = c.execute("DELETE FROM answers WHERE key=? AND scope=?", (_norm(question), scope))
        c.commit()
        return cur.rowcount > 0
    except Exception:
        return False


# ─────────────────────────────────────────────────────────── what it enables
def ever_submitted(ats: str) -> bool:
    """Have we EVER had a confirmed submission on this ATS? Powers first-run safety: the first
    application to an unseen ATS should stop at review rather than submit blind."""
    c = conn()
    if c is None:
        return True                     # unknown -> do not add friction we cannot justify
    try:
        n = c.execute("SELECT COUNT(*) FROM runs WHERE ats=? AND ok=1 AND stage='submitted'",
                      (ats,)).fetchone()[0]
        return bool(n)
    except Exception:
        return True


def stats() -> dict:
    c = conn()
    if c is None:
        return {}
    try:
        g = lambda q, *a: c.execute(q, a).fetchone()[0]                            # noqa: E731
        return {
            "runs": g("SELECT COUNT(*) FROM runs"),
            "submitted": g("SELECT COUNT(*) FROM runs WHERE ok=1 AND stage='submitted'"),
            "fields": g("SELECT COUNT(*) FROM fields"),
            "answers": g("SELECT COUNT(*) FROM answers"),
            "confirmed_answers": g("SELECT COUNT(*) FROM answers WHERE confirmed=1"),
            "by_source": {r["source"]: r["n"] for r in c.execute(
                "SELECT source, COUNT(*) n FROM fields GROUP BY source ORDER BY n DESC")},
            "by_ats": {r["ats"]: r["n"] for r in c.execute(
                "SELECT ats, COUNT(*) n FROM runs GROUP BY ats")},
        }
    except Exception:
        return {}


def report(limit: int = 10) -> str:
    """A human-readable receipt: the last N runs, and where each answer came from."""
    c = conn()
    if c is None:
        return "memory unavailable"
    out, s = [], stats()
    out.append(f"{s.get('runs', 0)} run(s), {s.get('submitted', 0)} confirmed submission(s), "
               f"{s.get('answers', 0)} learned answer(s) "
               f"({s.get('confirmed_answers', 0)} confirmed)")
    if s.get("by_source"):
        out.append("answers by source: " + ", ".join(f"{k}={v}" for k, v in s["by_source"].items()))
    try:
        for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            nf = c.execute("SELECT COUNT(*) FROM fields WHERE run_id=?", (r["id"],)).fetchone()[0]
            flag = "OK " if r["ok"] else "-- "
            out.append(f"  {flag}{when}  {(r['ats'] or '?'):11s} {(r['employer'] or '?'):14s} "
                       f"{r['stage']:18s} {nf:>2} field(s)")
        # WHAT IT KNOWS, not just how much. A report that prints only counters cannot answer the
        # question the operator actually asks ("does it know this yet, or will it wake me?"), and a
        # diagnostic that does not name its subject sends the next investigation down the wrong road.
        rows = c.execute("SELECT key, answer, scope, source, confirmed, uses FROM answers "
                         "ORDER BY confirmed DESC, uses DESC, last_ts DESC "
                         "LIMIT 25").fetchall()
        if rows:
            out.append("")
            out.append("WHAT IT WILL ANSWER WITHOUT ASKING YOU:")
            for a in rows:
                mark = "confirmed" if a["confirmed"] else "unconfirmed"
                out.append(f"  [{mark:11s}] {a['scope']:<12s} {a['key'][:46]:<48s}"
                           f" -> {a['answer'][:34]!r}")
                out.append(f"  {'':13s}  from {a['source']}, used {a['uses']}x")
        last = c.execute("SELECT id, employer FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            f = c.execute("SELECT label, value, source FROM fields WHERE run_id=? ORDER BY id",
                          (last["id"],)).fetchall()
            if f:
                out.append("")
                out.append(f"LAST RUN ({last['employer'] or '?'}) — every field and where it came "
                           f"from:")
                for x in f:
                    out.append(f"  {x['source']:<11s} {x['label'][:40]:<42s} = "
                               f"{x['value'][:40]!r}")
        if not s.get("runs") and not s.get("answers"):
            out.append("")
            out.append("Nothing recorded yet. It fills up on the next `python jhw.py apply <job>`;")
            out.append("answers are promoted to REUSABLE only when a site confirms the submission.")
    except Exception as e:
        out.append(f"  (partial: {type(e).__name__}: {str(e)[:60]})")
    return "\n".join(out)


def _selftest() -> int:
    import tempfile
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    global DB
    DB = os.path.join(tempfile.mkdtemp(), "m.sqlite")
    _CONN[0] = None
    os.environ["JHW_LEARNED"] = os.path.join(tempfile.mkdtemp(), "none.json")

    print("\n[1] a run is recorded with every field AND its source")
    rid = start_run("https://job-boards.greenhouse.io/alpega/jobs/1", "greenhouse", "alpega")
    ck(rid > 0, f"run id {rid}")
    for lab, val, src in (("First Name", "Evgeny", "profile"),
                          ("What is your available start date?", "2026-09-20", "rule"),
                          ("What are your salary expectations?", "EUR 150.000", "rule"),
                          ("How does this job align with your career path?",
                           "I am a senior delivery leader...", "human"),
                          ("Largest org you have led?", "A 40-person programme...", "documents")):
        field(rid, lab, val, src)
    s = stats()
    ck(s["fields"] == 5, f"5 fields recorded: {s['fields']}")
    ck(s["by_source"].get("human") == 1 and s["by_source"].get("rule") == 2,
       f"grouped by source: {s['by_source']}")

    print("\n[2] NOTHING is learned until the SITE confirms the submission")
    finish(rid, stage="review", ok=False, ms=1000, log=lambda *a: None)
    ck(stats()["answers"] == 0, "a refused run teaches nothing")
    ck(recall("How does this job align with your career path?", "alpega") == "",
       "and the answer is not recalled")

    print("\n[3] a CONFIRMED submission promotes the answers a human or a model produced")
    rid2 = start_run("https://job-boards.greenhouse.io/okx/jobs/2", "greenhouse", "okx")
    field(rid2, "First Name", "Evgeny", "profile")
    field(rid2, "How does this job align with your career path?", "I am a senior delivery leader"
          " with twenty years in cyber and cloud.", "human")
    field(rid2, "Largest org you have led?", "A 40-person delivery programme.", "documents")
    finish(rid2, stage="submitted", ok=True, ms=89000,
           confirm_url="https://.../confirmation", log=lambda *a: None)
    st = stats()
    ck(st["answers"] == 2, f"only the ANSWERS were learned, not the profile fields: {st['answers']}")
    ck(st["confirmed_answers"] == 2, "and both are marked confirmed")
    ck(recall("How does this job align with your career path?", "okx").startswith("I am a senior"),
       "the human's own words come back")
    ck(recall("how does this job ALIGN with your career path", "okx").startswith("I am a senior"),
       "normalisation is loose about case and punctuation")

    print("\n[4] the next employer asking the same question does not need him")
    ck(recall("How does this job align with your career path?", "some-other-employer")
       .startswith("I am a senior"), "a confirmed answer is reusable across employers")

    print("\n[4b] but an answer ABOUT THE EMPLOYER is never reused for a different one")
    rid3 = start_run("https://job-boards.greenhouse.io/okx/jobs/9", "greenhouse", "okx")
    field(rid3, "Why do you want to work at OKX?", "Because of your derivatives platform.", "human")
    finish(rid3, stage="submitted", ok=True, log=lambda *a: None)
    ck(recall("Why do you want to work at OKX?", "okx").startswith("Because of your"),
       "the same employer gets its own answer back")
    ck(recall("Why do you want to work at OKX?", "alpega") == "",
       "a DIFFERENT employer does not inherit it — that answer would be wrong for them")

    print("\n[5] evidence outranks recency")
    remember("Pick one", "unconfirmed guess", scope="global", source="llm", confirmed=False)
    remember("Pick one", "confirmed truth", scope="acme", source="human", confirmed=True)
    ck(recall("Pick one", "acme") == "confirmed truth", "a confirmed answer wins")

    print("\n[6] NO CREDENTIAL is ever stored")
    for q, a in (("Password", "Xk9#mQ2$vLp8!wZr"), ("Enter your password", "hunter2!"),
                 ("API key", "sk-abcdefghijklmnopqrst")):  # pragma: allowlist secret
        ck(remember(q, a) is False, f"refused: {q}")
    ck(recall("Password") == "", "and nothing is recallable")

    print("\n[7] first-run safety: have we ever submitted to this ATS?")
    ck(ever_submitted("greenhouse") is True, "greenhouse has a confirmed submission")
    ck(ever_submitted("successfactors") is False, "an ATS we have never submitted to")

    print("\n[8] it never raises, whatever the disk does")
    _CONN[0] = None
    old = DB
    DB = "/proc/definitely/not/writable/m.sqlite"
    ck(start_run("x") == 0, "a broken store returns 0 instead of raising")
    field(0, "a", "b", "c")
    finish(0, stage="x", ok=True, log=lambda *a: None)
    ck(recall("anything") == "", "recall is empty, not an exception")
    ck(ever_submitted("greenhouse") is True, "and ever_submitted FAILS OPEN (no unjustified friction)")
    DB = old
    _CONN[0] = None

    print("\n[9] the report is readable")
    r = report()
    ck("confirmed submission" in r and "greenhouse" in r, r.splitlines()[0][:70])

    print("\n[9b] the KEY normaliser has one owner — learned._norm")
    # PINNED DIRECTLY, because the mutation run proved this was only covered by DEFENCE IN DEPTH:
    # restoring memory's own weaker rule still passed, since the fuzzy fallback rescued
    # authorised/authorized on its own. Two normalisers = two definitions of "the same question",
    # and the looser one wins wherever it is called. Assert the OWNERSHIP, not a symptom of it.
    import learned as _LN
    for q in ("Are you legally AUTHORISED to work in Germany?",
              "Workday (accenture.wd103.myworkdayjobs.com) asks: notice period in 3 months?",
              "How does this job align with your career path?"):
        ck(_norm(q) == _LN._norm(q), f"same key as learned._norm for {q[:34]!r}")
    ck(_norm("authorised") == _norm("authorized"), "spelling collapses in the KEY, not by fuzz")
    ck(_norm("notice period in 3 months") == _norm("notice period in 6 months"),
       "a tenant's digits do not split one question into two memories")

    print("\n[10] this module is the ONE store — learned.py must delegate, not duplicate")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned.py"),
               encoding="utf-8").read()
    ship = src[:src.index("def _selftest(")] if "def _selftest(" in src else src
    ck("memory" in ship, "learned.py references the one store")

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} MEMORY CONTRACT(S) BROKEN")
        return 1
    print("ALL MEMORY CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    if "--report" in sys.argv:
        print(report(20))
        raise SystemExit(0)
    print(__doc__)
