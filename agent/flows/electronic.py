"""electronic.py — ask the Electronic backend (DigitalOcean) for documents tailored to THIS job.

Why: the ATS demanded "Upload Resume" and "Upload your Cover Letter", and the sandbox had only a
generic out/resume.pdf. The tailoring logic (truth-check, KEY ACHIEVEMENTS, employer-preservation,
best-practice length) already lives in the deployed backend, so the apply engine calls it instead
of duplicating it locally.

Flow:  POST /api/electronic/jd  ->  POST /api/electronic/generate  ->  download the artifacts.
Files land in out/ so the CDP upload shim (and the human, in noVNC) can use them immediately.
"""
from __future__ import annotations
import os
import httpx

BASE = os.getenv("JHW_ELECTRONIC_BASE", "https://jobhuntwow.com")
EMAIL = os.getenv("JHW_ELECTRONIC_EMAIL", "")
OUT = os.getenv("JHW_OUT", "/agent/out")
TIMEOUT = float(os.getenv("JHW_ELECTRONIC_TIMEOUT", "180"))


async def tailor_for(job_url: str, jd_text: str, profile_text: str) -> dict:
    """Returns {"ok", "files": {name: path}, "job_id", "warnings", "gaps", "note"}."""
    out: dict = {"ok": False, "files": {}, "job_id": "", "warnings": [], "gaps": [], "note": ""}
    if not EMAIL:
        out["note"] = "JHW_ELECTRONIC_EMAIL not set — cannot attribute documents to an account"
        return out
    if not (jd_text or "").strip() and not job_url:
        out["note"] = "no job description text and no url"
        return out

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
        jd = {}
        try:
            r = await c.post(f"{BASE}/api/electronic/jd",
                             json={"text": jd_text or "", "url": job_url or ""})
            jd = r.json() if r.status_code < 400 else {}
        except Exception as e:
            out["note"] = f"jd parse failed: {e!r}"[:200]
            return out
        if not str(jd.get("text") or "").strip():
            # LinkedIn/Indeed/NTT often block server-side fetching; the scraped page text is the
            # fallback and is usually better anyway (we are already logged in / rendered).
            if not (jd_text or "").strip():
                out["note"] = "JD could not be read server-side and no page text was supplied"
                return out
            jd = {"text": jd_text, "url": job_url, "title": "", "company": ""}

        try:
            r = await c.post(f"{BASE}/api/electronic/generate",
                             json={"email": EMAIL, "jd": jd, "profile": profile_text})
            man = r.json()
        except Exception as e:
            out["note"] = f"generate failed: {e!r}"[:200]
            return out
        if r.status_code >= 400 or man.get("detail"):
            out["note"] = str(man.get("detail") or f"HTTP {r.status_code}")[:300]
            return out

        jid = man.get("job_id", "")
        out.update({"job_id": jid, "warnings": man.get("warnings") or [],
                    "gaps": man.get("gaps") or []})
        os.makedirs(OUT, exist_ok=True)
        for name in (man.get("files") or []):
            try:
                rr = await c.get(f"{BASE}/api/electronic/artifacts/{jid}/{name}",
                                 params={"email": EMAIL})
                if rr.status_code >= 400:
                    continue
                p = os.path.join(OUT, name)
                with open(p, "wb") as f:
                    f.write(rr.content)
                out["files"][name] = p
            except Exception:
                continue
    out["ok"] = bool(out["files"])
    out["note"] = out["note"] or ("downloaded " + ", ".join(sorted(out["files"])))
    return out


async def mark_applied(job_url: str, job_id: str, company: str = "", title: str = "",
                       status: str = "applied", note: str = "") -> bool:
    """Record the application so it shows up in the Pipeline instead of living only in a log."""
    if not EMAIL:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BASE}/api/electronic/applied",
                             json={"email": EMAIL, "job_id": job_id, "url": job_url,
                                   "company": company, "title": title,
                                   "status": status, "note": note})
            return r.status_code < 400
    except Exception:
        return False
