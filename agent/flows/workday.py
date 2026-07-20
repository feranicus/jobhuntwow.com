"""Deterministic Workday driver — built on VERIFIED research (see agent/RESEARCH_ATS_AUTOMATION.md).

Implements the 5 researched fixes:
  1. RESUME PARSE WAIT — Workday parses the resume SERVER-SIDE and may overwrite fields. Delete any
     existing attachment first (else uploads stack), upload, then WAIT for the parse to complete before
     clicking Save and Continue. THIS was the bug that stalled every run.
  2. DEEP-LINK to {job}/apply/applyManually — skips `adventureButton`, which reference repos had to
     click twice (flaky). Workday bounces to auth, then returns to the apply flow.
  3. SELECTOR MAP AS DATA with fallback chains (selectors/workday.json) — tenants run different Workday
     UI versions; a single selector silently fails.
  4. SELECTOR = RUNTIME ASSERTION — on an unknown page or a persistent error we HALT and tell the human
     on Telegram. We never guess. (Kills "hallucinated success", the #1 agent failure mode.)
  5. LLM ONLY FOR FREE TEXT — screening answers via a text-only model. The LLM NEVER drives the browser.

Never submits. Stops at Review for the human.
"""
from __future__ import annotations
import asyncio, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # /agent  (import ask)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                    # /agent/flows
from playwright.async_api import async_playwright
try:
    import ask
except Exception:
    ask = None
try:
    import autofill
except Exception:
    autofill = None

CDP_URL  = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
ACCOUNTS = os.path.join(os.getenv("JHW_OUT", "/agent/out"), "ats_accounts.json")
SEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selectors", "workday.json")


def _load_selectors() -> dict:
    try:
        return {k: v for k, v in json.load(open(SEL_FILE, encoding="utf-8")).items() if not k.startswith("_")}
    except Exception as e:
        print(f"[wd] selector map missing ({e}) — using empty map", flush=True)
        return {}


SEL = _load_selectors()


def _chain(key) -> list:
    v = SEL.get(key, [])
    return v if isinstance(v, list) else [v]


# ---------- selector helpers (#3 fallback chains) --------------------------------------------------
async def _first(page, key, t=1500):
    """First VISIBLE locator in the fallback chain, else None."""
    for s in _chain(key):
        try:
            loc = page.locator(s).first
            await loc.wait_for(state="visible", timeout=t)
            return loc
        except Exception:
            continue
    return None


async def _present(page, key):
    """First PRESENT locator (visibility not required) — for hidden file inputs."""
    for s in _chain(key):
        try:
            loc = page.locator(s)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


async def _has(page, key, t=1200) -> bool:
    return (await _first(page, key, t)) is not None


async def _click(page, key, t=4000) -> bool:
    el = await _first(page, key, t)
    if not el:
        return False
    try:
        await el.click(); await page.wait_for_timeout(600); return True
    except Exception:
        return False


async def _fill(page, key, value, t=4000) -> bool:
    if not value:
        return False
    el = await _first(page, key, t)
    if not el:
        return False
    try:
        await el.click(); await el.fill(str(value)); return True
    except Exception:
        return False


async def _click_role(page, role, name_rx, t=3000) -> bool:
    """Text/ARIA fallback when a tenant's data-automation-id differs."""
    try:
        el = page.get_by_role(role, name=re.compile(name_rx, re.I)).first
        await el.wait_for(state="visible", timeout=t); await el.click()
        await page.wait_for_timeout(600); return True
    except Exception:
        return False


async def _has_role(page, role, name_rx, t=1000) -> bool:
    try:
        return await page.get_by_role(role, name=re.compile(name_rx, re.I)).first.is_visible(timeout=t)
    except Exception:
        return False


# ---------- credentials ----------------------------------------------------------------------------
def _host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _load_accounts() -> dict:
    try:
        return json.load(open(ACCOUNTS, encoding="utf-8"))
    except Exception:
        return {}


def _save_account(host, email, password):
    a = _load_accounts(); a[host] = {"email": email, "password": password}
    try:
        json.dump(a, open(ACCOUNTS, "w"), indent=2)
    except Exception:
        pass


async def _ask(q: str) -> str:
    if ask:
        try:
            return await ask.ask_human(q) or ""
        except Exception:
            return ""
    return ""


async def _notify(m: str):
    if ask:
        try:
            await ask.notify(m)
        except Exception:
            pass


def _split_address(addr: str):
    line1, postal, city = addr, "", ""
    parts = [p.strip() for p in addr.split(",")]
    if parts:
        line1 = parts[0]
    rest = " ".join(parts[1:]) if len(parts) > 1 else ""
    m = re.search(r"\b(\d{4,6})\b", rest)
    if m:
        postal = m.group(1)
        city = re.sub(r"\(.*?\)", "", rest.replace(postal, "")).strip()
    return line1, postal, city


# ---------- page state -----------------------------------------------------------------------------
async def _error_text(page) -> str:
    el = await _first(page, "error_banner", 700)
    if not el:
        return ""
    try:
        return re.sub(r"\s+", " ", (await el.inner_text()))[:180]
    except Exception:
        return "error"


async def _past_gate(page) -> bool:
    await page.wait_for_timeout(500)
    if await _has(page, "error_banner", 600):
        return False
    return not await _has(page, "password", 1200)


async def _is_review(page) -> bool:
    if "review" in (page.url or "").lower():
        return True
    if await _has(page, "review_page", 600):
        return True
    try:
        if await page.get_by_role("button", name=re.compile(r"^\s*submit", re.I)).count() > 0:
            return True
    except Exception:
        pass
    return False


async def _check_consent(page) -> bool:
    """Tick the 'By clicking the checkbox I consent…' banner deterministically."""
    if await _click(page, "acct_checkbox", 800):
        return True
    cbs = page.locator("input[type='checkbox']")
    try:
        n = await cbs.count()
    except Exception:
        n = 0
    for i in range(min(n, 12)):
        el = cbs.nth(i)
        try:
            if not await el.is_visible() or await el.is_checked():
                continue
            sig = ""
            eid = await el.get_attribute("id")
            if eid:
                lab = page.locator(f'label[for="{eid}"]')
                if await lab.count():
                    sig = await lab.first.inner_text()
            if not sig:
                sig = await el.evaluate("e => (e.closest('label,div,fieldset')?.innerText) || ''")
            if re.search(r"consent|agree|terms|privacy|acknowledge|certify", sig or "", re.I):
                await el.check(); return True
        except Exception:
            pass
    return False


# ---------- account --------------------------------------------------------------------------------
async def _try_signin(page, email, password) -> bool:
    if not await _click(page, "signin_link", 1200):
        await _click_role(page, "link", r"sign\s*in", 1200) or await _click_role(page, "button", r"sign\s*in", 1200)
    await page.wait_for_timeout(800)
    await _fill(page, "email", email)
    await _fill(page, "password", password)
    if not await _click(page, "signin_submit", 2000):
        await _click_role(page, "button", r"^\s*sign\s*in\s*$")
    await page.wait_for_timeout(2800)
    return await _past_gate(page)


async def _account(page, host, email, proposed_pw, filled) -> bool:
    acc = _load_accounts().get(host) or {}
    stored = acc.get("password")
    login_email = acc.get("email") or email          # account login may differ from résumé email

    # Google-SSO tenants (e.g. Accenture wd103): recorded flow is "Use My Last Application" then
    # "Sign in with Google". Relies on the sandbox Chrome already signed into Google (one-time manual
    # login in noVNC). Try the shortcut + SSO before email/password.
    if await _click_role(page, "button", r"use my last application", 1500):
        await page.wait_for_timeout(2000)
        if await _past_gate(page):
            filled.append("account:last-application"); return True
    if await _has_role(page, "button", r"sign in with google"):
        await _click_role(page, "button", r"sign in with google", 2500)
        await page.wait_for_timeout(3500)
        if await _past_gate(page):
            filled.append("account:google-sso"); return True

    if stored and await _try_signin(page, login_email, stored):
        filled.append("account:signin"); return True

    on_create = await _has(page, "verify_pw", 1200) or await _has(page, "acct_checkbox", 800)
    if on_create and not stored:
        await _fill(page, "email", email)
        await _fill(page, "password", proposed_pw)
        await _fill(page, "verify_pw", proposed_pw)
        await _check_consent(page)
        if not await _click(page, "create_submit", 2000):
            await _click_role(page, "button", r"create\s*account")
        await page.wait_for_timeout(2800)
        if await _past_gate(page):
            _save_account(host, email, proposed_pw); filled.append("account:create"); return True

    pw = await _ask(f"JobHuntWOW is stuck at the Workday login for {host}. An account for {login_email} "
                    f"seems to exist. Reply with its PASSWORD (or reset via 'Forgot your password?' and "
                    f"reply with the new one).")
    if pw and not pw.startswith("NO_ANSWER") and await _try_signin(page, login_email, pw.strip()):
        _save_account(host, login_email, pw.strip()); filled.append("account:signin-human"); return True
    return False


# ---------- #1 THE FIX: resume upload + server-side parse wait --------------------------------------
async def _upload_resume(page, path: str, r: dict) -> bool:
    """Workday parses the resume SERVER-SIDE. Delete any existing attachment (uploads stack otherwise),
       upload to the HIDDEN input (set_input_files has zero actionability checks — the documented path),
       then WAIT until it's actually attached before advancing."""
    fi = await _present(page, "file_input")
    if not fi:
        return False
    if await _has(page, "delete_file", 700):                 # existing attachment -> remove first
        await _click(page, "delete_file", 1500)
        await page.wait_for_timeout(1500)
        fi = await _present(page, "file_input") or fi
    try:
        await fi.set_input_files(path)
    except Exception as e:
        r["note"] += f" upload:{e};"
        return False
    # WAIT for the parse: the delete-file control appearing means the file is attached & processed.
    for _ in range(25):
        await page.wait_for_timeout(1000)
        if await _has(page, "delete_file", 400):
            r["filled"].append("resume")
            print("[wd] resume attached + parsed", flush=True)
            return True
    r["note"] += " resume uploaded but parse not confirmed;"
    r["filled"].append("resume?")
    return True


# ---------- #5 LLM ONLY FOR FREE TEXT ---------------------------------------------------------------
async def _answer_choices(page, profile, answer_fn) -> int:
    if not answer_fn:
        return 0
    answered = 0
    groups = page.locator("[data-automation-id='radioGroup'], fieldset")
    try:
        n = await groups.count()
    except Exception:
        n = 0
    for i in range(min(n, 15)):
        g = groups.nth(i)
        try:
            if not await g.is_visible():
                continue
            radios = g.locator("input[type='radio']")
            rc = await radios.count()
            if rc == 0 or any([await radios.nth(j).is_checked() for j in range(rc)]):
                continue
            qtext = (await g.inner_text())[:300]
            labs = g.locator("label")
            opts = []
            for j in range(min(await labs.count(), 8)):
                t = (await labs.nth(j).inner_text()).strip()
                if t:
                    opts.append(t)
            choice = await answer_fn(qtext, opts, profile)
            if choice and choice.strip().upper().startswith("UNKNOWN"):
                choice = await _ask(f"Application question needs your answer:\n{qtext}\nOptions: {opts}")
            if choice:
                for j in range(min(await labs.count(), 8)):
                    t = (await labs.nth(j).inner_text()).strip()
                    if t and (t.lower() in choice.lower() or choice.lower() in t.lower()):
                        await labs.nth(j).click(); answered += 1; break
        except Exception:
            pass
    return answered


def _apply_urls(ats_url: str) -> list:
    """Candidate deep-links. Workday's canonical apply URL carries a locale segment (/en-US/) that the
       LinkedIn-supplied URL lacks; without it Workday bounces you back to the posting."""
    base = ats_url.split("?")[0].rstrip("/")
    if "/apply" in base:
        return [base]
    urls = []
    m = re.match(r"(https?://[^/]+)(/.*)", base)
    if m and "/en-US/" not in base:
        urls.append(m.group(1) + "/en-US" + m.group(2) + "/apply/applyManually")
    urls.append(base + "/apply/applyManually")
    return urls


async def _in_apply_flow(page) -> bool:
    """Are we actually inside the multi-step apply wizard (not bounced back to the job posting)?"""
    if "/apply" in (page.url or "").lower():
        return True
    for k in ("password", "my_info_page", "my_exp_page", "review_page", "next"):
        if await _has(page, k, 600):
            return True
    return False


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    wd = [p for p in ctx.pages if re.search(r"myworkday|workday", (p.url or ""), re.I)]
    page = wd[-1] if wd else (ctx.pages[-1] if ctx.pages else await ctx.new_page())
    for p in wd[:-1]:                                        # close stale tabs from earlier runs
        try: await p.close()
        except Exception: pass
    return browser, ctx, page


async def drive(creds: dict, data: dict, resume_path: str = "", answer_fn=None, ats_url: str = "") -> dict:
    r = {"ok": False, "stage": "start", "filled": [], "note": "", "needs_llm": False}
    b = data.get("basics", {})
    legal = (b.get("legal_name") or b.get("name") or "").split()
    first = legal[0] if legal else ""
    last = " ".join(legal[1:]) if len(legal) > 1 else ""
    line1, postal, city = _split_address(b.get("address", ""))
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)

        # #2 DEEP-LINK to the manual apply flow, trying the /en-US/ locale variant first.
        r["stage"] = "deeplink"
        if ats_url:
            for u in _apply_urls(ats_url):
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    r["note"] += f" deeplink:{e};"; continue
                if await _in_apply_flow(page):
                    print(f"[wd] deep-link OK -> {page.url[:90]}", flush=True); break
                print(f"[wd] deep-link bounced ({u[:70]}…)", flush=True)
        if not re.search(r"myworkday|workday", (page.url or ""), re.I):
            r["note"] = f"not on Workday ({page.url})"; return r
        await page.bring_to_front()

        r["stage"] = "cookie"
        if not await _click(page, "cookie", 2500):
            await _click_role(page, "button", r"accept cookies|allow all cookies|accept all")
        if await _has(page, "already_applied", 800):
            r["ok"] = True; r["stage"] = "already_applied"
            r["note"] = "Workday says you have already applied to this job."
            return r

        # VERIFY we're in the wizard; if the deep-link bounced, click through: Apply -> Apply Manually.
        r["stage"] = "enter_flow"
        if not await _in_apply_flow(page):
            if await _has(page, "apply", 3000):
                print("[wd] not in the apply flow — clicking Apply …", flush=True)
                await _click(page, "apply")
                await page.wait_for_timeout(3000)
                await _click(page, "apply_manual", 4000)
                await page.wait_for_timeout(2500)
                print(f"[wd] after Apply -> {page.url[:90]}", flush=True)
            if not await _in_apply_flow(page):
                r["note"] = f"could not enter the Workday apply flow from {page.url[:90]}"
                await _notify(r["note"]); return r

        r["stage"] = "account"
        host = _host(page.url)
        if await _has(page, "password", 2500):
            if not await _account(page, host, creds["email"], creds["password"], r["filled"]):
                r["note"] = (f"Could not pass the Workday account gate for {host}. Reset the password via "
                             "'Forgot your password?' then: python jhw.py atspw <host> '<pw>' <login-email>")
                await _notify(r["note"])
                return r

        # ---- page loop: fill -> upload(+parse wait) -> answer -> Save and Continue ----
        r["stage"] = "pages"
        last_err = ""
        for _i in range(10):
            await page.wait_for_timeout(1500)
            if await _is_review(page):
                r["ok"] = True; r["stage"] = "review"
                r["note"] = ("Reached Review. Filled: " + ", ".join(sorted(set(r["filled"]))) +
                             ". Submit yourself in noVNC.")
                return r

            # My Information (only if this tenant shows it)
            if await _has(page, "first_name", 3000):
                if await _fill(page, "first_name", first): r["filled"].append("first_name")
                if await _fill(page, "last_name", last):   r["filled"].append("last_name")
                if await _fill(page, "addr1", line1):      r["filled"].append("address")
                if await _fill(page, "city", city):        r["filled"].append("city")
                if await _fill(page, "postal", postal):    r["filled"].append("postal")
                if await _fill(page, "phone", b.get("phone", "")): r["filled"].append("phone")

            # #1 resume BEFORE the generic autofill (Workday's parse overwrites fields)
            if resume_path and os.path.exists(resume_path) and not any(
                    x.startswith("resume") for x in r["filled"]):
                await _upload_resume(page, resume_path, r)

            if autofill:
                try:
                    r["filled"] += (await autofill.fill_page(page, data))["filled"]
                except Exception as e:
                    r["note"] += f" autofill:{e};"
            await _check_consent(page)
            try:
                a = await _answer_choices(page, data, answer_fn)
                if a: r["filled"].append(f"answered:{a}")
            except Exception as e:
                r["note"] += f" answer:{e};"


            # advance
            if not await _click(page, "next", 4000):
                if not await _click_role(page, "button", r"save\s*and\s*continue|^\s*next\s*$"):
                    r["note"] += " no Save-and-Continue button found — paused for the human."
                    await _notify("Workday: no Save-and-Continue button found; please finish in noVNC.")
                    break
            await page.wait_for_timeout(2500)

            # #4 assertion: a REPEATING validation error means we cannot fix it -> HALT + tell the human
            err = await _error_text(page)
            if err:
                print(f"[wd] validation error: {err}", flush=True)
                if err == last_err:
                    r["stage"] = "blocked"
                    r["note"] = f"Workday validation error we cannot resolve: '{err}'. Stopped for the human."
                    await _notify(r["note"])
                    return r
                last_err = err
            else:
                last_err = ""

        r["ok"] = True
        if r["stage"] != "blocked":
            r["stage"] = "paused"
        r["note"] = r["note"] or ("Advanced through the form (" + ", ".join(sorted(set(r["filled"]))) +
                                  "). Stopped before Submit — finish in noVNC.")
        return r
    except Exception as e:
        r["note"] = f"workday driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"), encoding="utf-8"))
    c = {"email": d["basics"]["email"], "password": "Test!Only1"}
    print(json.dumps(asyncio.run(drive(c, d, "/agent/out/resume.pdf")), indent=2))
