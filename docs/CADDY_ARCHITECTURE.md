# Shared Caddy architecture on the droplet — and how to break/fix it safely

Written to hand to another team sharing this droplet, whose Caddy config broke. It explains how the
ONE shared Caddy fronts several independent projects, the exact contract each project must follow to
add or change a vhost, every failure mode we have actually hit (with the symptom, the real cause, and
the fix), and a test/remediation checklist. Everything here is from the running setup and the repair
scripts, not theory.

> One-line mental model: **there is a single Caddy container that owns :443 for the whole host and
> reverse-proxies each domain to that project's container. Its Caddyfile is a single bind-mounted
> file that every project appends its own marker-scoped block into. Break that file and you take
> down every site on the box at once.**


## 1. Topology

- **Droplet:** one DigitalOcean host, FRA1, public IP `64.225.108.200`, port 443 open to the world.
- **The shared proxy:** container **`videodead-caddy-1`** (image `caddy:2-alpine`) owns `:443` and
  terminates TLS for *every* site. It reverse-proxies each hostname to that project's container over
  a shared external Docker network (`videodead_appnet`).
- **Independent projects behind it** (each its own compose project, none may disturb another):
  - `jobhuntwow` → `jobhuntwow.com` → `jhw-web:8000`
  - `colt-stack` → `cybergod.ai`, `godeyes.ai/observe` (Grafana)
  - `videodead` → its own sites; **owns the Caddyfile and the caddy container**
  - Amnezia VPN, Joplin — unrelated, HTTP-independent.
- **The Caddyfile:** a single file on the host at **`/opt/videodead/Caddyfile`**, **bind-mounted as
  a FILE** (not a directory) into `videodead-caddy-1` at `/etc/caddy/Caddyfile`.

```
internet :443
      │
      ▼
videodead-caddy-1   (reads /etc/caddy/Caddyfile once at start; owns TLS for ALL hosts)
      ├── jobhuntwow.com      → reverse_proxy jhw-web:8000
      ├── cybergod.ai         → reverse_proxy colt-web:8000
      ├── godeyes.ai/observe  → Grafana
      └── <your project>      → reverse_proxy <your-container>:<port>
                         (all reachable over the external network videodead_appnet)
```


## 2. The vhost contract — how a project adds/edits its own block

Every project owns exactly one block in the shared Caddyfile, delimited by **unique begin/end
markers**, and re-applies it programmatically. Example (jobhuntwow's, from `deploy/caddy/jobhuntwow.caddy`):

```
# jhw:jobhuntwow BEGIN — managed by deploy_direct.py / CI.
www.jobhuntwow.com { redir https://jobhuntwow.com{uri} permanent }
jobhuntwow.com {
	log { output stdout
	      format json }          # access logs -> promtail -> Loki -> Grafana
	encode zstd gzip
	reverse_proxy jhw-web:8000 { flush_interval -1 }   # flush -1 keeps SSE unbuffered
}
# jhw:jobhuntwow END
```

Rules the contract enforces (violating any of these is how Caddy breaks for *everyone*):

1. **Own only your marker block.** Your deploy strips everything between your BEGIN/END markers and
   re-appends a fresh copy. Never edit another project's block, and never claim a hostname you don't
   own.
2. **Your app container must be on the shared network** `videodead_appnet` so caddy can resolve it by
   name (`reverse_proxy <service>:<port>`). One network only — a container on two networks makes
   Docker DNS hand caddy an unreachable IP and you get intermittent 502s.
3. **Rewrite the file IN PLACE; never `sed -i`.** See §3.1 — this is the single most damaging mistake.
4. **Validate + reload after writing**, and prove routing, not just content (see §3.2, §3.6).


## 3. Failure modes we have actually hit — symptom → cause → fix

### 3.1 `sed -i` on the bind-mounted Caddyfile silently stops working
- **Symptom:** you edit `/opt/videodead/Caddyfile`, `caddy validate` says "Valid", `caddy reload`
  succeeds — and nothing changes. Repeated "successful" deploys change nothing.
- **Cause:** `/etc/caddy/Caddyfile` is a bind-mounted **file**, not a directory. `sed -i` does not
  edit in place — it writes a temp file and renames it, creating a **new inode**. The bind mount
  still points at the OLD inode, so from the first `sed -i` onward every edit is invisible to caddy.
- **Fix:** rewrite the file with `open(path, "w")` (keeps the inode). After writing, compare
  `sha256sum` on the host vs `docker exec <caddy> sha256sum /etc/caddy/Caddyfile`; if they differ,
  `docker restart` the caddy container so the mount re-resolves. Better: bind-mount the **directory**,
  not the single file, in any new stack.

### 3.2 A `{$DOMAIN}` / `{env.X}` placeholder block silently claims a hostname
- **Symptom:** every path on your domain returns some other project's site; your container's access
  log shows only healthchecks — public traffic never reaches it. Marker-based edits can't fix it.
- **Cause:** another block claims the host through an env placeholder (e.g. `{$DOMAIN} {`) that
  expands to your hostname, or a bare `root * /srv/... ; file_server`. It has no markers to strip.
- **Fix:** **parse** the Caddyfile (track top-level brace depth), expand `{$DOMAIN}`/`{env.X}` from
  the caddy container's real environment, and remove your hostname from the address list of ANY block
  that is not your managed one (deleting a block only when nothing else claims it). This is exactly
  what `deploy/fix_caddy.py` does — it never touches other vhosts, only removes *your* hostnames from
  blocks that aren't yours, then appends your marker block. Timestamped `.bak` before any change.

### 3.3 The reported line number is not the fault — count the braces first
- **Symptom:** caddy crash-loops with `Caddyfile:90: unrecognized directive: <something valid>`.
- **Cause:** an earlier block (e.g. line 79) lost its closing `}` or its directives, so everything
  below parses as that block's contents. The parser blames the first line that no longer makes sense.
  We saw `open=22 close=21` in the diagnostic and wrongly acted on line 90.
- **Fix:** on any parse error, FIRST check structural integrity — `{` count must equal `}` count.
  Repair the unbalanced delimiter (insert the missing `}` at the first unindented `# <marker> END`
  or next site header) before trusting any line number. An unbalanced brace makes every later line a
  plausible-looking liar.

### 3.4 A validator must reproduce the container's IMAGE **and** ENV
- **Symptom:** your `caddy validate` disagrees with the running container — it flags a line the live
  caddy accepts (e.g. `wrong argument count after 'email', line 3`), so your repair aborts a fix that
  was actually fine.
- **Cause:** you validated with `caddy:latest` (newer than the running version) or without the env
  the container has, so an `email {$...}` / `{$DOMAIN}` placeholder resolved differently.
- **Fix:** validate with the container's OWN image (`docker inspect -f '{{.Config.Image}}' <caddy>`)
  AND its own env (`--env`/`-e` from `.Config.Env`, minus PATH/HOME/HOSTNAME). Reproducing a container
  = image **and** environment (and mounts). A validator that can't see what the process sees is not a
  validator.

### 3.5 A latent broken config detonates on the next REBOOT, not at write time
- **Symptom:** everything works for hours/days, then every site dies at once — typically after a
  reboot or an auto-patch — with no recent config change to point to.
- **Cause:** **Caddy reads its Caddyfile once, at start.** A file damaged at any earlier time is
  invisible while the process keeps serving from its in-memory config, until the next restart loads
  the broken file. "What changed just before the outage?" finds nothing; the damage was hours earlier.
- **Fix:** validate at WRITE time (every appender), and run a watchdog that validates the live file on
  a timer + `OnBootSec` — don't wait for a restart to discover breakage. Gate reboots on a valid
  config (an auto-patcher must refuse to reboot if the proxy config is invalid).

### 3.6 Content match is NOT proof of routing
- **Symptom:** a deploy "verifies OK" (the page body looks right) but the app was never reached —
  a byte-copy of a static page was being served by a different block.
- **Cause:** matching response *content* says nothing about *which block* served it.
- **Fix:** tag the request (`/api/health?probe<ts>=1`) and grep the TARGET container's access log for
  that tag. If the tag isn't there, something else owns the vhost — no matter what the body looks like.


## 4. Remediation tooling (reusable pattern)

- **`fix_caddy.py`** (runs on the droplet): the parser-based repair in §3.2 — makes a hostname belong
  to exactly one block, removes it from any other, appends the managed marker block, `.bak` first.
- **Write-in-place + host-vs-container sha256 compare + restart-on-mismatch** (§3.1).
- **Validate in the container's own image+env** (§3.4); **brace-balance check before line-number
  repair** (§3.3).
- **Generated, not hand-edited, config (the strongest fix):** keep each project's block as an isolated
  fragment (`/opt/<proxy>/blocks/<project>.caddy`) and ASSEMBLE the monolith from fragments, so one
  project can never delete another's bytes. Validate at write time (own image+env + brace/marker
  check), refuse+rollback on failure, and run a 10-minute + `OnBootSec` watchdog that re-assembles
  from fragments and alerts. In-place writes only (a single-file bind mount pins the inode).


## 5. Test / remediation checklist for the team that broke it

1. **Is caddy even up?** `docker ps -a | grep caddy`. If `Restarting`, it's a crash loop — do NOT
   `docker restart` blindly (that hides the cause). Read its log oldest-first: `docker logs videodead-caddy-1 2>&1 | head -40`.
2. **Braces balanced?** On the host: count `{` vs `}` in `/opt/videodead/Caddyfile`. Unequal → fix the
   structure first (§3.3), ignore the reported line number.
3. **Host vs container in sync?** `sha256sum /opt/videodead/Caddyfile` vs
   `docker exec videodead-caddy-1 sha256sum /etc/caddy/Caddyfile`. Differ → you `sed -i`'d it (§3.1);
   rewrite in place + restart caddy.
4. **Validate the way the container sees it** (§3.4): its own image + its own env.
5. **Who owns each hostname?** `docker exec videodead-caddy-1 caddy adapt --config /etc/caddy/Caddyfile`
   and check the routes. A hostname in a block that isn't yours (or via `{$DOMAIN}`) is the §3.2 bug.
6. **Prove routing, not content** (§3.6): probe-tag a request and grep the target container's log.
7. **Only after it's healthy:** reload (`docker exec videodead-caddy-1 caddy reload --config
   /etc/caddy/Caddyfile`) or `docker restart videodead-caddy-1`.

Golden rules: never `sed -i` a bind-mounted file · edit only your own marker block · one network per
app container · validate at write time in the container's own image+env · a reboot is when latent
damage detonates, so validate on a timer and gate reboots on a valid config.
