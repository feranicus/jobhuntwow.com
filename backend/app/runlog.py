"""THE RUN LOG: what the platform actually did, while it is doing it, per run.

"we literally do not have any easy direct logging and monitoring per each time we use the platform
 it needs to have this part of screen ... plus we get per each time we use the platform a txt file
 with logging"

The sibling site has had this for months and it is the difference between a progress bar and a
record: a black console that streams every step as it happens, and a .txt of the whole run kept
beside the artifacts it produced. A progress bar that says "35% writing your documents" for forty
seconds tells you nothing about which model answered, which draft was rejected and why, which
projects were selected, or what it cost.

THREE PROPERTIES, and each of them is the reason for a design choice here:

1. LIVE. The browser polls `/api/electronic/runlog/<run_id>?after=N` while the run is in flight, so
   the lines appear as they happen rather than in one dump at the end. No SSE: this endpoint is
   behind a proxy that buffers, the run is 15-45 seconds, and a poll that cannot half-work beats a
   stream that silently stalls.
2. DURABLE. The same lines are written to the job folder as `run_log.txt`, listed with the DOCX and
   the PDF, so the record outlives the browser tab. The sibling estate learned this one the
   expensive way: an incident tool whose output was one command away from being lost.
3. HONEST. A line is written when something HAPPENS, never in anticipation. "model X rejected the
   draft: THIN 412 chars" is the kind of line that makes a bad run diagnosable; "generating..." is
   the kind that makes it look fine.

IT IS PER USER AND IT IS BOUNDED. Every buffer records its owner, and `read()` refuses a caller who
does not own it -- a run log carries the employer, the role and the model chain, which is nobody
else's business. Buffers expire (TTL) and the whole store is capped, because an in-memory log with
neither is a memory leak with a nice interface.
"""
import json
import os
import threading
import time

MAX_LINES = int(os.environ.get("JHW_RUNLOG_LINES", "600"))      # per run
MAX_RUNS = int(os.environ.get("JHW_RUNLOG_RUNS", "40"))         # live runs kept in memory
TTL_S = int(os.environ.get("JHW_RUNLOG_TTL_S", "3600"))
SERVICE = os.environ.get("SERVICE", "jhw-web")

_runs = {}                       # run_id -> {"owner","lines","started","done","pct","msg"}
_lock = threading.Lock()


def _now():
    return time.time()


def _prune(now=None):
    """Drop expired runs, then the oldest if we are still over the cap. Called under the lock."""
    now = now or _now()
    for k in [k for k, v in _runs.items() if now - v["started"] > TTL_S]:
        _runs.pop(k, None)
    if len(_runs) > MAX_RUNS:
        for k, _v in sorted(_runs.items(), key=lambda kv: kv[1]["started"])[:len(_runs) - MAX_RUNS]:
            _runs.pop(k, None)


def start(run_id, owner="", what=""):
    """Open a buffer. Returns the run_id so a caller can `rid = runlog.start(req.run_id, email)`."""
    rid = str(run_id or "")[:64]
    if not rid:
        return ""
    with _lock:
        _prune()
        _runs[rid] = {"owner": str(owner or ""), "lines": [], "started": _now(),
                      "done": False, "pct": 0, "msg": what or "starting"}
    line(rid, "==== %s ====" % (what or "RUN"))
    return rid


def line(run_id, text):
    """One plain line. Never raises: a log that can break the thing it observes is not worth it."""
    rid = str(run_id or "")[:64]
    if not rid:
        return
    try:
        s = str(text).rstrip()[:2000]
        with _lock:
            r = _runs.get(rid)
            if not r:
                return
            r["lines"].append(s)
            if len(r["lines"]) > MAX_LINES:
                # KEEP THE HEAD AND THE TAIL. Dropping the middle of a long run keeps the start
                # (what was asked for) and the end (what happened), which is what a reader needs.
                keep = MAX_LINES // 2
                r["lines"] = (r["lines"][:keep]
                              + ["[...] %d line(s) dropped to stay inside the buffer" %
                                 (len(r["lines"]) - 2 * keep)]
                              + r["lines"][-keep:])
    except Exception:
        pass


def event(run_id, **fields):
    """A STRUCTURED line, in the same shape the rest of the estate emits: one JSON object, with the
    service and the user on it, so Loki can answer questions about it later. It is printed to the
    event log too, not only to the browser."""
    try:
        fields.setdefault("ts", _now())
        fields.setdefault("service", SERVICE)
        s = json.dumps(fields, default=str)
    except Exception:
        return
    line(run_id, s)
    try:
        from . import observability as _obs
        _obs.emit(**fields)
    except Exception:
        try:
            print(s, flush=True)
        except Exception:
            pass


def progress(run_id, pct, msg):
    """Percent + a sentence, and the sentence says what is HAPPENING, not what is hoped for."""
    try:
        p = max(0, min(100, int(pct)))
    except Exception:
        p = 0
    with _lock:
        r = _runs.get(str(run_id or "")[:64])
        if r:
            r["pct"], r["msg"] = p, str(msg)[:200]
    line(run_id, "PROGRESS: [%d%%] %s" % (p, msg))


def done(run_id, msg="complete"):
    with _lock:
        r = _runs.get(str(run_id or "")[:64])
        if r:
            r["done"], r["pct"], r["msg"] = True, 100, str(msg)[:200]
    line(run_id, "==== %s ====" % msg)


def read(run_id, after=0, owner=None):
    """(lines_since, next_index, state). `owner` is CHECKED when given: this is not public data.

    A missing run is not an error -- the page may poll once before the POST has been handled, or
    long after the buffer expired -- so it answers with an empty list and says `known: false`
    instead of a 404 the UI would have to special-case.
    """
    rid = str(run_id or "")[:64]
    with _lock:
        r = _runs.get(rid)
        if not r:
            return [], int(after or 0), {"known": False, "done": False, "pct": 0, "msg": ""}
        if owner is not None and r["owner"] and str(owner) != r["owner"]:
            return [], int(after or 0), {"known": False, "done": False, "pct": 0,
                                         "msg": "", "refused": True}
        try:
            i = max(0, int(after or 0))
        except Exception:
            i = 0
        out = r["lines"][i:]
        return list(out), i + len(out), {"known": True, "done": r["done"], "pct": r["pct"],
                                         "msg": r["msg"]}


def dump(run_id, header=""):
    """The whole run as text, for the .txt file kept beside the documents."""
    rid = str(run_id or "")[:64]
    with _lock:
        r = _runs.get(rid)
        body = list(r["lines"]) if r else []
    head = []
    if header:
        head = [header, "=" * min(78, max(20, len(header))), ""]
    return "\n".join(head + body) + "\n"


def write_file(run_id, path, header=""):
    """Write the run log beside the artifacts. Returns the path, or "" and NEVER raises: a
    bookkeeping step must not be able to fail a run that produced real documents."""
    try:
        txt = dump(run_id, header)
        if len(txt.strip()) < 2:
            return ""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.replace(tmp, path)
        return path
    except Exception as e:
        try:
            print(json.dumps({"evt": "runlog_unwritable", "path": str(path)[:200],
                              "err": repr(e)[:160], "service": SERVICE}), flush=True)
        except Exception:
            pass
        return ""


# --------------------------------------------------------------------------- self-test
def _selftest():
    import tempfile
    global MAX_LINES, MAX_RUNS, TTL_S
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    _runs.clear()
    rid = start("run-1", "him@example.com", "TAILOR")
    line(rid, "first")
    event(rid, evt="phase", name="jd", status="ok")
    progress(rid, 35, "writing")

    got, nxt, st = read(rid, 0, "him@example.com")
    ck("the owner reads his own run", len(got) == 4 and st["known"], str(len(got)))
    ck("and the progress state comes with it", st["pct"] == 35 and st["msg"] == "writing")
    ck("a structured event is a JSON line in the same shape the estate emits",
       any(l.startswith("{") and '"evt": "phase"' in l for l in got))

    got2, nxt2, _ = read(rid, nxt, "him@example.com")
    ck("polling with the last index returns NOTHING new", got2 == [] and nxt2 == nxt)
    line(rid, "second")
    got3, _n3, _s = read(rid, nxt, "him@example.com")
    ck("and returns exactly the new line after that", got3 == ["second"], str(got3))

    # IT IS NOT PUBLIC DATA.
    got4, _n4, st4 = read(rid, 0, "someone-else@example.com")
    ck("another account is refused, and told nothing about the run",
       got4 == [] and not st4["known"] and st4.get("refused"), str(st4))

    got5, _n5, st5 = read("no-such-run", 0, "him@example.com")
    ck("an unknown run answers `known: false`, never an error", got5 == [] and not st5["known"])

    done(rid, "DONE")
    _g, _n, st6 = read(rid, 0, "him@example.com")
    ck("done() is visible to the poller so it can stop", st6["done"] and st6["pct"] == 100)

    # BOUNDED.
    MAX_LINES = 20
    rid2 = start("run-2", "him@example.com")
    for i in range(200):
        line(rid2, "line %d" % i)
    g, _n, _s = read(rid2, 0, "him@example.com")
    ck("a runaway run cannot grow without limit", len(g) <= MAX_LINES + 2, str(len(g)))
    ck("and the head and the tail survive - the middle is what gets dropped",
       any("line 0" in x for x in g) and any("line 199" in x for x in g)
       and any("dropped" in x for x in g))

    MAX_RUNS = 3
    for i in range(10):
        start("r%d" % i, "him@example.com")
    with _lock:
        n = len(_runs)
    ck("the number of live runs is capped too", n <= MAX_RUNS + 1, str(n))

    TTL_S = 0
    start("expire-me", "him@example.com")
    time.sleep(0.01)
    start("fresh", "him@example.com")
    _g, _n, st7 = read("expire-me", 0, "him@example.com")
    ck("an old buffer expires by itself", not st7["known"])
    TTL_S = 3600

    # THE FILE.
    MAX_LINES = 600
    rid3 = start("run-3", "him@example.com", "TAILOR")
    line(rid3, "a real line")
    p = os.path.join(tempfile.mkdtemp(), "sub", "run_log.txt")
    out = write_file(rid3, p, header="JobHuntWOW run log")
    ck("the log is written beside the documents", out == p and os.path.exists(p))
    txt = open(p, encoding="utf-8").read()
    ck("and carries the header and the lines", "JobHuntWOW run log" in txt and "a real line" in txt)
    # AN UNWRITABLE TARGET, THE SAME WAY ON EVERY PLATFORM. The first version used
    # "/proc/nope/run_log.txt", which is unwritable on Linux and, on Windows, is just a relative
    # path that os.makedirs creates happily as C:\proc\nope -- so the write SUCCEEDED and the
    # check failed on the operator's machine while passing in a Linux sandbox. "Which machine,
    # which interpreter?" is the question this repo keeps paying for. Making the PARENT a FILE
    # raises on both (FileExistsError on Windows, NotADirectoryError on Linux), which is the same
    # idiom llm_meter's self-test already proves portable on his box.
    blocker = os.path.join(tempfile.mkdtemp(), "not-a-directory")
    open(blocker, "w").close()
    bad = write_file(rid3, os.path.join(blocker, "run_log.txt"))
    ck("an unwritable path returns '' and does not raise", bad == "", repr(bad))
    ck("an unknown run writes nothing rather than an empty file",
       write_file("nope", os.path.join(tempfile.mkdtemp(), "x.txt")) == "")

    print("runlog selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
