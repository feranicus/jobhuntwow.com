#!/usr/bin/env python3
"""fix_caddy.py — runs ON THE DROPLET. Make jobhuntwow.com belong to jhw-web, and nothing else.

WHY THIS EXISTS (measured, not theorised): https://jobhuntwow.com/api/health returned
259,464 bytes of HTML with `etag` + `last-modified` + `accept-ranges` — the signature of a
STATIC FILE SERVER handing out a copy of the old one-pager. Our container never saw the
request (its log showed only healthchecks). A `sed` on comment markers could not fix that,
because the offending block has no markers: it claims the hostname via `{$DOMAIN}` or a
plain `root * /srv/...` site block.

So we PARSE the Caddyfile instead of pattern-matching it:
  * find every top-level site block and its address list,
  * expand {$DOMAIN} / {env.DOMAIN} using the caddy container's real environment,
  * any address that is jobhuntwow.com or www.jobhuntwow.com and is NOT our managed block
    is REMOVED from that block's address list,
  * a block left with no addresses is deleted entirely,
  * our managed block is then appended from deploy/caddy/jobhuntwow.caddy.

Other vhosts (videodead, cybergod.ai) are never touched: we only ever remove OUR hostnames.
A timestamped .bak is written before any change.
"""
from __future__ import annotations
import argparse, os, re, shutil, sys, time

OURS = {"jobhuntwow.com", "www.jobhuntwow.com"}
BEGIN, END = "# jhw:jobhuntwow BEGIN", "# jhw:jobhuntwow END"


def strip_managed(lines: list[str]) -> list[str]:
    """Drop our own marker block so we can re-append a fresh copy."""
    out, skip = [], False
    for ln in lines:
        if BEGIN in ln:
            skip = True
        if not skip:
            out.append(ln)
        if END in ln:
            skip = False
    return out


def find_blocks(lines: list[str]):
    """Yield (start_idx, end_idx, address_string) for every TOP-LEVEL site block.

    Brace depth is tracked so nested directives ({...} inside a site) are never mistaken
    for a site header.
    """
    depth, start, addr = 0, None, ""
    for i, ln in enumerate(lines):
        code = ln.split("#", 1)[0]
        if depth == 0 and code.strip().endswith("{"):
            start, addr = i, code.strip()[:-1].strip()
        depth += code.count("{") - code.count("}")
        if depth == 0 and start is not None and code.count("}"):
            yield (start, i, addr)
            start, addr = None, ""


def expand(addr: str, env: dict) -> list[str]:
    """Resolve {$VAR} / {env.VAR} placeholders so we can see the REAL hostnames."""
    def sub(m):
        return env.get(m.group(1) or m.group(2) or "", "")
    resolved = re.sub(r"\{\$([A-Za-z_][A-Za-z0-9_]*)\}|\{env\.([A-Za-z_][A-Za-z0-9_]*)\}", sub, addr)
    return [a.strip() for a in resolved.split(",") if a.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caddyfile", required=True)
    ap.add_argument("--snippet", required=True)
    ap.add_argument("--env", default="", help="KEY=VAL;KEY=VAL from the caddy container")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    env = {}
    for pair in a.env.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            env[k.strip()] = v.strip()

    src = open(a.caddyfile, encoding="utf-8", errors="replace").read().splitlines()
    lines = strip_managed(src)

    drop_lines: set[int] = set()
    rewrite: dict[int, str] = {}
    removed: list[str] = []

    for start, end, addr in find_blocks(lines):
        raw = [x.strip() for x in addr.split(",") if x.strip()]
        real = expand(addr, env)
        keep_raw = []
        for token, resolved in zip(raw, real if len(real) == len(raw) else raw):
            if resolved.lower().lstrip("*.") in OURS or resolved.lower() in OURS:
                removed.append(f"line {start+1}: '{token}' (resolves to '{resolved}')")
            else:
                keep_raw.append(token)
        if len(keep_raw) == len(raw):
            continue                                  # block untouched
        if not keep_raw:
            drop_lines.update(range(start, end + 1))   # whole block was ours -> delete
            removed.append(f"  -> deleted the entire block at lines {start+1}-{end+1}")
        else:
            rewrite[start] = ", ".join(keep_raw) + " {"

    out = []
    for i, ln in enumerate(lines):
        if i in drop_lines:
            continue
        out.append(rewrite.get(i, ln))

    snippet = open(a.snippet, encoding="utf-8").read().rstrip("\n")
    text = "\n".join(out).rstrip("\n") + "\n\n" + snippet + "\n"

    if removed:
        print("  REMOVED conflicting claims on our hostnames:")
        for r in removed:
            print("    " + r)
    else:
        print("  no conflicting block claimed jobhuntwow.com (only ours)")

    if a.dry_run:
        print("  (dry run — nothing written)")
        return
    shutil.copy2(a.caddyfile, f"{a.caddyfile}.bak.{int(time.time())}")
    open(a.caddyfile, "w", encoding="utf-8", newline="\n").write(text)
    print(f"  wrote {a.caddyfile} (backup kept)")

    # Prove the result, do not assume it: our hostname must now appear exactly once.
    final = open(a.caddyfile, encoding="utf-8").read().splitlines()
    claims = [ln.strip() for s, e, ad in find_blocks(final)
              for ln in [ad] if set(x.lower() for x in expand(ad, env)) & OURS]
    print(f"  site blocks now claiming our hostnames: {claims}")
    if len(claims) != 2:      # one for www (redirect), one for the apex
        print(f"  [!] expected 2 (www redirect + apex), got {len(claims)}")


if __name__ == "__main__":
    main()
