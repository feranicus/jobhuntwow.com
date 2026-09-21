"""THE WALL IN FRONT OF THE WALLET. One chokepoint decides whether a model call may happen.

WHY THIS FILE EXISTS, precisely. The 2026-08-30 -> 09-06 LLM-jacking of jobhuntwow.com ended with
twelve findings, and the incident report's *still open* list names this one as the most likely
recurrence path, verbatim:

    "jobhuntwow still has no budget cap and no rate limit. Authentication is now required, but
     self-signup is open. The same spend is available to anyone willing to create an account, and
     nothing currently prevents it."

`llm_events.py` is the RECORDER and says of itself "NOT A BUDGET". This is the budget. They are
deliberately two files: a recorder that can never block, and a gate that can. They share ONE
pricing home -- `llm_events.rate_for/cost_of` -- because a cap computed from different arithmetic
than the ledger is a cap on a number nobody can reconcile.

THE FOUR RULES IT ENFORCES, in order, each with its own reason for existing:
  1. GLOBAL daily USD     -- the account-level stop. What the incident actually cost.
  2. PER-USER daily USD   -- open self-signup means "authenticated" is not "trusted". One account
                             may not spend the whole day's budget and lock every other user out.
  3. PER-USER calls/hour  -- the rate limit. 1,539 requests over seven days tripped nothing.
  4. GLOBAL calls/hour    -- the many-accounts version of 3. Signing up ten times must not multiply.

FAIL OPEN ON A STORAGE FAULT, CLOSED ON THE BUDGET. A broken meter must not take the product down,
so an unreadable database allows the call and says so out loud. But `None` (cannot read) and `0.0`
(a quiet day) are kept apart everywhere in this file: collapsing them is exactly how a broken meter
silently becomes an unlimited budget.

UNKNOWN TOKENS ARE CHARGED, NOT IGNORED. A streamed response carries no `usage` block, so its cost
is unknown -- and the actor in the incident used the STREAMING endpoint. A gate that only counts
what it can price is a gate the attacker walks through by asking for a stream. An unpriced call is
therefore charged JHW_UNKNOWN_CALL_USD against the caps and the row is marked `estimated`, so the
ledger never claims that estimate is a measurement.

NO MODEL DECIDES ANYTHING HERE. Arithmetic decides; the models are what is being paid for.
"""
import contextvars
import json
import os
import sqlite3
import time

DB_PATH = (os.environ.get("JHW_METER_DB")
           or os.path.join(os.environ.get("DATA_DIR", "/data"), "llm_meter.sqlite"))

# The four caps. Every one is an env var so the operator can move it without a deploy.
DAILY_USD = float(os.environ.get("JHW_DAILY_USD", "3.00"))
USER_DAILY_USD = float(os.environ.get("JHW_USER_DAILY_USD", "0.75"))
USER_CALLS_PER_HOUR = int(os.environ.get("JHW_USER_CALLS_PER_HOUR", "120"))
CALLS_PER_HOUR = int(os.environ.get("JHW_CALLS_PER_HOUR", "600"))
# What an un-priceable call (a stream with no usage block) costs against the caps.
UNKNOWN_CALL_USD = float(os.environ.get("JHW_UNKNOWN_CALL_USD", "0.01"))
# Warn once a day at this fraction of the global cap, and page on any single call this expensive.
WARN_FRACTION = float(os.environ.get("JHW_WARN_FRACTION", "0.60"))
ALERT_SINGLE_USD = float(os.environ.get("JHW_ALERT_SINGLE_USD", "0.15"))
SERVICE = os.environ.get("SERVICE", "electronic-backend")

_broken = [False]        # latch: once the store faults, this process stops asking and fails OPEN
_warned = [""]           # the day we last sent the 60% warning
_announced = [False]     # the storage fault is printed once, not once per request


class BudgetExceeded(RuntimeError):
    """The cap or the rate limit refused this call. Its own type on purpose: a different model or a
    retry cannot fix it, so no fallback chain should treat it as a model failure and walk the
    ladder spending four times over."""


# WHOSE CALL IS THIS. Set once per request by auth.require_user -- the single place in the app where
# "this request belongs to this account" is established -- and read by every chokepoint. Threading a
# `user=` parameter through twenty functions is how the fourth call site ends up unattributed, which
# is the exact shape of the incident: spend nobody could assign to anybody.
_CURRENT = contextvars.ContextVar("jhw_llm_user", default="")


def set_current_user(email):
    try:
        _CURRENT.set(str(email or "")[:120])
    except Exception:
        pass


def current_user():
    try:
        return _CURRENT.get() or ""
    except Exception:
        return ""


def gate(caller="", model="", user=None, estimate_usd=0.0, ip=""):
    """allow() + refuse() + raise, for the call sites that just want one line.

    Returns None when the call may proceed; raises BudgetExceeded when it may not.
    """
    who = current_user() if user is None else user
    ok, why = allow(who, estimate_usd=estimate_usd, model=model, caller=caller)
    if ok:
        return None
    refuse(user=who, model=model, caller=caller, reason=why, ip=ip)
    raise BudgetExceeded(why)


def _sib(name):
    """Import a sibling module whether this file is `app.llm_meter` or a script run from its folder.

    ship.py runs every suite as `python <file>`, and a relative import dies there. A gate whose
    self-test cannot RUN is not a gate, so the import bends instead of the test being skipped.
    """
    from importlib import import_module
    if __package__:
        return import_module("." + name, __package__)
    return import_module(name)


def _log(**k):
    k.setdefault("ts", int(time.time()))
    k.setdefault("service", SERVICE)
    try:
        print(json.dumps(k), flush=True)
    except Exception:
        pass
    p = os.environ.get("EVENTS_LOG", "")
    if p:
        try:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(k) + "\n")
        except Exception:
            pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    ts        REAL    NOT NULL,
    day       TEXT    NOT NULL,
    caller    TEXT    NOT NULL,
    model     TEXT    NOT NULL,
    tin       INTEGER NOT NULL,
    tout      INTEGER NOT NULL,
    usd       REAL    NOT NULL,
    charged   REAL    NOT NULL,
    estimated INTEGER NOT NULL,
    ms        INTEGER NOT NULL,
    status    TEXT    NOT NULL,
    user      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_day  ON calls (day);
CREATE INDEX IF NOT EXISTS calls_ts   ON calls (ts);
CREATE INDEX IF NOT EXISTS calls_user ON calls (user, day);
"""


_READY = [""]          # the path whose schema this process has already created
RETAIN_DAYS = int(os.environ.get("JHW_METER_RETAIN_DAYS", "120"))


def _connect():
    """A connection with the schema guaranteed. The DDL runs ONCE per process per path, not on
    every gate: four DDL statements and an implicit COMMIT in front of every model call is a cost
    paid thousands of times for a table that already exists."""
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=5)
    if _READY[0] != DB_PATH:
        c.executescript(_SCHEMA)
        # RETENTION, or the per-call cost grows without limit: at the CALLS_PER_HOUR ceiling this
        # table gains ~5M rows a year and every gate reads it. Nothing here needs last spring.
        try:
            cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - RETAIN_DAYS * 86400))
            c.execute("DELETE FROM calls WHERE day < ?", (cutoff,))
            c.commit()
        except Exception:
            pass
        _READY[0] = DB_PATH
    return c


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _fault(e):
    """Latch the store as unreadable and say so exactly once. Never raises."""
    _broken[0] = True
    if not _announced[0]:
        _announced[0] = True
        _log(evt="llm_meter_unavailable", path=DB_PATH, err=repr(e)[:160],
             effect="the budget gate is FAILING OPEN until this process restarts")


def usage(user=""):
    """(global_usd_today, user_usd_today, global_calls_hour, user_calls_hour) or None on a fault.

    None is never 0.0. A caller that cannot tell "nothing was spent" from "I could not look" will
    eventually report an unlimited budget as a quiet day.
    """
    if _broken[0]:
        return None
    day, hour_ago = _today(), time.time() - 3600
    try:
        with _connect() as c:
            g = c.execute("SELECT COALESCE(SUM(charged),0) FROM calls WHERE day=?", (day,)).fetchone()
            u = c.execute("SELECT COALESCE(SUM(charged),0) FROM calls WHERE day=? AND user=?",
                          (day, str(user or ""))).fetchone()
            gc = c.execute("SELECT COUNT(*) FROM calls WHERE ts>=?", (hour_ago,)).fetchone()
            uc = c.execute("SELECT COUNT(*) FROM calls WHERE ts>=? AND user=?",
                           (hour_ago, str(user or ""))).fetchone()
        return float(g[0] or 0.0), float(u[0] or 0.0), int(gc[0] or 0), int(uc[0] or 0)
    except Exception as e:
        _fault(e)
        return None


def spent_today():
    """Global USD today, or None when the meter cannot read itself."""
    v = usage("")
    return None if v is None else v[0]


def allow(user=None, estimate_usd=0.0, model="", caller=""):
    """(ok, reason). CALLED BEFORE THE REQUEST -- counting afterwards buys nothing.

    Order matters only for which reason the caller is told; any single failing rule refuses.

    HONEST LIMIT: the USD rules are enforced against spend ALREADY RECORDED, and callers pass no
    estimate, so N requests issued inside the same instant all read the same pre-spend total and
    all pass. What bounds that overshoot is the two CALL-RATE rules, which is why they exist
    alongside the money and why they are counted over a rolling hour rather than a day.
    """
    user = current_user() if user is None else user
    v = usage(user)
    if v is None:
        return True, ("meter unavailable - failing OPEN so a storage fault cannot take the "
                      "product down")
    g_usd, u_usd, g_calls, u_calls = v
    est = max(0.0, float(estimate_usd or 0.0))
    if g_usd + est >= DAILY_USD:
        return False, ("the daily AI budget for this service is spent: $%.4f of $%.2f "
                       "(JHW_DAILY_USD)" % (g_usd, DAILY_USD))
    if user and u_usd + est >= USER_DAILY_USD:
        return False, ("this account has spent its daily share: $%.4f of $%.2f "
                       "(JHW_USER_DAILY_USD)" % (u_usd, USER_DAILY_USD))
    if user and u_calls >= USER_CALLS_PER_HOUR:
        return False, ("this account has made %d model calls in the last hour, the limit is %d "
                       "(JHW_USER_CALLS_PER_HOUR)" % (u_calls, USER_CALLS_PER_HOUR))
    if g_calls >= CALLS_PER_HOUR:
        return False, ("this service has made %d model calls in the last hour, the limit is %d "
                       "(JHW_CALLS_PER_HOUR)" % (g_calls, CALLS_PER_HOUR))
    return True, ""


def refuse(user="", model="", caller="", reason="", ip=""):
    """Record and announce a refusal. A refusal nobody hears about is a silent outage to the user
    and an invisible attack to the operator, so this writes a row AND pages -- the same treatment a
    refused model id gets at the allowlist."""
    _log(evt="llm_budget_refused", user=str(user or "")[:120], model=str(model or "")[:60],
         caller=str(caller or "")[:40], ip=str(ip or "")[:64], reason=str(reason or "")[:300])
    try:
        alerts = _sib("alerts")
        alerts.fire("llm_budget", str(user or ip or "anonymous"),
                    "AI budget or rate limit refused a call",
                    ["user   : %s" % (user or "?"), "model  : %s" % (model or "?"),
                     "caller : %s" % (caller or "?"), "from   : %s" % (ip or "?"),
                     "reason : %s" % reason,
                     "Nothing was forwarded to the vendor, so this cost nothing."],
                    severity="HIGH")
    except Exception:
        pass


def record(caller, model, usage_dict, *, ms=0, status="ok", user=""):
    """Write one row AFTER the call. Returns the day's new global total, or None on a fault.

    `usage_dict` is the vendor's own dict. Anything else means the tokens are UNKNOWN, which is
    charged at UNKNOWN_CALL_USD and marked `estimated` -- never recorded as a free call.
    """
    try:
        llm_events = _sib("llm_events")
        u = usage_dict if isinstance(usage_dict, dict) else {}
        known = bool(u)
        ti = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        to = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        usd = llm_events.cost_of(model, ti, to) if known else 0.0
        charged = usd if known else UNKNOWN_CALL_USD
        if _broken[0]:
            return None
        with _connect() as c:
            c.execute("INSERT INTO calls (ts,day,caller,model,tin,tout,usd,charged,estimated,ms,"
                      "status,user) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), _today(), str(caller or "")[:40], str(model or "")[:60],
                       ti, to, usd, charged, 0 if known else 1, int(ms or 0),
                       str(status or "")[:24], str(user or "")[:120]))
            r = c.execute("SELECT COALESCE(SUM(charged),0) FROM calls WHERE day=?",
                          (_today(),)).fetchone()
        total = float(r[0] or 0.0)
        if usd >= ALERT_SINGLE_USD:
            _announce("one model call cost $%.4f (%s, %s, %d in / %d out)"
                      % (usd, model, caller, ti, to), user)
        if total >= DAILY_USD * WARN_FRACTION and _warned[0] != _today():
            _warned[0] = _today()
            _announce("AI spend today is $%.4f of the $%.2f daily cap (%.0f%%)"
                      % (total, DAILY_USD, 100.0 * total / max(DAILY_USD, 1e-9)), user)
        return total
    except Exception as e:
        _fault(e)
        return None


def _announce(text, user=""):
    try:
        alerts = _sib("alerts")
        alerts.fire("llm_spend", user or "service", "AI spend", [text], severity="HIGH")
    except Exception:
        _log(evt="llm_spend_note", note=text[:300])


def report(days=14):
    """What the operator's console reads. `healthy` false means every number here is meaningless."""
    out = {"db": DB_PATH, "healthy": not _broken[0], "cap_usd": DAILY_USD,
           "user_cap_usd": USER_DAILY_USD, "calls_per_hour": CALLS_PER_HOUR,
           "user_calls_per_hour": USER_CALLS_PER_HOUR,
           "today_usd": None, "calls_hour": None, "per_day": [], "per_user": [],
           "per_model": [], "unknown_models": []}
    if _broken[0]:
        return out
    try:
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
        with _connect() as c:
            out["today_usd"] = float(c.execute("SELECT COALESCE(SUM(charged),0) FROM calls "
                                               "WHERE day=?", (_today(),)).fetchone()[0] or 0.0)
            out["calls_hour"] = int(c.execute("SELECT COUNT(*) FROM calls WHERE ts>=?",
                                              (time.time() - 3600,)).fetchone()[0] or 0)
            out["per_day"] = [{"day": d, "usd": round(s, 6), "calls": n} for d, s, n in c.execute(
                "SELECT day, SUM(charged), COUNT(*) FROM calls WHERE day>=? GROUP BY day "
                "ORDER BY day DESC", (since,))]
            out["per_user"] = [{"user": u or "(none)", "usd": round(s, 6), "calls": n}
                               for u, s, n in c.execute(
                "SELECT user, SUM(charged), COUNT(*) FROM calls WHERE day>=? GROUP BY user "
                "ORDER BY SUM(charged) DESC LIMIT 20", (since,))]
            out["per_model"] = [{"model": m, "usd": round(s, 6), "calls": n}
                                for m, s, n in c.execute(
                "SELECT model, SUM(charged), COUNT(*) FROM calls WHERE day>=? GROUP BY model "
                "ORDER BY SUM(charged) DESC LIMIT 20", (since,))]
        try:
            llm = _sib("llm")
            known = set(llm.allowed_models())
            out["unknown_models"] = [m["model"] for m in out["per_model"]
                                     if m["model"] and m["model"] not in known]
        except Exception:
            out["unknown_models"] = []
        return out
    except Exception as e:
        _fault(e)
        out["healthy"] = False
        return out


# --------------------------------------------------------------------------- self-test
def _selftest():
    import tempfile
    global DB_PATH
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    d = tempfile.mkdtemp()
    DB_PATH = os.path.join(d, "meter.sqlite")
    _broken[0] = False
    _warned[0] = ""

    ok, why = allow("a@b.c")
    ck("an empty meter allows", ok, why)

    # 1. the GLOBAL cap closes.
    globals()["DAILY_USD"] = 0.10
    with _connect() as c:
        c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), _today(), "t", "deepseek-3.2", 0, 0, 0.2, 0.2, 0, 0, "ok", "x@y.z"))
    ok, why = allow("someone-else@b.c")
    ck("the global daily cap refuses everyone", (not ok) and "daily AI budget" in why, why)

    # 2. the PER-USER cap closes for the spender and NOT for anyone else.
    globals()["DAILY_USD"] = 100.0
    globals()["USER_DAILY_USD"] = 0.10
    ok_spender, why_s = allow("x@y.z")
    ok_other, _ = allow("someone-else@b.c")
    ck("the per-user cap refuses the spender", (not ok_spender) and "daily share" in why_s, why_s)
    ck("the per-user cap does not refuse a different account", ok_other)

    # 3. the per-user RATE limit closes without any USD being spent.
    globals()["USER_DAILY_USD"] = 100.0
    globals()["USER_CALLS_PER_HOUR"] = 3
    with _connect() as c:
        for _ in range(3):
            c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), _today(), "t", "m", 0, 0, 0.0, 0.0, 1, 0, "ok", "rate@b.c"))
    ok, why = allow("rate@b.c")
    ck("the per-user hourly rate limit refuses", (not ok) and "model calls in the last hour" in why,
       why)

    # 4. UNKNOWN TOKENS ARE CHARGED. This is the streaming hole the actor used.
    globals()["USER_CALLS_PER_HOUR"] = 10000
    globals()["UNKNOWN_CALL_USD"] = 0.05
    before = usage("stream@b.c")[1]
    record("qwen.chat_stream", "deepseek-3.2", None, user="stream@b.c", status="stream")
    after = usage("stream@b.c")[1]
    ck("a stream with no usage block still costs the caller something",
       after - before >= 0.05, "%.4f -> %.4f" % (before, after))

    # 5. a priced call is charged what the LEDGER says, not a second opinion.
    _le = _sib("llm_events")
    want = _le.cost_of("deepseek-3.2", 1000, 1000)
    before = usage("priced@b.c")[1]
    record("llm.complete", "deepseek-3.2", {"prompt_tokens": 1000, "completion_tokens": 1000},
           user="priced@b.c")
    got = usage("priced@b.c")[1] - before
    ck("a priced call is charged llm_events.cost_of exactly", abs(got - want) < 1e-9,
       "%.8f vs %.8f" % (got, want))

    # 6. FAIL OPEN on a storage fault, and say so.
    DB_PATH = os.path.join(d, "nope", "x")  # a directory that cannot be created (parent is a file)
    open(os.path.join(d, "nope"), "w").close()
    _broken[0] = False
    _announced[0] = False
    ok, why = allow("a@b.c")
    ck("an unreadable meter fails OPEN and says so", ok and "failing OPEN" in why, why)
    ck("and it latches, so it is not asked again", _broken[0])
    ck("spent_today() returns None, never 0.0, when the meter is broken", spent_today() is None)

    print("llm_meter selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys as _s
    if "--report" in _s.argv:
        print(json.dumps(report(), indent=2))
        _s.exit(0)
    _s.exit(_selftest())
