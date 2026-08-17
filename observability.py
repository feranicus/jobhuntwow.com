"""
observability.py — a single-file, framework-light observability stack.

One JSON event per interesting thing that happens, written to stdout AND an append-only events.log,
which promtail tails into Loki and Grafana renders. Plus an in-process alert engine (sliding-window
rules -> Telegram + email), a country lookup, and a MITRE ATT&CK attacker digest. Extracted from the
cybergod.ai platform and de-hardcoded so it drops into any project.

WHY THIS SHAPE
  * Structured events to a FILE (not just stdout). Whoever owns stdout changes (a pipe, a supervisor,
    a background task), but a file promtail tails is stable. If an event MUST reach the dashboard,
    write it to the file — never rely on who owns stdout.
  * Alerts are in-process, in-memory sliding windows. No database: detection must fire in
    milliseconds, and a detection stack that needs its own DB rots. Loki keeps the forensic history;
    this only keeps the last hour to make a decision.
  * Every alert is rate-limited (per-rule cooldown + a global hourly storm cap). An alert system that
    pages you 400 times during a flood is a second outage — you'd mute it and miss the real one.
  * Nothing here ever blocks or breaks the request it observes. Detection only. Blocking belongs at
    the edge (Cloudflare / reverse proxy / firewall), not in the app.

DEPENDENCIES (all optional; the core needs only the stdlib)
  * core events + alerts + digest : stdlib only
  * FastAPI/Starlette middleware   : starlette (already present in a FastAPI app)
  * country lookup                 : pip install maxminddb   (DB fetched lazily, no API key)
  * email delivery (Gmail API)     : pip install requests google-auth   (SMTP is often blocked)
  * Telegram delivery              : none (uses urllib)

QUICK START (FastAPI)
    import observability as obs
    obs.configure(app_name="myapp", service="myapp-web", events_log="/var/log/myapp/events.log")
    obs.install_middleware(app, session_user_fn=lambda req: current_user(req))
    # in your auth code:
    obs.alerts.observe_login_failure(email, obs.client_ip(request), reason="bad password")
    obs.alerts.observe_login_success(email, obs.client_ip(request))

CLI
    python observability.py selftest         # emit sample events + trip a rule (no network)
    python observability.py digest [log]     # print the MITRE attacker digest from an events.log
"""
from __future__ import annotations
import base64, datetime, gzip, hashlib, ipaddress, json, os, re, threading, time, urllib.parse, urllib.request
from collections import defaultdict, deque, Counter

# =================================================================================================
# CONFIG — env-driven, overridable at runtime via configure().
# =================================================================================================
class _Cfg:
    def __init__(self):
        self.app_name   = os.environ.get("OBS_APP_NAME", "app")
        self.service    = os.environ.get("SERVICE", "web")
        self.events_log = os.environ.get("EVENTS_LOG", "")        # "" -> stdout only
        self.grafana_hint = os.environ.get("OBS_GRAFANA_HINT", "")  # printed in alert bodies
        # privacy
        self.hash_ips   = os.environ.get("TELEMETRY_HASH_IPS", "0") == "1"
        self.ip_salt    = os.environ.get("TELEMETRY_IP_SALT", "change-me")
        # geoip
        self.geoip_dir     = os.environ.get("GEOIP_DIR", "/data")
        self.geoip_enabled = os.environ.get("GEOIP_ENABLED", "1") != "0"
        # notify
        self.tg_token     = os.environ.get("BOT_TOKEN", "")
        self.alert_chat   = os.environ.get("ALERT_TG_CHAT", "")     # comma list of numeric chat ids
        self.alert_mails  = [e.strip() for e in os.environ.get("ALERT_EMAIL", "").split(",") if e.strip()]
        self.gmail_sender = os.environ.get("GMAIL_SENDER", "")
        self.gmail_sa_b64 = os.environ.get("GMAIL_SA_B64", "")      # base64 of the service-account JSON
        self.tg_chat_files = [p for p in os.environ.get("OBS_TG_CHAT_FILES", "").split(",") if p]

CFG = _Cfg()


def configure(**kw):
    """Override any config field at runtime, e.g. configure(app_name='myapp', events_log='/var/log/x')."""
    for k, v in kw.items():
        if hasattr(CFG, k):
            setattr(CFG, k, v)
    return CFG


# =================================================================================================
# 1. STRUCTURED EVENTS  — the heart of the stack. Write to stdout AND events.log.
# =================================================================================================
def emit(**fields):
    """Emit ONE structured JSON event. Never raises. Adds ts/service if absent.

    Everything downstream (promtail -> Loki -> Grafana, the alert rules, the digest) consumes these
    lines, so every observable thing in your app should be an emit() call: evt='http', evt='login',
    evt='job_done', evt='security_alert', evt='error', ..."""
    fields.setdefault("ts", time.time())
    fields.setdefault("service", CFG.service)
    line = json.dumps(fields, default=str)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if CFG.events_log:
        try:
            with open(CFG.events_log, "a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


log = emit   # alias — some call sites read better as log(evt=...)


# =================================================================================================
# 2. GEOIP  — country only, DB-IP Lite (.mmdb, MaxMind-DB format), no API key, lazy download.
#    Country-level only is a deliberate privacy choice (GDPR data minimisation).
# =================================================================================================
_geo_lock = threading.Lock()
_geo_reader = None
_geo_tried = None      # YYYY-MM last attempted, so a failure retries next month, not every request


def _geo_path(ym):
    return os.path.join(CFG.geoip_dir, "dbip-country-lite-%s.mmdb" % ym)


def _geo_download(ym):
    url = "https://download.db-ip.com/free/dbip-country-lite-%s.mmdb.gz" % ym
    dst = _geo_path(ym); tmp = dst + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": "observability/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        fh.write(gzip.decompress(r.read()))
    os.replace(tmp, dst)
    for f in os.listdir(CFG.geoip_dir):                # keep only the current month
        if f.startswith("dbip-country-lite-") and f != os.path.basename(dst):
            try: os.unlink(os.path.join(CFG.geoip_dir, f))
            except OSError: pass
    return dst


def _geo_reader_get():
    global _geo_reader, _geo_tried
    if not CFG.geoip_enabled:
        return None
    ym = datetime.datetime.utcnow().strftime("%Y-%m")
    if _geo_reader is not None and _geo_tried == ym:
        return _geo_reader
    with _geo_lock:
        if _geo_reader is not None and _geo_tried == ym:
            return _geo_reader
        _geo_tried = ym
        try:
            import maxminddb
        except ImportError:
            return None
        p = _geo_path(ym)
        try:
            if not os.path.exists(p):
                os.makedirs(CFG.geoip_dir, exist_ok=True)
                try:
                    _geo_download(ym)
                except Exception:
                    prev = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
                    p = _geo_path(prev)
                    if not os.path.exists(p):
                        _geo_download(prev)
            if _geo_reader is not None:
                try: _geo_reader.close()
                except Exception: pass
            _geo_reader = maxminddb.open_database(p)
        except Exception as e:
            emit(evt="geoip", result="error", err=repr(e)[:140])
            _geo_reader = None
    return _geo_reader


def country(ip):
    """ISO-3166 alpha-2 country for an IP, or '-' for unknown/private/unavailable. Never raises."""
    if not ip or ip == "-" or ip.startswith(("10.", "192.168.", "127.", "172.16.", "h:")):
        return "-"
    try:
        r = _geo_reader_get()
        if not r:
            return "-"
        rec = r.get(ip) or {}
        return (rec.get("country", {}) or {}).get("iso_code") or "-"
    except Exception:
        return "-"


# =================================================================================================
# 3. REQUEST TELEMETRY  — client IP, UA classification, and a FastAPI/Starlette middleware.
# =================================================================================================
SKIP_PATH_RE = re.compile(r"\.(css|js|map|png|jpe?g|svg|ico|woff2?|ttf)$", re.I)

_BOTS = [
    ("googlebot", "Googlebot"), ("bingbot", "Bingbot"), ("yandex", "YandexBot"),
    ("duckduckbot", "DuckDuckBot"), ("baiduspider", "Baiduspider"), ("slurp", "Yahoo Slurp"),
    ("ahrefs", "AhrefsBot"), ("semrush", "SemrushBot"), ("mj12bot", "MJ12bot"), ("dotbot", "DotBot"),
    ("petalbot", "PetalBot"), ("bytespider", "Bytespider"), ("gptbot", "GPTBot"),
    ("claudebot", "ClaudeBot"), ("ccbot", "CCBot"), ("perplexity", "PerplexityBot"),
    ("facebookexternalhit", "Facebook"), ("twitterbot", "Twitterbot"), ("linkedinbot", "LinkedInBot"),
    ("telegrambot", "TelegramBot"), ("whatsapp", "WhatsApp"), ("discordbot", "Discordbot"),
    # scanners / tooling — the interesting ones
    ("censys", "Censys"), ("shodan", "Shodan"), ("zgrab", "zgrab"), ("masscan", "masscan"),
    ("nmap", "nmap"), ("nuclei", "nuclei"), ("sqlmap", "sqlmap"), ("nikto", "Nikto"),
    ("dirbuster", "DirBuster"), ("gobuster", "gobuster"), ("wpscan", "WPScan"),
    ("curl", "curl"), ("wget", "wget"), ("python-requests", "python-requests"),
    ("go-http-client", "Go-http-client"), ("java/", "Java"), ("libwww-perl", "libwww-perl"),
    ("headlesschrome", "HeadlessChrome"), ("phantomjs", "PhantomJS"), ("scrapy", "Scrapy"),
]
_OS = [("windows nt 11", "Windows 11"), ("windows nt 10", "Windows 10"), ("windows", "Windows"),
       ("iphone", "iOS"), ("ipad", "iPadOS"), ("android", "Android"),
       ("mac os x", "macOS"), ("cros", "ChromeOS"), ("linux", "Linux")]
_BROWSER = [("edg/", "Edge"), ("opr/", "Opera"), ("chrome/", "Chrome"), ("firefox/", "Firefox"),
            ("safari/", "Safari")]


def client_ip(request):
    """Real client IP. CF-Connecting-IP (Cloudflare) -> first X-Forwarded-For -> X-Real-IP -> peer.
    Trust XFF only because exactly one proxy you control sits in front; the FIRST entry is the client."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "-")


def _maybe_hash_ip(ip):
    if not CFG.hash_ips or not ip or ip == "-":
        return ip
    return "h:" + hashlib.sha256((CFG.ip_salt + ip).encode()).hexdigest()[:16]


def classify_ua(ua):
    """{bot, bot_name, browser, os, device} from a User-Agent string. A 'browser' with no browser
    token and no OS is almost certainly tooling, so it is flagged bot=True (unknown-client)."""
    u = (ua or "").lower()
    if not u.strip():
        return {"bot": True, "bot_name": "no-user-agent", "browser": "-", "os": "-", "device": "unknown"}
    for pat, name in _BOTS:
        if pat in u:
            return {"bot": True, "bot_name": name, "browser": "-", "os": "-", "device": "bot"}
    os_ = next((n for p, n in _OS if p in u), "-")
    br = next((n for p, n in _BROWSER if p in u), "-")
    device = "mobile" if ("mobile" in u or "iphone" in u or "android" in u) else \
             ("tablet" if "ipad" in u else "desktop")
    bot = (br == "-" and os_ == "-")
    return {"bot": bot, "bot_name": "unknown-client" if bot else "-",
            "browser": br, "os": os_, "device": device}


def install_middleware(app, session_user_fn=None):
    """Add one Starlette/FastAPI middleware that emits evt='http' per request and feeds the alert
    rules. Never breaks the request it observes. `session_user_fn(request) -> str` is optional and
    lets a signed-in user be attributed on the event."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class _Telemetry(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            t0 = time.time()
            try:
                response = await call_next(request)
                status = response.status_code
            except Exception:
                _safe_emit(request, 500, t0)
                raise
            _safe_emit(request, status, t0)
            return response

    def _safe_emit(request, status, t0):
        try:
            path = request.url.path
            if SKIP_PATH_RE.search(path):
                return
            ua = request.headers.get("user-agent", "")
            c = classify_ua(ua)
            ip = client_ip(request)
            user = ""
            try:
                if session_user_fn:
                    user = session_user_fn(request) or ""
            except Exception:
                user = ""
            ev = dict(evt="http", ip=_maybe_hash_ip(ip), method=request.method, path=path[:200],
                      status=status, ms=int((time.time() - t0) * 1000), ua=ua[:220],
                      browser=c["browser"], os=c["os"], device=c["device"],
                      bot=c["bot"], bot_name=c["bot_name"],
                      ref=(request.headers.get("referer") or "")[:160],
                      lang=(request.headers.get("accept-language") or "")[:40].split(",")[0],
                      country=(request.headers.get("cf-ipcountry")
                               or request.headers.get("x-country") or country(ip)),
                      user=user)
            emit(**ev)
            try:
                alerts.observe_http(ev)
            except Exception:
                pass
        except Exception:
            pass

    app.add_middleware(_Telemetry)
    return app


# =================================================================================================
# 4. NOTIFY  — the one place that reaches a human: Telegram + email (Gmail API). Never raises.
# =================================================================================================
def _tg_chats():
    if CFG.alert_chat:
        return [c.strip() for c in CFG.alert_chat.split(",") if c.strip()]
    out = []
    for p in CFG.tg_chat_files:            # optional: files mapping uid->... (alert every operator)
        try:
            for uid in (json.load(open(p)) or {}):
                if str(uid).lstrip("-").isdigit() and str(uid) not in out:
                    out.append(str(uid))
        except Exception:
            pass
    return out


def notify_telegram(text):
    if not CFG.tg_token:
        return False
    ok = False
    for chat in _tg_chats():
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                           "parse_mode": "Markdown",
                                           "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % CFG.tg_token, data=data)
            with urllib.request.urlopen(req, timeout=12) as r:
                ok = (r.status == 200) or ok
        except Exception as e:
            emit(evt="alert_delivery", channel="telegram", chat=str(chat), result="error", err=repr(e)[:160])
    return ok


def notify_email(subject, body, to=None):
    """Send via the Gmail API over HTTPS (works where outbound SMTP is blocked). Needs a Google
    service account with domain-wide delegation: GMAIL_SENDER + GMAIL_SA_B64 (base64 of the SA JSON)."""
    if to is None:
        to = CFG.alert_mails
    if isinstance(to, str):
        to = [x.strip() for x in to.split(",") if x.strip()]
    to = ", ".join(to)
    if not (CFG.gmail_sender and CFG.gmail_sa_b64 and to):
        emit(evt="alert_delivery", channel="email", result="skipped", err="GMAIL_SENDER/GMAIL_SA_B64/recipients not set")
        return False
    try:
        from email.message import EmailMessage
        import requests
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GRequest
        msg = EmailMessage()
        msg["Subject"] = subject[:200]; msg["From"] = CFG.gmail_sender; msg["To"] = to
        msg.set_content(body)
        info = json.loads(base64.b64decode(CFG.gmail_sa_b64))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/gmail.send"], subject=CFG.gmail_sender)
        creds.refresh(GRequest())
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = requests.post("https://gmail.googleapis.com/gmail/v1/users/%s/messages/send" % CFG.gmail_sender,
                          headers={"Authorization": "Bearer " + creds.token, "Content-Type": "application/json"},
                          json={"raw": raw}, timeout=20)
        if r.status_code in (200, 202):
            return True
        emit(evt="alert_delivery", channel="email", result="error", status=r.status_code, err=r.text[:200])
    except Exception as e:
        emit(evt="alert_delivery", channel="email", result="error", err=repr(e)[:200])
    return False


def notify_both(subject, body):
    """Fire both channels independently — email failing must not silence Telegram."""
    t = notify_telegram("\U0001F6A8 *%s*\n\n%s" % (subject, body))
    e = notify_email("[%s] %s" % (CFG.app_name, subject), body)
    emit(evt="alert_delivery", channel="both", telegram=bool(t), email=bool(e), subject=subject[:120])
    return t or e


# =================================================================================================
# 5. ALERT ENGINE  — sliding-window rules, per-rule cooldown + global storm cap. Detection only.
# =================================================================================================
def _i(name, d):
    try: return int(os.environ.get(name, d))
    except ValueError: return d


class Alerts:
    """In-memory sliding-window detector. Thresholds are env-tunable (defaults suit a low-traffic
    internal tool — a burst that is normal on a public site is genuinely suspicious here)."""

    # thresholds (env-tunable)
    FAIL_LOGIN_N   = _i("ALERT_FAIL_LOGIN_N", 3);   FAIL_LOGIN_WIN = _i("ALERT_FAIL_LOGIN_WIN", 600)
    SPRAY_EMAILS_N = _i("ALERT_SPRAY_EMAILS_N", 3);  SPRAY_WIN      = _i("ALERT_SPRAY_WIN", 900)
    OTP_FAIL_N     = _i("ALERT_OTP_FAIL_N", 4);      OTP_FAIL_WIN   = _i("ALERT_OTP_FAIL_WIN", 600)
    BURST_N        = _i("ALERT_BURST_N", 6);         BURST_WIN      = _i("ALERT_BURST_WIN", 900)  # generic business burst
    DDOS_REQ_N     = _i("ALERT_DDOS_REQ_N", 300);    DDOS_IP_N      = _i("ALERT_DDOS_IP_N", 40)
    DDOS_WIN       = _i("ALERT_DDOS_WIN", 60)
    BURST_IP_N     = _i("ALERT_BURST_IP_N", 120);    BURST_IP_WIN   = _i("ALERT_BURST_IP_WIN", 60)
    PROBE_404_N    = _i("ALERT_PROBE_404_N", 12);    PROBE_404_WIN  = _i("ALERT_PROBE_404_WIN", 300)
    DENY_N         = _i("ALERT_DENY_N", 5);          DENY_WIN       = _i("ALERT_DENY_WIN", 300)
    DL_N           = _i("ALERT_DOWNLOAD_N", 25);     DL_WIN         = _i("ALERT_DOWNLOAD_WIN", 600)
    SESSION_IP_N   = _i("ALERT_SESSION_IP_N", 3);    SESSION_IP_WIN = _i("ALERT_SESSION_IP_WIN", 1800)
    COOLDOWN       = _i("ALERT_COOLDOWN", 900);      STORM_CAP      = _i("ALERT_STORM_CAP", 12)
    ENABLED        = os.environ.get("ALERTS_ENABLED", "1") != "0"

    # paths a scanner asks for and a real user never does
    PROBE_PATHS = ("/.env", "/.git", "/wp-", "/wordpress", "/phpmyadmin", "/admin.php", "/.aws",
                   "/config.json", "/actuator", "/vendor/", "/xmlrpc.php", "/shell", "/cgi-bin",
                   "/.ssh", "/backup", "/.docker", "/api/v1/pods", "/solr/", "/struts")
    # sensitive-download URL fragment (attribute exfil bursts). Override for your app.
    DOWNLOAD_MARKER = os.environ.get("ALERT_DOWNLOAD_MARKER", "/deck/")

    def __init__(self):
        self._w = defaultdict(deque)   # key -> deque[(ts, value)]
        self._sent = {}                # (rule|subject) -> ts
        self._storm = deque()

    # ---- window primitives ----
    def _push(self, key, value=None, win=3600):
        now = time.time(); d = self._w[key]; d.append((now, value))
        cut = now - win
        while d and d[0][0] < cut: d.popleft()
        return d

    def _count(self, key, win):
        cut = time.time() - win
        return sum(1 for ts, _ in self._w[key] if ts >= cut)

    def _distinct(self, key, win):
        cut = time.time() - win
        return {v for ts, v in self._w[key] if ts >= cut and v is not None}

    # ---- the one place an alert leaves the process ----
    def fire(self, rule, subject, title, lines, severity="HIGH"):
        if not self.ENABLED:
            return False
        now = time.time(); key = "%s|%s" % (rule, subject)
        if now - self._sent.get(key, 0) < self.COOLDOWN:
            return False
        while self._storm and self._storm[0] < now - 3600: self._storm.popleft()
        if len(self._storm) >= self.STORM_CAP:
            emit(evt="alert_suppressed", rule=rule, subject=str(subject)[:80],
                 reason="storm cap %d/h reached" % self.STORM_CAP)
            return False
        self._sent[key] = now; self._storm.append(now)
        body = "\n".join(str(x) for x in lines)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now))
        tail = ("\n\nGrafana: %s" % CFG.grafana_hint) if CFG.grafana_hint else ""
        full = "%s\n\nWhen : %s\nRule : %s\nWhere: %s (%s)\n\n%s%s" % (
            title, stamp, rule, CFG.app_name, CFG.service, body, tail)
        emit(evt="security_alert", rule=rule, severity=severity, subject=str(subject)[:120],
             title=title[:160], detail=body[:600])
        return notify_both("%s — %s" % (severity, title), full)

    # ---- HTTP-level rules (fed by the middleware) ----
    def observe_http(self, ev):
        ip, path, status = ev.get("ip", "-"), ev.get("path", ""), int(ev.get("status", 0))
        self._push("req:all", ip, self.DDOS_WIN)
        self._push("req:%s" % ip, None, self.BURST_IP_WIN)

        # 1) DDoS / volumetric: high rate AND many sources (one loud IP is rule 2)
        if self._count("req:all", self.DDOS_WIN) >= self.DDOS_REQ_N:
            ips = self._distinct("req:all", self.DDOS_WIN)
            if len(ips) >= self.DDOS_IP_N:
                self.fire("ddos", "global", "Possible DDoS / traffic flood",
                          ["Requests in %ds: %d (threshold %d)" % (self.DDOS_WIN, self._count("req:all", self.DDOS_WIN), self.DDOS_REQ_N),
                           "Distinct source IPs: %d (threshold %d)" % (len(ips), self.DDOS_IP_N),
                           "Sample IPs: %s" % ", ".join(list(ips)[:12]),
                           "", "Detection only — nothing was blocked. Rate-limit at the edge if real."],
                          severity="CRITICAL")

        # 2) single-source flood (scraper / brute-force runner)
        if self._count("req:%s" % ip, self.BURST_IP_WIN) >= self.BURST_IP_N:
            self.fire("ip_burst", ip, "Single IP flooding the app",
                      ["IP: %s" % ip,
                       "Requests in %ds: %d (threshold %d)" % (self.BURST_IP_WIN, self._count("req:%s" % ip, self.BURST_IP_WIN), self.BURST_IP_N),
                       "Client: %s / %s / %s" % (ev.get("browser"), ev.get("os"), ev.get("device")),
                       "Bot: %s (%s)" % (ev.get("bot"), ev.get("bot_name")),
                       "UA: %s" % ev.get("ua", "")[:160]])

        # 3) scanner probing for well-known loot
        low = path.lower()
        if status in (404, 403) and any(p in low for p in self.PROBE_PATHS):
            self._push("probe:%s" % ip, path, self.PROBE_404_WIN)
            n = self._count("probe:%s" % ip, self.PROBE_404_WIN)
            if n >= 3:
                self.fire("path_probe", ip, "Vulnerability scanner probing the app",
                          ["IP: %s" % ip, "Suspicious paths in %ds: %d" % (self.PROBE_404_WIN, n),
                           "Paths: %s" % ", ".join(list(self._distinct("probe:%s" % ip, self.PROBE_404_WIN))[:10]),
                           "UA: %s (%s)" % (ev.get("ua", "")[:120], ev.get("bot_name")),
                           "", "Nothing is exposed at those paths — recon, not a breach."])

        # 4) 404 walking (directory brute force)
        if status == 404:
            self._push("404:%s" % ip, path, self.PROBE_404_WIN)
            if self._count("404:%s" % ip, self.PROBE_404_WIN) >= self.PROBE_404_N:
                self.fire("dir_bruteforce", ip, "Directory brute-force against the app",
                          ["IP: %s" % ip, "404s in %ds: %d" % (self.PROBE_404_WIN, self._count("404:%s" % ip, self.PROBE_404_WIN)),
                           "UA: %s" % ev.get("ua", "")[:160]])

        # 5) 401/403 storm on the API — IDOR / token replay
        if status in (401, 403) and path.startswith("/api/"):
            self._push("deny:%s" % ip, path, self.DENY_WIN)
            if self._count("deny:%s" % ip, self.DENY_WIN) >= self.DENY_N:
                self.fire("authz_probe", ip, "Repeated authorization failures (possible IDOR / token replay)",
                          ["IP: %s" % ip, "401/403 in %ds: %d" % (self.DENY_WIN, self._count("deny:%s" % ip, self.DENY_WIN)),
                           "Paths: %s" % ", ".join(list(self._distinct("deny:%s" % ip, self.DENY_WIN))[:8]),
                           "User (if any): %s" % (ev.get("user") or "-")])

        # 6) sensitive-download burst = exfiltration
        if self.DOWNLOAD_MARKER and self.DOWNLOAD_MARKER in path and status == 200:
            who = ev.get("user") or ip
            self._push("dl:%s" % who, path, self.DL_WIN)
            if self._count("dl:%s" % who, self.DL_WIN) >= self.DL_N:
                self.fire("download_burst", who, "Unusual download volume (possible exfiltration)",
                          ["User/IP: %s" % who,
                           "Downloads in %ds: %d (threshold %d)" % (self.DL_WIN, self._count("dl:%s" % who, self.DL_WIN), self.DL_N)])

        # 7) one account seen from several IPs quickly = shared/stolen session
        user = ev.get("user")
        if user and status < 400:
            self._push("uip:%s" % user, ip, self.SESSION_IP_WIN)
            ips = self._distinct("uip:%s" % user, self.SESSION_IP_WIN)
            if len(ips) >= self.SESSION_IP_N:
                self.fire("session_multi_ip", user, "One account active from several IPs (session sharing/theft?)",
                          ["User: %s" % user, "Distinct IPs in %dmin: %d" % (self.SESSION_IP_WIN // 60, len(ips)),
                           "IPs: %s" % ", ".join(list(ips)[:8]),
                           "", "Mobile roaming can cause this. Confirm before revoking."])

    # ---- auth + business rules (call these from your own code) ----
    def observe_login_failure(self, email, ip, reason="", ua=""):
        self._push("fail:%s" % (email or ip), ip, self.FAIL_LOGIN_WIN)
        self._push("sprayip:%s" % ip, email, self.SPRAY_WIN)
        n = self._count("fail:%s" % (email or ip), self.FAIL_LOGIN_WIN)
        if n >= self.FAIL_LOGIN_N:
            self.fire("login_failed", email or ip, "Repeated failed logins",
                      ["Email tried : %s" % (email or "-"), "Source IP   : %s" % ip,
                       "Failures    : %d in %d min (threshold %d)" % (n, self.FAIL_LOGIN_WIN // 60, self.FAIL_LOGIN_N),
                       "Reason      : %s" % reason, "User-Agent  : %s" % (ua or "-")[:160]],
                      severity="CRITICAL")
        emails = {e for e in self._distinct("sprayip:%s" % ip, self.SPRAY_WIN) if e}
        if len(emails) >= self.SPRAY_EMAILS_N:
            self.fire("password_spray", ip, "Password spraying: one IP trying multiple identities",
                      ["Source IP: %s" % ip, "Distinct emails in %dmin: %d" % (self.SPRAY_WIN // 60, len(emails)),
                       "Emails: %s" % ", ".join(list(emails)[:10]), "UA: %s" % (ua or "-")[:140]],
                      severity="CRITICAL")

    def observe_otp_failure(self, email, ip):
        """Worse than a bad password: the password was already accepted and they are guessing the code."""
        self._push("otp:%s" % (email or ip), ip, self.OTP_FAIL_WIN)
        n = self._count("otp:%s" % (email or ip), self.OTP_FAIL_WIN)
        if n >= self.OTP_FAIL_N:
            self.fire("otp_bruteforce", email or ip, "OTP brute-force — the password is already known",
                      ["Email: %s" % (email or "-"), "Source IP: %s" % ip,
                       "Bad codes: %d in %d min" % (n, self.OTP_FAIL_WIN // 60),
                       "", "ESCALATE: reaching the OTP step means the shared password was accepted."],
                      severity="CRITICAL")

    def observe_login_success(self, email, ip, ua=""):
        known = self._distinct("okip:%s" % email, 30 * 86400)
        self._push("okip:%s" % email, ip, 30 * 86400)
        if known and ip not in known:
            self.fire("new_ip_login", "%s|%s" % (email, ip), "Sign-in from a new IP",
                      ["User: %s" % email, "New IP: %s" % ip,
                       "Previously seen from: %s" % ", ".join(list(known)[:6]),
                       "UA: %s" % (ua or "-")[:140], "", "Informational — travel/mobile does this too."],
                      severity="INFO")

    def observe_burst(self, subject, item, label="actions", ip=""):
        """Generic business-rate rule: too many distinct `item`s for one `subject` (e.g. a user
        running many jobs, hitting an API too often). Emits an 'over-use' alert."""
        self._push("burst:%s" % subject, (str(item) or "").lower(), self.BURST_WIN)
        items = self._distinct("burst:%s" % subject, self.BURST_WIN)
        if len(items) >= self.BURST_N:
            self.fire("usage_burst", subject, "Unusual %s volume for one subject" % label,
                      ["Subject: %s" % subject, "Source IP: %s" % (ip or "-"),
                       "Distinct %s in %d min: %d (threshold %d)" % (label, self.BURST_WIN // 60, len(items), self.BURST_N),
                       "Items: %s" % ", ".join(list(items)[:12])])


alerts = Alerts()   # module-level singleton — import and call alerts.observe_*(...)


# =================================================================================================
# 6. ATTACKER DIGEST  — turn evt=http / security_alert lines into a MITRE ATT&CK-mapped report.
#    Deterministic lookup (NOT an LLM): for commodity web recon the technique is unambiguous.
# =================================================================================================
MITRE = {
    "path_probe":      ("T1595.003", "Active Scanning: Wordlist Scanning", "Reconnaissance (TA0043)"),
    "dir_bruteforce":  ("T1595.003", "Active Scanning: Wordlist Scanning", "Reconnaissance (TA0043)"),
    "ip_burst":        ("T1595.001", "Active Scanning: Scanning IP Blocks", "Reconnaissance (TA0043)"),
    "authz_probe":     ("T1190",     "Exploit Public-Facing Application (IDOR)", "Initial Access (TA0001)"),
    "login_failed":    ("T1110.001", "Brute Force: Password Guessing", "Credential Access (TA0006)"),
    "password_spray":  ("T1110.003", "Brute Force: Password Spraying", "Credential Access (TA0006)"),
    "otp_bruteforce":  ("T1110.002", "Brute Force: Password Cracking (OTP)", "Credential Access (TA0006)"),
    "download_burst":  ("T1530",     "Data from Cloud/Web Object", "Collection (TA0009)"),
    "usage_burst":     ("T1530",     "Data from Cloud/Web Object", "Collection (TA0009)"),
    "ddos":            ("T1498",     "Network Denial of Service", "Impact (TA0040)"),
    "session_multi_ip":("T1563",     "Remote Service Session Hijacking", "Lateral Movement (TA0008)"),
}
PATH_INTENT = [
    ("/.env", "secrets theft (env file)"), ("/.git", "source-code disclosure"),
    ("wp-", "WordPress exploit hunt"), ("phpmyadmin", "database console hunt"),
    ("xmlrpc", "WordPress XML-RPC abuse"), (".php", "PHP webshell/RCE hunt"),
    ("/.aws", "cloud-credential theft"), ("/actuator", "Spring Boot info leak"),
    ("/vendor/", "PHPUnit RCE (CVE-2017-9841)"), ("/cgi-bin", "CGI RCE hunt"),
    ("adminer", "database console hunt"), ("/.ssh", "SSH key theft"),
]
ASN_ABUSE = {
    "azure": ("Microsoft Azure", "abuse@microsoft.com"), "amazon": ("Amazon AWS", "abuse@amazonaws.com"),
    "google": ("Google Cloud", "abuse@google.com"), "digitalocean": ("DigitalOcean", "abuse@digitalocean.com"),
    "ovh": ("OVH", "abuse@ovh.net"), "hetzner": ("Hetzner", "abuse@hetzner.com"),
    "censys": ("Censys (research scanner)", "abuse@censys.io"),
}


def _guess_provider(ip):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    first = ip.split(".")[0]
    if ip.startswith(("20.", "40.", "13.")):
        return "azure"
    if first in ("3", "18", "52", "54", "15", "16"):
        return "amazon"
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


def attacker_digest(hours=24, events_log=None):
    """Return {attackers, alerts, summary} from the last `hours` of events.log."""
    events_log = events_log or CFG.events_log or "/var/log/app/events.log"
    since = time.time() - hours * 3600
    per_ip = defaultdict(lambda: {"hits": 0, "paths": set(), "statuses": Counter(), "uas": set(),
                                   "country": "-", "rules": set(), "first": None, "last": None,
                                   "bot": False, "bot_name": "-"})
    alerts_seen = []
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
                    alerts_seen.append(e)
                if e.get("evt") != "http":
                    continue
                st = int(e.get("status", 0)); ip = e.get("ip", "-")
                probe = any(x in (e.get("path") or "").lower()
                            for x in (".php", "wp-", ".env", ".git", "phpmyadmin", "xmlrpc",
                                      "/vendor/", "/.aws", "actuator", "cgi-bin", "adminer"))
                if not (e.get("bot") or st in (404, 403, 401) or probe):
                    continue
                d = per_ip[ip]; d["hits"] += 1; d["paths"].add(e.get("path", "")); d["statuses"][st] += 1
                if e.get("ua"): d["uas"].add(e.get("ua")[:120])
                if e.get("country", "-") != "-": d["country"] = e["country"]
                d["bot"] = d["bot"] or bool(e.get("bot"))
                if e.get("bot_name", "-") != "-": d["bot_name"] = e["bot_name"]
                t = float(e.get("ts") or 0)
                d["first"] = min(d["first"] or t, t); d["last"] = max(d["last"] or t, t)
    except FileNotFoundError:
        pass
    for a in alerts_seen:
        subj = a.get("subject", "")
        if subj in per_ip:
            per_ip[subj]["rules"].add(a.get("rule"))
    attackers = []
    for ip, d in per_ip.items():
        if ip in ("-", "testclient"):
            continue
        techniques = sorted({MITRE[r] for r in d["rules"] if r in MITRE})
        if not techniques and (d["bot"] or d["statuses"].get(404)):
            techniques = [MITRE["path_probe"]]
        prov = _guess_provider(ip)
        attackers.append({"ip": ip, "country": d["country"], "hits": d["hits"], "bot": d["bot"],
                          "bot_name": d["bot_name"], "rules": sorted(d["rules"]),
                          "mitre": [{"id": t[0], "name": t[1], "tactic": t[2]} for t in techniques],
                          "intents": sorted({path_intent(p) for p in d["paths"] if p}),
                          "sample_paths": sorted(d["paths"])[:8], "statuses": dict(d["statuses"]),
                          "user_agents": sorted(d["uas"])[:3], "first_seen": d["first"], "last_seen": d["last"],
                          "provider": ASN_ABUSE.get(prov, (None, None))[0],
                          "abuse_contact": ASN_ABUSE.get(prov, (None, None))[1]})
    attackers.sort(key=lambda x: -x["hits"])
    return {"attackers": attackers, "alerts": alerts_seen,
            "summary": {"distinct_ips": len(attackers), "bot_ips": sum(1 for a in attackers if a["bot"]),
                        "alerts": len(alerts_seen),
                        "techniques": sorted({m["id"] for a in attackers for m in a["mitre"]})}}


def render_digest(hours=24, events_log=None):
    d = attacker_digest(hours, events_log)
    def ts(t): return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M") if t else "-"
    L = ["%s — attacker / abuse digest  (last %dh)" % (CFG.app_name, hours), "=" * 72]
    s = d["summary"]
    L += ["  Distinct source IPs : %d  (%d bots/scanners)" % (s["distinct_ips"], s["bot_ips"]),
          "  Security alerts      : %d" % s["alerts"],
          "  MITRE techniques seen: %s" % (", ".join(s["techniques"]) or "none"), ""]
    if not d["attackers"]:
        L.append("  No hostile traffic in this window.")
    for a in d["attackers"][:40]:
        L.append("-" * 72)
        L.append("  %s   %s   %d requests   %s" % (a["ip"], a["country"], a["hits"],
                 ("BOT/" + a["bot_name"]) if a["bot"] else "client"))
        if a["provider"]: L.append("     network   : %s   (abuse: %s)" % (a["provider"], a["abuse_contact"]))
        L.append("     window    : %s -> %s UTC" % (ts(a["first_seen"]), ts(a["last_seen"])))
        L.append("     statuses  : %s" % a["statuses"])
        if a["rules"]: L.append("     rules     : %s" % ", ".join(a["rules"]))
        for m in a["mitre"]:
            L.append("     MITRE     : %s  %s  [%s]" % (m["id"], m["name"], m["tactic"]))
        if a["intents"]: L.append("     intent    : %s" % "; ".join(a["intents"]))
        if a["sample_paths"]: L.append("     paths     : %s" % ", ".join(a["sample_paths"]))
        if a["user_agents"]: L.append("     UA        : %s" % a["user_agents"][0])
    L += ["", "-" * 72,
          "Recon of internet background noise. Reputation reporting (AbuseIPDB) is opt-in; nothing is",
          "auto-sent to third parties. Blocking belongs at the edge, not here."]
    return "\n".join(L)


# =================================================================================================
# CLI
# =================================================================================================
def _selftest():
    import tempfile
    log_path = os.path.join(tempfile.gettempdir(), "obs_selftest.log")
    open(log_path, "w").close()
    configure(app_name="selftest", service="selftest-web", events_log=log_path,
              grafana_hint="http://grafana.local")
    # emit some traffic events
    for i in range(3):
        emit(evt="http", ip="203.0.113.7", method="GET", path="/.env", status=404, ms=3,
             bot=True, bot_name="curl", ua="curl/8.0", country="US")
        alerts.observe_http({"ip": "203.0.113.7", "path": "/.env", "status": 404, "bot": True,
                             "bot_name": "curl", "ua": "curl/8.0"})
    # trip the login-failure rule (no network: notify_* will just no-op without creds)
    for i in range(3):
        alerts.observe_login_failure("attacker@example.com", "203.0.113.7", reason="bad password")
    print("--- events.log ---")
    print(open(log_path).read().strip())
    print("\n--- digest ---")
    print(render_digest(24, log_path))
    print("\nselftest OK ->", log_path)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "digest":
        print(render_digest(int(os.environ.get("OBS_HOURS", "24")),
                            sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd == "selftest":
        _selftest()
    else:
        print(__doc__)
