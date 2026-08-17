#!/usr/bin/env python3
"""quorum.py - 2 soldiers + 2 auditors read ONE staging evidence file and write the verdict digest.

TWO EXECUTION CONTEXTS, ON PURPOSE:

  1. INSIDE jhw-web ON THE STAGING DROPLET (the panel):
         docker exec -i jhw-web python3 /opt/stagegate/quorum.py < evidence.json
     It runs there because DO_INFERENCE_KEY lives in /opt/jobhuntwow/.env on the droplet and
     never on the operator's PC. A script that needs inference has to execute where the key
     already is. (The sibling project learned this the expensive way: its catalogue check printed
     "catalog unavailable - skipping" on EVERY run for weeks because it needed a key it could not
     see. A check that cannot see its subject is not a check.)

  2. ON THE OPERATOR'S PC (the pure functions):
         import quorum; quorum.decide_from_verdict(v)
     Everything outside `ask_one`/`main` is stdlib-only and imports nothing from the app, so
     `deploy/stagegate/gate.py` and `tests/` can exercise the governance rules with no droplet,
     no key and no network. Every app import in this file is LAZY, inside a function, for exactly
     that reason.

CONTRACT
    stdin  : evidence JSON  {host, checks:[{name, ok, detail}], expected:[name...], reboot:{...}}
    stdout : verdict JSON   {gate, checks_total, checks_failed, reviews:[...], answered, dissent,
                             digest}

WHO DECIDES WHAT - the load-bearing design decision
---------------------------------------------------
`gate` is computed from the DETERMINISTIC checks alone. The models never set it. They read the
same evidence and write the REASONING, the risk list and any objection worth a human's attention.

That is CLAUDE.md standing rule 4 ("the LLM assists, it does not decide side effects"), and it is
not timidity - it is the only arrangement that fails safely in BOTH directions:
  * a 429, an outage or a truncated answer cannot block a correct release;
  * a hallucinated objection cannot stall a good deploy;
  * and a model feeling agreeable cannot wave through a container that is not running.
Both directions are asserted in tests/test_gate_governance.py.

WHY FOUR, IN TWO ROLES
  soldiers  "did this deploy work?"     Operational reading of the evidence.
  auditors  "what did everyone miss?"   Adversarial reading, explicitly asked for the failure mode
                                        nobody listed.
Both roles are filled from DIFFERENT VENDORS. A 429 is provider-wide and a blind spot is
model-family-wide, so four models from one provider is one model wearing four hats. This is the
same doctrine resume_consensus.pick_auditor already enforces for the Tailor: nothing marks its own
work, and the auditor is never the author's vendor.
"""
import json
import os
import sys

# ---------------------------------------------------------------- the panel
# 2 soldiers + 2 auditors, one per vendor. The order mirrors resume_consensus.CHAIN, which was
# chosen from MEASURED evidence on the real prompt (see MODEL_SELECTION / CLAUDE.md), not taste.
# Do not invent ids here: a marketing name from a pricing page ("deepseek-v4-flash") once sat at
# the head of a chain and returned HTTP 404 on every call for days, silently degrading.
SOLDIERS = ["deepseek-3.2", "llama-4-maverick"]
AUDITORS = ["gemma-4-31B-it", "kimi-k2.6"]

# Reviewer answer ceiling. NOT 900: the sibling project asked for <= 3 reasons + <= 3 risks at 400
# chars each plus two 500-char fields, which is ~1020 tokens of CONTENT, so a reviewer answering
# FULLY was guaranteed to be cut off mid-JSON - and was then reported as "did not answer" and
# acquired a reputation for being erratic across two reviews. A truncated answer is OUR ceiling,
# not the model's fault. (In JSON mode this gateway rejects max_tokens outright and
# resume_consensus.build_payload drops it; the ceiling therefore only applies to kimi, which
# refuses response_format and keeps it.)
MAX_TOKENS = 2600
TIMEOUT_S = 120

ARCH = """FACTS ABOUT THIS SYSTEM (jobhuntwow). Read this before claiming something is unverified
or proposing a fix - three consecutive panels on the sibling project proposed fixes for checks that
already worked the way they were asking for, because the briefing had a hole in it.

WHAT THE THING IS
  * ONE container, `jhw-web`: a FastAPI backend plus the BUILT React SPA inside a single image
    (Dockerfile.web). uvicorn serves `serve:app` on 0.0.0.0:8000 INSIDE the container.
  * There are NO PUBLISHED PORTS, deliberately. Nothing listens on 0.0.0.0 on the host. You cannot
    curl the app from the host; every app probe in this evidence was made with
    `docker exec jhw-web curl http://127.0.0.1:8000/...` and therefore measures uvicorn directly.
  * Persistence is a JSON FILE STORE under DATA_DIR=/data (docker named volume `jhw_data`), plus
    generated DOCX/PDF artifacts. There is no Postgres, no MySQL, no Redis, no SQLite server.
  * Language models come from DigitalOcean Serverless Inference (OpenAI-compatible). The key
    DO_INFERENCE_KEY is server-side only; the local apply agent gets a low-privilege
    AGENT_PROXY_TOKEN and reaches models through the backend's /v1 proxy.

HOW A CODE CHANGE REACHES PRODUCTION
  * `python jhw.py deploy` -> deploy_direct.py -> ONE ssh session -> a tar of the build context is
    unpacked into /opt/jobhuntwow -> `docker compose -p jobhuntwow -f docker-compose.web.yml build`
    -> `up -d --force-recreate`. THE CONTAINER IS REPLACED. There is no hot reload, no code volume
    mount, no restart-on-edit: editing a file on the droplet changes nothing until an image
    rebuild. GitHub/GHCR/CI are NOT in the deploy path.
  * Consequence: "the container is running" and "the container is running THIS code" are different
    claims. `engine_fresh` is the check that measures the second one; a three-day-old container
    answers /api/health perfectly happily.

THE SHARED REVERSE PROXY (production only - and this is why staging cannot check it)
  * ONE Caddy container (`videodead-caddy-1`) owns :443 for EVERY domain on the production box
    (jobhuntwow.com, cybergod.ai, godeyes.ai and others owned by unrelated projects). jobhuntwow
    owns exactly one marker-delimited block in it and must never touch another project's bytes.
  * /etc/caddy/Caddyfile is a single-FILE bind mount from /opt/videodead/Caddyfile, so the mount is
    pinned to an INODE. Replacing the file (mv, `sed -i`, tmp+rename) leaves the container reading
    the OLD inode forever while the host file looks perfectly correct.
  * Caddy reads its config ONLY at start or on an explicit reload. A file edited afterwards is
    silently unapplied until the next restart. That is a real 12-hour-latent outage on this box.
  * `flush_interval -1` in that vhost is what keeps the streaming response unbuffered end to end.
  * STAGING IS DEPLOYED WITH `--no-caddy` AND HAS NO SHARED PROXY. So this evidence proves the
    APPLICATION, not the vhost. The vhost is proven separately on production by `jhw.py status`,
    which tags a request and greps jhw-web's own access log for the tag (content matching alone
    once passed while a byte-identical static copy was serving the domain). If you want to say
    "the proxy path is unverified here", that is CORRECT and already known - say it once, do not
    propose adding a Caddy to staging, because a twin with a different proxy topology would
    validate something that never ships.

THE JOB MODEL - THERE IS NO QUEUE
  * POST /api/electronic/generate does the ENTIRE tailor INSIDE THE REQUEST: it drafts a resume and
    a cover letter with an author model, has a DIFFERENT-VENDOR auditor review them, bounds the
    auditor's flags against deterministic evidence, optionally revises, renders DOCX+PDF, writes a
    manifest and returns it. It is async in-process work, not a background job.
  * There is NO broker, NO worker process, NO task queue, NO scheduler for this. "Is the worker
    running" has no meaning on this system. Do not propose Celery/RQ/a queue; propose changes to
    files that exist.
  * `/api/chat` is a StreamingResponse of text/plain (NOT text/event-stream, and there is no
    reconnect/Last-Event-ID protocol). A stream that opens and emits nothing is the empty-200 of
    streaming, which is why the evidence measures BYTES RECEIVED, not the status code.

WHAT IS NOT ON THE DROPLET AT ALL
  * The ATS apply drivers (Workday/Phenom DOM automation, Chromium, Stagehand, the Telegram
    ask-bridge) run in a LOCAL sandbox on the operator's own PC (agent/docker-compose.local.yml).
    Nothing in this gate exercises them and no check here can. Do not report their absence as a
    regression.
  * There is NO Kubernetes, no config-map, no service mesh, no sidecar, no hot-reload watcher, no
    systemd unit for the app, no CI step in the deploy path, and no second Loki/Grafana. Events are
    appended as one JSON object per line to EVENTS_LOG on a volume that a DIFFERENT project's
    promtail tails on production; that volume and that promtail DO NOT EXIST on staging.

WHAT EACH CHECK ALREADY MEASURES (one line each - do not ask for what is already here)
  * container_running       docker inspect .State.Status of jhw-web is exactly "running".
  * container_healthy       docker's own healthcheck state (the image HEALTHCHECKs /api/health).
  * restart_count           .RestartCount <= 1. A crash loop can otherwise look like "running".
  * api_health              GET /api/health returns JSON with ok:true, measured inside the container.
  * api_auth                GET /api/me with NO cookie returns 401. Probing "/" alone would pass on
                            a cached SPA shell; this proves the tenant gate is armed.
  * app_served              GET / with a BROWSER User-Agent returns 2xx/3xx AND the body links to
                            /login. A curl-shaped UA is deliberately avoided.
  * spa_shell               GET /login returns the SPA shell (id="root"), i.e. the built frontend
                            is actually in the image and not a 404 from a failed npm build.
  * html_nocache            / carries Cache-Control: no-cache. Without it browsers apply HEURISTIC
                            caching and served a stale landing for over a day on this project.
  * chat_stream_delivers    POST /api/chat and count the BYTES the stream actually emitted. This is
                            the live model path end to end (proxy -> DO -> stream), not a ping.
  * job_completes           POST /api/electronic/jd then /generate with a tiny JD and a tiny
                            profile, and READ THE OUTPUT: 4 rendered files in the manifest, a
                            non-empty resume, and the artifacts non-trivially sized on disk. "The
                            endpoint returned 200" is not "the job worked".
  * auditor_not_author      from that same manifest: the auditing model is not the authoring model.
                            The product claim is that nothing marks its own work; this measures it.
  * engine_fresh            sha256 of every backend/app/*.py INSIDE the container against the same
                            files as shipped. This is the DEPLOY PROOF. It covers backend python
                            only - NOT the frontend bundle, NOT the landing page - so if you want
                            to point out that the SPA build is unverified by hash, that is true.
  * events_written          the app appended at least one JSON line to its EVENTS_LOG. On staging
                            no promtail/Loki exists, so this proves the WRITE, not the pipeline.
  * disk / memory           free space and available RAM on the host.
  * reboot_recovery         /proc/sys/kernel/random/boot_id CHANGED. Waiting for ssh to answer is
                            NOT a reboot test: right after `systemctl reboot` the box is still up
                            and answers instantly, which is how an earlier version reported a
                            1-second reboot and passed.
  * evidence_complete       every name in the COMMITTED expected list reported in this pass. The
                            gate knows what it EXPECTS; it does not count what it received. A
                            shrinking denominator is missing evidence, not a smaller pass.
"""

_GOVERNANCE = """GOVERNANCE - this is the part that saves the operator a dozen round-trips. For
every failed check, say what you believe the ROOT CAUSE is and name the REPO FILE that should
change. Distinguish clearly between "the system under test is broken" and "the CHECK ITSELF is
broken" - a check whose detail contradicts its own verdict is a defect in the check, and saying so
is more valuable than restating the failure. If a check's evidence is insufficient to tell, say
that instead of guessing."""

SOLDIER_PROMPT = """You are a release engineer reviewing a STAGING validation run before the change
is promoted to production. Production serves a live product from a host it SHARES with several
unrelated projects behind one reverse proxy, so a bad promotion is expensive.

Answer ONLY from the evidence below. Never invent a check, a container name, an endpoint, a version
or a number that is not present. If the evidence does not cover something, say so plainly.

%s

Return STRICT JSON, no prose outside it:
{"verdict":"go"|"no-go"|"unsure",
 "diagnosis":"<1-2 sentences: the most likely ROOT CAUSE, or '' if the evidence does not support one>",
 "proposed_fix":"<the concrete change, naming a file/function where you can, or '' if unsure>",
 "reasons":["<= 3 short factual sentences, each citing a specific check by name>"],
 "risks":["<= 3 concrete risks this change carries into production, or [] if none are evidenced>"]}

EVIDENCE:
%s"""

AUDITOR_PROMPT = """You are an independent auditor. Two other reviewers have already said whether
this staging run looks healthy. Your job is the opposite one: find what the checks DO NOT cover.

Look for: a check that passed for the wrong reason; a service that is "running" but was never
exercised; a stream that opened but delivered nothing; a job that returned 200 with an empty
artifact; something that only breaks after a reboot, under load, or on the next restart; and any
claim in the evidence that is weaker than it sounds.

Answer ONLY from the evidence. Do not invent findings - "no gap evidenced" is a valid and useful
answer, and is much better than a plausible-sounding guess. If you believe a check is missing,
first re-read WHAT EACH CHECK ALREADY MEASURES above.

%s

Return STRICT JSON, no prose outside it:
{"verdict":"go"|"no-go"|"unsure",
 "diagnosis":"<1-2 sentences: the most likely ROOT CAUSE, or '' if the evidence does not support one>",
 "proposed_fix":"<the concrete change, naming a file/function where you can, or '' if unsure>",
 "reasons":["<= 3 sentences on what the evidence does and does not prove>"],
 "risks":["<= 3 gaps in the CHECKS themselves, or [] if none>"]}

EVIDENCE:
%s"""


# ---------------------------------------------------------------- the model call
def _post(payload, timeout=TIMEOUT_S):
    """POST to the DO inference endpoint with the stdlib and KEEP THE ERROR BODY.

    urllib rather than httpx/asyncio on purpose: this is a synchronous one-shot CLI, and the
    backend's own transport (`llm.chat`) is an async coroutine. Reaching for an event loop to make
    four sequential calls buys nothing and adds a failure mode.

    A 4xx BODY IS THE SERVER TELLING YOU WHAT IT WANTS. Discarding it is exactly how
    "temperature must be 0.6 for this model" stayed invisible while a healthy, entitled model was
    written off as broken three times. It is returned, not logged and dropped.
    """
    import urllib.error
    import urllib.request
    sys.path.insert(0, "/app")
    from app.settings import DO_BASE_URL, DO_KEY          # noqa: E402  (lazy: PC has no app pkg)
    if not DO_KEY:
        raise RuntimeError("DO_INFERENCE_KEY is not set in this container")
    req = urllib.request.Request(
        DO_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + DO_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = (e.read() or b"").decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise _HTTP(e.code, body)


class _HTTP(Exception):
    def __init__(self, status, body=""):
        self.status, self.body = int(status), str(body or "")
        super().__init__("HTTP %d: %s" % (self.status, self.body[:300]))


def ask_one(model, prompt):
    """One model, one answer. NEVER raises: a reviewer that cannot answer is DATA, not a failure.

    Per-model quirks are NOT re-implemented here. `resume_consensus.build_payload` and
    `repair_payload` are the ONE home in this repo for them (kimi demands a specific temperature
    and REFUSES response_format; this gateway rejects max_tokens together with json_object; and
    chain-of-thought must stay suppressed or a thinking model cannot honour a strict-JSON
    contract). A second copy of that knowledge is a second place to rediscover it, which is the
    "a value with a documented home must not be restated elsewhere" rule.
    """
    try:
        sys.path.insert(0, "/app")
        from app import resume_consensus as RC            # noqa: E402  (lazy, see module docstring)
    except Exception as e:
        return {"model": model, "verdict": "unsure", "error": "import",
                "reasons": ["app.resume_consensus unavailable: %r" % (e,)], "risks": []}
    try:
        payload = RC.build_payload(model, "You are a precise release reviewer. Output JSON only.",
                                  prompt, max_tokens=MAX_TOKENS, temperature=0.2, json_mode=True)
        try:
            data, _ = _post(payload)
        except _HTTP as e:
            if e.status not in (400, 422):
                raise
            # REPAIR WHAT THE SERVER NAMED, in the direction it named it. Stripping fields until
            # something works is how you disable a safeguard you did not know you had: blanket
            # dropping chat_template_kwargs re-enables thinking, which once produced 46,801
            # characters of rambling and a truncated non-answer.
            payload, fixes = RC.repair_payload(payload, e.body)
            print("[warn] quorum %s: HTTP %d -> %s | retrying with %s"
                  % (model, e.status, e.body[:200].replace("\n", " "), ", ".join(fixes) or "no change"),
                  file=sys.stderr)
            data, _ = _post(payload)
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        txt = msg.get("content") or msg.get("reasoning_content") or ""
        j = RC.json_loose(txt)
        if not isinstance(j, dict) or not j:
            raise ValueError("empty or non-object answer (finish_reason=%r, %d chars)"
                             % (ch.get("finish_reason"), len(txt)))
        v = str(j.get("verdict", "unsure")).lower().replace("_", "-").strip()
        if v not in ("go", "no-go", "unsure"):
            v = "unsure"
        clip = lambda xs: [str(x)[:400] for x in (xs if isinstance(xs, list) else [xs])][:3]  # noqa: E731
        return {"model": model, "verdict": v,
                "reasons": clip(j.get("reasons") or []),
                "risks": clip(j.get("risks") or []),
                "diagnosis": str(j.get("diagnosis") or "")[:500],
                "proposed_fix": str(j.get("proposed_fix") or "")[:500],
                "tokens_out": ((data.get("usage") or {}).get("completion_tokens") or 0),
                "finish": ch.get("finish_reason") or ""}
    except Exception as e:
        why = "%s: %s" % (type(e).__name__, e)
        if "Unterminated" in why or "Expecting" in why or "char 1" in why:
            why = ("answer was CUT OFF mid-JSON - that is OUR max_tokens ceiling, not the model "
                   "(raise quorum.MAX_TOKENS): " + why)
        return {"model": model, "verdict": "unsure", "error": type(e).__name__,
                "reasons": ["did not answer: " + why], "risks": []}


# ---------------------------------------------------------------- the deterministic gate
def gate_from_checks(checks, expected=None):
    """GO only if there is evidence AND nothing failed AND nothing EXPECTED is missing.

    THE MISSING-EVIDENCE RULE IS THE POINT. On 2026-08-16 the sibling project's gate printed
    "GATE: GO (35/35 deterministic checks passed)" while SIX post-reboot checks never arrived at
    all - the previous run had counted 41. It reported 100% of what it HAPPENED TO RECEIVE, so a
    shrinking denominator read as success. `expected` is a COMMITTED list, so this gate cannot be
    fooled that way in either pass: a check that vanishes is a FAILURE, not a smaller pass.
    """
    checks = list(checks or [])
    failed = [c for c in checks if not c.get("ok")]
    missing = sorted(set(expected or []) - {c.get("name") for c in checks})
    if not checks or failed or missing:
        return "NO-GO", failed, missing
    return "GO", failed, missing


def decide_from_verdict(verdict):
    """(gate, digest) - the FINAL promotion decision. PURE: no ssh, no droplet, no model call.

    THE DETERMINISTIC CHECKS DECIDE. A model must never veto a good release over a 429 or a bad
    mood, and an agreeable model must never wave a dead container through. Both directions are
    asserted in tests/test_gate_governance.py.

    ONE NARROW EXCEPTION - PANEL DISSENT AGAINST A GREEN GATE IS ITSELF EVIDENCE. Twice on the
    sibling project the panel contradicted a green gate and was RIGHT about a real defect:
      * 2026-08-07 - all four named the same check; the check was scoring a failure as a pass.
      * 2026-08-16 - two dissented, one naming the missing post-reboot checks, while the gate
        printed "35/35 passed".
    So >= 2 dissenters INCLUDING >= 1 hard NO-GO halts and asks a human. UNSURE counts as dissent
    (a reviewer that cannot convince itself the evidence is complete is making a NO-GO's point),
    but two UNSUREs with nobody actually objecting is hesitation, not a finding - and a gate that
    stops you for nothing is a gate you override by reflex.

    A single reviewer can never stop a release: that is what protects against one rate-limited or
    grumpy model. The halt is escapable with OVERRIDE_PANEL=1, because a guard an informed
    operator cannot override is a bug.
    """
    gate = str(verdict.get("gate", "NO-GO"))
    revs = verdict.get("reviews") or []

    def _v(r):
        return str(r.get("verdict", "")).lower().replace("_", "-").strip()

    answered = [r for r in revs if not r.get("error")]
    dissent = [r for r in answered if _v(r) in ("no-go", "unsure")]
    hard = [r for r in answered if _v(r) == "no-go"]
    verdict["panel_dissent"] = len(dissent) >= 2 and len(hard) >= 1
    if gate == "GO" and verdict["panel_dissent"] and not os.environ.get("OVERRIDE_PANEL"):
        who = ", ".join("%s=%s" % (r.get("model", "?"), _v(r) or "?") for r in dissent)
        verdict["gate"] = gate = "NO-GO"
        verdict["halted_by_panel"] = True
        verdict["digest"] = (
            "HALTED BY THE PANEL: every deterministic check passed, but %d of %d reviewers that "
            "answered dissented (%s).\n"
            "A panel contradicting a green gate has twice meant A CHECK IS LYING rather than the "
            "system being fine. Read their reasons below, and compare the CHECK COUNT with the "
            "previous run - a shrinking count is missing evidence.\n"
            "To promote anyway: set OVERRIDE_PANEL=1 and re-run.\n\n"
            % (len(dissent), len(answered), who)) + str(verdict.get("digest", ""))
    return gate, str(verdict.get("digest", ""))


# ---------------------------------------------------------------- bounded autonomy (NOT WIRED)
# PORTED, TESTED, AND DELIBERATELY CONNECTED TO NOTHING.
#
# The four-model design allows a panel to PROPOSE values for a small, named set of integers. The
# porting order is explicit about when that is earned (FOUR_MODEL_CONSENSUS.md section 7.2): write
# the deterministic checks, add the evidence contract, run the panel ADVISORY for a while, add the
# governance rules, and "ONLY THEN consider letting it tune anything". jobhuntwow is at step 4.
#
# So these two functions exist here for one reason: when a tunable does appear, the quorum + median
# + clamp arithmetic must already be the reviewed, tested version rather than something rewritten
# from memory in a hurry. `BOUNDS` is empty on purpose; `tuning_is_wired()` reports the state so a
# test can assert nobody has quietly switched it on, and tests/test_gate_governance.py does exactly
# that. Nothing in gate.py or ship.py calls either function.

QUORUM = 3                      # of 4 reviewers must agree on the DIRECTION before a key moves
MAX_STEP = 0.25                 # no key may move more than 25% in one cycle

# name -> (low, high). EMPTY: this gate tunes nothing. Adding a key here is a deliberate decision
# to hand a panel a lever, and it belongs in git so it is reviewable in a diff.
BOUNDS = {}


def clamp(key, value, current=None, bounds=None):
    """Force `value` into the COMMITTED bounds for `key`, and cap the step.

    THE BOUNDS ARE ENFORCED ON EVERY *READ*, NOT ON WRITE. That is the whole safety property: a
    hand-edited, corrupted or hostile tuning file still cannot push the system out of range, and
    the worst it can do is pick a different point inside a range that was already accepted. A
    write-time check protects only the values that happened to go through your writer.

    Returns the clamped int (or None if `key` is not a tunable at all - an unknown key must never
    become a value by accident).
    """
    b = BOUNDS if bounds is None else bounds
    if key not in b:
        return None
    lo, hi = b[key]
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if current is not None:
        try:
            cur = int(current)
            step = max(1, int(abs(cur) * MAX_STEP))
            v = max(cur - step, min(cur + step, v))
        except (TypeError, ValueError):
            pass
    return max(lo, min(hi, v))


def consensus(reviews, current, bounds=None):
    """(agreed, notes) - what the panel may change, by QUORUM on the DIRECTION and then the MEDIAN.

    Four properties make this safe, and all four are asserted in tests/test_gate_governance.py:
      1. the bounds live in COMMITTED code and are applied here on read;
      2. a QUORUM of 3 of 4 must agree on the DIRECTION before a key moves at all - three saying
         "raise" and one saying "lower" raises it; a two-two split changes NOTHING;
      3. the applied value is the MEDIAN, never the mean and never the boldest, so one model
         arguing for a drastic change is outvoted by three moderate ones;
      4. the step is capped, because a model that has misread one incident must not be able to
         swing the whole policy in a single cycle.

    The REFUSALS are returned, not swallowed. The operator seeing what was proposed, what was
    applied and why the rest was refused is the feature.
    """
    b = BOUNDS if bounds is None else bounds
    agreed, notes = {}, []
    keys = sorted({k for r in (reviews or []) for k in ((r or {}).get("propose") or {})})
    for k in keys:
        cur = (current or {}).get(k)
        if cur is None or k not in b:
            notes.append("%s: not a tunable key - refused" % k)
            continue
        up = [r["propose"][k] for r in reviews if ((r.get("propose") or {}).get(k, cur)) > cur]
        dn = [r["propose"][k] for r in reviews if ((r.get("propose") or {}).get(k, cur)) < cur]
        side = up if len(up) >= QUORUM else (dn if len(dn) >= QUORUM else [])
        if not side:
            notes.append("%s: only %d up / %d down - below the quorum of %d, unchanged"
                         % (k, len(up), len(dn), QUORUM))
            continue
        med = sorted(side)[len(side) // 2]
        v = clamp(k, med, current=cur, bounds=b)
        if v is None or v == cur:
            notes.append("%s: proposal %r clamped to no change" % (k, med))
            continue
        agreed[k] = v
    return agreed, notes


def tuning_is_wired():
    """True if this panel can change ANY runtime value. Must be False at this stage of the port."""
    return bool(BOUNDS)


# ---------------------------------------------------------------- digest
def build_digest(ev, checks, expected, failed, missing, gate, reviews):
    answered = [r for r in reviews if not r.get("error")]
    hard = [r for r in answered if str(r.get("verdict")) == "no-go"]
    lines = ["STAGING VALIDATION - %s" % ev.get("host", "?"),
             "",
             "GATE: %s   (%d of %d expected checks reported, %d failed)"
             % (gate, len(checks), len(expected or checks), len(failed))]
    if missing:
        lines += ["", "MISSING EVIDENCE (expected, never reported - NOT a smaller pass):"] + \
                 ["  ? %s" % m for m in missing]
    if failed:
        lines += ["", "FAILED CHECKS:"] + ["  x %-26s %s" % (c.get("name"), str(c.get("detail"))[:400])
                                           for c in failed]
    # The four diagnoses TOGETHER and FIRST, so agreement and disagreement are visible at a glance.
    # That is worth far more than four separately formatted opinions further down.
    diag = [r for r in answered if r.get("diagnosis")]
    if (failed or missing) and diag:
        lines += ["", "WHAT THE PANEL THINKS IS ACTUALLY WRONG:"]
        for r in diag:
            lines.append("  [%s] %s" % (r.get("model"), r["diagnosis"]))
            if r.get("proposed_fix"):
                lines.append("        -> fix: %s" % r["proposed_fix"])
    lines += ["", "REVIEW PANEL (%d of %d answered) - ADVISORY: it does not set the gate above:"
              % (len(answered), len(reviews))]
    for r in reviews:
        lines.append("  [%-7s] %-18s %s" % (r.get("role", "?")[:7], r.get("model"),
                                            str(r.get("verdict", "?")).upper()))
        for x in (r.get("reasons") or []):
            lines.append("        . %s" % x)
        for x in (r.get("risks") or []):
            lines.append("        ! risk: %s" % x)
    if hard:
        lines += ["", "NOTE: %d reviewer(s) said NO-GO." % len(hard)]
    if len(answered) < 2:
        lines += ["", "!! PANEL BELOW QUORUM: %d of %d answered (quota, timeout or outage). The "
                  "safeguard that HALTS a green gate on panel dissent needs >= 2 answers, so it "
                  "CANNOT FIRE this run. The deterministic gate is unaffected." % (len(answered),
                                                                                   len(reviews)),
                  "   Non-answers: " + "; ".join(
                      "%s (%s)" % (r.get("model"), r.get("error") or "?")
                      for r in reviews if r.get("error"))]
    return "\n".join(lines)


def main():
    ev = json.load(sys.stdin)
    checks = ev.get("checks") or []
    expected = ev.get("expected") or []
    gate, failed, missing = gate_from_checks(checks, expected)

    slim = json.dumps(ev, indent=1)[:9000]      # bound the prompt: latency scales with prompt size
    reviews = []
    for m in SOLDIERS:
        reviews.append(dict(role="soldier", **ask_one(m, SOLDIER_PROMPT % (_GOVERNANCE,
                                                                          ARCH + "\n" + slim))))
    for m in AUDITORS:
        reviews.append(dict(role="auditor", **ask_one(m, AUDITOR_PROMPT % (_GOVERNANCE,
                                                                          ARCH + "\n" + slim))))

    answered = [r for r in reviews if not r.get("error")]
    print(json.dumps({
        "gate": gate, "checks_total": len(checks), "checks_expected": len(expected),
        "checks_failed": len(failed), "failed": failed, "missing": missing,
        "reviews": reviews, "answered": len(answered),
        "dissent": len([r for r in answered if r.get("verdict") in ("no-go", "unsure")]),
        "digest": build_digest(ev, checks, expected, failed, missing, gate, reviews),
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
