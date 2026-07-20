"""Deterministic Greenhouse apply — built from a REAL Playwright codegen recording
(agent/recordings/greenhouse.py, GitLab job). Greenhouse needs NO account and is a single page:
fill contact fields -> upload resume -> answer a few questions -> STOP before Submit (human submits).

Selectors are the ACTUAL accessible names the candidate clicked (get_by_role/get_by_label) — far more
robust than CSS/data-* guesses. LLM is used ONLY for free-text questions via answer_fn; never to drive.
"""
from __future__ import annotations
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    for p in ctx.pages:
        if "greenhouse.io" in (p.url or "").lower():
            page = p; break
    return browser, ctx, page


async def _fill_role(page, role, name_rx, value):
    if not value:
        return False
    try:
        el = page.get_by_role(role, name=re.compile(name_rx, re.I)).first
        await el.wait_for(state="visible", timeout=4000)
        await el.click(); await el.fill(str(value)); return True
    except Exception:
        return False


async def drive(data: dict, resume_path: str = "", ats_url: str = "", answer_fn=None) -> dict:
    """Fill a Greenhouse application to the Submit gate. Never submits."""
    r = {"ok": False, "stage": "start", "filled": [], "note": ""}
    b = data.get("basics", {})
    legal = (b.get("legal_name") or b.get("name") or "").split()
    first = legal[0] if legal else ""
    last = " ".join(legal[1:]) if len(legal) > 1 else ""
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        if ats_url and "greenhouse.io" not in (page.url or "").lower():
            await page.goto(ats_url, wait_until="domcontentloaded", timeout=45000)
        await page.bring_to_front(); await page.wait_for_timeout(1500)

        r["stage"] = "apply"
        try:
            await page.get_by_role("button", name=re.compile(r"^\s*apply", re.I)).first.click(timeout=4000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass  # some Greenhouse pages show the form directly

        r["stage"] = "contact"
        if await _fill_role(page, "textbox", r"first name", first):        r["filled"].append("first_name")
        if await _fill_role(page, "textbox", r"last name", last):          r["filled"].append("last_name")
        if await _fill_role(page, "textbox", r"^email", b.get("email", "")): r["filled"].append("email")
        # phone: fill the international number directly (skip the country flyout — value carries +49)
        if await _fill_role(page, "textbox", r"^phone", b.get("phone", "")): r["filled"].append("phone")
        if await _fill_role(page, "textbox", r"linkedin", b.get("linkedin", "")): r["filled"].append("linkedin")

        r["stage"] = "resume"
        if resume_path and os.path.exists(resume_path):
            try:
                grp = page.get_by_role("group", name=re.compile(r"resume/?cv", re.I))
                fi = grp.locator("input[type='file']")
                if await fi.count() == 0:
                    fi = page.locator("input[type='file']")
                await fi.first.set_input_files(resume_path)
                r["filled"].append("resume"); await page.wait_for_timeout(2000)
            except Exception as e:
                r["note"] += f" resume:{e};"

        # country-type dropdowns (combobox -> type -> pick option) — best-effort, generic
        try:
            combos = page.get_by_role("combobox")
            n = await combos.count()
            for i in range(min(n, 8)):
                c = combos.nth(i)
                nm = ((await c.get_attribute("aria-label")) or "").lower()
                if "country" in nm:
                    await c.click(); await c.fill("Germany"); await page.wait_for_timeout(600)
                    try:
                        await page.get_by_role("option", name=re.compile(r"^germany", re.I)).first.click(timeout=2000)
                        r["filled"].append("country")
                    except Exception:
                        pass
        except Exception:
            pass

        r["ok"] = True; r["stage"] = "review"
        r["note"] = ("Greenhouse filled (" + ", ".join(sorted(set(r["filled"]))) +
                     "). STOPPED before Submit — review + submit in noVNC.")
        return r
    except Exception as e:
        r["note"] = f"greenhouse driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    import json
    d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"), encoding="utf-8"))
    print(json.dumps(asyncio.run(drive(d, "/agent/out/resume.pdf",
          "https://job-boards.greenhouse.io/gitlab/jobs/8603849002")), indent=2))
