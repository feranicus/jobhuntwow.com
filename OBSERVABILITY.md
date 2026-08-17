# Observability stack — reusable

A single-file, framework-light observability stack for any Python (FastAPI/Starlette) app:
**structured JSON events → an append-only log → promtail → Loki → Grafana**, plus an in-process
**alert engine** (sliding-window rules → Telegram + email), a **country lookup**, and a **MITRE
ATT&CK attacker digest**.

Everything app-side lives in **one file: `observability.py`**. The pipeline (promtail/Loki/Grafana)
is three small config files given in full below. Extracted from the cybergod.ai platform and
de-hardcoded so it drops into another project.

---

## 1. How it fits together

```
  your app  ──emit(evt=...)──►  stdout  ──►  container logs
      │                          │
      │                          └──►  events.log (append-only file)
      │                                     │
      │                                     ▼
      │                                promtail   (tails the file, parses JSON, adds labels)
      │                                     │  push
      │                                     ▼
      │                                   Loki     (stores + indexes, enforces retention)
      │                                     │  query (LogQL)
      │                                     ▼
      │                                 Grafana    (dashboards, tables, alerts UI)
      │
      └──alert rule trips──►  Telegram + email   (immediate, out-of-band)
```

Two independent paths on purpose:

* **The log path** is your *history and dashboards*. It is asynchronous and cheap — every request,
  login, job, error is an event.
* **The alert path** is your *page-me-now*. It is in-process and fires in milliseconds, out-of-band
  from Loki, so an alert still reaches you even if Loki is down.

### Design rules baked in

* **Write events to a FILE, not just stdout.** Whoever owns stdout changes (a pipe, a supervisor, a
  background task); a file promtail tails is stable. If an event *must* reach the dashboard, write it
  to the file.
* **Alerts are in-memory sliding windows — no database.** Detection must fire in ms, and a detector
  that needs its own DB rots. Loki keeps the forensic history; the alert engine only keeps the last
  hour to make a decision.
* **Rate-limit every alert** (per-rule cooldown + a global hourly storm cap). An alert flood during
  an incident is a second outage — you'd mute it and miss the real one.
* **Detection only — never block or break a request.** Blocking belongs at the edge (Cloudflare /
  reverse proxy / firewall), not in the app. Every function is wrapped so it can't take down the
  request it observes.

---

## 2. Wire it into a FastAPI app (3 lines)

```python
import observability as obs

obs.configure(
    app_name="myapp",
    service="myapp-web",                       # becomes the `service` label in Loki
    events_log="/var/log/myapp/events.log",    # the file promtail tails
    grafana_hint="https://grafana.mydomain/d/myapp",   # printed in alert bodies (optional)
)

# one middleware => evt="http" per request + feeds the HTTP alert rules
obs.install_middleware(app, session_user_fn=lambda req: current_user_email(req))
```

Then sprinkle events and auth hooks where they belong:

```python
# auth
obs.alerts.observe_login_failure(email, obs.client_ip(request), reason="bad password", ua=ua)
obs.alerts.observe_login_success(email, obs.client_ip(request), ua=ua)
obs.alerts.observe_otp_failure(email, obs.client_ip(request))

# business events (anything you'll want on a dashboard later)
obs.emit(evt="job_done", user=email, kind="report", ms=elapsed_ms, cost_usd=cost)

# a generic "too much of X for one subject" rule (licence abuse, API hammering, …)
obs.alerts.observe_burst(subject=email, item=company, label="assessments", ip=ip)
```

That's the whole integration. Everything else (dashboards, alerting) reads the events.

> **Not FastAPI?** Skip `install_middleware` and call `obs.emit(evt="http", ...)` yourself from your
> framework's request hook, passing the same fields (see the schema below). The alert engine,
> notify, geoip and digest are framework-agnostic.

---

## 3. The event schema

Every line is one JSON object. `ts` (epoch seconds) and `service` are added automatically. Common
event types:

| `evt` | Emitted by | Key fields |
|---|---|---|
| `http` | the middleware, per request | `ip country method path status ms ua browser os device bot bot_name ref lang user` |
| `security_alert` | the alert engine when a rule trips | `rule severity subject title detail` |
| `alert_delivery` | notify, after sending | `channel telegram email result` |
| `alert_suppressed` | storm-cap reached | `rule subject reason` |
| `geoip` | geoip on error | `result err` |
| *(yours)* | your `emit()` calls | anything — `evt="job_done"`, `evt="login"`, `evt="error"`, … |

Rules of thumb for your own events: always include an `evt`, attribute a `user` where you can, put
scalars (numbers/strings) at the top level so Loki can `unwrap` them, and emit an `evt="error"` on
any failure so a crash is as visible as a success.

---

## 4. The alert rules

All thresholds are env-tunable. Defaults suit a **low-traffic internal tool** — a burst that is
normal on a public site is genuinely suspicious here. Raise them for public traffic.

| Rule | Fires when | Severity | Threshold env (count / window secs) |
|---|---|---|---|
| `ddos` | high total req-rate **and** many distinct IPs | CRITICAL | `ALERT_DDOS_REQ_N`=300 / `ALERT_DDOS_IP_N`=40 / `ALERT_DDOS_WIN`=60 |
| `ip_burst` | one IP hammering | HIGH | `ALERT_BURST_IP_N`=120 / `ALERT_BURST_IP_WIN`=60 |
| `path_probe` | scanner hitting `/.env`, `/.git`, `/wp-`, … | HIGH | ≥3 / `ALERT_PROBE_404_WIN`=300 |
| `dir_bruteforce` | many 404s (directory walking) | HIGH | `ALERT_PROBE_404_N`=12 / 300 |
| `authz_probe` | 401/403 storm on `/api/` (IDOR / token replay) | HIGH | `ALERT_DENY_N`=5 / `ALERT_DENY_WIN`=300 |
| `download_burst` | sensitive-download burst (exfil) | HIGH | `ALERT_DOWNLOAD_N`=25 / `ALERT_DOWNLOAD_WIN`=600 |
| `session_multi_ip` | one account from many IPs quickly | HIGH | `ALERT_SESSION_IP_N`=3 / `ALERT_SESSION_IP_WIN`=1800 |
| `login_failed` | repeated failed logins | CRITICAL | `ALERT_FAIL_LOGIN_N`=3 / `ALERT_FAIL_LOGIN_WIN`=600 |
| `password_spray` | one IP, several identities | CRITICAL | `ALERT_SPRAY_EMAILS_N`=3 / `ALERT_SPRAY_WIN`=900 |
| `otp_bruteforce` | bad OTPs after the password was accepted | CRITICAL | `ALERT_OTP_FAIL_N`=4 / `ALERT_OTP_FAIL_WIN`=600 |
| `new_ip_login` | sign-in from an unseen IP | INFO | (30-day memory) |
| `usage_burst` | generic: too many distinct items for one subject | HIGH | `ALERT_BURST_N`=6 / `ALERT_BURST_WIN`=900 |

Global controls:

* `ALERT_COOLDOWN` (900s) — minimum gap between identical `(rule, subject)` alerts.
* `ALERT_STORM_CAP` (12/hour) — hard cap on total alerts per hour; extras become `alert_suppressed`.
* `ALERTS_ENABLED=0` — disable all alerting (events still flow).
* `ALERT_DOWNLOAD_MARKER` (`/deck/`) — URL fragment that marks a sensitive download for the exfil rule.

The HTTP rules (`ddos`, `ip_burst`, `path_probe`, `dir_bruteforce`, `authz_probe`, `download_burst`,
`session_multi_ip`) fire automatically from the middleware. The auth/business rules are the
`alerts.observe_*` calls you place yourself.

---

## 5. Notifications (Telegram + email)

Both are optional and independent — email failing must not silence Telegram. If neither is
configured, alerts still land in the log (`evt=security_alert`); only the out-of-band page is skipped.

**Telegram** — set `BOT_TOKEN` (a bot from @BotFather) and `ALERT_TG_CHAT` (comma-separated numeric
chat IDs). To get a chat ID: message the bot, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

**Email via the Gmail API** (works where outbound SMTP is blocked, which is common on cloud hosts):

1. Create a Google Cloud service account; enable the Gmail API; grant it **domain-wide delegation**
   for the scope `https://www.googleapis.com/auth/gmail.send`.
2. `GMAIL_SENDER` = the mailbox it sends as; `GMAIL_SA_B64` = `base64` of the service-account JSON.
3. `ALERT_EMAIL` = comma-separated recipients.

> Prefer the Gmail API over SMTP on servers: many providers block ports 25/465/587 outbound, and the
> API is one HTTPS call.

---

## 6. Country lookup (geoip)

`obs.country(ip)` returns an ISO-3166 alpha-2 code (or `-`). It uses **DB-IP IP-to-Country Lite**
(`.mmdb`, MaxMind-DB format) — free, **no API key**, fetched lazily into `GEOIP_DIR` (default
`/data`) on first use and refreshed monthly.

* `pip install maxminddb` to enable it; without the package it returns `-` harmlessly.
* Licence is **CC-BY-4.0** → you must credit *"IP geolocation by DB-IP"* somewhere user-visible.
* **Country only, deliberately** — city-level geolocation is a bigger privacy intrusion than security
  monitoring needs (GDPR data-minimisation, Art. 5(1)(c)).
* `GEOIP_ENABLED=0` disables it. Cloudflare's `CF-IPCountry` header, if present, is used first (free).

---

## 7. Privacy (the claims are load-bearing)

* **An IP address is personal data** under GDPR/DSGVO. You log it for security monitoring
  (legitimate-interest basis), which requires a **retention limit** — enforced here by **Loki's
  `retention_period`** (§8), not by the app. If Loki keeps logs longer than your privacy notice
  says, the notice is wrong. Verify before you publish it.
* Set `TELEMETRY_HASH_IPS=1` (+ a `TELEMETRY_IP_SALT`) to store a **salted hash** instead of the raw
  IP — you keep "same visitor?" correlation and drop the identifier. Raw is the default for forensics.
* Geo is **country-level only** (§6). No ad cookies, no cross-site tracking, no profiling.
* The attacker digest is **owner-facing**; third-party reporting (AbuseIPDB etc.) is a separate,
  opt-in step so background-noise scans never auto-blast an abuse desk.

---

## 8. The pipeline: promtail + Loki + Grafana

Three config files. Point promtail at your `events.log`, ship to Loki, read from Grafana.

### 8a. `promtail-config.yml`

```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0
  log_level: warn
positions:
  filename: /tmp/promtail/positions.yaml
clients:
  - url: ${LOKI_URL}          # e.g. http://loki:3100/loki/api/v1/push  (start promtail with -config.expand-env=true)
scrape_configs:
  - job_name: app
    static_configs:
      - targets: [localhost]
        labels:
          service: myapp-web   # matches the `service` field you set via configure()
          job: myapp
          __path__: /logs/events.log     # mount your events.log here
    pipeline_stages:
      - json:
          expressions:
            evt: evt
            service: service
            status: status
            rule: rule
            severity: severity
            user: user
      - labels:
          evt:
          service:
          severity:
```

> Keep the label set **small** (evt/service/severity). Labels are indexed; high-cardinality fields
> like `ip`, `path` or `user` must stay as log *content* (filter them with `| json | user="x"`),
> never as labels, or Loki's index explodes.

### 8b. `loki-config.yml` (single-binary, filesystem — fine for one host)

```yaml
auth_enabled: false
server: { http_listen_port: 3100, grpc_listen_port: 9096, log_level: warn }
common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage: { filesystem: { chunks_directory: /loki/chunks, rules_directory: /loki/rules } }
  replication_factor: 1
  ring: { kvstore: { store: inmemory } }
schema_config:
  configs:
    - from: 2020-10-24
      store: boltdb-shipper
      object_store: filesystem
      schema: v11
      index: { prefix: index_, period: 24h }
storage_config:
  boltdb_shipper: { active_index_directory: /loki/boltdb-shipper-active, cache_location: /loki/boltdb-shipper-cache, cache_ttl: 24h }
  filesystem: { directory: /loki/chunks }
limits_config:
  retention_period: 168h          # 7 days — THIS is your log-retention promise (see §7)
  reject_old_samples: true
  reject_old_samples_max_age: 168h
compactor:
  working_directory: /loki/compactor
  retention_enabled: true         # required for retention_period to actually delete
  retention_delete_delay: 2h
  delete_request_store: filesystem
```

### 8c. Grafana Loki datasource (provisioning)

```yaml
apiVersion: 1
datasources:
  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://loki:3100
    isDefault: true
    jsonData: { maxLines: 1000, timeout: 60 }
```

### 8d. Minimal `docker-compose` for the pipeline

```yaml
services:
  loki:
    image: grafana/loki:3.0.0
    command: -config.file=/etc/loki/loki-config.yml
    volumes: ["./loki-config.yml:/etc/loki/loki-config.yml:ro", "loki-data:/loki"]
  promtail:
    image: grafana/promtail:3.0.0
    command: -config.file=/etc/promtail/promtail-config.yml -config.expand-env=true
    environment: { LOKI_URL: "http://loki:3100/loki/api/v1/push" }
    volumes:
      - "./promtail-config.yml:/etc/promtail/promtail-config.yml:ro"
      - "/var/log/myapp:/logs:ro"          # <-- your events.log directory
  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
    volumes: ["./grafana/provisioning:/etc/grafana/provisioning:ro"]
volumes: { loki-data: {} }
```

> **Ship into an EXISTING Loki instead?** Drop the `loki`/`grafana` services, set
> `LOKI_URL` to your existing push endpoint, and give promtail a **distinct `service`/`job` label**
> so your app's logs don't mix with another stack's.

---

## 9. Grafana queries (LogQL) to get started

```logql
# Visitor / request log (table)
{service="myapp-web"} | json | evt="http"

# Security alerts, most recent first
{service="myapp-web", evt="security_alert"} | json

# Requests per minute
sum(count_over_time({service="myapp-web"} | json | evt="http" [1m]))

# Top source IPs (last 24h)
topk(10, sum by (ip) (count_over_time({service="myapp-web"} | json | evt="http" [24h])))

# 4xx/5xx rate
sum(count_over_time({service="myapp-web"} | json | evt="http" | status>=400 [5m]))

# Humans vs bots
sum by (bot) (count_over_time({service="myapp-web"} | json | evt="http" [1h]))

# A numeric field you emit yourself (e.g. cost), summed over a range
sum(sum_over_time({service="myapp-web"} | json | evt="job_done" | unwrap cost_usd [$__range]))
```

Build a dashboard with rows for **Visitors** (visits / unique / humans-vs-bots / a visitor-log table
/ top IPs) and **Security** (criticals, suppressed, an alert-log table with full forensics, an
auth-audit table). Watch **`alert_delivery` with `result="error"`** — a non-zero rate means alerts
are not reaching you (flying blind).

---

## 10. Attacker digest (MITRE ATT&CK)

`observability.py` also turns the raw `evt=http` / `security_alert` lines into an attacker-centric,
MITRE-mapped report — a deterministic lookup table (not an LLM: for commodity web recon the technique
is unambiguous, and a static map is faster, free and reliable).

```bash
# print a digest of the last 24h from an events.log
python observability.py digest /var/log/myapp/events.log

# or from your app / a scheduled job:
import observability as obs
text = obs.render_digest(hours=24, events_log="/var/log/myapp/events.log")
obs.notify_email("Daily attacker digest", text)      # e.g. send it every morning
```

Each hostile IP gets its country, request count, the rules it tripped, the mapped MITRE technique(s)
and tactic, the inferred intent per probed path, and — for well-known clouds — the abuse-desk contact.

---

## 11. CLI & self-test

```bash
python observability.py selftest              # emit sample events, trip two rules, print the digest (no network)
python observability.py digest [events.log]   # print the attacker digest
```

`selftest` needs no config and no network — it writes to a temp log, trips `path_probe` and
`login_failed`, shows that notify no-ops cleanly without credentials, and prints the rendered digest.

---

## 12. Environment variable reference

| Var | Default | Purpose |
|---|---|---|
| `OBS_APP_NAME` | `app` | Name in alert subjects/bodies. |
| `SERVICE` | `web` | The `service` label/field on every event. |
| `EVENTS_LOG` | *(unset → stdout only)* | Append-only file promtail tails. |
| `OBS_GRAFANA_HINT` | — | Link printed at the bottom of alert bodies. |
| `TELEMETRY_HASH_IPS` / `TELEMETRY_IP_SALT` | `0` / `change-me` | Salted-hash IPs instead of raw. |
| `GEOIP_DIR` / `GEOIP_ENABLED` | `/data` / `1` | DB-IP database location / toggle. |
| `BOT_TOKEN` / `ALERT_TG_CHAT` | — | Telegram bot token / target chat IDs. |
| `GMAIL_SENDER` / `GMAIL_SA_B64` / `ALERT_EMAIL` | — | Gmail-API sender / SA JSON (base64) / recipients. |
| `OBS_TG_CHAT_FILES` | — | Optional JSON files mapping uid→… to alert every operator. |
| `ALERTS_ENABLED` | `1` | Master switch for alerting. |
| `ALERT_*` (see §4) | per rule | Per-rule thresholds, cooldown, storm cap, download marker. |

---

## 13. Files in this stack

| File | Role |
|---|---|
| `observability.py` | **The whole app-side stack** — events, geoip, request telemetry + middleware, alert engine, Telegram/Gmail notify, MITRE digest, CLI. Stdlib-only core; optional deps for geo/email. |
| `promtail-config.yml` | Tails `events.log`, parses JSON, ships to Loki (§8a). |
| `loki-config.yml` | Single-binary Loki with retention = your log-retention promise (§8b). |
| Grafana datasource yml | Points Grafana at Loki (§8c). |

Copy `observability.py` into your project, add the three pipeline configs (or point promtail at an
existing Loki), wire the 3 lines from §2, and you have the full stack.
