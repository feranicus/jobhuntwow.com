#!/usr/bin/env python3
"""
probe_models.py — see every model your DigitalOcean key can reach, and pick a real backup chain.

  python probe_models.py --list          # JUST the catalog. One API call, ~1 second.
  python probe_models.py                 # catalog + live probe of a fast/smart shortlist (EN)
  python probe_models.py --lang de       # ...and check the German output too
  python probe_models.py --models a,b    # probe exactly these
  python probe_models.py --timeout 40    # per-call seconds (default 25)

WHY a chain and not "the best model": the decks are rendered by deterministic JS (pptxgenjs); the
LLM only returns a JSON blob of prose. So we need: reachable on this account, contract-valid JSON,
good business German, fast, cheap. And the BACKUP MUST BE A DIFFERENT VENDOR — a 429/outage is
provider-wide, so deepseek -> another deepseek is the same failure twice.

Read-only: runs inside colt-web via docker exec, using the key that is already there. Output streams
live (no silent 10-minute wait).
"""
import argparse, os, subprocess, sys

HOST = os.environ.get("DROPLET_HOST", "64.225.108.200")
USER = os.environ.get("DROPLET_USER", "root")
KEY  = os.environ.get("SSH_KEY", "")
SSH  = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"] + (["-i", KEY] if KEY and os.path.exists(KEY) else [])
CT   = os.environ.get("COLT_CONTAINER", "colt-web")

PROBE = r'''
import json, os, sys, time, urllib.request, urllib.error
BASE = os.environ.get("OPENAI_BASE_URL", "https://inference.do-ai.run/v1").rstrip("/")
KEY  = os.environ.get("OPENAI_API_KEY", "")
LANG = os.environ.get("PROBE_LANG", "en")
ONLY_LIST = os.environ.get("PROBE_LIST") == "1"
TMO  = int(os.environ.get("PROBE_TIMEOUT", "75"))   # deepseek-3.2 measured ~63s — 25s was too tight
WANT = [m for m in os.environ.get("PROBE_MODELS", "").split(",") if m.strip()]

def out(s=""): print(s, flush=True)
if not KEY:
    out("[X] no OPENAI_API_KEY inside the container"); raise SystemExit(1)

def vendor(m):
    m = m.lower()
    for v in ("anthropic", "openai", "gpt-oss", "deepseek", "qwen", "llama", "mistral", "google", "fal", "nomic"):
        if v in m: return v
    return "other"

# ---------- 1. catalog: ONE call, always fast ----------
t = time.time()
try:
    rq = urllib.request.Request(BASE + "/models", headers={"Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(rq, timeout=25) as r:
        catalog = sorted(m.get("id") for m in json.loads(r.read()).get("data", []))
except Exception as e:
    out("[X] /models failed: %r" % e); raise SystemExit(1)

out("=" * 78)
out("  DIGITALOCEAN MODEL CATALOG — %d models visible to this key   (%dms)" % (len(catalog), int((time.time()-t)*1000)))
out("=" * 78)
byv = {}
for m in catalog: byv.setdefault(vendor(m), []).append(m)
for v in sorted(byv):
    out("\n  %s (%d)" % (v.upper(), len(byv[v])))
    for m in byv[v]: out("     %s" % m)
if ONLY_LIST:
    out("\n  (--list: catalog only, no probing)"); raise SystemExit(0)

# ---------- 2. shortlist: fast + smart only ----------
# EVIDENCE from the live probe on this account:
#   * every anthropic-* and commercial openai-* model -> http-403 Forbidden (DO Tier 1/2 blocks them;
#     gpt-oss-* is the documented exception). So the chain must be OPEN-WEIGHT models.
#   * deepseek-3.2            -> ok, German ok (but ~63s: slow, hence a faster backup matters)
#   * deepseek-r1-distill-70b -> bad-contract  (REASONING model: emits thinking, not strict JSON)
#   * qwen3.5-397b-a17b       -> bad-contract  (same problem)
# So: open-weight INSTRUCT models only, one per vendor, no reasoning/thinking variants.
PREFERRED = [
    "deepseek-3.2",                 # proven head: contract-valid, good German, cheapest
    "openai-gpt-oss-120b",          # Apache-2.0 open weights; the ONE openai id Tier 1/2 may call
    "glm-5.2", "glm-5.1", "glm-5",  # Zhipu GLM — open weights, strong instruction-following
    "kimi-k2.6", "kimi-k2.5",       # Moonshot — open weights, built for tool/JSON output
    "llama-4-maverick",             # Meta — open weights
    "minimax-m2.5",
    "mistral-3-14B",
    "gemma-4-31B-it",               # Google open weights
    "nvidia-nemotron-3-super-120b",
    "openai-gpt-oss-20b",           # smaller/faster last-ditch
    "deepseek-4-flash",             # same vendor as head (poor backup) but fast — probed for info
]
# Never probe these: reasoning/thinking models break strict JSON; the rest are not text generators.
SKIP = ("embed", "rerank", "whisper", "tts", "image", "guard", "mini-lm", "bge-", "all-mini", "nomic",
        "e5-large", "gte-large", "multi-qa", "stable-diffusion", "wan2", "-codex", "coder",
        "thinking", "distill", "openai-o1", "openai-o3", "router:")
if WANT:
    cands = WANT
else:
    cands = [m for m in PREFERRED if m in catalog]
    have = {vendor(m) for m in cands}
    for m in catalog:                       # make sure no OPEN-WEIGHT vendor family is missed
        ml = m.lower()
        if any(s in ml for s in SKIP): continue
        if ml.startswith("anthropic-") or (ml.startswith("openai-") and "oss" not in ml):
            continue                        # 403 on this tier — proven, do not waste a probe slot
        if vendor(m) not in have:
            cands.append(m); have.add(vendor(m))
    cands = cands[:9]

CONTRACT = ('Return ONLY strict JSON, no prose, no markdown fence: '
            '{"exec_summary":"<2 sentences>","findings":[{"id":"C1","why":["..."]}],"strengths":["..."]} '
            "Subject: an internet-exposed PostgreSQL database on a manufacturer's network edge.")
if LANG == "de":
    CONTRACT += " Schreibe ALLE Fliesstexte auf Hochdeutsch (Sie-Form). JSON-Schluessel bleiben englisch."

out("\n" + "=" * 78)
out("  PROBING %d candidates with the REAL enrichment contract (lang=%s, %ds timeout each)" % (len(cands), LANG, TMO))
out("=" * 78)
out("\n  %-30s %-9s %8s %7s  %s" % ("model", "vendor", "status", "ms", "note"))
out("  " + "-" * 74)

res = []
for m in cands:
    rec = {"model": m, "vendor": vendor(m)}
    t = time.time()
    def _ask(strict=True, tmo=TMO):
        body = {"model": m, "messages": [{"role": "user", "content": CONTRACT}],
                "temperature": 0.3, "max_tokens": 400}
        if strict: body["response_format"] = {"type": "json_object"}
        rq = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
             headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=tmo) as r:
            return json.loads(r.read())
    try:
        try:
            d = _ask(True)
        except urllib.error.HTTPError as e:
            if e.code in (400, 422):
                # exactly what enrich.py does: some models reject response_format. Retrying without
                # it is why kimi-* showed http-400 here but may be perfectly usable in production.
                rec["note_extra"] = "no response_format"; d = _ask(False)
            elif e.code == 429:
                time.sleep(4); d = _ask(True)      # account quota is transient — give it one retry
            else:
                raise
        rec["ms"] = int((time.time() - t) * 1000)
        msg = d["choices"][0]["message"]
        txt = msg.get("content") or msg.get("reasoning_content") or ""
        try:
            a = txt.find("{"); j, _ = json.JSONDecoder().raw_decode(txt[a:])
            rec["contract_ok"] = ("exec_summary" in j)
            s = str(j.get("exec_summary", ""))
            rec["sample"] = s[:60]
            if LANG == "de":
                de = sum(w in s for w in ("Datenbank","exponiert","Internet","Sie","Risiko","Angreifer","Schaden","wird","ist"))
                en = sum(w in s for w in (" the "," is "," and ","database","exposed"))
                rec["german"] = de > en
            rec["status"] = "ok" if rec["contract_ok"] else "bad-json"
            rec["note"] = ("German OK" if rec.get("german") else "NOT German") if LANG == "de" else "contract OK"
            if rec.get("note_extra"): rec["note"] += " (" + rec["note_extra"] + ")"
            if not rec["contract_ok"]: rec["note"] = "no exec_summary"
        except Exception:
            rec.update(status="bad-json", contract_ok=False, note="not JSON", sample=txt[:60])
    except urllib.error.HTTPError as e:
        rec.update(status="http-%d" % e.code, ms=int((time.time()-t)*1000), contract_ok=False,
                   note={401:"no access on this key",403:"forbidden on this tier",
                         404:"not on this endpoint",429:"account quota/balance"}.get(e.code, e.reason))
    except Exception as e:
        rec.update(status="timeout" if "timed out" in repr(e) else "error",
                   ms=int((time.time()-t)*1000), contract_ok=False, note=repr(e)[:22])
    res.append(rec)
    out("  %-30s %-9s %8s %7s  %s" % (m[:30], rec["vendor"], rec["status"], rec.get("ms","-"), rec.get("note","")[:24]))

ok = sorted([r for r in res if r.get("contract_ok") and (LANG != "de" or r.get("german"))],
            key=lambda x: x["ms"])
out("\n" + "=" * 78)
if not ok:
    out("  NOTHING passed. If everything is http-429 that is an ACCOUNT quota or an empty prepaid")
    out("  balance — a different model cannot fix it. See https://cloud.digitalocean.com/limits")
    raise SystemExit(0)

best, seen = [], set()
for r in ok:
    if r["vendor"] not in seen:
        seen.add(r["vendor"]); best.append(r)
out("  RECOMMENDED CHAIN — best model per VENDOR (a 429 is provider-wide, so the backup must be")
out("  a different vendor; deepseek -> deepseek is the same failure twice)")
out("=" * 78)
for i, r in enumerate(best[:3]):
    out("   %d. %-30s %-9s %5dms   %s" % (i+1, r["model"], r["vendor"], r["ms"], r.get("sample","")[:40]))
out("\n  Paste into assess-bot/.env on the droplet, then re-run  python deploy_web_direct.py :")
out('    ENRICH_MODELS="%s"' % ",".join(r["model"] for r in best[:3]))
out('    ENRICH_TIMEOUT=90')
out("  (ENRICH_MODEL is legacy — a single value now just becomes the head of the chain.)")
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="only list the catalog (1 API call, instant)")
    ap.add_argument("--lang", default="en", choices=["en", "de"])
    ap.add_argument("--models", default="")
    ap.add_argument("--timeout", default="25")
    a = ap.parse_args()

    env = "-e PROBE_LANG=%s -e PROBE_TIMEOUT=%s" % (a.lang, a.timeout)
    if a.list:   env += " -e PROBE_LIST=1"
    if a.models: env += " -e PROBE_MODELS=%s" % a.models
    # python3 -u + no capture => output streams live instead of a silent wait
    cmd = SSH + ["%s@%s" % (USER, HOST), "docker exec %s -i %s python3 -u -" % (env, CT)]
    r = subprocess.run(cmd, input=PROBE.encode("utf-8"))
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
