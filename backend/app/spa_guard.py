# -*- coding: utf-8 -*-
"""spa_guard.py - should this unknown path get the SPA shell, or a 404?

THE MEASUREMENT THAT PRODUCED THIS FILE (24h, 2026-09-21). jobhuntwow logged 4002 requests, 320 of
them attack-shaped, and fired ZERO alerts, while cybergod fired 25 on a quarter of the traffic.
The cause was not the rules. It was this:

    serve.py's SPA catch-all answered **HTTP 200** to every unknown GET, including /.env,
    /wp-login.php and /phpmyadmin/.

Three consequences, all of which this file exists to end:

  1. THE ALERT RULES COULD NOT FIRE. `path_probe` and `dir_bruteforce` in observability.py are
     gated on `status in (404, 403)`. Against a server that answers 200 to everything they are
     structurally unable to fire - not mistuned, unable. That is the whole of the zero.

  2. THE SIDECAR DISARMED ITSELF. perseus_client._learn_served() counts probe-shaped paths this
     app answered 2xx on; at PERSEUS_CATCHALL_N (3) it concludes the app has a catch-all and
     _decide_raw() returns ALLOW forever with the reason "this app has a catch-all - local
     enforcement is off here". jobhuntwow's local shield has almost certainly never blocked
     anything. Returning 404 for probe shapes keeps those paths OUT of `_served`, which un-blinds
     the detector.

  3. WE TOLD SCANNERS /wp-login.php EXISTS. A 200 is an invitation to come back with a payload.

WHY THE JUDGEMENT IS NOT WRITTEN HERE. `perseus_client.probe_shape()` is the estate's ONE HOME for
"does this look like scanner behaviour" - nineteen classes measured against a real mass-scanning
corpus, every pattern anchored, with a committed test asserting both directions. A second path
table in this file would drift from it, and the stale copy always wins. So this file contributes
exactly one thing that perseus_client cannot know: WHICH PATHS THIS PARTICULAR APP SERVES.

THE FALSE POSITIVE IS THE ONLY RISK THAT MATTERS. A single-page application legitimately serves
client-side routes the server has never heard of, so a wrong 404 here is a real person staring at
a dead page. Every rule below therefore fails toward serving the SPA:

  * a declared client-side route is NEVER 404'd, whatever its shape or its query string;
  * a file that exists on disk is NEVER 404'd;
  * if perseus_client cannot be imported, or anything at all raises, we serve the SPA;
  * JHW_PROBE_404=0 turns the whole behaviour off with a container restart and no deploy.

CLIENT_ROUTES IS A MIRROR, NOT A SOURCE. The routes live in frontend/src/App.jsx; they are
restated here because the container ships the BUILT bundle and cannot read App.jsx at runtime.
tests/test_spa_guard.py parses App.jsx and FAILS if the two ever disagree, so the mirror cannot
rot quietly - which is the only way a restated value is allowed to exist in this estate.
"""
import os
import re

# Mirrored from frontend/src/App.jsx. Both <Routes> blocks: the signed-out pair and the cabinet.
# "*" catch-alls are deliberately NOT listed - a wildcard route is not evidence that a specific
# path is real, and treating it as such would exempt every path on the internet.
CLIENT_ROUTES = frozenset({
    "/",             # Dashboard (signed in) / the public landing one-pager (signed out)
    "/login",
    "/signup",
    "/scout",
    "/pipeline",
    "/electronic",
    "/tailor",
    "/hermes",       # <Navigate to="/electronic">, still a URL a bookmark can hold
    "/connections",
    "/security",     # the operator's console: a normal SPA route here, admin-only at the API
})

# Files a web root serves by convention. Not routes; shapes. None of these is a page a person can
# be locked out of, and ordinary browsers ask for them unprompted.
WELL_KNOWN = frozenset({
    "/robots.txt", "/sitemap.xml", "/favicon.ico", "/manifest.webmanifest", "/sw.js",
    "/healthz", "/health",
})

_OFF = ("0", "off", "false", "no")


def enabled():
    """DEFAULT ON, switchable without a deploy. A control that can only be removed by rebuilding
    an image is a control nobody dares arm in the first place."""
    return str(os.environ.get("JHW_PROBE_404", "1")).strip().lower() not in _OFF


def norm(path):
    """The comparable form: query dropped, trailing slash dropped, never empty."""
    raw = str(path or "/")
    if not raw.startswith("/"):
        raw = "/" + raw
    return (raw.split("?")[0] or "/").rstrip("/") or "/"


def is_ours(path):
    """The route predicate handed to probe_shape(): what THIS app serves.

    Union of the declared client routes, the conventional web-root files, and whatever
    perseus_client has learned this app actually answers 2xx on. Widening this set can only ever
    make us serve MORE, never 404 more, so an error inside it resolves to 'ours'.
    """
    p = norm(path)
    if p in CLIENT_ROUTES or p in WELL_KNOWN:
        return True
    try:
        from . import perseus_client as pc
    except Exception:
        try:
            import perseus_client as pc          # standalone (the self-test below)
        except Exception:
            return False
    try:
        return bool(pc.local_is_ours(p))
    except Exception:
        return False


def decision(path, exists_on_disk=False):
    """-> (should_404, reason). NEVER raises. Every error resolves to (False, ...): serve the SPA.

    `reason` is the string that goes into the event, so it must name what was actually measured -
    a diagnostic that does not name its subject sends the next investigation down the wrong road.
    """
    try:
        if not enabled():
            return False, "JHW_PROBE_404 is off"
        p = norm(path)
        # A DECLARED ROUTE IS NEVER AN ATTACK, and this is checked before anything else. It is
        # checked on the path with the QUERY STRIPPED on purpose: probe_shape() scores a hostile
        # query even on our own pages (correctly - the payload is the query), but refusing to
        # SERVE /?XDEBUG_SESSION_START=x would take the homepage away from whoever followed that
        # link. The request is still scored and still reported; it is only not refused.
        if p in CLIENT_ROUTES or p in WELL_KNOWN:
            return False, "declared client-side route"
        if exists_on_disk:
            return False, "a real file exists at this path"
        try:
            from . import perseus_client as pc
        except Exception:
            try:
                import perseus_client as pc      # standalone (the self-test below)
            except Exception:
                # THE JUDGE IS ABSENT, SO THERE IS NO JUDGEMENT. Absence of evidence is never a
                # finding: we serve the page rather than invent a verdict without the detector.
                return False, "perseus_client unavailable - no judgement, serving the SPA"
        if not pc.probe_shape(p, is_ours):
            return False, "not probe-shaped"
        return True, "probe-shaped and not a route this app serves"
    except Exception as exc:                     # fail open, always
        return False, "spa_guard raised %s - serving the SPA" % type(exc).__name__


def should_404(path, exists_on_disk=False):
    return decision(path, exists_on_disk)[0]


# =================================================================================================
# SELF-TEST. Standalone, stdlib only, no network, no FastAPI: `python3 backend/app/spa_guard.py`.
# Wired into ship.py's suite list, because a check nobody runs is not a check.
# =================================================================================================
# The corpus of REAL routes. None of these may EVER be refused. Deep/unknown-but-harmless paths
# are included deliberately: an SPA is allowed to own URLs the server has never heard of.
REAL_PATHS = [
    "/", "/login", "/signup", "/scout", "/pipeline", "/electronic", "/tailor", "/hermes",
    "/connections",
    "/security",
    "/login/", "/pipeline/", "/tailor/",                       # trailing slash
    "/?ref=linkedin", "/login?next=/pipeline",                 # ordinary query strings
    "/?XDEBUG_SESSION_START=phpstorm",                         # hostile QUERY on a real page
    "/robots.txt", "/favicon.ico", "/sitemap.xml", "/manifest.webmanifest", "/sw.js",
    "/assets/index-PklFsJG7.js", "/assets/index-Bzo5McA4.css",  # the real built bundle names
    "/assets/logo.svg", "/assets/hero.webp",
]

# Paths from the real 24h log and the mass-scanning corpus. Every one must be refused.
PROBE_PATHS = [
    "/.env", "/.git/config", "/wp-login.php", "/wp-admin/setup-config.php", "/phpmyadmin/",
    "/xmlrpc.php", "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php", "/.aws/credentials",
    "/admin.php", "/cgi-bin/luci", "/actuator/env", "/solr/admin/info/system",
    "/.ssh/id_rsa", "/backup.zip", "/config.json.old", "/server-status", "/.DS_Store",
    "/1.php7", "/@fs/etc/passwd", "/kubeconfig", "/Dockerfile", "/web.config",
    "/%2eenv", "/assets/../../.env", "/telescope/requests", "/autodiscover/autodiscover.xml",
]


def _selftest():
    import sys
    fails = []

    def ck(name, ok, detail):
        print("  %-5s %-34s %s" % ("ok" if ok else "FAIL", name, detail))
        if not ok:
            fails.append(name)

    print("spa_guard self-test")
    print("-" * 78)

    # ---- 1. THE ONE THAT MATTERS. No real route is ever treated as an attack. --------------
    refused = [p for p in REAL_PATHS if should_404(p)]
    ck("no_real_route_is_ever_404d", not refused,
       "%d real path(s) checked, refused: %s" % (len(REAL_PATHS), refused or "none"))

    # ---- 2. The probes ARE refused, or the whole change bought nothing. --------------------
    served = [p for p in PROBE_PATHS if not should_404(p)]
    ck("every_probe_is_refused", not served,
       "%d probe path(s) checked, still served: %s" % (len(PROBE_PATHS), served or "none"))

    # ---- 3. A file that exists is never refused, whatever its shape. -----------------------
    ck("real_file_wins_over_shape", not should_404("/.well-known/security.txt", True)
       and not should_404("/backup.zip", exists_on_disk=True),
       "exists_on_disk=True overrides the shape verdict")

    # ---- 4. The switch really switches. A flag nobody has seen work is not a flag. ---------
    old = os.environ.get("JHW_PROBE_404")
    try:
        os.environ["JHW_PROBE_404"] = "0"
        off = should_404("/wp-login.php")
        os.environ["JHW_PROBE_404"] = "1"
        on = should_404("/wp-login.php")
    finally:
        if old is None:
            os.environ.pop("JHW_PROBE_404", None)
        else:
            os.environ["JHW_PROBE_404"] = old
    ck("env_switch_works", (on and not off),
       "JHW_PROBE_404=0 -> serve (%s), =1 -> refuse (%s)" % (off, on))

    # ---- 5. FAIL OPEN. With no judge available nothing is ever refused. --------------------
    import builtins
    real_import = builtins.__import__

    def _no_perseus(name, *a, **k):
        if "perseus_client" in name:
            raise ImportError("simulated: the sidecar is not importable")
        return real_import(name, *a, **k)

    builtins.__import__ = _no_perseus
    try:
        blind = [p for p in PROBE_PATHS if should_404(p)]
    finally:
        builtins.__import__ = real_import
    ck("fails_open_without_perseus", not blind,
       "with perseus_client unimportable, %d/%d probes refused (must be 0)"
       % (len(blind), len(PROBE_PATHS)))

    # ---- 6. The reason is never empty: the event has to name what was measured. ------------
    empty = [p for p in (REAL_PATHS + PROBE_PATHS) if not (decision(p)[1] or "").strip()]
    ck("every_decision_names_its_reason", not empty, "paths with a blank reason: %s" % (empty or "none"))

    print("-" * 78)
    print("%d checks run, %d failed" % (6, len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
