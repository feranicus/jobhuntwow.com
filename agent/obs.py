"""Structured observability events — one JSON object per line -> Promtail -> Loki -> Grafana.

KISS: no OTel collector, no metrics server, no sidecar. Just append-only JSON lines to a file that
Promtail tails (the same pattern as the colt-stack sibling project).

RULE: never log secrets or PII. Log ids, stages, counts, durations, error text — not passwords,
tokens, resume contents, or answers.

    from obs import event
    event("apply_start", job="4435446898", ats="workday")
    event("apply_done", job=jid, stage="review", filled=6, ms=12345)
"""
from __future__ import annotations
import json, os, sys, time

EVENTS_LOG = os.getenv("EVENTS_LOG", "/agent/out/events.jsonl")
SERVICE    = os.getenv("SERVICE", "jhw-agent")

# keys we refuse to emit even if a caller passes them (defence in depth)
_SECRET_KEYS = {"password", "passwd", "pw", "token", "api_key", "apikey", "secret", "cookie",
                "authorization", "creds", "credential"}


def _scrub(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k.lower() in _SECRET_KEYS:
            out[k] = "***"
        elif isinstance(v, str) and len(v) > 500:
            out[k] = v[:500] + "…"
        else:
            out[k] = v
    return out


def event(evt: str, **fields) -> None:
    """Emit one structured event. Never raises — observability must not break the pipeline."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "service": SERVICE, "evt": evt}
    rec.update(_scrub(fields))
    line = json.dumps(rec, ensure_ascii=False)
    try:
        os.makedirs(os.path.dirname(EVENTS_LOG), exist_ok=True)
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass                      # a full disk must never kill an application run
    print("[evt] " + line, file=sys.stderr, flush=True)
