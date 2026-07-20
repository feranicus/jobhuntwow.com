"""Deterministic iCIMS apply — built from a REAL codegen recording (agent/recordings/icims.py, Atlassian).

REALITY CHECK (from the recording): iCIMS wraps the form in iframe[name="icims_content_iframe"], and
this tenant gates apply behind **hCaptcha** + **email OTP** + Google SSO. Those are deliberate anti-bot
walls a compliant bot CANNOT pass. So this adapter fills the pre-gate fields (email, consent) and then
HANDS OFF to the human at the CAPTCHA / OTP: it pauses and pings Telegram, and the candidate solves the
CAPTCHA in noVNC or replies with the emailed code. It never tries to solve a CAPTCHA. Never submits.
"""
from __future__ import annotations
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.async_api import async_playwright
try:
    import ask
except Exception:
    ask = None

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
IFRAME  = 'iframe[name="icims_content_iframe"]'


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    for p in ctx.pages:
        if "icims.com" in (p.url or "").lower():
            page = p; break
    return browser, ctx, page


async def _notify(m):
    if ask:
        try: await ask.notify(m)
        except Exception: pass


async def _ask(q):
    if ask:
        try: return await ask.ask_human(q) or ""
        except Exception: return ""
    return ""


async def _captcha_present(frame) -> bool:
    try:
        return await frame.locator('iframe[title="hCaptcha challenge"], iframe[src*="hcaptcha"], '
                                   'iframe[src*="recaptcha"]').count() > 0
    except Exception:
        return False


async def drive(data: dict, resume_path: str = "", ats_url: str = "", answer_fn=None) -> dict:
    r = {"ok": False, "stage": "start", "filled": [], "note": "", "needs_human": False}
    b = data.get("basics", {})
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        if ats_url and "icims.com" not in (page.url or "").lower():
            await page.goto(ats_url, wait_until="domcontentloaded", timeout=45000)
        await page.bring_to_front(); await page.wait_for_timeout(2000)
        frame = page.frame_locator(IFRAME)

        r["stage"] = "apply"
        try:
            await frame.get_by_role("link", name=re.compile(r"apply for this job online", re.I)).first.click(timeout=5000)
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        r["stage"] = "contact"
        try:
            em = frame.get_by_role("textbox", name=re.compile(r"^email", re.I)).first
            if await em.is_visible(timeout=3000):
                await em.fill(b.get("email", "")); r["filled"].append("email")
        except Exception:
            pass
        # consent checkbox "I accept"
        try:
            cb = frame.get_by_role("checkbox", name=re.compile(r"i accept", re.I)).first
            if await cb.is_visible(timeout=1500) and not await cb.is_checked():
                await cb.check(); r["filled"].append("consent")
        except Exception:
            pass
        try:
            await frame.get_by_role("button", name=re.compile(r"^\s*next\s*$", re.I)).first.click(timeout=3000)
            await page.wait_for_timeout(2500)
        except Exception:
            pass

        # ---- anti-bot gates: hand off to the human, never solve ----
        if await _captcha_present(frame):
            r["stage"] = "captcha"; r["needs_human"] = True
            r["note"] = ("iCIMS shows a CAPTCHA — a bot cannot (and must not) solve it. Solve it in the "
                         "noVNC window (http://localhost:9090), then continue the application there.")
            await _notify(r["note"]); r["ok"] = False; return r

        # email OTP
        try:
            otp = frame.get_by_role("textbox", name=re.compile(r"verification code", re.I)).first
            if await otp.is_visible(timeout=2000):
                r["stage"] = "otp"
                code = await _ask("iCIMS emailed you a verification code. Reply with the 6-digit code.")
                m = re.search(r"\b(\d{4,8})\b", code or "")
                if m:
                    await otp.fill(m.group(1))
                    await frame.get_by_role("button", name=re.compile(r"submit verification", re.I)).first.click()
                    r["filled"].append("otp"); await page.wait_for_timeout(2500)
                else:
                    r["needs_human"] = True
                    r["note"] = "iCIMS needs the emailed verification code — none received. Finish in noVNC."
                    await _notify(r["note"]); return r
        except Exception:
            pass

        r["ok"] = True; r["stage"] = "handoff"
        r["note"] = ("iCIMS pre-fill done (" + ", ".join(sorted(set(r["filled"]))) +
                     "). iCIMS gates (CAPTCHA / OTP / SSO) need you — finish + submit in noVNC.")
        return r
    except Exception as e:
        r["note"] = f"icims driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    import json
    d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"), encoding="utf-8"))
    print(json.dumps(asyncio.run(drive(d, "/agent/out/resume.pdf")), indent=2))
