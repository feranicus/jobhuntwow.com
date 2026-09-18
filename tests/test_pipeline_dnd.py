#!/usr/bin/env python3
"""THE PIPELINE BOARD — dragging a card must really move the row.

He asked for it in one line: *"I need to be able to move myself the jobs in pipeline but just moving
them with my mouse from lets say tailored to applied"*.

TWO HALVES, AND BOTH ARE CHECKED HERE:
  1. THE ENDPOINT the drop calls, exercised over REAL HTTP through the app (not a mocked function):
     a legal move lands, an invented stage is refused, an unknown job is 404, and a refusal leaves
     the stored row UNCHANGED — because the board reverts on failure and would otherwise be showing
     a state the database does not hold.
  2. THE WIRING in Pipeline.jsx. I cannot run React here (no node_modules in this sandbox), so this
     asserts the drag CONTRACT is present rather than pretending to have clicked: a draggable card,
     a drop target that calls preventDefault (without it the browser silently refuses every drop),
     an optimistic update, the PATCH, and the revert. Stated as a limit, not hidden.

ALSO PERMANENT: the one-click `rejected` link is gone. It sat on every card next to `→ applied`, and
one mis-click is how a live application landed in Rejected on his own board.

Stdlib + the app's own deps. Writes to a TEMP database, never his pipeline.
"""
import os
import re
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ["JHW_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(prefix="jhwdnd"), "t.sqlite")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="jhwdata"))

FAILS = []


def ck(cond, msg, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + msg + (("   [%s]" % detail) if detail else ""))
    if not cond:
        FAILS.append(msg)


def main() -> int:
    print("[pipeline] drag a card -> the row moves")

    # ---------------------------------------------------------------- 1) the endpoint, over HTTP
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import require_user
    from app import tracker as T

    app.dependency_overrides[require_user] = lambda: "feranicus@s4biz.io"
    c = TestClient(app)
    T.init()
    T.record_tailored({"job_id": "j-drag", "email": "feranicus@s4biz.io",
                       "jd": {"company": "Cisco", "title": "Project Manager",
                              "url": "https://cisco.com/jobs/1"},
                       "files": ["resume_cisco_project-manager.pdf"]}, jd_text="a posting")

    r = c.get("/api/applications?days=365")
    ck(r.status_code == 200 and r.json().get("count") == 1, "the board can read the row", r.status_code)
    ck(r.json()["applications"][0]["stage"] == "tailored", "it starts in Tailored")

    r = c.patch("/api/applications/j-drag", json={"stage": "applied"})
    ck(r.status_code == 200 and r.json().get("stage") == "applied",
       "dropping it on Applied moves it", "%s %s" % (r.status_code, r.json().get("stage")))
    ck(T.get("j-drag")["stage"] == "applied", "...and the STORE agrees (not just the response)")

    r = c.patch("/api/applications/j-drag", json={"stage": "president"})
    ck(r.status_code == 400, "an invented stage is refused", r.status_code)
    ck(T.get("j-drag")["stage"] == "applied",
       "a refused move leaves the row where it was — the board reverts to the truth")

    r = c.patch("/api/applications/nope", json={"stage": "offer"})
    ck(r.status_code == 404, "a card we do not have is 404, never a silent create", r.status_code)

    for st in ("hr_screen", "tech", "task", "manager", "final", "offer", "rejected", "tailored"):
        rr = c.patch("/api/applications/j-drag", json={"stage": st})
        if rr.status_code != 200 or rr.json().get("stage") != st:
            ck(False, "every column on the board is a legal destination", "%s -> %s" % (st, rr.status_code))
            break
    else:
        ck(True, "every column on the board is a legal destination (all nine)")

    # ---------------------------------------------------------------- 2) the wiring in the page
    jsx = open(os.path.join(ROOT, "frontend", "src", "pages", "Pipeline.jsx"), encoding="utf-8").read()
    ck("draggable" in jsx, "the card is draggable")
    ck("onDragStart" in jsx, "picking it up records which card and which column it came from")
    # WITHOUT preventDefault ON DRAGOVER THE BROWSER REFUSES EVERY DROP. This is the one line whose
    # absence makes drag-and-drop look implemented and do nothing at all.
    dragover = re.search(r"onDragOver=\{\(ev\)\s*=>\s*\{([^}]*)\}", jsx)
    ck(bool(dragover) and "preventDefault" in dragover.group(1),
       "the column calls preventDefault on dragOver (without it no drop ever fires)")
    drop = re.search(r"function onDrop\(ev, to\) \{(.+?)\n\}", jsx, re.S)
    ck(bool(drop) and "preventDefault" in drop.group(1) and "move(" in drop.group(1),
       "dropping calls the move")
    mv = re.search(r"async function move\(jobId, to, from\) \{(.+?)\n  \}", jsx, re.S)
    body = mv.group(1) if mv else ""
    ck(bool(mv), "there is ONE move(), used by the drop and by the fallback select")
    ck("setRows" in body and body.index("setRows") < body.index("patchJSON"),
       "the card moves FIRST (optimistic), so the board never waits on a round trip")
    ck("patchJSON" in body, "...and the move is persisted")
    ck("stage: from" in body or "stage: from }" in body,
       "...and REVERTED when the server refuses (a UI must not show what the DB does not hold)")
    ck("STAGES.includes(to)" in body, "a destination outside the board is refused client-side too")
    # THE MIS-CLICK THAT COST HIM A LIVE APPLICATION
    ck('move(r, "rejected")' not in jsx and ">rejected</a>" not in jsx,
       "the one-click `rejected` link is GONE — moving is deliberate now")
    ck("<select" in jsx and "kmove" in jsx,
       "a keyboard/touch path remains (dragging needs a mouse)")

    # ---------------------------------------------------- 2b) THE LIFECYCLE HE ACTUALLY LIVES
    # *"Apply and Submitted is same shit different color. but in the interview process there are at
    # least 3-4-5 stages"*.
    ck("submitted" not in T.STAGES, "Applied and Submitted are ONE column now")
    for st in ("hr_screen", "tech", "task", "manager", "final"):
        if st not in T.STAGES:
            ck(False, "the interview season has its own rounds (%s missing)" % st)
            break
    else:
        ck(True, "the interview season is five real rounds, not one word")
    # THE OLD VOCABULARY MUST KEEP WORKING: the apply engine on his PC still says "submitted".
    ck(T.canon_stage("submitted") == "applied" and T.canon_stage("interview") == "hr_screen",
       "every name we have ever used still lands in a real column")
    ck(T.canon_stage("president") == "", "...and an invented one lands nowhere")
    r = c.patch("/api/applications/j-drag", json={"stage": "interview"})
    ck(r.status_code == 200 and r.json().get("stage") == "hr_screen",
       "a legacy name sent by an older client is accepted and mapped", r.json().get("stage"))
    # AND THE EVIDENCE SURVIVES THE MERGE.
    T.record_sent("j-conf", url="https://jobs.ashbyhq.com/x/1", employer="X", status="submitted")
    conf = T.get("j-conf")
    ck(conf.get("stage") == "applied" and conf.get("confirmed") == 1,
       "a site-confirmed send sits in Applied and KEEPS its confirmation")
    jsx_cols = open(os.path.join(ROOT, "frontend", "src", "pages", "Pipeline.jsx"),
                    encoding="utf-8").read()
    for st in T.STAGES:
        if ('"%s"' % st) not in jsx_cols:
            ck(False, "the board renders every stage the store allows (%s missing)" % st)
            break
    else:
        ck(True, "the board renders every stage the store allows — no orphan column, no lost row")
    ck("r.confirmed ?" in jsx_cols, "...and the confirmation tick is on the card")

    # ---------------------------------------------------------------- 3) CLICK A CARD, SEE THE JOB
    # *"if I click on this job in the pipeline it needs to give me its details such as the job
    # description and when exactly It was created time and full date"*.
    one = c.get("/api/applications/j-drag")
    ck(one.status_code == 200, "a single card can be read", one.status_code)
    d = one.json()
    ck(d.get("jd_text", None) is not None and "a posting" in d.get("jd_text", ""),
       "the details carry the WHOLE job description (the list deliberately does not)")
    ck(isinstance(d.get("created_ts"), int) and d["created_ts"] > 0,
       "...and the exact moment it was created, as a timestamp the page can format")
    for k in ("updated_ts", "sent_ts", "files", "stage", "jd_url", "employer_source"):
        if k not in d:
            ck(False, "the details carry %s" % k)
            break
    else:
        ck(True, "...with sent/updated times, the documents, the stage and where the employer came from")

    # HIS WORD BEATS OUR GUESS.
    # ASSERT THE PROPERTY, NOT A LITERAL: this used to demand `stage == "tailored"`, which broke the
    # moment an earlier section legitimately moved the card. What matters is that editing the
    # employer does not move it.
    before_stage = c.get("/api/applications/j-drag").json().get("stage")
    r = c.patch("/api/applications/j-drag", json={"employer": "Cisco Systems Inc"})
    ck(r.status_code == 200 and r.json().get("employer") == "Cisco Systems Inc",
       "he can correct the employer by hand")
    ck(r.json().get("stage") == before_stage,
       "...without touching the stage it is in", "%s -> %s" % (before_stage, r.json().get("stage")))
    ck(c.patch("/api/applications/j-drag", json={}).status_code == 400,
       "an empty patch changes nothing and says so")

    # RE-READ: the ladder again, on demand, for rows written before the sniff understood postings.
    T.record_tailored({"job_id": "j-blind", "email": "feranicus@s4biz.io",
                       "jd": {"title": "About the job", "company": "", "url": ""},
                       "files": ["resume_about-the-job.pdf"]},
                      jd_text="About the job\nAtera is looking for a Senior Program Manager.")
    rr = c.post("/api/applications/j-blind/reread")
    ck(rr.status_code == 200 and rr.json().get("employer") == "Atera",
       "re-reading a blind card finds the employer in the text it already holds",
       rr.json().get("employer"))
    ck(rr.json().get("reread", {}).get("source") == "text",
       "...and says WHICH rung answered")
    # ASSERT THE WRITE, NOT THE DISPLAY. `get()` also DERIVES an employer from the text on every
    # read, so "the card shows Atera" is true even when nothing was saved — defence in depth hiding
    # the very thing under test, for the fourth time in this project. `employer_source` is "jd" only
    # when the value is STORED; a derivation reports "text".
    after = c.get("/api/applications/j-blind").json()
    ck(after.get("employer") == "Atera" and after.get("employer_source") == "jd",
       "...and it is PERSISTED, so the card stays fixed", after.get("employer_source"))
    ck(c.post("/api/applications/nope/reread").status_code == 404,
       "re-reading a card we do not have is 404")

    # the panel's wiring
    ck("openCard(" in jsx and "onClick={() => { if (!didDrag.current) openCard(id); }}" in jsx,
       "a click opens the card — and a DRAG never counts as a click")
    ck("jd_text" in jsx and "stamp(open.created_ts)" in jsx,
       "the panel shows the job description and the exact creation time")
    ck("timeZoneName" in jsx, "...with the timezone, so 'exactly when' is unambiguous")
    ck("/api/electronic/artifacts/" in jsx, "the documents are downloadable from the panel")
    ck("saveDraft" in jsx and "reread" in jsx, "he can correct it, or make it read the posting again")

    print("=" * 66)
    if FAILS:
        print("[X] %d PIPELINE CONTRACT(S) BROKEN" % len(FAILS))
        for f in FAILS:
            print("    " + f)
        return 1
    print("ALL PIPELINE CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
