#!/usr/bin/env python3
"""diagnose_web.py — ONE command that answers: who actually serves jobhuntwow.com?

No theories. It SSHes to the droplet and prints hard facts:
  1. every Caddy site block that mentions jobhuntwow (and which file it is in)
  2. every running container + which one owns :80/:443
  3. what jhw-web returns on its OWN port (is our app healthy?)
  4. what Caddy returns for jobhuntwow.com forced to localhost (is the vhost wired?)
  5. what the public DNS resolves to, from the droplet's point of view
"""
from __future__ import annotations
import os, subprocess, sys

HOST = os.environ.get("DROPLET_HOST", "64.225.108.200")
USER = os.environ.get("DROPLET_USER", "root")
KEY  = os.environ.get("SSH_KEY", "")
TGT  = f"{USER}@{HOST}"
SSH  = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
if os.name != "nt":
    SSH += ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/cm-jhw-%r@%h:%p", "-o", "ControlPersist=10m"]
if KEY and os.path.exists(KEY):
    SSH += ["-i", KEY]

SCRIPT = r'''
set +e
echo "===== 1. CADDY CONTAINER + CONFIG FILE ====="
CADDY_CT="$(docker ps --format '{{.Names}}' | grep -i caddy | head -1)"
echo "caddy container : $CADDY_CT"
CF="$(docker inspect "$CADDY_CT" --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')"
echo "Caddyfile path  : $CF"
echo "--- every line mentioning jobhuntwow ---"
grep -n "jobhuntwow" "$CF" 2>/dev/null || echo "(none in the main Caddyfile)"
echo "--- imports (a vhost may live in another file) ---"
grep -n "^import\|import " "$CF" 2>/dev/null || echo "(no imports)"
echo
echo "===== 2. WHO OWNS 80/443 ====="
docker ps --format '{{.Names}}\t{{.Ports}}' | grep -E ':80->|:443->' || echo "(nothing publishes 80/443?)"
echo
echo "===== 3. IS OUR APP HEALTHY (direct to the container) ====="
docker exec "$CADDY_CT" wget -qO- -T5 http://jhw-web:8000/api/health 2>&1 | head -c 200; echo
echo
echo "===== 4. WHAT CADDY SERVES FOR jobhuntwow.com (forced to localhost) ====="
curl -sk --resolve jobhuntwow.com:443:127.0.0.1 https://jobhuntwow.com/api/health | head -c 200; echo
echo "--- and for the site root ---"
curl -sk --resolve jobhuntwow.com:443:127.0.0.1 https://jobhuntwow.com/ | head -c 160; echo
echo
echo "===== 5. DNS AS THE DROPLET SEES IT ====="
(getent hosts jobhuntwow.com || nslookup jobhuntwow.com 2>/dev/null | tail -4) | head -5
echo
echo "===== 6. jhw-web STATE ====="
docker ps --filter name=jhw-web --format '{{.Names}}  {{.Status}}  {{.Image}}'
docker inspect jhw-web -f 'networks: {{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null
'''

def main():
    print(f"=== diagnosing {TGT} (enter key passphrase if asked) ===", flush=True)
    r = subprocess.run(SSH + [TGT, "bash -s"], input=SCRIPT.encode(), timeout=180)
    if r.returncode:
        sys.exit(f"[X] ssh/diagnose failed (exit {r.returncode})")

if __name__ == "__main__":
    main()
