#!/usr/bin/env python3
"""
ship_web.py -- THE one command that ships JobHuntWOW ("Electronic") to www.jobhuntwow.com.

    python ship_web.py

It does every step itself, in order, and fails fast with an explicit reason:
  0) preflight   : git + gh present, gh logged in, and this folder is a git repo with an origin
                   (it bootstraps `git init` / `gh repo create` if they are missing)
  1) safety      : refuse to push if any .env / secret file got staged
  2) push        : commit + push -> triggers .github/workflows/web-deploy.yml
  3) watch       : trigger the workflow explicitly and stream it (`gh run watch --exit-status`)
                   CI builds Dockerfile.web -> GHCR -> ssh droplet -> compose pull/up ->
                   append deploy/caddy/jobhuntwow.caddy -> caddy reload
  4) verify      : GET https://www.jobhuntwow.com/api/health from HERE until it answers 200

Nothing is built on the droplet and nothing on the droplet is hand-edited. See DEPLOY.md.
One-time human step: the DNS A-records (jobhuntwow.com and www -> the droplet IP).
"""
import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = "web-deploy.yml"
DEFAULT_REPO = "feranicus/jobhuntwow-app"
DOMAIN = "www.jobhuntwow.com"
HEALTH = "https://%s/api/health" % DOMAIN
# never push these, whatever git thinks
SECRET_NAMES = (".env", "agent/.env", "backend/.env", "frontend/.env")


def sh(args, cap=False, check=False, quiet=False):
    if not quiet:
        print("  $ " + " ".join(args), flush=True)
    r = subprocess.run(args, cwd=HERE, text=True, capture_output=cap)
    if check and r.returncode != 0:
        if cap:
            sys.stderr.write((r.stderr or "") + "\n")
        sys.exit("[X] command failed: " + " ".join(args))
    return r


def out(args):
    return (sh(args, cap=True, quiet=True).stdout or "").strip()


def need(tool, howto):
    if not shutil.which(tool):
        sys.exit("[X] '%s' not found. %s" % (tool, howto))


def http_status(url, timeout=12):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "ship_web"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return "ERR(%s)" % type(e).__name__


# ------------------------------------------------------------------ 0) preflight
def preflight(repo, branch):
    need("git", "Install Git: https://git-scm.com")
    need("gh", "Install the GitHub CLI: https://cli.github.com  then run:  gh auth login")
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        sys.exit("[X] GitHub CLI is not logged in. Run:  gh auth login   then re-run this script.")

    if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=HERE,
                      capture_output=True).returncode != 0:
        print("  (no git repo here yet -> git init)")
        sh(["git", "init", "-b", branch], check=True)

    cur = out(["git", "branch", "--show-current"])
    if not cur:
        sh(["git", "checkout", "-b", branch], check=True)
    elif cur != branch:
        print("  [!] current branch is '%s' but the workflow deploys '%s'." % (cur, branch))
        print("      Pass --branch %s, or switch branches, then re-run." % cur)

    if not out(["git", "remote"]):
        print("  (no 'origin' remote -> creating/linking the GitHub repo %s)" % repo)
        if subprocess.run(["gh", "repo", "view", repo], capture_output=True).returncode == 0:
            sh(["git", "remote", "add", "origin", "https://github.com/%s.git" % repo], check=True)
        else:
            sh(["gh", "repo", "create", repo, "--private", "--source=.", "--remote=origin"],
               check=True)
    origin_url = out(["git", "remote", "get-url", "origin"])

    # ---- SAFETY GUARD (learned the hard way) --------------------------------
    # jobhuntwow-app sits INSIDE the 'Linkedin Scraper' (feranicus/electronic) working tree.
    # Without this check, git/gh resolve UP to that repo and `gh workflow run web-deploy.yml`
    # triggers CYBERGOD's deploy (it has a workflow with the same filename) — wrong project,
    # and it touches a live production site. Refuse to run unless we are in OUR OWN repo.
    top = out(["git", "rev-parse", "--show-toplevel"]).replace("\\", "/").rstrip("/")
    here = os.path.abspath(os.path.dirname(__file__)).replace("\\", "/").rstrip("/")
    if top.lower() != here.lower():
        sys.exit(
            "[X] REFUSING TO DEPLOY — this folder is not its own git repository.\n"
            "    git root : %s\n    expected : %s\n"
            "    You are inside the 'electronic' (cybergod.ai) repo, so GitHub would run\n"
            "    CYBERGOD's .github/workflows/web-deploy.yml, not JobHuntWOW's.\n\n"
            "    Fix once (from this folder):\n"
            "      git init && git add -A && git commit -m \"JobHuntWOW Electronic\"\n"
            "      gh repo create %s --private --source=. --remote=origin --push\n"
            "    Then add 'jobhuntwow-app/' to the parent repo's .gitignore.\n" % (top, here, repo))
    want = repo.lower()
    if want not in origin_url.lower():
        sys.exit("[X] REFUSING TO DEPLOY — origin is '%s' but this project expects '%s'.\n"
                 "    Point origin at the right repo, or pass --repo.\n" % (origin_url, repo))
    return origin_url


# ------------------------------------------------------------------ 1) secret safety
def assert_no_secrets_staged():
    staged = out(["git", "diff", "--cached", "--name-only"]).splitlines()
    bad = [f for f in staged
           if f in SECRET_NAMES or f.endswith("/.env") or f == ".env"
           or (f.endswith(".env") and not f.endswith(".env.example"))]
    if bad:
        sys.exit("[X] refusing to push: secret file(s) are staged: %s\n"
                 "    They belong in .gitignore only. Run:  git rm --cached %s"
                 % (", ".join(bad), " ".join(bad)))


# ------------------------------------------------------------------ 2/3) push + watch
def push_and_watch(branch, message):
    print("\n=== 2/4  commit + push (this is what triggers the workflow) ===")
    sh(["git", "add", "-A"])
    assert_no_secrets_staged()
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE).returncode != 0:
        sh(["git", "commit", "-m", message], check=True)
    else:
        print("  (nothing new to commit)")
    sh(["git", "push", "-u", "origin", branch], check=True)

    print("\n=== 3/4  trigger + watch the deploy workflow ===")
    r = sh(["gh", "workflow", "run", WORKFLOW, "--ref", branch], cap=True)
    if r.returncode != 0:
        print("  (could not dispatch: %s)" % (r.stderr or "").strip()[:200])
        print("  the push above already triggers it if it touched a watched path")
    time.sleep(8)
    run_id = out(["gh", "run", "list", "--workflow", WORKFLOW, "--limit", "1",
                  "--json", "databaseId", "--jq", ".[0].databaseId"])
    if not run_id:
        sys.exit("[X] no workflow run found. Check the Actions tab, or:  "
                 "gh run list --workflow " + WORKFLOW)
    r = sh(["gh", "run", "watch", run_id, "--exit-status"])
    if r.returncode != 0:
        sys.exit("[X] the deploy workflow FAILED. Logs:  gh run view %s --log-failed" % run_id)


# ------------------------------------------------------------------ 4) verify
def verify(tries=8, delay=15):
    print("\n=== 4/4  verify from here ===")
    for i in range(tries):
        code = http_status(HEALTH)
        print("  %s -> %s   (200 = live)" % (HEALTH, code))
        if code == 200:
            print("\nDONE. https://%s is live." % DOMAIN)
            return True
        time.sleep(delay if i < tries - 1 else 0)
    print("\n[!] The workflow finished but %s is not answering 200 yet." % HEALTH)
    print("    Most likely, in order:")
    print("      1. DNS: A jobhuntwow.com and A www.jobhuntwow.com must point at the droplet.")
    print("         Until they do, Caddy cannot issue a certificate. This is the ONE human step.")
    print("      2. TLS issuance / DNS propagation: give it 2-10 minutes and re-run (idempotent).")
    print("      3. Read the deploy log:  gh run view --log --workflow " + WORKFLOW)
    return False


def main():
    ap = argparse.ArgumentParser(description="Ship JobHuntWOW to www.jobhuntwow.com (one command).")
    ap.add_argument("--repo", default=os.environ.get("JHW_REPO", DEFAULT_REPO),
                    help="owner/name of the GitHub repo (default: %s)" % DEFAULT_REPO)
    ap.add_argument("--branch", default="main", help="branch the workflow deploys (default: main)")
    ap.add_argument("-m", "--message", default="ship: jobhuntwow.com web deploy via CI",
                    help="commit message")
    ap.add_argument("--no-verify", action="store_true", help="skip the public health check")
    a = ap.parse_args()

    print("=== 1/4  preflight ===")
    origin = preflight(a.repo, a.branch)
    print("  origin: %s" % origin)
    push_and_watch(a.branch, a.message)
    ok = True if a.no_verify else verify()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
