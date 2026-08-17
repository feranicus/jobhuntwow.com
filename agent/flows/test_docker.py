#!/usr/bin/env python3
"""THE CONTAINER RULES WE ALREADY WROTE DOWN, ENFORCED.

CLAUDE.md carries a standing rule for every containerised app in this estate: never publish a port on
0.0.0.0 unless the internet must reach it, least privilege (`no-new-privileges`, `cap_drop`), resource
limits so a runaway browser cannot take the host down, healthchecks so orchestration knows what
"ready" means, and secrets only at runtime. It was a rule in a markdown file, which is the state every
other rule in this project was in before it started costing runs. This makes it a test.

MEASURED 2026-08-17, before this file existed: `cap_drop` was missing on three of four services and
`healthcheck` on three of four. Nothing had regressed -- it had simply never been applied.

ONE DELIBERATE EXEMPTION, and it is named rather than hidden. `jhw-browser` does NOT get
`cap_drop: [ALL]`. Chrome's own sandbox needs capabilities; the container already runs it with
`--no-sandbox`, and changing that on a stack that works, without being able to run docker and prove
it, would be trading a working browser for a checkbox. The exemption is asserted HERE with its reason,
so it is a decision on the record instead of an oversight.
"""
from __future__ import annotations
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = os.path.abspath(os.path.join(HERE, "..", "docker-compose.local.yml"))

# service -> why it may skip cap_drop. Empty dict would mean "no exemptions".
CAP_EXEMPT = {
    "jhw-browser": "Chrome's sandbox needs capabilities; it already runs --no-sandbox, and dropping "
                   "them unverified risks a working browser for a checkbox",
}
NEEDS_HEALTH = {"jhw-browser", "jhw-agent"}      # the two long-running ones the stack waits on

FAILS: list = []


def ck(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    if not os.path.exists(COMPOSE):
        print(f"  [i] SKIPPED: {COMPOSE} not present in this copy")
        print("  ALL DOCKER CONTRACTS HOLD (not evaluated here)")
        return 0
    # A CHECK MORE PERMISSIVE THAN THE CONSUMER IS NOT A CHECK.
    # MEASURED 2026-08-17: I added `security_opt` to two services that already had one. `yaml.safe_load`
    # accepted the file silently (PyYAML keeps the LAST duplicate key), this suite passed, and
    # `docker compose` then refused to start the whole stack with
    #     mapping key "security_opt" already defined at line 38
    # Two apply runs died before they began. The verification has to be at least as strict as the tool
    # that consumes the file, or it certifies files that cannot be used.
    class _Strict(yaml.SafeLoader):
        pass

    def _no_dupes(loader, node, deep=False):
        seen, out = set(), {}
        for k, v in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f'DUPLICATE mapping key "{key}" — docker compose will refuse this '
                                f'file even though yaml.safe_load accepts it', k.start_mark)
            seen.add(key)
            out[key] = loader.construct_object(v, deep=deep)
        return out

    _Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dupes)
    print("\n[0] the file is valid to the STRICTNESS DOCKER USES, not just to PyYAML")
    try:
        with open(COMPOSE, encoding="utf-8") as fh:
            d = yaml.load(fh, Loader=_Strict)
        ck(True, "no duplicate mapping key anywhere in the file")
    except Exception as e:
        ck(False, f"the compose file is invalid: {e}")
        print("\n" + "=" * 78)
        print("[X] 1 DOCKER CONTRACT(S) BROKEN")
        return 1
    svcs = d.get("services") or {}
    print(f"\n[1] the file parses and describes the expected stack ({len(svcs)} services)")
    ck(set(svcs) == {"jhw-browser", "jhw-stagehand", "jhw-bot", "jhw-agent"}, str(sorted(svcs)))

    print("\n[2] NO PORT ON 0.0.0.0 — a published CDP is unauthenticated remote browser control")
    for n, sv in svcs.items():
        for p in (sv.get("ports") or []):
            ck(str(p).startswith("127.0.0.1:"), f"{n}: {p} is loopback-only")
        # 9222 (CDP), 5900 (VNC) must never be published at all
        for p in (sv.get("ports") or []):
            ck(not str(p).split(":")[-1] in ("9222", "5900"),
               f"{n}: {p} does not expose CDP or VNC")

    print("\n[3] least privilege")
    for n, sv in svcs.items():
        so = " ".join(str(x) for x in (sv.get("security_opt") or []))
        ck("no-new-privileges" in so, f"{n}: no-new-privileges")
        if n in CAP_EXEMPT:
            ck(True, f"{n}: cap_drop EXEMPT — {CAP_EXEMPT[n][:62]}")
        else:
            caps = [str(x).upper() for x in (sv.get("cap_drop") or [])]
            ck("ALL" in caps, f"{n}: cap_drop [ALL]")

    print("\n[4] resource limits — a runaway loop must not take the host down")
    for n, sv in svcs.items():
        ck(bool(sv.get("mem_limit")), f"{n}: mem_limit {sv.get('mem_limit') or '—'}")
        ck(bool(sv.get("cpus")), f"{n}: cpus {sv.get('cpus') or '—'}")

    print("\n[5] healthchecks on the services the stack waits for")
    for n in sorted(NEEDS_HEALTH):
        ck(bool((svcs.get(n) or {}).get("healthcheck")), f"{n}: healthcheck")

    print("\n[6] no secret is baked into the compose file")
    blob = yaml.safe_dump(d)
    import re
    for pat, what in ((r"\b\d{9,10}:[A-Za-z0-9_-]{30,}\b", "a Telegram bot token"),
                      (r"\bsk-[A-Za-z0-9]{20,}", "an API key"),
                      (r"PASSWORD\s*[:=]\s*[^\s${}]{6,}", "a literal password")):
        ck(not re.search(pat, blob), f"no {what} in the compose file")
    envs = [e for sv in svcs.values() for e in (sv.get("environment") or [])]
    hard = [e for e in envs if isinstance(e, str) and "=" in e
            and re.search(r"(TOKEN|PASSWORD|KEY|SECRET)=", e)
            and not re.search(r"=\s*$|=\$\{", e)]
    ck(not hard, f"no credential is hardcoded in `environment:` ({hard})")

    print("\n[7] every volume is well-formed and the risky ones are read-only")
    for n, sv in svcs.items():
        for v in (sv.get("volumes") or []):
            ck(isinstance(v, str) and ":" in v, f"{n}: {v}")
        for v in (sv.get("volumes") or []):
            if isinstance(v, str) and v.startswith("../"):
                ck(v.endswith(":ro"), f"{n}: a mount reaching OUTSIDE agent/ is read-only -> {v}")

    print("\n[8] every command we TELL the operator to run must actually exist")
    # MEASURED 2026-08-17: a failure path printed `Evidence: python jhw.py doctor`, and from the repo
    # root that verb does not exist -- so the one line meant to help produced
    # `invalid choice: 'doctor'`. A diagnostic that names a nonexistent command is worse than silence.
    import re
    roots = {"jhw.py": os.path.abspath(os.path.join(HERE, "..", "..", "jhw.py")),
             "agent/jhw.py": os.path.abspath(os.path.join(HERE, "..", "jhw.py"))}
    verbs = {}
    for name, path in roots.items():
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                verbs[name] = set(re.findall(r'add_parser\("([a-z-]+)"', fh.read()))
    bad = []
    for name, path in roots.items():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for m in re.finditer(r"python\s+((?:agent/)?jhw\.py)\s+([a-z-]+)", src):
            which, verb = m.group(1), m.group(2)
            # RESOLVE AGAINST THE FILE THAT PRINTS THE MESSAGE. A bare `python jhw.py atspw` inside
            # agent/jhw.py means ITS OWN verbs (the operator would be in agent/), and my first version
            # assumed the root file and flagged twelve perfectly correct lines. An explicit
            # `agent/jhw.py X` always means the agent one.
            if which.startswith("agent/"):
                target = "agent/jhw.py"
            else:
                target = name          # a bare jhw.py means the file we are reading
            have = verbs.get(target, set())
            if have and verb not in have:
                bad.append(f"{name} tells you `python {which} {verb}` — {target} has no such verb")
    for b in sorted(set(bad)):
        ck(False, b)
    if not bad:
        ck(True, f"every `python jhw.py <verb>` we print is a real verb "
                 f"({sum(len(v) for v in verbs.values())} verbs across {len(verbs)} entry points)")

    print("\n" + "=" * 78)
    if FAILS:
        print(f"[X] {len(FAILS)} DOCKER CONTRACT(S) BROKEN")
        return 1
    print("ALL DOCKER CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
