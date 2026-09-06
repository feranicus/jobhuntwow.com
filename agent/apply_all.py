#!/usr/bin/env python3
"""apply_all.py -- ONE command, every job.

Reads a list of job-posting URLs and runs `python jhw.py apply <url>` for each,
in order, continuing past failures, and prints a summary table at the end.

    python apply_all.py                     # reads userdata/joburls.txt
    python apply_all.py urls.txt            # reads a file you name
    python apply_all.py <url> <url> ...     # or URLs straight on the command line

The list file is plain text: one URL per line. Blank lines and lines starting
with '#' are ignored, so you can park a job without deleting it.

WHY THIS EXISTS: `jhw.py apply` takes exactly one job. Applying to twenty
postings meant twenty commands, which is the "talmud command blob" the standing
rules forbid. This wrapper is the one command; it does nothing clever beyond
sequencing, so every safety property of `jhw.py apply` (presubmit audit,
conduct rate limits, the 3-LLM panel, Telegram escalation) is untouched.

Per-host pacing is already enforced inside the engine by flows/conduct.py; the
GAP below is only a courtesy delay so a long run does not hammer one ATS.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_LIST = HERE / "userdata" / "joburls.txt"
LOG = HERE / "out" / "apply_all.log"
GAP = 20  # seconds between jobs


def read_list(path: Path) -> list[str]:
    """One URL per line; '#' comments and blank lines dropped."""
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def collect(argv: list[str]) -> tuple[list[str], str]:
    """URLs on the command line win; then a named file; then the default list."""
    urls = [a for a in argv if a.startswith("http")]
    if urls:
        return urls, "command line"
    named = [a for a in argv if not a.startswith("-")]
    if named:
        p = Path(named[0])
        if not p.is_absolute():
            p = HERE / p
        return read_list(p), str(p)
    return read_list(DEFAULT_LIST), str(DEFAULT_LIST)


def log(line: str) -> None:
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass  # a bookkeeping failure must never stop an application


def apply_one(url: str) -> tuple[int, str]:
    """Run the engine for one job. Returns (returncode, last meaningful line)."""
    cmd = [sys.executable, str(HERE / "jhw.py"), "apply", url]
    # encoding is mandatory on Windows: the default cp1252 dies on any
    # non-latin1 byte in docker/ssh output and kills the reader thread.
    proc = subprocess.Popen(
        cmd,
        cwd=str(HERE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail = ""
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        s = line.strip()
        if s:
            tail = s
    proc.wait()
    return proc.returncode, tail


def main() -> int:
    urls, source = collect(sys.argv[1:])
    if not urls:
        print(f"No job URLs found (looked in: {source}).")
        print(f"Put one URL per line in {DEFAULT_LIST} and run this again.")
        return 2

    log(f"=== apply_all: {len(urls)} job(s) from {source} ===")
    results: list[tuple[str, int, str]] = []

    for i, url in enumerate(urls, 1):
        log(f"--- [{i}/{len(urls)}] {url}")
        started = time.time()
        try:
            rc, tail = apply_one(url)
        except KeyboardInterrupt:
            log("interrupted by the operator")
            break
        except Exception as exc:  # a crash on one job must not lose the rest
            rc, tail = 1, f"{type(exc).__name__}: {exc}"
        took = time.time() - started
        log(f"--- [{i}/{len(urls)}] rc={rc} in {took:.0f}s :: {tail[:160]}")
        results.append((url, rc, tail))
        if i < len(urls):
            time.sleep(GAP)

    log("")
    log("=== SUMMARY ===")
    ok = 0
    for url, rc, tail in results:
        mark = "OK  " if rc == 0 else "FAIL"
        if rc == 0:
            ok += 1
        log(f"{mark} {url}")
        if rc != 0:
            log(f"      last line: {tail[:160]}")
    log(f"=== {ok}/{len(results)} finished with rc=0 ===")
    log(f"full log: {LOG}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
