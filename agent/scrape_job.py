"""Scrape a LinkedIn job posting by ID, using the candidate's real logged-in session.

Step 1 of V1 (see V1-PLAN.md §6). Proves the stealth core opens LinkedIn as the candidate
and pulls a job description. Output is a JSON blob the cloud LLM will later tailor against.

Usage:
    python scrape_job.py 4435446898
    python scrape_job.py https://www.linkedin.com/jobs/view/4435446898/
    python scrape_job.py 4435446898 --out job.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

from playwright.async_api import async_playwright

from stealth import (
    create_stealth_context, load_cookies, detect_challenge,
    human_delay, human_scroll, log,
)

JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"

# Candidate selectors — LinkedIn ships several markups; try each, first hit wins.
SEL = {
    "title": [
        "h1.job-details-jobs-unified-top-card__job-title",
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title",
        "h1.top-card-layout__title",
        "h1",
    ],
    "company": [
        ".job-details-jobs-unified-top-card__company-name a",
        ".job-details-jobs-unified-top-card__company-name",
        ".jobs-unified-top-card__company-name",
        "a.topcard__org-name-link",
        ".topcard__org-name-link",
    ],
    "location": [
        ".job-details-jobs-unified-top-card__primary-description-container span.tvm__text",
        ".jobs-unified-top-card__bullet",
        ".topcard__flavor--bullet",
    ],
    "description": [
        "#job-details",
        ".jobs-description__content .jobs-box__html-content",
        ".jobs-description-content__text",
        ".jobs-box__html-content",
        ".show-more-less-html__markup",
        ".description__text",
    ],
    "apply_button": [
        ".jobs-apply-button",
        ".jobs-s-apply button",
        "button.jobs-apply-button",
    ],
}


def parse_job_id(arg: str) -> str:
    m = re.search(r"(\d{6,})", arg)
    if not m:
        sys.exit(f"[ERR] Could not find a job id in '{arg}'")
    return m.group(1)


async def first_text(page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.inner_text()).strip()
                if txt:
                    return re.sub(r"\s+\n", "\n", txt)
        except Exception:
            continue
    return ""


async def detect_apply_type(page) -> tuple[str, str]:
    """Return (apply_type, note). easy_apply | external | unknown."""
    for sel in SEL["apply_button"]:
        try:
            btn = await page.query_selector(sel)
            if not btn:
                continue
            label = ((await btn.inner_text()) or "").strip().lower()
            aria = ((await btn.get_attribute("aria-label")) or "").lower()
            blob = f"{label} {aria}"
            if "easy apply" in blob:
                return "easy_apply", "LinkedIn Easy Apply (stays on LinkedIn)"
            if "apply" in blob:
                # External apply — clicking opens the company ATS (e.g. Workday) in a new tab.
                return "external", "External apply -> company ATS (target for the Workday adapter)"
        except Exception:
            continue
    return "unknown", "Apply button not found (login state or markup changed)"


async def scrape(job_id: str) -> dict:
    url = JOB_URL.format(job_id=job_id)
    async with async_playwright() as pw:
        context, browser = await create_stealth_context(pw)
        try:
            have_cookies = await load_cookies(context)
            if not have_cookies:
                log("[WARN] Continuing without cookies — LinkedIn will likely show an authwall.")
            page = await context.new_page()
            log(f"Opening {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await human_delay()

            if await detect_challenge(page):
                log("[CHALLENGE] LinkedIn security checkpoint hit — solve it in the window, then re-run.")
                return {"job_id": job_id, "url": url, "status": "challenge",
                        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S")}

            # Let the JS-heavy job panel settle; nudge lazy content.
            try:
                await page.wait_for_selector(",".join(SEL["description"]), timeout=12000)
            except Exception:
                log("[info] Description selector didn't appear in 12s — extracting what's present.")
            await human_scroll(page)
            await human_delay(1.0, 2.0)

            # Expand "see more" on the description if present.
            for more in ["button.show-more-less-html__button", ".jobs-description__footer-button",
                         "button[aria-label*='see more']"]:
                try:
                    b = await page.query_selector(more)
                    if b:
                        await b.click()
                        await asyncio.sleep(0.6)
                except Exception:
                    pass

            title = await first_text(page, SEL["title"])
            company = await first_text(page, SEL["company"])
            location = await first_text(page, SEL["location"])
            description = await first_text(page, SEL["description"])
            apply_type, apply_note = await detect_apply_type(page)

            result = {
                "job_id": job_id,
                "url": url,
                "status": "ok" if (title or description) else "empty",
                "title": title,
                "company": company,
                "location": location,
                "apply_type": apply_type,
                "apply_note": apply_note,
                "description": description,
                "description_chars": len(description),
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            return result
        finally:
            await browser.close()


async def main():
    ap = argparse.ArgumentParser(description="Scrape a LinkedIn job by ID or URL.")
    ap.add_argument("job", help="LinkedIn job id or full /jobs/view/ URL")
    ap.add_argument("--out", default="", help="Write JSON to this file too")
    args = ap.parse_args()

    job_id = parse_job_id(args.job)
    result = await scrape(job_id)

    print("\n" + "=" * 70)
    print(f"status      : {result.get('status')}")
    print(f"title       : {result.get('title', '')[:80]}")
    print(f"company     : {result.get('company', '')[:60]}")
    print(f"location    : {result.get('location', '')[:60]}")
    print(f"apply_type  : {result.get('apply_type')}  ({result.get('apply_note')})")
    print(f"desc chars  : {result.get('description_chars', 0)}")
    print("=" * 70)
    print(result.get("description", "")[:600] + ("..." if result.get("description_chars", 0) > 600 else ""))
    print("=" * 70 + "\n")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log(f"[OK] Wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
