"""THE OPERATOR'S CONSOLE: what arrived, what it was, what we did about it, and what it cost.

"You cannot protect something you cannot see." Everything this service knows is already written as
one JSON event per line; until now the only way to look was Grafana on another domain, or a curl
with a session cookie. This module reads that record back and answers, for one window:

    who is arriving (per hostname -- jobhuntwow.com AND jobhw.org, separately)
    are they people or scripts (three buckets, never two: visitor, client, NOT DETERMINABLE)
    what did they ask for (attack-shaped paths, by class, with the worst offenders named)
    what did we do (tarpits, blocks, refusals, and whether enforcement is even armed)
    did anyone hear about it (alerts delivered, alerts suppressed, and why)
    what did the models cost, and who spent it

THE ONE RULE THIS FILE OBEYS. A source that cannot be read reports `readable: false` and a reason,
and every number derived from it is None -- never 0. "I could not look" and "there was nothing"
are the same number and completely different facts, and rendering them identically is the defect
that let a log pipeline report success for a week while shipping an empty archive, and let a
diagnostic read a failed query as innocence three runs in a row.

IT IS READ-ONLY. Nothing here blocks, unblocks, changes a threshold or spends anything. It opens
files, parses JSON, and counts. A console that can act is a second enforcement path with none of
the guardrails of the first.
"""
import json
import os
import time

EVENTS_LOG = os.environ.get("EVENTS_LOG", "")
BEATS_DIR = (os.environ.get("PERSEUS_BEATS")
             or (os.path.join(os.path.dirname(EVENTS_LOG), "perseus_beats") if EVENTS_LOG else ""))
SERVICE = os.environ.get("SERVICE", "jhw-web")
# How much of the tail to read. The shared file carries five services; 8 MB is roughly a day of
# ours and is bounded on purpose -- a console that reads a 2 GB file IS the outage.
MAX_BYTES = int(os.environ.get("JHW_SEC_TAIL_BYTES", str(8 * 1024 * 1024)))
FEED_MAX = int(os.environ.get("JHW_SEC_FEED", "200"))
STALE_BEAT_S = int(os.environ.get("JHW_SEC_STALE_BEAT_S", "900"))


def _sib(name):
    from importlib import import_module
    if __package__:
        return import_module("." + name, __package__)
    return import_module(name)


# --------------------------------------------------------------------------- reading the record
def tail_lines(path, max_bytes=None):
    """The last `max_bytes` of a file as decoded lines. (lines, error_or_empty)."""
    mb = MAX_BYTES if max_bytes is None else max_bytes
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > mb:
                fh.seek(size - mb)
                fh.readline()                     # discard the partial first line
            data = fh.read()
        return data.decode("utf-8", "replace").splitlines(), ""
    except Exception as e:
        return [], "%s: %s" % (type(e).__name__, str(e)[:160])


def read_events(window_h=24.0, service=None):
    """(events, source) for OUR service inside the window. `source` always says what happened."""
    svc = service or SERVICE
    source = {"path": EVENTS_LOG or None, "readable": False, "why": "", "lines_read": 0,
              "ours": 0, "window_h": window_h}
    if not EVENTS_LOG:
        source["why"] = ("EVENTS_LOG is not set in this container, so there is no event file to "
                         "read. Nothing below is a statement about traffic.")
        return [], source
    # READ ONLY WHAT THE WINDOW CAN NEED. The tail was always MAX_BYTES regardless of `hours`, so
    # a 1-hour view parsed the same 8 MB as a 7-day one: ~1.5s of CPU and ~100MB of dicts per
    # request, on a 1 GB / 1 CPU container, refreshed every 30s per open tab. Scale with the
    # window, keep the ceiling, and never go below 256 KB so a short window still has context.
    want = int(min(MAX_BYTES, max(256 * 1024, MAX_BYTES * (window_h / 24.0))))
    lines, err = tail_lines(EVENTS_LOG, want)
    if err:
        source["why"] = ("the event file could not be read (%s). Nothing below is a statement "
                         "about traffic." % err)
        return [], source
    cutoff = time.time() - window_h * 3600.0
    evs = []
    for ln in lines:
        if not ln or ln[0] != "{":
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        # OURS MEANS OURS. `service` absent used to count as ours, on a file five projects append
        # to: any writer that omits the field inflated every number on this page.
        if d.get("service") != svc:
            continue
        try:
            if float(d.get("ts") or 0) < cutoff:
                continue
        except Exception:
            continue
        evs.append(d)
    source.update(readable=True, lines_read=len(lines), ours=len(evs))
    return evs, source


# --------------------------------------------------------------------------- the state words
def sidecar_state(now=None):
    """active | stale | not installed | unverifiable -- plus the beat itself when there is one.

    Every one of those words exists because the alternative is a page that says 0 attacks when the
    truth is that nothing has been running. "No attacks" and "not running" are the same number.
    """
    now = now or time.time()
    if not BEATS_DIR:
        return "unverifiable", {}, "no beats directory is configured (PERSEUS_BEATS)"
    p = os.path.join(BEATS_DIR, "%s.json" % SERVICE)
    try:
        with open(p, encoding="utf-8") as fh:
            b = json.load(fh)
    except FileNotFoundError:
        return "not installed", {}, "no heartbeat file at %s" % p
    except Exception as e:
        return "unverifiable", {}, "the heartbeat file could not be read: %r" % (e,)
    age = now - float(b.get("ts") or 0)
    if age > STALE_BEAT_S:
        return "stale", b, "the last heartbeat is %.0f minutes old" % (age / 60.0)
    return "active", b, "heartbeat %.0fs ago, cycle %s" % (age, b.get("cycle"))


def enforcement_state(beat, sidecar):
    """unknown | none | armed | empty | active. Never `none` for something we did not measure."""
    if sidecar in ("not installed", "unverifiable"):
        return "unknown", "the sidecar's own state could not be read, so enforcement is unknown"
    if not beat:
        return "unknown", "no heartbeat carried an enforcement flag"
    if not beat.get("enforcing"):
        return "none", "the sidecar is running in DETECTION ONLY (PERSEUS_ENFORCE=0)"
    if beat.get("blocked"):
        return "active", "%s address(es) are being refused right now" % beat.get("blocked")
    if beat.get("watching"):
        return "armed", "enforcement is on and %s address(es) are being watched" % beat.get("watching")
    return "empty", "enforcement is on and nothing currently qualifies"


def alerting_state(evs):
    """active | off | unknown. Configuration ALONE is not evidence of delivery."""
    try:
        notify = _sib("notify")
        configured = bool(notify.telegram_configured() if hasattr(notify, "telegram_configured")
                          else (os.environ.get("BOT_TOKEN") and os.environ.get("ALERT_TG_CHAT")))
    except Exception:
        configured = bool(os.environ.get("BOT_TOKEN") and os.environ.get("ALERT_TG_CHAT"))
    fired = sum(1 for e in evs if e.get("evt") == "security_alert")
    suppressed = sum(1 for e in evs if e.get("evt") in ("alert_suppressed", "usage_suppressed"))
    # DELIVERED IS NOT FIRED. alerts.fire() writes evt=security_alert and only THEN calls the
    # sender, discarding its result -- so counting those lines and calling it "active" reports a
    # healthy alert channel while every Telegram message failed. notify writes its own
    # evt=alert_delivery carrying the booleans; that is the only evidence of delivery there is.
    deliveries = [e for e in evs if e.get("evt") == "alert_delivery"]
    delivered = sum(1 for e in deliveries if e.get("telegram") or e.get("email"))
    failed = len(deliveries) - delivered
    if not configured:
        return ("off", fired, suppressed,
                "no Telegram bot token and chat id are configured in this container")
    if delivered:
        return ("active", fired, suppressed,
                "%d alert(s) fired, %d suppressed, %d DELIVERED in this window"
                % (fired, suppressed, delivered))
    if failed:
        return ("off", fired, suppressed,
                "%d alert(s) were raised and NONE were delivered - the channel is configured and "
                "not working" % failed)
    if fired or suppressed:
        return ("unknown", fired, suppressed,
                "%d alert(s) fired and %d suppressed, but no delivery was recorded either way"
                % (fired, suppressed))
    return ("unknown", fired, suppressed,
            "configured, but nothing has been sent in this window, so delivery is unproven here")


# --------------------------------------------------------------------------- the counting
_OURS = [None]          # the route predicate, built once


def _ours():
    """`is_ours(path) -> bool` for OUR declared pages, from the one place that mirrors App.jsx.

    READ THE SIGNATURE. perseus_client.probe_shape takes a CALLABLE here, not a bool; passing
    False raised TypeError inside a try/except and every path came back unclassified -- an attack
    count of zero produced by a broken call, which is the exact defect class this console exists to
    refuse. It is now proven by the self-test instead of assumed.
    """
    if _OURS[0] is None:
        try:
            routes = set(_sib("spa_guard").CLIENT_ROUTES)
        except Exception:
            routes = {"/", "/login", "/signup", "/tailor", "/pipeline", "/electronic",
                      "/scout", "/connections", "/security"}
        _OURS[0] = lambda p: p in routes
    return _OURS[0]


def _probe_class(path):
    """The attack CLASS of this path, or "" -- perseus_client.CLASSES is the one home for the
    table, and probe_shape is the one home for "is this actionable". Neither is re-implemented."""
    try:
        pc = _sib("perseus_client")
        if not pc.probe_shape(path, _ours()):
            return ""
        return pc.lane_of(path) or "probe"
    except Exception:
        return ""


def overview(window_h=24.0, feed_max=None):
    evs, source = read_events(window_h)
    http = [e for e in evs if e.get("evt") == "http"]
    out = {
        "generated": int(time.time()), "window_h": window_h, "service": SERVICE,
        "source": source,
        "hosts": {}, "requests": None, "refused": None, "status": {},
        "visitors": None, "clients": None, "unjudged": None, "addresses": None,
        "visitor_split": "none",
        "attacks": None, "attack_classes": {}, "top_offenders": [], "top_paths": [],
        "countries": {}, "users": [], "feed": [],
        "sidecar": "unverifiable", "sidecar_why": "", "sidecar_beat": {},
        "enforce": "unknown", "enforce_why": "",
        "alerting": "unknown", "alerts_fired": None, "alerts_suppressed": None, "alerting_why": "",
        "shield": {"blocks": None, "would_block": None, "tarpits": None, "refused_429": None},
        "llm": {"healthy": False},
        "caveat": "",
    }
    word, beat, why = sidecar_state()
    out["sidecar"], out["sidecar_beat"], out["sidecar_why"] = word, beat, why
    out["enforce"], out["enforce_why"] = enforcement_state(beat, word)

    if not source["readable"]:
        out["caveat"] = (source["why"] + " Every count on this page is therefore NOT MEASURED "
                         "rather than zero.")
        try:
            out["llm"] = _sib("llm_meter").report()
        except Exception:
            pass
        return out

    a, f, s, awhy = alerting_state(evs)
    out["alerting"], out["alerts_fired"], out["alerts_suppressed"], out["alerting_why"] = \
        a, f, s, awhy

    out["requests"] = len(http)
    hosts, statuses, paths, offenders, countries, users = {}, {}, {}, {}, {}, {}
    attacks, classes, refused = 0, {}, 0
    for e in http:
        h = e.get("host") or "(not recorded)"
        st = int(e.get("status") or 0)
        row = hosts.setdefault(h, {"requests": 0, "refused": 0, "attacks": 0, "addresses": set()})
        row["requests"] += 1
        if e.get("ip"):
            row["addresses"].add(e["ip"])
        statuses[str(st)] = statuses.get(str(st), 0) + 1
        if st in (401, 403, 429):
            refused += 1
            row["refused"] += 1
        p = str(e.get("path") or "")
        paths[p] = paths.get(p, 0) + 1
        c = _probe_class(p)
        if c:
            attacks += 1
            row["attacks"] += 1
            classes[c] = classes.get(c, 0) + 1
            ip = e.get("ip") or "?"
            o = offenders.setdefault(ip, {"ip": ip, "hits": 0, "paths": set(),
                                          "country": e.get("country") or "-"})
            o["hits"] += 1
            o["paths"].add(p)
        if e.get("country"):
            countries[e["country"]] = countries.get(e["country"], 0) + 1
        if e.get("user"):
            users[e["user"]] = users.get(e["user"], 0) + 1
    out["refused"] = refused
    out["attacks"] = attacks
    out["attack_classes"] = dict(sorted(classes.items(), key=lambda kv: -kv[1])[:12])
    out["status"] = dict(sorted(statuses.items()))
    out["hosts"] = {k: {"requests": v["requests"], "refused": v["refused"],
                        "attacks": v["attacks"], "addresses": len(v["addresses"])}
                    for k, v in sorted(hosts.items(), key=lambda kv: -kv[1]["requests"])}
    out["top_paths"] = [{"path": p, "hits": n}
                        for p, n in sorted(paths.items(), key=lambda kv: -kv[1])[:15]]
    # VARIETY, NOT VOLUME. A real visitor misses the same few stale paths; a scanner misses
    # hundreds of different ones. The offender list is ordered by DISTINCT paths for that reason.
    out["top_offenders"] = [{"ip": o["ip"], "hits": o["hits"], "distinct_paths": len(o["paths"]),
                             "country": o["country"],
                             "sample": sorted(o["paths"])[:4]}
                            for o in sorted(offenders.values(),
                                            key=lambda o: (-len(o["paths"]), -o["hits"]))[:12]]
    out["countries"] = dict(sorted(countries.items(), key=lambda kv: -kv[1])[:12])
    out["users"] = [{"user": u, "requests": n}
                    for u, n in sorted(users.items(), key=lambda kv: -kv[1])[:15]]

    try:
        v = _sib("visitors").count(http)
        out.update(visitors=v["visitors"], clients=v["clients"], unjudged=v["unjudged"],
                   addresses=v["addresses"])
        out["visitor_split"] = "measured" if v["addresses"] else "none"
    except Exception as e:
        out["visitor_split"] = "none"
        out["caveat"] = "the visitor split could not be computed (%r)" % (e,)

    out["shield"] = {
        "blocks": sum(1 for e in evs if e.get("evt") == "perseus_shield_block"),
        "would_block": sum(1 for e in evs if e.get("evt") == "perseus_shield_would_block"),
        "tarpits": sum(1 for e in evs if e.get("evt") == "perseus_shield_tarpit"),
        "refused_429": statuses.get("429", 0),
    }

    # THE FEED: the most recent requests, newest first. This is the literal answer to "I want to
    # see every user who is trying to enter the site".
    fm = FEED_MAX if feed_max is None else int(feed_max)
    for e in sorted(http, key=lambda e: float(e.get("ts") or 0), reverse=True)[:fm]:
        out["feed"].append({
            "ts": e.get("ts"), "host": e.get("host") or "", "ip": e.get("ip") or "",
            "method": e.get("method") or "", "path": str(e.get("path") or "")[:120],
            "status": e.get("status"), "ms": e.get("ms"), "country": e.get("country") or "",
            "user": e.get("user") or "", "bot": bool(e.get("bot")),
            "bot_name": e.get("bot_name") or "", "ua": str(e.get("ua") or "")[:120],
            "attack": _probe_class(str(e.get("path") or "")),
        })

    # THE MONEY, on the same page as the traffic, because the last incident was both at once.
    try:
        out["llm"] = _sib("llm_meter").report()
    except Exception as e:
        out["llm"] = {"healthy": False, "why": repr(e)[:160]}
    out["llm"]["calls_window"] = sum(1 for e in evs if e.get("evt") == "llm_call")
    out["llm"]["budget_refusals_window"] = sum(1 for e in evs
                                               if e.get("evt") == "llm_budget_refused")
    out["llm"]["model_refusals_window"] = sum(
        1 for e in evs if e.get("evt") == "llm_call" and str(e.get("caller", "")).endswith("REFUSED"))
    return out


# --------------------------------------------------------------------------- self-test
def _selftest():
    import tempfile
    global EVENTS_LOG, BEATS_DIR, SERVICE
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    d = tempfile.mkdtemp()
    SERVICE = "jhw-test"
    now = time.time()

    # 1. UNREADABLE IS NOT ZERO. The most important property in the file.
    EVENTS_LOG = os.path.join(d, "does-not-exist.log")
    BEATS_DIR = os.path.join(d, "beats")
    o = overview()
    ck("an unreadable event log reports requests=None, never 0", o["requests"] is None)
    ck("and it says why, in the caveat", "not a statement about traffic" in o["caveat"].lower()
       or "NOT MEASURED" in o["caveat"], o["caveat"][:90])
    ck("a missing sidecar is 'not installed', and enforcement is then 'unknown'",
       o["sidecar"] == "not installed" and o["enforce"] == "unknown",
       "%s / %s" % (o["sidecar"], o["enforce"]))

    # 2. a real window
    EVENTS_LOG = os.path.join(d, "events.log")
    rows = []
    for i in range(5):
        rows.append({"evt": "http", "service": SERVICE, "ts": now - 60, "host": "jobhuntwow.com",
                     "ip": "198.51.100.%d" % i, "method": "GET", "path": "/tailor", "status": 200,
                     "bot": False, "sf": 15, "ua": "Mozilla/5.0", "country": "DE",
                     "user": "a@b.c" if i < 2 else ""})
    for p in ["/wp-login.php", "/.env", "/.git/config", "/admin/config.php", "/phpinfo.php"]:
        rows.append({"evt": "http", "service": SERVICE, "ts": now - 30, "host": "jobhw.org",
                     "ip": "203.0.113.7", "method": "GET", "path": p, "status": 404,
                     "bot": False, "ua": "python-requests/2.31", "country": "US"})
    rows.append({"evt": "http", "service": SERVICE, "ts": now - 20, "host": "jobhw.org",
                 "ip": "198.51.100.9", "method": "GET", "path": "/pipeline", "status": 301,
                 "bot": False, "sf": 15, "ua": "Mozilla/5.0", "country": "DE"})
    rows.append({"evt": "security_alert", "service": SERVICE, "ts": now - 10, "rule": "path_probe"})
    rows.append({"evt": "alert_suppressed", "service": SERVICE, "ts": now - 9, "rule": "path_probe"})
    rows.append({"evt": "llm_budget_refused", "service": SERVICE, "ts": now - 8, "user": "x@y.z"})
    rows.append({"evt": "http", "service": "SOMEBODY-ELSE", "ts": now - 5, "host": "other.site",
                 "ip": "1.2.3.4", "path": "/", "status": 200})
    rows.append({"evt": "http", "ts": now - 5, "host": "other.site",          # NO service field
                 "ip": "1.2.3.5", "path": "/", "status": 200})
    rows.append({"evt": "http", "service": SERVICE, "ts": now - 40 * 3600, "host": "jobhuntwow.com",
                 "ip": "9.9.9.9", "path": "/old", "status": 200})
    with open(EVENTS_LOG, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    ck("the path classifier answers at all (a silent TypeError here reads as 'no attacks')",
       _probe_class("/wp-login.php") == "wordpress" and _probe_class("/.env") == "env_secrets",
       "%r / %r" % (_probe_class("/wp-login.php"), _probe_class("/.env")))
    ck("and it never calls our own pages an attack", _probe_class("/tailor") == ""
       and _probe_class("/pipeline") == "")

    o = overview(window_h=24)
    ck("another service's lines are not counted as ours", o["requests"] == 11, str(o["requests"]))
    ck("the five probes are counted as attacks, by class", o["attacks"] == 5
       and set(o["attack_classes"]) >= {"wordpress", "env_secrets"}, str(o["attack_classes"]))
    ck("a line older than the window is not counted",
       all(f["path"] != "/old" for f in o["feed"]))
    ck("the two hostnames are counted separately",
       set(o["hosts"]) == {"jobhuntwow.com", "jobhw.org"}, str(list(o["hosts"])))
    ck("the short domain's own traffic is visible", o["hosts"]["jobhw.org"]["requests"] == 6,
       str(o["hosts"].get("jobhw.org")))
    ck("the offender is ranked by DISTINCT paths, not volume",
       o["top_offenders"] and o["top_offenders"][0]["distinct_paths"] >= 5,
       str(o["top_offenders"][:1]))
    ck("alerts fired and suppressed are both counted",
       o["alerts_fired"] == 1 and o["alerts_suppressed"] == 1)
    ck("an alert that was RAISED but never delivered does not read as 'active'",
       o["alerting"] in ("unknown", "off"), "%s - %s" % (o["alerting"], o["alerting_why"]))
    ck("a line from another service with no service field is NOT counted as ours",
       o["requests"] == 11, str(o["requests"]))
    ck("a budget refusal in the window is reported",
       o["llm"].get("budget_refusals_window") == 1)
    ck("the feed carries the newest request first and names the host",
       o["feed"] and o["feed"][0]["host"] in ("jobhw.org", "jobhuntwow.com"))

    # 3. the sidecar words
    os.makedirs(BEATS_DIR, exist_ok=True)
    with open(os.path.join(BEATS_DIR, "%s.json" % SERVICE), "w") as fh:
        json.dump({"ts": now, "cycle": 3, "enforcing": True, "blocked": 0, "watching": 2}, fh)
    o = overview()
    ck("a fresh heartbeat reads active, and enforcement armed",
       o["sidecar"] == "active" and o["enforce"] == "armed",
       "%s / %s" % (o["sidecar"], o["enforce"]))
    with open(os.path.join(BEATS_DIR, "%s.json" % SERVICE), "w") as fh:
        json.dump({"ts": now - 4000, "cycle": 3, "enforcing": True}, fh)
    o = overview()
    ck("an old heartbeat reads stale, never active", o["sidecar"] == "stale", o["sidecar_why"])
    with open(os.path.join(BEATS_DIR, "%s.json" % SERVICE), "w") as fh:
        json.dump({"ts": now, "cycle": 3, "enforcing": False}, fh)
    o = overview()
    ck("detection-only reads 'none', which is a measurement, not a guess",
       o["enforce"] == "none", o["enforce_why"])

    print("security selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        print(json.dumps(overview(), indent=2, default=str))
        sys.exit(0)
    sys.exit(_selftest())
