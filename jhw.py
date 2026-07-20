#!/usr/bin/env python3
"""jhw.py — THE single entry point for JobHuntWOW ("Electronic"). One verb, one command.

    python jhw.py deploy      # commit+push -> build image in CI -> ship to the droplet -> wire Caddy -> VERIFY
    python jhw.py diagnose    # hard facts: who actually serves jobhuntwow.com right now
    python jhw.py secrets     # push local .env values into GitHub secrets (reuses cybergod's values)
    python jhw.py status      # is the PUBLIC site our app? (checks content, not the status code)
    python jhw.py local       # bring up the local apply stack (browser + stagehand + candidate bot)
    python jhw.py local-down  # stop it

You never run ship_web.py / deploy_direct.py / sync_secrets.py / diagnose_web.py yourself —
this file drives them. (CLAUDE.md HARD RULE: one command, no lists of scripts.)
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time

HERE = os.path.abspath(os.path.dirname(__file__))
AGENT = os.path.join(HERE, "agent")
sys.path.insert(0, HERE)
REPO = os.environ.get("JHW_REPO", "feranicus/jobhuntwow.com")
BRANCH = os.environ.get("JHW_BRANCH", "main")
WORKFLOW = "web-deploy.yml"


def sh(args, cwd=HERE, check=False, cap=False):
    return subprocess.run(args, cwd=cwd, check=check,
                          capture_output=cap, text=True)


def out(args, cwd=HERE) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return (r.stdout or "").strip()


# ------------------------------------------------------------------ git + CI
def _commit_and_push(message: str) -> bool:
    """Returns True if a new commit was pushed (i.e. CI has something new to build)."""
    top = out(["git", "rev-parse", "--show-toplevel"]).replace("\\", "/").rstrip("/")
    if top.lower() != HERE.replace("\\", "/").rstrip("/").lower():
        sys.exit(f"[X] git root is {top}, expected {HERE}.\n"
                 "    This folder must be its own repo, or we would deploy the wrong project.")
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
    before = out(["git", "rev-parse", "HEAD"])
    sh(["git", "push", "-u", "origin", BRANCH])
    return bool(staged) or before != out(["git", "rev-parse", f"origin/{BRANCH}"])


def _build_in_ci() -> None:
    """Trigger the workflow and wait for the BUILD job only.

    The deploy job in CI is expected to fail/skip (GitHub runners cannot reach the firewalled
    droplet) — we deploy over SSH from here instead. Only the image build matters.
    """
    if subprocess.run(["gh", "--version"], capture_output=True).returncode:
        sys.exit("[X] GitHub CLI ('gh') not found — install it and run `gh auth login`.")
    print("  triggering the image build ...")
    sh(["gh", "workflow", "run", WORKFLOW, "--ref", BRANCH])
    time.sleep(6)
    rid = out(["gh", "run", "list", "--workflow", WORKFLOW, "--limit", "1",
               "--json", "databaseId", "--jq", ".[0].databaseId"])
    if not rid:
        sys.exit("[X] no workflow run found — check the Actions tab.")
    print(f"  run {rid}: waiting for the BUILD job ...")
    for _ in range(80):                              # ~10 min cap
        raw = out(["gh", "run", "view", rid, "--json", "jobs"])
        try:
            jobs = json.loads(raw).get("jobs", [])
        except Exception:
            jobs = []
        build = next((j for j in jobs if j.get("name", "").lower().startswith("build")), None)
        if build and build.get("conclusion"):
            c = build["conclusion"]
            if c != "success":
                sys.exit(f"[X] the image BUILD failed ({c}).  gh run view {rid} --log-failed")
            print("  image build: success")
            return
        time.sleep(8)
    sys.exit(f"[X] timed out waiting for the build.  gh run view {rid}")


# ------------------------------------------------------------------ verbs
def cmd_deploy(a):
    import deploy_direct as dd
    print("=== 1/4 git: commit + push ===", flush=True)
    changed = _commit_and_push(a.message)
    print("=== 2/4 CI: build the image ===", flush=True)
    if changed or a.force_build:
        _build_in_ci()
    else:
        print("  nothing new pushed -> reusing the image already in GHCR (use --force-build to rebuild)")
    print("=== 3/4 droplet: ship + start + wire Caddy ===", flush=True)
    dd.preflight()
    body = dd.runtime_env()
    dd.ship_files()
    dd.write_env(body)
    r = subprocess.run(dd.SSH + [dd.TGT, "bash -s"],
                       input=dd.remote_script(not a.no_caddy).encode())
    if r.returncode:
        sys.exit("[X] remote deploy failed (see output above)")
    print("=== 4/4 verify (content, not status code) ===", flush=True)
    cmd_status(a)


def cmd_status(a=None):
    r = subprocess.run(["curl", "-s", "https://jobhuntwow.com/api/health"],
                       capture_output=True, text=True)
    body = (r.stdout or "")
    if '"ok"' in body:
        print("  https://jobhuntwow.com  ->  OUR APP  [OK]")
    else:
        print("  https://jobhuntwow.com  ->  NOT our app  [FAIL]")
        print("  got: " + body.replace("\n", " ")[:120])
        print("  run:  python jhw.py diagnose")


def cmd_diagnose(a):
    import diagnose_web
    diagnose_web.main()


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


def _agent(verb):
    subprocess.run([sys.executable, os.path.join(AGENT, "jhw.py"), verb], cwd=AGENT)


def main():
    p = argparse.ArgumentParser(description="JobHuntWOW — one command per job")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="commit+push -> build -> ship -> verify")
    d.add_argument("-m", "--message", default="deploy")
    d.add_argument("--no-caddy", action="store_true")
    d.add_argument("--force-build", action="store_true")
    d.set_defaults(fn=cmd_deploy)

    sub.add_parser("diagnose", help="who serves jobhuntwow.com right now").set_defaults(fn=cmd_diagnose)
    sub.add_parser("status", help="is the public site our app").set_defaults(fn=cmd_status)

    s = sub.add_parser("secrets", help="push local .env values into GitHub secrets")
    s.add_argument("--ssh-key", default="")
    s.add_argument("--ts-authkey", default="")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_secrets)

    sub.add_parser("local").set_defaults(fn=lambda a: _agent("local"))
    sub.add_parser("local-down").set_defaults(fn=lambda a: _agent("local-down"))

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
