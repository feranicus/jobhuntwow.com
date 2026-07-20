"""Deterministic LinkedIn navigation + login via Playwright (connect to the running Chrome over CDP).

No LLM. Handles: login (auto-attempt LinkedIn-native or Google, else ping+wait for a one-time manual
login that then persists), open job, click Apply, detect Easy-Apply vs external ATS. It never fills a
signup form, so it cannot 'create a LinkedIn account'.
"""
from __future__ import annotations
import re
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /agent on path for `import ask`
from playwright.async_api import async_playwright
try:
    import ask
except Exception:
    ask = None

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
JOB_URL = "https://www.linkedin.com/jobs/view/{jid}/"
APPLY_SEL = "button.jobs-apply-button, .jobs-s-apply button, button.jobs-apply-button--top-card"
LOGIN_TIMEOUT = int(os.getenv("JHW_LOGIN_TIMEOUT", "480"))

LI_EMAIL = os.getenv("LINKEDIN_EMAIL", ""); LI_PW = os.getenv("LINKEDIN_PASSWORD", "")
G_EMAIL  = os.getenv("GOOGLE_EMAIL", "");   G_PW  = os.getenv("GOOGLE_PASSWORD", "")


async def _get_page(ctx):
    for p in ctx.pages:
        if "linkedin.com" in (p.url or ""):
            return p
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    # Always use a FRESH tab: prior browser-use runs leave detached/closed tabs ("Frame has been detached").
    page = await ctx.new_page()
    return browser, ctx, page


async def is_logged_in(page) -> bool:
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
    u = (page.url or "").lower()
    return not any(x in u for x in ("/login", "/authwall", "/uas/login", "signup", "/checkpoint"))


async def _ask(q):
    if ask:
        try:
            return await ask.ask_human(q)
        except Exception:
            return ""
    return ""


async def _google_signin(page) -> bool:
    try:
        await page.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=45000)
        if "myaccount.google.com" in page.url:
            return True
        em = page.locator('input[type="email"]').first
        if await em.count():
            await em.fill(G_EMAIL)
            await page.locator('#identifierNext button, button:has-text("Next")').first.click()
            await page.wait_for_selector('input[type="password"]', timeout=20000)
        pwf = page.locator('input[type="password"]').first
        await pwf.fill(G_PW)
        await page.locator('#passwordNext button, button:has-text("Next")').first.click()
        await page.wait_for_timeout(4500)
        body = (await page.content()).lower()
        if any(x in body for x in ("2-step", "verification", "verify it", "enter the code", "2fa")):
            code = await _ask("Google wants a 2-step verification code — what is it?")
            if code:
                await page.locator('input[type="tel"], input[type="text"]').first.fill(code)
                await page.keyboard.press("Enter"); await page.wait_for_timeout(4000)
        return "myaccount.google.com" in page.url or "google.com" in page.url
    except Exception as e:
        print(f"[login] google sign-in issue: {e}", flush=True)
        return False


async def _linkedin_via_google(page):
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=45000)
    for sel in ['button:has-text("Continue with Google")', 'button:has-text("Sign in with Google")',
                'a:has-text("Continue with Google")']:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.click(timeout=6000); await page.wait_for_timeout(5000); return
        except Exception:
            pass


async def _attempt_login(page) -> bool:
    if LI_EMAIL and LI_PW:
        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=45000)
            await page.fill('#username', LI_EMAIL); await page.fill('#password', LI_PW)
            await page.locator('button[type="submit"]').first.click(); await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"[login] linkedin-native issue: {e}", flush=True)
    elif G_EMAIL and G_PW:
        if await _google_signin(page):
            await _linkedin_via_google(page)
    return await is_logged_in(page)


async def ensure_login(timeout: int = LOGIN_TIMEOUT) -> bool:
    """True when LinkedIn is logged in. Tries auto-login; else pings + waits for a one-time manual login."""
    pw = await async_playwright().start()
    browser = None
    try:
        browser, ctx, page = await _connect(pw)
        if await is_logged_in(page):
            print("[login] already logged in.", flush=True); return True
        print("[login] logged out — attempting automatic sign-in ...", flush=True)
        if await _attempt_login(page):
            print("[login] auto sign-in OK.", flush=True); return True
        msg = "Please log into LinkedIn once at http://localhost:9090/vnc.html — it persists after that."
        print("[login] auto-login didn't work. " + msg, flush=True)
        await _ask("JobHuntWOW couldn't auto-login. " + msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(12)
            if await is_logged_in(page):
                print("[login] detected login — continuing.", flush=True); return True
        print("[login] timed out waiting for manual login.", flush=True); return False
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


_ATS_RX = r"myworkday|workday|greenhouse|icims|successfactors|taleo|personio|hibob|smartrecruiters|lever|ashby|jobvite|brassring"


async def _click_apply(pg) -> bool:
    """Click an Apply/Apply-Now button on a careers landing page to reach the real ATS."""
    for nm in (r"apply now", r"apply for this job", r"^\s*apply\s*$", r"apply externally",
               r"apply on (the )?company", r"apply here"):
        for role in ("button", "link"):
            try:
                loc = pg.get_by_role(role, name=re.compile(nm, re.I))
                if await loc.count():
                    await loc.first.click(timeout=5000); return True
            except Exception:
                pass
    return False


async def prepare_application(jid: str) -> dict:
    """Assumes logged in. Open job, click Apply, detect Easy-Apply vs external ATS. Leaves Chrome on
       the ATS tab (front) when external. Returns {status, apply_type, ats_url, note}."""
    result = {"status": "error", "apply_type": "unknown", "ats_url": "", "note": ""}
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        if not await is_logged_in(page):
            result.update(status="not_logged_in", note="LinkedIn logged out; run login first."); return result
        result["status"] = "logged_in"
        await page.goto(JOB_URL.format(jid=jid), wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)  # let the top-card lazy-load
        import re as _re
        btn = None
        try:
            await page.wait_for_selector(APPLY_SEL, timeout=8000)
            btn = page.locator(APPLY_SEL).first
        except Exception:
            cand = page.get_by_role("button", name=_re.compile("apply", _re.I))
            if await cand.count() == 0:
                cand = page.get_by_role("link", name=_re.compile("apply", _re.I))
            if await cand.count() > 0:
                btn = cand.first
        if btn is None:
            result["note"] = "Apply button not found (falling back to LLM apply)."; return result
        label = (((await btn.inner_text()) or "") + " " + ((await btn.get_attribute("aria-label")) or "")).lower()
        if "easy apply" in label:
            result["apply_type"] = "easy_apply"; await btn.click(); await page.wait_for_timeout(1500)
            result["note"] = "Easy Apply dialog opened."
        else:
            result["apply_type"] = "external"
            try:
                async with ctx.expect_page(timeout=20000) as newp:
                    await btn.click()
                ats = await newp.value
                await ats.wait_for_load_state("domcontentloaded", timeout=45000)
                # (a) auto-redirect: careers.<co>.com -> <co>.wdN.myworkdayjobs.com may take a few seconds
                for _ in range(8):
                    await ats.wait_for_timeout(1000)
                    if re.search(_ATS_RX, ats.url or "", re.I):
                        break
                # (b) careers LANDING page (no auto-redirect, e.g. careers.gevernova.com): click its
                #     Apply to reach the real ATS — it may open a popup OR navigate the same tab.
                if not re.search(_ATS_RX, ats.url or "", re.I):
                    popup = None
                    try:
                        async with ctx.expect_page(timeout=12000) as newp2:
                            await _click_apply(ats)
                        popup = await newp2.value
                    except Exception:
                        popup = None
                    if popup:
                        ats = popup
                        try: await ats.wait_for_load_state("domcontentloaded", timeout=45000)
                        except Exception: pass
                    for _ in range(10):
                        await ats.wait_for_timeout(1000)
                        if re.search(_ATS_RX, ats.url or "", re.I):
                            break
                # (c) safety net: the Apply may have opened the real ATS in a tab we didn't switch
                #     to (that is exactly how GE Vernova's Workday tab was left unrouted). If ANY open
                #     tab is already a known ATS, adopt it before deciding the route.
                if not re.search(_ATS_RX, ats.url or "", re.I):
                    for pg in ctx.pages:
                        if re.search(_ATS_RX, pg.url or "", re.I):
                            ats = pg
                            try: await ats.wait_for_load_state("domcontentloaded", timeout=15000)
                            except Exception: pass
                            break
                await ats.bring_to_front()
                result["ats_url"] = ats.url; result["note"] = f"External apply -> {ats.url}"
            except Exception as e:
                result["note"] = f"external Apply, no new tab captured: {e}"
        return result
    except Exception as e:
        result["note"] = f"playwright error: {e}"; return result
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    import json
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "login":
        print("logged_in" if asyncio.run(ensure_login()) else "not_logged_in")
    else:
        print(json.dumps(asyncio.run(prepare_application(sys.argv[1])), indent=2))
