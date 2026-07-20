"""Deterministic LinkedIn job SEARCH — built from a REAL Playwright codegen recording
(agent/recordings/linkedin_search.py), not from guesses.

KEY FINDING from the recording: LinkedIn's jobs search UI runs INSIDE an iframe
`[data-testid="interop-iframe"]`. The search box, every filter, and the results list live in its
content frame — NOT the top page. This is almost certainly why earlier scraping/agent attempts saw an
"empty DOM". Assumes an already-logged-in Chrome (persistent profile / injected cookies); login is NOT
done here (the recording's login + password were removed for security).
"""
from __future__ import annotations
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright

CDP_URL  = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
JOBS_URL = "https://www.linkedin.com/jobs/"
IFRAME   = "[data-testid='interop-iframe']"

# recorded filter labels (exact text, as they appear in the iframe)
DATE_LABEL = {"day": "Past 24 hours", "week": "Past week", "month": "Past month", "any": None}


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = None
    for p in ctx.pages:
        if "linkedin.com" in (p.url or ""):
            page = p; break
    page = page or (await ctx.new_page())
    return browser, ctx, page


async def _apply_filter(frame, open_rx, choose_text):
    """Open a filter dropdown, tick an option, click 'Apply current filter'. Best-effort; never raises."""
    try:
        await frame.get_by_role("button", name=re.compile(open_rx, re.I)).first.click()
        await frame.get_by_text(choose_text, exact=True).first.click()
        await frame.get_by_role("button", name=re.compile("Apply current filter", re.I)).first.click()
        return True
    except Exception:
        return False


async def search(keywords: str, location: str = "", date_posted: str = "month",
                 remote: bool = False, experience: str = "", limit: int = 25) -> dict:
    """Run a LinkedIn jobs search and return the job ids found. Deterministic; the iframe is the key."""
    r = {"keywords": keywords, "count": 0, "job_ids": [], "note": ""}
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)

        # top-page keyword box (recording line 24-26): "Title, skill or Company"
        try:
            box = page.get_by_role("textbox", name=re.compile(r"Title, skill", re.I)).first
            await box.click(); await box.fill(keywords); await box.press("Enter")
        except Exception as e:
            r["note"] += f" keyword box: {e};"
        await page.wait_for_timeout(3000)

        frame = page.frame_locator(IFRAME)   # <-- everything below is INSIDE the search iframe

        if location:
            try:
                loc = frame.get_by_role("combobox", name=re.compile(r"location|city", re.I)).first
                await loc.click(); await loc.fill(location); await page.wait_for_timeout(800)
                await loc.press("Enter")
            except Exception as e:
                r["note"] += f" location: {e};"

        if DATE_LABEL.get(date_posted):
            await _apply_filter(frame, r"Date posted", DATE_LABEL[date_posted])
        if experience:
            await _apply_filter(frame, r"Experience level", experience)  # e.g. "Mid-Senior level"
        if remote:
            await _apply_filter(frame, r"Remote", "Remote")
        await page.wait_for_timeout(2500)

        # harvest job links from inside the iframe (a[href*='/jobs/view/'])
        try:
            links = frame.locator("a[href*='/jobs/view/']")
            n = await links.count()
            seen = []
            for i in range(min(n, limit * 2)):
                href = await links.nth(i).get_attribute("href")
                m = re.search(r"/jobs/view/(\d+)", href or "")
                if m and m.group(1) not in seen:
                    seen.append(m.group(1))
                if len(seen) >= limit:
                    break
            r["job_ids"] = seen; r["count"] = len(seen)
        except Exception as e:
            r["note"] += f" harvest: {e};"

        r["note"] = (r["note"] or f"found {r['count']} jobs for '{keywords}'").strip()
        return r
    except Exception as e:
        r["note"] = f"linkedin_search error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    import json
    kw = sys.argv[1] if len(sys.argv) > 1 else "cloud"
    print(json.dumps(asyncio.run(search(kw, date_posted="month", remote=True)), indent=2))
