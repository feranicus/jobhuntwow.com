#!/usr/bin/env python3
"""deploy_direct.py — ship JobHuntWOW ("Electronic") to the droplet FROM THIS MACHINE over SSH.

Why this exists: your key already works from this computer, but GitHub's runners cannot necessarily
reach the droplet's :22 (that is exactly why cybergod needs Tailscale). So CI builds the image and
pushes it to GHCR; THIS script pulls and starts it on the droplet. We never build on the droplet.

  python deploy_direct.py              # ship + start + wire Caddy + verify
  python deploy_direct.py --no-caddy   # start only, leave the vhost alone

Env (all optional): DROPLET_HOST (default 64.225.108.200) · DROPLET_USER (root) ·
                    SSH_KEY (path, else your default ~/.ssh key) · GHCR_TOKEN (only if the
                    GHCR package is private) · GHCR_USER
"""
from __future__ import annotations
import argparse, os, subprocess, sys, tarfile, tempfile

HERE = os.path.abspath(os.path.dirname(__file__))
HOST = os.environ.get("DROPLET_HOST", "64.225.108.200")
USER = os.environ.get("DROPLET_USER", "root")
def _find_key() -> str:
    """Auto-detect the droplet key so PowerShell users need no $env:SSH_KEY.
    Windows OpenSSH does not read ~/.ssh the way WSL does, so we pass -i explicitly."""
    k = os.environ.get("SSH_KEY", "")
    if k and os.path.exists(k):
        return k
    home = os.path.expanduser("~")
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        cand = os.path.join(home, ".ssh", name)
        if os.path.exists(cand):
            return cand
    return ""


KEY  = _find_key()
TGT  = f"{USER}@{HOST}"
APP  = "/opt/jobhuntwow"
PROJ = "jobhuntwow"                       # isolated compose project: never touches colt-stack/videodead
SHIP = ["docker-compose.web.yml", "deploy/caddy/jobhuntwow.caddy"]

# NOTE: no BatchMode here on purpose. The droplet key is passphrase-protected, and BatchMode
# forbids the prompt -> "Permission denied (publickey,password)". ControlMaster keeps ONE
# authenticated connection alive, so you type the passphrase once and every later ssh/scp reuses it.
_TMO = ["-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
if os.name != "nt":                      # Windows OpenSSH does not support connection multiplexing
    _TMO += ["-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/cm-jhw-%r@%h:%p",
             "-o", "ControlPersist=10m"]
SSH = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"] + _TMO
SCP = ["scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"] + _TMO
if KEY and os.path.exists(KEY):
    SSH += ["-i", KEY]; SCP += ["-i", KEY]


def run(cmd, **kw):
    print("  $ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""), flush=True)
    return subprocess.run(cmd, **kw)


def preflight():
    print(f"=== 1/5 preflight: ssh {TGT} ===", flush=True)
    # HARD timeout: ConnectTimeout only caps the TCP connect, so a stall during auth would hang forever.
    try:
        # not captured: the passphrase prompt must be visible and typable
        print("  (if your key has a passphrase, enter it once — the connection is then reused)", flush=True)
        r = subprocess.run(SSH + [TGT, "echo ssh-ok && docker ps --format '{{.Names}}' | head -6"],
                           text=True, timeout=180)
    except subprocess.TimeoutExpired:
        sys.exit(
            f"[X] ssh to {TGT} TIMED OUT after 25s — no response.\n\n"
            "    Most likely: this shell has no usable key. Your working SSH is probably in WSL.\n"
            "      * From WSL:      cd /mnt/c/Python\\ SW/Linkedin\\ Scraper/jobhuntwow-app && python3 deploy_direct.py\n"
            "      * Or point at the key explicitly (PowerShell):\n"
            "          $env:SSH_KEY=\"C:\\Users\\feran\\.ssh\\id_ed25519\"; python deploy_direct.py\n"
            "      * Or the droplet is not answering :22 from this network. Test:  ssh -v " + TGT + " \"echo ok\"\n")
    if r.returncode:
        sys.exit(f"[X] cannot SSH to {TGT} (exit {r.returncode})\n\n"
                 "    Passphrase-protected key? Load it ONCE so it stops asking:\n"
                 "      PowerShell:  Start-Service ssh-agent; ssh-add $env:USERPROFILE\\.ssh\\id_ed25519\n"
                 "      WSL/bash  :  eval $(ssh-agent -s) && ssh-add ~/.ssh/id_ed25519\n\n"
                 f"    Test it directly:  ssh -v {TGT} \"echo ok\"\n"
                 "    PowerShell uses C:\\Users\\<you>\\.ssh, WSL uses ~/.ssh — or set SSH_KEY=<path>.")
    print("  ssh OK", flush=True)


def runtime_env() -> str:
    """Reuse the same merge sync_secrets.py does, so there is ONE definition of the runtime env."""
    sys.path.insert(0, HERE)
    try:
        import sync_secrets
        merged, _ = sync_secrets.collect()
    except Exception as e:
        sys.exit(f"[X] could not build the runtime env from your local .env files: {e}")
    missing = [k for k in ("DO_INFERENCE_KEY", "GMAIL_SENDER", "GMAIL_SA_B64") if not merged.get(k)]
    if missing:
        print("  [!] missing (OTP mail / LLM may not work): " + ", ".join(missing), flush=True)
    print("  runtime env keys: " + " ".join(sorted(merged)), flush=True)
    return "\n".join(f"{k}={merged[k]}" for k in sorted(merged)) + "\n"


def ship_files():
    print("=== 2/5 ship compose + caddy snippet ===", flush=True)
    tf = tempfile.NamedTemporaryFile(suffix=".tgz", delete=False); tf.close()
    with tarfile.open(tf.name, "w:gz") as tar:
        for item in SHIP:
            p = os.path.join(HERE, item)
            if not os.path.exists(p):
                sys.exit(f"[X] missing {item} — run this from the repo root.")
            tar.add(p, arcname=item)
    if run(SSH + [TGT, f"mkdir -p {APP}"]).returncode:
        sys.exit("[X] mkdir failed")
    if run(SCP + [tf.name, f"{TGT}:{APP}/ship.tgz"]).returncode:
        sys.exit("[X] scp failed")
    os.unlink(tf.name)
    if run(SSH + [TGT, f"cd {APP} && tar xzf ship.tgz && rm -f ship.tgz"]).returncode:
        sys.exit("[X] untar failed")


def write_env(body: str):
    print("=== 3/5 write runtime .env (over stdin — never in argv/logs) ===", flush=True)
    p = subprocess.run(SSH + [TGT, f"cat > {APP}/.env && chmod 600 {APP}/.env"],
                       input=body.encode(), capture_output=True)
    if p.returncode:
        sys.exit(f"[X] writing .env failed: {p.stderr.decode()[:200]}")
    print("  .env written (chmod 600)", flush=True)


def remote_script(with_caddy: bool) -> str:
    tok, usr = os.environ.get("GHCR_TOKEN", ""), os.environ.get("GHCR_USER", "feranicus")
    lines = ["set -e", f"cd {APP}"]
    if tok:
        lines.append(f"echo '{tok}' | docker login ghcr.io -u {usr} --password-stdin")
    lines += [
        "echo '== pull + (re)start jhw-web (NO --remove-orphans: shared host) =='",
        f"docker compose -p {PROJ} -f docker-compose.web.yml pull",
        f"docker compose -p {PROJ} -f docker-compose.web.yml up -d --force-recreate",
        "echo -n 'jhw-web image : '; docker inspect jhw-web -f '{{.Config.Image}}'",
        "echo -n 'jhw-web nets  : '; docker inspect jhw-web -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'; echo",
    ]
    if with_caddy:
        lines += [
            "echo '== wire jobhuntwow.com into the shared Caddy (marker-scoped) =='",
            "CADDY_CT=\"$(docker ps --format '{{.Names}}' | grep -i caddy | head -1)\"",
            "CF=\"$(docker inspect \"$CADDY_CT\" --format '{{range .Mounts}}{{if eq .Destination \"/etc/caddy/Caddyfile\"}}{{.Source}}{{end}}{{end}}')\"",
            "cp \"$CF\" \"$CF.bak.$(date +%s)\"",
            # marker-scoped ONLY. A broad /jobhuntwow/,/^}/d would nuke the app.jobhuntwow.com vhost.
            # The OLD static one-pager block claimed the SAME hostname and, being first, WON.
            # Remove it so our container owns jobhuntwow.com (it now serves that landing itself).
            "sed -i '/# >>> jobhuntwow.com one-pager/,/# <<< jobhuntwow.com one-pager/d' \"$CF\"",
            "sed -i '/# jhw:jobhuntwow BEGIN/,/# jhw:jobhuntwow END/d' \"$CF\"",
            "cat deploy/caddy/jobhuntwow.caddy >> \"$CF\"",
            "docker exec \"$CADDY_CT\" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile",
            "docker exec \"$CADDY_CT\" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile || docker restart \"$CADDY_CT\"",
            "sleep 4",
            "echo -n 'caddy->jhw-web: '; docker exec \"$CADDY_CT\" wget -qO- -T5 http://jhw-web:8000/api/health 2>&1 | head -c 80; echo",
        ]
    lines += [
        "echo '== verify =='",
        # Assert CONTENT, not just the status code: the old GitHub-Pages landing also returns 200,
        # which made an earlier deploy look "LIVE" when the domain was still served by Pages.
        "B=\"$(curl -sk --resolve jobhuntwow.com:443:127.0.0.1 https://jobhuntwow.com/api/health || true)\"",
        "case \"$B\" in *'\"ok\"'*) echo 'caddy serves OUR app  = YES' ;; "
        "*) echo 'caddy serves OUR app  = NO -> got HTML/other, not our JSON' ;; esac",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-caddy", action="store_true", help="start the app but do not touch the vhost")
    a = ap.parse_args()

    preflight()
    body = runtime_env()
    ship_files()
    write_env(body)

    print("=== 4/5 start on the droplet ===", flush=True)
    p = subprocess.run(SSH + [TGT, "bash -s"], input=remote_script(not a.no_caddy).encode())
    if p.returncode:
        sys.exit("[X] remote deploy failed (see output above)")

    print("=== 5/5 verify from here ===", flush=True)
    r = subprocess.run(["curl", "-s", "https://jobhuntwow.com/api/health"],
                       capture_output=True, text=True)
    body = (r.stdout or "")[:200]
    if '"ok"' in body:
        print("  PUBLIC https://jobhuntwow.com -> OUR APP  ✅")
        print("\n[OK] live.")
    else:
        print("  PUBLIC https://jobhuntwow.com -> NOT our app ❌")
        print("  got: " + body.replace("\n", " ")[:120])
        print("\n[!] The droplet is serving the app correctly (see 'caddy serves OUR app' above),")
        print("    but the PUBLIC domain resolves somewhere else — almost always leftover")
        print("    GitHub-Pages A-records (185.199.108-111.153) and/or Pages still claiming the")
        print("    custom domain. Fix in GoDaddy + repo Settings->Pages, then re-run.")


if __name__ == "__main__":
    main()
