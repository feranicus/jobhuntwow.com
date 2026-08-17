"""phenom.py — deterministic helpers for Phenom People career sites (see skills/ats-phenom).

No LLM: the apply form is at a URL derived from the job URL, so we navigate instead of hunting
for an "Apply now" button that overlays can swallow.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse, parse_qs

JOB_RE = re.compile(r"^(?P<base>https?://[^/]+)(?P<pre>/.*?)/job/(?P<seq>[^/?#]+)", re.I)


def is_phenom(url: str) -> bool:
    u = url or ""
    return bool(JOB_RE.match(u)) or "jobseqno" in u.lower()


def apply_url(url: str) -> str:
    """/global/en/job/<SEQ>/<slug>  ->  /global/en/apply?jobSeqNo=<SEQ>&step=1  ("" if not Phenom)."""
    u = url or ""
    if "jobseqno" in u.lower():
        q = parse_qs(urlparse(u).query)
        seq = (q.get("jobSeqNo") or q.get("jobseqno") or [""])[0]
        if seq:
            p = urlparse(u)
            pre = p.path.rsplit("/apply", 1)[0] if "/apply" in p.path else ""
            return f"{p.scheme}://{p.netloc}{pre}/apply?jobSeqNo={seq}&step=1"
        return u
    m = JOB_RE.match(u)
    if not m:
        return ""
    return f"{m.group('base')}{m.group('pre')}/apply?jobSeqNo={m.group('seq')}&step=1"
