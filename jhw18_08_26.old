#!/usr/bin/env python3
"""jhw.py — THE single entry point for JobHuntWOW ("Electronic"). One verb, one command.

    python jhw.py deploy      # ship source -> BUILD ON THE DROPLET -> wire Caddy -> VERIFY   (no GitHub)
    python jhw.py status      # is the PUBLIC site our app? (asserts content, prints evidence)
    python jhw.py diagnose    # hard facts: who actually serves jobhuntwow.com right now
    python jhw.py push        # git commit + push (source control only — NOT part of deploying)
    python jhw.py secrets     # push local .env values into GitHub secrets
    python jhw.py local       # bring up the local apply stack (browser + stagehand + candidate bot)
    python jhw.py local-down  # stop it

DEPLOY DOES NOT TOUCH GITHUB. This PC scp's the build context to the droplet and the droplet
builds the image. GitHub is source control, not a deployment dependency.

You never run deploy_direct.py / sync_secrets.py / diagnose_web.py yourself — this file drives them.
"""
from __future__ import annotations
import argparse, json, os, ssl, subprocess, sys
import urllib.request, urllib.error

HERE = os.path.abspath(os.path.dirname(__file__))
AGENT = os.path.join(HERE, "agent")
sys.path.insert(0, HERE)
REPO = os.environ.get("JHW_REPO", "feranicus/jobhuntwow.com")
BRANCH = os.environ.get("JHW_BRANCH", "main")


def sh(args, cwd=HERE, check=False, cap=False):
    # encoding/errors are MANDATORY on Windows: the default cp1252 decoder crashes on any
    # non-latin1 byte in a subprocess's output (this killed a whole deploy at the verify step).
    return subprocess.run(args, cwd=cwd, check=check, capture_output=cap,
                          text=True, encoding="utf-8", errors="replace")


def out(args, cwd=HERE) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def _get(url: str, timeout=20):
    """Fetch a URL with the stdlib. Returns (status, body, error).

    Deliberately NOT curl: on Windows `curl` may be a PowerShell alias, and capturing its output
    with the default codec crashed the verifier. An empty `got:` told us nothing.
    """
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "jhw-deploy/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            # Read the WHOLE body (capped). Reading only 4 KB made the landing check fail on a
            # perfectly good deploy: the landing is 210 KB and href="/login" sits at byte 30169,
            # so the assertion could never see it. Truncated evidence is worse than none.
            return r.status, r.read(1_000_000).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        return e.code, e.read(1_000_000).decode("utf-8", "replace"), ""
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ git (source control only)
def _commit_and_push(message: str) -> bool:
    top = out(["git", "rev-parse", "--show-toplevel"]).replace("\\", "/").rstrip("/")
    if top.lower() != HERE.replace("\\", "/").rstrip("/").lower():
        sys.exit(f"[X] git root is {top}, expected {HERE}.\n"
                 "    This folder must be its own repo, or we would push the wrong project.")
    origin = out(["git", "remote", "get-url", "origin"])
    if REPO.lower() not in origin.lower():
        sys.exit(f"[X] origin is '{origin}' but this project expects '{REPO}'.")
    sh(["git", "add", "-A"])
    staged = out(["git", "diff", "--cached", "--name-only"])
    if not staged:
        print("  (no changes to commit)")
    else:
        print("  committing %d file(s)" % len(staged.splitlines()))
        sh(["git", "commit", "-m", message])
    sh(["git", "push", "-u", "origin", BRANCH])
    return bool(staged)


# ------------------------------------------------------------------ verbs
def _check_requirements() -> None:
    """Parse backend/requirements.txt the way pip does, BEFORE shipping.

    A missing space before an inline '#' made pip read the comment as a version marker and the
    droplet build died 3 minutes in. Catching it locally costs milliseconds.
    """
    import re as _re
    path = os.path.join(HERE, "backend", "requirements.txt")
    comment = _re.compile(r"(^|\s+)#.*$")
    bad = []
    for i, raw in enumerate(open(path, encoding="utf-8"), 1):
        if _re.search(r"\S#", raw):
            bad.append(f"line {i}: inline '#' needs whitespace before it -> {raw.strip()}")
        line = comment.sub("", raw).strip()
        if not line or line.startswith("-"):
            continue
        if not _re.match(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?\s*([=<>!~].*)?$", line):
            bad.append(f"line {i}: cannot parse -> {line}")
    if bad:
        sys.exit("[X] backend/requirements.txt is invalid (pip would fail on the droplet):\n  "
                 + "\n  ".join(bad))


def cmd_deploy(a):
    """ONE ssh session does everything: unpack -> build -> start -> wire Caddy -> verify.

    Never split this into several ssh/scp calls again: sshd throttles rapid repeats and Windows
    OpenSSH cannot multiplex, so the 2nd or 3rd connection hangs with no output at all.
    """
    import deploy_direct as dd
    _check_requirements()
    print("=== 1/3 ship the build context to the droplet ===", flush=True)
    dd.deploy(not a.no_caddy)
    print("=== 3/3 verify the PUBLIC site from here ===", flush=True)
    cmd_status(a)


def cmd_status(a=None):
    """Assert each URL against what it is SUPPOSED to return.

    '/' is the public landing (HTML) by design — the image serves index.html there. Demanding
    JSON at '/' is what made a working deploy report FAIL.
    """
    checks = [
        ("/api/health", lambda s, b: '"ok"' in b,                 "JSON health"),
        # assert the landing links INTO the portal. "JobHuntWOW in body" was too weak: the
        # old static one-pager matched it too, and its "Jump in" pointed at #arch.
        ("/",           lambda s, b: 'href="/login"' in b,         "landing -> /login"),
        ("/login",      lambda s, b: 'id="root"' in b or "<div id=root" in b, "SPA shell"),
    ]
    allok = True
    for path, ok, what in checks:
        st, body, err = _get("https://jobhuntwow.com" + path)
        good = (not err) and ok(st, body)
        allok &= good
        print(f"  {path:<12} [{st or '---'}] {what:<12} {'OK' if good else 'FAIL'}")
        if not good:
            if err:
                print(f"      got: {err}")
            else:
                import re as _re
                title = _re.search(r"<title>([^<]{0,80})", body)
                print(f"      got: {len(body)} bytes, title={title.group(1).strip() if title else '?'!r}")
    if allok:
        print("\n[OK] https://jobhuntwow.com is our app — landing at /, portal at /login.")
    else:
        print("\n[!] Not fully live. Next:  python jhw.py diagnose")
    return allok


def _ssh_script(script: str, timeout=300) -> int:
    """Run a script on the droplet in ONE ssh session (see CLAUDE.md: never open several)."""
    import deploy_direct as dd
    r = subprocess.run(dd.SSH + [dd.TGT, "bash -s"], input=script.encode(), timeout=timeout)
    return r.returncode


def cmd_logs(a):
    """Show what jhw-web is actually saying — the fastest way to a real error message."""
    grep = f"| grep -i -- {a.grep!r}" if a.grep else ""
    _ssh_script(f"docker logs --tail {int(a.tail)} jhw-web 2>&1 {grep} || true")


def cmd_mailcheck(a):
    """Diagnose OTP email end to end and print Google's ACTUAL refusal, not a guess."""
    import base64 as _b64, os as _os
    code = open(_os.path.join(HERE, "deploy", "mailcheck.py"), "rb").read()
    b64 = _b64.b64encode(code).decode()
    to = a.to or ""
    _ssh_script(
        "base64 -d > /tmp/mailcheck.py <<'MC_EOF'\n" + b64 + "\nMC_EOF\n"
        "docker cp /tmp/mailcheck.py jhw-web:/tmp/mailcheck.py >/dev/null 2>&1 || true\n"
        "echo '=== Gmail / OTP self-test (inside jhw-web) ==='\n"
        f"docker exec jhw-web python3 /tmp/mailcheck.py {to} || true\n"
        "echo\n"
        "echo '=== recent otp_send events ==='\n"
        "docker logs --tail 400 jhw-web 2>&1 | grep -i 'otp_send\\|auth' | tail -15 || true\n")


def cmd_mirror(a):
    """Clone EVERY cybergod dashboard in the shared Grafana into a JobHuntWOW twin (1:1,
    from the LIVE source). Runs deploy/mirror_grafana.py on the droplet in ONE ssh session."""
    import base64 as _b64, os as _os
    code = open(_os.path.join(HERE, "deploy", "mirror_grafana.py"), "rb").read()
    b64 = _b64.b64encode(code).decode()
    _ssh_script(
        "base64 -d > /tmp/mirror_grafana.py <<'MG_EOF'\n" + b64 + "\nMG_EOF\n"
        "echo '=== mirror ALL cybergod dashboards -> JobHuntWOW twins ==='\n"
        "python3 /tmp/mirror_grafana.py\n"
        "echo\n"
        "echo '=== JobHuntWOW dashboards now in Grafana ==='\n"
        "GRA=$(docker ps --format '{{.Names}}' | grep -i grafana | head -1)\n"
        "GU=$(docker exec \"$GRA\" printenv GF_SECURITY_ADMIN_USER 2>/dev/null || echo admin)\n"
        "GP=$(docker exec \"$GRA\" printenv GF_SECURITY_ADMIN_PASSWORD 2>/dev/null || echo admin)\n"
        "docker exec \"$GRA\" sh -c \"curl -s -u '$GU:$GP' http://127.0.0.1:3000/api/search?query=JobHuntWOW\" "
        "| python3 -c \"import json,sys;[print('   ',d.get('title'),'->','/observe/d/'+d.get('uid','')) for d in json.load(sys.stdin)]\" || true\n",
        timeout=300)


def cmd_chat(a):
    """Call the LIVE /api/chat and print what actually comes back.

    "The bubble just spins" is not a diagnosis. This shows the model in use, the HTTP status and
    the raw stream (or the upstream inference error) so the failure is readable.
    """
    import json as _json
    st, body, err = _get("https://jobhuntwow.com/api/models")
    if err:
        print("  /api/models failed:", err); return
    try:
        d = _json.loads(body)
        print("  default model :", d.get("default"))
        print("  catalog size  :", len(d.get("models", [])))
    except Exception:
        print("  /api/models did not return JSON:", body[:200]); return

    payload = _json.dumps({"messages": [{"role": "user", "content": a.message}]}).encode()
    req = urllib.request.Request("https://jobhuntwow.com/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    print(f"  asking Electronic: {a.message!r} ...")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            print("  HTTP", r.status)
            got = r.read(4000).decode("utf-8", "replace")
        print("  --- raw response (first 1500 chars) ---")
        print("  " + (got[:1500].replace("\n", "\n  ") or "(EMPTY - the stream produced nothing)"))
        if "\u26a0" in got or "Inference error" in got:
            print("\n  ^ the backend reported an upstream inference error (see above)")
    except urllib.error.HTTPError as e:
        print("  HTTP", e.code, e.read(1000).decode("utf-8", "replace")[:600])
    except Exception as e:
        print("  request failed:", f"{type(e).__name__}: {e}")


LOKI_Q = '{job=~".+"} |= `jhw-web`'      # backticks are LogQL's raw-string quotes


def _quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def cmd_obs(a):
    """Wire + verify Electronic's events in Grafana, and prove they reached Loki.

    Discovers the REAL promtail configs (from the bind-mount paths, not a guessed filename),
    asks Loki whether our lines are actually indexed, then installs the dashboard into whichever
    Grafana provisioning directory exists. Everything in ONE ssh session.
    """
    import base64 as _b64, os as _os
    # BOTH dashboards: app events (signups/OTP/mailer) and the ported visitors+security board.
    ddir = _os.path.join(HERE, "obs", "grafana", "dashboards")
    names = ["electronic.json", "electronic-security.json", "jobhuntwow-web.json"]
    blobs = {n: _b64.b64encode(open(_os.path.join(ddir, n), "rb").read()).decode()
             for n in names if _os.path.isfile(_os.path.join(ddir, n))}
    b64 = blobs.get("electronic.json", "")
    b64sec = blobs.get("electronic-security.json", "")
    b64web = blobs.get("jobhuntwow-web.json", "")   # cybergod webapp.json, retargeted to jhw-web
    _ssh_script("""
set -u
echo '===== 1. THE REAL PROMTAIL CONFIGS (from their bind mounts) ====='
for pt in $(docker ps --format '{{.Names}}' | grep -i promtail); do
  CFG="$(docker inspect "$pt" --format '{{range .Mounts}}{{if eq .Type "bind"}}{{if ne .Destination "/var/run/docker.sock"}}{{.Source}}{{end}}{{end}}{{end}}')"
  echo "--- $pt  config=$CFG ---"
  grep -E '__path__|job_name|job:|url:|docker_sd|container' "$CFG" 2>/dev/null | sed 's/^/    /' | head -25
done

echo
echo '===== 2. IS OUR EVENT FILE PRESENT AND GROWING? ====='
docker run --rm -v ${JHW_EVENTS_VOLUME:-colt-stack_colt_events}:/l busybox sh -c 'ls -l /l/ 2>/dev/null' || echo '  (volume missing)'
echo -n '  lines from jhw-web: '
docker run --rm -v ${JHW_EVENTS_VOLUME:-colt-stack_colt_events}:/l busybox sh -c 'grep -c jhw-web /l/events.log 2>/dev/null' || echo 0

echo
echo '===== 3. DOES LOKI HAVE OUR LINES? (ask Loki, do not assume) ====='
LOKI="$(docker ps --format '{{.Names}}' | grep -i loki | head -1)"
echo "  loki container: $LOKI"
docker exec "$LOKI" wget -qO- 'http://127.0.0.1:3100/loki/api/v1/labels' 2>/dev/null | head -c 300; echo
Q="__LOKI_QUERY__"
echo "  querying last 1h for jhw-web lines ..."
docker exec "$LOKI" wget -qO- "http://127.0.0.1:3100/loki/api/v1/query_range?query=$Q&limit=3&since=1h" 2>/dev/null \
  | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); rs=d.get('data',{}).get('result',[])
    print('  streams found:', len(rs))
    for r in rs[:2]:
        print('  labels:', r.get('stream'))
        for v in r.get('values',[])[:1]: print('  sample:', v[1][:160])
    if not rs: print('  [!] Loki has NO jhw-web lines yet — promtail is not picking them up')
except Exception as e: print('  could not parse Loki answer:', e)"

echo
echo '===== 4. INSTALL THE DASHBOARDS (Grafana HTTP API, not file provisioning) ====='
GRA="$(docker ps --format '{{.Names}}' | grep -i grafana | head -1)"
echo "  grafana container: $GRA"
base64 -d > /tmp/electronic.json <<'DASH_EOF'
__DASH_B64__
DASH_EOF
base64 -d > /tmp/electronic-security.json <<'SEC_EOF'
__SEC_B64__
SEC_EOF
base64 -d > /tmp/jobhuntwow-web.json <<'WEB_EOF'
__WEB_B64__
WEB_EOF
# Copying a file into a directory only works if a FILE PROVIDER is configured to watch it.
# The import API always works, so use the admin credentials the container already holds.
GU="$(docker exec "$GRA" printenv GF_SECURITY_ADMIN_USER 2>/dev/null || echo admin)"
GP="$(docker exec "$GRA" printenv GF_SECURITY_ADMIN_PASSWORD 2>/dev/null || echo admin)"
[ -z "$GU" ] && GU=admin
[ -z "$GP" ] && GP=admin
for f in electronic electronic-security jobhuntwow-web; do
  [ -s "/tmp/$f.json" ] || continue
  python3 - "$f" <<'PY' > "/tmp/$f.payload.json"
import json, sys
d = json.load(open("/tmp/%s.json" % sys.argv[1]))
d.pop("id", None)
json.dump({"dashboard": d, "overwrite": True, "folderId": 0,
           "message": "installed by jhw.py obs"}, sys.stdout)
PY
  docker cp "/tmp/$f.payload.json" "$GRA:/tmp/$f.payload.json" >/dev/null
  OUT="$(docker exec "$GRA" sh -c "curl -s -u '$GU:$GP' -H 'Content-Type: application/json' \
        -X POST -d @/tmp/$f.payload.json http://127.0.0.1:3000/api/dashboards/db" 2>&1)"
  echo "  $f -> $(echo "$OUT" | head -c 220)"
done
echo
echo "  If you see \"status\":\"success\" above, open godeyes.ai/observe/dashboards and look for"
echo "  'JobHuntWOW - Electronic'. An \"Unauthorized\" means the admin password differs from the"
echo "  container env - set GF_ADMIN_PASS and re-run."
""".replace("__DASH_B64__", b64).replace("__SEC_B64__", b64sec).replace("__WEB_B64__", b64web)
       .replace("__LOKI_QUERY__", _quote(LOKI_Q)), timeout=420)


def cmd_diagnose(a):
    import diagnose_web
    diagnose_web.main()


def cmd_push(a):
    print("=== git: commit + push (source control only) ===", flush=True)
    _commit_and_push(a.message)
    print("  pushed.")


def cmd_secrets(a):
    import sync_secrets
    argv = ["sync_secrets.py"]
    if a.ssh_key:    argv += ["--ssh-key", a.ssh_key]
    if a.ts_authkey: argv += ["--ts-authkey", a.ts_authkey]
    if a.dry_run:    argv += ["--dry-run"]
    old, sys.argv = sys.argv, argv
    try:
        sync_secrets.main()
    finally:
        sys.argv = old


def _agent(verb, *args):
    subprocess.run([sys.executable, os.path.join(AGENT, "jhw.py"), verb, *args], cwd=AGENT)


def main():
    p = argparse.ArgumentParser(description="JobHuntWOW — one command per job")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="ship source -> build on droplet -> wire Caddy -> verify")
    d.add_argument("--no-caddy", action="store_true")
    d.set_defaults(fn=cmd_deploy)

    sub.add_parser("diagnose", help="who serves jobhuntwow.com right now").set_defaults(fn=cmd_diagnose)

    lg = sub.add_parser("logs", help="tail jhw-web's logs")
    lg.add_argument("--tail", default=120)
    lg.add_argument("--grep", default="")
    lg.set_defaults(fn=cmd_logs)

    pr = sub.add_parser("probe", help="measure which DO model drives a browser fastest + correctly")
    pr.add_argument("--models", default="")
    pr.add_argument("--runs", default="2")
    pr.set_defaults(fn=lambda a: subprocess.run(
        [sys.executable, os.path.join(HERE, "probe_models.py"),
         *(["--models", a.models] if a.models else []), "--runs", str(a.runs)], cwd=HERE))

    ch = sub.add_parser("chat", help="test the live Electronic chat end to end")
    ch.add_argument("message", nargs="?", default="Say OK if you can hear me.")
    ch.set_defaults(fn=cmd_chat)

    sub.add_parser("obs", help="wire + verify Electronic events in Grafana").set_defaults(fn=cmd_obs)
    sub.add_parser("mirror", help="clone ALL cybergod Grafana dashboards for jobhuntwow (1:1)").set_defaults(fn=cmd_mirror)

    mc = sub.add_parser("mailcheck", help="why is the OTP email failing? (asks Google)")
    mc.add_argument("--to", default="", help="send a test message to this address")
    mc.set_defaults(fn=cmd_mailcheck)
    sub.add_parser("status", help="is the public site our app").set_defaults(fn=cmd_status)

    g = sub.add_parser("push", help="git commit + push (NOT needed to deploy)")
    g.add_argument("-m", "--message", default="update")
    g.set_defaults(fn=cmd_push)

    s = sub.add_parser("secrets", help="push local .env values into GitHub secrets")
    s.add_argument("--ssh-key", default="")
    s.add_argument("--ts-authkey", default="")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_secrets)

    ap = sub.add_parser("apply", help="drive a real application in the local sandboxed browser")
    ap.add_argument("job", help="job URL")
    ap.set_defaults(fn=lambda a: _agent("apply", a.job))

    ll = sub.add_parser("local-logs", help="logs from the unified sandbox (browser/stagehand/agent/bot)")
    ll.add_argument("service", nargs="?", default="jhw-stagehand")
    ll.add_argument("--tail", default="60")
    ll.set_defaults(fn=lambda a: subprocess.run(
        ["docker", "compose", "-f", "docker-compose.local.yml", "logs",
         "--tail", str(a.tail), a.service], cwd=AGENT))

    sub.add_parser("local").set_defaults(fn=lambda a: _agent("local"))
    sub.add_parser("local-down").set_defaults(fn=lambda a: _agent("local-down"))

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
