#!/usr/bin/env python3
"""CLAUDE.md IS RE-INJECTED ON EVERY TURN — so its size is a correctness property.

MEASURED 2026-09-18: CLAUDE.md had grown to 219 KB / 2,719 lines / 126 sections, which is roughly
55,000 tokens spent before the operator types a word. That is what makes a long session run out of
room and start losing the thread — the operator's words were *"I think you lost your memory"*, and
he was right about the cause. The sibling project hit the identical wall at 646 KB.

THE SPLIT: rules stay in CLAUDE.md, the narrative evidence moved to docs/decisions/, verbatim.

This suite is the thing that keeps it split. A rule written in a markdown file and enforced by
nobody is exactly the defect this repo records over and over (`cap_drop` was "standing policy" and
missing on three of four services), so the budget is a BUILD GATE, not a note.

Run by `python ship.py` (phase 1) and by `python jhw.py apply`. Stdlib only.
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CLAUDE = os.path.join(ROOT, "CLAUDE.md")
HISTORY_DIR = os.path.join(ROOT, "docs", "decisions")
# 40 KB ~= 10k tokens per turn. Generous against today's 28 KB, and far below the 219 KB that
# caused the incident. Raising this number is a decision, not a convenience.
BUDGET = 40_000

FAILS = []


def ck(cond, msg, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + msg + (("   [%s]" % detail) if detail else ""))
    if not cond:
        FAILS.append(msg)


def main() -> int:
    print("[claude.md] the file that is re-injected on every turn")
    src = io.open(CLAUDE, encoding="utf-8").read()
    size = len(src.encode("utf-8"))
    ck(size <= BUDGET, "CLAUDE.md is within its per-turn budget",
       "%d bytes / %d budget (~%dk tokens)" % (size, BUDGET, size // 4000))

    # THE HISTORY MUST EXIST AND BE NAMED, or "nothing was deleted" is a claim with no subject.
    hist = [f for f in sorted(os.listdir(HISTORY_DIR))] if os.path.isdir(HISTORY_DIR) else []
    ck(bool(hist), "docs/decisions/ holds the narrative history", ", ".join(hist)[:80])
    ck("docs/decisions/" in src, "CLAUDE.md POINTS at the history (a reader must be able to find it)")

    # THE RULES THEMSELVES MUST SURVIVE EVERY FUTURE TRIM. These are the ones that were earned the
    # hard way and must be true on every turn; if a trim drops one, this fails by name.
    for must in ("HARDEST RULE — NEVER use the Edit/Write file tools",
                 "HARD RULE — ONE command",
                 "HARD RULE — VERIFY EVERYTHING",
                 "HARD RULE — write files with bash heredoc",
                 "HARD RULE — the engine must never ASK for a fact it already owns",
                 "STANDING ORDER — never stop a run on a stall",
                 "STANDING RULE — BUILD IT",
                 "STANDING RULE — containers",
                 "SETTLED — deploy is DIRECT from the PC",
                 "A write is not done until it has been READ BACK",
                 "ONE HOME",
                 "the WEBSITE",
                 "The answer ladder"):      # a cute .upper()[:12] here asserted "THE ANSWER L"
        ck(must in src, "kept: %s" % must[:58])

    # AND THE HISTORY MUST STILL CONTAIN WHAT WAS MOVED. Spot-check by heading, not by size: a file
    # that exists is not a file that holds the evidence.
    body = ""
    for f in hist:
        p = os.path.join(HISTORY_DIR, f)
        if f.endswith(".md"):
            body += io.open(p, encoding="utf-8").read()
    for moved in ("THE HONEYPOT", "VISION IS RUNG 1 NOW", "THE UNION WAS A RACE",
                  "'ger' PUT ALGERIA", "THE FIRST WORKDAY SUBMISSION",
                  "THE TAILOR CORRELATION", "EVERY APPLICATION PRODUCED"):
        ck(moved in body, "history keeps: %s" % moved)
    ck(len(re.findall(r"(?m)^## ", body)) >= 100,
       "the history still holds the whole narrative",
       "%d sections" % len(re.findall(r"(?m)^## ", body)))

    # ---- THE DOCS MUST NAME COMMANDS THAT EXIST -------------------------------------------------
    # `flows/test_docker.py` §8 enforces this for commands we PRINT at runtime. The markdown had
    # drifted the same way and nobody was checking: CLAUDE.md's own deploy section described
    # `python jhw.py deploy`, and DEPLOY.md offered `push`, `chat`, `diagnose` and `mailcheck` —
    # five verbs that have never existed. A doc that sends the operator to a dead command is the
    # same defect as a diagnostic that does, and it had been there for weeks.
    # docs/decisions/ is EXCLUDED on purpose: it is a record of what was said at the time.
    jhw = io.open(os.path.join(ROOT, "agent", "jhw.py"), encoding="utf-8").read()
    verbs = set(re.findall(r'add_parser\("([a-z-]+)"', jhw))
    ck(len(verbs) > 15, "agent/jhw.py's verb list was read", "%d verbs" % len(verbs))
    docs = [f for f in ("CLAUDE.md", "README.md", "DEPLOY.md", "HANDOVER.md", "ELECTRONIC.md",
                        "agent/README.md", "docs/TAILOR_LOGIC.md", "docs/CICD.md")
            if os.path.exists(os.path.join(ROOT, f))]
    stale, missing = [], []
    for f in docs:
        text = io.open(os.path.join(ROOT, f), encoding="utf-8").read()
        for v in sorted(set(re.findall(r'python (?:agent/)?jhw\.py ([a-z-]+)', text))):
            if v not in verbs:
                stale.append("%s -> `jhw.py %s`" % (f, v))
        for sc in sorted(set(re.findall(r'python ([a-zA-Z_][a-zA-Z0-9_/]*\.py)', text))):
            # RESOLVE AGAINST THE FILE THAT PRINTS IT. `agent/README.md` says `python apply_all.py`
            # and the reader is standing in `agent/` — resolving only against the repo root flagged
            # a correct line, which is the identical false positive test_docker.py §8 once had.
            # THE OPERATOR STANDS IN ONE OF TWO PLACES in this repo: the root (website work) or
            # `agent/` (the apply sandbox). A command is valid if it resolves from the doc's own
            # directory or from either of those — resolving only against the root flagged
            # `python apply_all.py`, which is correct from `agent/`. Same false positive as §8's.
            here = os.path.dirname(os.path.join(ROOT, f))
            if not any(os.path.exists(os.path.join(d, sc))
                       for d in (here, ROOT, os.path.join(ROOT, "agent"))):
                missing.append("%s -> `%s`" % (f, sc))
    ck(not stale, "every `python jhw.py <verb>` in the docs is a real verb", "; ".join(stale)[:140])
    ck(not missing, "every `python <script>.py` in the docs exists", "; ".join(missing)[:140])
    print("=" * 66)
    if FAILS:
        print("[X] %d CLAUDE.md CONTRACT(S) BROKEN" % len(FAILS))
        for f in FAILS:
            print("    " + f)
        return 1
    print("ALL CLAUDE.MD CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
