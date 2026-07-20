"""Alibaba page-agent integration — https://github.com/alibaba/page-agent

The in-page, text-DOM GUI agent: NO screenshots, NO vision, NO agent-side browser process. We inject
its IIFE bundle into the live ATS tab over CDP and drive it with natural language, using OUR DO proxy
as the LLM (bring-your-own-LLM). This is the FAST fallback for NON-Workday ATS (Taleo, SuccessFactors,
Personio, HiBob, Greenhouse, Lever, iCIMS) where we don't yet have a deterministic adapter.

page-agent API (from its README):
    new window.PageAgent({ model, baseURL, apiKey, language })  ->  await agent.execute("...")
It calls the LLM from inside the page, so the backend proxy must send permissive CORS (see proxy.py).
"""
from __future__ import annotations
import asyncio, os
import httpx
from playwright.async_api import async_playwright

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
PROXY   = os.getenv("JHW_PROXY_BASE", "http://host.docker.internal:8000/v1")
TOKEN   = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL   = os.getenv("JHW_PAGEAGENT_MODEL", "jhw-driver")   # proxy alias -> reliable tool-caller
CDN     = os.getenv("JHW_PAGEAGENT_CDN",
                    "https://cdn.jsdelivr.net/npm/page-agent@1.10.0/dist/iife/page-agent.js")


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    for p in ctx.pages:                     # prefer the ATS tab (not LinkedIn / blank)
        u = (p.url or "").lower()
        if u and "linkedin.com" not in u and "about:blank" not in u and u.startswith("http"):
            page = p
    return browser, ctx, page


async def run(task: str) -> dict:
    """Inject page-agent into the current ATS tab and execute the apply task IN-PAGE (text DOM)."""
    r = {"ok": False, "note": "", "result": "", "url": ""}
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front()
        r["url"] = page.url
        # Enterprise ATS pages (Workday) set a strict CSP that blocks <script src=cdn…>. So we DON'T use
        # add_script_tag(url=…). Instead we get the bundle TEXT (local vendor file, else Python-side fetch —
        # both avoid the browser network) and inject it via page.evaluate, which runs through CDP in the
        # page's main world and BYPASSES the page CSP script-src.
        bundle = ""
        vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "page-agent.js")
        if os.path.exists(vendor):
            try: bundle = open(vendor, encoding="utf-8").read()
            except Exception: bundle = ""
        if not bundle:
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                    rr = await c.get(CDN); rr.raise_for_status(); bundle = rr.text
            except Exception as e:
                r["note"] = (f"could not obtain the page-agent bundle ({e}). Vendor it to agent/flows/vendor/"
                             "page-agent.js, or set JHW_PAGEAGENT_CDN.")
                return r
        try:
            await page.evaluate(bundle)                          # inject via CDP (bypasses page CSP)
            await page.wait_for_function("() => !!window.PageAgent", timeout=8000)
        except Exception as e:
            r["note"] = f"page-agent injected but window.PageAgent not defined ({e})."
            return r
        out = await page.evaluate(
            """async ({task, model, baseURL, apiKey}) => {
                 try {
                   const agent = new window.PageAgent({ model, baseURL, apiKey, language: 'en-US' });
                   const res = await agent.execute(task);
                   return { ok: true, res: String(res ?? '') };
                 } catch (e) { return { ok: false, res: String(e && e.message || e) }; }
               }""",
            {"task": task, "model": MODEL, "baseURL": PROXY, "apiKey": TOKEN},
        )
        r["ok"] = bool(out.get("ok"))
        r["result"] = out.get("res", "")
        r["note"] = "page-agent executed in-page." if r["ok"] else f"page-agent error: {r['result']}"
        return r
    except Exception as e:
        r["note"] = f"page-agent driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    print(asyncio.run(run("Fill this job application form with the candidate's data and stop at Review; do not submit.")))
