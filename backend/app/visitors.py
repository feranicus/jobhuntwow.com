#!/usr/bin/env python3
"""VISITORS vs CLIENTS — three buckets, and the browser probe he asked for.

`python backend/app/visitors.py` runs the contracts.

PART 1 — SERVER-SIDE (catches the bots that actually arrive)
    VISITOR   the record carried evidence and nothing contradicted itself
    CLIENT    self-identified bot, OR the record contradicted itself
    UNJUDGED  the record did not carry the fields to look at
Never two buckets. The moment every request must be human-or-robot you start inventing answers for
requests you never measured, and a dashboard that confidently reports a wrong number is worse than
one that admits a gap. Precedence runs ONE way: an address seen once as a client stays a client for
the window, however browser-like its other requests looked.

PART 2 — THE BROWSER PROBE (WebRTC + HTTP/3), which he asked for by name
His description is accurate about what the technique measures. What it CANNOT do is the reason it
is wired as evidence and not as a gate:
  * IT NEVER SEES A SCRAPER. curl, python-requests, Go clients and every scanner run no JavaScript,
    so the probe simply never executes for them. The population it can judge is browsers — real
    ones versus headless/cloud ones.
  * IT ACCUSES REAL PEOPLE. Corporate networks block UDP (no HTTP/3, no WebRTC), Safari and privacy
    extensions restrict ICE candidates, and a VPN is a lawful choice millions make. Treating "no
    HTTP/3" as "bot" would file a corporate laptop as an attacker.
  * THE TURN HALF NEEDS NEW INFRASTRUCTURE. Reading the address a UDP packet physically came from
    requires OUR OWN TURN relay (coturn on the droplet, a new open port, a new attack surface).
    That is a decision to take deliberately, not a line of code to slip in. Not built here; what is
    built is the part that needs no new server.
  * PRIVACY IS A HARD LINE. An IP is personal data (GDPR, CJEU C-582/14 Breyer). The probe compares
    the WebRTC-visible address to the request address ON THE SERVER and stores ONLY the boolean.
    The address the browser revealed is never written to a log, an event or a database.

So: the probe RAISES CONFIDENCE for a real browser and FLAGS automation, it never blocks, and a
missing probe is UNJUDGED — never a bot.
"""
from __future__ import annotations

import os
import sys

# ------------------------------------------------------------------ Tier 2: fetch metadata
SF_SITE, SF_MODE, SF_DEST, SF_CHUA = 1, 2, 4, 8
FETCH_METADATA_BITS = SF_SITE | SF_MODE | SF_DEST
_SF_HEADERS = (("sec-fetch-site", SF_SITE), ("sec-fetch-mode", SF_MODE),
               ("sec-fetch-dest", SF_DEST), ("sec-ch-ua", SF_CHUA))

# `sec-ch-ua` is deliberately OUTSIDE the required set: it is a Chromium-only Client Hint, so
# requiring it would flag every Firefox and Safari user. Recorded as a fourth bit because it is
# free corroboration when present.

FETCH_METADATA_SINCE = {"chrome": 76, "edge": 79, "opera": 63, "firefox": 90, "safari": (16, 4)}
H2_EXPECTED_SINCE = {"chrome": 41, "edge": 79, "opera": 28, "firefox": 36, "safari": (9, 0)}

HV_FROM_CLIENT = "p"      # a proxy that actually saw the client told us
HV_FROM_HOP = "s"         # the local hop only — says NOTHING about the client
HV_CLIENT_HEADER = "x-client-proto"

REASON_HTTP11 = "claims_h2_browser_but_spoke_http11"
REASON_NO_FETCH_METADATA = "claims_modern_browser_but_sent_no_fetch_metadata"
REASON_PROBE_AUTOMATION = "browser_probe_reported_automation"
EXEMPT_PREFIXES = ("/api/", "/.well-known/")


def sf_mask(get_header) -> int:
    """Bitmask of which fetch-metadata headers were PRESENT. Presence, never values: the values
    carry navigation context we have no business keeping next to an address. Never raises."""
    mask = 0
    try:
        for name, bit in _SF_HEADERS:
            if get_header(name) is not None:
                mask |= bit
    except Exception:
        return 0
    return mask


_ENGINES = (("edge", "edg/"), ("opera", "opr/"), ("chrome", "chrome/"), ("chrome", "crios/"),
            ("firefox", "firefox/"), ("firefox", "fxios/"), ("safari", "version/"))
_AUTOMATION = ("headless", "phantomjs", "electron/", "puppeteer", "playwright", "selenium")


def claimed_engine(ua: str):
    """('chrome', 131) or None. ORDER MATTERS: Edge's UA contains `Chrome/` and Opera's contains
    both, so matching Chrome first files every Edge user against the wrong floor."""
    import re
    u = (ua or "").lower()
    if not u or any(t in u for t in _AUTOMATION):
        return None                       # already a client by tier 0; no verdict needed here
    for name, token in _ENGINES:
        i = u.find(token)
        if i < 0:
            continue
        m = re.match(r"(\d+)(?:\.(\d+))?", u[i + len(token):])
        if not m:
            return None
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        return (name, (major, minor) if name == "safari" else major)
    return None


def _at_or_above(name, version, table) -> bool:
    floor = table.get(name)
    if floor is None:
        return False
    if isinstance(floor, tuple):
        v = version if isinstance(version, tuple) else (version, 0)
        return v >= floor
    v = version[0] if isinstance(version, tuple) else version
    return v >= floor


def evaluate(ev) -> tuple:
    """(reasons, determinable) for ONE logged request. Never raises, never enforces."""
    try:
        if not isinstance(ev, dict):
            return (), False
        path = ev.get("path") or ""
        if path.startswith(EXEMPT_PREFIXES):
            return (), False
        if ev.get("bot"):
            return (), False              # counted once already by tier 0
        engine = claimed_engine(ev.get("ua"))
        if engine is None:
            return (), False
        name, version = engine
        reasons, determinable = [], False

        sf = ev.get("sf")
        if sf is not None and _at_or_above(name, version, FETCH_METADATA_SINCE):
            determinable = True
            if not (int(sf) & FETCH_METADATA_BITS):
                reasons.append(REASON_NO_FETCH_METADATA)

        hv, hvs = ev.get("hv"), ev.get("hvs")
        if hv and hvs == HV_FROM_CLIENT and _at_or_above(name, version, H2_EXPECTED_SINCE):
            determinable = True
            if str(hv).startswith("1."):
                reasons.append(REASON_HTTP11)

        # The browser probe, when one arrived for this address. Only a POSITIVE automation verdict
        # counts; a missing or inconclusive probe leaves the record exactly as it was.
        if ev.get("probe") == "automation":
            determinable = True
            reasons.append(REASON_PROBE_AUTOMATION)
        elif ev.get("probe") == "browser":
            determinable = True

        return tuple(reasons), determinable
    except Exception:
        return (), False                  # fail open, always


def count(events) -> dict:
    """Three buckets over a window of logged events."""
    addresses, clients, browsers, unjudged = set(), set(), set(), set()
    why = {}
    for ev in events or []:
        ip = (ev or {}).get("ip")
        if not ip:
            continue
        addresses.add(ip)
        if ev.get("bot"):
            clients.add(ip)
            why.setdefault(ip, ev.get("bot_name") or "self-identified")
            continue
        reasons, determinable = evaluate(ev)
        if reasons:
            clients.add(ip)
            why.setdefault(ip, reasons[0])
        elif determinable:
            browsers.add(ip)
        else:
            unjudged.add(ip)
    browsers -= clients
    unjudged -= clients | browsers
    return {"addresses": len(addresses), "visitors": len(browsers), "clients": len(clients),
            "unjudged": len(unjudged), "why": why}


# ------------------------------------------------------------------ Part 2: the browser probe
PROBE_H3_REQUIRED = os.environ.get("JHW_PROBE_STRICT_H3", "0").strip() == "1"


def judge_probe(report, request_ip: str = "") -> dict:
    """{verdict, signals} from what the page measured. PURE — no I/O, no address stored.

    verdict: 'automation' (it declared itself, or the engine says so) · 'browser' (real browser
    evidence) · 'unjudged' (we cannot tell, which is a real answer).

    The address the browser revealed is compared HERE and then dropped; only the boolean survives.
    """
    r = report if isinstance(report, dict) else {}
    sig = {
        "webdriver": bool(r.get("webdriver")),
        "headless_ua": bool(r.get("headlessUa")),
        "h3": bool(r.get("h3")),                       # the page itself arrived over HTTP/3
        "ice": bool(r.get("ice")),                     # WebRTC produced any candidate at all
        "srflx": bool(r.get("srflx")),                 # ... including a server-reflexive one
        "ip_match": None,
        "plugins": int(r.get("plugins") or 0),
        "langs": int(r.get("langs") or 0),
    }
    seen = str(r.get("publicIp") or "").strip()
    if seen and request_ip:
        sig["ip_match"] = (seen == request_ip)        # the ONLY thing kept from the address

    # A self-declared automation stack is the one certain signal in the whole method.
    if sig["webdriver"] or sig["headless_ua"]:
        return {"verdict": "automation", "signals": sig, "why": "navigator.webdriver / headless UA"}

    # A browser that reached us over HTTP/3 spoke UDP end to end — no TCP-only proxy in the path.
    if sig["h3"] and sig["ice"]:
        return {"verdict": "browser", "signals": sig, "why": "HTTP/3 and WebRTC both worked"}

    # WebRTC reporting an address that is NOT the one the request came from is the classic
    # proxied-browser shape. It is EVIDENCE, not proof: CGNAT and split tunnels do this too.
    if sig["ip_match"] is False:
        return {"verdict": "automation", "signals": sig,
                "why": "WebRTC reported a different address than the request came from"}

    if sig["h3"] and PROBE_H3_REQUIRED:
        return {"verdict": "browser", "signals": sig, "why": "HTTP/3 worked"}

    # No HTTP/3 and no ICE is what a corporate firewall looks like. UNJUDGED, deliberately.
    return {"verdict": "unjudged", "signals": sig,
            "why": "no HTTP/3 and no WebRTC candidates — a corporate network looks exactly like this"}


# ------------------------------------------------------------------ live window (in-process)
# Same reasoning as alerts.py: jhw-web is ONE container, the number must be readable in
# milliseconds, and a counter that needs its own database is a component that rots. Loki keeps the
# forensic history; this keeps the last hours to answer "how many people are using it".
from collections import deque as _deque           # noqa: E402

WINDOW_S = int(os.environ.get("JHW_VISITOR_WINDOW", 86400))
MAX_EVENTS = int(os.environ.get("JHW_VISITOR_MAX", 20000))
PROBE_TTL = int(os.environ.get("JHW_PROBE_TTL", 3600))
_events = _deque()
_probes = {}            # ip -> (verdict, ts).  Booleans only; no revealed address, ever.


def record(ev) -> None:
    """Keep one logged request for the window. Never raises: counting must not cost a request."""
    try:
        import time as _t
        now = _t.time()
        e = dict(ev or {})
        e["_ts"] = now
        pv = _probes.get(e.get("ip"))
        if pv and now - pv[1] <= PROBE_TTL:
            e["probe"] = pv[0]
        _events.append(e)
        while _events and (now - _events[0]["_ts"] > WINDOW_S or len(_events) > MAX_EVENTS):
            _events.popleft()
    except Exception:
        pass


def remember_probe(ip: str, verdict: str) -> None:
    import time as _t
    if ip and verdict:
        _probes[ip] = (verdict, _t.time())
        if len(_probes) > 5000:                    # a flood must not turn counting into the outage
            _probes.clear()


def window(hours: float = 24.0) -> dict:
    import time as _t
    since = _t.time() - max(0.1, float(hours)) * 3600
    evs = [e for e in _events if e.get("_ts", 0) >= since]
    out = count(evs)
    out["hours"] = hours
    out["requests"] = len(evs)
    out["probes"] = len(_probes)
    return out


# ------------------------------------------------------------------ wiring (its own middleware)
# It lives HERE, not in observability.py, for two reasons. One: counting is this module's job, and a
# rule with two homes drifts. Two, measured 2026-09-21: `observability.py` is READ-ONLY on the
# operator's filesystem (locked by an editor), and a feature that cannot be written cannot ship —
# routing around the lock beats waiting for it.
def install(app, session_user_fn=None):
    """Record per-request evidence for the counter. Detection only: never blocks, never raises."""
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from . import observability as obs
    except Exception as e:                       # pragma: no cover
        print('{"evt":"visitors_init","result":"error","err":"%s"}' % repr(e)[:120], flush=True)
        return app

    class _Visitors(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            try:
                resp = await call_next(request)
            except Exception:
                _safe(request, 500)
                raise
            _safe(request, resp.status_code)
            return resp

    def _safe(request, status):
        try:
            ua = request.headers.get("user-agent", "")
            c = obs.classify_ua(ua)
            ip = obs.client_ip(request)
            cp = request.headers.get(HV_CLIENT_HEADER)
            hv = str(cp).split("/")[-1] if cp else str(request.scope.get("http_version", "") or "")
            record({"ip": ip, "path": request.url.path[:200], "status": status,
                    "method": request.method, "ua": ua[:220],
                    "bot": c.get("bot"), "bot_name": c.get("bot_name"),
                    "sf": sf_mask(request.headers.get),
                    "hv": hv, "hvs": HV_FROM_CLIENT if cp else HV_FROM_HOP})
        except Exception:
            pass

    app.add_middleware(_Visitors)
    return app


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("[visitors] three buckets, and what each signal is allowed to prove")
    CH = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    EDGE = CH + " Edg/131.0.0.0"
    FF = "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"
    SAF = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Safari/605.1.15"

    ck(claimed_engine(EDGE) == ("edge", 131), "Edge is Edge, not Chrome (its UA contains both)")
    ck(claimed_engine(CH) == ("chrome", 131), "Chrome is Chrome")
    ck(claimed_engine(FF) == ("firefox", 121), "Firefox")
    ck(claimed_engine(SAF) == ("safari", (16, 3)), "Safari needs (major, minor) — the floor is 16.4")
    ck(claimed_engine("HeadlessChrome/120") is None, "an automation UA gets no engine verdict here")
    ck(claimed_engine("") is None and claimed_engine("curl/8.4") is None, "no engine, no verdict")

    hdr = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    ck(sf_mask(hdr.get) == FETCH_METADATA_BITS, "the three navigation headers set their bits")
    ck(sf_mask({}.get) == 0, "none present is ZERO, which is a measurement")
    ck(sf_mask(lambda n: (_ for _ in ()).throw(RuntimeError("boom"))) == 0, "a raising header reader is 0, never a crash")

    # Tier 2
    good = {"ip": "1.1.1.1", "ua": CH, "sf": FETCH_METADATA_BITS, "path": "/"}
    bad = dict(good, sf=0)
    ck(evaluate(good) == ((), True), "a real Chrome navigation is determinable and clean")
    ck(evaluate(bad)[0] == (REASON_NO_FETCH_METADATA,), "Chrome 131 with NO fetch metadata contradicts itself")
    ck(evaluate(dict(good, sf=SF_SITE))[0] == (), "one header stripped by a middlebox convicts nobody")
    old_ff = {"ip": "1.1.1.1", "path": "/", "sf": 0,
              "ua": "Mozilla/5.0 (X11; Linux x86_64; rv:85.0) Gecko/20100101 Firefox/85.0"}
    ck(evaluate(old_ff) == ((), False), "below the per-engine floor we say NOTHING (Firefox 90)")
    saf163 = {"ip": "1.1.1.1", "path": "/", "sf": 0, "ua": SAF}
    ck(evaluate(saf163) == ((), False), "Safari 16.3 is below 16.4 — no verdict")

    # Tier 3, and the trap
    ck(evaluate(dict(good, hv="1.1", hvs=HV_FROM_HOP))[0] == (),
       "a protocol version from the LOCAL HOP proves nothing about the client")
    ck(evaluate(dict(good, hv="1.1", hvs=HV_FROM_CLIENT))[0] == (REASON_HTTP11,),
       "...but the proxy's own view of the client does")
    ck(evaluate(dict(good, hv="2", hvs=HV_FROM_CLIENT))[0] == (), "h2 is what a browser speaks")

    ck(evaluate({"ip": "1.1.1.1", "path": "/api/applications", "ua": CH, "sf": 0}) == ((), False),
       "/api/ is exempt — a fetch() is not a navigation")
    ck(evaluate({"ip": "1.1.1.1", "path": "/", "ua": "curl/8.4", "bot": True, "sf": 0}) == ((), False),
       "an honest curl is already counted by tier 0; never double-count it")
    ck(evaluate(None) == ((), False) and evaluate("nonsense") == ((), False), "garbage in, no verdict, no crash")

    # Counting, and precedence
    evs = [
        {"ip": "9.9.9.1", "ua": CH, "sf": FETCH_METADATA_BITS, "path": "/"},
        {"ip": "9.9.9.2", "ua": "python-requests/2.31", "bot": True, "bot_name": "python-requests", "path": "/"},
        {"ip": "9.9.9.3", "ua": CH, "sf": 0, "path": "/"},
        {"ip": "9.9.9.4", "ua": "Mozilla/5.0 (Linux; Android 10) AppleWebKit", "path": "/"},
        {"ip": "9.9.9.1", "ua": CH, "sf": 0, "path": "/"},          # same address, contradicts later
    ]
    c = count(evs)
    # MY OWN ARITHMETIC WAS WRONG, NOT THE CODE: the fixture holds THREE clients — the scraper
    # (tier 0), .3 which contradicted itself outright, and .1 which looked clean first and
    # contradicted itself on its second request. Counting .1 is the precedence rule working.
    ck(c["clients"] == 3, "the scraper and BOTH self-contradicting addresses are clients (got %d)" % c["clients"])
    ck(c["visitors"] == 0, "an address that contradicted itself ONCE is not promoted back")
    ck(c["unjudged"] == 1, "a UA we cannot place is UNJUDGED, not a bot")
    ck(c["addresses"] == 4, "and every address is still counted somewhere")

    # The probe
    ck(judge_probe({"webdriver": True})["verdict"] == "automation", "navigator.webdriver is a confession")
    ck(judge_probe({"headlessUa": True})["verdict"] == "automation", "so is a headless build string")
    ck(judge_probe({"h3": True, "ice": True})["verdict"] == "browser", "HTTP/3 + WebRTC = a real browser path")
    ck(judge_probe({"h3": True, "ice": True, "publicIp": "5.5.5.5"}, "5.5.5.5")["verdict"] == "browser",
       "...and the addresses agreeing keeps it a browser")
    j = judge_probe({"h3": False, "ice": True, "srflx": True, "publicIp": "10.0.0.7"}, "5.5.5.5")
    ck(j["verdict"] == "automation", "WebRTC showing a different address than the request is the proxy shape")
    ck("publicIp" not in j["signals"] and "5.5.5.5" not in str(j["signals"]),
       "THE REVEALED ADDRESS IS NEVER KEPT — only the boolean")
    ck(j["signals"]["ip_match"] is False, "...and the boolean is what survives")
    ck(judge_probe({"h3": False, "ice": False})["verdict"] == "unjudged",
       "no UDP at all is a corporate firewall, not an accusation")
    ck(judge_probe({})["verdict"] == "unjudged" and judge_probe(None)["verdict"] == "unjudged",
       "an empty or missing report is unjudged")
    ck(evaluate({"ip": "1.1.1.1", "path": "/", "ua": CH, "probe": "automation"})[0]
       == (REASON_PROBE_AUTOMATION,), "a positive automation probe convicts")
    ck(evaluate({"ip": "1.1.1.1", "path": "/", "ua": CH, "probe": "unjudged"}) == ((), False),
       "an unjudged probe changes nothing")

    print("=" * 62)
    if fails:
        print("[X] %d failed" % len(fails))
        return 1
    print("ALL VISITOR CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
