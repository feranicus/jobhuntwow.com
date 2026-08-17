#!/usr/bin/env python3
"""ONE home for "how does a DOM self-test get a browser", and why it must not be the one you watch.

THE INCIDENT (2026-08-16). The candidate sent five noVNC screenshots — Application Questions with
his real Accenture ethics question, Voluntary Disclosures, a Skills typeahead, and finally
**"Thank you for your application."** — and asked whether they were leftovers from the NTT Phenom
submission. They were not. Every one is a `page.set_content()` FIXTURE from
flows/test_workday_dom.py, rendered in the SANDBOX Chromium because that suite did

    browser = await pw.chromium.connect_over_cdp(W.CDP_URL)
    ctx     = browser.contexts[0]          # <-- his REAL persistent profile context
    page    = await ctx.new_page()

The URL bar in every screenshot says `about:blank`, which is the tell.

THREE REAL PROBLEMS, not one cosmetic one:
 1. **A FAKE CONFIRMATION IS THE WORST POSSIBLE FALSE SIGNAL.** This repo's rule is that a submit is
    only believed on the SITE's own confirmation. A self-test that paints "Thank you for your
    application." into the browser the operator is watching manufactures exactly the evidence the
    rule exists to protect. He was right to ask.
 2. **IT RAN IN HIS PERSISTENT PROFILE CONTEXT** — the one holding the Google session that gates his
    mailbox. `set_content` is harmless in itself, but a test has no business in that context.
 3. **LEAKED TABS.** `page.close()` sits in a `finally`, and `_dom_selftest` kills the suite at 120s;
    a SIGKILL cannot run `finally`, which is why he had two stray `about:blank` tabs.

SO: a self-test gets its OWN throwaway browser. `channel="chrome"` uses the google-chrome already in
this image (the Dockerfile installs google-chrome-stable), so nothing new is downloaded --
`pw.chromium.launch()` with no channel would look for playwright's BUNDLED chromium, which is never
installed here, and the suite would have failed on his machine with "Executable doesn't exist". That
is the fifth time in this workstream a check could not run where it was invoked.
Only if a private browser cannot start do we fall back to CDP, and then we SELF-HEAL leaked tabs on
entry (a `finally` cannot survive a kill, so cleanup has to happen on the way IN) and every fixture
carries a banner saying it is not real.
"""
from __future__ import annotations
import os

# Painted at the top of every fixture. If a self-test is ever visible in noVNC again, it says so.
BANNER = (
    '<div style="position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#b00020;'
    'color:#fff;font:bold 13px/1.5 sans-serif;padding:6px 10px;text-align:center">'
    'SELF-TEST FIXTURE — NOT A REAL APPLICATION. Nothing here is submitted to any employer.'
    '</div><div style="height:30px"></div>')

ISOLATE = os.getenv("JHW_TEST_ISOLATE", "1").lower() not in ("0", "false", "no")


async def open_test_page(pw, cdp_url: str, viewport: dict | None = None):
    """(page, closer, mode). `mode` is 'private' or 'shared' so the suite can say which it used.

    NEVER raises for the wrong reason: if a private browser cannot start we say why and fall back,
    rather than reporting a DOM contract as broken because of a missing binary."""
    vp = viewport or {"width": 1100, "height": 800}
    if ISOLATE:
        # DELIBERATELY NO bare `launch()` variant. Playwright's bundled chromium is never
        # installed in this image, and attempting it prints a TEN-LINE "please run playwright
        # install" ASCII box to stderr even when the exception is caught -- which swallowed the
        # whole diagnostic window and hid a real assertion failure on the operator's run.
        for kw in ({"channel": "chrome"}, {"executable_path": "/usr/bin/google-chrome"}):
            try:
                b = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], **kw)
                pg = await b.new_page(viewport=vp)
                _banner(pg)

                async def _close(b=b):
                    try:
                        await b.close()
                    except Exception:
                        pass
                how = kw.get("channel") or ("system chrome" if kw else "bundled chromium")
                return pg, _close, f"private ({how})"
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:70]}"
        print(f"  [note] no private browser available ({last}) — falling back to the sandbox over "
              f"CDP. Fixtures WILL be visible in noVNC; they carry a NOT-REAL banner.", flush=True)

    # ---- fallback: the shared sandbox browser. Self-heal leaked test tabs FIRST.
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    killed = 0
    for p in list(ctx.pages):
        try:
            if (p.url or "") in ("about:blank", "") and len(ctx.pages) > 1:
                await p.close(); killed += 1
        except Exception:
            pass
    if killed:
        print(f"  [note] closed {killed} leaked self-test tab(s) from an earlier killed run",
              flush=True)
    page = await ctx.new_page()
    _banner(page)

    async def _close(page=page, browser=browser):
        try:
            await page.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
    return page, _close, "shared sandbox (visible in noVNC)"


def _banner(page):
    """Make EVERY fixture this page renders carry the not-real banner, by wrapping set_content once.

    Wrapping here rather than editing ~25 `set_content(...)` call sites across three suites keeps ONE
    home for the rule and cannot miss one. If the object refuses the attribute we say so and carry
    on: a missing banner must never fail a DOM contract."""
    try:
        real = page.set_content

        async def wrapped(html, **kw):
            return await real(BANNER + (html or ""), **kw)

        page.set_content = wrapped
        return True
    except Exception as e:
        print(f"  [note] could not wrap set_content ({type(e).__name__}) — fixtures render "
              f"without the NOT-REAL banner", flush=True)
        return False


def fixture(html: str) -> str:
    """Every fixture goes through here, so a test page can never be mistaken for a real form."""
    return BANNER + html
