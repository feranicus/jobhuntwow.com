#!/usr/bin/env python3
"""test_gate_governance.py - the RULES that decide a release, and the negative test for each one.

    python3 tests/test_gate_governance.py

NO NETWORK, NO DROPLET, NO API KEY, NO pytest. Everything here is a pure function, which is exactly
why the governance was written as pure functions: a rule that needs two droplets to exercise is a
rule that ships unverified (the sibling project's catch-all-scores-a-pass defect got in that way).

WHAT IS PROVEN
  1  a failing gate can NEVER be rescued by a happy panel
  2  a single dissenting reviewer can NEVER block a good release (a 429 must not hold a release)
  3  a reviewer that ERRORED is not dissent - it is a non-answer
  4  2+ dissenters INCLUDING one hard NO-GO halt a green gate, and OVERRIDE_PANEL=1 releases it
  5  two UNSUREs with nobody objecting do NOT halt (a gate that stops you for nothing gets ignored)
  6  MISSING evidence is a failure: the gate knows what it EXPECTS, it does not count what it got
  7  a PASS whose detail describes a failure is DEMOTED, and a benign phrase is not
  8  the CHECK| protocol parses, and a stray pipe or a non-CHECK line cannot corrupt it
  9  a detail is WRAPPED, never truncated
 10  consensus() needs a QUORUM on the direction, takes the MEDIAN, and clamps ON READ
 11  the panel is wired to tune NOTHING at this stage of the port
 12  the prompts and the digest actually render (a stray % in a briefing is a runtime crash)
Each rule is then NEGATIVE-TESTED by mutating a COPY of the module and proving the rule fails.

THE EXIT GATE IS THE LAST STATEMENT IN THIS FILE, on purpose. A gate in the middle silently stops
enforcing every check appended after it, and checks are always appended: the sibling project found
73 checks - including its public-suffix guard - running with rc=0 for exactly that reason.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SG = os.path.join(ROOT, "deploy", "stagegate")
sys.path.insert(0, SG)
sys.path.insert(0, ROOT)

import gate      # noqa: E402
import quorum    # noqa: E402

RUN = [0]
FAILS = []


def check(cond, what):
    RUN[0] += 1
    print("  %s  %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def variant(module_file, replacements, name):
    """Import a MUTATED COPY of a module from a temp dir. Nothing in the repo is touched.

    Mutating the real file and restoring it in a `finally` is not safe: a `finally` cannot survive
    SIGKILL, and a killed harness leaves a marker that fails the NEXT run for an unrelated reason
    (that has happened twice on the sibling project). A copy has no such failure mode.

    Every replacement is ASSERTED to have matched. A mutation whose anchor never matched proves
    nothing, and it reads exactly like a guard that does not work.
    """
    d = tempfile.mkdtemp(prefix="jhwgate_")
    for f in os.listdir(SG):
        if f.endswith(".py"):
            shutil.copy(os.path.join(SG, f), os.path.join(d, f))
    p = os.path.join(d, os.path.basename(module_file))
    src = open(p, encoding="utf-8").read()
    for old, new in replacements:
        assert old in src, "anchor never matched, the mutation is meaningless: %r" % old[:70]
        src = src.replace(old, new, 1)
    open(p, "w", encoding="utf-8").write(src)
    sys.path.insert(0, d)
    try:
        spec = importlib.util.spec_from_file_location(name, p)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(d)


def mutant(module_file, replacements, name):
    """variant(), but an ANCHOR MISS becomes a legible FAILED check instead of a traceback.

    Measured, not theorised: breaking the real `panel_dissent` rule in the source made this suite
    exit with `AssertionError: anchor never matched` - which still blocks the ship (non-zero is
    non-zero) but tells the reader nothing about WHY. A negative test whose own anchor is the line
    under test collides with a real regression of that same line, and a failure message that does
    not name the mechanism costs the next person an hour.

    Returns None on a miss, having already recorded the failure.
    """
    try:
        return variant(module_file, replacements, name)
    except AssertionError as e:
        check(False, "the line this negative test mutates is NO LONGER IN %s. Either the rule was "
                     "REMOVED (a governance regression - look at the anchor below) or it was renamed "
                     "(update the anchor). %s" % (os.path.basename(module_file), e))
        return None


def rev(model, verdict, error=None):
    r = {"model": model, "verdict": verdict, "reasons": [], "risks": []}
    if error:
        r["error"] = error
    return r


def V(gate_value, reviews):
    return {"gate": gate_value, "reviews": list(reviews), "digest": "d"}


os.environ.pop("OVERRIDE_PANEL", None)

# =========================================================== 1-5  decide_from_verdict
print("\n[1-5] decide_from_verdict - who decides, and who merely advises")

g, _ = quorum.decide_from_verdict(V("NO-GO", [rev("a", "go"), rev("b", "go"),
                                              rev("c", "go"), rev("d", "go")]))
check(g == "NO-GO", "a RED gate with four happy reviewers stays NO-GO "
                    "(an agreeable model cannot wave through a dead container)")

g, _ = quorum.decide_from_verdict(V("GO", [rev("a", "go"), rev("b", "go"),
                                           rev("c", "go"), rev("d", "no-go")]))
check(g == "GO", "ONE grumpy reviewer against a green gate does NOT block the release")

g, _ = quorum.decide_from_verdict(V("GO", [rev("a", "go"), rev("b", "go"), rev("c", "go"),
                                           rev("d", "unsure", error="HTTPError")]))
check(g == "GO", "a reviewer that 429'd/timed out is a NON-ANSWER, not dissent - a rate limit "
                 "cannot block a good release")

g, _ = quorum.decide_from_verdict(V("GO", [rev("a", "go"), rev("b", "go"),
                                           rev("c", "unsure"), rev("d", "unsure")]))
check(g == "GO", "two UNSUREs with nobody actually objecting is hesitation, not a finding")

v = V("GO", [rev("a", "go"), rev("b", "go"), rev("c", "unsure"), rev("d", "no-go")])
g, dig = quorum.decide_from_verdict(v)
check(g == "NO-GO", "2 dissenters INCLUDING one hard NO-GO HALT a green gate")
check("OVERRIDE_PANEL=1" in dig and "CHECK IS LYING" in dig,
      "the halt tells the operator exactly how to override it, and why it exists")
check(v.get("halted_by_panel") is True, "the halt is recorded in the verdict, not only printed")

g, _ = quorum.decide_from_verdict(V("GO", [rev("a", "no-go"), rev("b", "no-go"),
                                           rev("c", "no-go"), rev("d", "no-go")]))
check(g == "NO-GO", "a unanimous NO-GO against a green gate HALTS")

os.environ["OVERRIDE_PANEL"] = "1"
g, _ = quorum.decide_from_verdict(V("GO", [rev("a", "no-go"), rev("b", "no-go"),
                                           rev("c", "no-go"), rev("d", "no-go")]))
check(g == "GO", "OVERRIDE_PANEL=1 lets an informed operator promote through a panel halt")
os.environ.pop("OVERRIDE_PANEL")

g, _ = quorum.decide_from_verdict(V("GO", []))
check(g == "GO", "no reviews at all (the whole panel is down) does not block a green gate")

g, _ = quorum.decide_from_verdict(V("GO", [rev("a", "NO_GO"), rev("b", "No-Go")]))
check(g == "NO-GO", "verdict strings are normalised: 'NO_GO' and 'No-Go' both count as NO-GO")

# NEGATIVE: break the "code decides" rule and prove the suite catches it.
m = mutant(os.path.join(SG, "quorum.py"),
            [('gate = str(verdict.get("gate", "NO-GO"))',
              'gate = "GO" if [r for r in (verdict.get("reviews") or []) '
              'if r.get("verdict") == "go"] else str(verdict.get("gate", "NO-GO"))')],
            "quorum_panel_decides")
gm = m and m.decide_from_verdict(V("NO-GO", [rev("a", "go"), rev("b", "go"),
                                              rev("c", "go"), rev("d", "go")]))[0]
check(m is None or gm == "GO", "NEGATIVE: a mutant that lets the panel set the gate turns the RED gate green "
                  "(so the assertion above is real, not vacuous)")

m = mutant(os.path.join(SG, "quorum.py"),
            [("verdict[\"panel_dissent\"] = len(dissent) >= 2 and len(hard) >= 1",
              "verdict[\"panel_dissent\"] = len(dissent) >= 1")],
            "quorum_one_veto")
gm = m and m.decide_from_verdict(V("GO", [rev("a", "go"), rev("b", "go"), rev("c", "go"),
                                          rev("d", "no-go")]))[0]
check(m is None or gm == "NO-GO", "NEGATIVE: a mutant where ONE reviewer can veto blocks a good release")

m = mutant(os.path.join(SG, "quorum.py"),
            [("answered = [r for r in revs if not r.get(\"error\")]",
              "answered = list(revs)")],
            "quorum_counts_errors")
gm = m and m.decide_from_verdict(V("GO", [rev("a", "go"), rev("b", "no-go"),
                                          rev("c", "unsure", error="HTTPError"),
                                          rev("d", "unsure", error="timeout")]))[0]
check(m is None or gm == "NO-GO", "NEGATIVE: a mutant that counts ERRORED reviewers as dissent lets two "
                     "timeouts plus one objection block a good release")

# =========================================================== 6  missing evidence
print("\n[6] gate_from_checks - missing evidence is a FAILURE, not a smaller pass")

C = lambda n, ok=True: {"name": n, "ok": ok, "detail": "x"}   # noqa: E731
exp = ["a", "b", "c"]
g, failed, missing = quorum.gate_from_checks([C("a"), C("b"), C("c")], exp)
check(g == "GO" and not failed and not missing, "all expected checks reported and passed -> GO")

g, failed, missing = quorum.gate_from_checks([C("a"), C("b")], exp)
check(g == "NO-GO" and missing == ["c"],
      "a check that VANISHED is NO-GO and is named ('2 of 2 passed' must never read as success)")

g, failed, _ = quorum.gate_from_checks([C("a"), C("b", False), C("c")], exp)
check(g == "NO-GO" and [f["name"] for f in failed] == ["b"], "a failing check is NO-GO")

g, _, _ = quorum.gate_from_checks([], exp)
check(g == "NO-GO", "NO evidence at all is NO-GO (an empty run is not a clean run)")

g, _, _ = quorum.gate_from_checks([C("a"), C("b"), C("c"), C("z")], exp)
check(g == "GO", "an EXTRA check is not a failure - only a missing or failing one is")

m = mutant(os.path.join(SG, "quorum.py"),
            [("missing = sorted(set(expected or []) - {c.get(\"name\") for c in checks})",
              "missing = []")],
            "quorum_no_expected")
gm = m and m.gate_from_checks([C("a"), C("b")], exp)[0]
check(m is None or gm == "GO", "NEGATIVE: a mutant that only counts what it RECEIVED calls a cut-short run a "
                  "pass - which is the exact 2026-08-16 '35/35' defect")

# gate.completeness() is the check ABOUT the checks
c = gate.completeness([C("a"), C("b")], exp, "pre-reboot", 255)
check(c["ok"] is False and "c" in c["detail"] and "255" in c["detail"],
      "completeness() names the lost check AND the ssh return code (rc is never discarded)")
c = gate.completeness([C("post_reboot_a"), C("post_reboot_b"), C("post_reboot_c")], exp,
                      "post-reboot", 0, prefix="post_reboot_")
check(c["ok"] is True and c["name"] == "post_reboot_evidence_complete",
      "completeness() strips the post_reboot_ prefix before comparing by NAME")
c = gate.completeness([C("post_reboot_a")], exp, "post-reboot", 0, prefix="post_reboot_")
check(c["ok"] is False and "b, c" in c["detail"],
      "post-reboot checks are compared BY NAME to the committed list, so six vanishing is caught")

# =========================================================== 7-8  the evidence contract
print("\n[7-8] the CHECK| contract, and the self-contradiction demoter")

txt = "\n".join([
    "noise before",
    "CHECK|container_running|yes|jhw-web is running",
    "CHECK|api_auth|no|GET /api/me -> 200, want 401",
    "not a check line",
    "CHECK|detail_with_pipe|yes|a | b | c",
    "CHECK|malformed|yes",
])
cs = gate.parse_checks(txt)
check([c["name"] for c in cs] == ["container_running", "api_auth", "detail_with_pipe"],
      "only well-formed CHECK| lines are parsed; a malformed one is ignored, noise cannot inject")
check(cs[0]["ok"] is True and cs[1]["ok"] is False, "the ok field is exactly 'yes', nothing else")
check(cs[2]["detail"] == "a | b | c",
      "split(|,3) keeps a pipe inside the DETAIL (chk squashes them at source; the parser must not "
      "lose the tail either way)")

d = gate.parse_checks("CHECK|config|yes|the live file is STALE and could not be reloaded")
check(d[0]["ok"] is False and "SELF-CONTRADICTORY" in d[0]["detail"],
      "a PASS whose detail describes a failure is DEMOTED to a failure automatically")
check("'stale'" in d[0]["detail"].lower() and "..." in d[0]["detail"],
      "the demotion shows the MATCH IN CONTEXT (a bare 5-char match once failed a green run and "
      "cost a deploy cycle to diagnose)")
d = gate.parse_checks("CHECK|secret_not_baked_into_image|yes|image jhw-web:local env carries no "
                      "secret keys")
check(d[0]["ok"] is True,
      "a benign phrase containing a scary word ('no secret keys') is NOT demoted")
d = gate.parse_checks("CHECK|events_written|yes|3 lines in the log (proves the WRITE; on the twin "
                      "there is no promtail/Loki, so it does not prove the pipeline)")
check(d[0]["ok"] is True, "an honest limitation in a PASS detail is not a contradiction")

m = mutant(os.path.join(SG, "gate.py"), [("        if word:\n", "        if False:\n")],
            "gate_no_demote")
d = m and m.parse_checks("CHECK|config|yes|the live file is STALE and could not be reloaded")
check(m is None or d[0]["ok"] is True,
      "NEGATIVE: a mutant with the demoter disabled accepts a self-contradictory PASS")

# =========================================================== 9  wrapping, not truncating
print("\n[9] say_check WRAPS the detail - truncating destroys the evidence the check exists for")
long = "A" * 260 + "-END-OF-DETAIL"
buf = []
gate.say_check(buf.append, {"name": "x", "ok": True, "detail": long})
joined = "".join(l.strip() for l in buf)
check("-END-OF-DETAIL" in joined, "the LAST characters of a 274-char detail survive")
check(len(buf) > 1, "it wrapped onto several lines rather than cutting")
m = mutant(os.path.join(SG, "gate.py"),
            [('say("  %-*s %s  %s" % (pad, c["name"], "OK  " if c["ok"] else "FAIL", d[:width]))',
              'say("  %-*s %s  %s" % (pad, c["name"], "OK  " if c["ok"] else "FAIL", d[:80]))\n'
              '    return')],
            "gate_truncates")
buf2 = []
if m:
    m.say_check(buf2.append, {"name": "x", "ok": True, "detail": long})
check(m is None or "-END-OF-DETAIL" not in "".join(buf2),
      "NEGATIVE: a mutant that truncates loses the tail (so the assertion above is real)")

# =========================================================== 10-11  bounded autonomy
print("\n[10-11] consensus(): quorum on the direction, MEDIAN, clamp ON READ - and NOT WIRED")

B = {"k": (5, 100)}
cur = {"k": 20}
ups = [{"propose": {"k": 30}}, {"propose": {"k": 24}}, {"propose": {"k": 40}}, {"propose": {"k": 10}}]
agreed, notes = quorum.consensus(ups, cur, bounds=B)
check("k" in agreed, "3 of 4 agreeing on the DIRECTION moves the key")
check(agreed["k"] == 25, "the applied value is the MEDIAN of the agreeing side, then step-capped "
                         "to +25%% (30/24/40 -> median 30 -> capped to 25), got %r" % agreed.get("k"))

split = [{"propose": {"k": 30}}, {"propose": {"k": 32}},
         {"propose": {"k": 10}}, {"propose": {"k": 8}}]
agreed, notes = quorum.consensus(split, cur, bounds=B)
check(not agreed and any("below the quorum" in n for n in notes),
      "a 2-2 split changes NOTHING, and the refusal is REPORTED rather than swallowed")

agreed, notes = quorum.consensus([{"propose": {"nope": 9}}] * 4, {"nope": 1}, bounds=B)
check(not agreed and any("not a tunable" in n for n in notes),
      "an unknown key can never become a value by accident")

check(quorum.clamp("k", 9999, bounds=B) == 100, "clamp() forces a value into the COMMITTED bounds")
check(quorum.clamp("k", -5, bounds=B) == 5, "clamp() forces a value up to the lower bound")
check(quorum.clamp("unknown", 7, bounds=B) is None, "an unbounded key clamps to None, not a value")
check(quorum.clamp("k", "banana", bounds=B) is None, "garbage clamps to None, never to a number")
check(quorum.clamp("k", 90, current=40, bounds=B) == 50,
      "the STEP is capped at 25% per cycle (40 -> 90 becomes 50)")
check(quorum.tuning_is_wired() is False and quorum.BOUNDS == {},
      "NOTHING is tunable at this stage of the port (section 7.2 step 5 is not earned yet) - this "
      "assertion is what stops it being switched on quietly")

# =========================================================== 12  the prompts must render
print("\n[12] the briefing and the prompts render - a stray % is a runtime crash, not a typo")
ev = {"host": "203.0.113.9", "checks": [C("a"), C("b", False)], "expected": ["a", "b"],
      "reboot": {"came_back": True, "seconds": 41}}
try:
    s = quorum.SOLDIER_PROMPT % (quorum._GOVERNANCE, quorum.ARCH + "\n" + json.dumps(ev))
    a = quorum.AUDITOR_PROMPT % (quorum._GOVERNANCE, quorum.ARCH + "\n" + json.dumps(ev))
    check(len(s) > 4000 and len(a) > 4000, "both prompts %%-format cleanly (%d / %d chars)"
          % (len(s), len(a)))
    check("ROOT CAUSE" in s and "ROOT CAUSE" in a,
          "the governance paragraph (root cause + name the file) is in BOTH prompts, verbatim")
    low = s.lower()
    absent = [w for w in ("kubernetes", "config-map", "hot-reload", "queue", "broker", "worker")
              if ("no " + w) in low]
    check(len(absent) >= 5 and "do not propose" in low,
          "the briefing declares what does NOT exist (%s) and says not to propose it, so the panel "
          "cannot invent architecture it cannot see" % ", ".join(absent))
    check("WHAT EACH CHECK ALREADY MEASURES" in a,
          "the auditor is told what each check already measures (a briefing with a hole in it costs "
          "a review slot every run)")
except Exception as e:
    check(False, "prompts render: %r" % (e,))

dig = quorum.build_digest(ev, [C("a"), C("b", False)], ["a", "b", "gone"],
                          [C("b", False)], ["gone"], "NO-GO",
                          [dict(role="soldier", **rev("deepseek-3.2", "no-go"))])
check("MISSING EVIDENCE" in dig and "gone" in dig, "the digest names missing evidence explicitly")
check("ADVISORY" in dig, "the digest says out loud that the panel does not set the gate")
check("BELOW QUORUM" in dig,
      "a panel below quorum ANNOUNCES that the halt safeguard cannot fire this run - a safeguard "
      "that cannot fire must never be silent about it")

# =========================================================== 13  the panel's model call
print("\n[13] ask_one: the real payload builder, the real repair path, a STUBBED transport")
# `app` resolves to backend/app as a namespace package, which is exactly how it resolves inside the
# container (WORKDIR /app, `COPY backend/app ./app`). Nothing here touches the network: quorum._post
# is replaced, and the assertion below proves the replacement was actually used.
sys.path.insert(0, os.path.join(ROOT, "backend"))
sent = []


def _stub(payload, timeout=None):
    sent.append(json.loads(json.dumps(payload)))
    if len(sent) == 1 and payload.get("model", "").startswith("kimi-fail"):
        raise quorum._HTTP(400, '{"message":"temperature must be 0.42 for this model"}')
    body = {"verdict": "go", "diagnosis": "all clear", "proposed_fix": "",
            "reasons": ["container_running passed", "engine_fresh matched"],
            "risks": []}
    return ({"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}],
             "usage": {"completion_tokens": 120}}, "")


_real_post = quorum._post
try:
    quorum._post = _stub
    r = quorum.ask_one("deepseek-3.2", "EVIDENCE: ...")
    check(r.get("verdict") == "go" and not r.get("error"),
          "a well-formed answer is parsed into a review (%r)" % r.get("verdict"))
    check(r["reasons"] and r["diagnosis"] == "all clear",
          "reasons and the diagnosis survive into the digest input")
    check(len(sent) == 1 and sent[0]["model"] == "deepseek-3.2",
          "the stub transport was actually used - this test reached no network")
    check("response_format" in sent[0] and "max_tokens" not in sent[0],
          "JSON mode is kept and the ceiling is dropped: on this gateway max_tokens and "
          "response_format are MUTUALLY EXCLUSIVE, and a ceiling cut lands MID-JSON")

    sent.clear()
    r = quorum.ask_one("kimi-k2.6", "EVIDENCE: ...")
    check(sent and sent[0].get("temperature") == 0.6 and "response_format" not in sent[0],
          "kimi's published constraints are ENCODED, not rediscovered: temperature 0.6 and NO "
          "response_format (payload=%s)"
          % {k: v for k, v in (sent[0] if sent else {}).items() if k != "messages"})
    check(sent and sent[0].get("max_tokens") == quorum.MAX_TOKENS,
          "kimi keeps its ceiling (it refuses JSON mode, so the mutual exclusion does not apply)")
    check(sent and sent[0].get("chat_template_kwargs") == {"enable_thinking": False},
          "chain-of-thought stays suppressed - a thinking model cannot honour a strict-JSON contract")

    sent.clear()
    r = quorum.ask_one("kimi-fail-once", "EVIDENCE: ...")
    check(len(sent) == 2 and sent[1].get("temperature") == 0.42,
          "a 400 is repaired by WHAT THE SERVER NAMED: the temperature is READ OUT OF THE BODY and "
          "re-sent (the required value has changed under us before, so the body outranks our table)")
    check(sent[1].get("chat_template_kwargs") == {"enable_thinking": False},
          "the retry does NOT blanket-strip chat_template_kwargs - doing that once re-enabled "
          "thinking and produced 46,801 characters and a truncated non-answer")
    check(r.get("verdict") == "go", "and the repaired call succeeds")

    quorum._post = lambda payload, timeout=None: (
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}, "")
    r = quorum.ask_one("deepseek-3.2", "x")
    check(r.get("error") and r["verdict"] == "unsure",
          "an EMPTY answer is rejected, not recorded as agreement - '{}' parses fine and is a "
          "silent quality failure")

    def _boom(payload, timeout=None):
        raise quorum._HTTP(429, "rate limited")

    quorum._post = _boom
    r = quorum.ask_one("gemma-4-31B-it", "x")
    check(r["verdict"] == "unsure" and r.get("error"),
          "a 429 is recorded as a NON-ANSWER and never raises - the release must not depend on the "
          "panel answering")
finally:
    quorum._post = _real_post

check(quorum.SOLDIERS == ["deepseek-3.2", "llama-4-maverick"]
      and quorum.AUDITORS == ["gemma-4-31B-it", "kimi-k2.6"],
      "2 soldiers + 2 auditors, one per vendor: %s / %s" % (quorum.SOLDIERS, quorum.AUDITORS))
vendors = set()
try:
    from app import resume_consensus as _RC
    vendors = {_RC.vendor(m) for m in quorum.SOLDIERS + quorum.AUDITORS}
    check(len(vendors) == 4, "all four reviewers are DIFFERENT VENDORS (%s) - a 429 or a blind spot "
                             "is provider-wide, so four on one provider is one model in four hats"
          % sorted(vendors))
    check(set(quorum.SOLDIERS) & set(quorum.AUDITORS) == set(),
          "no model both writes and audits (nothing marks its own work)")
except Exception as e:
    check(False, "could not resolve vendors via resume_consensus.vendor: %r" % (e,))

# =========================================================== THE GATE (LAST STATEMENT)
print("\n%s\n%d checks run, %d failed" % ("=" * 74, RUN[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)
