#!/usr/bin/env python3
"""gate.py - validate a change on the STAGING droplet before production is touched.

BUILDING BLOCK, NOT A COMMAND. `python ship.py` calls it. `python deploy/stagegate/gate.py` exists
for debugging only and the operator should never need it (CLAUDE.md: ONE orchestrator, ONE verb).

    ship.py -> gate.run() -> ("GO"|"NO-GO"|"SKIP", digest) -> production deploy proceeds or is refused

WHY A SECOND DROPLET AND WHY A REBOOT
-------------------------------------
Config that is valid on disk but never re-read is invisible until the next restart. On this very
host a shared Caddyfile was truncated at 16:15, nothing failed for twelve hours, and an unattended
kernel reboot at 04:22 made Caddy re-read the file and took every domain down together. There was
no environment in which that change could have been REBOOTED first.

So this gate is not "did the deploy succeed". It is:
    deploy to staging (the SAME function production uses) -> health -> REBOOT IT -> health again
    -> completeness -> advisory panel -> GO / NO-GO
The reboot is the point, and it cannot be run on production or in a container that shares the
host kernel.

THE HOST IS A PARAMETER, AND AN UNCONFIGURED GATE SAYS SO OUT LOUD
------------------------------------------------------------------
    JHW_STAGING_HOST=<ip>            the twin (nothing else needed)
There is no default: a hardcoded IP would either be someone else's box or silently absent. When it
is unset `run()` returns "SKIP" with the one line that enables it, and ship.py prints that and
CONTINUES. A check that cannot run must SAY SO - never silently pass, and never silently fail a
release for a facility the operator has not provisioned yet.

WHAT THIS GATE DOES *NOT* PROVE, stated here so nobody has to infer it
  * staging is deployed with --no-caddy and has NO shared reverse proxy, so nothing here measures
    the vhost, TLS, or SSE buffering through Caddy. Production proves that separately via
    `jhw.py status` (which tags a request and greps jhw-web's own access log for the tag).
  * the ATS apply drivers run in a local sandbox on the operator's PC, never on a droplet.
  * `engine_fresh` hashes backend python only - not the frontend bundle, not the landing page.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import quorum  # noqa: E402  (same directory; stdlib-only for the pure functions)

APP_DIR = "/opt/jobhuntwow"
CONTAINER = "jhw-web"
# The external network + volume docker-compose.web.yml expects. On production they belong to other
# projects and already exist; on a FRESH twin they do not, and `docker compose up` fails outright
# with "external volume not found" - which would look like a broken app rather than an unprovisioned
# box. The twin therefore creates them itself, with the same default names.
NETWORK = "videodead_appnet"
EVENTS_VOLUME = "colt-stack_colt_events"

# HOW LONG THE END-TO-END JOB CHECK MAY TAKE. It drives a real tailor run (author model + a
# different-vendor auditor + rendering), so it is the slowest check by far and it costs a fraction
# of a cent per pass. It runs in BOTH passes on purpose: "a job still completes after a reboot" is
# precisely what the reboot test is for.
JOB_TIMEOUT = int(os.environ.get("JHW_GATE_JOB_TIMEOUT", "420"))


def staging_host() -> str:
    return os.environ.get("JHW_STAGING_HOST", "").strip()


def _ssh_base():
    """Reuse deploy_direct's ssh options so there is ONE definition of how we reach a droplet.

    It handles the two platform facts this repo has already paid for: Windows OpenSSH cannot
    multiplex, and a passphrase-protected key forbids BatchMode.
    """
    import deploy_direct as dd
    return list(dd.SSH), (os.environ.get("DROPLET_USER", "root"))


def ssh_script(script: str, host: str = "", timeout: int = 900, retries: int = 2):
    """Run a multi-line bash script on the twin in ONE session. Returns (stdout, stderr, rc).

    THREE PLATFORM LESSONS ARE ENCODED HERE, EACH ONE A PREVIOUS FAILURE:
      1. the payload travels over STDIN, never in argv. Windows caps a command line at ~32 KB and
         a base64'd payload silently exceeded it - surfacing as FileNotFoundError, which was then
         reported as "ssh client not found" on a machine where ssh worked perfectly.
      2. it is written as BYTES. Python text mode on Windows rewrites every \\n into \\r\\n and bash
         answers `$'\\r': command not found`.
      3. sshd 9.8 enables PerSourcePenalties by default and penalties ACCRUE, so a refused
         connection with NO output is the throttle signature and is retried with back-off. A real
         command failure still produces output and must NOT be retried.
    """
    host = host or staging_host()
    SSH, user = _ssh_base()
    delay = 15
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(SSH + ["%s@%s" % (user, host), "bash -s"],
                               input=script.encode("utf-8"), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            return "", "TIMEOUT after %ds" % timeout, 124
        dec = lambda b: (b or b"").decode("utf-8", "replace")   # noqa: E731
        out, err = dec(r.stdout), dec(r.stderr)
        if r.returncode == 255 and not out and attempt < retries:
            time.sleep(delay)
            delay *= 2
            continue
        return out, err, r.returncode
    return "", "unreachable", 255


def sections(out: str) -> dict:
    """Split output on '#### NAME' delimiters into {name: text}, in emission order."""
    res, cur = {}, None
    for line in (out or "").splitlines():
        m = re.match(r"^####\s+(\S+)\s*$", line)
        if m:
            cur = m.group(1)
            res[cur] = []
        elif cur is not None:
            res[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in res.items()}


# =========================================================================== the expected evidence
# THE GATE KNOWS WHAT IT EXPECTS. It does not count what it received.
#
# This list is COMMITTED, and both the pre-reboot and the post-reboot pass are compared against it.
# The sibling project derived "expected" from whatever the pre-reboot pass happened to produce,
# which cannot see a pre-reboot run that was ALSO cut short - both halves then agree with each other
# and the gate reports "35/35 passed" over six checks that never ran. A shrinking denominator is
# missing evidence, not a smaller pass.
#
# Adding a chk line to HEALTH without adding its name here (or vice versa) fails
# tests/test_gate_integrity.py, so the list cannot drift from the script.
EXPECTED = [
    "container_running",
    "container_healthy",
    "restart_count",
    "no_published_ports",
    "runs_as_nonroot",
    "secret_not_baked_into_image",
    "api_health",
    "api_auth",
    "app_served",
    "spa_shell",
    "html_nocache",
    "api_404_is_json",
    "datadir_writable",
    "chat_stream_delivers",
    "job_completes",
    "job_artifacts_rendered",
    "auditor_not_author",
    "engine_fresh",
    "events_written",
    "disk",
    "memory",
]

# ============================================================================ the remote scripts
PROVISION = r"""
set +e
export DEBIAN_FRONTEND=noninteractive
echo "#### PROVISION"
if ! command -v docker >/dev/null 2>&1; then
  echo "installing docker (first run on this twin)..."
  apt-get update -qq && apt-get install -y -qq ca-certificates curl git >/dev/null 2>&1
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1
fi
echo "docker : $(docker --version 2>/dev/null || echo MISSING)"
echo "python3: $(python3 --version 2>&1 || echo MISSING)"
# docker-compose.web.yml declares BOTH of these `external:`. On production they belong to other
# projects; on a fresh twin they must exist or `up` refuses to start and the whole gate fails for
# a provisioning reason rather than a code reason.
docker network create __NETWORK__ >/dev/null 2>&1 && echo "created network __NETWORK__" || echo "network __NETWORK__ present"
docker volume  create __EVENTS_VOLUME__ >/dev/null 2>&1 && echo "created volume __EVENTS_VOLUME__" || echo "volume __EVENTS_VOLUME__ present"
mkdir -p __APP_DIR__ /opt/stagegate
echo "kernel : $(uname -r)"
echo "ram    : $(free -m | awk '/Mem:/{print $2}')MB total, $(free -m | awk '/Mem:/{print $7}')MB available"
echo "disk   : $(df -h / | awk 'NR==2{print $4}') free of $(df -h / | awk 'NR==2{print $2}')"
"""

# ONE CHECK = ONE LINE = `CHECK|name|yes|no|detail`, and the SAME strings become the evidence the
# panel reads. `tr` is the PROTOCOL PROTECTION: a newline or a pipe inside a detail does not merely
# look untidy, it truncates the record and everything after it is silently lost. `cut` is ONLY a
# runaway guard for a crashing command dumping a traceback into $3, and it sits FAR above any real
# detail - the sibling project had it at 200 and silently amputated every long detail AT SOURCE for
# weeks, then "fixed" it on the printing side where the cut had never happened.
HEALTH = r"""
set +e
C=__CONTAINER__
IN="docker exec __CONTAINER__"
# A BROWSER USER-AGENT, and this is not cosmetic. Any bot/probe gate serves an unrecognised agent a
# 404 by design, so a curl-shaped UA gets the 404 the gate exists to return and the check records a
# broken app. (Measured on the sibling: its external monitor reported HTTP 404 for its own home page
# for weeks and counted it as "answering".) jobhuntwow has no such gate TODAY - verified: there is no
# BOT_404 branch in backend/app/observability.py - so this is armour against the day it gains one.
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36'
chk() { printf 'CHECK|%s|%s|%s\n' "$1" "$2" "$(printf '%s' "$3" | tr '\n|' '  ' | cut -c1-1000)"; }
echo "#### KERNEL"
uname -r
echo "#### HEALTH"

S=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
if [ "$S" = "running" ]; then chk container_running yes "$C is running"; else chk container_running no "$C is ${S:-absent}"; fi

# docker's OWN healthcheck (the image HEALTHCHECKs /api/health). "running" alone can be a boot loop
# that has not fallen over yet.
HS=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$C" 2>/dev/null)
case "$HS" in
  healthy)  chk container_healthy yes "docker healthcheck: healthy" ;;
  starting) chk container_healthy no  "docker healthcheck still 'starting' - it never went healthy" ;;
  none)     chk container_healthy no  "the image declares no healthcheck (Dockerfile.web should)" ;;
  *)        chk container_healthy no  "docker healthcheck: ${HS:-unknown}" ;;
esac

R=$(docker inspect -f '{{.RestartCount}}' "$C" 2>/dev/null || echo 99)
if [ "${R:-99}" -le 1 ]; then chk restart_count yes "restarts=$R"; else chk restart_count no "restarts=$R - crash loop, and a crash loop still reports 'running' between restarts"; fi

# HARD RULE (CLAUDE.md): nothing may be published on 0.0.0.0. Public traffic arrives ONLY through
# the shared proxy. A stray `ports:` entry is a remote-access finding, not a convenience.
PB=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$C" 2>/dev/null)
PM=$(docker inspect -f '{{json .NetworkSettings.Ports}}' "$C" 2>/dev/null)
case "${PB}${PM}" in
  'null{}'|'{}{}'|'{}null'|'nullnull') chk no_published_ports yes "no host port bindings (bindings=$PB mapped=$PM)" ;;
  *) chk no_published_ports no "the container PUBLISHES ports: bindings=$PB mapped=$PM" ;;
esac

U=$($IN id -u 2>/dev/null)
if [ "$U" = "10001" ]; then chk runs_as_nonroot yes "uid=$U (the non-root user the image creates)"; else chk runs_as_nonroot no "uid=${U:-unknown}, expected 10001"; fi

# A secret must never be baked into a layer. Compare the IMAGE's env, not the container's: the
# container legitimately receives DO_INFERENCE_KEY at runtime via env_file.
IMG=$(docker inspect -f '{{.Config.Image}}' "$C" 2>/dev/null)
IE=$(docker image inspect "$IMG" -f '{{json .Config.Env}}' 2>/dev/null)
case "$IE" in
  *DO_INFERENCE_KEY*|*AGENT_PROXY_TOKEN*|*GMAIL_SA_B64*) chk secret_not_baked_into_image no "image $IMG carries a secret NAME in its env layer: $IE" ;;
  \[*\]) chk secret_not_baked_into_image yes "read the env of image $IMG and it carries no secret keys" ;;
  *) chk secret_not_baked_into_image no "could not read the image config for ${IMG:-unknown} (got '${IE:-empty}') - being unable to see the subject is not a clean image" ;;
esac

# ---- the app itself. Measured from INSIDE the container: there are no published ports on the host,
# ---- and on the twin there is no shared proxy in front, so this is uvicorn directly.
H=$($IN curl -s -o /tmp/h.json -w '%{http_code}' --max-time 15 http://127.0.0.1:8000/api/health 2>/dev/null)
B=$($IN sh -c 'head -c 400 /tmp/h.json 2>/dev/null')
case "$H|$B" in
  200*'"ok":true'*) chk api_health yes "GET /api/health -> 200 $B" ;;
  *) chk api_health no "GET /api/health -> ${H:-no answer} body=${B:-empty}" ;;
esac

# AUTH IS ARMED. Probing "/" alone would go green on a cached SPA shell served to anyone.
H=$($IN curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8000/api/me 2>/dev/null)
if [ "$H" = "401" ]; then chk api_auth yes "GET /api/me with no cookie -> 401 (up AND locked)"; else chk api_auth no "GET /api/me -> ${H:-no answer}, want 401 - the tenant gate is not enforcing"; fi

$IN curl -s -A "$UA" -o /tmp/root.html -w '%{http_code}' --max-time 20 http://127.0.0.1:8000/ >/tmp/rc.txt 2>/dev/null
H=$($IN sh -c 'cat /tmp/rc.txt 2>/dev/null')
SZ=$($IN sh -c 'wc -c < /tmp/root.html 2>/dev/null' | tr -d ' ')
# BYTES, NOT JUST THE STATUS CODE: a 200 with an empty body is the failure mode this project has
# actually shipped (a file_server pointed at an empty directory answered 200/0 bytes).
if $IN grep -q 'href="/login"' /tmp/root.html 2>/dev/null; then
  chk app_served yes "GET / (browser UA) -> $H, ${SZ} bytes, links to /login"
else
  chk app_served no "GET / -> ${H:-no answer}, ${SZ:-0} bytes, and the body does NOT link to /login"
fi

$IN curl -s -A "$UA" -o /tmp/login.html --max-time 20 http://127.0.0.1:8000/login >/dev/null 2>&1
if $IN grep -qE 'id="root"|id=root' /tmp/login.html 2>/dev/null; then
  chk spa_shell yes "GET /login -> the built SPA shell (id=root) is in the image"
else
  chk spa_shell no "GET /login is NOT the SPA shell - the frontend build did not land in the image"
fi

CC=$($IN curl -s -A "$UA" -o /dev/null -D - --max-time 20 http://127.0.0.1:8000/ 2>/dev/null | tr -d '\r' | grep -i '^cache-control:' | head -1)
case "$CC" in
  *no-cache*) chk html_nocache yes "/ sends $CC" ;;
  *) chk html_nocache no "/ has NO no-cache header (got '${CC:-none}') - browsers then apply HEURISTIC caching and serve a stale landing for days" ;;
esac

# The SPA catch-all must never swallow an unknown API path and turn a 404 into HTML.
AJ=$($IN curl -s --max-time 15 -o /tmp/nf.json -w '%{http_code}' http://127.0.0.1:8000/api/definitely-not-a-route 2>/dev/null)
if [ "$AJ" = "404" ] && $IN grep -q 'detail' /tmp/nf.json 2>/dev/null; then
  chk api_404_is_json yes "unknown /api path -> 404 JSON, not the SPA shell"
else
  chk api_404_is_json no "unknown /api path -> ${AJ:-no answer} and the body is not JSON - the SPA catch-all is swallowing API routes"
fi

if $IN sh -c 'touch /data/.gatewrite && rm -f /data/.gatewrite' 2>/dev/null; then
  chk datadir_writable yes "DATA_DIR=/data is writable by uid 10001 (the JSON store and artifacts live there)"
else
  chk datadir_writable no "/data is NOT writable by the app user - the store and every artifact write fails"
fi

# ---- THE LIVE MODEL PATH, END TO END. /api/chat is a StreamingResponse; a stream that opens and
# ---- emits nothing is the empty-200 of streaming, so count the BYTES.
$IN sh -c 'printf %s "{\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ready\"}]}" > /tmp/chat.json'
SB=$($IN sh -c 'curl -s --max-time 90 -X POST -H "Content-Type: application/json" --data-binary @/tmp/chat.json http://127.0.0.1:8000/api/chat 2>/dev/null | head -c 2000 | wc -c' | tr -d ' ')
if [ "${SB:-0}" -ge 4 ]; then
  chk chat_stream_delivers yes "POST /api/chat streamed ${SB} bytes (the DO proxy, the key and the stream all work)"
else
  chk chat_stream_delivers no "POST /api/chat delivered ${SB:-0} bytes - the stream opened and emitted nothing (key, proxy or model)"
fi

# ---- A REAL JOB, END TO END, AND WE READ THE OUTPUT. "The endpoint returned 200" is not "the job
# ---- worked": a well-formed nothing parses perfectly and renders an empty document.
cat > /tmp/jhw_gen.json <<'GENEOF'
{"email":"gate@staging.invalid",
 "jd":"Senior Presales Solutions Architect, Frankfurt. Own technical discovery and demos for enterprise network security deals. Required: 8+ years presales, SASE and ZTNA, MEDDICC qualification, fluent German and English, RFP leadership.",
 "profile":"# Jane Gate\nPresales Solutions Architect, Frankfurt.\n\n## Experience\n### Acme Networks - Senior Presales Engineer (2019-2026)\n- Led technical discovery and demos for enterprise network security deals across DACH.\n- Ran RFP responses with legal and finance; qualified opportunities with MEDDICC.\n- Designed SASE and ZTNA architectures for retail and manufacturing customers.\n### Globex GmbH - Network Engineer (2015-2019)\n- Operated MPLS and SD-WAN estates for German mid-market customers.\n\n## Skills\nSASE, ZTNA, SD-WAN, MPLS, MEDDICC, RFP leadership, German (native), English (fluent)\n"}
GENEOF
docker cp /tmp/jhw_gen.json "$C":/tmp/jhw_gen.json >/dev/null 2>&1
GC=$($IN curl -s -o /tmp/jhw_gen_out.json -w '%{http_code}' --max-time __JOB_TIMEOUT__ \
     -X POST -H 'Content-Type: application/json' --data-binary @/tmp/jhw_gen.json \
     http://127.0.0.1:8000/api/electronic/generate 2>/dev/null)
$IN sh -c 'cat /tmp/jhw_gen_out.json 2>/dev/null' > /tmp/jhw_gen_out.json 2>/dev/null
# THE MANIFEST NAMING A FILE IS NOT THE FILE EXISTING WITH CONTENT IN IT. Size the rendered
# artifacts where they actually live: inside the container, under DATA_DIR.
JID=$(sed -n 's/.*"job_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /tmp/jhw_gen_out.json | head -1)
: > /tmp/jhw_sizes.txt
if [ -n "$JID" ]; then
  $IN sh -c "cd /data 2>/dev/null && find . -path '*$JID*' -type f \( -name '*.pdf' -o -name '*.docx' \) -printf '%f %s\n' 2>/dev/null" > /tmp/jhw_sizes.txt 2>/dev/null
fi
# THREE DIFFERENT PROPERTIES of one expensive run, so a failure says WHICH one broke. They are
# computed in python because the manifest is JSON, in the ONE helper shipped by ship_verdict_py() -
# a second copy of "what does this manifest prove" is a second place for it to rot. The verdicts are
# still EMITTED by chk, so there is exactly one emitter of the CHECK| protocol.
python3 /tmp/jhw_verdict.py "$GC" /tmp/jhw_gen_out.json /tmp/jhw_sizes.txt > /tmp/jhw_gen_verdicts.txt 2>&1
while IFS='|' read -r n o d; do
  [ -n "$n" ] && chk "$n" "$o" "$d"
done < /tmp/jhw_gen_verdicts.txt

# ---- THE DEPLOY PROOF. A liveness probe is not a deploy proof: a three-day-old container answers
# ---- /api/health and 401s /api/me perfectly happily. Compare the sha256 of every backend python
# ---- file INSIDE the container against the sources this deploy actually shipped to the droplet.
SUM_IN=$($IN sh -c 'cd /app/app 2>/dev/null && find . -name "*.py" -not -path "*__pycache__*" | LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum' | cut -d" " -f1)
N_IN=$($IN sh -c 'cd /app/app 2>/dev/null && find . -name "*.py" -not -path "*__pycache__*" | wc -l' | tr -d ' ')
SUM_ON=$(cd __APP_DIR__/backend/app 2>/dev/null && find . -name "*.py" -not -path "*__pycache__*" | LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum | cut -d" " -f1)
N_ON=$(cd __APP_DIR__/backend/app 2>/dev/null && find . -name "*.py" -not -path "*__pycache__*" | wc -l | tr -d ' ')
if [ "${N_IN:-0}" -lt 6 ] || [ "${N_ON:-0}" -lt 6 ]; then
  chk engine_fresh no "could not enumerate the sources (container=${N_IN:-0} files, droplet=${N_ON:-0}) - the check could not see its subject, which is not the same as a match"
elif [ -n "$SUM_IN" ] && [ "$SUM_IN" = "$SUM_ON" ]; then
  chk engine_fresh yes "backend python in the container matches the shipped sources ($N_IN files, ${SUM_IN}) - covers backend/app/*.py only, NOT the frontend bundle"
else
  chk engine_fresh no "STALE CONTAINER: container=${SUM_IN} ($N_IN files) vs shipped=${SUM_ON} ($N_ON files) - the image was not rebuilt from this source"
fi

EL=$($IN sh -c 'printenv EVENTS_LOG' 2>/dev/null)
if [ -z "$EL" ]; then
  chk events_written no "EVENTS_LOG is not set in the container - structured events have nowhere to go"
else
  EN=$($IN sh -c "grep -c 'jhw-web' \"$EL\" 2>/dev/null" | tr -d ' ')
  if [ "${EN:-0}" -ge 1 ]; then
    chk events_written yes "$EN line(s) tagged jhw-web in $EL (proves the WRITE; on the twin there is no promtail/Loki, so it does NOT prove the pipeline)"
  else
    chk events_written no "no jhw-web lines in $EL - the app cannot append (the shared volume is root-owned and the app is uid 10001)"
  fi
fi

DF=$(df -P / | awk 'NR==2{print $4}')
DP=$(df -P / | awk 'NR==2{print $5}' | tr -d '%')
if [ "${DP:-100}" -lt 90 ]; then chk disk yes "${DF}KB free on / (${DP}% used)"; else chk disk no "${DP}% of / is used (${DF}KB free) - a docker build will fail"; fi

MA=$(free -m | awk '/Mem:/{print $7}')
if [ "${MA:-0}" -ge 250 ]; then chk memory yes "${MA}MB available"; else chk memory no "only ${MA:-0}MB available - the app or a build will be OOM-killed"; fi

echo "#### HEALTH_END"
"""

# The verdict computation is written to the droplet ONCE and re-used by both python invocations, so
# there is exactly one implementation of "what does this manifest prove".
VERDICT_PY = r"""
# jhw_verdict.py - job verdicts from the generate manifest.
# ONE implementation, shipped to the droplet by gate.ship_verdict_py() and called once per pass.
#
#     python3 /tmp/jhw_verdict.py <http_code> <manifest.json> [<sizes.txt>]
#
# Prints `name|yes|no|detail` lines; the CALLER emits them through chk(), so the CHECK| protocol
# keeps exactly one emitter.
#
# NOTE: no docstring, deliberately. This source is embedded in an r-string in gate.py and a triple
# quote here terminates that literal - which is a syntax error in gate.py, not here.
import json, sys

code = sys.argv[1] or "?"
path = sys.argv[2]
sizes = {}
if len(sys.argv) > 3:
    try:
        for ln in open(sys.argv[3], encoding="utf-8", errors="replace"):
            parts = ln.split()
            if len(parts) >= 2 and parts[-1].isdigit():
                sizes[" ".join(parts[:-1])] = int(parts[-1])
    except Exception:
        pass


def out(name, ok, detail):
    # squash newlines and pipes HERE too: this text becomes a CHECK| field downstream.
    print("%s|%s|%s" % (name, "yes" if ok else "no",
                        " ".join(str(detail).replace("|", " ").split())[:900]))


try:
    m = json.load(open(path, encoding="utf-8"))
    if not isinstance(m, dict):
        raise ValueError("manifest is %s, not an object" % type(m).__name__)
except Exception as e:
    raw = ""
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()[:200]
    except Exception:
        pass
    # Report the failure on ALL THREE names. Staying silent would turn a readable failure into
    # missing evidence, which is a worse thing to hand an operator.
    for n in ("job_completes", "job_artifacts_rendered", "auditor_not_author"):
        out(n, False, "HTTP %s and the response is not a manifest (%s: %s) raw=%r"
                      % (code, type(e).__name__, e, raw))
    sys.exit(0)

files = [f for f in (m.get("files") or []) if isinstance(f, str)]
out("job_completes", code == "200" and len(files) >= 4,
    "HTTP %s, %d rendered file(s) %s, elapsed %sms, job_id=%s, warnings=%d, quality=%s"
    % (code, len(files), files[:6], m.get("elapsed_ms"), m.get("job_id"),
       len(m.get("warnings") or []), m.get("quality")))

small = sorted(k for k, v in sizes.items() if int(v or 0) < 6000)
if sizes:
    out("job_artifacts_rendered", (not small) and len(sizes) >= 4,
        "on disk: %s%s" % (sorted("%s=%dB" % (k, int(v)) for k, v in sizes.items()),
                           (" TOO SMALL, a well-formed nothing renders too: %s" % small)
                           if small else ""))
else:
    out("job_artifacts_rendered", False,
        "no rendered artifact could be sized on disk (the manifest listed %d file(s)) - being "
        "unable to see the subject is not a pass" % len(files))

mods = m.get("models") or {}
author, auditor = str(mods.get("resume") or ""), str(mods.get("auditor") or "")
out("auditor_not_author", bool(author) and bool(auditor) and author != auditor,
    "author=%r auditor=%r auditor_vendor=%r - nothing may mark its own work"
    % (author, auditor, mods.get("auditor_vendor")))
"""

REBOOT = "echo '#### REBOOT'\n(sleep 1; systemctl reboot) >/dev/null 2>&1 &\necho issued\n"
BOOTID = ("echo '#### BOOTID'\ncat /proc/sys/kernel/random/boot_id\nuname -r\n"
          "cut -d. -f1 /proc/uptime\n")


def health_script() -> str:
    """The rendered HEALTH script. Substitution, NOT %-format: the script is full of printf '%s'."""
    return (HEALTH.replace("__CONTAINER__", CONTAINER)
                  .replace("__APP_DIR__", APP_DIR)
                  .replace("__JOB_TIMEOUT__", str(JOB_TIMEOUT)))


def provision_script() -> str:
    return (PROVISION.replace("__NETWORK__", NETWORK)
                     .replace("__EVENTS_VOLUME__", EVENTS_VOLUME)
                     .replace("__APP_DIR__", APP_DIR))


def ship_verdict_py() -> str:
    """Put the one verdict implementation on the droplet before HEALTH runs."""
    return ("mkdir -p /opt/stagegate\ncat > /tmp/jhw_verdict.py <<'JHWVEOF'\n%s\nJHWVEOF\n"
            "python3 -m py_compile /tmp/jhw_verdict.py || echo '[!] verdict helper does not compile'\n"
            % VERDICT_PY)


# ============================================================================ parsing the evidence
# A PASS whose DETAIL describes a failure is a BROKEN CHECK, and a check we cannot trust must not be
# counted as evidence that we may promote. Twice on the sibling project the panel spotted exactly
# this and was right both times; it is cheaper to demote it automatically.
_CONTRADICTION = re.compile(
    r"(?i)\b(stale|unrecognis|unrecogniz|unavailable|cannot|could not|failed|failure|broken|"
    r"never |no answer|empty|too small|refus)")
# Phrases that legitimately contain a scary word while describing a HEALTHY outcome. Without these,
# "carries no secret keys" and "does NOT prove the pipeline" would flag themselves. Keep this list
# SHORT and specific: a blanket exemption for one question silences a different question.
_BENIGN = re.compile(r"(?i)(no secret keys|does not prove the pipeline|not the same as a match|"
                     r"no host port bindings|never mark|not empty)")


def self_contradictory(c: dict) -> str:
    """"" or a fragment SHOWING THE MATCH IN CONTEXT.

    It must show the context. Returning the bare regex match printed `detail says 'refus'` on the
    sibling - five characters, matched inside the word "refuses" in a detail that was describing
    correct behaviour, and it took a model reasoning from first principles to work out why a green
    run was being failed. Print what matched AND what surrounds it.
    """
    if not c.get("ok"):
        return ""
    d = str(c.get("detail") or "")
    if _BENIGN.search(d):
        return ""
    m = _CONTRADICTION.search(d)
    if not m:
        return ""
    a, b = max(0, m.start() - 30), min(len(d), m.end() + 30)
    return "%r in ...%s..." % (m.group(0), d[a:b].strip())


def parse_checks(text: str) -> list:
    out = []
    for ln in (text or "").splitlines():
        if not ln.startswith("CHECK|"):
            continue
        p = ln.split("|", 3)
        if len(p) != 4:
            continue
        c = {"name": p[1].strip(), "ok": p[2].strip() == "yes", "detail": p[3].strip()}
        word = self_contradictory(c)
        if word:
            c["ok"] = False
            c["detail"] = ("SELF-CONTRADICTORY CHECK (said PASS, detail says %s) -> treated as a "
                           "FAILURE: %s" % (word, c["detail"]))
        out.append(c)
    return out


def say_check(say, c, pad=26, width=96):
    """Print ONE check, WRAPPING the detail rather than truncating it.

    The detail is where a check states WHAT it measured. Cutting it destroys exactly the evidence
    the check exists to provide, silently, on the PASSING path where nobody looks twice - and it
    feeds the panel, so a reviewer reading half a sentence reasons from half the facts. ONE
    implementation, called by both passes: the sibling fixed the pre-reboot loop and left the
    post-reboot loop truncating for three more releases, twelve lines below the comment explaining
    why truncating is wrong.
    """
    d = str(c.get("detail") or "")
    say("  %-*s %s  %s" % (pad, c["name"], "OK  " if c["ok"] else "FAIL", d[:width]))
    rest = d[width:]
    while rest:
        say("  %-*s       %s" % (pad, "", rest[:width]))
        rest = rest[width:]


def completeness(checks: list, expected: list, label: str, rc, prefix="") -> dict:
    """One check ABOUT the checks: did every COMMITTED expectation report in this pass?"""
    got = {c["name"][len(prefix):] if prefix and c["name"].startswith(prefix) else c["name"]
           for c in checks}
    lost = sorted(set(expected) - got)
    if lost:
        return {"name": prefix + "evidence_complete", "ok": False,
                "detail": "%d expected check(s) never reported in the %s pass: %s. The run was cut "
                          "short (ssh rc=%s), so this is MISSING EVIDENCE, not a smaller pass."
                          % (len(lost), label, ", ".join(lost), rc)}
    return {"name": prefix + "evidence_complete", "ok": True,
            "detail": "all %d expected checks reported in the %s pass" % (len(expected), label)}


# ============================================================================ droplet interaction
def deploy_to_staging(say) -> bool:
    """Deploy with THE SAME FUNCTION production uses, pointed at the twin, with no proxy wiring.

    `deploy_direct.deploy()` is exactly what `jhw.py deploy` calls. Using a different path for
    staging would validate something other than what ships - and the sibling's first gate did not
    deploy at all, then health-checked a container that had never been put there and failed 14
    checks for entirely the wrong reason. A gate that fails because the GATE is incomplete teaches
    you to ignore it.

    A SUBPROCESS, not an import: deploy_direct resolves HOST/TGT/SSH at IMPORT time from the
    environment, so overriding the host inside this process would be read too late. `--no-caddy` is
    an existing flag; the twin has no shared proxy to wire.
    """
    host = staging_host()
    say("provisioning %s (docker, the external network + volume compose expects)..." % host)
    out, err, rc = ssh_script(provision_script(), timeout=600)
    for ln in (sections(out).get("PROVISION") or "").splitlines():
        say("  " + ln)
    if rc != 0:
        say("  [X] provisioning failed (rc=%s) %s" % (rc, (err or "").strip()[:200]))
        return False
    say("deploying to the twin with deploy_direct.deploy() - the same call jhw.py deploy makes")
    env = dict(os.environ, DROPLET_HOST=host)
    r = subprocess.run([sys.executable, "-c",
                        "import deploy_direct as dd; dd.deploy(False)"],
                       cwd=ROOT, env=env)
    if r.returncode != 0:
        say("  [X] the staging deploy failed (exit %s). Production was NOT touched." % r.returncode)
        return False
    return True


def boot_id(host="", quiet=False):
    """(boot_id, kernel, uptime_s) or None. retries=0 while polling: a refusal IS the signal."""
    out, _, rc = ssh_script(BOOTID, host=host, timeout=25, retries=0 if quiet else 1)
    if rc != 0:
        return None
    lines = (sections(out).get("BOOTID") or "").splitlines()
    return (lines + ["", "", ""])[:3]


def wait_for_reboot(host, was, timeout=420):
    """Wait for a DIFFERENT boot_id. Returns (seconds, new) or (None, None).

    NOT "wait until ssh answers": right after `systemctl reboot` is issued the box is still up and
    answers instantly, which is how an earlier version of this test reported a 1-second reboot and
    PASSED. A test that can pass without the event happening is not a test. boot_id is regenerated
    by the kernel on every boot, so it is the identity of the running kernel instance.

    Sleep FIRST and sleep LONG: a droplet takes ~25-40s to come back and polling every 8s only buys
    ~30 extra ssh handshakes, which is the shape sshd's PerSourcePenalties exists to refuse - and it
    is the NEXT run that then hangs on its first connection.
    """
    t0 = time.time()
    time.sleep(25)
    while time.time() - t0 < timeout:
        now = boot_id(host, quiet=True)
        if now and was and now[0] and now[0] != was[0]:
            return int(time.time() - t0), now
        time.sleep(30)
    return None, None


def ask_panel(evidence: dict, say) -> dict:
    """Ship quorum.py into the container and run it there (the inference key lives there).

    ADVISORY. Whatever it returns, the gate below is recomputed from the deterministic checks; the
    panel cannot set it. If the panel does not answer at all, that is DATA and the release is
    unaffected.
    """
    q = open(os.path.join(HERE, "quorum.py"), encoding="utf-8").read()
    ev = json.dumps(evidence)
    script = ("set +e\nmkdir -p /opt/stagegate\ncat > /opt/stagegate/quorum.py <<'QEOF'\n%s\nQEOF\n"
              "docker cp /opt/stagegate/quorum.py %s:/tmp/quorum.py >/dev/null 2>&1\n"
              "cat > /tmp/jhw_ev.json <<'EVEOF'\n%s\nEVEOF\n"
              "docker cp /tmp/jhw_ev.json %s:/tmp/jhw_ev.json >/dev/null 2>&1\n"
              "echo '#### PANEL'\n"
              "docker exec %s sh -c 'python3 /tmp/quorum.py < /tmp/jhw_ev.json'\n"
              % (q, CONTAINER, ev, CONTAINER, CONTAINER))
    out, err, rc = ssh_script(script, timeout=900)
    body = sections(out).get("PANEL") or out
    try:
        return json.loads(body[body.index("{"):body.rindex("}") + 1])
    except Exception as e:
        say("  [!] the panel did not return JSON (%s). The gate is unaffected - it is decided by "
            "the deterministic checks." % type(e).__name__)
        if (err or "").strip():
            say("      stderr: " + (err or "").strip()[:300])
        return {}


# ============================================================================ the run
def run(reboot_test=True, quiet=False):
    """-> (gate, digest). gate is 'GO', 'NO-GO' or 'SKIP'. Never raises."""
    say = (lambda *a: None) if quiet else (lambda *a: print("  " + " ".join(str(x) for x in a),
                                                           flush=True))
    host = staging_host()
    if not host:
        return "SKIP", (
            "STAGING GATE NOT CONFIGURED - no twin to validate on, so nothing was validated.\n"
            "This is a SKIP, not a pass: no deterministic check ran and the panel saw no evidence.\n"
            "To enable it, create a droplet that matches production (same size, image and region -\n"
            "a twin that differs in those is not a twin) and set ONE variable:\n"
            "    PowerShell:  $env:JHW_STAGING_HOST = \"<ip>\"\n"
            "    bash      :  export JHW_STAGING_HOST=<ip>\n"
            "Then `python ship.py` provisions it, deploys, health-checks, REBOOTS it, health-checks\n"
            "again and asks the panel - all by itself.")

    say("staging twin: %s   (synthetic fixtures only - never production personal data)" % host)
    if not deploy_to_staging(say):
        return "NO-GO", ("Could not deploy to the staging twin (%s), so the change is UNVALIDATED. "
                         "Nothing on production was touched." % host)

    checks = []
    out, err, rc = ssh_script(ship_verdict_py() + health_script(), timeout=JOB_TIMEOUT + 480)
    pre = parse_checks(out)
    # THE SSH RETURN CODE IS NEVER DISCARDED. ssh returns PARTIAL stdout when a connection drops,
    # and a call site that ignores rc reads a cut-short run as a complete one.
    if "#### HEALTH_END" not in out:
        pre.append({"name": "health_run_complete", "ok": False,
                    "detail": "the health script never reached its terminal sentinel (ssh rc=%s, "
                              "%d check(s) received, stderr=%r). Everything above is PARTIAL "
                              "evidence." % (rc, len(pre), (err or "").strip()[:120])})
    kernel_before = (sections(out).get("KERNEL") or "").strip().splitlines()[:1]
    for c in pre:
        say_check(say, c)
    pre.append(completeness(pre, EXPECTED, "pre-reboot", rc))
    say_check(say, pre[-1])
    checks += pre

    reboot = {}
    if reboot_test:
        say("rebooting the twin - the one test production can never run...")
        was = boot_id(host)
        ssh_script(REBOOT, timeout=60, retries=0)
        waited, now = wait_for_reboot(host, was, timeout=420)
        if waited is None:
            checks.append({"name": "reboot_recovery", "ok": False,
                           "detail": "the twin never came back with a NEW boot_id within 420s "
                                     "(boot_id before=%s)" % ((was or ["?"])[0][:8])})
            say_check(say, checks[-1])
            reboot = {"came_back": False}
        else:
            say("back after %ds - boot_id %s -> %s (kernel %s)"
                % (waited, (was or ["?"])[0][:8], now[0][:8], now[1]))
            checks.append({"name": "reboot_recovery", "ok": True,
                           "detail": "boot_id CHANGED after %ds (kernel %s, uptime %ss) - the "
                                     "kernel instance is genuinely new, not merely reachable"
                                     % (waited, now[1], now[2])})
            say_check(say, checks[-1])
            out2, err2, rc2 = ssh_script(ship_verdict_py() + health_script(),
                                         timeout=JOB_TIMEOUT + 480)
            post = parse_checks(out2)
            if "#### HEALTH_END" not in out2:
                post.append({"name": "health_run_complete", "ok": False,
                             "detail": "the POST-REBOOT health script never reached its terminal "
                                       "sentinel (ssh rc=%s, %d check(s) received)" % (rc2, len(post))})
            for c in post:
                c["name"] = "post_reboot_" + c["name"]
                say_check(say, c)
            post.append(completeness(post, EXPECTED, "post-reboot", rc2, prefix="post_reboot_"))
            say_check(say, post[-1])
            reboot = {"came_back": True, "seconds": waited,
                      "boot_id_before": (was or [None])[0], "boot_id_after": now[0],
                      "kernel_before": kernel_before, "kernel_after": now[1]}
            checks += post

    expected_all = list(EXPECTED) + ["evidence_complete"]
    if reboot.get("came_back"):
        expected_all += ["reboot_recovery"] + ["post_reboot_" + n for n in EXPECTED] \
                        + ["post_reboot_evidence_complete"]
    elif reboot_test:
        expected_all += ["reboot_recovery"]

    evidence = {"host": host, "role": "staging twin (synthetic fixtures only)",
                "checks": checks, "expected": expected_all, "reboot": reboot,
                "container": CONTAINER, "ts": int(time.time())}

    say("asking 2 soldiers + 2 auditors to read the SAME evidence (advisory)...")
    verdict = ask_panel(evidence, say)

    # THE GATE IS RECOMPUTED HERE FROM THE CHECKS, whatever the panel said or failed to say. The
    # panel's own copy of the rule is only there so the artifact the operator receives shows it.
    gate, failed, missing = quorum.gate_from_checks(checks, expected_all)
    if not verdict:
        verdict = {"reviews": [], "answered": 0,
                   "digest": quorum.build_digest(evidence, checks, expected_all, failed, missing,
                                                 gate, [])}
    verdict["gate"] = gate
    globals()["_LAST"] = verdict
    return quorum.decide_from_verdict(verdict)


_LAST = {}


def last_verdict():
    return _LAST


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Validate a change on the staging twin (building "
                                             "block: `python ship.py` calls this).")
    ap.add_argument("--no-reboot", action="store_true",
                    help="skip the reboot test (faster, and WEAKER - say so if you use it)")
    ap.add_argument("--print-health", action="store_true",
                    help="print the rendered remote script and exit (no droplet needed)")
    a = ap.parse_args()
    if a.print_health:
        print(provision_script())
        print(health_script())
        return 0
    gate, digest = run(reboot_test=not a.no_reboot)
    print("\n" + digest + "\n\nGATE: %s" % gate)
    return 0 if gate in ("GO", "SKIP") else 1


if __name__ == "__main__":
    sys.exit(main())
