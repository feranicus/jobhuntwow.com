#!/usr/bin/env python3
"""diagnose_web.py — ONE ssh session. Answers: who ACTUALLY serves https://jobhuntwow.com?

Self-contained: ships whoserves.py inline (base64), so it does not depend on a prior deploy.
Reads Caddy's RUNNING config from the admin API (:2019) — the Caddyfile on disk is NOT evidence
of what Caddy is currently doing, and assuming it was cost several wasted cycles.
"""
from __future__ import annotations
import base64, os, subprocess, sys

HERE = os.path.abspath(os.path.dirname(__file__))
HOST = os.environ.get("DROPLET_HOST", "64.225.108.200")
USER = os.environ.get("DROPLET_USER", "root")


def _find_key() -> str:
    k = os.environ.get("SSH_KEY", "")
    if k and os.path.exists(k):
        return k
    home = os.path.expanduser("~")
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        c = os.path.join(home, ".ssh", name)
        if os.path.exists(c):
            return c
    return ""


TGT = f"{USER}@{HOST}"
SSH = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
       "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
_K = _find_key()
if _K:
    SSH += ["-i", _K]

# NOTE: plain string + .replace(), never an f-string -- this contains {} (find -exec) and
# {{.Names}} (docker --format), both of which an f-string mangles or rejects.
SCRIPT = r"""
set -u
base64 -d > /tmp/whoserves.py <<'WS_EOF'
__WHOSERVES_B64__
WS_EOF

CADDY_CT="$(docker ps --format '{{.Names}}' | grep -i caddy | head -1)"
CF="$(docker inspect "$CADDY_CT" --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')"
echo "caddy container : $CADDY_CT"
echo "Caddyfile path  : $CF"

echo
echo "===== A. CADDY'S *RUNNING* CONFIG (admin API :2019) = ground truth ====="
docker exec "$CADDY_CT" wget -qO- http://127.0.0.1:2019/config/ > /tmp/running.json 2>/dev/null \
  || docker exec "$CADDY_CT" curl -s http://127.0.0.1:2019/config/ > /tmp/running.json 2>/dev/null \
  || echo "(admin API unreachable)"
echo -n "  running config bytes: "; wc -c < /tmp/running.json 2>/dev/null || echo 0
python3 /tmp/whoserves.py < /tmp/running.json 2>&1 | head -60 || echo "(parse failed)"

echo
echo "===== B. WHAT THE FILE ON DISK WOULD PRODUCE (adapt) ====="
docker exec "$CADDY_CT" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile 2>/dev/null > /tmp/onfile.json
python3 /tmp/whoserves.py < /tmp/onfile.json 2>&1 | head -40 || echo "(adapt failed)"

echo
echo "===== C. IS THE RUNNING CONFIG THE SAME AS THE FILE ON DISK? ====="
python3 - <<'CMP_EOF'
import json
try:
    a = json.load(open('/tmp/running.json')); b = json.load(open('/tmp/onfile.json'))
    print("  running == file ?", "YES" if a == b else "NO  <-- CADDY IS RUNNING SOMETHING ELSE")
except Exception as e:
    print("  could not compare:", e)
CMP_EOF

echo
echo "===== D. HUNT THE ~259 KB ONE-PAGER THAT IS ACTUALLY BEING SERVED ====="
echo "--- inside the caddy container ---"
docker exec "$CADDY_CT" sh -c 'find /srv /usr/share/caddy /var/www -maxdepth 4 -name "*.html" -size +240k -size -280k 2>/dev/null | while read f; do ls -l "$f"; done' || true
echo "--- on the host ---"
find /srv /opt /var/www -maxdepth 5 -name "*.html" -size +240k -size -280k 2>/dev/null | while read f; do ls -l "$f"; done | head
echo "--- every running container that holds one ---"
for c in $(docker ps --format '{{.Names}}'); do
  docker exec "$c" sh -c 'find / -xdev -maxdepth 6 -name "*.html" -size +240k -size -280k 2>/dev/null | head -3' 2>/dev/null | while read -r f; do echo "  $c : $f"; done
done

echo
echo "===== E. WHO ANSWERS ON 443 + WHAT CERT IS SERVED ====="
docker ps --format '{{.Names}}   {{.Ports}}' | grep -E ':443|:80' || true
echo | openssl s_client -connect 127.0.0.1:443 -servername jobhuntwow.com 2>/dev/null | openssl x509 -noout -subject -issuer -dates 2>/dev/null || echo "  (no cert info)"

echo
echo "===== G. OBSERVABILITY WIRING (how do app events reach Grafana?) ====="
echo "--- promtail / loki / grafana containers ---"
docker ps --format '{{.Names}}   {{.Image}}' | grep -iE 'promtail|loki|grafana' || echo "  (none running)"
for pt in $(docker ps --format '{{.Names}}' | grep -i promtail); do
  echo "--- $pt volumes (where it reads logs from) ---"
  docker inspect "$pt" --format '{{range .Mounts}}  {{.Type}} {{.Source}} -> {{.Destination}}{{println}}{{end}}'
  echo "--- $pt scrape targets ---"
  docker exec "$pt" sh -c 'cat /etc/promtail/config.yml 2>/dev/null || cat /etc/promtail/promtail.yml 2>/dev/null' | grep -E '__path__|job:|host:' | head -20
done
echo "--- does jhw-web write events anywhere promtail can see? ---"
docker inspect jhw-web --format '{{range .Mounts}}  {{.Type}} {{.Source}} -> {{.Destination}}{{println}}{{end}}'
echo -n "  EVENTS_LOG inside jhw-web: "; docker exec jhw-web sh -c 'echo $EVENTS_LOG; ls -l $EVENTS_LOG 2>/dev/null | head -1' || true
echo "--- most recent app events (proves the format Grafana would receive) ---"
docker logs --tail 200 jhw-web 2>&1 | grep -E '"evt"' | tail -8 || echo "  (no structured events yet)"

echo
echo "===== F. TAGGED REQUEST — which container logs it? ====="
PROBE="probe$(date +%s)"
curl -sk --resolve jobhuntwow.com:443:127.0.0.1 "https://jobhuntwow.com/api/health?$PROBE=1" -o /tmp/body.txt -D /tmp/hdr.txt
echo "  status line:"; head -1 /tmp/hdr.txt | sed 's/^/    /'
echo -n "  body bytes: "; wc -c < /tmp/body.txt
for c in $(docker ps --format '{{.Names}}'); do
  if docker logs --since 90s "$c" 2>&1 | grep -q "$PROBE"; then echo "  LOGGED BY -> $c"; fi
done
echo "  (nothing listed = caddy answered it itself, from disk)"
"""


def build_script() -> str:
    ws = base64.b64encode(
        open(os.path.join(HERE, "deploy", "whoserves.py"), "rb").read()).decode()
    return SCRIPT.replace("__WHOSERVES_B64__", ws)


def main():
    print(f"=== diagnosing {TGT} ===", flush=True)
    r = subprocess.run(SSH + [TGT, "bash -s"], input=build_script().encode(), timeout=300)
    if r.returncode:
        sys.exit(f"[X] ssh/diagnose failed (exit {r.returncode})")


if __name__ == "__main__":
    main()
