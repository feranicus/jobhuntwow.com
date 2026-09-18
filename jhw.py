#!/usr/bin/env python3
"""jhw — ONE orchestrator. This file is a LAUNCHER for `agent/jhw.py`, nothing more.

WHY IT IS A LAUNCHER AND NOT A SCRIPT (measured 2026-09-18):

This path held a COPY of agent/jhw.py, and the copy was the one he actually runs
(`PS C:\\...\\jobhuntwow-app> python jhw.py apply ...`). The two drifted, exactly the way this
project's oldest rule says a value with several homes always drifts -- and the STALE one won:

  * the copy had no `git`, `memory` or `digest` verb, so the safepoint that commits the tree before
    and after every apply never ran from the command he types;
  * it did not register the memory / ashby / workday-question / conduct / docker suites, so those
    contracts never ran either;
  * it still printed `python jhw.py adapt <name>` -- a verb that has never existed -- which
    `flows/test_docker.py` catches, and which is how this whole duplicate was found;
  * every fix landed in one copy and had to be remembered into the other. It never was.

`agent/jhw.py` already locates itself (it works whether it is invoked from `agent/` or from the
repo root), so ONE file can serve both places. This launcher forwards argv and the EXIT CODE, and
says plainly when the real orchestrator is missing rather than failing with an import error.

Everything -- verbs, flags, help -- lives in agent/jhw.py. Add nothing here.
"""
import os
import subprocess
import sys

REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent", "jhw.py")


def main() -> int:
    if not os.path.isfile(REAL):
        print("[X] agent/jhw.py is missing next to this launcher (%s)" % REAL)
        print("    That file IS the orchestrator; this one only forwards to it.")
        return 2
    try:
        return subprocess.call([sys.executable, REAL] + sys.argv[1:])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
