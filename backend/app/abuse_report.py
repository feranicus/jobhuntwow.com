"""
abuse_report.py — submit hostile IPs to AbuseIPDB (the correct, community-consumed way).

WHY AbuseIPDB and NOT auto-emailing BSI/ENISA/ISP abuse desks:
  * BSI and ENISA do not ingest individual-operator abuse reports — it is not their function.
  * A server that auto-blasts abuse email daily gets ITS OWN domain flagged as an abuse source and
    blocklisted, which is fatal for a host that sends OTP + reports over the same domain.
  * AbuseIPDB is purpose-built: one HTTPS POST per IP, categorised, deduplicated by their side,
    consumed by firewalls/WAFs worldwide. That is the KISS-correct channel.

OPT-IN: does nothing unless ABUSEIPDB_KEY is set. Rate-limited + deduped locally so we never double
report an IP within a day (their free tier is 1,000 reports/day; we send a handful).
Categories: https://www.abuseipdb.com/categories  (14=port scan, 21=web app attack, 18=brute force,
19=bad web bot, 15=hacking).
"""
import json, os, time, urllib.parse, urllib.request

KEY   = os.environ.get("ABUSEIPDB_KEY", "")
STATE = os.path.join(os.environ.get("DATA_DIR", "/data"), "abuseipdb_sent.json")
DEDUP_H = int(os.environ.get("ABUSEIPDB_DEDUP_HOURS", "24"))

# our rule -> AbuseIPDB category ids
RULE_CAT = {
    "path_probe": [21, 14], "dir_bruteforce": [21, 19], "ip_burst": [14],
    "authz_probe": [21], "login_failed": [18], "password_spray": [18],
    "otp_bruteforce": [18], "download_burst": [21], "ddos": [4], "session_multi_ip": [15],
}


def _load():
    try: return json.load(open(STATE))
    except Exception: return {}


def _save(d):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(d, open(STATE, "w"))
    except Exception: pass


def _report_one(ip, categories, comment):
    data = urllib.parse.urlencode({"ip": ip, "categories": ",".join(str(c) for c in sorted(set(categories))),
                                   "comment": comment[:1024]}).encode()
    req = urllib.request.Request("https://api.abuseipdb.com/api/v2/report", data=data,
          headers={"Key": KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def report_digest(digest, dry_run=False):
    """Take threat_intel.build() output and submit each hostile IP. Returns a summary."""
    if not KEY and not dry_run:
        return {"status": "disabled", "reason": "ABUSEIPDB_KEY not set (reporting is opt-in)"}
    sent = _load()
    now = time.time()
    out = []
    for a in digest.get("attackers", []):
        ip = a["ip"]
        # skip research scanners you may not want to flag (Censys/Shodan do legitimate research)
        if a.get("provider", "").startswith(("Censys",)) and os.environ.get("ABUSEIPDB_SKIP_RESEARCH", "1") == "1":
            out.append({"ip": ip, "status": "skipped-research"}); continue
        if now - float(sent.get(ip, 0)) < DEDUP_H * 3600:
            out.append({"ip": ip, "status": "already-reported"}); continue
        cats = sorted({c for r in a.get("rules", []) for c in RULE_CAT.get(r, [14])}) or [14]
        mitre = " ".join(m["id"] for m in a.get("mitre", []))
        comment = ("Automated recon/scan against jobhuntwow.com. Rules: %s. MITRE: %s. "
                   "Sample paths: %s. Detected by jobhuntwow.com security monitoring."
                   % (",".join(a.get("rules", [])) or "scan", mitre or "T1595.003",
                      ", ".join(a.get("sample_paths", [])[:5])))
        if dry_run:
            out.append({"ip": ip, "status": "would-report", "categories": cats, "comment": comment})
            continue
        try:
            _report_one(ip, cats, comment)
            sent[ip] = now
            out.append({"ip": ip, "status": "reported", "categories": cats})
        except Exception as e:
            out.append({"ip": ip, "status": "error", "error": repr(e)[:120]})
    if not dry_run:
        _save(sent)
    return {"status": "ok" if KEY else "dry-run", "results": out,
            "reported": sum(1 for r in out if r["status"] == "reported")}


if __name__ == "__main__":
    import sys
    from importlib import import_module
    sys.path.insert(0, os.path.dirname(__file__))
    ti = import_module("threat_intel")
    dry = "--send" not in sys.argv
    d = ti.build(24, sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None)
    print(json.dumps(report_digest(d, dry_run=dry), indent=2))
    if dry:
        print("\n(dry-run — add --send and set ABUSEIPDB_KEY to actually submit)")
