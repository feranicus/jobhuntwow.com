#!/usr/bin/env python3
"""DOMAINS — what the droplet serves, what DNS must say, and whether it actually works.

`python domains.py`

WHY IT EXISTS (2026-09-18): he registered **jobhw.org** at Squarespace and wants it to land on the
cabinet. Two halves, and only one of them is ours:

  * OURS      — the Caddy block (`deploy/caddy/jobhuntwow.caddy`). `python ship.py` puts it on the
                droplet, Caddy fetches the certificate itself. Nothing else to do.
  * HIS       — the DNS at the registrar. Nobody can do that but the account holder, so this script
                PRINTS THE EXACT RECORDS to set, reads the hostnames out of the Caddy block itself
                (one home: what we serve is what we tell him to point), and then MEASURES whether
                each one resolves here and answers.

IT NEVER CHANGES ANYTHING. Read-only: DNS lookups and HTTPS requests. Run it before `ship.py` to see
what is missing, and after to prove the redirect really works end to end.

ORDER MATTERS: set the DNS first. Caddy asks Let's Encrypt for a certificate the moment a hostname
is in its config, and that can only succeed once the name points at this droplet.
"""
from __future__ import annotations

import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCK = os.path.join(HERE, "deploy", "caddy", "jobhuntwow.caddy")
DROPLET = os.environ.get("DROPLET_HOST", "64.225.108.200")
CANON = "jobhuntwow.com"          # the one canonical host everything else redirects to


def hostnames() -> list:
    """Every hostname our managed Caddy block claims — read from the block, never from memory."""
    out, depth = [], 0
    for raw in open(BLOCK, encoding="utf-8").read().splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if depth == 0 and ln.endswith("{"):
            for tok in ln[:-1].split(","):
                tok = tok.strip()
                if re.match(r"^[A-Za-z0-9*.\-]+\.[A-Za-z]{2,}$", tok):
                    out.append(tok.lower())
        depth += ln.count("{") - ln.count("}")
    seen, uniq = set(), []
    for h in out:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


def resolves_to(host: str) -> list:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, socket.AF_INET)})
    except Exception:
        return []


def probe(url: str) -> tuple:
    """(status, location, note). Redirects are NOT followed: the hop itself is what we check."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect,
                                     urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        r = op.open(urllib.request.Request(url, headers={"User-Agent": "jhw-domains"}), timeout=12)
        return r.status, r.headers.get("Location", ""), ""
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), ""
    except Exception as e:
        return 0, "", "%s: %s" % (type(e).__name__, str(e)[:70])


def main() -> int:
    hosts = hostnames()
    extra = [h for h in hosts if not h.endswith(CANON)]
    print("THE DROPLET            %s" % DROPLET)
    print("CANONICAL HOST         https://%s" % CANON)
    print("SERVED BY OUR BLOCK    %s" % ", ".join(hosts))
    print()

    print("1) DNS — set these at the registrar (Squarespace: Domains -> DNS -> DNS Settings).")
    print("   DELETE the 'Squarespace Defaults' preset first: its four A records and the")
    print("   CNAME/HTTPS rows keep pointing the name at Squarespace, and they win over anything")
    print("   you add underneath.")
    print()
    print("   TYPE   NAME   TTL    DATA")
    for h in extra:
        if h.startswith("www."):
            continue
        print("   A      @      1 hr   %s" % DROPLET)
        print("   A      www    1 hr   %s" % DROPLET)
    print()
    print("   No CNAME, no HTTPS/SVCB record, no URL-forwarding rule — Caddy on the droplet does")
    print("   the redirect itself, with its own certificate.")
    print()

    # A CHECK THAT CANNOT SEE ITS SUBJECT MUST SAY SO, NOT REPORT A FINDING. Run from a machine
    # with no DNS (a sandbox, an offline laptop) every name looks "not configured", which would
    # send him to the registrar to fix something that is already right.
    control = resolves_to(CANON)
    if not control:
        print("2) STATUS — CANNOT BE MEASURED FROM THIS MACHINE")
        print("   %s itself does not resolve here, so DNS is unavailable (no resolver, or the" % CANON)
        print("   network blocks it). Nothing below would mean anything. Run this on your own PC.")
        return 2

    print("2) STATUS (measured now, not assumed)")
    ok = True
    for h in hosts:
        ips = resolves_to(h)
        points_here = DROPLET in ips
        mark = "OK  " if points_here else "NOT YET"
        print("   %-22s %-7s A -> %s" % (h, mark, ", ".join(ips) or "(no A record)"))
        if not points_here:
            ok = False
    print()

    # Same rule for the HTTPS half: prove the canonical host answers before judging the new ones.
    cst, _, cnote = probe("https://%s/" % CANON)
    if not cst:
        print("   (https to %s failed here — %s; skipping the redirect checks)" % (CANON, cnote))
        print()
        print("NEXT: set the DNS above, then `python ship.py`, then re-run this on your own PC.")
        return 2

    for h in extra:
        st, loc, note = probe("https://%s/" % h)
        if st in (301, 302, 307, 308) and CANON in loc:
            print("   https://%-18s -> %s  %s   REDIRECT WORKS" % (h + "/", st, loc))
        elif st:
            print("   https://%-18s -> %s  %s" % (h + "/", st, loc or "(no Location)"))
        else:
            print("   https://%-18s -> not reachable yet (%s)" % (h + "/", note))
            ok = False

    print()
    if not ok:
        print("NEXT: set the DNS above, wait for it to propagate (minutes to a few hours),")
        print("      then run:  python ship.py      — it puts the Caddy block on the droplet and")
        print("      Caddy fetches the certificate on the first request.")
        print("      Re-run `python domains.py` to confirm.")
        return 1
    print("Every hostname points here and every redirect answers. Nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
