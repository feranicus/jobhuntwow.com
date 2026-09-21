# The security and observability stack of jobhuntwow.com

> You cannot protect something you cannot see.
>
> Written after the LLM-jacking of 2026-08-30 to 2026-09-06, which was found by a bank statement
> six days late. Every control below names the finding it answers.

## The five sentences that govern everything

1. **The header is attacker-controlled. The path is the evidence.**
2. **A claim is evidence only when contradicting it costs the attacker something.**
3. **Models propose, code decides.** No language model causes a side effect here.
4. **Absence of evidence is never a finding.** A failed read reports "not determinable", never zero.
5. **Fail open on a fault, closed on a budget.** A broken meter must not take the product down; a
   spent budget must not open the wallet.

Standards this is measured against: NIST SP 800-53 (AC-3 access enforcement, AU-2 audit events,
SI-4 monitoring), CISA/NSA **Secure by Design** (deny by default, and prove it), OWASP API Top 10
(API1 object-level authorisation, API4 resource consumption, API5 function-level authorisation),
BSI IT-Grundschutz APP.3.1 / OPS.1.1.5 (logging that is actually written and actually read).

---

## The layers, and where each one lives

| # | Layer | Where | State |
|---|---|---|---|
| 0 | the event record, one JSON line per request | `backend/app/observability.py` | on |
| 1 | detection: path shape, 22 attack classes, variety over volume | `backend/app/perseus_client.py` | on |
| 2 | classification: three buckets, never two | `backend/app/visitors.py` | on |
| 3 | enforcement: tarpit, timed block, blast cap, kill switch | `backend/app/perseus_client.py` | on, reversible |
| 4 | alerting: twelve rules, cooldown + storm cap, Telegram + mail | `backend/app/alerts.py` | on |
| 5 | autonomy: the hub's published ruleset | the perseus hub (cybergod) | consumed, not run here |
| 6 | observability: events -> promtail -> Loki -> Grafana, heartbeats | shared `colt_events` volume | on |
| 7 | perimeter: ten headers, authorisation, probe-shaped 404, canonical host | `security_headers.py`, `auth.py`, `spa_guard.py`, `hosts.py` | on |
| 8 | **the wallet**: budget, rate limit, meter, two-source watch | `llm_meter.py`, `llm_events.py`, `spend_watch.py` | **new, on** |
| 9 | **the console**: what arrived, what we did, what it cost | `security.py` + `/security` | **new, admin-only** |

---

## 1. The wallet (incident findings 1-6, and the one item the report left open)

The report's *still open* list, item 3, verbatim: *"jobhuntwow still has no budget cap and no rate
limit. Authentication is now required, but self-signup is open. The same spend is available to
anyone willing to create an account, and nothing currently prevents it."* That is now closed.

`backend/app/llm_meter.py` is asked **before every request to a paid model**, at all four
chokepoints (`llm.chat`, `llm.complete`, `qwen.chat_stream`, `proxy.chat_completions`). Four rules:

| Rule | Env var | Default | Why it exists |
|---|---|---|---|
| global daily USD | `JHW_DAILY_USD` | 3.00 | the account-level stop |
| per-account daily USD | `JHW_USER_DAILY_USD` | 0.75 | self-signup means authenticated is not trusted |
| per-account calls/hour | `JHW_USER_CALLS_PER_HOUR` | 120 | 1,539 requests over seven days tripped nothing |
| service calls/hour | `JHW_CALLS_PER_HOUR` | 600 | signing up ten times must not multiply the budget |

* **Unknown tokens are charged, not ignored.** A streamed response carries no `usage` block, and the
  actor used the streaming endpoint. An unpriced call costs `JHW_UNKNOWN_CALL_USD` (0.01) against
  the caps and the row is marked `estimated`, so the ledger never calls that estimate a measurement.
* **One pricing home.** `llm_events.rate_for/cost_of` prices both the ledger and the cap; an unknown
  model is priced at the dearest rate known, never an average.
* **Fails open on a storage fault** (and says so, once), **closed on the budget**. `None` and `0.0`
  are kept apart everywhere.
* A refusal is **evidence**: `evt=llm_budget_refused` with user, model, caller, address and reason,
  plus a Telegram page (subject to the alert cooldown and storm cap, so under a sustained attack the
  row always lands and the page does not repeat). `/api/electronic/*` and `/v1/*` answer **429 with
  Retry-After**, never a 500. `/api/chat` is a stream whose response has already started, so it
  cannot carry a status: the refusal is yielded as the answer text, which is also what the person
  needs to read.
* **Honest limits of the money rules.** The USD caps are enforced against spend ALREADY RECORDED
  and no caller passes an estimate, so requests issued in the same instant all read the same total
  and all pass; what bounds that overshoot is the two call-rate rules. And the proxy has no
  accounts, so its "per-user" key is the source address — attribution, not authorisation, and
  trivially multiplied by rotating addresses. Only the two service-wide rules bind there.
* **Measured cost**, so nobody has to guess: `allow()` 7 ms and `record()` 17 ms against a
  20,000-row ledger, synchronous, on the event loop. At this service's call rate that is noise;
  the ledger is pruned past `JHW_METER_RETAIN_DAYS` (120) so it stays that way.

`backend/app/spend_watch.py` polls hourly and compares **two sources**: our ledger (who) against
DigitalOcean's month-to-date usage (whether). Median baseline, today excluded from its own baseline,
a ratio **and** an absolute floor, and it names models called today that were never called before.
Needs `DO_API_TOKEN` in the container; without it, that half reports unavailable rather than zero.

## 2. The console (finding 11, and the operator's own question)

`GET /api/security/overview` and the `/security` page, **administrator only, checked server-side on
every request** (an anonymous caller gets 401 and a signed-in non-administrator gets 403, both
proven over the real ASGI app in `tests/test_security_console.py`) (`auth.require_admin`; `ADMIN_EMAILS` is committed, `EXTRA_ADMIN_EMAILS` can only
add, the gate fails closed). It shows, per window:

per-hostname traffic (jobhuntwow.com **and** jobhw.org separately) · three buckets · attack classes
by shape · worst offenders ranked by **distinct** paths · blocks, would-blocks, tarpits, 429s ·
alerts fired, delivered and suppressed · **who opened the site, and which visits were held back and
why** · spend by account and model · and a live feed of the last 200 requests.

### The visit feed

A signed-out page view sends one Telegram message naming the host, page, address, country, client,
referrer and language. Gated on the PATH, not the user agent — a scanner announcing itself as Safari
while asking for `/wp-login.php` is refused by its path, which is the evidence; the user agent is
attacker-controlled. A record that contradicts itself is a client; a record carrying no evidence at
all is still a person (a corporate network blocks the very headers that would prove it, and making
that person invisible is the expensive error). One message per visitor per 6 h, own hourly cap so it
can never silence a security alert, sent on a background thread so a third party's latency never
reaches the page. `JHW_VISIT_NOTIFY=0` turns it off.

The window selector scales the read: a 1-hour view parses roughly a twenty-fourth of what a 7-day
view does, rather than the whole file every time. The page refreshes every 30 s while it is open.

**Nothing on that page renders "I could not look" as a zero.** An unreadable source paints an em
dash and a reason, the sidecar has the words *active / stale / not installed / unverifiable*, and
enforcement has *unknown / none / armed / empty / active*.

## 3. Seeing the short domain (the new visibility)

`jobhw.org` used to be redirected by a `redir` line in the shared Caddyfile: one line, works, and
means the request never reaches the only process that writes an event. Nobody who typed the short
domain appeared anywhere in our record.

Caddy now **proxies** `jobhw.org` and `www.jobhuntwow.com` to the application, and
`backend/app/hosts.py` issues the 301 itself, in one hop, after the request has been observed with
`host=<what they typed>`. The destination is a constant in that file and is never read from the
request, so it cannot become an open redirect; a hostname we do not own is served normally rather
than bounced on its own say-so; and the ACME challenge is never redirected, because a bounced
challenge is a certificate outage for every domain on the host.

## 4. Two defects that made existing controls blind

* **The route-table gate saw 10 routes of 31.** FastAPI 0.139 stopped flattening `include_router()`
  into `app.routes`, so walking it and filtering for `APIRoute` silently skipped every auth,
  electronic, tracker and `/v1` route — including the `/api/electronic/*` tree that served another
  user's CV during the incident — and reported a clean run. `authz_audit.iter_api_routes()` now
  descends, and both the gate and the audit assert they found at least 25 routes.
* **Two `evt=http` writers.** The sidecar and observability both appended an access line, and the
  only thing stopping the double count was an env var in one compose file. The single writer is now
  nominated **in code**, and only if the nominated writer is importable.

## 5. Running it

```
python ship.py            # tests -> authz audit -> commit -> staging gate -> deploy -> verify
python authz_audit.py     # every route, every method, asked anonymously, in-process
```

`ship.py` runs the audit as a **gate**, and it is fail-closed in both directions: a route that
serves content to an anonymous caller stops the ship, **and so does an audit that could not run**
(rc 2, a hang, or a crash) — *blind is not clean*. The audit refuses to report a verdict at all if
its route walk finds fewer than `MIN_ROUTES` (25); the regression suite imports that same constant
rather than restating it.

## 6. What this does NOT do (state the boundary, or it is discovered during an incident)

* HTTP layer only. It sees nothing in email, on endpoints, or in identity systems.
* It never touches a firewall, and it never scans or connects back. Retaliation is criminal
  (StGB §202a/§202b/§303a/§303b/§202c, EU 2013/40, US CFAA §1030, CC s.342.1); the lawful path is a
  complaint to the provider, drafted by us and filed by a person.
* It does not defeat automation that pays for a real browser engine. It defeats the large, cheap
  population built on plain HTTP clients.
* There is no tamper-evident logging: an attacker with host access can edit the trail.
* The WebRTC/HTTP3 browser probe is **evidence, never a gate**, and it is off at **both** ends:
  the server drops those fields unless `JHW_PROBE_WEBRTC=1`, and the browser does not even open the
  peer connection unless the bundle was built with `VITE_JHW_PROBE_WEBRTC=1`. It cannot see a
  scripted client at all (no JavaScript runs in one), it accuses corporate networks that block UDP,
  and the address it reveals is compared server-side and dropped.
* **Still open, and not ours to close from here:** the DigitalOcean model access key is shared,
  legacy and unscoped across five projects. Our allowlist and budget close one path each; a scoped
  per-project key closes every path including the ones nobody has imagined, and it is console work.

## 7. Privacy

An IP address is personal data (GDPR; CJEU C-582/14 *Breyer*). Everything here is derived from what
the client voluntarily sent, kept for security under legitimate interest, bounded by the window the
event file holds. `PERSEUS_HASH_IPS=1` stores a salted hash instead, and the correlation survives
while the identifier does not.
