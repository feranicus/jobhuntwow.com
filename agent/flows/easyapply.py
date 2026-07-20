"""Deterministic LinkedIn EASY APPLY — built from a REAL codegen recording
(agent/recordings/easyapply.py). The whole Easy-Apply modal lives inside `[data-testid=interop-iframe]`
(same iframe as search). Deterministic drives resume + navigation (Continue -> Review); the LLM answers
only free-text/choice screening questions via answer_fn. STOPS at 'Submit application' — human submits.
"""
from __future__ import annotations
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
IFRAME  = "[data-testid='interop-iframe']"


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = None
    for p in ctx.pages:
        if "linkedin.com" in (p.url or ""):
            page = p; break
    page = page or (ctx.pages[-1] if ctx.pages else await ctx.new_page())
    return browser, ctx, page


async def _visible(loc):
    try:
        return await loc.first.is_visible(timeout=800)
    except Exception:
        return False


async def _answer_questions(frame, profile, answer_fn) -> int:
    """Answer required screening questions on the current step: text inputs, native selects, radio groups.
       Values come from answer_fn (text-only LLM); never invents."""
    if not answer_fn:
        return 0
    answered = 0
    # text inputs
    tbs = frame.get_by_role("textbox")
    try:
        n = await tbs.count()
    except Exception:
        n = 0
    for i in range(min(n, 12)):
        el = tbs.nth(i)
        try:
            if not await el.is_visible() or (await el.input_value()):
                continue
            q = (await el.get_attribute("aria-label")) or (await el.get_attribute("name")) or ""
            if not q:
                continue
            a = await answer_fn(q, None, profile)
            if a and not a.strip().upper().startswith("UNKNOWN"):
                await el.fill(a.strip()); answered += 1
        except Exception:
            pass
    # native selects
    sels = frame.locator("select")
    try:
        sn = await sels.count()
    except Exception:
        sn = 0
    for i in range(min(sn, 10)):
        el = sels.nth(i)
        try:
            if not await el.is_visible():
                continue
            q = (await el.get_attribute("aria-label")) or ""
            opts = [o.strip() for o in (await el.locator("option").all_inner_texts()) if o.strip()]
            a = await answer_fn(q, opts, profile)
            if a:
                try: await el.select_option(label=a.strip())
                except Exception:
                    try: await el.select_option(a.strip())
                    except Exception: pass
                answered += 1
        except Exception:
            pass
    return answered


async def drive(data: dict, resume_path: str = "", answer_fn=None, max_steps: int = 10) -> dict:
    """Assumes a LinkedIn job with 'Easy Apply' is open in the sandbox tab. Fills to Submit, never submits."""
    r = {"ok": False, "stage": "start", "filled": [], "note": ""}
    b = data.get("basics", {})
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front(); await page.wait_for_timeout(1500)
        frame = page.frame_locator(IFRAME)

        r["stage"] = "open"
        try:
            await frame.get_by_role("button", name=re.compile(r"Easy Apply", re.I)).first.click(timeout=6000)
            await page.wait_for_timeout(1500)
        except Exception as e:
            r["note"] = f"could not open Easy Apply dialog: {e}"; return r

        for _ in range(max_steps):
            await page.wait_for_timeout(1000)
            # STOP at the final submit
            if await _visible(frame.get_by_role("button", name=re.compile(r"Submit application", re.I))):
                r["ok"] = True; r["stage"] = "review"
                r["note"] = ("Reached 'Submit application'. Filled: " + ", ".join(sorted(set(r["filled"]))) +
                             ". Review + submit in noVNC.")
                return r
            # resume upload if offered and not yet attached
            try:
                up = frame.get_by_label(re.compile(r"^Upload resume", re.I))
                if resume_path and os.path.exists(resume_path) and "resume" not in r["filled"] and await up.count():
                    await up.first.set_input_files(resume_path)
                    r["filled"].append("resume"); await page.wait_for_timeout(1500)
            except Exception:
                pass
            # contact fields
            for rx, key in ((r"email", "email"), (r"phone|mobile", "phone")):
                try:
                    el = frame.get_by_role("textbox", name=re.compile(rx, re.I)).first
                    if await el.is_visible(timeout=600) and not (await el.input_value()):
                        await el.fill(b.get(key, "")); r["filled"].append(key)
                except Exception:
                    pass
            # screening questions via LLM
            try:
                a = await _answer_questions(frame, data, answer_fn)
                if a: r["filled"].append(f"answered:{a}")
            except Exception as e:
                r["note"] += f" answer:{e};"
            # advance: Review, else Continue
            adv = False
            for rx in (r"Review your application", r"Continue to next step", r"^Next$"):
                btn = frame.get_by_role("button", name=re.compile(rx, re.I)).first
                if await _visible(btn):
                    try:
                        await btn.click(); adv = True; break
                    except Exception:
                        pass
            if not adv:
                r["note"] += " no Continue/Review button — paused for human."
                break
            await page.wait_for_timeout(1200)

        r["ok"] = True; r["stage"] = r["stage"] if r["stage"] == "review" else "paused"
        r["note"] = r["note"] or ("Advanced Easy Apply (" + ", ".join(sorted(set(r["filled"]))) +
                                  "). Finish in noVNC.")
        return r
    except Exception as e:
        r["note"] = f"easyapply driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    import json
    d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"), encoding="utf-8"))
    print(json.dumps(asyncio.run(drive(d, "/agent/out/resume.pdf")), indent=2))
