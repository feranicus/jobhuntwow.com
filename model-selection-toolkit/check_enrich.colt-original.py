#!/usr/bin/env python3
"""
check_enrich.py — show EXACTLY what the model returned on the last enrichment (read-only).

enrich.py already dumps every raw model answer to <jobdir>/enrich_last.json. When a run falls back to
English templates, this tells you WHY in one command instead of guessing at the shape:
  * the JSON type it returned (dict / list)
  * the types of each findings entry  <- the Bezeq AttributeError lived here
  * finish_reason (a 'length' means we truncated it -> raise max_tokens, not a parser bug)
  * the first 800 chars of the raw answer

  python check_enrich.py            # newest job on the droplet
  python check_enrich.py <job_id>
"""
import json, os, subprocess, sys

HOST = os.environ.get("DROPLET_HOST", "64.225.108.200")
USER = os.environ.get("DROPLET_USER", "root")
KEY  = os.environ.get("SSH_KEY", "")
SSH  = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"] + (["-i", KEY] if KEY and os.path.exists(KEY) else [])

REMOTE = r'''
import glob, json, os, sys
pat = "/data/jobs/*/%s/enrich_last.json" % (sys.argv[1] if len(sys.argv) > 1 else "*")
files = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
if not files:
    print("no enrich_last.json found (has an assessment run since the last deploy?)"); raise SystemExit
p = files[0]
d = json.load(open(p))
print("file        :", p)
print("model       :", d.get("model"))
print("finish      :", d.get("finish"), " <- 'length' means WE truncated it (raise max_tokens)")
print("usage       :", d.get("usage"))
raw = d.get("raw") or ""
print("raw length  :", len(raw), "chars")
i = min([x for x in (raw.find("{"), raw.find("[")) if x >= 0] or [-1])
if i < 0:
    print("!! no JSON value at all in the answer"); raise SystemExit
try:
    obj, _ = json.JSONDecoder().raw_decode(raw[i:])
except Exception as e:
    print("!! raw_decode failed:", repr(e)); print(raw[:800]); raise SystemExit
print("top-level   :", type(obj).__name__)
if isinstance(obj, dict):
    print("keys        :", sorted(obj))
    f = obj.get("findings")
    print("findings    :", type(f).__name__, "->", [type(x).__name__ for x in f][:8] if isinstance(f, list) else f)
elif isinstance(obj, list):
    print("list of     :", [type(x).__name__ for x in obj][:8])
print("\n--- first 800 chars ---")
print(raw[:800])
'''

def main():
    job = sys.argv[1] if len(sys.argv) > 1 else "*"
    r = subprocess.run(SSH + ["%s@%s" % (USER, HOST),
                              "docker exec -i colt-web python3 - %s" % job],
                       input=REMOTE.encode())
    sys.exit(r.returncode)

if __name__ == "__main__":
    main()
