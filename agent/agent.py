"""jhw-agent — deterministic-first browser agent.

  scrape <job>  read a LinkedIn job -> out/job.json   (LLM, fast)
  apply  <job>  STAGE 1 (Playwright, deterministic): check LinkedIn login, open job, click Apply,
                detect Easy-Apply vs external ATS.  On Workday, STAGE 1b (Playwright) also drives the
                cookie/Apply/account/My-Information/resume steps — NO LLM.  STAGE 2 (LLM/browser-use,
                governance) only fills the remaining screening questions, ask_human for unknowns, and
                STOPS at Review. The LLM never navigates LinkedIn or creates a LinkedIn account.
"""
from __future__ import annotations
import argparse, asyncio, json, os, re, secrets, sys, time
import httpx
from browser_use import Agent, BrowserSession, BrowserProfile, ChatOpenAI, Tools
import ask, obs
from flows import linkedin, workday, page_agent, llm_driver, greenhouse, easyapply, icims, workday_gevernova

PROXY   = os.getenv("JHW_PROXY_BASE", "http://host.docker.internal:8000/v1")
TOKEN   = os.getenv("AGENT_PROXY_TOKEN", "none")
CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
COOKIES = os.getenv("LINKEDIN_COOKIES", "/agent/linkedin_cookies.json")
OUT     = os.getenv("JHW_OUT", "/agent/out")
DATA    = os.getenv("JHW_DATA", "/agent/templates/resume_data.json")
JOB_URL = "https://www.linkedin.com/jobs/view/{jid}/"

# LLM chain used ONLY for the form fill (Stage 2). (alias, use_vision) — match model capability.
APPLY_CHAIN = [("jhw-driver", False), ("jhw-driver2", True), ("jhw-driver3", False)]
STEPS_PER_TRY = int(os.getenv("JHW_STEPS_PER_TRY", "40"))


def parse_jid(a):
    m = re.search(r"(\d{6,})", a)
    if not m:
        sys.exit(f"[ERR] no job id in '{a}'")
    return m.group(1)


def load_cookies():
    try:
        raw = json.load(open(COOKIES, encoding="utf-8"))
    except FileNotFoundError:
        return None
    ss = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None", "unspecified": "Lax", "": "Lax"}
    ck = []
    for c in raw:
        if not c.get("name") or c.get("value") is None:
            continue
        ck.append({"name": c["name"], "value": c["value"], "domain": c.get("domain", ".linkedin.com"),
                   "path": c.get("path", "/"), "expires": -1 if c.get("session") else float(c.get("expirationDate", c.get("expires", -1)) or -1),
                   "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", True)),
                   "sameSite": ss.get(str(c.get("sameSite", "")).lower().strip(), "Lax")})
    out = "/tmp/jhw_storage_state.json"
    json.dump({"cookies": ck, "origins": []}, open(out, "w"))
    return out


def mk_session():
    return BrowserSession(cdp_url=CDP_URL,
                          browser_profile=BrowserProfile(storage_state=load_cookies(), enable_default_extensions=False))


def make_llm(alias):
    return ChatOpenAI(model=alias, base_url=PROXY, api_key=TOKEN, add_schema_to_system_prompt=True, timeout=120)


async def llm_answer(question: str, options, profile) -> str:
    """TEXT-ONLY LLM call (no vision, no screenshots, no agent loop) to answer a screening question.
       Returns the chosen option text / a short answer, or 'UNKNOWN' if not derivable from the facts."""
    b = profile.get("basics", {})
    facts = (f"Name: {b.get('name')} (legal: {b.get('legal_name')}). Location: {b.get('location')}, "
             f"{b.get('address','')}. Currently employed in Germany at Colt Technology Services. "
             f"Languages: {', '.join(l.get('name') for l in b.get('languages', []))}. "
             f"Email {b.get('email')}, phone {b.get('phone')}.")
    instr = ("Choose EXACTLY ONE option and reply with its exact text: " + " | ".join(options)) if options \
            else "Reply with a short direct answer."
    msgs = [
        {"role": "system", "content": "You complete job-application questions for a candidate. Answer "
         "truthfully and ONLY from the given facts. If the answer is not determinable (citizenship, "
         "visa/sponsorship, exact salary, notice period, anything not in the facts), reply with exactly "
         "UNKNOWN. Reply with the answer only — no explanation."},
        {"role": "user", "content": f"Facts:\n{facts}\n\nQuestion: {question}\n{instr}"},
    ]
    # retry DOWN the model chain on timeout/error: Gemma 4 -> DeepSeek 3.2 -> GLM-5.2 (never dead-stop).
    for alias in ("jhw-answer", "jhw-answer2", "jhw-answer3"):
        try:
            async with httpx.AsyncClient(timeout=45) as c:
                rr = await c.post(f"{PROXY}/chat/completions",
                                  headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                  json={"model": alias, "messages": msgs, "temperature": 0.2})
                rr.raise_for_status()
                out = (rr.json()["choices"][0]["message"]["content"] or "").strip()
                if out:
                    return out
        except Exception as e:
            print(f"[warn] llm_answer {alias} failed: {e}; trying next model")
    return ""


def build_tools():
    tools = Tools()

    @tools.action("Ask the human candidate for a value you do not have or are unsure about (postal "
                  "address, phone, work authorization, salary, notice period, a screening answer). "
                  "One clear question. Returns their answer, or NO_ANSWER (then leave blank + note it).")
    async def ask_human(question: str) -> str:
        a = await ask.ask_human(question)
        return a or "NO_ANSWER: human did not reply; leave blank and list it for review."

    return tools


def candidate_block(d):
    b = d.get("basics", {})
    lines = [f"Name: {b.get('name')}", f"Email: {b.get('email')}", f"Phone: {b.get('phone')}",
             f"Address: {b.get('address','')}", f"Location: {b.get('location','')}",
             f"LinkedIn: {b.get('linkedin','')}", f"Website: {(b.get('websites',[]) or [''])[0]}",
             f"Languages: {', '.join(l.get('name') for l in b.get('languages', []))}", "", "WORK EXPERIENCE:"]
    for e in d.get("experience", []):
        when = f"{e.get('start','')}-{e.get('end','')}".strip("-") or "-"
        lines.append(f"- {e.get('title')} at {e.get('company')} ({when}) - {e.get('summary','')}")
    edu = (d.get("education") or [{}])[0]
    lines += ["", f"EDUCATION: {edu.get('credential','')}, {edu.get('org','')} ({edu.get('years','')})",
              f"CERTIFICATIONS: {', '.join(d.get('certifications', []))}",
              f"SKILLS: {', '.join(d.get('skills_flat', [])[:25])}"]
    return "\n".join(lines)


def apply_context():
    data = json.load(open(DATA, encoding="utf-8"))
    email = data.get("basics", {}).get("email", "")
    pw = "Jhw!" + secrets.token_urlsafe(10).replace("-", "x").replace("_", "y") + "9"
    creds = {"email": email, "password": pw, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    json.dump(creds, open(os.path.join(OUT, "credentials.json"), "w"), indent=2)
    files = [p for p in [os.path.join(OUT, "resume.pdf"), os.path.join(OUT, "cover_letter.pdf")] if os.path.exists(p)]
    if not files:
        print("[WARN] no resume.pdf in out/ — run `jhw.py tailor` first.", file=sys.stderr)
    return candidate_block(data), files, creds


SCRAPE_TASK = ("Go to {url} . You are logged in. Read the full LinkedIn job, expanding any 'see more'. "
 "Finish by outputting ONLY a JSON object: title, company, location, workplace_type, "
 "apply_type ('easy_apply'|'external'), description (full role text). Empty strings if not shown; no invention.")

# Stage-2 tasks: browser is ALREADY on the right page (Playwright got us here). No LinkedIn nav.
APPLY_ATS = """You are on the company ATS application page (e.g. Workday) — LinkedIn is already handled, do NOT go back to LinkedIn.
1) If THIS ATS asks to sign in / create an account, use email {email} and password {password} (this is the ATS account, NOT LinkedIn). If the account exists, sign in.
2) If "Autofill with Resume" is offered, upload the resume, then correct fields.
3) Fill My Information, Work Experience, Education and screening questions using ONLY this data (never invent employers/dates/authorization/salary):
------------------------------------------------------------
{candidate}
------------------------------------------------------------
Upload the resume PDF at any resume/CV upload.
4) For any value NOT in the data (address, work authorization, sponsorship, salary, notice period, custom questions) CALL ask_human and use the reply; only leave blank if it returns NO_ANSWER. Click consent checkboxes when required.
HUMAN GATE: fill to the final Review page then STOP. NEVER click the final Submit. Report the ATS, what you filled/uploaded, and fields still needing the human."""

EASY_APPLY = """You are in LinkedIn's "Easy Apply" dialog — complete it, do NOT navigate away.
Fill each step (contact info, resume, screening questions) using ONLY this data:
------------------------------------------------------------
{candidate}
------------------------------------------------------------
Upload the resume PDF if asked. For any value not in the data, CALL ask_human. Click Next/Review through the steps.
HUMAN GATE: stop at the final "Submit application" — NEVER submit; a human does. Report what you filled and what still needs the human."""

APPLY_RESUME = """You are PART-WAY through the application in the CURRENT tab (deterministic Playwright already did the account + My-Information + resume upload). Do NOT restart, do NOT re-enter the account, do NOT leave the page. Assess the current page and continue toward Review.
- Fill only the REMAINING fields: screening questions, work authorization, voluntary disclosures, EEO, consent checkboxes.
- Use ONLY this data; for any value not in it, CALL ask_human:
------------------------------------------------------------
{candidate}
------------------------------------------------------------
- ATS account (only if it asks again): email {email}, password {password}.
- Click Next / Save and Continue to advance pages.
HUMAN GATE: reach Review and STOP. NEVER click final Submit. Report what you filled and what still needs the human."""

FULL_APPLY = """Go to {url} . You are logged in on LinkedIn. Apply to this job for the candidate.
1) Click the job's Apply button (it may say "Apply" or "Apply on company website"). If it opens a company ATS (e.g. Workday) in a NEW TAB, switch to that tab and continue there.
2) If the ATS asks to sign in / create an account, use email {email} and password {password} (ATS account, NOT LinkedIn).
3) If "Autofill with Resume" is offered, upload the resume, then correct fields.
4) Fill My Information, Experience, Education and screening questions using ONLY this data (no invention):
------------------------------------------------------------
{candidate}
------------------------------------------------------------
Upload the resume PDF at any resume/CV upload. For any value not in the data, CALL ask_human.
HUMAN GATE: fill to the final Review page then STOP. NEVER click the final Submit. Report status."""

# Handed to Alibaba page-agent (in-page LLM) when the deterministic Workday driver stalls mid-form.
PAGEAGENT_FINISH = """You are on a Workday job application, already SIGNED IN, part way through. Finish it for the candidate. Do NOT sign out and do NOT go back to the job posting.
Fill My Information (legal name, address, phone), Work Experience, Education, and any screening / EEO / disclosure questions using ONLY this data:
------------------------------------------------------------
{candidate}
------------------------------------------------------------
The resume file was already uploaded by the system — do NOT try to upload files. Tick any required consent/terms checkbox. Click "Save and Continue" (or Next) to advance through each page. For a value not in the data, pick the most truthful option or leave it.
STOP at the final Review page. NEVER click Submit."""


async def run_scrape(jid):
    url = JOB_URL.format(jid=jid)
    if not await linkedin.ensure_login():
        print("[ACTION] not logged into LinkedIn — see message above."); return
    print("[i] scrape (LLM) — watch localhost:9090")
    agent = Agent(task=SCRAPE_TASK.format(url=url), llm=make_llm("jhw-extract"),
                  browser_session=mk_session(), use_vision=False, max_actions_per_step=4)
    hist = await agent.run(max_steps=15)
    final = (hist.final_result() if hasattr(hist, "final_result") else str(hist)) or ""
    print("\n===== RESULT =====\n" + final[:2500])
    if final:
        m = re.search(r"\{.*\}", final, re.S)
        data = None
        if m:
            try: data = json.loads(m.group(0))
            except Exception: pass
        data = data or {"raw": final}
        data.setdefault("job_id", jid); data.setdefault("url", url)
        json.dump(data, open(os.path.join(OUT, "job.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print(f"[OK] wrote {OUT}/job.json")


async def run_apply(jid):
    block, files, creds = apply_context()
    print("[i] Stage 1: deterministic LinkedIn (Playwright, no LLM) …")
    prep = await linkedin.prepare_application(jid)
    print(f"[i] LinkedIn -> status={prep['status']} apply_type={prep['apply_type']} :: {prep['note']}")
    if prep["status"] != "logged_in":
        print("[ACTION] " + (prep.get("note") or "Log into LinkedIn at http://localhost:9090/vnc.html, then re-run apply."))
        return

    data = json.load(open(DATA, encoding="utf-8"))
    resume_path = files[0] if files else ""
    ats = (prep.get("ats_url") or "").lower()

    _t0 = time.time()
    obs.event("apply_start", job=jid, apply_type=prep["apply_type"], ats=ats[:60])

    async def _report(kind, res):
        stage = res.get("stage") or ("review" if res.get("ok") else "paused")
        obs.event("apply_result", job=jid, ats=kind, stage=str(stage),
                  filled=len(set(res.get("filled", []) or [])),
                  ms=int((time.time() - _t0) * 1000), note=(res.get("note") or "")[:200])
        print(f"[i] {kind} -> ok={res.get('ok')} stage={stage} "
              f"filled={sorted(set(res.get('filled', []) or []))}")
        print("    " + (res.get("note") or ""))
        try:
            await ask.notify(f"{kind}: stage={stage}. {(res.get('note') or '')[:180]}")
        except Exception:
            pass
        print("\n[i] Review in noVNC (http://localhost:9090) and Submit yourself if ready.")

    # ROUTER — dispatch to the adapter built from the REAL recording for this ATS
    # (see RECORDINGS_FINDINGS.md). Deterministic drives; the LLM only answers free-text questions.
    if prep["apply_type"] == "easy_apply":
        print("[i] LinkedIn Easy Apply — recorded iframe adapter + text-LLM for questions ...")
        await _report("easy_apply", await easyapply.drive(data, resume_path, llm_answer)); return

    if prep["apply_type"] == "external" and "gevernova" in ats:
        print("[i] GE Vernova Workday — deterministic recorded adapter (careers->popup->Autofill->sign-in->Review) ...")
        await _report("workday_ge", await workday_gevernova.drive(creds, data, resume_path, llm_answer,
                                                                  prep.get("ats_url", ""))); return

    if prep["apply_type"] == "external" and re.search(r"myworkday|workday", ats):
        print("[i] Workday — recorded adapter (deep-link, SSO / Use-My-Last-App, resume parse wait) ...")
        await _report("workday", await workday.drive(creds, data, resume_path, llm_answer,
                                                      prep.get("ats_url", ""))); return

    if prep["apply_type"] == "external" and "greenhouse.io" in ats:
        print("[i] Greenhouse — recorded adapter ...")
        await _report("greenhouse", await greenhouse.drive(data, resume_path,
                                                           prep.get("ats_url", ""), llm_answer)); return

    if prep["apply_type"] == "external" and "icims.com" in ats:
        print("[i] iCIMS — recorded adapter (pre-fill, then hand CAPTCHA/OTP to the human) ...")
        await _report("icims", await icims.drive(data, resume_path,
                                                 prep.get("ats_url", ""), llm_answer)); return

    # Unknown ATS / apply type -> our generic text-DOM LLM driver (proxy, no CDN; asks Telegram when
    # it needs a value). NO browser-use, NO screenshots.
    print("[i] Unknown ATS / apply type — generic LLM DOM driver (our proxy + Telegram) ...")
    ld = await llm_driver.run(block, resume_path)
    await _report("llm_driver", {"ok": ld.get("ok"), "stage": "review" if ld.get("ok") else "paused",
                                 "filled": ld.get("actions", []), "note": ld.get("note", "")})
    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", choices=["scrape", "apply", "login"])
    ap.add_argument("job", nargs="?", default=""); ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.goal == "login":
        asyncio.run(linkedin.ensure_login()); return
    jid = parse_jid(a.job)
    asyncio.run(run_scrape(jid) if a.goal == "scrape" else run_apply(jid))


if __name__ == "__main__":
    main()
