#!/usr/bin/env python3
"""tailor.py — tailor the master resume to a scraped job and render beautiful PDFs.

Pipeline (3 DO models via the droplet /v1 proxy; DO key never here):
  extract (jhw-extract): master resume_data.json + JD -> a TAILORED copy (summary rewritten to the
                         JD, each role's bullets re-angled truthfully, skills ordered by relevance).
  content (jhw-content): writes the cover-letter prose for this company/role.
Then renders templates/resume.html + templates/cover_letter.html (their CSS, tailored content) and
prints them to PDF with the container's Google Chrome. Never invents facts (project standing rule).

    python jhw.py tailor        (needs a prior `scrape`; reads out/job.json)
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
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return httpx.post(f"{PROXY}/chat/completions", headers={"Authorization": f"Bearer {TOKEN}"}, json=payload, timeout=180)
    r = _post(alias)
    if r.status_code >= 400 and fb:
        print(f"[warn] {alias} -> {r.status_code}; falling back to {fb}", file=sys.stderr)
        r = _post(fb)
    if r.status_code >= 400:
        sys.exit(f"[ERR] LLM {alias} -> {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def as_json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def css_of(path):
    m = re.search(r"<style>(.*?)</style>", open(path, encoding="utf-8").read(), re.S)
    return m.group(1) if m else ""


# ---------- LLM tailoring ----------
def tailor_resume(base, jd):
    system = ("You tailor a resume to a job. Output ONLY JSON. NEVER invent facts, employers, dates "
              "or achievements — only re-angle and reorder what is already provided to match the job.")
    user = f"""MASTER RESUME DATA (facts — do not change employers/titles/dates):
{json.dumps(base)[:9000]}

TARGET JOB:
title: {jd.get('title','')}
company: {jd.get('company','')}
description: {jd.get('description','')[:5000]}

Return JSON:
{{
 "summary": "3-4 line professional summary rewritten to match this job, using only true facts",
 "skills_top": ["~14 skills from the master data, ordered by relevance to this JD (ATS keywords)"],
 "experience": [
    {{"company": "<exact company string from master>", "highlights": ["2-5 bullets re-angled to the JD, same facts"]}}
 ]
}}
Keep every company from the master resume. Only rephrase/reorder bullets; do not fabricate."""
    return as_json(call("jhw-extract", system, user, json_mode=True, max_tokens=3500, fb="jhw-extract_fb"))


def tailor_cover(base, jd):
    system = ("You write a concise, senior cover letter. Output ONLY JSON. Use only true facts from the "
              "candidate data; be specific to the job; no clichés, no fabrication.")
    b = base.get("basics", {})
    user = f"""CANDIDATE (facts): name {b.get('name')}, {base.get('summary','')[:800]}
Current role highlights: {json.dumps(base.get('experience',[])[:2])[:1500]}
TARGET JOB: {jd.get('title','')} at {jd.get('company','')}
JD: {jd.get('description','')[:3500]}
Return JSON: {{"salutation":"Hiring Team (or specific)","opening":"1 short para: interest + hook","proof":"1 para: most relevant proof from the candidate facts, mapped to the JD","fit":"1 short para: why this company/mission + soft close"}}"""
    return as_json(call("jhw-content", system, user, json_mode=True, max_tokens=2000, fb="jhw-content_fb"))


# ---------- rendering ----------
def render_resume(base, t):
    b = base.get("basics", {})
    css = css_of(RES_TPL)
    # merge tailored highlights by company
    hmap = {e.get("company"): e.get("highlights", []) for e in t.get("experience", []) if isinstance(e, dict)}
    contact = "".join(f"<span>{esc(x)}</span>" for x in [
        b.get("location",""), b.get("phone",""), b.get("email",""),
        b.get("linkedin",""), (b.get("websites",[]) or [""])[0]] if x)
    exp_groups = "".join(
        f'<div class="exp-grp"><b>{esc(k)}</b><p>{esc(" · ".join(v))}</p></div>'
        for k, v in base.get("expertise", {}).items())
    jobs = ""
    for e in base.get("experience", []):
        hl = hmap.get(e.get("company")) or e.get("highlights", [])
        when = " · ".join(x for x in [f'{e.get("start","")}{(" – "+e.get("end","")) if e.get("end") else ""}'.strip(" –"), e.get("location","")] if x)
        lede = f'<p class="lede">{esc(e.get("summary"))}</p>' if e.get("summary") else ""
        bullets = "".join(f"<li>{esc(x)}</li>" for x in hl) if hl else ""
        ul = f"<ul>{bullets}</ul>" if bullets else ""
        jobs += (f'<div class="job"><div class="job-top">'
                 f'<div><span class="role">{esc(e.get("title"))}</span> &nbsp;·&nbsp; '
                 f'<span class="co">{esc(e.get("company"))}</span></div>'
                 f'<span class="when">{esc(when)}</span></div>{lede}{ul}</div>')
    skills = t.get("skills_top") or base.get("skills_flat", [])[:14]
    chips = "".join(f'<span class="chip">{esc(s)}</span>' for s in skills[:16])
    certs = "".join(f"<li>{esc(c)}</li>" for c in base.get("certifications", []))
    edu = base.get("education", [{}])[0]
    langs = " · ".join(l.get("name","") for l in base.get("languages", []))
    body = f"""<div class="page">
  <header>
    <div class="name">{esc(b.get('name'))}</div>
    <div class="headline">{esc(b.get('headline'))}</div>
    <div class="tagline">{esc(b.get('tagline'))}</div>
    <div class="contact">{contact}</div>
  </header>
  <section><h2>Professional Summary</h2><p class="summary">{esc(t.get('summary') or base.get('summary'))}</p></section>
  <section><h2>Core Expertise</h2><div class="exp-grid">{exp_groups}</div></section>
  <section><h2>Professional Experience</h2>{jobs}</section>
  <div class="band">
    <div><h2>Education</h2><p><span class="k">{esc(edu.get('credential'))}</span><br>{esc(edu.get('org'))} · {esc(edu.get('years'))}</p>
      <h2 style="margin-top:11px">Languages</h2><p>{esc(langs)}<br><span style="color:var(--muted)">All native / bilingual</span></p></div>
    <div><h2>Certifications</h2><ul>{certs}</ul>
      <h2 style="margin-top:9px">Awards</h2><p>{esc(" · ".join(base.get('awards',[])))}</p></div>
    <div><h2>Key Skills</h2><div class="chips">{chips}</div></div>
  </div>
</div>"""
    return f"<!DOCTYPE html><html lang=en><head><meta charset=UTF-8>{FONTS}<style>{css}</style></head><body>{body}</body></html>"


def render_cover(base, jd, c):
    b = base.get("basics", {})
    css = css_of(COV_TPL)
    today = time.strftime("%d %B %Y")
    contact = "".join(f"<span>{esc(x)}</span>" for x in [b.get("location",""), b.get("phone",""), b.get("email",""), b.get("linkedin","")] if x)
    body = f"""<div class="page">
  <header><div class="name">{esc(b.get('name'))}</div>
    <div class="headline">{esc(b.get('headline'))}</div>
    <div class="contact">{contact}</div></header>
  <div class="meta">{esc(today)}</div>
  <div class="to"><div class="k">{esc(jd.get('company'))}</div><div>Hiring Team — {esc(jd.get('title'))}</div></div>
  <div class="greet">Dear {esc(c.get('salutation') or 'Hiring Team')},</div>
  <p class="body">{esc(c.get('opening'))}</p>
  <p class="body">{esc(c.get('proof'))}</p>
  <p class="body">{esc(c.get('fit'))}</p>
  <p class="body">I would welcome the opportunity to discuss how my experience maps to your needs. Thank you for your time and consideration.</p>
  <div class="sign"><div>Sincerely,</div><div class="nm" style="margin-top:6px">{esc(b.get('name'))}</div>
    <div class="tt">{esc(b.get('headline'))}</div></div>
</div>"""
    return f"<!DOCTYPE html><html lang=en><head><meta charset=UTF-8>{FONTS}<style>{css}</style></head><body>{body}</body></html>"


def to_pdf(html_path, pdf_path):
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or "/usr/bin/google-chrome-stable"
    cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
           "--user-data-dir=/tmp/print-profile", "--no-pdf-header-footer",
           "--virtual-time-budget=4000", "--run-all-compositor-stages-before-draw",
           f"--print-to-pdf={pdf_path}", f"file://{html_path}"]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def main():
    jp = os.path.join(OUT, "job.json")
    if not os.path.exists(jp):
        sys.exit(f"[ERR] {jp} not found — run `jhw.py scrape <job>` first.")
    jd = json.load(open(jp, encoding="utf-8"))
    base = json.load(open(DATA, encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)

    print("[i] extract (jhw-extract): tailoring resume to the JD ...")
    t = tailor_resume(base, jd)
    print("[i] content (jhw-content): writing the cover letter ...")
    c = tailor_cover(base, jd)

    res_html = os.path.join(OUT, "resume.html"); open(res_html, "w", encoding="utf-8").write(render_resume(base, t))
    cov_html = os.path.join(OUT, "cover_letter.html"); open(cov_html, "w", encoding="utf-8").write(render_cover(base, jd, c))
    json.dump({"job": {"title": jd.get("title"), "company": jd.get("company")}, "tailored": t, "cover": c,
               "candidate": base.get("basics", {})},
              open(os.path.join(OUT, "fields.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print("[i] rendering PDFs with Chrome ...")
    try:
        to_pdf(res_html, os.path.join(OUT, "resume.pdf"))
        to_pdf(cov_html, os.path.join(OUT, "cover_letter.pdf"))
        print(f"[OK] wrote resume.pdf, cover_letter.pdf, resume.html, cover_letter.html, fields.json in {OUT}")
    except subprocess.CalledProcessError as e:
        print(f"[WARN] PDF render failed: {e.stderr.decode()[:300]}\n       HTML files are in {OUT} (open + Ctrl+P).")


if __name__ == "__main__":
    main()
