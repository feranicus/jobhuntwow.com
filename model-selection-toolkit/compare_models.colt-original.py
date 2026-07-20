#!/usr/bin/env python3
"""
compare_models.py — settle "which model writes the better deck" with the REAL artifact, not benchmarks.

MMLU does not tell you whose German loss-narrative a CISO believes. This runs the SAME findings.json
through each model using the EXACT enrichment prompt the engine uses (same DELTAS bible, same LANG_DE
block, same strict-JSON contract), then prints the prose side by side with latency and cost.

  python compare_models.py                          # newest assessment on the droplet, DE
  python compare_models.py --lang en
  python compare_models.py --models deepseek-3.2,llama-4-maverick,openai-gpt-oss-120b
  python compare_models.py --findings /data/jobs/.../findings.json
  python compare_models.py --field realComparable   # focus on one contract field

Read-only: docker exec into colt-web, uses the key already there, writes nothing.
"""
import argparse, os, subprocess, sys

HOST = os.environ.get("DROPLET_HOST", "64.225.108.200")
USER = os.environ.get("DROPLET_USER", "root")
KEY  = os.environ.get("SSH_KEY", "")
SSH  = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"] + (["-i", KEY] if KEY and os.path.exists(KEY) else [])
CT   = os.environ.get("COLT_CONTAINER", "colt-web")

REMOTE = r'''
import glob, json, os, sys, time, textwrap
sys.path.insert(0, "/opt/shodan-skill/scripts")
import enrich as E                       # the REAL prompt, bible and contract — no re-implementation

LANG   = os.environ.get("CMP_LANG", "de")
FIELD  = os.environ.get("CMP_FIELD", "")
MODELS = [m.strip() for m in os.environ.get("CMP_MODELS", "").split(",") if m.strip()] or E._FALLBACKS

def out(s=""): print(s, flush=True)

# --- pick the findings.json to test against ---
fp = os.environ.get("CMP_FINDINGS", "")
if not fp:
    cands = sorted(glob.glob("/data/jobs/*/*/findings.json"), key=os.path.getmtime, reverse=True)
    if not cands:
        out("[X] no findings.json under /data/jobs — run one assessment first"); raise SystemExit(1)
    fp = cands[0]
fj = json.load(open(fp))
company = fj.get("target", {}).get("company", "?")
n = len(fj.get("findings", []))
out("=" * 96)
out("  MODEL BAKE-OFF — same findings, same prompt, same contract.   lang=%s" % LANG)
out("  input : %s" % fp)
out("  target: %s  (%d findings)" % (company, n))
out("=" * 96)

# --- build the EXACT prompt enrich.py would send ---
slim = {"company": company, "scope": fj["target"].get("scope", ""),
        "findings": [{"id": f["id"], "sev": f["sev"], "title": f["title"],
                      "evidence": f.get("evidence", [])} for f in fj["findings"]]}
prompt = E.PROMPT % (E._bible(), (E.LANG_DE if LANG.startswith("de") else ""),
                     json.dumps(slim, ensure_ascii=False))
out("  prompt: %d chars (%s)\n" % (len(prompt), "LANG_DE block included" if LANG.startswith("de") else "English"))

res = []
for m in MODELS:
    out("-" * 96)
    out("  %s" % m)
    out("-" * 96)
    t = time.time()
    try:
        content, usage = E._call(prompt, m, 180)
        ms = int((time.time() - t) * 1000)
        j = E._json(content)
        ti = int(usage.get("prompt_tokens", 0)); to = int(usage.get("completion_tokens", 0))
        cost = round((ti + to) / 1e6 * E._price(m), 6)
        ex = str(j.get("exec_summary", "") or "")
        de_hits = sum(w in ex for w in ("Sie", "der", "die", "das", "und", "ist", "wird", "Risiko"))
        en_hits = sum(w in ex for w in (" the ", " is ", " and ", " of ", "exposed"))
        rec = {"model": m, "ms": ms, "cost": cost, "tokens": ti + to,
               "german": de_hits > en_hits, "exec": ex,
               "findings_rewritten": len(j.get("findings", []) or []),
               "strengths": len(j.get("strengths", []) or []),
               "mitigation": len(j.get("colt_mitigation", []) or []),
               "comparables": [str(x.get("realComparable", "")) for x in (j.get("findings") or [])
                               if x.get("realComparable")]}
        res.append(rec)
        out("  %dms · %d tok · ~$%.4f · German=%s · findings rewritten=%d · strengths=%d · colt_mitigation=%d"
            % (ms, rec["tokens"], cost, rec["german"], rec["findings_rewritten"], rec["strengths"], rec["mitigation"]))
        out("\n  EXEC SUMMARY (this is slide 2 of the Findings deck):")
        for l in textwrap.wrap(ex, 90) or ["(empty)"]: out("    " + l)
        if rec["comparables"]:
            out("\n  realComparable — the DATED PUBLIC BREACH it cites (hallucination risk lives here):")
            for c in rec["comparables"][:3]:
                for i, l in enumerate(textwrap.wrap(c, 88)[:2]): out("    " + ("- " if i == 0 else "  ") + l)
        else:
            out("\n  realComparable: NONE produced  <- C-BIQ precedent slides would stay templated")
        if FIELD:
            out("\n  --field %s:" % FIELD)
            for f_ in (j.get("findings") or [])[:3]:
                v = f_.get(FIELD)
                if v: out("    %s: %s" % (f_.get("id"), (v if isinstance(v, str) else " / ".join(map(str, v)))[:150]))
    except Exception as e:
        out("  [X] FAILED after %dms: %r" % (int((time.time()-t)*1000), e))
    out()

if len(res) > 1:
    out("=" * 96)
    out("  SCOREBOARD")
    out("=" * 96)
    out("  %-24s %8s %10s %8s %9s %10s %12s" % ("model", "ms", "cost", "German", "rewritten", "strengths", "precedents"))
    for r in sorted(res, key=lambda x: x["ms"]):
        out("  %-24s %8d %10.4f %8s %9d %10d %12d"
            % (r["model"][:24], r["ms"], r["cost"], r["german"], r["findings_rewritten"],
               r["strengths"], len(r["comparables"])))
    out("\n  How to read this: latency and cost are facts. 'rewritten/strengths/precedents' show how")
    out("  much of the deck each model actually fills in — a model that returns 0 precedents leaves")
    out("  your C-BIQ slides templated. The exec summaries above are the real test: read them as a")
    out("  German CISO would. Cheapest+fastest is NOT the win condition; a credible deck is.")
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="de", choices=["en", "de"])
    ap.add_argument("--models", default="", help="default: the live chain from enrich.py")
    ap.add_argument("--findings", default="", help="path INSIDE the container (default: newest job)")
    ap.add_argument("--field", default="", help="also dump one contract field (why/what/rem/realComparable)")
    a = ap.parse_args()
    env = "-e CMP_LANG=%s" % a.lang
    if a.models:   env += " -e CMP_MODELS=%s" % a.models
    if a.findings: env += " -e CMP_FINDINGS=%s" % a.findings
    if a.field:    env += " -e CMP_FIELD=%s" % a.field
    cmd = SSH + ["%s@%s" % (USER, HOST), "docker exec %s -i %s python3 -u -" % (env, CT)]
    sys.exit(subprocess.run(cmd, input=REMOTE.encode("utf-8")).returncode)


if __name__ == "__main__":
    main()
