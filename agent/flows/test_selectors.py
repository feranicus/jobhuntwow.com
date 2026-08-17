#!/usr/bin/env python3
"""REPO-WIDE SELECTOR GUARD — the audit found my previous guard covered a third of the surface.

WHY: `r"sign\\s*in"` was UNANCHORED, so Playwright's `get_by_role(name=<regex>)` matched
"Sign in with Google" and `.first` picked it by DOM order. The driver navigated off the ATS and every
page reader then honestly reported an empty Workday page. Hours.

The guard I wrote afterwards read ONE file, matched ONE helper (`_click_role`) and only literal
string arguments. A subagent audit measured its blind spot: 53 such regexes exist across flows/, and
it saw 12. Findings in `workday_wd.py`, `greenhouse.py`, `icims.py`, `linkedin.py`,
`linkedin_search.py`, `workday_gevernova.py` and `vision.py` all sat outside it.

THIS scans EVERY file in flows/ and EVERY helper that resolves a control by NAME, and tests each
regex against a corpus of real adjacent controls. It is the property, not the line.
"""
from __future__ import annotations
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Real control labels that sit NEXT TO the one a selector usually wants, on pages we have measured.
# A regex intended for one of these that also matches another is a silent mis-click.
CORPUS = [
    "Sign in with Google", "Sign in with LinkedIn", "Sign in with Microsoft",
    "Continue with Google", "Apply with LinkedIn", "Apply With LinkedIn",
    "Save for Later", "Submit Another Application", "Next Steps",
    "Back to Job Posting", "Cancel application", "Delete resume.pdf",
    "Sign Out", "My Information", "Review", "Introduce Yourself",
]
# A call site whose pattern deliberately NAMES one of these is exempt: it exists to click it.
INTENTIONAL = re.compile(r"google|linkedin|microsoft|sso|oauth|idp", re.I)
# ARIA roles are the FIRST argument of get_by_role and are not control text.
ROLES = {"button", "link", "textbox", "combobox", "checkbox", "radio", "listbox", "option",
         "heading", "dialog", "tab", "tabpanel", "menuitem", "menu", "row", "cell", "img",
         "group", "region", "list", "listitem", "alert", "status", "switch", "slider",
         "spinbutton", "progressbar", "separator", "table", "columnheader", "rowheader"}
# A file whose whole job is LinkedIn may legitimately match LinkedIn labels.
FILE_INTENT = {"linkedin.py": r"linkedin", "linkedin_search.py": r"linkedin",
               "easyapply.py": r"linkedin"}

# Every helper in this repo that resolves a control by NAME/TEXT.
CALLS = re.compile(
    r"(?:_click_role|_has_role|_click_text|_has_text|_fill_labeled|get_by_role|get_by_text|"
    r"get_by_label|get_by_placeholder|get_by_alt_text|get_by_title)\s*\(", re.I)
# a raw or plain string, or re.compile(r"...")
STR = re.compile(r'r?"((?:[^"\\]|\\.)*)"')


def _strip_comments(src: str) -> str:
    """BOTH syntaxes. These files embed JavaScript, so a `//` comment explaining a removed pattern
    still contains it -- which false-positived my own guard twice."""
    out = []
    for ln in src.splitlines():
        t = ln.strip()
        if t.startswith("#") or t.startswith("//"):
            continue
        out.append(ln)
    return "\n".join(out)


def regexes(src: str):
    """(lineno, pattern, context) for every name-resolving call. Only the args on the SAME logical
    call are considered, so an unrelated string later on the line cannot be blamed."""
    code = _strip_comments(src)
    for m in CALLS.finditer(code):
        # take the argument list up to the matching close paren
        i, depth = m.end(), 1
        while i < len(code) and depth:
            depth += (code[i] == "(") - (code[i] == ")")
            i += 1
        args = code[m.start():i]
        ln = code[:m.start()].count("\n") + 1
        for sm in STR.finditer(args):
            pat = sm.group(1)
            # NOT a name regex: the ARIA ROLE argument. `get_by_role("link", name=...)` -- my first
            # version flagged r'link' six times and every one was the role, not the text.
            if pat.lower() in ROLES:
                continue
            # NOT a pattern: a fragment of a concatenated one (r"^\s*" + escape(x) + r"\s*$").
            if not re.search(r"[a-zA-Z]{3}", pat):
                continue
            if len(pat) < 3:
                continue
            yield ln, pat, args[:160]


def main() -> int:
    fails, checked = [], 0
    for fn in sorted(os.listdir(HERE)):
        if not fn.endswith(".py") or fn.startswith("test_"):
            continue
        src = open(os.path.join(HERE, fn), encoding="utf-8").read()
        # MEASURE THE SHIPPING CODE ONLY. A module's own `_selftest` contains FIXTURES -- lines
        # quoting what a real page looked like, e.g. `get_by_role("button", name="Apply")` copied out
        # of a human recording. Those are evidence, not selectors we resolve against a live page, and
        # flagging them fails a correct file. Same doctrine already applied to the brand gate, the
        # SoM pixel-click check and the vision by-value check: a check must name its subject.
        for marker in ("\ndef _selftest(", "\ndef _logic(", "\nif __name__ =="):
            if marker in src:
                src = src[:src.index(marker)]
                break
        for ln, pat, ctx in regexes(src):
            # a pattern that is not a regex of control text (a CSS selector, a url) is skipped
            if pat.startswith(("[", ".", "#", "http", "input", "button[")):
                continue
            try:
                rx = re.compile(pat, re.I)
            except re.error:
                continue
            checked += 1
            if INTENTIONAL.search(pat) or INTENTIONAL.search(ctx):
                continue
            # `exact=True` makes a bare-string name an EXACT match in Playwright, so it cannot reach
            # an adjacent control. Not a workaround: it is what the API does.
            if "exact=True" in ctx and "re.compile" not in ctx:
                continue
            hits = [c for c in CORPUS if rx.search(c)]
            fi = FILE_INTENT.get(fn)
            if fi:
                hits = [h for h in hits if not re.search(fi, h, re.I)]
            if hits:
                fails.append((fn, ln, pat, hits))

    print(f"scanned {checked} name-resolving regex(es) across flows/*.py")
    if fails:
        print(f"\n[X] {len(fails)} regex(es) can silently resolve to an ADJACENT control:")
        for fn, ln, pat, hits in fails:
            print(f"  {fn}:{ln}\n      r{pat!r}\n      also matches: {hits}")
        print("\nFIX: anchor both ends -- r\"^\\s*<text>\\s*$\" -- or narrow the alternation.")
        return 1
    print("ALL SELECTOR CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
