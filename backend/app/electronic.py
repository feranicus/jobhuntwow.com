"""electronic.py — the CORE product loop: job description IN -> tailored resume + cover OUT.

Endpoints (mounted by main.py: `app.include_router(electronic.router)`):

    POST /api/electronic/jd                    {text?, url?}            -> parsed JD | needs_paste
    POST /api/electronic/generate              {email, jd, profile}     -> tailored docs + manifest
    GET  /api/electronic/jobs                  ?email=...               -> this user's jobs
    GET  /api/electronic/artifacts/{job_id}    ?email=...               -> list the generated files
    GET  /api/electronic/artifacts/{job_id}/{filename}?email=...        -> download one file

TENANCY: every endpoint takes `email` and resolves to DATA_DIR/users/<sha256(email)[:16]>/<job_id>/.
This module does NOT implement auth (another module owns sessions) - it only scopes storage by the
email it is handed. main.py is expected to inject the authenticated email.

TRUTH RULES (these are the product, not decoration):
  * The LLM may only re-angle, reorder and rephrase facts that are IN the supplied profile.
    It may never add an employer, a date, a degree, a certification, a metric or a tool.
  * A deterministic post-check (`_truth_check`) compares every organisation in the generated
    resume against the profile text and reports anything it cannot find. We surface the
    warning; we never silently "fix" it by rewriting prose.
  * If the profile has no number for a claim, the bullet stays qualitative. No invented metrics.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import documents, jd_ingest, llm
from .settings import DATA_DIR

router = APIRouter(prefix="/api/electronic", tags=["electronic"])

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
ALLOWED_FILES = {"resume.docx", "resume.pdf", "cover_letter.docx", "cover_letter.pdf",
                 "job.json", "tailored.json"}
MIME = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf", ".json": "application/json"}


# --------------------------------------------------------------------------- storage
def user_dir(email: str) -> str:
    """DATA_DIR/users/<sha256(email)[:16]> - stable, non-reversible, filesystem-safe."""
    e = (email or "").strip().lower()
    if not e:
        raise HTTPException(status_code=400, detail="email is required (tenant scope)")
    h = hashlib.sha256(e.encode("utf-8")).hexdigest()[:16]
    return os.path.join(str(DATA_DIR), "users", h)


def job_dir(email: str, job_id: str) -> str:
    if not JOB_ID_RE.match(job_id or ""):
        raise HTTPException(status_code=400, detail="invalid job_id")
    return os.path.join(user_dir(email), job_id)


def _new_job_id(company: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (company or "", title or "")).lower()).strip("-")
    slug = (slug[:40] or "job").strip("-")
    return "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), slug, uuid.uuid4().hex[:6])


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- LLM plumbing
def _json(txt: Any) -> dict:
    """Tolerate every shape a model actually emits: fenced, prefixed prose, [ {...} ].

    Learned the hard way in the sibling project: a GOOD answer thrown away by a strict
    parser costs the whole budget. Tolerate any SHAPE; reject only an EMPTY answer.
    """
    if isinstance(txt, dict):
        return txt
    s = (txt or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s)
        if isinstance(d, list):
            d = next((x for x in d if isinstance(x, dict)), {})
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


async def _ask(role: str, fallback: str, system: str, user: str, max_tokens: int) -> dict:
    """One LLM call with a cross-vendor fallback. Returns {'_error': ...} rather than raising."""
    last = None
    for r in (role, fallback):
        if not r:
            continue
        try:
            out = await llm.complete(r, [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
                                     temperature=0.3, max_tokens=max_tokens, json_mode=True)
            d = _json(out)
            if d:
                d["_model_role"] = r
                return d
            last = "empty/unparseable answer from role '%s'" % r
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
    return {"_error": last or "no model answered"}


# --------------------------------------------------------------------------- prompts
_TRUTH = (
    "HARD RULES - these override everything else:\n"
    "1. NEVER invent, embellish or infer a fact about the candidate. Employers, job titles, dates, "
    "degrees, certifications, tools, team sizes, revenue and percentages may ONLY appear if they are "
    "present in the CANDIDATE PROFILE below. If the profile has no number for a claim, write the "
    "bullet WITHOUT a number rather than inventing one.\n"
    "2. You may re-angle, reorder, compress and rephrase what IS there so it speaks to this job.\n"
    "3. Mirror the job description's real vocabulary ONLY where it is truthfully the same thing the "
    "candidate has done. Do not adopt a JD keyword the candidate cannot back up.\n"
    "4. ATS-safe output: plain text, no tables, no columns, no graphics, no emoji, no markdown "
    "syntax inside field values.\n"
    "5. Every bullet must carry a FACT, a NUMBER or an OUTCOME. No filler, no adjectives-only lines.\n"
    "6. Output ONLY the requested JSON object. No prose before or after."
)

_RESUME_SYS = ("You tailor an existing resume to a specific job. You are a truth-gated editor, not a "
               "copywriter with imagination.\n" + _TRUTH)

_COVER_SYS = ("You write a concise, senior cover letter that a hiring manager will actually finish "
              "reading. Specific, no cliches, no fabrication.\n" + _TRUTH)


def _resume_user(profile_txt: str, jd: dict) -> str:
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n"
        "%s\n\n"
        "TARGET JOB\n"
        "title: %s\ncompany: %s\nlocation: %s\n"
        "description:\n%s\n\n"
        "Return JSON with EXACTLY this shape:\n"
        "{\n"
        '  "summary": "3-4 sentence professional summary aimed at THIS job, true to the profile",\n'
        '  "skills": ["12-20 skills taken from the profile, ordered by relevance to the JD"],\n'
        '  "experience": [\n'
        '    {"title":"<exact title from the profile>", "company":"<exact company from the profile>",\n'
        '     "dates":"<exact dates from the profile, or empty>", "location":"<from profile, or empty>",\n'
        '     "bullets":["3-5 bullets re-angled to the JD. Same facts. Each carries a fact, number or outcome."]}\n'
        "  ],\n"
        '  "keywords_matched": ["JD terms the candidate genuinely covers"],\n'
        '  "gaps": ["JD requirements the profile does NOT evidence - be honest, this is for the candidate only"]\n'
        "}\n"
        "Keep EVERY employer from the profile, in the profile order. Do not merge or drop roles."
        % (profile_txt[:14000], jd.get("title", ""), jd.get("company", ""),
           jd.get("location", ""), (jd.get("text") or "")[:9000]))


def _cover_user(profile_txt: str, jd: dict) -> str:
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n%s\n\n"
        "TARGET JOB\ntitle: %s\ncompany: %s\nlocation: %s\ndescription:\n%s\n\n"
        "Return JSON with EXACTLY this shape:\n"
        "{\n"
        '  "salutation": "Hiring Team, or a named person if the JD gives one",\n'
        '  "paragraphs": [\n'
        '    "Opening: why this role at this company, and the single strongest true hook. 2-3 sentences.",\n'
        '    "Proof: the most relevant concrete evidence from the profile mapped to the top JD requirement.",\n'
        '    "Fit: what the candidate would do in the first months, grounded in what they have already done.",\n'
        '    "Close: short, direct, no grovelling."\n'
        "  ],\n"
        '  "closing": "Sincerely,"\n'
        "}\n"
        "Max ~320 words total. No sentence may contain a fact absent from the profile."
        % (profile_txt[:9000], jd.get("title", ""), jd.get("company", ""),
           jd.get("location", ""), (jd.get("text") or "")[:6000]))


# --------------------------------------------------------------------------- profile handling
def _profile_text(profile: Any) -> str:
    if isinstance(profile, str):
        return profile
    try:
        return json.dumps(profile, ensure_ascii=False, indent=1)
    except Exception:
        return str(profile)


def _pget(profile: Any, key: str, default):
    return profile.get(key, default) if isinstance(profile, dict) else default


def _basics(profile: Any) -> dict:
    """Pull the contact block out of a dict profile, or out of markdown 'key: value' lines."""
    keys = ("name", "full_name", "headline", "title", "email", "phone", "location", "city",
            "country", "linkedin", "website", "portfolio")
    out = {}
    if isinstance(profile, dict):
        src = profile.get("basics") if isinstance(profile.get("basics"), dict) else profile
        for k in keys:
            v = src.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                out[k] = str(v).strip()
    else:
        for line in str(profile or "").split("\n"):
            m = re.match(r"^\s*-?\s*([a-z_]+)\s*:\s*(.+?)\s*$", line, re.I)
            if m and m.group(1).lower() in keys:
                out.setdefault(m.group(1).lower(), m.group(2).strip().strip('"'))
    if "name" not in out and "full_name" in out:
        out["name"] = out["full_name"]
    if "headline" not in out and "title" in out:
        out["headline"] = out["title"]
    if "location" not in out:
        loc = " ".join(x for x in (out.get("city"), out.get("country")) if x)
        if loc:
            out["location"] = loc
    return out


def _truth_check(tailored: dict, profile: Any) -> list:
    """Deterministic guard: flag any organisation the profile does not contain.

    Absence of evidence is reported as a WARNING for the human, never silently corrected
    and never treated as proof of fabrication - the profile may simply be terse.
    """
    hay = re.sub(r"\s+", " ", _profile_text(profile).lower())
    warns = []
    seen = set()
    for e in tailored.get("experience") or []:
        if not isinstance(e, dict):
            continue
        v = str(e.get("company") or "").strip()
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        probe = re.sub(r"[^a-z0-9 ]+", " ", v.lower())
        probe = re.sub(r"\b(gmbh|ag|inc|ltd|llc|ou|se|bv|plc|kg|co)\b", " ", probe).strip()
        head = probe.split(" ")[0] if probe else ""
        if head and len(head) > 2 and head not in hay:
            warns.append("Employer '%s' is not present in the supplied profile - verify it before "
                         "sending." % v)
    return warns


# --------------------------------------------------------------------------- API models
class JDReq(BaseModel):
    text: Optional[str] = ""
    url: Optional[str] = ""


class GenerateReq(BaseModel):
    email: str
    jd: Any = None                 # the dict from /jd, or a raw JD string
    profile: Any = None            # dict or markdown text (candidate.md)
    job_id: Optional[str] = ""


# --------------------------------------------------------------------------- endpoints
@router.post("/jd")
async def parse_jd(req: JDReq):
    """Job description IN. Pasted text always works; URLs are best-effort by tier."""
    return await jd_ingest.ingest(text=req.text or "", url=req.url or "")


@router.post("/generate")
async def generate(req: GenerateReq):
    """Tailor resume + cover letter to the JD and write DOCX + PDF for both."""
    t0 = time.time()

    jd = req.jd
    if isinstance(jd, str):
        jd = jd_ingest.from_text(jd)
    if not isinstance(jd, dict):
        raise HTTPException(status_code=400, detail="jd must be an object or a string")
    if jd.get("status") in ("needs_paste", "needs_local_fetch"):
        raise HTTPException(status_code=400, detail=jd.get("note") or "JD could not be fetched")
    if not str(jd.get("text") or "").strip():
        raise HTTPException(status_code=400,
                            detail="jd.text is empty - parse it first via POST /api/electronic/jd")

    if not req.profile:
        raise HTTPException(status_code=400,
                            detail="profile is required - we never invent candidate facts")
    ptxt = _profile_text(req.profile)
    if len(ptxt.strip()) < 60:
        raise HTTPException(status_code=400,
                            detail="profile is too thin to tailor from (we need the candidate's "
                                   "real experience; we will not invent it)")

    jid = req.job_id or _new_job_id(jd.get("company", ""), jd.get("title", ""))
    if not JOB_ID_RE.match(jid):
        raise HTTPException(status_code=400, detail="invalid job_id")
    outdir = job_dir(req.email, jid)
    os.makedirs(outdir, exist_ok=True)

    tailored, cover = await asyncio.gather(
        _ask("content", "content_fb", _RESUME_SYS, _resume_user(ptxt, jd), 6000),
        _ask("content", "content_fb", _COVER_SYS, _cover_user(ptxt, jd), 2000),
    )

    errors = {}
    if tailored.get("_error"):
        errors["resume"] = tailored["_error"]
    if cover.get("_error"):
        errors["cover"] = cover["_error"]
    if "_error" in tailored and "_error" in cover:
        raise HTTPException(status_code=502,
                            detail="the tailoring models did not answer: %s" % errors)

    basics = _basics(req.profile)
    resume_struct = {
        "basics": basics,
        "summary": tailored.get("summary") or _pget(req.profile, "summary", ""),
        "skills": tailored.get("skills") or _pget(req.profile, "skills", []),
        "experience": tailored.get("experience") or _pget(req.profile, "experience", []),
        "education": _pget(req.profile, "education", []),
        "certifications": _pget(req.profile, "certifications", []),
        "languages": _pget(req.profile, "languages", []),
    }
    cover_struct = {
        "basics": basics,
        "company": jd.get("company", ""),
        "job_title": jd.get("title", ""),
        "salutation": cover.get("salutation") or "Hiring Team",
        "paragraphs": cover.get("paragraphs") or [],
        "closing": cover.get("closing") or "Sincerely,",
    }

    written = documents.write_all(resume_struct, cover_struct, outdir)
    if written.get("errors"):
        errors.update(written["errors"])
    if not written.get("files"):
        raise HTTPException(status_code=500, detail="document rendering failed: %s" % errors)

    manifest = {
        "job_id": jid,
        "created": int(t0),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "jd": {k: jd.get(k, "") for k in ("title", "company", "location", "source", "url", "note")},
        "models": {"resume": tailored.get("_model_role"), "cover": cover.get("_model_role")},
        "files": sorted(os.path.basename(p) for p in written["files"].values()),
        "keywords_matched": tailored.get("keywords_matched") or [],
        "gaps": tailored.get("gaps") or [],
        "warnings": _truth_check(resume_struct, req.profile),
        "errors": errors,
    }
    _write_json(os.path.join(outdir, "job.json"), manifest)
    _write_json(os.path.join(outdir, "tailored.json"),
                {"resume": resume_struct, "cover": cover_struct})
    return manifest


@router.get("/jobs")
def list_jobs(email: str = Query(...)):
    """Every job this user has generated, newest first."""
    base = user_dir(email)
    if not os.path.isdir(base):
        return {"count": 0, "jobs": []}
    jobs = []
    for name in sorted(os.listdir(base), reverse=True):
        if not JOB_ID_RE.match(name) or not os.path.isdir(os.path.join(base, name)):
            continue
        item = {"job_id": name}
        mf = os.path.join(base, name, "job.json")
        if os.path.exists(mf):
            try:
                with open(mf, encoding="utf-8") as f:
                    m = json.load(f)
                item.update({"jd": m.get("jd", {}), "created": m.get("created"),
                             "files": m.get("files", []), "warnings": m.get("warnings", [])})
            except Exception:
                item["note"] = "manifest unreadable"
        jobs.append(item)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/artifacts/{job_id}")
def artifacts(job_id: str, email: str = Query(...)):
    """List the generated files for one job (with sizes + download URLs)."""
    d = job_dir(email, job_id)
    if not os.path.isdir(d):
        raise HTTPException(status_code=404, detail="job not found")
    files = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p) or not FILE_RE.match(name):
            continue
        files.append({"name": name, "bytes": os.path.getsize(p),
                      "url": "/api/electronic/artifacts/%s/%s" % (job_id, name)})
    manifest = {}
    mp = os.path.join(d, "job.json")
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {"note": "manifest unreadable"}
    return {"job_id": job_id, "files": files, "manifest": manifest}


@router.get("/artifacts/{job_id}/{filename}")
def artifact(job_id: str, filename: str, email: str = Query(...)):
    """Download one generated file. Path traversal is impossible: the name is allow-listed."""
    if not FILE_RE.match(filename or "") or filename not in ALLOWED_FILES:
        raise HTTPException(status_code=400, detail="unknown artifact")
    d = job_dir(email, job_id)
    p = os.path.join(d, filename)
    # belt and braces: the resolved path must still be inside the user's job dir
    if os.path.commonpath([os.path.realpath(p), os.path.realpath(d)]) != os.path.realpath(d):
        raise HTTPException(status_code=400, detail="unknown artifact")
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="artifact not found")
    ext = os.path.splitext(filename)[1].lower()
    return FileResponse(p, media_type=MIME.get(ext, "application/octet-stream"), filename=filename)
