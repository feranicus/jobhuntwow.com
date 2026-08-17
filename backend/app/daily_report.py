"""
daily_report.py — "who used jobhuntwow.com yesterday, and what did they build?" -> emailed to ALERT_EMAIL.

Sources (both already exist, nothing new to maintain):
  * the jobs SQLite  -> every tailored job: user, company, language, status, decks
  * events.log       -> logins (ok/fail), visitors, security alerts, LLM cost

Runs as a background task inside jhw-web (no cron in the container, no systemd unit on the droplet
to drift out of the repo). It computes the next 07:00 UTC on every loop, so a restart cannot make it
double-send or silently stop. Manual run:
    docker exec jhw-web python3 -m app.daily_report          # prints + sends
    docker exec jhw-web python3 -m app.daily_report --print  # prints only
"""
import asyncio, json, os, sqlite3, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    from . import notify
except ImportError:
    import notify

EVENTS_LOG  = os.environ.get("EVENTS_LOG", "/var/log/colt/events.log")
JOBS_DB     = os.path.join(os.environ.get("DATA_DIR", "/data"), "jobs.sqlite")
REPORT_HOUR = int(os.environ.get("DAILY_REPORT_HOUR", "7"))       # UTC
ENABLED     = os.environ.get("DAILY_REPORT", "1") != "0"


def _read_events(since):
    out = []
    try:
        with open(EVENTS_LOG, "r", errors="replace") as fh:
            for line in fh:
                i = line.find("{")
                if i < 0:
                    continue
                try:
                    e = json.loads(line[i:])
                except Exception:
                    continue
                if float(e.get("ts") or 0) >= since:
                    out.append(e)
    except FileNotFoundError:
        pass
    return out


def _read_jobs(since):
    """JobHuntWOW stores each tailored job as DATA_DIR/users/<hash>/<job_id>/job.json.

    cybergod used a jobs SQLite; porting the report 1:1 means adapting THIS function only - the
    rest of the report (per-user rollup, logins, countries, alerts) is unchanged.
    """
    out = []
    base = os.path.join(os.environ.get("DATA_DIR", "/data"), "users")
    try:
        for uh in os.listdir(base):
            ud = os.path.join(base, uh)
            if not os.path.isdir(ud):
                continue
            for jid in os.listdir(ud):
                mp = os.path.join(ud, jid, "job.json")
                if not os.path.isfile(mp):
                    continue
                try:
                    m = json.load(open(mp, encoding="utf-8"))
                except Exception:
                    continue
                if float(m.get("created") or 0) < since:
                    continue
                jd = m.get("jd") or {}
                out.append({
                    "email": m.get("email") or ("user:" + uh[:8]),
                    "company": jd.get("company") or "",
                    "title": jd.get("title") or "",
                    "status": "failed" if m.get("errors") else "done",
                    "created": m.get("created") or 0,
                    "decks": json.dumps(m.get("files") or []),
                    "lang": "",
                })
    except Exception:
        return []
    return sorted(out, key=lambda r: r["created"])


def build(hours=24):
    since = time.time() - hours * 3600
    ev, jobs = _read_events(since), _read_jobs(since)
    http   = [e for e in ev if e.get("evt") == "http"]
    auth   = [e for e in ev if e.get("evt") == "auth"]
    alerts = [e for e in ev if e.get("evt") == "security_alert"]
    done   = [e for e in ev if e.get("evt") == "assess_done"]
    errs   = [e for e in ev if e.get("evt") == "assess_error"]

    logins_ok   = [e for e in auth if e.get("result") in ("ok", "success", "authed")]
    logins_fail = [e for e in auth if e.get("result") == "fail"]
    humans = [e for e in http if not e.get("bot")]
    cost = sum(float(e.get("qwen_cost_usd") or 0) for e in done)

    # who did what
    per_user = defaultdict(lambda: {"jobs": [], "logins": 0, "ips": set(), "countries": set()})
    for j in jobs:
        per_user[j.get("email", "?")]["jobs"].append(j)
    for e in logins_ok:
        per_user[e.get("email") or e.get("user") or "?"]["logins"] += 1
    for e in http:
        u = e.get("user")
        if u:
            per_user[u]["ips"].add(e.get("ip", "-"))
            if e.get("country") and e["country"] != "-":
                per_user[u]["countries"].add(e["country"])

    L = []
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    L.append("jobhuntwow.com — daily access report")
    L.append("Window: last %dh  ·  generated %s UTC" % (hours, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")))
    L.append("=" * 72)
    L.append("")
    L.append("SUMMARY")
    L.append("  Visitors (unique IPs, humans) : %d" % len({e.get("ip") for e in humans}))
    L.append("  Requests (human / bot)        : %d / %d" % (len(humans), len(http) - len(humans)))
    L.append("  Logins (success / failed)     : %d / %d" % (len(logins_ok), len(logins_fail)))
    L.append("  Documents built               : %d completed, %d failed" % (len(done), len(errs)))
    L.append("  AI cost                       : $%.4f" % cost)
    L.append("  Security alerts               : %d" % len(alerts))
    L.append("")

    L.append("WHO USED THE PLATFORM AND WHAT THEY BUILT  (account email = identity)")
    L.append("-" * 72)
    if not per_user:
        L.append("  (nobody signed in during this window)")
    for user, d in sorted(per_user.items(), key=lambda kv: -len(kv[1]["jobs"])):
        L.append("  %s" % user)
        L.append("     logins: %d   IPs: %s   countries: %s"
                 % (d["logins"], ", ".join(sorted(d["ips"])) or "-", ", ".join(sorted(d["countries"])) or "-"))
        if d["jobs"]:
            L.append("     tailored jobs (%d):" % len(d["jobs"]))
            for j in d["jobs"]:
                t = datetime.fromtimestamp(j.get("created", 0), timezone.utc).strftime("%H:%M")
                decks = len(json.loads(j.get("decks") or "[]"))
                L.append("       %s  %-28s  lang=%-2s  %-7s  %d file(s)"
                         % (t, (j.get("company") or "?")[:28], j.get("lang", "en"),
                            j.get("status", "?"), decks))
        else:
            L.append("     tailored jobs: none")
        L.append("")

    if alerts:
        L.append("SECURITY ALERTS")
        L.append("-" * 72)
        for a in alerts:
            L.append("  [%s] %s — %s" % (a.get("severity"), a.get("rule"), a.get("subject")))
        L.append("")

    if logins_fail:
        L.append("FAILED LOGINS (identity + source)")
        L.append("-" * 72)
        for e in logins_fail[:25]:
            L.append("  %s  %s" % (datetime.fromtimestamp(e.get("ts", 0), timezone.utc).strftime("%H:%M"),
                                   e.get("email") or e.get("user") or "?"))
        L.append("")

    top_c = Counter(e.get("country") for e in humans if e.get("country") not in (None, "-"))
    if top_c:
        L.append("VISITOR COUNTRIES: " + ", ".join("%s=%d" % (c, n) for c, n in top_c.most_common(10)))
        L.append("")

    # attacker / abuse digest with MITRE ATT&CK mapping
    try:
        from . import threat_intel as _ti
        L.append("")
        L.append(_ti.render(hours))
        L.append("")
    except Exception as _e:
        L.append("  [threat digest unavailable: %s]" % repr(_e)[:80])

    L.append("-" * 72)
    L.append("Personal data in this report (email, IP, country) is processed for security monitoring")
    L.append("and service administration — GDPR Art. 6(1)(f). Retention: see jobhuntwow.com/privacy.")
    L.append("Full detail: godeyes.ai/observe -> 'JobHuntWOW - Electronic (app events)'")
    return day, "\n".join(L)


def send(hours=24):
    day, body = build(hours)
    ok = notify.email("jobhuntwow.com — daily access report %s" % day, body)
    notify._log(evt="daily_report", result="sent" if ok else "error", window_h=hours)
    return ok, body


async def scheduler():
    """Fire at REPORT_HOUR UTC daily. Recomputes the delay each loop, so a restart never double-sends."""
    if not ENABLED:
        return
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep(max(60, (nxt - now).total_seconds()))
        try:
            send(24)
        except Exception as e:
            notify._log(evt="daily_report", result="error", err=repr(e)[:160])
        # opt-in: submit hostile IPs to AbuseIPDB (no-op unless ABUSEIPDB_KEY is set)
        try:
            from . import threat_intel as _ti, abuse_report as _ar
            res = _ar.report_digest(_ti.build(24))
            notify._log(evt="abuse_report", **{k: res[k] for k in ("status", "reported") if k in res})
        except Exception as e:
            notify._log(evt="abuse_report", result="error", err=repr(e)[:160])


if __name__ == "__main__":
    day, body = build(24)
    print(body)
    if "--print" not in sys.argv:
        ok = notify.email("jobhuntwow.com — daily access report %s" % day, body)
        print("\n[email] sent:", ok)
