#!/usr/bin/env python3
"""sync_secrets.py — reuse the cybergod.ai / JobHuntWOW .env values instead of retyping them.

Builds the PRODUCTION runtime .env for the droplet by merging what you already have locally, then
uploads it (plus the deploy secrets) to the JobHuntWOW GitHub repo. Re-runnable and idempotent.

  python sync_secrets.py --dry-run                 # show WHICH keys would be set (names only)
  python sync_secrets.py                           # set them
  python sync_secrets.py --ssh-key C:\\path\\id_ed25519 --ts-authkey tskey-auth-xxxx

SECURITY: values are passed to `gh` over STDIN, never on the command line (argv shows up in `ps`
and shell history) and are NEVER printed. Only key NAMES are ever echoed.
"""
from __future__ import annotations
import argparse, base64, os, secrets, subprocess, sys

HERE   = os.path.abspath(os.path.dirname(__file__))
PARENT = os.path.abspath(os.path.join(HERE, ".."))          # ...\Linkedin Scraper (cybergod)
DEFAULT_REPO = os.environ.get("JHW_REPO", "feranicus/jobhuntwow.com")
DROPLET_IP   = os.environ.get("DROPLET_HOST", "64.225.108.200")

# source .env files, in priority order (later files do NOT override earlier ones)
SOURCES = [
    os.path.join(HERE, ".env"),                              # DO_INFERENCE_KEY, AGENT_PROXY_TOKEN
    os.path.join(PARENT, "cassandra-bot", ".env"),           # GMAIL_SENDER, GMAIL_SA_B64, OTP tunables
    os.path.join(PARENT, "assess-bot", ".env"),              # fallback for the same
]

# what the production app actually needs
WANT = ["DO_INFERENCE_KEY", "DO_INFERENCE_BASE_URL", "AGENT_PROXY_TOKEN",
        "GMAIL_SENDER", "GMAIL_SA_B64",
        "OTP_TTL_SECS", "OTP_MAX_TRIES", "AUTH_MAX_FAILS", "AUTH_LOCK_SECS"]

# sensible production defaults when a key isn't in any source file
DEFAULTS = {
    "DATA_DIR": "/data",
    "CORS_ORIGINS": "https://jobhuntwow.com,https://www.jobhuntwow.com",
    "DO_INFERENCE_BASE_URL": "https://inference.do-ai.run/v1",
    "OTP_TTL_SECS": "600", "OTP_MAX_TRIES": "5",
    "AUTH_MAX_FAILS": "5", "AUTH_LOCK_SECS": "900",
}


def read_env(path: str) -> dict:
    out = {}
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def collect() -> tuple[dict, list]:
    merged, seen_in = {}, []
    for src in SOURCES:
        env = read_env(src)
        if env:
            seen_in.append((src, len(env)))
        for k in WANT:
            if k in env and k not in merged and env[k]:
                merged[k] = env[k]
    for k, v in DEFAULTS.items():
        merged.setdefault(k, v)
    # SESSION_SECRET must exist and must be stable across deploys -> reuse if present, else mint one
    existing = read_env(os.path.join(HERE, ".env")).get("SESSION_SECRET")
    merged["SESSION_SECRET"] = existing or secrets.token_urlsafe(48)
    return merged, seen_in


def gh_set(repo: str, name: str, value: str) -> None:
    """Set a GitHub secret with the value on STDIN (never argv, never printed)."""
    p = subprocess.run(["gh", "secret", "set", name, "--repo", repo],
                       input=value.encode(), capture_output=True)
    if p.returncode != 0:
        sys.exit(f"[X] gh secret set {name} failed: {p.stderr.decode()[:300]}")
    print(f"  [OK] {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--ssh-key", default="", help="path to the droplet private key (DROPLET_SSH_KEY)")
    ap.add_argument("--ts-authkey", default=os.environ.get("TS_AUTHKEY", ""), help="fresh Tailscale auth key")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    merged, seen = collect()
    print("=== sources read ===")
    for src, n in seen:
        print(f"  {src}  ({n} keys)")
    if not seen:
        sys.exit("[X] no source .env found. Expected jobhuntwow-app/.env and/or ../cassandra-bot/.env")

    missing = [k for k in ("DO_INFERENCE_KEY", "GMAIL_SENDER", "GMAIL_SA_B64") if not merged.get(k)]
    print("\n=== runtime .env that will become JHW_ENV_B64 (names only) ===")
    print("  " + " ".join(sorted(merged)))
    if missing:
        print("\n  [!] MISSING (OTP email will not work without these): " + ", ".join(missing))

    body = "\n".join(f"{k}={merged[k]}" for k in sorted(merged)) + "\n"
    env_b64 = base64.b64encode(body.encode()).decode()

    print("\n=== GitHub secrets to set on %s ===" % a.repo)
    plan = {"JHW_ENV_B64": env_b64, "DROPLET_HOST": DROPLET_IP}
    if a.ssh_key:
        if not os.path.isfile(a.ssh_key):
            sys.exit(f"[X] ssh key not found: {a.ssh_key}")
        plan["DROPLET_SSH_KEY"] = open(a.ssh_key, encoding="utf-8").read()
    if a.ts_authkey:
        plan["TS_AUTHKEY"] = a.ts_authkey
    print("  " + " ".join(plan))

    if a.dry_run:
        print("\n(dry run — nothing was sent)")
        return

    for name, value in plan.items():
        gh_set(a.repo, name, value)

    todo = [n for n in ("DROPLET_SSH_KEY", "TS_AUTHKEY") if n not in plan]
    if todo:
        print("\n[!] Still needed (GitHub secrets are write-only, so these CANNOT be copied from the")
        print("    cybergod repo — supply them once):")
        if "DROPLET_SSH_KEY" in todo:
            print("      python sync_secrets.py --ssh-key C:\\path\\to\\droplet_key")
        if "TS_AUTHKEY" in todo:
            print("      python sync_secrets.py --ts-authkey tskey-auth-...   (mint a fresh one)")
    print("\nNext:  python ship_web.py")


if __name__ == "__main__":
    main()
