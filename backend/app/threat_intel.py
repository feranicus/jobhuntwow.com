"""
threat_intel.py — turn raw evt=http / security_alert lines into an attacker-centric daily digest.

Deterministic MITRE ATT&CK mapping (a lookup table, NOT an LLM): for commodity web recon the
technique is unambiguous and a static map is more reliable, faster and free. The LLM is reserved for
the human-readable narrative if ever wanted — never for the classification itself.

Owner-facing by design: this builds the report; sending to third parties (AbuseIPDB etc.) is a
separate, opt-in step so a background-noise scan never auto-blasts an abuse desk and gets US
blocklisted.
"""
import ipaddress, json, os, time
from collections import defaultdict, Counter

# ---- MITRE ATT&CK mapping for the signatures our rules already detect --------------------------
# Reconnaissance / Initial-Access techniques that match commodity web scanning.
MITRE = {
    "path_probe":      ("T1595.003", "Active Scanning: Wordlist Scanning", "Reconnaissance (TA0043)"),
    "dir_bruteforce":  ("T1595.003", "Active Scanning: Wordlist Scanning", "Reconnaissance (TA0043)"),
    "ip_burst":        ("T1595.001", "Active Scanning: Scanning IP Blocks", "Reconnaissance (TA0043)"),
    "authz_probe":     ("T1190",     "Exploit Public-Facing Application (IDOR probing)", "Initial Access (TA0001)"),
    "login_failed":    ("T1110.001", "Brute Force: Password Guessing", "Credential Access (TA0006)"),
    "password_spray":  ("T1110.003", "Brute Force: Password Spraying", "Credential Access (TA0006)"),
    "otp_bruteforce":  ("T1110.002", "Brute Force: Password Cracking (OTP)", "Credential Access (TA0006)"),
    "download_burst":  ("T1530",     "Data from Cloud/Web Object", "Collection (TA0009)"),
    "ddos":            ("T1498",     "Network Denial of Service", "Impact (TA0040)"),
    "session_multi_ip":("T1563",     "Remote Service Session Hijacking", "Lateral Movement (TA0008)"),
}
# What a probed PATH tells us about intent.
PATH_INTENT = [
    ("/.env", "secrets theft (env file)"), ("/.git", "source-code disclosure"),
    ("wp-", "WordPress exploit hunt"), ("phpmyadmin", "database console hunt"),
    ("xmlrpc", "WordPress XML-RPC abuse"), (".php", "PHP webshell/RCE hunt"),
    ("/.aws", "cloud-credential theft"), ("/actuator", "Spring Boot info leak"),
    ("/vendor/", "PHPUnit RCE (CVE-2017-9841)"), ("/cgi-bin", "CGI RCE hunt"),
    ("adminer", "database console hunt"), ("/.ssh", "SSH key theft"),
]

# Known scanner / cloud ASNs -> where an abuse report actually goes.
ASN_ABUSE = {
    "azure": ("Microsoft Azure", "abuse@microsoft.com"),
    "amazon": ("Amazon AWS", "abuse@amazonaws.com"),
    "google": ("Google Cloud", "abuse@google.com"),
    "digitalocean": ("DigitalOcean", "abuse@digitalocean.com"),
    "ovh": ("OVH", "abuse@ovh.net"),
    "hetzner": ("Hetzner", "abuse@hetzner.com"),
    "censys": ("Censys (research scanner)", "abuse@censys.io"),
}


def _guess_provider(ip):
    """Cheap, offline hint from well-known cloud ranges — enough to name the abuse desk. No lookups."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    n = int(a)
    # Azure publishes 20.0.0.0/8-ish; AWS 3/13/15/18/52/54; GCP 34/35. Coarse but useful.
    first = ip.split(".")[0]
    if first in ("20", "40", "13", "51", "104") and ip.startswith(("20.", "40.", "13.")):
        return "azure"
    if first in ("3", "18", "52", "54", "15", "16", "35"):
        return "amazon" if first != "35" else "google"
    if first in ("34", "35"):
        return "google"
    if ip.startswith("216.144."):
        return "censys"
    return None


def path_intent(path):
    p = (path or "").lower()
    for frag, meaning in PATH_INTENT:
        if frag in p:
            return meaning
    return "unclassified probe"


def build(hours=24, events_log=None):
    """Return {attackers:[...], summary:{...}} from the last `hours` of events.log."""
    events_log = events_log or os.environ.get("EVENTS_LOG", "/var/log/colt/events.log")
    since = time.time() - hours * 3600
    per_ip = defaultdict(lambda: {"hits": 0, "paths": set(), "statuses": Counter(), "uas": set(),
                                   "country": "-", "rules": set(), "first": None, "last": None,
                                   "bot": False, "bot_name": "-"})
    alerts = []
    try:
        with open(events_log, "r", errors="replace") as fh:
            for line in fh:
                i = line.find("{")
                if i < 0:
                    continue
                try:
                    e = json.loads(line[i:])
                except Exception:
                    continue
                if float(e.get("ts") or 0) < since:
                    continue
                if e.get("evt") == "security_alert":
                    alerts.append(e)
                if e.get("evt") != "http":
                    continue
                st = int(e.get("status", 0))
                ip = e.get("ip", "-")
                # only interesting traffic: bots, probes, or error responses
                probe = any(x in (e.get("path") or "").lower()
                            for x in (".php", "wp-", ".env", ".git", "phpmyadmin", "xmlrpc",
                                      "/vendor/", "/.aws", "actuator", "cgi-bin", "adminer"))
                if not (e.get("bot") or st in (404, 403, 401) or probe):
                    continue
                d = per_ip[ip]
                d["hits"] += 1
                d["paths"].add(e.get("path", ""))
                d["statuses"][st] += 1
                if e.get("ua"): d["uas"].add(e.get("ua")[:120])
                if e.get("country", "-") != "-": d["country"] = e["country"]
                d["bot"] = d["bot"] or bool(e.get("bot"))
                if e.get("bot_name", "-") != "-": d["bot_name"] = e["bot_name"]
                t = float(e.get("ts") or 0)
                d["first"] = min(d["first"] or t, t); d["last"] = max(d["last"] or t, t)
    except FileNotFoundError:
        pass

    # attach the rules each IP triggered
    for a in alerts:
        subj = a.get("subject", "")
        if subj in per_ip:
            per_ip[subj]["rules"].add(a.get("rule"))

    attackers = []
    for ip, d in per_ip.items():
        if ip in ("-", "testclient"):
            continue
        techniques = sorted({MITRE[r] for r in d["rules"] if r in MITRE})
        if not techniques and (d["bot"] or d["statuses"].get(404)):
            techniques = [MITRE["path_probe"]]        # scanning without a fired rule is still recon
        prov = _guess_provider(ip)
        attackers.append({
            "ip": ip, "country": d["country"], "hits": d["hits"],
            "bot": d["bot"], "bot_name": d["bot_name"],
            "rules": sorted(d["rules"]),
            "mitre": [{"id": t[0], "name": t[1], "tactic": t[2]} for t in techniques],
            "intents": sorted({path_intent(p) for p in d["paths"] if p}),
            "sample_paths": sorted(d["paths"])[:8],
            "statuses": dict(d["statuses"]),
            "user_agents": sorted(d["uas"])[:3],
            "first_seen": d["first"], "last_seen": d["last"],
            "provider": ASN_ABUSE.get(prov, (None, None))[0],
            "abuse_contact": ASN_ABUSE.get(prov, (None, None))[1],
        })
    attackers.sort(key=lambda x: -x["hits"])
    return {"attackers": attackers, "alerts": alerts,
            "summary": {"distinct_ips": len(attackers),
                        "bot_ips": sum(1 for a in attackers if a["bot"]),
                        "alerts": len(alerts),
                        "techniques": sorted({m["id"] for a in attackers for m in a["mitre"]})}}


def render(hours=24, events_log=None):
    d = build(hours, events_log)
    from datetime import datetime, timezone
    def ts(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") if t else "-"
    L = []
    L.append("jobhuntwow.com — attacker / abuse digest  (last %dh)" % hours)
    L.append("=" * 72)
    s = d["summary"]
    L.append("  Distinct source IPs : %d  (%d classed as bots/scanners)" % (s["distinct_ips"], s["bot_ips"]))
    L.append("  Security alerts      : %d" % s["alerts"])
    L.append("  MITRE techniques seen: %s" % (", ".join(s["techniques"]) or "none"))
    L.append("")
    if not d["attackers"]:
        L.append("  No hostile traffic in this window.")
    for a in d["attackers"][:40]:
        L.append("-" * 72)
        L.append("  %s   %s   %d requests   %s"
                 % (a["ip"], a["country"], a["hits"], ("BOT/" + a["bot_name"]) if a["bot"] else "client"))
        if a["provider"]:
            L.append("     network   : %s   (abuse: %s)" % (a["provider"], a["abuse_contact"]))
        L.append("     window    : %s → %s UTC" % (ts(a["first_seen"]), ts(a["last_seen"])))
        L.append("     statuses  : %s" % a["statuses"])
        if a["rules"]:
            L.append("     rules     : %s" % ", ".join(a["rules"]))
        for m in a["mitre"]:
            L.append("     MITRE     : %s  %s  [%s]" % (m["id"], m["name"], m["tactic"]))
        if a["intents"]:
            L.append("     intent    : %s" % "; ".join(a["intents"]))
        if a["sample_paths"]:
            L.append("     paths     : %s" % ", ".join(a["sample_paths"]))
        if a["user_agents"]:
            L.append("     UA        : %s" % a["user_agents"][0])
    L.append("")
    L.append("-" * 72)
    L.append("Recon of internet background noise. Reputation reporting (AbuseIPDB) is opt-in; nothing")
    L.append("is auto-sent to third parties. Blocking belongs at the edge (Cloudflare/Caddy), not here.")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    print(render(24, sys.argv[1] if len(sys.argv) > 1 else None))
