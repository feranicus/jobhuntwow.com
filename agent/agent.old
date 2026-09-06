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
from flows import atscreds, ashby
from flows import (linkedin, workday, page_agent, llm_driver, greenhouse, easyapply, icims,
                   workday_gevernova, stagehand, electronic, phenom, workday_wd)

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
    """LinkedIn job id, or a DIRECT ATS URL kept intact.

    The old version regex-grabbed the first 6+ digit run from anything. An NTT/Phenom URL
    (".../NTT1GLOBAL337575EXTERNALENGLOBAL/...") therefore collapsed to "337575", the real target
    was lost, and the run fell back to searching LinkedIn for a job id that does not exist there.
    """
    s_ = str(a or "").strip()
    if s_.startswith(("http://", "https://")) and "linkedin.com" not in s_.lower():
        return s_                      # the URL IS the identifier - never reduce it to digits
    m = re.search(r"(\d{6,})", s_)
    if not m:
        sys.exit(f"[ERR] no job id or ATS URL in '{a}'")
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
    lines += ["", _screening_block()]
    return "\n".join(lines)


def _screening_block() -> str:
    """The ALWAYS-answers from userdata/candidate.md, verbatim, plus a start date computed at
    RUN TIME.

    These were written down weeks ago and never reached the form driver: candidate_block() was
    built only from resume_data.json, so the model had no idea travel="Yes" was already decided
    and asked the human on Telegram - twice - for answers that were sitting on disk. A fact the
    engine owns must never become a question.
    """
    import datetime as _dt
    start = (_dt.date.today() + _dt.timedelta(days=30)).strftime("%d-%m-%Y")
    lines = ["SCREENING ANSWERS - these are DECIDED. Use them verbatim; never ask the human, "
             "never invent an alternative:",
             f"- available_start_date: {start}  (one month out, recomputed every run; write it in "
             "the format the field's placeholder asks for, e.g. dd-mm-yyyy)",
             "- notice_period: 1 month",
             "- salary_expectation: 150000 EUR gross per year"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "userdata", "candidate.md")
    try:
        keep, txt = [], open(path, encoding="utf-8").read()
        sect = txt.split("## screening_defaults", 1)
        if len(sect) == 2:
            for ln in sect[1].splitlines():
                if ln.startswith("##"):
                    break
                s = ln.strip()
                # available_start_date is computed above - the file's literal date goes stale.
                if s.startswith("- ") and not s.startswith("- available_start_date"):
                    keep.append(s)
        lines += keep
    except Exception as e:
        print(f"[warn] could not read screening defaults ({str(e)[:70]}) - "
              "the driver will have to ask", file=sys.stderr)
    return "\n".join(lines)


def _ats_email(data: dict):
    """(address, provenance). ONE decision, stated out loud.

    MEASURED 2026-08-16, and it would have silently broken the whole unattended path: the ATS login
    address had FOUR homes and they disagreed.
        templates/resume_data.json basics.email  -> feranicus@s4biz.io   (what the account used)
        userdata/candidate.md      email         -> evgeny@s4biz.io
        out/ats_accounts.json      accenture     -> feranicus@s4biz.io
        out/ats_accounts.json      redhat        -> feranicus@gmail.com
    The candidate then set JHW_MAIL_USER=evgeny@s4biz.io, so Workday would have mailed the
    verification code to feranicus@ while the reader watched evgeny@ -- finding nothing, forever,
    and looking exactly like a broken mailbox reader rather than a mismatch. Same disease as
    ENRICH_MODELS having four homes: generalise one layer, leave a stale one in front, and the stale
    one silently wins.
    RULE: THE ADDRESS WE REGISTER WITH MUST BE THE MAILBOX WE CAN READ. `JHW_MAIL_USER` is by
    definition a mailbox the candidate controls, so when it is set it is the best available answer
    and it is used -- unless JHW_ATS_EMAIL says otherwise explicitly."""
    import os as _os
    override = (_os.getenv("JHW_ATS_EMAIL") or "").strip()
    if override:
        return override, "JHW_ATS_EMAIL (explicit override)"
    mail = (_os.getenv("JHW_MAIL_USER") or "").strip()
    resume = (data.get("basics", {}) or {}).get("email", "") or ""
    if mail:
        if resume and mail.lower() != resume.lower():
            print(f"[creds] NOTE: registering with the MAILBOX we can read ({mail}) rather than the "
                  f"r\u00e9sum\u00e9 address ({resume}) — the verification code has to land somewhere we "
                  f"can read it. Set JHW_ATS_EMAIL to override.", flush=True)
        return mail, "JHW_MAIL_USER (the mailbox we can read)"
    return resume, "resume_data.json basics.email (no mailbox configured)"


def apply_context(url: str = ""):
    """MEASURED BUG, fixed 2026-08-16: this used to do
        pw = "Jhw!" + secrets.token_urlsafe(10)... + "9"
    i.e. mint a BRAND-NEW password on EVERY run and overwrite out/credentials.json. An
    account created last week could therefore never be signed into again, because the only
    copy of its password had been thrown away. Meanwhile flows/workday.py already read a
    PERSISTENT per-tenant store (out/ats_accounts.json) that this function never touched:
    one credential, two homes, and the ephemeral home won.

    flows/atscreds.py is now the single owner. A password is invented ONCE PER ATS VENDOR
    and remembered forever, so every Workday tenant is opened with the same password while
    the ACCOUNT stays per tenant (Workday accounts do not span employers)."""
    data = json.load(open(DATA, encoding="utf-8"))
    email, why = _ats_email(data)
    print(f"[creds] ATS login e-mail = {email}   ({why})", flush=True)
    acct = atscreds.account(url, email)
    creds = {"email": acct["email"], "password": acct["password"],
             "vendor": acct["vendor"], "tenant": acct["tenant"], "exists": acct["exists"]}
    # out/credentials.json is kept ONLY as a read-only convenience for the older flows that
    # still expect it. atscreds owns the truth; this file is a projection of it, never a source.
    json.dump({k: v for k, v in creds.items() if k != "password"} | {"password": "***"},
              open(os.path.join(OUT, "credentials.json"), "w"), indent=2)
    print(f"[creds] {acct['vendor']}/{acct['tenant']} "
          f"({'existing account' if acct['exists'] else 'no account recorded yet'})", flush=True)
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
    block, files, creds = apply_context(str(jid or ''))

    # A DIRECT ATS URL MUST NOT GO ANYWHERE NEAR LINKEDIN. Stage 1 exists to resolve a LinkedIn
    # job id into its external "Apply" target; when the caller already handed us that target,
    # visiting linkedin.com is pure overhead - and it fails the whole run if LinkedIn is
    # unreachable or the session expired, for a job that has nothing to do with LinkedIn.
    _u = str(jid or "")
    _is_url = _u.startswith("http://") or _u.startswith("https://")
    if _is_url and "linkedin.com" not in _u.lower():
        prep = {"status": "logged_in", "apply_type": "external", "ats_url": _u,
                "note": "direct ATS URL - LinkedIn stage skipped"}
        print(f"[i] direct ATS URL -> skipping LinkedIn entirely :: {_u[:90]}")
    else:
        print("[i] Stage 1: deterministic LinkedIn (Playwright, no LLM) …")
        prep = await linkedin.prepare_application(jid)
        print(f"[i] LinkedIn -> status={prep['status']} apply_type={prep['apply_type']} :: {prep['note']}")
        if prep["status"] != "logged_in":
            note = prep.get("note") or ""
            if "ERR_INTERNET_DISCONNECTED" in note or "ERR_NAME_NOT_RESOLVED" in note:
                print("[ACTION] the SANDBOX has no internet (not your PC). Recreate it:\n"
                      "         python jhw.py local-down  &&  python jhw.py apply <url>")
            else:
                print("[ACTION] " + (note or "Log into LinkedIn at http://localhost:9090/vnc.html, then re-run apply."))
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
        if stage == "submitted":
            print("\n[OK] Application SUBMITTED. Nothing left for you to do.")
        else:
            print("\n[i] NOT submitted. Open http://localhost:9090 to see where it stopped.")

    # ROUTER — dispatch to the adapter built from the REAL recording for this ATS
    # (see RECORDINGS_FINDINGS.md). Deterministic drives; the LLM only answers free-text questions.
    if prep["apply_type"] == "easy_apply":
        print("[i] LinkedIn Easy Apply — recorded iframe adapter + text-LLM for questions ...")
        await _report("easy_apply", await easyapply.drive(data, resume_path, llm_answer)); return

    if prep["apply_type"] == "external" and "gevernova" in ats:
        print("[i] GE Vernova Workday — deterministic recorded adapter (careers->popup->Autofill->sign-in->Review) ...")
        await _report("workday_ge", await workday_gevernova.drive(creds, data, resume_path, llm_answer,
                                                                  prep.get("ats_url", ""))); return

    # WORKDAY IS DISPATCHED FURTHER DOWN, ON PURPOSE. It used to be handled here, which put it
    # BEFORE the Electronic document-tailoring block - so it applied with whatever stale PDF was
    # left in out/ from a previous job. The Workday branch now sits directly after tailoring (and
    # before Stagehand, because we have recorded ground truth for Workday and a generic agent must
    # not be turned loose on it). flows/workday.py -- built from the candidate's own recording --
    # is the DEFAULT; JHW_WD_EXPERIMENTAL=1 opts into the unverified workday_wd.py instead.

    if prep["apply_type"] == "external" and "greenhouse.io" in ats:
        print("[i] Greenhouse — recorded adapter ...")
        # TWO DIFFERENT THINGS, deliberately: `llm_answer` writes free-text prose with a model,
        # `ask.ask_human` asks the CANDIDATE. Passing the model in as the human rung would let it
        # answer a veteran/disability declaration on his behalf — which is the exact behaviour he
        # suspected on the OKX run and which must never be possible.
        await _report("greenhouse", await greenhouse.drive(
            data, resume_path, prep.get("ats_url", ""), answer_fn=llm_answer,
            asker=ask.ask_human)); return

    if prep["apply_type"] == "external" and "ashbyhq.com" in ats:
        # ASHBY, FROM HIS RECORDING. Before this the URL fell through to "Unknown ATS" -> Stagehand
        # (which answered HTTP 500 and did nothing, ~40s) -> the generic driver, which filled ONE field
        # of fourteen and pressed Submit. The recording is the ground truth; a generic agent must not
        # be turned loose on a form with a mandatory consent box.
        print("[i] Ashby — recorded adapter (upload -> location -> salary/notice -> radios -> essays)")
        await _report("ashby", await ashby.drive(
            data, resume_path, answer_fn=llm_answer, asker=ask.ask_human,
            ats_url=prep.get("ats_url", ""))); return

    if prep["apply_type"] == "external" and "icims.com" in ats:
        print("[i] iCIMS — recorded adapter (pre-fill, then hand CAPTCHA/OTP to the human) ...")
        await _report("icims", await icims.drive(data, resume_path,
                                                 prep.get("ats_url", ""), llm_answer)); return

    # Unknown ATS (Phenom/NTT, SmartRecruiters, Ashby, ...) -> STAGEHAND first: act/observe are
    # schema-constrained, so it follows the DOM instead of narrating. It was built and running but
    # NEVER wired into this router, which is why every unknown site silently fell through to the
    # text-DOM driver. Stagehand is tried first and we fall back only if it is unavailable/fails.
    target = prep.get("ats_url") or ""

    # PHENOM (NTT and most "careers.<brand>" sites): the apply form is a URL derived from the job
    # URL, so navigate straight to it. Clicking "Apply now" is fragile - the cookie banner, the
    # "Career Guide" chat widget and Chrome's "Restore pages?" bubble all overlay that button.
    # MEASURED, and it broke the Workday target before it was guarded: phenom.is_phenom() matches
    # ANY url containing "/job/<x>", so
    #   .../AccentureCareers/job/Hamburg/Agentic-AI-..._R00329883/apply
    # was claimed as Phenom and rewritten to
    #   .../AccentureCareers/apply?jobSeqNo=Hamburg&step=1
    # - the LOCATION slug taken for a Phenom job-sequence number and the real path thrown away.
    # A more specific ATS must be excluded before a pattern this broad is allowed to rewrite a URL.
    if phenom.is_phenom(target) and not (workday_wd.is_workday(target)
                                         or workday_wd.is_workday_gateway(target)):
        au = phenom.apply_url(target)
        if au and au != target:
            print(f"[i] Phenom ATS -> going straight to the application form: {au[:100]}")
            target = au
            prep["ats_url"] = au

    # DOCUMENTS FIRST. The ATS demands "Upload Resume" and "Upload your Cover Letter", so ask the
    # deployed Electronic backend to tailor BOTH to this posting and download them into out/.
    # CACHE: that is TWO LLM calls on the droplet plus four downloads - the 1-2 minute wait before
    # the browser does anything. The documents depend only on the JOB + profile, so rebuilding
    # them for the same posting is pure latency. JHW_FRESH_DOCS=1 forces a rebuild.
    import hashlib as _hl
    doc_note = ""
    _stamp = os.path.join(OUT, ".tailored_for")
    _key = _hl.sha1((target or "").encode()).hexdigest()[:16]
    _have = all(os.path.isfile(os.path.join(OUT, f)) for f in ("resume.pdf", "cover_letter.pdf"))
    _same = False
    if os.path.isfile(_stamp):
        try:
            _same = open(_stamp, encoding="utf-8").read().strip() == _key
        except Exception:
            _same = False

    if _have and _same and os.getenv("JHW_FRESH_DOCS", "").lower() not in ("1", "true", "yes"):
        _age = int(time.time() - os.path.getmtime(os.path.join(OUT, "resume.pdf")))
        print(f"[i] reusing documents already tailored for THIS posting ({_age // 60}m old)"
              " - set JHW_FRESH_DOCS=1 to rebuild")
        files = [os.path.join(OUT, f) for f in ("resume.pdf", "cover_letter.pdf")]
        resume_path = files[0]
        doc_note = "cached"
    else:
        try:
            page_text = ""
            try:
                page_text = json.dumps(data)[:12000]
            except Exception:
                pass
            tail = await electronic.tailor_for(target, page_text, block)
            if tail.get("ok"):
                for nm, pth in tail["files"].items():
                    if nm == "resume.pdf":
                        resume_path = pth
                files = list(tail["files"].values())
                doc_note = "tailored: " + ", ".join(sorted(tail["files"]))
                print(f"[i] Electronic tailored this posting -> {doc_note}")
                try:
                    open(_stamp, "w", encoding="utf-8").write(_key)
                except Exception:
                    pass
                if tail.get("warnings"):
                    print("    truth-check: "
                          + "; ".join(str(w)[:90] for w in tail["warnings"][:3]))
            else:
                print(f"[warn] Electronic tailoring unavailable ({tail.get('note')})"
                      " - using whatever is in out/")
        except Exception as e:
            print(f"[warn] Electronic tailoring error ({repr(e)[:110]}) - using out/ files")

    # WORKDAY (*.myworkdayjobs.com, and the accenture.com careers GATEWAY whose Workday URL is
    # NOT derivable by string rules - see userdata/skills/ats-workday/SKILL.md). The adapter owns
    # URL derivation, the account gate and Workday's button->listbox portal dropdowns, then hands
    # the rest of the form to the generic driver.
    if workday_wd.is_workday(target) or workday_wd.is_workday_gateway(target):
        # ROUTING CORRECTED 2026-08-16. flows/workday.py is built from the CANDIDATE'S OWN
        # Playwright recording of the full Accenture wd103 lifecycle (recordings/workday.py,
        # documented in RECORDINGS_FINDINGS.md): Accept Cookies -> Use My Last Application ->
        # Sign in with Google -> How-did-you-hear (Job Boards/LinkedIn) -> resume delete+upload
        # -> questions -> self-ID -> "I accept." -> Save and Continue -> Submit. It selects by
        # ROLE/LABEL/TEXT, which RECORDINGS_FINDINGS.md measured to be far more robust across
        # tenants than data-automation-id.
        # flows/workday_wd.py was written by me from DOCUMENTATION, has never been executed
        # against a real Workday page, and leans on data-automation-id (9 uses vs 2). Making it
        # the default demoted the ground-truth adapter to "legacy" behind an env var -- the exact
        # inversion of this repo's own rule: build adapters from the recordings, never from
        # guesses. Opt in with JHW_WD_EXPERIMENTAL=1 once it has actually been proven on a run.
        if os.getenv("JHW_WD_EXPERIMENTAL", "").lower() in ("1", "true", "yes"):
            kind = "gateway" if workday_wd.is_workday_gateway(target) else "direct"
            print(f"[i] Workday ({kind}) — EXPERIMENTAL adapter (JHW_WD_EXPERIMENTAL=1), "
                  f"never verified on a live page :: {target[:100]}")
            wd = await workday_wd.drive(creds, data, resume_path, llm_answer, target, facts=block)
        else:
            print(f"[i] Workday — recorded adapter (role/label selectors from the real "
                  f"Accenture lifecycle) :: {target[:100]}")
            await _report("workday", await workday.drive(creds, data, resume_path, llm_answer,
                                                        target))
            return
        stage = wd.get("stage") or ("review" if wd.get("ok") else "paused")
        if wd.get("ok"):
            jd_bits = data if isinstance(data, dict) else {}
            await electronic.mark_applied(
                target, "", company=str(jd_bits.get("company") or "")[:120],
                title=str(jd_bits.get("title") or "")[:160],
                status=stage, note=(wd.get("note") or "")[:200])
        if wd.get("warnings"):
            print("    warnings: " + "; ".join(str(w)[:90] for w in wd["warnings"][:4]))
        if wd.get("unresolved"):
            print(f"    unresolved: {wd['unresolved'][:4]}")
        await _report("workday", wd)
        return

    if target:
        try:
            h = await stagehand.health()
        except Exception as e:
            h = {"ok": False, "err": repr(e)[:80]}
        if h.get("ok"):
            print("[i] Unknown ATS — Stagehand (schema-constrained act/observe on our DO proxy) ...")
            try:
                await stagehand.goto(target)
                if resume_path:
                    try:
                        await stagehand.upload(resume_path)
                    except Exception as e:
                        print(f"[warn] stagehand upload skipped: {repr(e)[:80]}")
                res = await stagehand.agent(
                    "Fill this job application with the candidate's details. Upload the resume if "
                    "a file input is present. Answer screening questions from the facts given. "
                    "NEVER click the final Submit button - stop at the review/summary step.",
                    system=block, max_steps=25)
                steps = res.get("steps") or res.get("actions") or []
                if res.get("ok") or steps:
                    await _report("stagehand", {"ok": True, "stage": "review",
                                                "filled": steps, "note": res.get("note", "")})
                    return
                print(f"[warn] stagehand did nothing ({str(res)[:120]}) — falling back")
            except Exception as e:
                detail = ""
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        detail = " :: " + resp.text[:400]     # server.ts returns error + stack
                    except Exception:
                        pass
                print(f"[warn] stagehand failed ({repr(e)[:120]}){detail}")
                print("       logs: python jhw.py local-logs jhw-stagehand")
                print("[i] falling back to the DOM driver")
        else:
            print(f"[i] stagehand not reachable ({h.get('err', 'no health')}) — using the DOM driver")

    print("[i] Fallback — generic LLM DOM driver (our proxy + Telegram) ...")
    ld = await llm_driver.run(block, resume_path, url=prep.get("ats_url") or "")
    # The candidate asked for end-to-end automation: the driver clicks Submit itself and only
    # records status="submitted" when the SITE confirmed it. "review" means it stopped short.
    sent = bool(ld.get("submitted"))
    stage = "submitted" if sent else ("review" if ld.get("ok") else "paused")
    if ld.get("ok"):
        jd_bits = data if isinstance(data, dict) else {}
        await electronic.mark_applied(
            target, "", company=str(jd_bits.get("company") or "")[:120],
            title=str(jd_bits.get("title") or "")[:160],
            status=stage, note=(ld.get("note") or "")[:200])
    await _report("llm_driver", {"ok": ld.get("ok"), "stage": stage,
                                 "filled": ld.get("actions", []), "note": ld.get("note", "")})
    if sent:
        print(f"[OK] APPLICATION SUBMITTED — {target[:90]}", flush=True)
    elif ld.get("ok"):
        print("[i] Filled to Review but the site did not confirm a submission — check "
              "http://localhost:9090 before closing.", flush=True)
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
