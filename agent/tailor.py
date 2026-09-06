#!/usr/bin/env python3
"""tailor.py — tailor master resume_data.json to a scraped job and render PDFs.

Master facts: templates/resume_data.json  (NEVER invent employers/dates)
Style only:   templates/resume.html + cover_letter.html  (CSS extracted; body is rebuilt)
Output:       out/resume.pdf, out/cover_letter.pdf, out/resume.html, out/cover_letter.html

    python jhw.py scrape <job>
    python jhw.py tailor
    python jhw.py apply  <url>
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, sys, time, html as htmlmod
import httpx

PROXY   = os.getenv("JHW_PROXY_BASE", "http://host.docker.internal:8000/v1")
TOKEN   = os.getenv("AGENT_PROXY_TOKEN", "none")
OUT     = os.getenv("JHW_OUT", "/agent/out")
TPLDIR  = os.getenv("JHW_TEMPLATES", "/agent/templates")
DATA    = os.path.join(TPLDIR, "resume_data.json")
RES_TPL = os.path.join(TPLDIR, "resume.html")
COV_TPL = os.path.join(TPLDIR, "cover_letter.html")
FONTS   = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
           '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">')


def esc(s):
    return htmlmod.escape(str(s or ""))


def call(alias, system, user, json_mode=False, max_tokens=3500, fb=None):
    def _post(mdl):
        payload = {"model": mdl, "temperature": 0.3, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return httpx.post(f"{PROXY}/chat/completions",
                          headers={"Authorization": f"Bearer {TOKEN}"},
                          json=payload, timeout=180)
    r = _post(alias)
    if r.status_code >= 400 and fb:
        print(f"[warn] {alias} -> {r.status_code}; falling back to {fb}", file=sys.stderr)
        r = _post(fb)
    if r.status_code >= 400:
        sys.exit(f"[ERR] LLM {alias} -> {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def as_json(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def css_of(path):
    """Templates are STYLE SOURCES only. Body content is rebuilt from resume_data.json."""
    try:
        raw = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        return _fallback_css()
    m = re.search(r"<style>(.*?)</style>", raw, re.S)
    return (m.group(1) if m else "") or _fallback_css()


def _fallback_css():
    return """
@page { size: A4; margin: 13mm 14mm 12mm 14mm; }
* { box-sizing: border-box; }
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 8.6pt; line-height: 1.34; color: #1f2328; margin: 0; }
h1, .name { font-size: 22pt; color: #1f4e79; margin: 0 0 1mm 0; letter-spacing: -0.2pt; font-weight: 700; }
.role, .headline { font-size: 9.6pt; color: #3c434a; margin: 0 0 2mm 0; }
.contact { font-size: 7.9pt; color: #5b6167; border-top: 0.6pt solid #c9d3dc; padding-top: 1.6mm; margin-bottom: 3.6mm; }
.contact a { color: #5b6167; text-decoration: none; }
h2 { font-size: 11pt; color: #1f4e79; margin: 4.2mm 0 1.8mm 0; padding-bottom: 0.8mm; border-bottom: 0.9pt solid #1f4e79; }
h3 { font-size: 9.3pt; color: #17384f; margin: 2.6mm 0 0.5mm 0; }
h4 { font-size: 8.7pt; color: #1f2328; margin: 2.2mm 0 0.3mm 0; }
.meta { font-size: 7.7pt; color: #6b7178; margin: 0 0 0.9mm 0; }
p { margin: 0 0 1.4mm 0; }
ul { margin: 0.4mm 0 1.4mm 0; padding-left: 3.6mm; }
li { margin-bottom: 0.5mm; }
.umbrella { border-left: 2.2pt solid #1f4e79; padding-left: 3.2mm; margin-top: 1.4mm; }
.eng { margin-bottom: 1.6mm; }
.skills { font-size: 8.2pt; }
.foot { font-size: 7.3pt; color: #6b7178; margin-top: 3mm; border-top: 0.6pt solid #c9d3dc; padding-top: 1.4mm; }
"""


def _when(e: dict) -> str:
    """Support both from/to (our schema) and start/end (legacy)."""
    a = str(e.get("from") or e.get("start") or "").strip()
    b = str(e.get("to") or e.get("end") or "").strip()
    if a and b:
        return f"{a} – {b}"
    return a or b or ""


def _flatten_experience(base: dict) -> list[dict]:
    """Expand nested roles under consultancy umbrella into renderable job blocks."""
    out = []
    for e in base.get("experience") or []:
        roles = e.get("roles") or []
        if roles:
            # parent umbrella once
            out.append({
                "company": e.get("company"),
                "title": e.get("title"),
                "location": e.get("location"),
                "from": e.get("from"),
                "to": e.get("to"),
                "highlights": e.get("highlights") or [],
                "is_umbrella": True,
            })
            for r in roles:
                out.append({
                    "company": e.get("company"),
                    "title": r.get("title") or r.get("role"),
                    "location": r.get("location") or e.get("location"),
                    "from": r.get("from") or r.get("start"),
                    "to": r.get("to") or r.get("end"),
                    "highlights": r.get("highlights") or [],
                    "is_subrole": True,
                })
        else:
            out.append({
                "company": e.get("company"),
                "title": e.get("title"),
                "location": e.get("location"),
                "from": e.get("from") or e.get("start"),
                "to": e.get("to") or e.get("end"),
                "highlights": e.get("highlights") or [],
            })
    return out


def _skills_list(base: dict, tailored: dict | None = None) -> list:
    if tailored and tailored.get("skills_top"):
        return list(tailored["skills_top"])[:16]
    return list(base.get("skills") or base.get("skills_flat") or [])[:16]


def _edu_line(ed: dict) -> tuple[str, str]:
    degree = ed.get("degree") or ed.get("credential") or ""
    school = ed.get("school") or ed.get("org") or ""
    years = ""
    if ed.get("from") or ed.get("to"):
        years = f"{ed.get('from','')} – {ed.get('to','')}".strip(" –")
    else:
        years = str(ed.get("years") or "")
    loc = ed.get("location") or ""
    meta = " · ".join(x for x in [years, loc] if x)
    return f"{degree}", f"{school}" + (f" · {meta}" if meta else "")


# ---------- LLM tailoring ----------
def tailor_resume(base, jd):
    system = (
        "You tailor a resume to a job. Output ONLY JSON. NEVER invent facts, employers, dates "
        "or achievements — only re-angle and reorder what is already provided to match the job."
    )
    # Flatten for the model so nested roles are visible
    flat = []
    for e in _flatten_experience(base):
        if e.get("is_umbrella"):
            continue
        flat.append({
            "company": e.get("company"),
            "title": e.get("title"),
            "from": e.get("from"),
            "to": e.get("to"),
            "highlights": e.get("highlights") or [],
        })
    payload = {
        "basics": base.get("basics"),
        "summary": base.get("summary"),
        "skills": base.get("skills") or base.get("skills_flat"),
        "experience": flat[:18],
        "education": base.get("education"),
        "certifications": base.get("certifications"),
    }
    user = f"""MASTER RESUME DATA (facts — do not change employers/titles/dates):
{json.dumps(payload, ensure_ascii=False)[:12000]}

TARGET JOB:
title: {jd.get('title','')}
company: {jd.get('company','')}
description: {jd.get('description','')[:5000]}

Return JSON:
{{
 "summary": "3-4 line professional summary rewritten to match this job, using only true facts",
 "skills_top": ["~14 skills from the master data, ordered by relevance to this JD (ATS keywords)"],
 "experience": [
    {{"company": "<exact company string from master>", "title": "<exact title>", "highlights": ["2-5 bullets re-angled to the JD, same facts"]}}
 ]
}}
Keep real employers only. Only rephrase/reorder bullets; do not fabricate."""
    return as_json(call("jhw-extract", system, user, json_mode=True, max_tokens=4000, fb="jhw-extract_fb"))


def tailor_cover(base, jd):
    system = (
        "You write a concise, senior cover letter. Output ONLY JSON. Use only true facts from the "
        "candidate data; be specific to the job; no clichés, no fabrication."
    )
    b = base.get("basics", {})
    # Prefer recent concrete roles for proof
    flat = [e for e in _flatten_experience(base) if not e.get("is_umbrella")][:4]
    user = f"""CANDIDATE (facts): name {b.get('name')}, headline {b.get('headline')}
Summary: {base.get('summary','')[:900]}
Recent roles: {json.dumps(flat, ensure_ascii=False)[:2200]}
TARGET JOB: {jd.get('title','')} at {jd.get('company','')}
JD: {jd.get('description','')[:3500]}
Return JSON: {{"salutation":"Hiring Team","opening":"1 short para: interest + hook from real experience","proof":"1-2 paras: most relevant proof mapped to the JD (real employers only)","fit":"1 short para: why this company/mission + soft close"}}"""
    return as_json(call("jhw-content", system, user, json_mode=True, max_tokens=2200, fb="jhw-content_fb"))


# ---------- rendering ----------
def render_resume(base, t):
    b = base.get("basics", {}) or {}
    css = css_of(RES_TPL)
    # Map tailored highlights: key by company, and by company|title
    hmap_co, hmap_title = {}, {}
    for e in (t.get("experience") or []):
        if not isinstance(e, dict):
            continue
        co = (e.get("company") or "").strip()
        ti = (e.get("title") or "").strip()
        hl = e.get("highlights") or []
        if co:
            hmap_co[co] = hl
        if co and ti:
            hmap_title[f"{co}|{ti}"] = hl

    contact_bits = []
    for x in [b.get("address") or b.get("location"), b.get("phone"), b.get("email"),
              b.get("linkedin"), b.get("website") or ((b.get("websites") or [""])[0])]:
        if x:
            contact_bits.append(esc(x))
    contact = " &nbsp;|&nbsp; ".join(contact_bits)

    jobs_html = []
    for e in _flatten_experience(base):
        if e.get("is_umbrella"):
            jobs_html.append(
                f'<h3>{esc(e.get("title"))}</h3>'
                f'<div class="meta">{esc(_when(e))} &nbsp;|&nbsp; {esc(e.get("location"))}</div>'
                f'<p>{esc(" ".join(e.get("highlights") or []))}</p>'
                f'<div class="umbrella">'
            )
            continue
        key = f"{(e.get('company') or '').strip()}|{(e.get('title') or '').strip()}"
        hl = hmap_title.get(key) or hmap_co.get((e.get("company") or "").strip()) or e.get("highlights") or []
        bullets = "".join(f"<li>{esc(x)}</li>" for x in hl if x)
        ul = f"<ul>{bullets}</ul>" if bullets else ""
        if e.get("is_subrole"):
            jobs_html.append(
                f'<div class="eng"><h4>{esc(e.get("title"))}</h4>'
                f'<div class="meta">{esc(_when(e))}</div>{ul}</div>'
            )
        else:
            jobs_html.append(
                f'<h3>{esc(e.get("title"))} &middot; {esc(e.get("company"))}</h3>'
                f'<div class="meta">{esc(_when(e))} &nbsp;|&nbsp; {esc(e.get("location"))}</div>{ul}'
            )
    # close umbrella if left open
    html_jobs = "".join(jobs_html)
    if '<div class="umbrella">' in html_jobs and html_jobs.count('<div class="umbrella">') > html_jobs.count("</div></div>"):
        html_jobs += "</div>"

    skills = _skills_list(base, t)
    skills_html = " &nbsp;&bull;&nbsp; ".join(esc(s) for s in skills)

    edu_blocks = []
    for ed in base.get("education") or []:
        deg, meta = _edu_line(ed)
        edu_blocks.append(f"<h4>{esc(deg)}</h4><div class=\"meta\">{esc(meta)}</div>")
    certs = " &nbsp;&bull;&nbsp; ".join(esc(c) for c in (base.get("certifications") or []))
    langs = " &nbsp;&bull;&nbsp; ".join(
        f"{esc(l.get('name'))} — {esc(l.get('level') or 'Fluent')}"
        for l in (base.get("languages") or b.get("languages") or [])
        if isinstance(l, dict)
    )

    summary = (t.get("summary") if t else None) or base.get("summary") or ""

    body = f"""
<h1>{esc(b.get('name'))}</h1>
<div class="role">{esc(b.get('headline'))}</div>
<div class="contact">{contact}</div>

<h2>Summary</h2>
<p>{esc(summary)}</p>

<h2>Core Expertise</h2>
<p class="skills">{skills_html}</p>

<h2>Experience</h2>
{html_jobs}

<h2>Education</h2>
{''.join(edu_blocks)}

<h2>Certifications</h2>
<p class="skills">{certs}</p>

<h2>Languages</h2>
<p class="skills">{langs}</p>
"""
    return (f"<!DOCTYPE html><html lang=en><head><meta charset=UTF-8>{FONTS}"
            f"<style>{css}</style></head><body>{body}</body></html>")


def render_cover(base, jd, c):
    b = base.get("basics", {}) or {}
    css = css_of(COV_TPL)
    contact_bits = [x for x in [b.get("location"), b.get("phone"), b.get("email"),
                                b.get("linkedin"), b.get("website")] if x]
    contact = " &nbsp;|&nbsp; ".join(esc(x) for x in contact_bits)
    opening = c.get("opening") or ""
    proof = c.get("proof") or ""
    fit = c.get("fit") or ""
    sal = c.get("salutation") or "Hiring Team"
    body = f"""
<h1>{esc(b.get('name'))}</h1>
<div class="role">{esc(b.get('headline'))}</div>
<div class="contact">{contact}</div>

<p>Dear {esc(sal)},</p>
<p>{esc(opening)}</p>
<p>{esc(proof)}</p>
<p>{esc(fit)}</p>
<p>I would welcome the opportunity to discuss how my experience maps to your needs.</p>
<p class="sig">Kind regards,<br><b>{esc(b.get('name'))}</b></p>
"""
    return (f"<!DOCTYPE html><html lang=en><head><meta charset=UTF-8>{FONTS}"
            f"<style>{css}</style></head><body>{body}</body></html>")


def to_pdf(html_path, pdf_path):
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or "/usr/bin/google-chrome-stable")
    cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
           "--user-data-dir=/tmp/print-profile", "--no-pdf-header-footer",
           "--virtual-time-budget=4000", "--run-all-compositor-stages-before-draw",
           f"--print-to-pdf={pdf_path}", f"file://{html_path}"]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def _load_jd():
    jp = os.path.join(OUT, "job.json")
    if os.path.exists(jp):
        return json.load(open(jp, encoding="utf-8"))
    # Allow direct-ATS applies: synthetic JD from env or argv
    title = os.getenv("JHW_JOB_TITLE", "").strip()
    company = os.getenv("JHW_JOB_COMPANY", "").strip()
    desc = os.getenv("JHW_JOB_DESC", "").strip()
    if title or company:
        return {"title": title, "company": company, "description": desc}
    sys.exit(f"[ERR] {jp} not found — run `jhw.py scrape <job>` first, or set JHW_JOB_TITLE/COMPANY.")


def main():
    jd = _load_jd()
    if not os.path.isfile(DATA):
        sys.exit(f"[ERR] master resume missing: {DATA}\n"
                 "Copy the real resume_data.json (Evgeny, not Alex Example) into templates/.")
    base = json.load(open(DATA, encoding="utf-8"))
    name = (base.get("basics") or {}).get("name", "")
    if not name or name.lower().startswith("alex example"):
        sys.exit("[ERR] templates/resume_data.json is still the Alex Example placeholder. "
                 "Replace it with the real master (Evgeny Vainshtein).")
    os.makedirs(OUT, exist_ok=True)

    print(f"[i] master: {name}  |  target: {jd.get('title','?')} @ {jd.get('company','?')}")
    print("[i] extract (jhw-extract): tailoring resume to the JD ...")
    t = tailor_resume(base, jd)
    print("[i] content (jhw-content): writing the cover letter ...")
    c = tailor_cover(base, jd)

    res_html = os.path.join(OUT, "resume.html")
    cov_html = os.path.join(OUT, "cover_letter.html")
    open(res_html, "w", encoding="utf-8").write(render_resume(base, t))
    open(cov_html, "w", encoding="utf-8").write(render_cover(base, jd, c))
    json.dump({"job": {"title": jd.get("title"), "company": jd.get("company")},
               "tailored": t, "cover": c, "candidate": base.get("basics", {})},
              open(os.path.join(OUT, "fields.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    print("[i] rendering PDFs with Chrome ...")
    try:
        to_pdf(res_html, os.path.join(OUT, "resume.pdf"))
        to_pdf(cov_html, os.path.join(OUT, "cover_letter.pdf"))
        print(f"[OK] wrote resume.pdf, cover_letter.pdf, resume.html, cover_letter.html, fields.json in {OUT}")
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode()[:300] if isinstance(e.stderr, (bytes, bytearray)) else str(e)[:300]
        print(f"[WARN] PDF render failed: {err}\n       HTML files are in {OUT} (open + Ctrl+P).")


if __name__ == "__main__":
    main()
