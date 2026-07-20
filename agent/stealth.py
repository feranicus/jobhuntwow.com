"""Stealth browser core for jhw-agent.

Lifted from Linkedin Scraper/linkedin_verifier.py (create_stealth_context, load_cookies,
human_* behaviours, detect_challenge). Runs on the CANDIDATE's machine, using their real
logged-in LinkedIn session — never in the cloud. See V1-PLAN.md §1.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeoutError  # noqa: F401 (re-exported)

# ---------- CONFIG ---------- #
BASE_DIR = Path(__file__).parent
COOKIES_PATH = Path(os.getenv("LINKEDIN_COOKIES", BASE_DIR / "linkedin_cookies.json"))
# Headed by default: LinkedIn detects headless aggressively. WSLg (Win11) shows the window.
HEADLESS = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")

MIN_DELAY = float(os.getenv("MIN_DELAY", "2.25"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "4.5"))

CHALLENGE_URL_PATTERNS = [
    "/checkpoint/challenge", "/authwall", "/uas/login", "/captcha", "security/challenge",
]

# Chrome 131 Windows — keep fingerprint CONSISTENT with the cookie's origin (do not randomise tz).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
        {name: 'Native Client', filename: 'internal-nacl-plugin'}
    ]
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'de'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------- HUMANISATION ---------- #
async def human_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    base = random.uniform(min_s, max_s)
    if random.random() < 0.10:
        base += random.uniform(1.0, 3.0)
    await asyncio.sleep(base)


async def human_scroll(page: Page) -> None:
    for _ in range(random.randint(2, 5)):
        await page.mouse.wheel(0, random.randint(200, 600))
        await asyncio.sleep(random.uniform(0.4, 1.2))
    if random.random() < 0.3:
        await page.mouse.wheel(0, -random.randint(100, 300))
        await asyncio.sleep(random.uniform(0.3, 0.8))


async def human_mouse_move(page: Page) -> None:
    vp = page.viewport_size or {"width": 1280, "height": 720}
    for _ in range(random.randint(1, 3)):
        x = random.randint(100, vp["width"] - 100)
        y = random.randint(100, vp["height"] - 100)
        await page.mouse.move(x, y, steps=random.randint(8, 15))
        await asyncio.sleep(random.uniform(0.1, 0.4))


# ---------- CONTEXT ---------- #
async def create_stealth_context(pw):
    """Returns (context, browser). Consistent Windows-Chrome / Berlin fingerprint."""
    log(f"Browser: headless={HEADLESS}, UA=Chrome131 Win, tz=Europe/Berlin")
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--no-sandbox",
        ],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="Europe/Berlin",
        device_scale_factor=1,
        has_touch=False,
        is_mobile=False,
        color_scheme="light",
    )
    await context.add_init_script(STEALTH_JS)
    return context, browser


async def load_cookies(context: BrowserContext) -> bool:
    """Load LinkedIn session cookies exported from the candidate's browser."""
    if not COOKIES_PATH.exists():
        log(f"[WARN] No cookies file at {COOKIES_PATH}.")
        log("       Export via a 'Get cookies.txt' browser extension -> save as linkedin_cookies.json")
        return False
    try:
        raw = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        ss_map = {
            "strict": "Strict", "lax": "Lax", "none": "None",
            "no_restriction": "None", "unspecified": "Lax", "": "Lax",
        }
        cookies, skipped = [], 0
        for c in raw:
            same_site = ss_map.get(str(c.get("sameSite", "")).lower().strip(), "Lax")
            expires = c.get("expirationDate", c.get("expires", -1))
            if c.get("session"):
                expires = -1
            cookie = {
                "name": c.get("name"), "value": c.get("value"),
                "domain": c.get("domain", ".linkedin.com"), "path": c.get("path", "/"),
                "expires": expires, "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", True), "sameSite": same_site,
            }
            if not cookie["name"] or cookie["value"] is None:
                skipped += 1
                continue
            cookies.append(cookie)
        await context.add_cookies(cookies)
        log(f"[OK] Loaded {len(cookies)} cookies ({skipped} skipped).")
        return True
    except Exception as e:
        log(f"[ERR] Could not load cookies: {e}")
        return False


async def detect_challenge(page: Page) -> bool:
    """True only for REAL checkpoint/captcha pages, not 'verified' badges or SalesNav promos."""
    url = page.url.lower()
    for pattern in CHALLENGE_URL_PATTERNS:
        if pattern in url:
            return True
    try:
        title = (await page.title()).lower()
        if any(x in title for x in [
            "security verification", "security check", "please verify",
            "let's do a quick security check", "challenge",
        ]):
            return True
    except Exception:
        pass
    try:
        frames = await page.query_selector_all(
            "iframe[src*='captcha'], iframe[src*='recaptcha'], "
            "iframe[src*='hcaptcha'], iframe[src*='arkose']"
        )
        if frames:
            return True
    except Exception:
        pass
    return False
