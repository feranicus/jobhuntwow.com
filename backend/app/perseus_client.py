"""The ONLY file copied into each project. Stateless-on-disk, stdlib-only, no network call.

COPY THIS, NOT THE BRAIN. Everything that LEARNS -- the four-model panel, rule promotion, abuse
reporting, the ruleset history -- lives in the hub, in one place, and changes there. This file
carries two things and nothing else:

  1. THE HUB'S PUBLISHED BLOCKLIST, read from a file on a volume every project already mounts. A
     file cannot be down, cannot rate-limit us, and needs no token.
  2. THE LOCAL SHIELD -- the same deterministic detection and enforcement cybergod.ai has run since
     10 Aug 2026, so that a project is defended on its FIRST hostile request instead of waiting for
     a nightly cycle to publish a pattern about it. Until this existed, four of the five sites ran
     `check()` against an empty pattern list and blocked NOTHING, for their entire lives.

WHY THE DETECTION TABLE LIVES HERE AND NOT IN shield.py. It used to live in shield.py, which is not
copied anywhere, so four projects could not see it. Now `webapp/backend/app/shield.py` IMPORTS the
table from this module -- one implementation, in one file, and no way for the two to drift, because
there is no second copy to drift from. shield.py keeps the parts that are cybergod's alone: its
route list, its operator console, its disk-backed evidence store, its Telegram escalation.

WHAT THIS FILE MAY NEVER DO, and a test asserts each one:
  · NO firewall. No iptables, nft or ufw, and no subprocess to reach one. Amnezia VPN shares this
    host and enforcement is HTTP-layer, inside our own process, or it does not happen. StGB
    §202a-§303b / EU 2013/40 / CFAA §1030: we never scan back, connect back, or retaliate.
  · NO network call of any kind. A defence that makes a request is a defence that can hang a site.
  · NO credential. Alerting stays in colt-web (notify.py). Putting a bot token in five repositories
    is the "one value, several homes" defect this estate has already paid for repeatedly.
  · NO Starlette, no FastAPI, no third-party import. Pure ASGI and the standard library, so it adds
    a dependency to nothing.
  · NO state on disk. Everything the shield remembers is in memory, per worker, and evaporates on
    restart. That is a real limitation and it is named in `shield_state()["persistent"]` rather
    than hidden: the long window cannot outlive a deploy here, where in colt-web slow_store.py
    gives it a database. The five-minute rule is unaffected.

FAILS OPEN, ALWAYS. A missing file, a corrupt file, a permission error, a clock problem, an
unexpected exception anywhere: every one of them returns "allow". There is exactly one safe
direction here and a defence that takes six sites down because one JSON file was half-written is
on the wrong side of it.

EVERY LOCAL DECISION EMITS A LINE, to stdout AND to the shared event log, through the same writer
`observe()` uses -- `evt=perseus_shield_*`. colt-web's brain reads those lines and pages the
operator; Loki holds them for the forensics afterwards. A decision nobody can see is not a control.

USAGE (one line in any ASGI app):

    app.add_middleware(perseus_client.Middleware)
"""
import asyncio
import json
import os
import re
import threading
import time

BLOCKLIST = os.environ.get("PERSEUS_BLOCKLIST", "/var/log/colt/perseus_blocklist.json")
RELOAD_S = int(os.environ.get("PERSEUS_RELOAD_S", "30"))
ENABLED = os.environ.get("PERSEUS_ENABLED", "1") != "0"

SERVICE = os.environ.get("SERVICE") or os.environ.get("PERSEUS_SERVICE") or "unknown"

EVENTS = os.environ.get("EVENTS_LOG", "/var/log/colt/events.log")

# ASK WHERE THE LOG ACTUALLY IS -- NEVER ASSUME THE PATH.
# BEAT_DIR used to be hardcoded to "/var/log/colt/perseus_beats". jev-api mounts the shared
# colt_events volume at **/coltevents** (EVENTS_LOG=/coltevents/events.log), so that hardcoded path
# is backed by NO VOLUME there: os.makedirs() SUCCEEDED into the container's own ephemeral overlay,
# the beat was written where nobody reads, and `perseus_beat_unwritable` never fired because nothing
# errored. A silent write to the wrong place is worse than a permission error -- the Fleet page read
# "UNGUARDED" for a project whose middleware was running perfectly.
# The event log is the one path every project has already told us is on the shared volume, so the
# beat belongs beside it. Same rule dbbackup and logship already learned the hard way.
BEAT_DIR = (os.environ.get("PERSEUS_BEATS")
            or os.path.join(os.path.dirname(EVENTS) or "/var/log/colt", "perseus_beats"))
BEAT_S = int(os.environ.get("PERSEUS_BEAT_S", "60"))

HASH_IPS = os.environ.get("PERSEUS_HASH_IPS", "0") == "1"
_SALT = os.environ.get("PERSEUS_SALT", "")

_LOCK = threading.Lock()
_CACHE = {"mtime": 0.0, "loaded": 0.0, "patterns": [], "thresholds": {}, "cycle": 0,
          "beat": 0.0, "checks": 0, "warned": False}


def _i(name, d):
    """An integer from the environment, or the committed default. Never raises."""
    try:
        return int(os.environ.get(name, d))
    except Exception:
        return d


def _on(name, d="1"):
    return str(os.environ.get(name, d)).lower() not in ("0", "off", "false", "no")


# =============================================================================================
# SECTION A -- THE DETECTION TABLE. THE ONE HOME.
#
# Moved here VERBATIM from webapp/backend/app/shield.py on 2026-09-10, comments and all, because
# four projects could not import a file that is not copied to them. shield.py now imports these
# names from this module. Read that file's header for the incident that produced each rule; every
# pattern below was written from evidence of what scanners actually send to THIS estate, and the
# reason a given rule is anchored the way it is, is written beside it.
#
# CHANGING A PATTERN HERE CHANGES IT ON ALL FIVE SITES AT ONCE. That is the point, and it is also
# the hazard: an over-broad rule now reaches five customers instead of one. Every rule that could
# match an ordinary word is root-anchored and exact, and tests/test_perseus_shield.py asserts both
# directions -- what must be caught, and what must never be.
# =============================================================================================

# A HONEYTOKEN IS THE ONLY ZERO-FALSE-POSITIVE SIGNAL WE HAVE. These paths are listed in robots.txt
# as Disallow and are linked from nowhere, so a request for one is either a deliberate scan or a
# robots-ignoring crawler. Either way it is not a visitor. (Thinkst canarytoken doctrine.)
HONEYTOKENS = ("/admin.php", "/wp-login.php", "/.env.bak", "/backup.zip", "/config.json.old")

PROBE_RE = re.compile(
    # /.git /.env /.aws /.ssh — dot-directories.
    # THE NEGATIVE LOOKAHEAD IS LOAD-BEARING. `/.well-known/` is an IANA-registered namespace we
    # serve on purpose: ACME renewal and RFC 9116 security.txt live there. Without the exclusion
    # every Let's Encrypt validation and every security.txt fetch scored `probe_path` at weight 3.
    # It could not be BLOCKED (the prefix is exempt) but it was counted, so our own certificate
    # renewals were inflating the attack figures in the daily digest.
    # An impostor is still caught: `/.well-knownX/` fails the lookahead because it requires the
    # slash, and `/.well-known/x.php` or a traversal underneath it is caught by the rules below.
    r"(?:^|/)\.(?!well-known/)[^/]"
    r"|//"                                 # //slug — a doubled slash is a template artefact
    r"|/\["                                # /[workspace]/ — an UNRENDERED PLACEHOLDER. A human
                                           #   cannot type this; it is a scanner replaying docs.
    r"|\.(?:php|asp|aspx|jsp|cgi|sql|bak|old|db|sqlite|pem|key|log|ini|yml|yaml|env)(?:$|[?/])"
    r"|/(?:wp-|wordpress|phpmyadmin|xmlrpc|cgi-bin|adminer|actuator|struts|vendor/|solr|jenkins)"
    # THE NINETEEN CLASSES MEASURED AGAINST THE REAL MASS-SCANNING CORPUS (OWASP OAT-014, CISA
    # advisories, public honeypot feeds). Written from evidence of what scanners actually send,
    # not from imagination — `analyse_attacks.py` re-runs that comparison against OUR OWN log so
    # the next gap is found the same way.
    r"|/(?:admin|manager/html|cpanel|webadmin|adminpanel)(?:$|[/?])"      # admin consoles
    r"|/(?:swagger|api-docs|graphql|graphiql)|/v\d/api-docs"              # API introspection
    r"|/(?:boaform|goform|HNAP1|hudson|setup\.cgi|shell\?)"              # router / IoT / CI
    r"|/(?:web\.config|server-status|server-info|\.DS_Store|\.npmrc|\.dockercfg)"
    r"|XDEBUG_SESSION|/_ignition|/telescope/|/login\.action"             # debug + RCE chains
    r"|%2e%2e|\.\./|/%2e[a-z]"    # traversal, AND the single-encoded dot: 185.177.72.x
                                  # asked for /%2eenv five times each. One %2e IS ".", so that is
                                  # /.env wearing a costume, and the double-encoded rule missed it.
    r"|/autodiscover/autodiscover\.xml"                                  # Exchange probe (we run none)
    r"|(?:^|/)(?:id_rsa|credentials|dump|backup|shell|cmd|eval)(?:$|[./])"
    r"|(?:^|/)[A-Z_]{3,}\.md$"             # /DOCS.md /IAM.md /README.md at the root: repository
                                           #   documentation we do not serve, a leaked-docs scan.
    r"|/null$"

    # ------------------------------------------------------------------------------------------
    # THE NINE PATHS OUR OWN 14-DAY DIGEST WAS SEEING AND THIS REGEX COULD NOT SCORE (2026-08-22).
    # analyse_attacks.py named them in its "NEW OR UNRECOGNISED" section for days and nobody
    # joined the two up. Measured before the fix: 9 of 17 real attack paths from that digest were
    # invisible to the blocker, which is the whole reason `blocked` kept reading low. The corpus
    # knowing a class is worthless if probe_shape() cannot score it.
    #
    # EVERY PATTERN BELOW IS ANCHORED so it cannot reach a real route. The live route set is
    # main.py::_APP_ROUTES = {"", login, app, privacy, impressum, contact, demo, experience,
    # partners} plus /assets/** (StaticFiles) and /api/** (exempt anyway). test_shield.py asserts
    # all of them against this regex, because a shield that blocks a visitor is worse than none.

    # 1. PHP VERSION SUFFIXES. `.php$` missed /1.php7, /about.php525, /alfa-rex.php7, and the
    #    digest counted 24,069 php probes while these specific ones scored nothing.
    r"|\.php\d{1,4}(?:$|[?/])"

    # 2. VITE DEV-SERVER ARBITRARY FILE READ (CVE-2025-30208 family). /@fs/ is a Vite internal
    #    prefix; /@fs/etc/passwd and /@fs/proc/self/environ were the single most-repeated
    #    unrecognised probe in the digest, from three separate sources. We BUILD with Vite and
    #    serve static files in production, so this cannot succeed here, but a request for it is
    #    unambiguously a scanner: no browser and no human ever emits it.
    r"|(?:^|/)@(?:fs|vite|id)(?:$|/)"

    # 3. CLOUD AND SERVICE CREDENTIALS, by exact filename rather than by extension. Matching
    #    "*.json" would hit the SBOM and any future public document; matching these names cannot.
    r"|(?:^|/)(?:service[-_]?account(?:[-_]?key)?|serviceaccountkey|firebase[-_]?adminsdk"
    r"|firebase|credentials|secrets?|gcp[-_]?key|client[-_]secret"
    r"|application_default_credentials)\.json(?:$|[?/])"
    r"|\.(?:tfstate|tfvars|pfx|p12|jks|keystore|axd)(?:$|[?/])"
    # kubeconfig has NO extension, so it needs a filename rule and not an extension rule. The
    # committed test caught this: it was listed in the extension alternation, where it could never
    # match, which is a rule that looks present and does nothing.
    r"|(?:^|/)(?:kubeconfig|\.git-credentials|id_ed25519|authorized_keys)(?:$|[?/])"

    # 4. BUILD AND DEPLOY ARTEFACTS. Present in a repository, never on a web root.
    r"|(?:^|/)(?:Dockerfile|docker-compose|Procfile|Makefile|Jenkinsfile|Vagrantfile)(?:$|[.?/])"

    # 5. FRAMEWORK DEBUG CONSOLES. Information disclosure by design, which is why scanners want
    #    them. /telescope/ and /_ignition are already above; these are the rest of the family.
    r"|/(?:_?debugbar|_profiler|_debug|elmah\.axd|trace\.axd)(?:$|[/?])"

    # 7. FRAMEWORK AND CLOUD CONFIG FILES, from the 2026-08-26 digest. `/amplifyconfiguration.json`
    #    (AWS Amplify), `/application.properties` and `/appsettings.json` (Spring Boot and .NET)
    #    all carry credentials and none of them was scored. Named files, not an extension rule:
    #    the panel also proposed a bare `\.json$`, which would match `/.well-known/sbom.cdx.json`
    #    that we now serve deliberately. That proposal was REFUSED for exactly that reason.
    r"|(?:^|/)(?:amplifyconfiguration|awsconfiguration|appsettings(?:\.[a-z]+)?"
    r"|application|application-[a-z]+)\.(?:json|properties|ya?ml|config)(?:$|[?/])"

    # 8. WORDPRESS REST API ENUMERATION: /blog/wp/v2/users, /wp-json/wp/v2/users. The `/wp-` rule
    #    above misses the `/blog/wp/v2/` form, which is what the digest actually saw.
    r"|/wp/v2/(?:users|posts|media|categories|tags|pages)"
    r"|/wp-json(?:$|/)"

    # 6. ROOT-LEVEL HEX DIRECTORIES: /1b7e06/ /2ff83958/ /3fa375/. Cache and WAF probing.
    #    DELIBERATELY ROOT-ONLY. The obvious `(?:^|/)[0-9a-f]{5,12}/?$` would also match the LAST
    #    SEGMENT of a legitimate path, and job identifiers are hex, so /app/<jobid> would have
    #    been read as an attack on the operator's own cabinet. Anchor at ^ and the risk is gone.
    r"|^/[0-9a-f]{5,12}/?$"

    # ------------------------------------------------------------------------------------------
    # 9. THE EIGHT PATHS FROM THE 2026-08-29 DIGEST (added 2026-08-29).
    #
    # WHY THEY WERE MISSED IS MORE IMPORTANT THAN THE PATHS. Every rule above matches a dot, an
    # extension or a well-known product name. These eight carry NONE of those: `/env` is `.env`
    # with the dot removed, `/phpinfo` is the classic PHP disclosure page without its extension,
    # and `/Gaia/` and `/WebInterface/` are appliance consoles whose names look like ordinary
    # English words. A rule set built around punctuation cannot see a bare word.
    #
    # ALL EIGHT ARE ROOT-ANCHORED AND EXACT. That is the whole safety argument, and it matters
    # more here than anywhere else in this regex: `/info` and `/environment` ARE plausible routes
    # on somebody's site. They are not routes on OURS (main.py::_APP_ROUTES), and an anchored
    # exact match cannot reach `/api/info`, `/app/environment` or any asset path. Both directions
    # are asserted in test_shield.py against the real route list.
    #
    # `/crusader-404-probe` is DELIBERATELY LEFT OUT even though it appeared alongside these. It
    # arrived from three separate Google Cloud addresses, which is the shape of a commercial
    # scanning service rather than an attacker, and "we do not recognise it" is the honest state
    # until somebody establishes what it is. Absence of evidence is not a detection rule.
    # TWO RULES, NOT ONE, AND THE SPLIT IS THE POINT. `Gaia`, `WebInterface` and `geoserver` are
    # PRODUCT NAMES: unambiguous wherever they appear, so a subpath is allowed and must be, because
    # the real request in the digest was `/geoserver/web/` and an exact rule scored it False.
    # `env`, `environment`, `info`, `phpinfo` and `flight` are ORDINARY WORDS and stay exact at the
    # root: `^/info/?$` cannot reach `/api/info` or `/information`, and a prefix rule would.
    r"|^/(?:Gaia|WebInterface|geoserver)(?:$|[/?])"
    r"|^/(?:env|environment|phpinfo|info|flight)/?$"

    # 10. CLOUD INSTANCE METADATA, the SSRF payoff path. 169.254.169.254 is only reachable from
    #     inside the instance, so a request arriving over the internet for one of these is an
    #     attacker testing whether our front end will proxy it. We run no such proxy, which is
    #     exactly why a request for it is unambiguous: nothing legitimate ever asks.
    r"|(?:^|/)latest/(?:meta-data|user-data|dynamic)(?:$|/)"
    r"|(?:^|/)computeMetadata/(?:v\d|$)"
    r"|(?:^|/)metadata/(?:instance|identity|v1)(?:$|/)",
    re.I)


# ---------------------------------------------------------------------------------------------
# THE CLASS VOCABULARY. One table, used by four things now: the public siege feed, the Fleet page,
# analyse_attacks.py and the Grafana labels. It lived only in analyse_attacks.py, which is a
# repo-root ops script and is NOT copied into the colt-web image - so the feed could not have named
# a lane without a second copy, and a second copy is how ENRICH_MODELS ended up with four homes.
# NOTE this does NOT make the gap analysis circular: that compares this CORPUS against
# probe_shape(), which is a separate regex. The corpus is "what exists"; probe_shape is "what we
# detect". Sharing the vocabulary is what lets the two be compared at all.
CLASSES = [
    ("wordpress",   re.compile(r"(?i)/(wp-|wordpress|xmlrpc)")),
    ("php_probe",   re.compile(r"(?i)\.php(?:$|[?/])")),
    ("env_secrets", re.compile(r"(?i)(?:^|/)\.(env|git|aws|ssh|svn)")),
    ("admin_panel", re.compile(r"(?i)/(admin|manager|phpmyadmin|adminer|cpanel|webadmin)")),
    ("api_docs",    re.compile(r"(?i)/(swagger|openapi|graphql|actuator|\.well-known/openid)")),
    ("shell_rce",   re.compile(r"(?i)(cgi-bin|/shell|/cmd|eval\(|\bbash\b|\bwget\b|\bcurl\b)")),
    ("traversal",   re.compile(r"(\.\./|%2e%2e|\.\.%2f)")),
    ("sqli",        re.compile(r"(?i)(union\s+select|'\s+or\s+1=1|sleep\(|benchmark\()")),
    ("xss",         re.compile(r"(?i)(<script|javascript:|onerror=)")),
    ("backup_file", re.compile(r"(?i)\.(bak|old|sql|zip|tar|gz|db|sqlite|log|ini|ya?ml)(?:$|[?/])")),
    ("docs_leak",   re.compile(r"(?:^|/)[A-Z_]{3,}\.md$")),
    ("template",    re.compile(r"(//|/\[)")),
    ("iot_router",  re.compile(r"(?i)/(boaform|goform|HNAP1|setup\.cgi|hudson|jenkins|solr)")),
    # Added 2026-08-22 alongside the probe_shape patterns. THE TWO MUST MOVE TOGETHER: the corpus
    # is "what exists" and probe_shape is "what we detect", and the gap analysis is only
    # meaningful while both are current. A class here with no scoring rule there is a name for
    # something we still cannot block.
    ("dev_server",  re.compile(r"(?i)(?:^|/)@(fs|vite|id)(?:$|/)")),
    ("cloud_creds", re.compile(r"(?i)(?:^|/)(service[-_]?account|firebase|credentials|secrets?)"
                               r"\.json|\.(tfstate|tfvars|kubeconfig|pfx|p12|jks)(?:$|[?/])")),
    ("build_files", re.compile(r"(?i)(?:^|/)(Dockerfile|docker-compose|Procfile|Makefile"
                               r"|Jenkinsfile|Vagrantfile)(?:$|[.?/])")),
    ("debug_panel", re.compile(r"(?i)/(_?debugbar|_profiler|_debug|elmah\.axd|trace\.axd"
                               r"|_ignition|telescope)(?:$|[/?])")),
    ("hex_spray",   re.compile(r"(?i)^/[0-9a-f]{5,12}/?$")),
    # Added 2026-08-29 with section 9 and 10 of PROBE_RE. THE TWO MUST MOVE TOGETHER, per the
    # note above: a class here with no scoring rule there is a name for something we still cannot
    # block, and a scoring rule with no class here is a block the digest cannot explain.
    # Root-anchored and exact, for the same reason the regex is: these are ordinary words.
    ("bare_secret", re.compile(r"(?i)^/(env|environment|phpinfo|info)/?$")),
    ("appliance_ui", re.compile(r"(?i)^/(Gaia|WebInterface|geoserver)(?:$|[/?])|^/flight/?$")),
    ("cloud_metadata", re.compile(r"(?i)(?:^|/)(latest/(meta-data|user-data|dynamic)"
                                  r"|computeMetadata/v\d|metadata/(instance|identity|v1))(?:$|/)")),
]


def classify(path):
    """Every class a path belongs to, most specific first. [] means it is not attack-shaped."""
    return [name for name, rx in CLASSES if rx.search(path or "")]


def lane_of(path):
    """The single lane the public feed should draw this in, or None."""
    hits = classify(path)
    return hits[0] if hits else None


ESCAPE_RE = re.compile(r"\.\.|%2e|%2f|%5c|\\", re.I)

# Static assets are matched by SHAPE, not by prefix: a real build asset is a filename with a known
# extension, so `/assets/index-a1b2c3.js` is ours and `/assets/.env` is not.
ASSET_RE = re.compile(r"^/(?:assets|media|icons|static)/[\w][\w.\-]*"
                      r"\.(?:js|mjs|css|map|png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|otf|eot"
                      r"|mp4|webm|json|txt)$", re.I)


def norm_path(path):
    """The comparable form of a path: query dropped, trailing slash dropped, never empty."""
    raw = str(path or "/")
    return (raw.split("?")[0] or "/").rstrip("/") or "/"


def is_our_route(path, top=(), app_routes=(), exact=(), app_prefix="app"):
    """True for a page or asset this application serves. Never scored, never blocked.

    A PREFIX EXEMPTION IS A HIDING PLACE UNLESS IT REFUSES TRAVERSAL. The first version returned
    True for anything under `/assets/`, so `/assets/../../.env` was waved through before the
    traversal rule could fire, and the negative test caught it immediately. That is the identical
    defect the `/api/` prefix already caused once, reintroduced in the fix for a different one. No
    legitimate route contains `..` or an encoded slash or dot, so refuse first and match afterwards.

    THE ROUTE LISTS ARE ARGUMENTS, not constants, because this file is copied into five projects
    with five different route sets. shield.py passes cybergod's; the local shield below passes what
    its own project declared in PERSEUS_OUR_ROUTES plus what it has watched this app actually
    serve. An empty list is safe: it only means fewer paths are exempt from SCORING, and scoring is
    not enforcement.
    """
    raw = str(path or "/")
    if ESCAPE_RE.search(raw):
        return False
    p = norm_path(raw)
    if p in exact or path in exact:
        return True
    if ASSET_RE.match(p):                        # a real build asset, matched by SHAPE
        return True
    seg = [s for s in p.strip("/").split("/") if s]
    if not seg:
        return True                              # "/"
    if len(seg) == 1:
        return seg[0] in top
    # Exactly one level under the cabinet prefix, and only a route that cabinet registers.
    return len(seg) == 2 and seg[0] == app_prefix and seg[1] in app_routes


def probe_shape(path, is_ours=None, extra=None):
    """Does the path LOOK like scanner behaviour? Pure pattern, NO enforcement exemptions.

    SEPARATED FROM is_probe_path BECAUSE THE EXEMPTION HAD BECOME A HIDING PLACE. /api/ is never
    blocked (every deploy verifier asserts 401 on /api/me), and the first version returned False
    for anything beneath it -- so /api/wp-login.php, /api/.env and /api/../../etc/passwd scored
    NOTHING AT ALL. An attacker who prefixed every probe with /api/ was invisible to the shield.
    Now the SHAPE is always scored; the EXEMPTION only decides whether we may ACT on that request.

    `is_ours` is the caller's route predicate (shield.py's committed list, or the local shield's).
    `extra` is a set of lowercase paths the operator banned by hand.
    """
    raw = str(path or "/")
    if extra and raw.lower() in extra:
        return True
    # THE QUERY STRING IS ALWAYS SCANNED, EVEN ON OUR OWN PAGES.
    # `/?XDEBUG_SESSION_START=phpstorm` has `/` as its path. The first version of the route
    # exemption stripped the query, saw the homepage, and returned False, so a payload delivered
    # in the query on any legitimate URL became invisible. That is the `/api/` hiding place for
    # the THIRD time in one change: exemption from ACTION kept turning into exemption from
    # OBSERVATION. The path may be ours; the query never is.
    p, _sep, q = raw.partition("?")
    if q and PROBE_RE.search(q):
        return True
    # OUR OWN ROUTES ARE NEVER AN ATTACK SHAPE, and this is checked FIRST.
    # `/app/admin` matched the `/(admin|manager|cpanel|...)` console rule and came back ACTIONABLE,
    # so the administrator moving around their own administration page accumulated probe_path at
    # weight 3 per request and could have tarpitted, then blocked, themselves out of the one page
    # only they can reach. A detector tuned on attacker behaviour has to be checked against OUR
    # behaviour before it can be trusted.
    if is_ours is not None and is_ours(p):
        return False
    return bool(PROBE_RE.search(raw))


def is_honeytoken(path):
    return str(path or "").split("?")[0].lower() in HONEYTOKENS


# =============================================================================================
# SECTION B -- THE LOCAL SHIELD. Autonomous, bounded, reversible, HTTP-layer only.
#
# WHY IT IS LOCAL AND NOT IN THE HUB. The hub runs ONCE A NIGHT. A scanner that arrives at 09:00
# would have had fifteen hours of free enumeration before a pattern about it could be published,
# and on four of the five sites the published pattern list has been EMPTY for the whole life of
# the project. Detection has to be where the request is. The hub still owns everything that LEARNS.
#
# EVERY NUMBER BELOW IS SHIELD'S OR THE HUB'S. Nothing here was invented for this file except the
# three caps named as NEW, and each of those says what it bounds and why.
# =============================================================================================

# THE BOUNDS ARE THE CONTRACT. The hub may tune the values inside these ranges; it can never reach
# the ranges themselves, because they live here in committed code and are enforced by clamp() on
# every read. A model cannot turn the shield off, and it cannot turn it into a self-inflicted
# outage. Copied from shield.py::BOUNDS / DEFAULTS -- the same numbers, on all five sites.
BOUNDS = {
    "tarpit_after":  (3, 25),        # distinct suspicious hits before we start slowing them down
    "block_after":   (6, 60),        # ... before a timed block
    "window_s":      (60, 900),      # observation window
    "block_s":       (300, 86400),   # how long a block lasts (5 min .. 24 h)
    "tarpit_ms":     (250, 8000),    # per-request delay while tarpitting
    "ua_rotation_n": (3, 10),        # distinct client fingerprints from one IP = scanner
}
DEFAULTS = {"tarpit_after": 5, "block_after": 12, "window_s": 300, "block_s": 900,
            "tarpit_ms": 1500, "ua_rotation_n": 3}

# THE HUB PUBLISHES TWO OF THESE UNDER ITS OWN NAMES, and its committed defaults are already the
# same numbers as shield's -- perseus/ruleset.py DEFAULTS: block_minutes 15 (= block_s 900) and
# slow_distinct 12 (= SLOW_DISTINCT 12). Mapping them is therefore not a reinterpretation, it is
# the same value under the name each side already uses. Anything the hub has not published falls
# back to the committed default, never to zero: absence is not a threshold.
_HUB_KEY = {"block_s": ("block_minutes", 60)}
# The hub's own committed range for slow_distinct (perseus/ruleset.py::BOUNDS). Clamped on read
# here for the same reason it is clamped there: a corrupt or hostile file cannot widen it.
SLOW_DISTINCT_BOUNDS = (4, 60)

# THE MASTER SWITCHES.
#
# LOCAL DEFAULTS OFF WHERE A FULL SHIELD ALREADY RUNS. cybergod.ai has webapp/backend/app/shield.py
# sitting beside this file, wired into its own middleware with an operator console, a disk-backed
# evidence store and a Telegram escalation menu. Two independent blockers on one request path would
# double-count nothing but would produce two different verdicts on the same address, and the second
# one has no console to release from. So: if `shield.py` is our sibling on disk, this half stays
# quiet and colt-web keeps its own. Detected from the filesystem rather than from an import,
# because shield.py imports THIS module and an import here would be a cycle. PERSEUS_LOCAL
# overrides in either direction.
def _shield_sibling():
    """True when a full shield.py sits in the same directory as this copy of the client."""
    try:
        return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "shield.py"))
    except Exception:
        return False


LOCAL = _on("PERSEUS_LOCAL", "0" if _shield_sibling() else "1")
# Detection always runs; ENFORCE decides whether a verdict is acted on. Off means the events are
# still emitted as `perseus_shield_would_block`, which is how a new project is watched before it
# is armed -- and it is a state that must be visible, so shield_state() reports it.
ENFORCE = _on("PERSEUS_ENFORCE", "1")

# Paths that must always work NO MATTER WHAT. Identical to shield.py::NEVER_BLOCK_PREFIXES.
#   /.well-known/ — ACME/TLS renewal and RFC 9116. Blocking it turns a scanner into a CERTIFICATE
#                   outage for every visitor of every domain on the box.
#   /api/         — every deploy verifier asserts 401 on /api/me. Authentication is what protects
#                   /api/, not the shield; a 401 is already a refusal. The SHAPE is still scored,
#                   or the prefix becomes a hiding place -- that mistake has been made three times.
NEVER_BLOCK_PREFIXES = ("/.well-known/", "/api/")

# Addresses that may NEVER be blocked. The operator's own, plus anything the project adds.
ALLOW_IPS = {x.strip() for x in os.environ.get("PERSEUS_ALLOW_IPS", "").split(",") if x.strip()}

# VARIETY, NOT VOLUME -- the numbers, and where each came from (shield.py, same names):
#   NF_DISTINCT 6      distinct 404 paths before a miss is evidence at all. Below this it is a
#                      stale bookmark. On 10 Aug two GENUINE visitors produced 439 and 362 404s
#                      each, entirely on our own stale routes, and a VOLUME rule would have
#                      blocked both. A person misses the same few paths; a scanner misses hundreds
#                      of DIFFERENT ones.
#   SLOW_DISTINCT 12   distinct PROBE-shaped paths over 24 hours. Measured on the 2026-08-26
#                      digest: the three biggest sources ran at ~0.55 probe requests per
#                      five-minute window, so a source could enumerate 319 distinct paths and
#                      never once reach the fast threshold. A real visitor with four hundred 404s
#                      records ZERO here, because our own routes are not probe shapes, and that
#                      asymmetry is the entire safety argument.
NF_DISTINCT = _i("PERSEUS_NF_DISTINCT", 6)
SLOW_WINDOW_S = _i("PERSEUS_SLOW_WINDOW_S", 86400)     # 24 hours
SLOW_DISTINCT = _i("PERSEUS_SLOW_DISTINCT", 12)
SLOW_MAX_IPS = _i("PERSEUS_SLOW_MAX_IPS", 4000)
SLOW_MAX_PATHS = _i("PERSEUS_SLOW_MAX_PATHS", 64)      # enough to prove a scan, far short of a log

# THE BLAST CAP. An automatic control that can block everybody is worse than no control.
# The percentage alone is wrong on a quiet site -- with one scanner and one honest visitor,
# blocking the scanner is 50% of the traffic -- so a small ABSOLUTE number of blocks is always
# permitted and the percentage only governs once there are enough of them to be a pattern.
BLAST_CAP = _i("PERSEUS_BLAST_CAP_PCT", 20)
MIN_ABS_BLOCKS = _i("PERSEUS_MIN_ABS_BLOCKS", 5)
MAX_TARPIT_CONCURRENT = _i("PERSEUS_TARPIT_MAX", 24)

# --- the three NEW caps. There is no human in this loop, so every local decision is bounded. -----
# MAX_BLOCKS: a hard ceiling on how many addresses this worker may hold at once, whatever the
#   blast cap says. It is memory bound and outage bound at the same time.
MAX_BLOCKS = _i("PERSEUS_MAX_BLOCKS", 256)
# MAX_WATCHED: how many addresses may be scored at all. Past it we stop recording new ones rather
#   than grow without limit; the input is chosen by the attacker.
MAX_WATCHED = _i("PERSEUS_MAX_WATCHED", 8192)
# AUTH_TTL_S: how long a PROVEN authenticated session keeps its exemption without renewing it.
#   Shorter than block_s so an exemption cannot outlive the block it suppresses by much, long
#   enough that a person reading a page is not re-judged mid-visit.
AUTH_TTL_S = _i("PERSEUS_AUTH_TTL_S", 1800)
# SERVED_MAX / SERVED_TTL_S: the learned route set (below). Bounded and expiring like everything.
SERVED_MAX = _i("PERSEUS_SERVED_MAX", 512)
SERVED_TTL_S = _i("PERSEUS_SERVED_TTL_S", 86400)
# CATCHALL_N: how many PROBE-SHAPED paths this app may answer 2xx to before we conclude it has a
#   catch-all and disarm enforcement entirely. See _learn_served().
CATCHALL_N = _i("PERSEUS_CATCHALL_N", 3)

# WHAT THIS PROJECT SERVES, declared. Comma-separated exact paths, e.g.
# PERSEUS_OUR_ROUTES="/,/login,/pricing,/app/admin". Optional: the learned set below fills in for
# a project that declares nothing. Declaring is still better, because a declaration is available
# on the first request and a learned route is not.
_OUR_EXACT = tuple(norm_path(x) for x in os.environ.get("PERSEUS_OUR_ROUTES", "").split(",")
                   if x.strip()) + (
    # Files a web root serves by convention. Not a route list, a shape list: none of these is a
    # page anyone can be locked out of, and every one of them is requested by ordinary browsers.
    "/robots.txt", "/sitemap.xml", "/favicon.ico", "/manifest.webmanifest", "/sw.js",
    "/healthz", "/health")

_WEIGHT = {"honeytoken": 6, "ua_rotation": 4, "probe_path": 3,
           "method_abuse": 2, "authz_probe": 1, "not_found": 1}

# ---------------------------------------------------------------- state (in memory, per worker)
_hits = {}          # ip -> [(ts, reason), ...]
_fps = {}           # ip -> {fingerprint: ts}
_blocked = {}       # ip -> expires_at
_seen_ips = {}      # ip -> last_seen  (denominator for the blast cap)
_miss = {}          # ip -> {distinct 404 path: ts} — variety separates a scan from a typo
_slow = {}          # key -> {distinct PROBE path: ts}; key is an address or a /24
_served = {}        # normalised path -> ts, for paths THIS app answered 2xx/3xx on
_authed = {}        # ip -> ts of the last PROVEN authenticated response
_recent = {}        # ip -> last few paths, so an emitted decision can show WHAT was asked for
_tarpits = [0]      # concurrent tarpitted requests, list so it is mutable from a closure
_catchall = [False]
_told = {}          # (evt, ip) -> ts, so a steady state is never re-emitted


def net_key(ip):
    """The /24 an address belongs to. Scoring is per /24; BLOCKING stays per address."""
    parts = str(ip or "").split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else ""


def cfg(key):
    """The effective value: committed default, hub tune, env override -- always CLAMPED.

    Clamping on READ rather than on write is deliberate. A published file that is hand-edited, or
    corrupt, or written by a future version with different ideas, still cannot push the shield
    outside the range this file commits to.
    """
    lo, hi = BOUNDS[key]
    v = DEFAULTS[key]
    try:
        hub = _load().get("thresholds") or {}
        name, mult = _HUB_KEY.get(key, (None, 1))
        if name and hub.get(name) is not None:
            v = int(hub[name]) * mult
    except Exception:
        pass
    v = _i("PERSEUS_" + key.upper(), v)
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return DEFAULTS[key]


def slow_distinct():
    """SLOW_DISTINCT, with the hub's published `slow_distinct` if it has published one. Clamped to
    the hub's own committed range, so the file cannot widen what ruleset.py already narrowed."""
    v = SLOW_DISTINCT
    try:
        hub = _load().get("thresholds") or {}
        if hub.get("slow_distinct") is not None:
            v = int(hub["slow_distinct"])
    except Exception:
        pass
    lo, hi = SLOW_DISTINCT_BOUNDS
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return max(lo, min(hi, SLOW_DISTINCT))


_UA_BROWSER = (("Edg", "Edge"), ("OPR", "Opera"), ("Chrome", "Chrome"), ("Firefox", "Firefox"),
               ("Safari", "Safari"), ("curl", "curl"), ("python", "python"), ("Go-http", "go"),
               ("wget", "wget"))
_UA_OS = (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iOS"),
          ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("Linux", "Linux"), ("X11", "Linux"))


def ua_fingerprint(ua):
    """A COARSE client identity: browser + OS + form factor. Cheap, and rotating it is the tell.

    THE EVASION IS THE EVIDENCE. An attacker rotating user agents to defeat per-client rate
    limiting produces the one thing a real visitor never produces: several distinct browser/OS
    identities from a single address in seconds. On the 10 Aug incident one address announced six
    browsers in two seconds, and this alone identified it from its second request.

    Deliberately coarse. A fingerprint that includes the version string would call every Chrome
    auto-update a rotation, and a household behind one NAT address legitimately has three browsers
    -- which is why rotation NEVER convicts alone (see _score).
    """
    s = str(ua or "")
    b = next((name for token, name in _UA_BROWSER if token in s), "-")
    o = next((name for token, name in _UA_OS if token in s), "-")
    d = "mobile" if ("Mobile" in s or "Android" in s or "iPhone" in s) else "desktop"
    return "%s|%s|%s" % (b, o, d)


def _prune(now, window):
    for ip in list(_hits):
        _hits[ip] = [(t, r) for (t, r) in _hits[ip] if now - t < window]
        if not _hits[ip]:
            _hits.pop(ip, None)
    for ip in list(_miss):
        _miss[ip] = {k: t for k, t in _miss[ip].items() if now - t < window}
        if not _miss[ip]:
            _miss.pop(ip, None)
    for ip in list(_fps):
        _fps[ip] = {f: t for f, t in _fps[ip].items() if now - t < window}
        if not _fps[ip]:
            _fps.pop(ip, None)
    for ip, exp in list(_blocked.items()):
        if exp <= now:
            _blocked.pop(ip, None)
            _emit("perseus_shield_expired", ip=ip)
    for ip, t in list(_seen_ips.items()):
        if now - t > 3600:
            _seen_ips.pop(ip, None)
    for ip, t in list(_authed.items()):
        if now - t > AUTH_TTL_S:
            _authed.pop(ip, None)
    for p, t in list(_served.items()):
        if now - t > SERVED_TTL_S:
            _served.pop(p, None)
    for k, t in list(_told.items()):
        if now - t > 3600:
            _told.pop(k, None)


def local_is_ours(path):
    """The local shield's route predicate: what THIS project declared, plus what it serves.

    A LEARNED ROUTE IS A ROUTE WE ANSWERED 2xx OR 3xx ON. That is the only project-independent
    evidence available to a file copied into five different applications: shield.py can hold
    cybergod's committed route list because it is cybergod's file, and this one cannot.
    """
    raw = str(path or "/")
    if ESCAPE_RE.search(raw):
        return False                             # no legitimate route contains .. or an encoded /
    p = norm_path(raw)
    if p == "/" or p in _OUR_EXACT:
        return True
    if ASSET_RE.match(p):
        return True
    return p in _served


def _learn_served(path, status):
    """Remember a path this app actually served, and notice an app that serves EVERYTHING.

    THE CATCH-ALL IS THE ONE THING THAT WOULD MAKE THIS DANGEROUS. An SPA that answers 200 with
    index.html for every unknown path makes "we serve it" meaningless, and a shield that cannot
    tell a served route from an unserved one could refuse a real page. So we MEASURE it: once this
    app has answered 2xx to CATCHALL_N distinct PROBE-SHAPED paths, it has told us it serves
    anything, and local enforcement turns itself off and SAYS SO. Detection and reporting continue.

    That is the fail-open direction, and it is stated rather than hidden, because a feature that
    quietly stops working is the defect this estate has paid for more than any other.
    """
    try:
        code = int(status or 0)
        if not (200 <= code < 400):
            return
        p = norm_path(path)
        if len(_served) < SERVED_MAX or p in _served:
            _served[p] = time.time()
        if _catchall[0]:
            return
        # Counted on the RAW shape -- ignoring the learned set, or this could never fire: the path
        # is already in _served by the line above and local_is_ours would call it ours.
        if not PROBE_RE.search(p):
            return
        n = sum(1 for q in _served if PROBE_RE.search(q))
        if n >= CATCHALL_N:
            _catchall[0] = True
            _emit("perseus_shield_catchall", paths=n,
                  reason="this app answered 2xx to %d probe-shaped paths - it has a catch-all, so "
                         "'a route we serve' cannot be told from 'a path we do not have'. Local "
                         "enforcement is OFF here; detection and reporting continue." % n)
    except Exception:
        pass


def watch(ip, path, status=200, ua="", method="GET", authed_hint=False):
    """Record ONE request and return the reasons it looked hostile. Never raises, fails open.

    This is shield.py::observe under a different name -- `observe()` in this module has always been
    the event WRITER and renaming it would break five projects. Same signals, same weights.
    """
    reasons = []
    if not LOCAL or not ENABLED or not ip or ip in ALLOW_IPS:
        return reasons
    try:
        now = time.time()
        win = cfg("window_s")
        if ip in _seen_ips or len(_seen_ips) < MAX_WATCHED:
            _seen_ips[ip] = now
        elif ip not in _hits:
            return reasons                       # bounded: the input is chosen by the attacker
        if len(_seen_ips) % 64 == 0:
            _prune(now, win)

        _learn_served(path, status)
        if authed_hint and 200 <= int(status or 0) < 400:
            # PROVEN, not claimed. A credential that produced a successful response came from
            # somebody the application itself accepted; a scanner spraying Authorization headers
            # collects 401s and never earns this.
            _authed[ip] = now

        # An exempt path contributes no STATUS signal: /api/me answers 401 to every anonymous
        # caller, so counting that as an authz probe scores ordinary visitors. The path SHAPE is
        # still scored below, or /api/ becomes a hiding place.
        _exempt = str(path or "").lower().startswith(NEVER_BLOCK_PREFIXES)

        if is_honeytoken(path):
            reasons.append("honeytoken")         # zero false positives, by construction
        if probe_shape(path, is_ours=local_is_ours):
            reasons.append("probe_path")
            # THE SLOW WINDOW. Remember the DISTINCT probe paths this address has asked for, so a
            # scanner pacing itself under the five-minute rule still accumulates. Only PROBE-shaped
            # paths are recorded, which is what makes it safe: a real visitor with hundreds of 404s
            # on stale routes of ours records nothing here.
            for k in (ip, net_key(ip)):
                if not k:
                    continue
                if len(_slow) < SLOW_MAX_IPS or k in _slow:
                    seen = _slow.setdefault(k, {})
                    if len(seen) < SLOW_MAX_PATHS or path in seen:
                        seen[norm_path(path)[:200]] = now
        # A 404 ON ONE OF OUR OWN ROUTES IS A STALE LINK, not evidence, however many there are.
        if int(status or 0) == 404 and not _exempt and not local_is_ours(path):
            # A 404 ALONE IS NOT EVIDENCE. VARIETY is the discriminator: a person misses the same
            # few stale paths, a scanner misses hundreds of DIFFERENT ones. So a 404 scores only
            # once this address has missed on NF_DISTINCT DISTINCT paths inside the window.
            d404 = _miss.setdefault(ip, {})
            if len(d404) < SLOW_MAX_PATHS * 2 or norm_path(path) in d404:
                d404[norm_path(path)[:120]] = now
            for k, t in list(d404.items()):
                if now - t > win:
                    d404.pop(k, None)
            if len(d404) >= NF_DISTINCT:
                reasons.append("not_found")
        if int(status or 0) in (401, 403) and not _exempt:
            reasons.append("authz_probe")
        if str(method).upper() in ("PUT", "DELETE", "PATCH", "TRACE", "CONNECT"):
            reasons.append("method_abuse")

        fp = ua_fingerprint(ua)
        seen_fp = _fps.setdefault(ip, {})
        if len(seen_fp) < 32 or fp in seen_fp:
            seen_fp[fp] = now
        if len(seen_fp) >= cfg("ua_rotation_n"):
            reasons.append("ua_rotation")

        if reasons:
            h = _hits.setdefault(ip, [])
            h.append((now, reasons[0]))
            del h[:-200]                         # bounded
            rp = _recent.setdefault(ip, [])
            rp.append(norm_path(path)[:120])
            del rp[:-10]
        return reasons
    except Exception:
        return []                                # fail open, always


def _score(ip, now, win):
    """Weighted hostility for this address inside the window.

    UA ROTATION ONLY COUNTS WHEN SOMETHING ELSE IS ALSO WRONG. Rotation is strong evidence of
    AUTOMATION; it is not by itself evidence of ATTACK. A deploy verifier that sends twelve user
    agents from one address to prove a bot gate works, asking only for legitimate routes, was duly
    blocked by the first version -- and monitoring, uptime checks and CI all look exactly like
    that. On the real 10 Aug incident the rotation arrived WITH four probe paths and a row of
    404s, so requiring corroboration loses nothing there and removes a whole class of false
    positive. Same doctrine as every ownership anchor in the engine: a strong signal still has to
    be corroborated before it is allowed to convict.
    """
    hits = [h for h in _hits.get(ip, ()) if now - h[0] < win]
    base = sum(_WEIGHT.get(r, 1) for (_t, r) in hits if r != "ua_rotation")
    if base <= 0:
        return 0, len(hits)                      # automation on legitimate paths is not an attack
    rot = sum(_WEIGHT["ua_rotation"] for (_t, r) in hits if r == "ua_rotation")
    return base + rot, len(hits)


def blast_ok():
    """Refuse to act when acting would affect too much of the traffic, or too much memory.

    An automatic control that can block everybody is worse than no control: narrow, never wipe.
    """
    if len(_blocked) >= MAX_BLOCKS:
        return False
    if len(_blocked) + 1 <= MIN_ABS_BLOCKS:
        return True
    return (len(_blocked) + 1) * 100.0 / max(1, len(_seen_ips)) <= BLAST_CAP


def _distinct(key, window, now):
    """Distinct probe paths recorded under `key` inside `window`. Prunes as it reads.

    A path this app has since been observed SERVING is dropped rather than counted. Evidence that
    turned out to be a real route of ours is not evidence, and leaving it in would let a project
    convict an address for asking after a page it actually has.
    """
    seen = _slow.get(key)
    if not seen:
        return 0
    cutoff = now - window
    for p in [p for p, ts in seen.items() if ts < cutoff or p in _served]:
        seen.pop(p, None)
    if not seen:
        _slow.pop(key, None)
        return 0
    return len(seen)


def slow_scan(ip, now=None):
    """(own, net) distinct probe paths for this address and for its /24.

    Returns (0, 0) for every ordinary visitor, because only probe-shaped paths are ever recorded.
    """
    try:
        now = now or time.time()
        nk = net_key(ip)
        return _distinct(ip, SLOW_WINDOW_S, now), (_distinct(nk, SLOW_WINDOW_S, now) if nk else 0)
    except Exception:
        return 0, 0


def _decide_raw(ip, path):
    """ALLOW | TARPIT | BLOCK for this request. Pure function of recorded state. Never raises.

    THIS IS THE SCORING, NOT THE ENFORCEMENT. `decide()` wraps it and applies the exemptions that
    keep a human from being locked out of the product. Kept separate on purpose: the evidence this
    records must not change just because we declined to act on it.
    """
    try:
        if not LOCAL or not ENABLED or not ip or ip in ALLOW_IPS:
            return "ALLOW", ""
        if _catchall[0]:
            return "ALLOW", "this app has a catch-all - local enforcement is off here"
        if str(path or "").lower().startswith(NEVER_BLOCK_PREFIXES):
            return "ALLOW", "never-block prefix (ACME / security.txt / API)"
        now = time.time()
        exp = _blocked.get(ip, 0)
        if exp > now:
            # DELIBERATELY SILENT. The decision line was emitted when the block was TAKEN; a held
            # address may make hundreds of requests and re-emitting on each one is how the line
            # that matters gets read past. The per-request record still exists: the middleware
            # writes `evt=http status=429` for every refusal through the same writer, so the brain
            # sees each one and the decision line explains why they are happening.
            return "BLOCK", "already blocked for %ds more" % int(exp - now)

        # THE SLOW SCAN, checked BEFORE the fast score. Distinct probe paths over 24 hours, which
        # is the evidence a five-minute window throws away. A source that has asked for a dozen
        # different probe paths in a day has proved what it is, however patiently it did so.
        # THE /24 IS EVIDENCE HERE AND A DECISION ONLY IN colt-web. shield.py can convict on a
        # network horizon because it has a fortnight of evidence in slow_store.py and a Telegram
        # button to release from. This file has neither: its memory dies with the worker, so a
        # fourteen-day rule could not fire honestly, and a /24 is up to 256 addresses that nobody
        # here can un-block by hand. So the neighbourhood count is COUNTED and REPORTED -- it goes
        # out with the block line and colt-web's brain can act on it -- and it never convicts
        # locally. Corroboration before conviction, and no un-releasable decision without a human.
        own, net = slow_scan(ip, now)
        want = slow_distinct()
        if own >= want:
            why = "%d distinct probe paths in %dh - a low-and-slow scan" % (own, SLOW_WINDOW_S // 3600)
            if not blast_ok():
                _emit("perseus_shield_refused", ip=ip, distinct=own, net=net,
                      reason="blast cap on a slow scan: %d held of %d seen"
                             % (len(_blocked), len(_seen_ips)))
                return "TARPIT", "blast cap reached - slowing instead of blocking"
            if ENFORCE:
                _blocked[ip] = now + cfg("block_s")
                _emit("perseus_shield_block", ip=ip, rule="slow_scan", distinct=own, net=net,
                      seconds=cfg("block_s"), window_s=SLOW_WINDOW_S,
                      paths=sorted(_slow.get(ip, {}))[:8], why=why)
                return "BLOCK", why
            _emit("perseus_shield_would_block", ip=ip, rule="slow_scan", distinct=own, net=net,
                  why=why)
            return "TARPIT", "enforcement off - would have blocked a slow scan"

        score, n = _score(ip, now, cfg("window_s"))
        if score >= cfg("block_after"):
            if not blast_ok():
                _emit("perseus_shield_refused", ip=ip, score=score,
                      reason="blast cap: %d blocked of %d seen addresses exceeds %d%%"
                             % (len(_blocked), len(_seen_ips), BLAST_CAP))
                return "TARPIT", "blast cap reached - slowing instead of blocking"
            if ENFORCE:
                _blocked[ip] = now + cfg("block_s")
                _emit("perseus_shield_block", ip=ip, rule="fast", score=score, hits=n,
                      seconds=cfg("block_s"), reasons=sorted({r for (_t, r) in _hits.get(ip, ())}),
                      paths=sorted(set(_recent.get(ip, ())))[:5])
                return "BLOCK", "score %d over %d" % (score, cfg("block_after"))
            _emit("perseus_shield_would_block", ip=ip, rule="fast", score=score, hits=n)
            return "TARPIT", "enforcement off - would have blocked"
        if score >= cfg("tarpit_after"):
            return "TARPIT", "score %d over %d" % (score, cfg("tarpit_after"))
        return "ALLOW", ""
    except Exception:
        return "ALLOW", ""


def decide(ip, path, authed=False, credential=False):
    """ALLOW | TARPIT | BLOCK, with the exemptions that stop us locking a human out.

    THE OPERATOR WAS LOCKED OUT OF HIS OWN ADMIN CONSOLE by the first version of this logic in
    colt-web, silently, with a page saying the route did not exist. These are the exemptions that
    followed, and this file is stricter than shield.py on the first of them because the projects it
    is copied into have no operator console to release anybody from:

    1. AN AUTHENTICATED SESSION IS NEVER BLOCKED AND NEVER TARPITTED. `authed` here means PROVEN --
       this address presented a credential and the APPLICATION ITSELF answered 2xx to it. A scanner
       cannot manufacture that; a scanner spraying Authorization headers collects 401s, which score
       authz_probe. shield.py exempts such a session from BLOCK only; here it is exempt from the
       tarpit too, because a delay on a logged-in customer is a support ticket in a product whose
       operator is not watching the logs of four of these five sites.

    2. A CREDENTIAL WITHOUT PROOF downgrades a block to a tarpit and no further. This is the path
       back for a REAL user whose address was blocked while they were away: their browser sends the
       session cookie, they are slowed by one and a half seconds instead of refused, the response
       is a 2xx, and from that moment they are proven and exempt. Without it, a block would be a
       permanent lockout for the one person we most need not to lock out. It is not a full
       exemption, because a cookie is trivially forged and a full exemption on a forgeable header
       would be a bypass anyone could use.

    3. A ROUTE WE ACTUALLY SERVE IS NEVER BLOCKED, only slowed. The shield exists to stop people
       asking for things we do not have; refusing a real page is what locks a person out of the
       product, and the tarpit already answers the throughput half of that concern.

    THE BLOCK IS STILL RECORDED EITHER WAY. `_decide_raw` sets `_blocked[ip]`, so the address stays
    blocked for the probe paths that convicted it and the evidence is unchanged. Only the response
    to a legitimate request is softened. An exemption that erased the finding would be a hiding
    place, which is the defect this codebase has already paid for four times.
    """
    try:
        if authed:
            # Checked BEFORE the scoring, because "never tarpitted" cannot be implemented after a
            # verdict that only distinguishes BLOCK. The evidence is still recorded by watch().
            v, why = _decide_raw(ip, path)
            if v != "ALLOW":
                _once("perseus_shield_exempt", ip, path=norm_path(path)[:120],
                      reason="authenticated session", would_have=why)
            return "ALLOW", ("authenticated session - never blocked or slowed (%s)" % why
                             if v != "ALLOW" else "")
        verdict, why = _decide_raw(ip, path)
        if verdict != "BLOCK":
            return verdict, why
        if credential:
            _once("perseus_shield_credential", ip, path=norm_path(path)[:120],
                  reason="a credential was presented but has not yet been accepted by this app",
                  would_have=why)
            return "TARPIT", "unproven credential - slowed, not blocked (%s)" % why
        if local_is_ours(path):
            _once("perseus_shield_exempt", ip, path=norm_path(path)[:120],
                  reason="a route we serve", would_have=why)
            return "TARPIT", "our own route - slowed, not blocked (%s)" % why
        return verdict, why
    except Exception:
        return "ALLOW", ""                       # fail open, always


def tarpit_seconds():
    """How long to stall, or 0 when too many stalls are already in flight.

    A NAIVE TARPIT IS A SELF-INFLICTED DENIAL OF SERVICE: every stalled request holds a connection,
    so a scanner opening hundreds of them exhausts the server rather than itself. The concurrency
    cap is what makes this safe -- past the cap we simply answer immediately.
    """
    if _tarpits[0] >= MAX_TARPIT_CONCURRENT:
        return 0.0
    return cfg("tarpit_ms") / 1000.0


def enter_tarpit():
    _tarpits[0] += 1


def leave_tarpit():
    _tarpits[0] = max(0, _tarpits[0] - 1)


def is_blocked(ip):
    """Is this address currently held? A measured fact, not an inference from a status code."""
    try:
        if not ip or str(ip) in ALLOW_IPS:
            return False
        return _blocked.get(str(ip), 0) > time.time()
    except Exception:
        return False


def unblock(ip):
    """Release: lift the block AND forgive the history that caused it. Reversible by design.

    Clearing the history is the whole point, not tidiness. A version that popped only the timer
    re-scored the same accumulated hits on the very next request, sailed past the threshold again
    and re-blocked instantly -- a hand brake that did nothing. Releasing somebody means forgiving
    what they did, or it is not a release. The /24's evidence is deliberately kept: forgiving one
    host must not forgive 255 neighbours.
    """
    was = _blocked.pop(str(ip), None) is not None
    for d in (_hits, _fps, _slow, _miss, _recent):
        d.pop(str(ip), None)
    _emit("perseus_shield_unblock", ip=ip, was_blocked=was)
    return was


def shield_state():
    """What the local shield currently believes. Read by status() and by anything that asks."""
    now = time.time()
    return {
        "local": LOCAL, "enforcing": ENFORCE, "catchall": _catchall[0],
        "sibling_shield": _shield_sibling(),
        # NAMED, NOT HIDDEN. There is no database here, so the 24-hour window restarts with the
        # worker. The five-minute rule is unaffected; the low-and-slow rule is weaker than
        # cybergod's, which has slow_store.py behind it.
        "persistent": False,
        "config": {k: cfg(k) for k in BOUNDS},
        "bounds": {k: list(v) for k, v in BOUNDS.items()},
        "slow_distinct": slow_distinct(), "nf_distinct": NF_DISTINCT,
        "blocked": {ip: int(exp - now) for ip, exp in _blocked.items() if exp > now},
        "watching": len(_hits), "seen_ips_1h": len(_seen_ips),
        "served_routes": len(_served), "authed_sessions": len(_authed),
        "blast_cap_pct": BLAST_CAP, "max_blocks": MAX_BLOCKS,
        "tarpits_in_flight": _tarpits[0],
    }


# =============================================================================================
# SECTION C -- the hub blocklist client, the event writer, and the ASGI middleware.
# =============================================================================================

def _ident(ip):
    if not HASH_IPS or not ip:
        return ip or ""
    import hashlib
    return "h:" + hashlib.sha256((_SALT + str(ip)).encode("utf-8")).hexdigest()[:16]


def _load():
    """Re-read only when the file changed, and at most every RELOAD_S. A blocklist consulted on
    every request must never become a disk read on every request."""
    now = time.time()
    if now - _CACHE["loaded"] < RELOAD_S:
        return _CACHE
    with _LOCK:
        _CACHE["loaded"] = now
        try:
            st = os.stat(BLOCKLIST)
            if st.st_mtime == _CACHE["mtime"]:
                return _CACHE
            with open(BLOCKLIST, encoding="utf-8") as fh:
                doc = json.load(fh)
            pats = []
            for p in doc.get("patterns") or []:
                try:
                    pats.append((p.get("id"), re.compile(p["pattern"], re.I)))
                except Exception:
                    continue        # one bad pattern must not discard the rest
            _CACHE.update(mtime=st.st_mtime, patterns=pats, cycle=doc.get("cycle", 0),
                          thresholds=doc.get("thresholds") or {})
        except Exception:
            pass                    # keep whatever we had; never clear on a read failure
    return _CACHE


def _write(rec):
    """THE ONE WRITER. stdout first, then the shared event log. Never raises, says so once.

    stdout first because it costs nothing and the docker json-file driver scrapes it into Loki, so
    a project whose shared-volume write is broken is still observable. That accident is the only
    reason the jobhuntwow abuse could be reconstructed at all -- used deliberately this time.

    jobhuntwow ran for its whole life with `except: pass` around this write while a
    UID-10001-vs-root permission bug silently discarded every line. An observability write that
    swallows its own failure is a self-inflicted blind spot, so the first failure PRINTS.
    """
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except Exception:
        return
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        if not _CACHE["warned"]:
            _CACHE["warned"] = True
            try:
                print(json.dumps({"evt": "events_log_unwritable", "service": SERVICE,
                                  "file": EVENTS, "err": repr(exc)[:160]}), flush=True)
            except Exception:
                pass


def _emit(evt, ip=None, **fields):
    """EVERY LOCAL DECISION LEAVES A LINE, through the same writer as the access log.

    Same shape, same file, same stdout as `evt=http`, so colt-web's brain and Loki both see it
    without either learning a new format. No Telegram, no token, no network: alerting lives in
    colt-web and this file must never learn how to page anybody.
    """
    rec = {"evt": evt, "ts": int(time.time()), "service": SERVICE}
    if ip is not None:
        rec["ip"] = _ident(ip)
    rec.update(fields)
    _write(rec)


def _once(evt, ip, **fields):
    """Edge-triggered: one line per (event, address) per hour.

    A warning that fires on every request trains the operator to read past the one that matters,
    and a steady state must never re-page. The exempt line in particular fires on every request a
    logged-in person makes.
    """
    try:
        now = time.time()
        key = (evt, str(ip))
        if now - _told.get(key, 0) < 3600:
            return
        _told[key] = now
    except Exception:
        pass
    _emit(evt, ip=ip, **fields)


def _beat(cycle):
    """WRITE A HEARTBEAT SO 'IS THE SIDECAR CONNECTED' IS A MEASURED FACT.

    The operator asked why he sees nothing from jev.best, jobhuntwow and polara. The answer was that
    the client was copied into those projects and imported by NOTHING -- correct code, wired to no
    request path, which this repository has already recorded as 'a control that is correct and
    unreachable is not a control'. Nothing could have told him, because nothing was reporting.

    So the client leaves a trace: one small JSON file per service on the volume the hub already
    reads, written at most once a minute (never per request), naming the service, the blocklist
    cycle it is actually running, whether the LOCAL shield is armed, and how many addresses it is
    currently holding. Absence of a file then means exactly one thing: that project is not running
    this code.

    Fails open and silent, like everything else here."""
    now = time.time()
    if now - _CACHE["beat"] < BEAT_S:
        return
    _CACHE["beat"] = now
    rec = {"evt": "perseus_beat", "service": SERVICE, "ts": int(now), "cycle": cycle,
           "checks": _CACHE["checks"], "pid": os.getpid(), "dir": BEAT_DIR,
           # THE LOCAL SHIELD REPORTS ITSELF. A defence nobody has seen working is off, so the
           # heartbeat carries whether it is armed and what it has done -- otherwise the Fleet page
           # would show a connected sidecar and say nothing about whether it can act.
           "local": LOCAL, "enforcing": ENFORCE, "catchall": _catchall[0],
           "blocked": len(_blocked), "watching": len(_hits)}
    try:
        print(json.dumps(rec), flush=True)
    except Exception:
        pass
    try:
        os.makedirs(BEAT_DIR, exist_ok=True)
        tmp = os.path.join(BEAT_DIR, ".%s.%d" % (SERVICE, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, os.path.join(BEAT_DIR, "%s.json" % SERVICE))
    except Exception as exc:
        # SAY SO ONCE. A swallowed heartbeat failure is indistinguishable from "this project never
        # deployed the sidecar", and that is exactly what the Fleet page showed for jobhuntwow after
        # a PERFECT deploy: the container runs as uid 10001 and os.makedirs() inside a root-owned
        # 0755 directory on the shared volume raises PermissionError. Silence sent the operator
        # looking for a deploy bug that did not exist.
        if not _CACHE.get("beat_warned"):
            _CACHE["beat_warned"] = True
            try:
                # NOTE: no os.getuid() here -- it is POSIX-only and this module is imported by the
                # test suite, which runs on Windows. The directory and the error name the fault.
                print(json.dumps({"evt": "perseus_beat_unwritable", "service": SERVICE,
                                  "dir": BEAT_DIR, "err": repr(exc)[:160]}), flush=True)
            except Exception:
                pass


def check(ip, path):
    """(allowed, retry_after, reason). Never raises, never blocks on IO, fails OPEN.

    The HUB's published patterns only. The local shield is `decide()`, and the middleware asks both
    -- the hub first, because a rule that four models agreed on and a promotion gate approved is a
    stronger statement than anything one worker's memory can hold.
    """
    if not ENABLED:
        return True, 0, ""
    try:
        c = _load()
        _CACHE["checks"] += 1
        _beat(c.get("cycle"))
        for rid, rx in c["patterns"]:
            if rx.search(path or ""):
                return False, 60, "perseus rule %s (cycle %s)" % (rid, c.get("cycle"))
    except Exception:
        return True, 0, ""
    return True, 0, ""


def observe(ip, path, status=200, ms=0, ua="", ref="", method="GET"):
    """REPORT WHAT HAPPENED, so the brain can alert on it.

    The client could once only BLOCK; it had no way to say a word about who arrived. It now writes
    the SAME `evt=http` line colt-web's telemetry writes, to the SAME shared events log, stamped
    with this project's SERVICE name.

    THE ALERTING STAYS IN ONE PLACE. This does not send Telegram messages and must never learn how:
    that would put a bot token in five repositories, which is the "one value, several homes" defect
    this estate has already paid for repeatedly. colt-web owns notify.py, alerts.py and the rules,
    reads these lines, and pages the operator. One brain, thin clients.

    AN IP IS PERSONAL DATA (GDPR; CJEU C-582/14 Breyer). `PERSEUS_HASH_IPS=1` stores a salted hash
    instead, which keeps correlation and drops the identifier. Off by default because the operator
    asked for forensics, exactly as colt-web is configured.
    """
    if not ENABLED:
        return
    _write({"evt": "http", "ts": int(time.time()), "service": SERVICE,
            "ip": _ident(ip), "method": method, "path": (path or "")[:200],
            "status": int(status or 0), "ms": int(ms or 0),
            "ua": (ua or "")[:180], "ref": (ref or "")[:180]})


# A credential was PRESENTED. Not proof of anything -- a header is attacker-controlled, which is
# why `watch()` only records an authenticated session once the APPLICATION answered 2xx to one.
# Deliberately loose (any cookie that looks like a session, or any Authorization header): a false
# positive here costs a scanner a tarpit instead of a block, and a false negative locks out a
# customer. There is one safe direction and this is it.
# COMPILED INSIDE A try, because the pattern can come from the environment and a bad one would
# raise at IMPORT time -- which would not degrade this file, it would delete it from five request
# paths at once. An operator typo must cost the loose default, never the module.
_AUTH_DEFAULT = r"(?i)(session|auth|token|sid|login|jwt)"
try:
    _AUTH_COOKIE_RE = re.compile(os.environ.get("PERSEUS_AUTH_COOKIES") or _AUTH_DEFAULT)
except Exception:
    _AUTH_COOKIE_RE = re.compile(_AUTH_DEFAULT)


def credential_hint(headers):
    """True when this request carries something that could be a session. Never raises."""
    try:
        if headers.get("authorization"):
            return True
        return bool(_AUTH_COOKIE_RE.search(headers.get("cookie") or ""))
    except Exception:
        return False


class Middleware:
    """ONE LINE PER PROJECT: `app.add_middleware(perseus_client.Middleware)`.

    Pure ASGI, no Starlette import, so it works in any ASGI app and adds no dependency. It asks the
    hub's published list, then the local shield, then reports what happened. Anything that raises
    here is swallowed: a defence that 500s the site it protects is worse than no defence.

    THE ORDER OF THE THREE IS THE DESIGN. Hub first (four models and a promotion gate agreed),
    local second (this worker's own eyes), report always -- including for the requests we refused,
    or the refusals would be the one thing the brain could not see.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        t0 = time.time()
        path = scope.get("path") or "/"
        method = scope.get("method", "GET")
        hdr = {}
        try:
            hdr = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in (scope.get("headers") or [])}
        except Exception:
            pass
        # Exactly one proxy sits in front of every site on this box, so the FIRST forwarded entry
        # is the client. CF-Connecting-IP wins if Cloudflare is ever put in front.
        ip = (hdr.get("cf-connecting-ip")
              or (hdr.get("x-forwarded-for", "").split(",")[0].strip())
              or ((scope.get("client") or ("", 0))[0]))
        ua = hdr.get("user-agent", "")
        cred = credential_hint(hdr)

        try:
            allowed, retry, why = check(ip, path)
        except Exception:
            allowed, retry, why = True, 0, ""
        if not allowed:
            await self._refuse(send, retry)
            observe(ip, path, 429, (time.time() - t0) * 1000, ua, hdr.get("referer", ""), method)
            watch(ip, path, 429, ua, method, cred)
            return

        try:
            verdict, why2 = decide(ip, path, authed=_authed.get(ip, 0) > time.time() - AUTH_TTL_S,
                                   credential=cred)
        except Exception:
            verdict, why2 = "ALLOW", ""
        if verdict == "BLOCK":
            # 429 AND A Retry-After, NEVER A 404. A 404 tells a real person the page does not
            # exist, which is exactly how the operator lost an hour to his own admin console. A 429
            # with a retry window is the truth: we are refusing you, for this long.
            await self._refuse(send, cfg("block_s"))
            observe(ip, path, 429, (time.time() - t0) * 1000, ua, hdr.get("referer", ""), method)
            watch(ip, path, 429, ua, method, cred)
            return
        if verdict == "TARPIT":
            secs = tarpit_seconds()
            if secs > 0:
                enter_tarpit()
                try:
                    await asyncio.sleep(secs)
                except Exception:
                    pass
                finally:
                    leave_tarpit()
                _once("perseus_shield_tarpit", ip, path=norm_path(path)[:120],
                      seconds=round(secs, 3), why=why2)

        status = {"code": 0}

        async def _send(msg):
            if msg.get("type") == "http.response.start":
                status["code"] = msg.get("status", 0)
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            observe(ip, path, status["code"], (time.time() - t0) * 1000, ua,
                    hdr.get("referer", ""), method)
            watch(ip, path, status["code"], ua, method, cred)

    @staticmethod
    async def _refuse(send, retry):
        body = b'{"error":"rate limited"}'
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"), (b"retry-after", str(int(retry)).encode())]})
        await send({"type": "http.response.body", "body": body})


def thresholds():
    """The hub's current numbers, for a project that wants to use them for its own rate limiting
    instead of hard-coding its own. Empty dict means 'the hub has not published yet': the caller
    keeps its committed defaults rather than treating absence as zero."""
    try:
        return dict(_load().get("thresholds") or {})
    except Exception:
        return {}


def status():
    c = _load()
    return {"enabled": ENABLED, "file": BLOCKLIST, "cycle": c.get("cycle"),
            "patterns": len(c.get("patterns") or []),
            "age_s": int(time.time() - c["mtime"]) if c["mtime"] else None,
            "shield": shield_state()}
