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

    for st in ("submitted", "interview", "offer", "rejected", "tailored"):
        rr = c.patch("/api/applications/j-drag", json={"stage": st})
        if rr.status_code != 200 or rr.json().get("stage") != st:
            ck(False, "every column on the board is a legal destination", "%s -> %s" % (st, rr.status_code))
            break
    else:
        ck(True, "every column on the board is a legal destination (all six)")

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
