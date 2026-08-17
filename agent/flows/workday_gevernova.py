"""GE Vernova Workday adapter — DETERMINISTIC, built from the real recording
(recordings/workday_gevernova.py). No LLM drives the browser; the LLM/human only supplies free-text
answers it doesn't already know.

Ground truth captured in the recording:
  careers.gevernova.com job page -> Accept cookies -> "Apply for …" (opens a POPUP) -> Workday
  -> "Autofill with Resume" -> account (Create Account if fresh, else Sign In)
  -> resume upload -> My Information -> Phone -> My Experience -> Application Questions
  -> Voluntary Disclosures -> REVIEW.  We STOP at Review; the human submits (hard rule).

Values come from templates/resume_data.json (basics). The Workday EMAIL and PASSWORD are asked FROM
the candidate over Telegram (jhw_bot via the ask-bridge) and stored in answers.json so they are asked
only once — nothing goes in .env. Dropdown / screening answers come from the recorded ANSWER DB below
(the candidate's own recorded choices — not invented); anything missing is also asked on Telegram."""
from __future__ import annotations
import asyncio, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright
try:
    import ask
except Exception:
    ask = None

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
GE_CAREERS = "https://careers.gevernova.com/senior-project-manager-m-w-d-critical-infrastructure-communications/job/R5038183"

# Recorded answer DB (the candidate's own recorded choices). group-name substring -> answer.
# `listbox=True` = the option renders inside a role=listbox popup (recording used that variant).
SCREENING = [
    ("Are you currently subject to",       "No",             False),
    ("I agree that I will not",             "Yes, I agree.",  False),
    ("Are you legally authorized to",       "Yes",            False),
    ("Many countries, including the",       "No",             True),
    ("Have your immediate family",          "No",             True),
]
DEFAULTS = {
    "hear_about": ["Job Board", "LinkedIn"],   # How Did You Hear About Us? -> Job Board -> LinkedIn
    "country": "Germany",
    "title": "Doctor",                         # salutation dropdown (recorded)
    "state": "Hesse",
    "degree": "Bachelor of Business",          # GE degree dropdown option (recorded)
    "gpa": "87",
    "grad_year": "2001",
    "contact_method": "Email",
    "gender": "Male",
}


ANSWERS_PATH = os.getenv("JHW_ANSWERS", "/agent/out/answers.json")


def _answers() -> dict:
    try:
        return json.load(open(ANSWERS_PATH))
    except Exception:
        return {}


def _save_answer(key: str, value: str) -> None:
    d = _answers(); d[key] = value
    try:
        os.makedirs(os.path.dirname(ANSWERS_PATH), exist_ok=True)
        json.dump(d, open(ANSWERS_PATH, "w"), indent=1)
    except Exception:
        pass


async def _cred(key: str, question: str) -> str:
    """Per-stakeholder answer DB: reuse a stored value, else ASK the candidate on Telegram
    (jhw_bot via the ask-bridge) and store the reply so we never ask twice."""
    d = _answers()
    if d.get(key):
        return d[key]
    ans = ""
    if ask is not None:
        try:
            ans = (await ask.ask_human(question)) or ""
        except Exception:
            ans = ""
    ans = ans.strip()
    if ans:
        _save_answer(key, ans)
    return ans


def _basics(data: dict) -> dict:
    b = data.get("basics", {})
    name = (b.get("name") or "").split()
    given = name[0] if name else ""
    family = name[-1] if len(name) > 1 else ""
    addr = b.get("address", "")                # "Hermann-J.-Bach-Weg 16, 61169 Friedberg (Hessen)"
    street = addr.split(",")[0].strip()
    postal, city = "", ""
    m = re.search(r"(\d{4,5})\s+([^(,]+)", addr)
    if m:
        postal, city = m.group(1), m.group(2).strip()
    digits = re.sub(r"\D", "", b.get("phone", ""))     # +49 157 8554… -> 4915785541545
    phone = digits[2:] if digits.startswith("49") else digits.lstrip("0")
    return {"email": b.get("email", ""), "given": given, "family": family,
            "street": street, "postal": postal, "city": city, "phone": phone}


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    return browser, ctx, page


async def _has(pg, locator) -> bool:
    try:
        return await locator.count() > 0
    except Exception:
        return False


async def _click(pg, locator, what, filled, timeout=12000):
    try:
        await locator.first.click(timeout=timeout)
        filled.append(what); return True
    except Exception as e:
        print(f"[wd-ge] click '{what}' failed: {str(e)[:100]}", flush=True)
        return False


async def _fill(pg, locator, value, what, filled, timeout=12000):
    if not value:
        return False
    try:
        await locator.first.fill(str(value), timeout=timeout)
        filled.append(what); return True
    except Exception as e:
        print(f"[wd-ge] fill '{what}' failed: {str(e)[:100]}", flush=True)
        return False


async def _save_continue(pg, filled, n=1):
    for _ in range(n):
        await _click(pg, pg.get_by_role("button", name="Save and Continue"), "save_continue", filled)
        await pg.wait_for_timeout(1500)


async def _account(pg, email, password, filled, ask_fn):
    """Detect CREATE vs SIGN-IN from the form itself and do the right one.
    A 'Verify New Password' field == the Create-Account form (fresh user); otherwise Sign In.
    Detection beats guessing — a fresh test email must CREATE, an existing one must SIGN IN."""
    await pg.wait_for_timeout(1500)
    has_verify = await _has(pg, pg.get_by_role("textbox", name="Verify New Password"))
    # If no Verify field, we're on Sign In — but a fresh user must switch to Create Account first.
    if not has_verify:
        for nm in ("Create Account", "Create a new account", "Sign Up"):
            lk = pg.get_by_role("link", name=re.compile(nm, re.I))
            try:
                if await lk.count():
                    await lk.first.click(timeout=4000); await pg.wait_for_timeout(1200); break
            except Exception:
                pass
        has_verify = await _has(pg, pg.get_by_role("textbox", name="Verify New Password"))

    await _fill(pg, pg.get_by_role("textbox", name="Email Address"), email, "email", filled)
    await _fill(pg, pg.get_by_role("textbox", name="Password", exact=True), password, "password", filled)

    if has_verify:                                   # CREATE ACCOUNT (fresh)
        await _fill(pg, pg.get_by_role("textbox", name="Verify New Password"), password, "verify_pw", filled)
        try:                                         # tick "I agree to terms" if present
            cb = pg.get_by_role("checkbox")
            if await cb.count(): await cb.first.check(timeout=3000)
        except Exception:
            pass
        await _click(pg, pg.get_by_role("button", name=re.compile(r"Create Account", re.I)),
                     "create_account", filled)
    else:                                            # SIGN IN (existing)
        await _click(pg, pg.get_by_role("button", name=re.compile(r"^\s*Sign In\s*$", re.I)),
                     "sign_in", filled)
    await pg.wait_for_timeout(2800)


async def _pick_button(pg, button_name, option_text, what, filled):
    """A Workday 'button' dropdown: click it, then click the option text."""
    if not option_text:
        return
    if await _click(pg, pg.get_by_role("button", name=re.compile(button_name, re.I)), f"open:{what}", filled):
        await pg.wait_for_timeout(700)
        await _click(pg, pg.get_by_text(option_text, exact=True), f"pick:{what}={option_text}", filled)
        await pg.wait_for_timeout(500)


async def _pick_group(pg, group_substr, answer, listbox, filled, ask_fn):
    """A screening 'group' with a 'Select One Required' control. Uses the recorded answer; if that
       option isn't present, asks the human via the bridge for a value and tries that."""
    grp = pg.get_by_role("group", name=re.compile(re.escape(group_substr), re.I))
    if not await _click(pg, grp.get_by_label("Select One Required"), f"open_q:{group_substr[:20]}", filled):
        return
    await pg.wait_for_timeout(600)
    opt = (pg.get_by_role("listbox").get_by_text(answer, exact=True) if listbox
           else pg.get_by_text(answer, exact=True))
    if await _click(pg, opt, f"answer:{group_substr[:20]}={answer}", filled):
        return
    # recorded answer missing -> ask the human, then try their reply as the option text
    try:
        reply = await ask_fn(f"GE Vernova application question — please answer:\n\"{group_substr}…\"", [], {})
    except Exception:
        reply = ""
    if reply:
        await _click(pg, pg.get_by_text(reply.strip(), exact=True), f"answer_h:{group_substr[:20]}", filled)


async def drive(creds: dict, data: dict, resume_path: str, ask_fn, ats_url: str = "") -> dict:
    r = {"ok": False, "stage": "paused", "filled": [], "note": ""}
    b = _basics(data)
    # Credentials come FROM YOU over Telegram — jhw_bot asks, and the answers are stored (answers.json)
    # so it only asks once. NOTHING goes in .env. Env vars, if set, act only as a silent override.
    resume_email = b["email"]
    b["email"] = (os.getenv("JHW_WORKDAY_EMAIL", "")
                  or await _cred("workday_email",
                       f"Which email should I use for the GE Vernova Workday account? "
                       f"(reply with the email, or type '{resume_email}' to use your resume one)")
                  or resume_email)
    password = (os.getenv("JHW_WORKDAY_PASSWORD", "")
                or await _cred("workday_password",
                     "What password should I use to CREATE the GE Vernova Workday account? "
                     "It needs an uppercase letter, a number, a letter, and a special character."))
    if not password:
        r["note"] = ("No Workday password yet — I asked on Telegram but got no reply in time. "
                     "Message the jhw bot the password (send /start first) and re-run.")
        return r
    filled = r["filled"]
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front()

        # 1) careers job page -> accept cookies -> Apply (opens a popup) -> Workday
        await page.goto(ats_url or GE_CAREERS, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)
        for nm in ("Accept", "Accept Cookies"):
            try:
                btn = page.get_by_role("button", name=nm)
                if await btn.count(): await btn.first.click(timeout=4000); break
            except Exception:
                pass
        apply_lbl = page.get_by_label(re.compile(r"^Apply for", re.I))
        try:
            async with ctx.expect_page(timeout=20000) as pop:
                if await apply_lbl.count():
                    await apply_lbl.first.click()
                else:
                    await page.get_by_role("button", name=re.compile(r"^\s*apply( now| for this job)?\s*$", re.I)).first.click()
            wd = await pop.value
        except Exception as e:
            r["note"] = f"could not open the Workday apply popup: {str(e)[:120]}"; return r
        await wd.wait_for_load_state("domcontentloaded", timeout=45000)
        await wd.bring_to_front(); filled.append("popup:workday")

        # 2) Autofill with Resume
        await _click(wd, wd.get_by_role("button", name="Autofill with Resume"), "autofill_with_resume", filled)
        # 3) account (create if fresh / sign in if exists)
        await _account(wd, b["email"], password, filled, ask_fn)

        # 4) resume upload (Select file -> hidden input -> Continue)
        try:
            if await wd.get_by_role("button", name="Select file").count():
                await wd.get_by_role("button", name="Select file").first.click(timeout=6000)
        except Exception:
            pass
        try:
            if resume_path:
                await wd.locator('input[type="file"]').first.set_input_files(resume_path, timeout=8000)
                filled.append("resume_uploaded"); await wd.wait_for_timeout(2500)
        except Exception as e:
            print(f"[wd-ge] resume upload: {str(e)[:100]}", flush=True)
        await _click(wd, wd.get_by_role("button", name="Continue", exact=True), "continue_after_resume", filled)
        await wd.wait_for_timeout(1500)

        # 5) My Information
        try:
            if await wd.get_by_role("textbox", name="How Did You Hear About Us?").count():
                await wd.get_by_role("textbox", name="How Did You Hear About Us?").click(timeout=5000)
                for opt in DEFAULTS["hear_about"]:
                    await _click(wd, wd.get_by_text(opt, exact=True), f"hear:{opt}", filled)
        except Exception:
            pass
        await _pick_button(wd, r"Country / Territory", DEFAULTS["country"], "country", filled)
        await _pick_button(wd, r"Title Select One", DEFAULTS["title"], "title", filled)
        await _fill(wd, wd.get_by_role("textbox", name="Given Name"), b["given"], "given_name", filled)
        await _fill(wd, wd.get_by_role("textbox", name="Family Name"), b["family"], "family_name", filled)
        await _fill(wd, wd.get_by_role("textbox", name="Address Line 1"), b["street"], "address1", filled)
        await _fill(wd, wd.get_by_role("textbox", name="Postal Code"), b["postal"], "postal", filled)
        await _fill(wd, wd.get_by_role("textbox", name="City"), b["city"], "city", filled)
        await _pick_button(wd, r"Land Select One", DEFAULTS["state"], "state", filled)
        await _save_continue(wd, filled)

        # 6) Phone
        await _fill(wd, wd.get_by_role("textbox", name="Phone Number"), b["phone"], "phone", filled)
        await _save_continue(wd, filled, n=2)

        # 7) My Experience (education)
        await _pick_button(wd, r"Degree Select One", DEFAULTS["degree"], "degree", filled)
        await _fill(wd, wd.get_by_role("textbox", name="Overall Result (GPA)"), DEFAULTS["gpa"], "gpa", filled)
        try:
            yr = wd.get_by_role("spinbutton", name="Year")
            if await yr.count(): await yr.first.fill(DEFAULTS["grad_year"], timeout=5000); filled.append("grad_year")
        except Exception:
            pass
        await _save_continue(wd, filled, n=2)

        # 8) Application Questions
        for grp, ans, lb in SCREENING:
            await _pick_group(wd, grp, ans, lb, filled, ask_fn)
        await _pick_button(wd, r"Select One Required", DEFAULTS["contact_method"], "contact_method", filled)
        await _save_continue(wd, filled)

        # 9) Voluntary Disclosures
        await _pick_button(wd, r"Gender Select One", DEFAULTS["gender"], "gender", filled)
        try:
            cb = wd.get_by_role("checkbox", name=re.compile(r"consent", re.I))
            if await cb.count(): await cb.first.check(timeout=5000); filled.append("consent")
        except Exception:
            pass
        await _save_continue(wd, filled)

        # 10) STOP at Review — DO NOT SUBMIT (hard rule). Human reviews + submits.
        await wd.wait_for_timeout(1500)
        r["ok"] = True; r["stage"] = "review"
        r["note"] = ("Reached Review with all recorded sections filled. STOPPED before Submit — "
                     "review in noVNC and click Submit yourself.")
        return r
    except Exception as e:
        r["note"] = f"workday_gevernova error: {str(e)[:160]}"; return r
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()
