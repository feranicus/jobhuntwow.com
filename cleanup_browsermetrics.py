#!/usr/bin/env python3
"""
cleanup_browsermetrics.py
=========================

Safely delete Chrome/Chromium `BrowserMetrics` telemetry files (*.pma) that
accumulate in a persistent user-data directory used by automation
(Playwright / Selenium / Puppeteer / Dockerized Chrome).

These .pma files are internal metrics staging dumps. They contain NO profile
data, logins, cookies, history, or settings. Chrome deletes them on a clean
shutdown, but automation runs that are killed/timed out leave them behind, so
they pile up (each is ~4 MB) and can consume tens of GB.

Deleting them is safe. Chrome recreates them as needed.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
Preview only (default, deletes NOTHING):
    python cleanup_browsermetrics.py

Actually delete:
    python cleanup_browsermetrics.py --delete

Point at a different profile folder:
    python cleanup_browsermetrics.py --path "D:\\some\\other\\chrome-data"

Delete files older than N days only (keeps recent ones):
    python cleanup_browsermetrics.py --delete --older-than 7

--------------------------------------------------------------------------
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Default profile location. Change this if your chrome-data lives elsewhere.
DEFAULT_PROFILE = r"C:\Docker SW\chrome-data"

# We only ever touch files inside this subfolder, matching this pattern.
METRICS_SUBDIR = "BrowserMetrics"
FILE_GLOB = "BrowserMetrics-*.pma"


def human(n_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024 or unit == "TB":
            return f"{n_bytes:.2f} {unit}"
        n_bytes /= 1024


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely delete Chrome BrowserMetrics *.pma telemetry files."
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PROFILE,
        help=f"Path to the chrome-data profile folder (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete. Without this flag the script only previews (dry run).",
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=0,
        metavar="DAYS",
        help="Only delete files last modified more than DAYS days ago (0 = all).",
    )
    args = parser.parse_args()

    profile = Path(args.path)
    metrics_dir = profile / METRICS_SUBDIR

    # --- Safety checks -----------------------------------------------------
    if not profile.exists():
        print(f"ERROR: profile folder not found: {profile}")
        return 1
    if not metrics_dir.exists():
        print(f"Nothing to do: no '{METRICS_SUBDIR}' folder inside {profile}")
        return 0
    # Make sure we are really inside a BrowserMetrics folder before deleting.
    if metrics_dir.name != METRICS_SUBDIR:
        print("ERROR: refusing to run — target is not a BrowserMetrics folder.")
        return 1

    cutoff = time.time() - args.older_than * 86400 if args.older_than else None

    # --- Scan --------------------------------------------------------------
    matched = []
    total_bytes = 0
    for f in metrics_dir.glob(FILE_GLOB):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if cutoff is not None and st.st_mtime > cutoff:
            continue  # too recent, keep it
        matched.append(f)
        total_bytes += st.st_size

    print("=" * 60)
    print(f"Profile folder : {profile}")
    print(f"Metrics folder : {metrics_dir}")
    if args.older_than:
        print(f"Filter         : older than {args.older_than} day(s)")
    print(f"Files matched  : {len(matched):,}")
    print(f"Space to free  : {human(total_bytes)}")
    print("=" * 60)

    if not matched:
        print("No matching files. Nothing to delete.")
        return 0

    if not args.delete:
        print("\nDRY RUN — nothing was deleted.")
        print("Re-run with  --delete  to actually remove these files.")
        return 0

    # --- Delete ------------------------------------------------------------
    print("\nDeleting...")
    deleted = 0
    freed = 0
    errors = 0
    for f in matched:
        try:
            size = f.stat().st_size
            f.unlink()
            deleted += 1
            freed += size
            if deleted % 1000 == 0:
                print(f"  ...{deleted:,} deleted ({human(freed)} freed)")
        except OSError as e:
            errors += 1
            if errors <= 10:
                print(f"  skipped {f.name}: {e}")

    print("-" * 60)
    print(f"Done. Deleted {deleted:,} files, freed {human(freed)}.")
    if errors:
        print(f"{errors} file(s) could not be deleted "
              "(likely in use — stop the browser/automation and re-run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
