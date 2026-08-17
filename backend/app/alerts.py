"""
alerts.py — secure-by-design detection for jobhuntwow.com. Sliding-window rules -> Telegram + email.

DESIGN NOTES
* In-process, in-memory windows. Deliberate: jhw-web is ONE container, alerts must fire in
  milliseconds, and a detection stack that needs its own database is a stack that rots. Loki keeps
  the forensic history; this only keeps the last hour to make a decision.
* Every rule is rate-limited by a COOLDOWN and a global storm cap. An alert system that pages you 400
  times during a DDoS is a second outage — you would mute it, and then miss the real one.
* Alerting NEVER blocks or breaks a request. Detection only. Blocking belongs in Caddy/the firewall,
  and we do not touch the firewall (Amnezia VPN shares this host).
* Thresholds are env-tunable. Defaults are for a low-traffic internal tool: a burst that is normal on
  a public site is genuinely suspicious here.
"""
import os, time
from collections import defaultdict, deque

try:
    from . import notify          # package import (uvicorn app.main:app)
except ImportError:
    import notify                # standalone (tests)

def _i(name, d):
    try: return int(os.environ.get(name, d))
    except ValueError: return d

# ---- thresholds (env-tunable) -------------------------------------------------------------------
FAIL_LOGIN_N      = _i("ALERT_FAIL_LOGIN_N", 3)     # >2 failures, as requested
FAIL_LOGIN_WIN    = _i("ALERT_FAIL_LOGIN_WIN", 600)
SPRAY_EMAILS_N    = _i("ALERT_SPRAY_EMAILS_N", 3)   # one IP trying several identities
SPRAY_WIN         = _i("ALERT_SPRAY_WIN", 900)
OTP_FAIL_N        = _i("ALERT_OTP_FAIL_N", 4)       # OTP brute force (password already correct!)
OTP_FAIL_WIN      = _i("ALERT_OTP_FAIL_WIN", 600)
ASSESS_N          = _i("ALERT_ASSESS_N", 6)         # >5 companies, as requested
ASSESS_WIN        = _i("ALERT_ASSESS_WIN", 900)
DDOS_REQ_N        = _i("ALERT_DDOS_REQ_N", 300)     # total req/min
DDOS_IP_N         = _i("ALERT_DDOS_IP_N", 40)       # distinct IPs/min
DDOS_WIN          = _i("ALERT_DDOS_WIN", 60)
BURST_IP_N        = _i("ALERT_BURST_IP_N", 120)     # single IP hammering
BURST_IP_WIN      = _i("ALERT_BURST_IP_WIN", 60)
PROBE_404_N       = _i("ALERT_PROBE_404_N", 12)     # scanner walking paths
PROBE_404_WIN     = _i("ALERT_PROBE_404_WIN", 300)
DENY_N            = _i("ALERT_DENY_N", 5)           # 401/403 storm = IDOR / token probing
DENY_WIN          = _i("ALERT_DENY_WIN", 300)
DL_N              = _i("ALERT_DOWNLOAD_N", 25)      # deck-download burst = exfil
DL_WIN            = _i("ALERT_DOWNLOAD_WIN", 600)
SESSION_IP_N      = _i("ALERT_SESSION_IP_N", 3)     # one account, many IPs = stolen session
SESSION_IP_WIN    = _i("ALERT_SESSION_IP_WIN", 1800)
COOLDOWN          = _i("ALERT_COOLDOWN", 900)       # per rule+subject
STORM_CAP         = _i("ALERT_STORM_CAP", 12)       # max alerts/hour, total
ENABLED           = os.environ.get("ALERTS_ENABLED", "1") != "0"

# paths a scanner asks for and a real user never does
_PROBE_PATHS = ("/.env", "/.git", "/wp-", "/wordpress", "/phpmyadmin", "/admin.php", "/.aws",
                "/config.json", "/actuator", "/vendor/", "/xmlrpc.php", "/shell", "/cgi-bin",
                "/.ssh", "/backup", "/.docker", "/api/v1/pods", "/solr/", "/struts")

_w = defaultdict(deque)          # key -> deque[(ts, value)]
_sent = {}                       # dedup key -> ts
_storm = deque()


def _push(key, value=None, win=3600):
    now = time.time()
    d = _w[key]; d.append((now, value))
    cut = now - win
    while d and d[0][0] < cut: d.popleft()
    return d


def _count(key, win):
    now = time.time(); cut = now - win
    return sum(1 for ts, _ in _w[key] if ts >= cut)


def _distinct(key, win):
    now = time.time(); cut = now - win
    return {v for ts, v in _w[key] if ts >= cut and v is not None}


def fire(rule, subject, title, lines, severity="HIGH"):
    """Send once per COOLDOWN per (rule, subject), and never more than STORM_CAP per hour."""
    if not ENABLED:
        return False
    now = time.time()
    key = "%s|%s" % (rule, subject)
    if now - _sent.get(key, 0) < COOLDOWN:
        return False
    while _storm and _storm[0] < now - 3600: _storm.popleft()
    if len(_storm) >= STORM_CAP:
        notify._log(evt="alert_suppressed", rule=rule, subject=str(subject)[:80],
                    reason="storm cap %d/h reached" % STORM_CAP)
        return False
    _sent[key] = now; _storm.append(now)

    body = "\n".join(str(x) for x in lines)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now))
    full = "%s\n\nWhen : %s\nRule : %s\nWhere: jobhuntwow.com (jhw-web)\n\n%s\n\n" \
           "Grafana: godeyes.ai/observe -> 'JobHuntWOW - Electronic (app events)' -> Security row" % (
               title, stamp, rule, body)
    notify._log(evt="security_alert", rule=rule, severity=severity, subject=str(subject)[:120],
                title=title[:160], detail=body[:600])
    return notify.both("%s — %s" % (severity, title), full)


# ---------------------------------------------------------------- HTTP-level rules
def observe_http(ev):
    ip, path, status = ev.get("ip", "-"), ev.get("path", ""), int(ev.get("status", 0))
    now = time.time()
    _push("req:all", ip, DDOS_WIN)
    _push("req:%s" % ip, None, BURST_IP_WIN)

    # 1) DDoS / volumetric: high request rate AND many sources (one loud IP is rule 2, not a DDoS)
    if _count("req:all", DDOS_WIN) >= DDOS_REQ_N:
        ips = _distinct("req:all", DDOS_WIN)
        if len(ips) >= DDOS_IP_N:
            fire("ddos", "global", "Possible DDoS / traffic flood on jobhuntwow.com",
                 ["Requests in %ds: %d  (threshold %d)" % (DDOS_WIN, _count("req:all", DDOS_WIN), DDOS_REQ_N),
                  "Distinct source IPs: %d  (threshold %d)" % (len(ips), DDOS_IP_N),
                  "Sample IPs: %s" % ", ".join(list(ips)[:12]),
                  "",
                  "Caddy is still terminating TLS; this is a DETECTION only — nothing was blocked.",
                  "If it is real: rate-limit at Caddy or put the host behind a scrubbing service."],
                 severity="CRITICAL")

    # 2) single-source flood (scraper / brute-force runner)
    if _count("req:%s" % ip, BURST_IP_WIN) >= BURST_IP_N:
        fire("ip_burst", ip, "Single IP flooding jobhuntwow.com",
             ["IP: %s" % ip,
              "Requests in %ds: %d (threshold %d)" % (BURST_IP_WIN, _count("req:%s" % ip, BURST_IP_WIN), BURST_IP_N),
              "Client: %s / %s / %s" % (ev.get("browser"), ev.get("os"), ev.get("device")),
              "Bot: %s (%s)" % (ev.get("bot"), ev.get("bot_name")),
              "UA: %s" % ev.get("ua", "")[:160]])

    # 3) scanner probing for well-known loot
    low = path.lower()
    if status in (404, 403) and any(p in low for p in _PROBE_PATHS):
        _push("probe:%s" % ip, path, PROBE_404_WIN)
        n = _count("probe:%s" % ip, PROBE_404_WIN)
        if n >= 3:
            fire("path_probe", ip, "Vulnerability scanner probing jobhuntwow.com",
                 ["IP: %s" % ip, "Suspicious paths in %ds: %d" % (PROBE_404_WIN, n),
                  "Paths: %s" % ", ".join(list(_distinct("probe:%s" % ip, PROBE_404_WIN))[:10]),
                  "UA: %s (%s)" % (ev.get("ua", "")[:120], ev.get("bot_name")),
                  "", "Nothing is exposed at those paths — this is recon, not a breach."])

    # 4) 404 walking (directory brute force)
    if status == 404:
        _push("404:%s" % ip, path, PROBE_404_WIN)
        if _count("404:%s" % ip, PROBE_404_WIN) >= PROBE_404_N:
            fire("dir_bruteforce", ip, "Directory brute-force against jobhuntwow.com",
                 ["IP: %s" % ip, "404s in %ds: %d" % (PROBE_404_WIN, _count("404:%s" % ip, PROBE_404_WIN)),
                  "UA: %s" % ev.get("ua", "")[:160]])

    # 5) 401/403 storm — someone poking at other people's jobs/decks (IDOR) or replaying a token
    if status in (401, 403) and path.startswith("/api/"):
        _push("deny:%s" % ip, path, DENY_WIN)
        if _count("deny:%s" % ip, DENY_WIN) >= DENY_N:
            fire("authz_probe", ip, "Repeated authorization failures (possible IDOR / token replay)",
                 ["IP: %s" % ip, "401/403 in %ds: %d" % (DENY_WIN, _count("deny:%s" % ip, DENY_WIN)),
                  "Paths: %s" % ", ".join(list(_distinct("deny:%s" % ip, DENY_WIN))[:8]),
                  "User (if any): %s" % (ev.get("user") or "-"),
                  "", "Job dirs are owner-scoped; a 403 means the gate held."])

    # 6) deck-download burst = data exfiltration of customer-facing material
    if "/deck/" in path and status == 200:
        who = ev.get("user") or ip
        _push("dl:%s" % who, path, DL_WIN)
        if _count("dl:%s" % who, DL_WIN) >= DL_N:
            fire("download_burst", who, "Unusual document-download volume (possible exfiltration)",
                 ["User/IP: %s" % who,
                  "Downloads in %ds: %d (threshold %d)" % (DL_WIN, _count("dl:%s" % who, DL_WIN), DL_N),
                  "These are CANDIDATE resumes/cover letters - personal data under GDPR."])

    # 7) one account seen from several IPs in a short window = shared or stolen session
    user = ev.get("user")
    if user and status < 400:
        _push("uip:%s" % user, ip, SESSION_IP_WIN)
        ips = _distinct("uip:%s" % user, SESSION_IP_WIN)
        if len(ips) >= SESSION_IP_N:
            fire("session_multi_ip", user, "One account active from several IPs (session sharing/theft?)",
                 ["User: %s" % user, "Distinct IPs in %dmin: %d" % (SESSION_IP_WIN // 60, len(ips)),
                  "IPs: %s" % ", ".join(list(ips)[:8]),
                  "", "Mobile roaming can cause this. Confirm with the user before revoking."])


# ---------------------------------------------------------------- auth + business rules
def observe_login_failure(email, ip, reason, ua=""):
    """>2 failures in the window -> immediate alert with the forensics, exactly as asked."""
    _push("fail:%s" % (email or ip), ip, FAIL_LOGIN_WIN)
    _push("sprayip:%s" % ip, email, SPRAY_WIN)
    n = _count("fail:%s" % (email or ip), FAIL_LOGIN_WIN)
    if n >= FAIL_LOGIN_N:
        fire("login_failed", email or ip, "Repeated failed logins on jobhuntwow.com",
             ["Email tried : %s" % (email or "-"),
              "Source IP   : %s" % ip,
              "Failures    : %d in %d min (threshold %d)" % (n, FAIL_LOGIN_WIN // 60, FAIL_LOGIN_N),
              "Reason      : %s" % reason,
              "User-Agent  : %s" % (ua or "-")[:160],
              "",
              "Gate: the account's own password + a 6-digit code emailed to that address.",
              "A failure here means the password or the identity was wrong — no session was issued."],
             severity="CRITICAL")
    # password spraying: one IP, several identities
    emails = {e for e in _distinct("sprayip:%s" % ip, SPRAY_WIN) if e}
    if len(emails) >= SPRAY_EMAILS_N:
        fire("password_spray", ip, "Password spraying: one IP trying multiple identities",
             ["Source IP: %s" % ip,
              "Distinct emails in %dmin: %d" % (SPRAY_WIN // 60, len(emails)),
              "Emails: %s" % ", ".join(list(emails)[:10]),
              "UA: %s" % (ua or "-")[:140]], severity="CRITICAL")


def observe_otp_failure(email, ip):
    """Worse than a failed password: the password was already accepted and they are guessing the code."""
    _push("otp:%s" % (email or ip), ip, OTP_FAIL_WIN)
    n = _count("otp:%s" % (email or ip), OTP_FAIL_WIN)
    if n >= OTP_FAIL_N:
        fire("otp_bruteforce", email or ip, "OTP brute-force - this account's password is already known",
             ["Email: %s" % (email or "-"), "Source IP: %s" % ip,
              "Bad codes: %d in %d min" % (n, OTP_FAIL_WIN // 60),
              "",
              "ESCALATE: reaching the OTP step means this account's password was accepted.",
              "If this was not the account owner: force a password reset now."], severity="CRITICAL")


def observe_login_success(email, ip, ua=""):
    known = _distinct("okip:%s" % email, 30 * 86400)
    _push("okip:%s" % email, ip, 30 * 86400)
    if known and ip not in known:
        fire("new_ip_login", "%s|%s" % (email, ip), "Sign-in from a new IP",
             ["User: %s" % email, "New IP: %s" % ip,
              "Previously seen from: %s" % ", ".join(list(known)[:6]),
              "UA: %s" % (ua or "-")[:140],
              "", "Informational — travel and mobile networks do this too."], severity="INFO")


def observe_assess(email, company, ip=""):
    """>5 companies in a short window: licence abuse, or a scripted client draining Shodan credits."""
    _push("assess:%s" % email, (company or "").lower(), ASSESS_WIN)
    companies = _distinct("assess:%s" % email, ASSESS_WIN)
    if len(companies) >= ASSESS_N:
        fire("assess_burst", email, "Unusual assessment volume for one user",
             ["User: %s" % email, "Source IP: %s" % (ip or "-"),
              "Distinct companies in %d min: %d (threshold %d)" % (ASSESS_WIN // 60, len(companies), ASSESS_N),
              "Companies: %s" % ", ".join(list(companies)[:12]),
              "",
              "Each run spends Shodan query credits and LLM tokens.",
              "Legitimate bulk research looks identical — confirm with the user."])
