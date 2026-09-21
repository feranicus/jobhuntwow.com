#!/usr/bin/env python3
"""deploy_direct.py — ship + BUILD + start JobHuntWOW ("Electronic") on the droplet, FROM THIS PC.

NO GitHub. NO GHCR. NO CI. This machine scp's the build context to /opt/jobhuntwow and the
droplet builds the image itself (`docker compose build`), exactly like cybergod's direct deploy.
Reason: the GitHub round-trip added a 2-minute wait, a second source of truth and a failure mode
that had nothing to do with the app.

Driven by `python ship.py` — you do not run this file yourself. (There is no `jhw.py deploy`
subcommand; that name appeared in the docs and never in the argparse.)

Env (all optional): DROPLET_HOST (default 64.225.108.200) · DROPLET_USER (root) · SSH_KEY (path)
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

# The BUILD CONTEXT the droplet needs to build Dockerfile.web, plus the compose + vhost files.
SHIP_FILES = ["docker-compose.web.yml", "Dockerfile.web", "index.html",
              "deploy/caddy/jobhuntwow.caddy", "deploy/fix_caddy.py",
              "deploy/whoserves.py"]
SHIP_DIRS  = ["backend", "frontend"]
# Never ship build junk / secrets. .env is written separately over stdin.
SKIP_DIRS  = {"node_modules", "dist", "__pycache__", ".git", ".vite", "ssrtmp", "out"}
SKIP_EXT   = {".pyc", ".pyo"}

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
    try:
        # not captured: the passphrase prompt must be visible and typable
        print("  (if your key has a passphrase, enter it once — the connection is then reused)", flush=True)
        r = subprocess.run(SSH + [TGT, "echo ssh-ok && docker ps --format '{{.Names}}' | head -6"],
                           text=True, timeout=180)
    except subprocess.TimeoutExpired:
        sys.exit(f"[X] ssh to {TGT} TIMED OUT — no response.\n"
                 "    Load the key once:  Start-Service ssh-agent; ssh-add $env:USERPROFILE\\.ssh\\id_ed25519\n"
                 f"    Test it directly :  ssh -v {TGT} \"echo ok\"\n")
    if r.returncode:
        sys.exit(f"[X] cannot SSH to {TGT} (exit {r.returncode})\n\n"
                 "    Passphrase-protected key? Load it ONCE so it stops asking:\n"
                 "      PowerShell:  Start-Service ssh-agent; ssh-add $env:USERPROFILE\\.ssh\\id_ed25519\n"
                 "      WSL/bash  :  eval $(ssh-agent -s) && ssh-add ~/.ssh/id_ed25519\n\n"
                 f"    Test it directly:  ssh -v {TGT} \"echo ok\"")
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
    # JHW_EXTRA_ENV: comma-separated K=V pairs appended to the runtime env for THIS deploy only.
    # It exists so the staging gate can hand the twin ALERT_DELIVERY=0 without a second .env, a
    # second compose file or a manual step on the droplet - a twin should never page the operator
    # with production-shaped alerts, and the gate deliberately fires real probe paths at it.
    # Never used for secrets: these values travel in the same stdin payload the rest of the env
    # does, but they are chosen by the caller, not read from a store.
    for pair in (os.environ.get("JHW_EXTRA_ENV") or "").split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _s, v = pair.partition("=")
            merged[k.strip()] = v
            print("  runtime env override: %s=%s" % (k.strip(), v), flush=True)
    print("  runtime env keys: " + " ".join(sorted(merged)), flush=True)
    return "\n".join(f"{k}={merged[k]}" for k in sorted(merged)) + "\n"


def _filter(ti: tarfile.TarInfo):
    parts = set(ti.name.replace("\\", "/").split("/"))
    if parts & SKIP_DIRS:
        return None
    if os.path.splitext(ti.name)[1] in SKIP_EXT:
        return None
    if os.path.basename(ti.name) in (".env",) or ti.name.endswith(".env"):
        return None                      # secrets go over stdin, never into a tarball
    return ti


def build_payload() -> str:
    """Tar the build context IN MEMORY and return it base64'd. Nothing is written to disk."""
    import base64, io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in SHIP_FILES + SHIP_DIRS:
            p = os.path.join(HERE, item)
            if not os.path.exists(p):
                sys.exit(f"[X] missing {item} — run this from the repo root.")
            tar.add(p, arcname=item, filter=_filter)
        n = len(tar.getnames())
    raw = buf.getvalue()
    print(f"  build context: {n} files, {len(raw)/1024:.0f} KB", flush=True)
    return base64.b64encode(raw).decode("ascii")


def remote_script(with_caddy: bool) -> str:
    lines = ["set -e", f"cd {APP}",
             # jhw-web runs as uid 10001 (non-root, by design) but the shared events volume is
             # root-owned, so our writes would fail SILENTLY (log() swallows IO errors). Create the
             # file as root once and hand it to our uid. Idempotent.
             "echo '== ensure the SHARED events.log is appendable by jhw-web (uid 10001) =='",
             "docker rm -f jhw-promtail >/dev/null 2>&1 || true",
             "docker volume inspect ${JHW_EVENTS_VOLUME:-colt-stack_colt_events} >/dev/null 2>&1 "
             "&& docker run --rm -v ${JHW_EVENTS_VOLUME:-colt-stack_colt_events}:/l busybox sh -c "
             "'touch /l/events.log && chown root:10001 /l/events.log && chmod 0664 /l/events.log && ls -l /l/events.log' "
             "|| echo '  [!] shared events volume not found - events still go to stdout'",
             "echo '== BUILD the image on the droplet (no registry, no CI) =='",
             f"docker compose -p {PROJ} -f docker-compose.web.yml build",
             "echo '== (re)start jhw-web (NO --remove-orphans: shared host) =='",
             f"docker compose -p {PROJ} -f docker-compose.web.yml up -d --force-recreate",
             "echo -n 'jhw-web image : '; docker inspect jhw-web -f '{{.Config.Image}}'",
             "echo -n 'jhw-web nets  : '; docker inspect jhw-web -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'; echo",
             "echo '== wait for the container to answer =='",
             "for i in $(seq 1 30); do docker exec jhw-web curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break; sleep 2; done",
             # THE SHARED EVENTS FILE MUST BE WRITABLE BY UID 10001. cybergod's containers create
             # /var/log/colt/events.log as root 0644; this container runs as `jhw` (Dockerfile.web),
             # so every append silently failed and jobhuntwow never wrote a line to the file promtail
             # tails. Any container on the volume could already write it, so a+w widens nothing.
             "echo '== events.log: writable by the jhw user, PROVEN by writing one line =='",
             "docker exec -u root jhw-web sh -c 'touch /var/log/colt/events.log && chmod a+w /var/log/colt/events.log'",
             # AND IT IS A GATE, NOT A NOTE. This printed a scary marker and exited 0, so a deploy
             # whose event pipeline was dead still said DONE -- and a log pipeline that has never
             # been observed working is off. `exit 1` here fails the ssh payload and the ship.
             "docker exec jhw-web sh -c 'echo \"{\\\"evt\\\": \\\"deploy_probe\\\", \\\"service\\\": \\\"jhw-web\\\"}\" >> /var/log/colt/events.log' && echo 'events.log: jhw can write' || { echo 'EVENTS_LOG_UNWRITABLE: jhw still cannot append to /var/log/colt/events.log'; exit 1; }",
             ]
    if with_caddy:
        lines += [
            "echo '== make jobhuntwow.com belong to jhw-web (and nothing else) =='",
            "CADDY_CT=\"$(docker ps --format '{{.Names}}' | grep -i caddy | head -1)\"",
            "CF=\"$(docker inspect \"$CADDY_CT\" --format '{{range .Mounts}}{{if eq .Destination \"/etc/caddy/Caddyfile\"}}{{.Source}}{{end}}{{end}}')\"",
            # The old one-pager block claims the host through {$DOMAIN}, so it has NO comment
            # markers to sed on. fix_caddy.py PARSES the file and removes any non-ours block that
            # claims our hostnames -- other vhosts are never touched.
            "CENV=\"$(docker inspect \"$CADDY_CT\" --format '{{range .Config.Env}}{{println .}}{{end}}' | tr '\\n' ';')\"",
            "python3 deploy/fix_caddy.py --caddyfile \"$CF\" --snippet deploy/caddy/jobhuntwow.caddy --env \"$CENV\"",
            # THE BUG THAT COST SIX DEPLOYS: /etc/caddy/Caddyfile is a bind-mounted FILE. An
            # earlier deploy used `sed -i`, which writes a temp file and RENAMES it -> new inode.
            # The bind mount still points at the OLD inode, so every later edit to the host file
            # was invisible to Caddy, and `caddy validate` happily validated the stale copy.
            # Compare the two by hash and re-resolve the mount (restart) when they differ.
            "HOST_SUM=\"$(sha256sum \"$CF\" | cut -d\" \" -f1)\"",
            "CT_SUM=\"$(docker exec \"$CADDY_CT\" sha256sum /etc/caddy/Caddyfile | cut -d\" \" -f1)\"",
            "if [ \"$HOST_SUM\" != \"$CT_SUM\" ]; then",
            "  echo '  [!] caddy is reading a STALE copy of the Caddyfile (bind-mount inode was replaced)'",
            "  echo \"      host file : $HOST_SUM\"",
            "  echo \"      in caddy  : $CT_SUM\"",
            "  echo '  -> restarting caddy so the bind mount re-resolves the path (a few seconds of blip for ALL vhosts)'",
            "  docker restart \"$CADDY_CT\" >/dev/null",
            "  for i in $(seq 1 20); do docker exec \"$CADDY_CT\" true 2>/dev/null && break; sleep 1; done",
            "  sleep 3",
            "  CT_SUM2=\"$(docker exec \"$CADDY_CT\" sha256sum /etc/caddy/Caddyfile | cut -d\" \" -f1)\"",
            "  if [ \"$HOST_SUM\" = \"$CT_SUM2\" ]; then echo '  caddy now reads the CURRENT file   OK'; else echo \"  [X] STILL STALE ($CT_SUM2) — the mount source is not $CF\"; fi",
            "else",
            "  echo '  caddy already reads the current file'",
            "fi",
            "docker exec \"$CADDY_CT\" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile",
            "docker exec \"$CADDY_CT\" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile || docker restart \"$CADDY_CT\"",
            "sleep 4",
        ]
    lines += [
        "echo '== verify (each URL asserted for its OWN expected content) =='",
        "R='--resolve jobhuntwow.com:443:127.0.0.1'",
        # DECISIVE TEST: tag the request and look for the tag in OUR container's access log.
        # Content matching alone fooled us once already -- the static one-pager was a byte-copy
        # of our landing page, so '/ -> landing OK' passed while the app was never reached.
        "PROBE=\"jhwprobe$(date +%s)\"",
        "H=\"$(curl -sk $R \"https://jobhuntwow.com/api/health?$PROBE=1\" || true)\"",
        "L=\"$(curl -sk $R https://jobhuntwow.com/ || true)\"",
        "P=\"$(curl -sk $R https://jobhuntwow.com/login || true)\"",
        "sleep 1",
        "OK=1",
        "if docker logs --since 120s jhw-web 2>&1 | grep -q \"$PROBE\"; then echo '  public traffic REACHES jhw-web   OK'; else OK=0; echo '  public traffic NEVER reached jhw-web   FAIL (something else owns the vhost)'; fi",
        "case \"$H\" in *'\"ok\"'*) echo '  /api/health -> JSON              OK' ;; *) OK=0; echo '  /api/health -> NOT JSON          FAIL'; echo \"      got: $(echo \"$H\" | head -c 90)\" ;; esac",
        "case \"$L\" in *'href=\"/login\"'*) echo '  /          -> landing links to /login   OK' ;; *) OK=0; echo '  /          -> landing MISSING /login  FAIL' ;; esac",
        # a stale browser cache made a correct deploy look broken for a day: assert the header
        # -D - (GET, headers to stdout), NOT -I (HEAD): the SPA route serves GET, and a HEAD
        # 405 made this assert fail on a deploy whose header was actually correct.
        "CC=\"$(curl -sk -o /dev/null -D - $R https://jobhuntwow.com/ | tr -d '\\r' | grep -i '^cache-control:' || true)\"",
        "case \"$CC\" in *no-cache*) echo '  /          -> no-cache header          OK' ;; *) OK=0; echo \"  /          -> MISSING no-cache ($CC)  FAIL\" ;; esac",
        "case \"$P\" in *'id=\"root\"'*) echo '  /login     -> SPA shell          OK' ;; *) OK=0; echo '  /login     -> NOT the SPA        FAIL'; echo \"      got: $(echo \"$P\" | head -c 90)\" ;; esac",
        "if [ \"$OK\" = 0 ]; then",
        "  echo '--- EVIDENCE: every site address Caddy has ---'",
        "  grep -nE '^[^[:space:]#].*\\{[[:space:]]*$' \"$CF\" || true",
        "  echo '--- EVIDENCE: response headers for /api/health ---'",
        "  curl -sk -o /dev/null -D - $R https://jobhuntwow.com/api/health | head -12 || true",
        "  echo '--- EVIDENCE: last 20 lines of jhw-web log ---'",
        "  docker logs --tail 20 jhw-web 2>&1 || true",
        "fi",
        "",
    ]
    return "\n".join(lines)


def full_script(b64: str, env_body: str, with_caddy: bool) -> str:
    """ONE stdin payload for ONE ssh session.

    HARD RULE (CLAUDE.md): sshd throttles rapid repeat connections and Windows OpenSSH cannot
    multiplex, so a deploy that opens several ssh/scp sessions STALLS on the 2nd or 3rd one with
    no output. Everything therefore travels over a single `ssh <host> bash -s`.
    Secrets ride on stdin (never argv, never a temp file on disk).
    """
    return "\n".join([
        "set -e",
        f"mkdir -p {APP}",
        f"cd {APP}",
        "echo '== unpack the build context =='",
        "base64 -d > ship.tgz <<'JHW_TGZ_EOF'",
        b64,
        "JHW_TGZ_EOF",
        # wipe old source so a DELETED file cannot linger in the build context; .env survives
        "rm -rf backend frontend Dockerfile.web index.html deploy",
        "tar xzf ship.tgz && rm -f ship.tgz",
        "echo '== write runtime .env (chmod 600) =='",
        "cat > .env <<'JHW_ENV_EOF'",
        env_body.rstrip("\n"),
        "JHW_ENV_EOF",
        "chmod 600 .env",
        remote_script(with_caddy),
    ])


def deploy(with_caddy: bool = True):
    # NO preflight ssh: that was a SECOND connection, and a second connection is exactly what
    # hangs. Build everything locally first, then open ONE session that does the whole job.
    env_body = runtime_env()
    b64 = build_payload()
    print("=== 2/3 ONE ssh session: unpack -> build -> start -> Caddy -> verify ===", flush=True)
    print("    (the docker build takes ~2-4 min; output appears as it happens)", flush=True)
    try:
        r = subprocess.run(SSH + [TGT, "bash -s"],
                           input=full_script(b64, env_body, with_caddy).encode(),
                           timeout=1800)
    except subprocess.TimeoutExpired:
        sys.exit("[X] the droplet did not finish within 30 min — run `python jhw.py diagnose`.")
    if r.returncode == 255:
        sys.exit(f"[X] SSH could not connect to {TGT}.\n"
                 "    Load your key once so it stops prompting:\n"
                 "      Start-Service ssh-agent; ssh-add $env:USERPROFILE\\.ssh\\id_ed25519\n"
                 f"    Then test:  ssh {TGT} \"echo ok\"")
    if r.returncode:
        sys.exit(f"[X] remote deploy failed (exit {r.returncode}) — see the output above.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-caddy", action="store_true", help="start the app but do not touch the vhost")
    a = ap.parse_args()
    deploy(not a.no_caddy)
    # NOTHING ABOUT THE LOCAL SANDBOX BELONGS HERE. This used to call `jhw.cmd_status`, which
    # reports the LOCAL apply stack (Chrome + noVNC containers for Workday/Ashby automation) — a
    # different product from the website, and as of 2026-09-18 that verb requires a running Docker
    # daemon. So a WEBSITE deploy, which builds entirely on the droplet over ONE ssh session, would
    # have ended by demanding Docker Desktop on a machine that needs neither. The deploy already
    # verified the real subject inside that session (a tagged /api/health probe read back out of
    # jhw-web's own access log), and `python ship.py` verifies it again in phase 5.
    print("\n=== 3/3 the site is deployed and was verified in the session above ===")
    print("    https://jobhuntwow.com/  ·  /tailor  ·  /pipeline  ·  /api/health")


if __name__ == "__main__":
    main()
