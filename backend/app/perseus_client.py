"""The ONLY file copied into each project. Stateless, stdlib-only, ~40 lines of logic.

COPY THIS, NOT THE BRAIN. Everything that decides -- scoring, the panel, promotion, abuse
reporting -- lives in the hub, in one place, and changes there. This file only READS what the hub
published. It has no state of its own to drift, no thresholds of its own to go stale, and no
network call, so it cannot introduce a failure mode into a request path.

HOW IT REACHES THE APP: the hub writes `perseus_blocklist.json` to a volume every project already
mounts. A file cannot be down, cannot rate-limit us, and needs no token. That is deliberate: an
HTTP call to a central service would put the hub in the blast radius of every site it protects.

FAILS OPEN, ALWAYS. A missing file, a corrupt file, a permission error, a clock problem: every one
of them returns "allow". The sibling incident cost money; a defence that takes six sites down
because one JSON file was half-written would cost more. There is exactly one safe direction here.

USAGE (three lines in any FastAPI app):

    from perseus_client import check
    ok, retry, why = check(client_ip, request.url.path)
    if not ok: return JSONResponse({"error": "rate limited"}, 429, {"Retry-After": str(retry)})
"""
import json
import os
import re
import threading
import time

BLOCKLIST = os.environ.get("PERSEUS_BLOCKLIST", "/var/log/colt/perseus_blocklist.json")
RELOAD_S = int(os.environ.get("PERSEUS_RELOAD_S", "30"))
ENABLED = os.environ.get("PERSEUS_ENABLED", "1") != "0"

BEAT_DIR = os.environ.get("PERSEUS_BEATS", "/var/log/colt/perseus_beats")
BEAT_S = int(os.environ.get("PERSEUS_BEAT_S", "60"))
SERVICE = os.environ.get("SERVICE") or os.environ.get("PERSEUS_SERVICE") or "unknown"

EVENTS = os.environ.get("EVENTS_LOG", "/var/log/colt/events.log")
HASH_IPS = os.environ.get("PERSEUS_HASH_IPS", "0") == "1"
_SALT = os.environ.get("PERSEUS_SALT", "")

_LOCK = threading.Lock()
_CACHE = {"mtime": 0.0, "loaded": 0.0, "patterns": [], "thresholds": {}, "cycle": 0,
          "beat": 0.0, "checks": 0, "warned": False}


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


def _beat(cycle):
    """WRITE A HEARTBEAT SO 'IS THE SIDECAR CONNECTED' IS A MEASURED FACT.

    The operator asked why he sees nothing from jev.best, jobhuntwow and polara. The answer was that
    the client was copied into those projects and imported by NOTHING -- correct code, wired to no
    request path, which this repository has already recorded as 'a control that is correct and
    unreachable is not a control'. Nothing could have told him, because nothing was reporting.

    So the client now leaves a trace. One small JSON file per service on the volume the hub already
    reads, written at most once a minute (never per request), naming the service, the blocklist
    cycle it is actually running, and when it last checked something. Absence of a file then means
    exactly one thing: that project is not running this code.

    Fails open and silent, like everything else here: a defence that crashes a site because a log
    directory is read-only is worse than no defence."""
    now = time.time()
    if now - _CACHE["beat"] < BEAT_S:
        return
    _CACHE["beat"] = now
    try:
        os.makedirs(BEAT_DIR, exist_ok=True)
        tmp = os.path.join(BEAT_DIR, ".%s.%d" % (SERVICE, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"service": SERVICE, "ts": int(now), "cycle": cycle,
                       "checks": _CACHE["checks"], "pid": os.getpid()}, fh)
        os.replace(tmp, os.path.join(BEAT_DIR, "%s.json" % SERVICE))
    except Exception as exc:
        # SAY SO ONCE. A swallowed heartbeat failure is indistinguishable from "this project never
        # deployed the sidecar", and that is exactly what the Fleet page showed for jobhuntwow after
        # a PERFECT deploy: the container runs as uid 10001 and os.makedirs() inside a root-owned
        # 0755 directory on the shared volume raises PermissionError. Silence sent the operator
        # looking for a deploy bug that did not exist. Same rule observe() already follows.
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
    """(allowed, retry_after, reason). Never raises, never blocks on IO, fails OPEN."""
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
    """REPORT WHAT HAPPENED, so the brain can alert on it. This is the half that was missing.

    The operator asked why he sees nothing from jev.best, jobhuntwow or polara. The client could
    only ever BLOCK; it had no way to say a word about who arrived. So it now writes the SAME
    `evt=http` line colt-web's telemetry writes, to the SAME shared events log, stamped with this
    project's SERVICE name.

    THE ALERTING STAYS IN ONE PLACE. This does not send Telegram messages and must never learn how:
    that would put a bot token in five repositories, which is the "one value, several homes" defect
    this estate has already paid for repeatedly. colt-web owns notify.py, alerts.py and the rules,
    reads these lines, and pages the operator. One brain, thin clients -- the same doctrine as the
    blocklist, extended from enforcement to observation.

    AN IP IS PERSONAL DATA (GDPR; CJEU C-582/14 Breyer). `PERSEUS_HASH_IPS=1` stores a salted hash
    instead, which keeps correlation and drops the identifier. Off by default because the operator
    asked for forensics, exactly as colt-web is configured.

    FAILS OPEN AND SAYS SO ONCE. jobhuntwow ran for its whole life with `except: pass` around this
    write while a UID-10001-vs-root permission bug silently discarded every line -- an observability
    write that swallows its own failure is a self-inflicted blind spot, so the first failure prints.
    """
    if not ENABLED:
        return
    try:
        rec = {"evt": "http", "ts": int(time.time()), "service": SERVICE,
               "ip": _ident(ip), "method": method, "path": (path or "")[:200],
               "status": int(status or 0), "ms": int(ms or 0),
               "ua": (ua or "")[:180], "ref": (ref or "")[:180]}
        line = json.dumps(rec, ensure_ascii=False)
        # stdout first: it costs nothing and the docker log driver scrapes it, so a project whose
        # shared-volume write is broken is still observable. That accident is the only reason the
        # jobhuntwow abuse could be reconstructed at all.
        print(line, flush=True)
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


class Middleware:
    """ONE LINE PER PROJECT: `app.add_middleware(perseus_client.Middleware)`.

    Pure ASGI, no Starlette import, so it works in any ASGI app and adds no dependency. It blocks
    what the hub published and reports every request. Anything that raises here is swallowed: a
    defence that 500s the site it protects is worse than no defence."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        t0 = time.time()
        path = scope.get("path") or "/"
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
        try:
            allowed, retry, why = check(ip, path)
        except Exception:
            allowed, retry, why = True, 0, ""
        if not allowed:
            body = b'{"error":"rate limited"}'
            await send({"type": "http.response.start", "status": 429, "headers": [
                (b"content-type", b"application/json"), (b"retry-after", str(retry).encode())]})
            await send({"type": "http.response.body", "body": body})
            observe(ip, path, 429, (time.time() - t0) * 1000,
                    hdr.get("user-agent", ""), hdr.get("referer", ""), scope.get("method", "GET"))
            return
        status = {"code": 0}

        async def _send(msg):
            if msg.get("type") == "http.response.start":
                status["code"] = msg.get("status", 0)
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            observe(ip, path, status["code"], (time.time() - t0) * 1000,
                    hdr.get("user-agent", ""), hdr.get("referer", ""), scope.get("method", "GET"))


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
            "age_s": int(time.time() - c["mtime"]) if c["mtime"] else None}
