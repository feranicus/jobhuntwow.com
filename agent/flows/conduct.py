#!/usr/bin/env python3
"""CONDUCT — how this engine behaves TOWARD a target site, and when it must stop and hand over.

Three concerns, all outward-facing:

1. **POLITENESS.** Nothing in this system currently limits how often it touches one employer's ATS.
   Twenty applications to the same Workday tenant in five minutes is indistinguishable from an attack,
   and the consequence is not a bad log line: it is the candidate's e-mail or IP blocked by the very
   companies he is applying to. `allow()` enforces a per-host and a global rate, PERSISTED across runs
   (a limit that resets when the process exits is not a limit).

2. **BOT CHALLENGES.** A Cloudflare interstitial, "unusual traffic", a CAPTCHA or an account-locked
   page currently just look like a page with no controls, and the engine grinds against it. Detecting
   one is easy and the correct response is not to solve it -- it is to STOP and hand the browser to the
   human, who is watching it in noVNC anyway. **We never attempt to defeat a bot check.** Doing so is
   the line between automating a form the candidate is entitled to fill in and attacking a site, and
   this project's whole safety story rests on staying the right side of it.

3. **HUMAN TIMING.** Real people do not fill six fields in 400 ms with identical inter-key delays.
   `pause()` adds bounded, randomised think-time between actions. It is deliberately small: the point
   is not to defeat detection (see above), it is to avoid tripping naive rate heuristics on a form the
   candidate is legitimately completing.

WHAT THIS FILE DELIBERATELY DOES NOT DO: no proxy rotation, no fingerprint churn per request, no
challenge solving, no header spoofing beyond the one browser identity the sandbox already presents.
Those are evasion, and evasion is what turns "filling in a form" into something else.
"""
from __future__ import annotations
import json
import os
import random
import re
import time

OUT = os.getenv("JHW_OUT", "/agent/out")
STATE = os.getenv("JHW_CONDUCT_STATE", os.path.join(OUT, "conduct.json"))

# Per-host: how many applications to ONE employer inside the window. Two is generous for a human.
HOST_MAX = int(os.getenv("JHW_HOST_MAX", "3"))
HOST_WINDOW_S = int(os.getenv("JHW_HOST_WINDOW", str(60 * 60)))          # 1 hour
# Global: how many applications in total inside the window, across every employer.
ALL_MAX = int(os.getenv("JHW_GLOBAL_MAX", "25"))
ALL_WINDOW_S = int(os.getenv("JHW_GLOBAL_WINDOW", str(24 * 60 * 60)))    # 1 day
# Minimum gap between two touches of the SAME host, whatever the counts say.
HOST_GAP_S = int(os.getenv("JHW_HOST_GAP", "90"))

JITTER = os.getenv("JHW_JITTER", "1").lower() not in ("0", "false", "no")


def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)", (url or "").lower())
    return (m.group(1) if m else (url or "").lower()).split("/")[0]


def _load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        # IN-PLACE WRITE, never a rename. `out/` may be a bind-mounted directory and this project has
        # already been bitten by a replaced inode making a file invisible to the container reading it.
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, sort_keys=True)
    except Exception:
        pass


def _prune(hits: list, window: int, now: float) -> list:
    return [t for t in hits if isinstance(t, (int, float)) and now - t < window]


def check(url: str, now: float | None = None, state: dict | None = None) -> dict:
    """May we apply to this URL right now? PURE with respect to the state it is handed.

    Returns {ok, why, wait_s}. `wait_s` is how long until it WOULD be allowed, so a caller can report
    something actionable instead of just refusing."""
    now = time.time() if now is None else now
    d = state if state is not None else _load()
    host = host_of(url)
    hh = _prune((d.get("hosts") or {}).get(host) or [], HOST_WINDOW_S, now)
    allh = _prune(d.get("all") or [], ALL_WINDOW_S, now)
    if hh and now - max(hh) < HOST_GAP_S:
        return {"ok": False, "why": f"only {int(now - max(hh))}s since the last application to "
                                    f"{host}; the minimum gap is {HOST_GAP_S}s",
                "wait_s": int(HOST_GAP_S - (now - max(hh)))}
    if len(hh) >= HOST_MAX:
        oldest = min(hh)
        return {"ok": False, "why": f"{len(hh)} applications to {host} in the last "
                                    f"{HOST_WINDOW_S // 60}min (limit {HOST_MAX})",
                "wait_s": int(HOST_WINDOW_S - (now - oldest))}
    if len(allh) >= ALL_MAX:
        oldest = min(allh)
        return {"ok": False, "why": f"{len(allh)} applications in the last "
                                    f"{ALL_WINDOW_S // 3600}h (global limit {ALL_MAX})",
                "wait_s": int(ALL_WINDOW_S - (now - oldest))}
    return {"ok": True, "why": "", "wait_s": 0}


def record(url: str, now: float | None = None) -> None:
    """Note that we DID apply. Called once per run, after the engine actually starts on the form."""
    now = time.time() if now is None else now
    d = _load()
    host = host_of(url)
    hosts = d.setdefault("hosts", {})
    hosts[host] = _prune(hosts.get(host) or [], HOST_WINDOW_S, now) + [now]
    d["all"] = _prune(d.get("all") or [], ALL_WINDOW_S, now) + [now]
    _save(d)


async def pause(kind: str = "action") -> None:
    """Bounded human-like think-time. Small on purpose — this is politeness, not evasion."""
    if not JITTER:
        return
    lo, hi = {"action": (0.12, 0.45), "field": (0.25, 0.9), "page": (0.8, 2.1),
              "submit": (1.2, 2.6)}.get(kind, (0.1, 0.4))
    import asyncio
    await asyncio.sleep(random.uniform(lo, hi))


# ─────────────────────────────────────────────────────────── bot challenges
# Ordered most-specific first: an account lock is not a bot wall and needs a different message.
CHALLENGES = [
    ("account_locked", r"account (has been )?(locked|disabled|suspended)|too many failed attempts"),
    ("captcha", r"\brecaptcha\b|\bhcaptcha\b|i'?m not a robot|are you a robot|"
                r"select all images|solve the puzzle"),
    ("cloudflare", r"checking if the site connection is secure|cloudflare|"
                   r"enable javascript and cookies to continue|attention required|"
                   r"ray id|cf-browser-verification"),
    ("unusual_traffic", r"unusual traffic|automated queries|suspicious activity|"
                        r"verify you are (a )?human|access denied|"
                        r"your request has been blocked|bot detect"),
    ("rate_limited", r"too many requests|rate limit|slow down and try again|429"),
]
_TITLE_ONLY = re.compile(r"just a moment|attention required|access denied|verify you are", re.I)

DETECT_JS = r"""
() => {
  const t = (document.title || '');
  const b = (document.body ? (document.body.innerText || '') : '');
  return {title: t, text: b.slice(0, 4000), url: location.href,
          // an iframe from a challenge provider is the strongest single signal
          frames: [...document.querySelectorAll('iframe[src]')]
                    .map(f => f.getAttribute('src') || '').slice(0, 12),
          controls: document.querySelectorAll(
            'input:not([type=hidden]),textarea,select,button,[role=button]').length};
}
"""


def classify(title: str, text: str, frames=None, controls: int = 0) -> dict:
    """(kind, evidence) or kind='' . PURE, so it is testable without a browser.

    IT REQUIRES MORE THAN A WORD MATCH. "Access denied" appears in plenty of legitimate copy, and a
    job description can mention CAPTCHA. A challenge page has almost NO interactive controls -- that
    is what makes it a wall -- so a page with a working form is never classified as one, however its
    prose reads. A guard that fires on a real application page would be worse than none."""
    blob = f"{title}\n{text}"
    frames = frames or []
    fhit = next((f for f in frames
                 if re.search(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare|geo\.captcha",
                              f, re.I)), "")
    for kind, rx in CHALLENGES:
        m = re.search(rx, blob, re.I)
        if not m:
            continue
        # A page with a real form is not a wall. An account lock is the exception: it is shown ON a
        # sign-in form, and it is the one case where the controls are still there.
        if controls > 6 and kind != "account_locked" and not fhit:
            continue
        return {"kind": kind, "evidence": m.group(0)[:60], "frame": fhit[:80]}
    if fhit:
        return {"kind": "captcha", "evidence": "a challenge iframe is present", "frame": fhit[:80]}
    if controls <= 2 and _TITLE_ONLY.search(title or ""):
        return {"kind": "cloudflare", "evidence": f"title {title[:40]!r} with no controls",
                "frame": ""}
    return {"kind": "", "evidence": "", "frame": ""}


async def challenge(page, log=print) -> dict:
    """Look at the live page. Returns {kind, evidence, ...} with kind='' when nothing is in the way."""
    try:
        d = await page.evaluate(DETECT_JS)
    except Exception:
        return {"kind": "", "evidence": "", "frame": ""}
    got = classify(d.get("title", ""), d.get("text", ""), d.get("frames"), d.get("controls", 0))
    if got["kind"]:
        log(f"    [conduct] BOT CHALLENGE detected: {got['kind']} — {got['evidence']!r}"
            + (f" (frame {got['frame']})" if got["frame"] else ""))
    return got


_MSG = {
    "captcha": "there is a CAPTCHA on the page. I will not attempt to solve it.",
    "cloudflare": "Cloudflare is holding the page behind a browser check.",
    "unusual_traffic": "the site says the traffic looks automated and has blocked the request.",
    "rate_limited": "the site is rate-limiting us.",
    "account_locked": "the account appears to be locked after failed sign-in attempts.",
}


def handover_text(kind: str, url: str, novnc: str = "http://localhost:9090/vnc.html") -> str:
    """What the human is told. Names the cause, the page, and exactly what to do."""
    what = _MSG.get(kind, "something is blocking the page.")
    todo = ("Open the browser and solve it yourself, then reply `done` and I will carry on."
            if kind in ("captcha", "cloudflare", "unusual_traffic")
            else "Have a look and reply `done` when it is clear, or `skip` to move on.")
    return (f"I have stopped: {what}\n\n{url[:110]}\n\nThe browser is live at {novnc}\n{todo}\n\n"
            f"I will not try to get past it on my own.")


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    NOW = 1_700_000_000.0
    U1 = "https://accenture.wd103.myworkdayjobs.com/x/apply"
    U2 = "https://job-boards.greenhouse.io/okx/jobs/1"

    print("\n[1] a fresh state allows the first application")
    ck(check(U1, NOW, {})["ok"], "nothing recorded -> allowed")

    print("\n[2] the MINIMUM GAP to the same host is enforced")
    st = {"hosts": {host_of(U1): [NOW - 10]}, "all": [NOW - 10]}
    r = check(U1, NOW, st)
    ck(not r["ok"] and "minimum gap" in r["why"], f"10s after the last one -> refused: {r['why']}")
    ck(r["wait_s"] > 0, f"and it says how long to wait: {r['wait_s']}s")
    ck(check(U1, NOW + HOST_GAP_S + 1, st)["ok"], "after the gap -> allowed")

    print("\n[3] the PER-HOST limit, and it does not affect a different employer")
    many = [NOW - 200 * (i + 1) for i in range(HOST_MAX)]
    st = {"hosts": {host_of(U1): many}, "all": list(many)}
    ck(not check(U1, NOW, st)["ok"], f"{HOST_MAX} to one host in the window -> refused")
    ck(check(U2, NOW, st)["ok"], "a DIFFERENT employer is unaffected")

    print("\n[4] the GLOBAL limit")
    allh = [NOW - 60 * (i + 1) for i in range(ALL_MAX)]
    st = {"hosts": {}, "all": allh}
    r = check(U2, NOW, st)
    ck(not r["ok"] and "global limit" in r["why"], f"refused: {r['why']}")
    ck(check(U2, NOW + ALL_WINDOW_S + 1, st)["ok"], "the window expires")

    print("\n[5] the window PRUNES — a limit that never forgets is a permanent ban")
    st = {"hosts": {host_of(U1): [NOW - HOST_WINDOW_S - 10] * 9}, "all": []}
    ck(check(U1, NOW, st)["ok"], "old hits outside the window do not count")

    print("\n[6] a challenge is recognised — and a REAL FORM never is")
    ck(classify("Just a moment...", "Checking if the site connection is secure", [], 1)["kind"]
       == "cloudflare", "a Cloudflare interstitial")
    ck(classify("Verify", "Please complete the reCAPTCHA to continue", [], 2)["kind"] == "captcha",
       "a CAPTCHA by text")
    ck(classify("Apply", "", ["https://www.google.com/recaptcha/api2/anchor?k=x"], 14)["kind"]
       == "captcha", "a CAPTCHA by IFRAME even on a page full of controls")
    ck(classify("Sign In", "Your account has been locked after too many failed attempts",
                [], 9)["kind"] == "account_locked", "an account lock, which IS shown on a form")
    ck(classify("Access Denied", "Your request has been blocked", [], 1)["kind"]
       == "unusual_traffic", "an outright block")
    # the property that matters: a working application page is NEVER a challenge
    ck(classify("Senior Engineer - Apply",
                "We use reCAPTCHA to protect this form. Access denied to unauthorised users. "
                "Please complete all required fields.", [], 24)["kind"] == "",
       "an application page that MENTIONS captcha/access-denied is not classified as a wall")
    ck(classify("Software Engineer", "Experience with rate limit design and bot detection.",
                [], 31)["kind"] == "", "a JOB DESCRIPTION about bot detection is not a wall")
    ck(classify("Apply now", "", [], 18)["kind"] == "", "an ordinary page is not a wall")

    print("\n[7] the handover NAMES the cause and refuses to solve it")
    t = handover_text("captcha", "https://x.example/apply")
    ck("will not" in t.lower() and "novnc" in t.lower() or "9090" in t, "it points at the browser")
    ck("solve it" in t.lower(), "and asks the human to solve it")
    for k in CHALLENGES:
        ck(k[0] in _MSG, f"every challenge kind has a human message: {k[0]}")

    print("\n[8] NOTHING in this module tries to defeat a bot check")
    src = open(__file__, encoding="utf-8").read()
    ship = src[:src.index("def _selftest(")]
    # STRIP DOCSTRINGS *AND* COMMENTS BEFORE A BANNED-WORD SCAN. The module docstring says, in so many
    # words, "no proxy rotation, no fingerprint churn, no challenge solving" -- so scanning the raw
    # text flags the very sentence promising we do not do it. Fourteenth time this project has hit a
    # check aimed at prose instead of code; stripping both is always the fix.
    code = re.sub(r'"""(?:.|\n)*?"""', "", ship)
    code = re.sub(r"#.*$", "", code, flags=re.M).lower()
    ck(len(code) > 3000, f"the stripped slice is still the real module ({len(code)} chars)")
    for banned in ("proxy", "user_agent", "useragent", "2captcha", "anticaptcha",
                   "webdriver", "stealth", "set_extra_http_headers"):
        ck(banned not in code, f"no {banned} in the CODE")
    # "solve" IS allowed -- but only as TEXT shown to the human ("solve it yourself"), never as
    # something this module does. My first version banned the word outright and failed on the very
    # message that hands the problem over. Ask the syntax tree: it may appear in a string constant, it
    # may not appear as an identifier, an attribute or a call.
    import ast as _a
    _t = _a.parse(ship)
    _BAD = ("solve", "captcha", "twocaptcha", "anticaptcha", "deathbycaptcha", "capmonster")
    # AN IMPORT IS A NAME TOO. `from twocaptcha import Solver` introduced a solver and walked straight
    # past a check that looked only at Name/Attribute/def/class. Walk the import nodes as well.
    _imports = [a.name for n in _a.walk(_t) if isinstance(n, _a.Import) for a in n.names] + \
               [(n.module or "") for n in _a.walk(_t) if isinstance(n, _a.ImportFrom)] + \
               [a.name for n in _a.walk(_t) if isinstance(n, _a.ImportFrom) for a in n.names]
    _bad_imp = [i for i in _imports if any(b in (i or "").lower() for b in _BAD)]
    ck(not _bad_imp, f"no solver library is imported ({_bad_imp})")
    _ident = [n for n in _a.walk(_t)
              if (isinstance(n, _a.Name) and "solve" in n.id.lower())
              or (isinstance(n, _a.Attribute) and "solve" in n.attr.lower())
              # AN `async def` IS AsyncFunctionDef, NOT FunctionDef. My first version checked only
              # the sync node, so `async def solve_captcha(...)` walked straight past it -- and the
              # solver you would actually write for a browser is async. Both node types, always.
              # ...and a CLASS is a third node type. `class CaptchaSolver` walked past a check that
              # knew about functions only. Name every node kind that can introduce a name.
              or (isinstance(n, (_a.FunctionDef, _a.AsyncFunctionDef, _a.ClassDef))
                  and "solve" in n.name.lower())]
    ck(not _ident, f"'solve' appears in no identifier, attribute or function ({len(_ident)} found)")
    # NO STRING-CONTENT CHECK HERE. My first attempt required every string containing "solve" to also
    # contain "yourself" -- which fails on the module docstring and on the CAPTCHA regex ("solve the
    # puzzle"), and, worse, failed IDENTICALLY with and without a real solver added. A check that
    # cannot tell the two apart measures nothing. The identifier test above is the property: solving
    # can only happen through a name, and there is no such name.
    ck("solve it yourself" in ship, "the handover tells the human to solve it, which is the only "
                                    "place solving is even mentioned as an action")
    ck(len(ship) > 4000, f"the measured slice is the real module ({len(ship)} chars)")

    print("\n[9] state is persisted, so a limit survives a restart")
    ck("json.dump" in ship and "STATE" in ship, "it writes to disk")
    ck('open(STATE, "w"' in ship, "in place, never via a rename (bind-mount inode)")

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} CONDUCT CONTRACT(S) BROKEN")
        return 1
    print("ALL CONDUCT CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
