"""WATCH TWO SOURCES: our meter says WHO, the vendor's balance says WHETHER.

The 2026-09 incident was found by a bank statement, six days late. A meter-only watcher would have
reported a completely normal fortnight while the invoice tripled, because the spend was made by a
caller our meter had never heard of. So this compares:

    OUR LEDGER   llm_meter -- per caller, per model, per user. Attribution, no authority.
    THE VENDOR   DigitalOcean's month-to-date usage, polled hourly. Authority, no attribution.

A gap between them is the finding. It means somebody is spending on this account through a door
this codebase does not own: another project on the shared key, or an agent created in the vendor's
console that appears in no repository and on no droplet.

THREE INDEPENDENT TRIGGERS, and each states which source produced it:
  1. our own spend today is a MEDIAN-baseline spike (median, not mean: one prior spike must not
     hide the next, and today is excluded from its own baseline)
  2. the ACCOUNT moved more than an absolute floor since the last poll
  3. UNATTRIBUTED: the account moved and our ledger cannot account for it
  4. a model was called today that was never called before -- the exact shape of the incident

IT NEVER BLOCKS ANYTHING. Enforcement is llm_meter.allow(); this is observation. It also never
raises: a watcher that can take the API down is a worse outcome than an unattributed dollar.
"""
import json
import os
import statistics
import time

STATE = os.environ.get("JHW_SPEND_STATE") or os.path.join(
    os.environ.get("DATA_DIR", "/data"), "spend_watch.json")
BASELINE_DAYS = int(os.environ.get("SPEND_BASELINE_DAYS", "14"))
MIN_BASELINE_DAYS = int(os.environ.get("SPEND_MIN_BASELINE_DAYS", "4"))
SPIKE_RATIO = float(os.environ.get("SPEND_SPIKE_RATIO", "4.0"))
SPIKE_MIN_USD = float(os.environ.get("SPEND_SPIKE_MIN_USD", "0.10"))
ACCOUNT_SPIKE_USD = float(os.environ.get("SPEND_ACCOUNT_SPIKE_USD", "2.00"))
UNATTRIBUTED_USD = float(os.environ.get("SPEND_UNATTRIBUTED_USD", "1.00"))
COOLDOWN_S = int(os.environ.get("SPEND_ALERT_COOLDOWN_S", "21600"))
EVERY_S = int(os.environ.get("SPEND_WATCH_EVERY_S", "3600"))
SERVICE = os.environ.get("SERVICE", "jhw-web")


def _sib(name):
    from importlib import import_module
    if __package__:
        return import_module("." + name, __package__)
    return import_module(name)


def _now():
    return time.time()


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(d):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, STATE)          # atomic: two processes must never read a half-written file
    except Exception:
        pass


def baseline(per_day, today):
    """(median, days_of_history) over COMPLETED days. Today is excluded from its own baseline.

    A day with no calls counts as ZERO rather than being skipped: a quiet week IS the baseline, and
    dropping the quiet days silently raises it and hides a return to spending.
    """
    days = {d.get("day"): float(d.get("usd") or 0.0) for d in (per_day or [])}
    days.pop(today, None)
    if not days:
        return None, 0
    newest = max(days)
    # UTC IN, UTC OUT. time.mktime() reads the struct as LOCAL time while the walk below is
    # gmtime, so on a non-UTC host the baseline was built from the wrong days. calendar.timegm is
    # the UTC counterpart, and _today() is UTC everywhere else in this file.
    import calendar
    t_end = calendar.timegm(time.strptime(newest, "%Y-%m-%d"))
    seq = [days.get(time.strftime("%Y-%m-%d", time.gmtime(t_end - i * 86400)), 0.0)
           for i in range(BASELINE_DAYS)]
    seq = seq[:max(len(days), MIN_BASELINE_DAYS)]
    return statistics.median(seq), len(days)


def account_spend():
    """(usd_since_last_poll, detail) from DigitalOcean, or (None, why). A delta needs two polls."""
    tok = (os.environ.get("DO_API_TOKEN") or "").strip()
    if not tok:
        return None, ("DO_API_TOKEN is not set in this container, so the vendor half cannot be "
                      "read. Our own ledger is still reported, and the gap between them is not.")
    import urllib.request
    req = urllib.request.Request("https://api.digitalocean.com/v2/customers/my/balance",
                                 headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        mtd = float(d.get("month_to_date_usage") or 0.0)
    except Exception as e:
        return None, "the DigitalOcean balance lookup failed: %s" % (repr(e)[:120],)
    st = _load()
    prev, prev_ts = st.get("mtd_usage"), st.get("mtd_ts")
    st["mtd_usage"], st["mtd_ts"] = mtd, _now()
    _save(st)
    if prev is None or prev_ts is None:
        return None, "first reading (month-to-date $%.2f); a delta needs two" % mtd
    if mtd < float(prev):
        return None, "month-to-date reset (a new billing month) - the baseline restarts"
    hours = max(0.25, (_now() - float(prev_ts)) / 3600.0)
    return mtd - float(prev), "$%.2f in %.1fh (month-to-date $%.2f)" % (mtd - float(prev), hours, mtd)


def _prior_models(today):
    """Models seen on any day BEFORE today. Empty on a fault, so a fault makes NOTHING look new:
    a false "NEW MODEL" accusation is worse than a quiet one, because the operator acts on it."""
    try:
        m = _sib("llm_meter")
        if m._broken[0]:
            return set()
        with m._connect() as c:
            return {r[0] for r in c.execute("SELECT DISTINCT model FROM calls WHERE day < ?",
                                            (today,))}
    except Exception:
        return set()


def check():
    out = {"ts": int(_now()), "service": SERVICE, "spike": False, "reasons": [], "notes": [],
           "today_usd": None, "baseline_usd": None, "baseline_days": 0,
           "account_delta": None, "account_detail": "", "new_models": [], "models": [],
           "meter_healthy": False}
    try:
        meter = _sib("llm_meter")
        rep = meter.report(days=BASELINE_DAYS + 1)
    except Exception as e:
        out["notes"].append("the meter could not be read (%r) - NOTHING here is a statement about "
                            "our own spend" % (e,))
        rep = None
    if rep:
        out["meter_healthy"] = bool(rep.get("healthy"))
        out["today_usd"] = rep.get("today_usd")
        base, n = baseline(rep.get("per_day"), _today())
        out["baseline_usd"], out["baseline_days"] = base, n
        cur = out["today_usd"]
        prior = _prior_models(_today())
        out["models"] = [{"model": m["model"], "usd": m["usd"], "calls": m["calls"],
                          "new": m["model"] not in prior} for m in (rep.get("per_model") or [])]
        out["new_models"] = [m["model"] for m in out["models"] if m["new"]]
        if cur is None:
            out["notes"].append("our own spend today is not readable")
        elif n < MIN_BASELINE_DAYS:
            out["notes"].append("only %d completed day(s) of history - too little for a baseline, "
                                "so no spike verdict is offered yet" % n)
        elif cur >= SPIKE_MIN_USD and (not base or cur >= base * SPIKE_RATIO):
            out["spike"] = True
            out["reasons"].append("our metered spend today is $%.4f against a %d-day median of "
                                  "$%.4f (%s)" % (cur, n, base or 0.0,
                                                  "no prior spend at all" if not base
                                                  else "%.1fx" % (cur / base)))
        if out["new_models"]:
            out["spike"] = True
            out["reasons"].append("model(s) called today that were never called before: %s - this "
                                  "is the exact shape of the 2026-09 incident"
                                  % ", ".join(sorted(out["new_models"])[:6]))
        if rep.get("unknown_models"):
            out["spike"] = True
            out["reasons"].append("model(s) called that are not on the allowlist: %s"
                                  % ", ".join(rep["unknown_models"][:6]))

    delta, detail = account_spend()
    out["account_delta"], out["account_detail"] = delta, detail
    if delta is None:
        out["notes"].append(detail)
    elif delta >= ACCOUNT_SPIKE_USD:
        out["spike"] = True
        out["reasons"].append("the DigitalOcean ACCOUNT spent %s since the last check" % detail)
    mine = out.get("today_usd")
    if delta is not None and mine is not None and delta - mine >= UNATTRIBUTED_USD:
        out["spike"] = True
        out["reasons"].append(
            "UNATTRIBUTED: the account moved $%.2f while this codebase accounts for $%.4f. The "
            "difference is a caller we do not control - another project on the shared key, or an "
            "agent created in the vendor's console." % (delta, mine))
    return out


def render(v):
    """PLAIN TEXT. A model id contains underscores and hyphens; one stray entity makes Telegram
    reject the entire message, and the alert that matters most is the one that never arrives."""
    L = ["AI SPEND WATCH - %s" % (v.get("service") or "?"),
         "verdict   : %s" % ("SPIKE" if v.get("spike") else "normal"),
         "ours today: %s" % ("not readable" if v.get("today_usd") is None
                             else "$%.4f" % v["today_usd"]),
         "baseline  : %s over %d completed day(s)"
         % ("none yet" if v.get("baseline_usd") is None else "$%.4f" % v["baseline_usd"],
            v.get("baseline_days") or 0),
         "account   : %s" % (v.get("account_detail") or "not read")]
    for r in v.get("reasons") or []:
        L.append("  ! " + r)
    for n in v.get("notes") or []:
        L.append("  . " + n)
    top = [m for m in (v.get("models") or [])][:5]
    if top:
        L.append("models today:")
        for m in top:
            L.append("  %-28s $%.4f  %d call(s)%s"
                     % (m["model"][:28], m["usd"], m["calls"], "   <- NEW" if m["new"] else ""))
    return "\n".join(L)


def run_once(force=False):
    v = check()
    v["alerted"] = False
    if not (v["spike"] or force):
        return v
    st = _load()
    if not force and _now() - float(st.get("last_alert") or 0) < COOLDOWN_S:
        v["suppressed"] = "cooldown"
        print(json.dumps({"evt": "spend_alert", "result": "suppressed", "reason": "cooldown",
                          "service": SERVICE, "ts": int(_now())}), flush=True)
        return v
    body = render(v)
    ok = False
    try:
        notify = _sib("notify")
        ok = bool(notify.telegram(body))
        try:
            notify.email("AI spend alert - jobhuntwow.com", body)
        except Exception:
            pass
    except Exception:
        ok = False
    st["last_alert"] = _now()               # stamped even on a failed send: a channel that recovers
    _save(st)                               # must not then deliver twenty queued copies
    v["alerted"] = ok
    print(json.dumps({"evt": "spend_alert", "result": "sent" if ok else "undelivered",
                      "spike": v["spike"], "reasons": (v.get("reasons") or [])[:3],
                      "service": SERVICE, "ts": int(_now())}), flush=True)
    return v


async def scheduler():
    import asyncio
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_once)
        except Exception as e:
            print(json.dumps({"evt": "spend_watch_error", "err": repr(e)[:160]}), flush=True)
        await asyncio.sleep(max(300, EVERY_S))


def _selftest():
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    per_day = [{"day": "2026-09-20", "usd": 0.10}, {"day": "2026-09-19", "usd": 0.10},
               {"day": "2026-09-18", "usd": 0.10}, {"day": "2026-09-17", "usd": 0.10},
               {"day": "2026-09-21", "usd": 9.99}]
    b, n = baseline(per_day, "2026-09-21")
    # `or b is not None` used to make this true for ANY value, including today's own 9.99.
    ck("today is excluded from its own baseline", b == 0.1, "median=%s days=%s" % (b, n))
    ck("and today's spike is not in the baseline it is judged against", b is not None and b < 1.0,
       "median=%s" % b)
    ck("a quiet day counts as zero, it is not skipped", n == 4, "days=%s" % n)
    b2, n2 = baseline([], "2026-09-21")
    ck("no history is None, never 0.0", b2 is None and n2 == 0)

    import tempfile
    global STATE
    STATE = os.path.join(tempfile.mkdtemp(), "s.json")
    os.environ.pop("DO_API_TOKEN", None)
    d, why = account_spend()
    ck("with no vendor token the account half says so, and returns None not 0",
       d is None and "DO_API_TOKEN" in why, why[:60])

    v = {"service": "x", "spike": True, "today_usd": None, "baseline_usd": None,
         "baseline_days": 0, "account_detail": "", "reasons": ["r"], "notes": ["n"],
         "models": [{"model": "deepseek-3.2", "usd": 0.1, "calls": 2, "new": True}]}
    txt = render(v)
    ck("an unreadable figure renders as words, never as $0.0000",
       "not readable" in txt and "$0.0000" not in txt.split("baseline")[0])
    ck("the alert body is plain text with no markdown entities",
       "*" not in txt and "_" not in txt.replace("spend_watch", ""))
    ck("a new model is called out by name", "<- NEW" in txt)
    print("spend_watch selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--send" in sys.argv:
        print(json.dumps(run_once(force=True), indent=2, default=str))
        sys.exit(0)
    if "--json" in sys.argv:
        print(json.dumps(check(), indent=2, default=str))
        sys.exit(0)
    sys.exit(_selftest())
