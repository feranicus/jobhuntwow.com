#!/usr/bin/env python3
"""mirror_grafana.py — RUN ON THE DROPLET (shipped + invoked by `python jhw.py mirror`).

Clone EVERY cybergod/colt dashboard in the shared Grafana into a JobHuntWOW twin, 1:1, straight
from the LIVE source — so whatever cybergod.ai has, jobhuntwow.com gets the identical board with
its own data. No hand-built approximations.

Transform (the only differences between the twins):
  cybergod.ai            -> jobhuntwow.com          (host, titles, request_host filters)
  colt-web               -> jhw-web                 (the `service` field our events carry)
  Colt / cybergod        -> JobHuntWOW              (in the title)
  uid X                  -> jhw-X                    (distinct, deterministic -> idempotent)
Everything else (queries, panels, layout, the container=assess-bot / videodead-caddy-1 selectors)
is preserved byte-for-byte. Talks to Grafana via `docker exec <grafana> curl` on localhost:3000,
using the admin creds the container already holds. Idempotent (overwrite=true).
"""
import json, re, subprocess, sys


def sh(*args, check=True):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(" ".join(args) + " -> " + (r.stderr or "")[:200])
    return r.stdout.strip()


def grafana_container():
    for n in sh("docker", "ps", "--format", "{{.Names}}").splitlines():
        if "grafana" in n.lower():
            return n.strip()
    raise SystemExit("no grafana container found")


def api(gra, method, path, creds, body=None):
    cmd = ["docker", "exec", gra, "curl", "-s", "-u", creds,
           "-H", "Content-Type: application/json", "-X", method,
           "http://127.0.0.1:3000" + path]
    if body is not None:
        cmd[cmd.index("-X"):cmd.index("-X")] = ["-d", body]  # insert -d before -X (order ok for curl)
        cmd = ["docker", "exec", gra, "curl", "-s", "-u", creds,
               "-H", "Content-Type: application/json", "-X", method,
               "-d", body, "http://127.0.0.1:3000" + path]
    return sh(*cmd)


def retarget(model: dict) -> dict:
    """Pure JSON->JSON transform. Deterministic; safe to run repeatedly."""
    raw = json.dumps(model)
    raw = raw.replace("cybergod.ai", "jobhuntwow.com")
    raw = raw.replace("colt-web", "jhw-web")
    d = json.loads(raw)
    old_uid = (d.get("uid") or "dash")
    d["uid"] = ("jhw-" + old_uid)[:40]
    t = d.get("title") or "Dashboard"
    t = t.replace("Colt Web", "JobHuntWOW Web").replace("Colt", "JobHuntWOW")
    t = t.replace("cybergod.ai", "jobhuntwow.com")
    if "jobhuntwow" not in t.lower():
        t = "JobHuntWOW — " + t
    d["title"] = t
    d.pop("id", None)
    d.pop("version", None)
    return d


SOURCE_RE = re.compile(r"colt|cybergod", re.I)


def main():
    gra = grafana_container()
    gu = sh("docker", "exec", gra, "printenv", "GF_SECURITY_ADMIN_USER", check=False) or "admin"
    gp = sh("docker", "exec", gra, "printenv", "GF_SECURITY_ADMIN_PASSWORD", check=False) or "admin"
    creds = "%s:%s" % (gu, gp)
    print("  grafana=%s  user=%s" % (gra, gu))

    search = json.loads(api(gra, "GET", "/api/search?type=dash-db", creds) or "[]")
    made, skipped = 0, 0
    for row in search:
        uid, title = row.get("uid"), row.get("title") or ""
        if not uid:
            continue
        if uid.startswith("jhw-") or "jobhuntwow" in title.lower():
            skipped += 1; continue                      # already a jobhuntwow twin
        if not SOURCE_RE.search(title) and not SOURCE_RE.search(uid):
            skipped += 1; continue                      # not a cybergod/colt board -> leave alone
        full = json.loads(api(gra, "GET", "/api/dashboards/uid/" + uid, creds) or "{}")
        model = full.get("dashboard")
        if not isinstance(model, dict):
            print("  [!] could not read %s" % uid); continue
        twin = retarget(model)
        payload = json.dumps({"dashboard": twin, "overwrite": True, "folderId": 0,
                              "message": "mirrored from %s by jhw.py mirror" % uid})
        out = api(gra, "POST", "/api/dashboards/db", creds, payload)
        status = "success" if '"status":"success"' in out.replace(" ", "") else out[:160]
        print("  %-40s -> %-40s [%s]" % (title[:40], twin["title"][:40], status))
        made += 1
    print("\n  mirrored %d cybergod dashboard(s) for jobhuntwow; skipped %d." % (made, skipped))
    print("  open godeyes.ai/observe/dashboards -> the 'JobHuntWOW —' copies.")


if __name__ == "__main__":
    main()
