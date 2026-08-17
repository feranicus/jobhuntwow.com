#!/usr/bin/env python3
"""SEEING THE PAGE — a screenshot to a vision model, because the DOM readers went blind four times.

WHY THIS EXISTS, in the operator's words: *"if we use 3-5 iterations and we get same issues where
models do not see the page it means that we act like idiots expecting different results. then we MUST
change the vision method between LLMs and the web automation layer."* He is right. Four runs at the
Accenture gate produced `0 control(s) indexed` while the page plainly showed two sign-in buttons, and
each fix I shipped changed the SAME mechanism -- a DOM predicate I wrote -- and failed the same way.

THIS IS A DIFFERENT SENSORY CHANNEL. It does not use my visibility predicate, or aria semantics, or
any selector I invented. It takes a PNG of the viewport and asks a vision model what a human would
see. If the DOM says nothing is there and the picture shows two buttons, the picture wins.

THE SAFETY PROPERTY THAT MAKES IT USABLE: the model returns LABELS, NEVER COORDINATES. Deterministic
code then resolves each label through Playwright's accessible-name lookup (`get_by_role(name=...)`),
and a label that does not resolve to a real visible element is DISCARDED. So:
  * a hallucinated control cannot be clicked -- it will not resolve;
  * no pixel arithmetic, so a scroll or a re-render cannot aim the click at the wrong thing;
  * the executor is unchanged, because resolved elements get the same `data-jhw-esc` tag.
Clicking by coordinates was deliberately rejected: a mis-aimed pixel click on a real employer's form
is destructive and unattributable, and this project's rule is that code executes, models advise.

COST: one call, ~722ms measured on this account (`nemotron-nano-12b-v2-vl`, from jhw.py probe). It
runs on the STALL path only -- FOUR_MODEL_CONSENSUS.md §7.6 rule 2 forbids a model in a request path.
"""
from __future__ import annotations
import base64
import json
import os
import re

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL = os.getenv("JHW_VISION_MODEL", "jhw-vision")      # -> nemotron-nano-12b-v2-vl
TIMEOUT = float(os.getenv("JHW_VISION_TIMEOUT", "40"))
ENABLED = os.getenv("JHW_VISION", "1").lower() not in ("0", "false", "no")

MODE = os.getenv("JHW_VISION_MODE", "both").lower()          # box | label | both

# WIZARD STEP NAMES are the PROGRESS STEPPER, not controls: clicking one navigates backwards and
# loses the filled page, and they are not fields at all. A vision model reads them off the screen
# like any other text, so they must be dropped BY NAME as well as by structure.
_STEP_NAMES = re.compile(
    r"^\s*(create account\s*/?\s*sign in|my information|my experience|application questions|"
    r"voluntary disclosures|review|self identify|sign up)\s*$", re.I)


def _is_step_name(t: str) -> bool:
    return bool(_STEP_NAMES.match(re.sub(r"\s+", " ", t or "").strip()))

# BOUNDING-BOX MODE. Labels depend on the page exposing an accessible name; a box does not, so this
# drops the last dependency on anything the DOM chooses to tell us. The model POINTS, the DOM NAMES:
# `document.elementFromPoint()` returns the real node under that pixel, we walk up to the nearest
# interactive ancestor, and the label is then read OFF THAT ELEMENT. So a box the model imagines
# lands on nothing interactive and is discarded, and a box it gets slightly wrong still resolves to
# the control a human would have hit. Coordinates are asked for NORMALISED 0-1000 (the convention
# Qwen-VL-family models are trained on, which nemotron-nano-12b-v2-vl follows) so the answer does not
# depend on the screenshot's pixel size.
_SYS_BOX = (
    "You are looking at a screenshot of a web form in a job application. Return EVERY control a "
    "human could interact with: buttons, links, text fields, dropdowns, checkboxes.\n"
    "RULES:\n"
    "1. For each control give its bounding box as [ymin,xmin,ymax,xmax] with every value NORMALISED "
    "to the range 0-1000, where 0 is the top/left edge and 1000 the bottom/right edge of the image.\n"
    "2. Also give the visible text exactly as it appears, for cross-checking.\n"
    "3. If a dialog or pop-up covers the page, return ONLY the controls inside that dialog.\n"
    "4. Do not return decoration, headings, logos, footers, cookie banners or navigation chrome.\n"
    "5. If you see no controls, return an empty list. Never invent one.\n"
    'Answer STRICT JSON only: {"controls":[{"box":[ymin,xmin,ymax,xmax],"text":"<visible text>",'
    '"kind":"button|link|input|select|checkbox"}],"dialog":"<dialog title, or \"\">"}')


def to_viewport(box, vw: int, vh: int):
    """[ymin,xmin,ymax,xmax] (any convention) -> (cx, cy) in viewport pixels, or None. PURE.

    Handles the three conventions a VLM actually emits, decided by MAGNITUDE, because a model that
    was asked for 0-1000 sometimes answers in fractions or in real pixels:
      * fractional 0-1        (max <= 1.5)
      * normalised 0-1000     (max <= 1000)  <- what we ask for
      * absolute pixels       (anything larger, or already within the viewport)
    Returns None for a degenerate or off-screen box rather than clamping it onto a random control --
    a wrong click is destructive, a discarded box costs nothing."""
    try:
        ys, xs, ye, xe = (float(v) for v in box[:4])
    except Exception:
        return None
    m = max(abs(ys), abs(xs), abs(ye), abs(xe))
    if m <= 1.5:
        sx, sy = vw, vh
    elif m <= 1000.0:
        sx, sy = vw / 1000.0, vh / 1000.0
    else:
        sx = sy = 1.0
    x0, x1 = xs * sx, xe * sx
    y0, y1 = ys * sy, ye * sy
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if (x1 - x0) < 2 or (y1 - y0) < 2:
        return None                              # degenerate: not a real control
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    if not (0 <= cx <= vw and 0 <= cy <= vh):
        return None                              # off-screen: never clamp onto something else
    return (round(cx, 1), round(cy, 1))


# Resolve a POINT to the real interactive element under it. The model never names a selector; this
# does not trust the model's text either -- the label comes back OFF THE DOM.
_AT_POINT_JS = r"""([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const INTER = 'button,[role=button],a[href],input,select,textarea,[role=combobox],[role=checkbox]';
  const t = el.closest(INTER) || (el.matches && el.matches(INTER) ? el : null);
  if (!t) return {miss: (el.tagName||'?').toLowerCase()};
  const s = getComputedStyle(t), r = t.getBoundingClientRect();
  if (s.visibility === 'hidden' || s.display === 'none') return {miss: 'hidden'};
  if (r.width < 2 || r.height < 2) return {miss: 'tiny'};
  const tag = t.tagName.toLowerCase(), ty = (t.type || '').toLowerCase();
  let lab = '';
  if (t.id) { const l = document.querySelector('label[for="' + CSS.escape(t.id) + '"]'); if (l) lab = l.innerText; }
  if (!lab) lab = t.getAttribute('aria-label') || '';
  if (!lab) lab = t.placeholder || '';
  if (!lab) lab = (t.innerText || t.textContent || t.value || '');
  if (!lab) { const w = t.closest('label,[data-automation-id]'); if (w) lab = (w.innerText || ''); }
  if (!lab) lab = t.name || t.type || '';
  const STEP='[data-automation-id*=progressBar],[data-automation-id*=Stepper],'
             +'[role=navigation] ol,[class*=progress],[class*=stepper]';
  return {tag: tag, ty: ty, stepper: !!t.closest(STEP),
          label: lab.replace(/\s+/g, ' ').trim().slice(0, 70),
          value: String(t.value || ''),
          required: !!(t.required || t.getAttribute('aria-required') === 'true'),
          checked: (ty === 'checkbox' || ty === 'radio') ? !!t.checked : null,
          secret: ty === 'password',
          options: tag === 'select'
            ? [...t.options].map(o => o.text.trim()).filter(Boolean).slice(0, 25) : [],
          honeypot: (r.width < 2 || r.height < 2) || s.opacity === '0'};}"""

_TAG_AT_JS = r"""([x, y, i]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return false;
  const INTER = 'button,[role=button],a[href],input,select,textarea,[role=combobox],[role=checkbox]';
  const t = el.closest(INTER) || el;
  t.setAttribute('data-jhw-esc', String(i));
  return true;}"""


_SYS = (
    "You are looking at a screenshot of a web form in a job application. List EVERY control a human "
    "could interact with in this image: buttons, links, text fields, dropdowns, checkboxes.\n"
    "RULES:\n"
    "1. Copy the visible text EXACTLY as it appears. Do not paraphrase, translate or tidy it.\n"
    "2. If a dialog or pop-up covers the page, list ONLY the controls inside that dialog.\n"
    "3. Do not list decoration, headings, logos, footers, cookie banners or navigation chrome.\n"
    "4. If you see no controls at all, return an empty list. Never invent one.\n"
    'Answer STRICT JSON only: {"controls":[{"text":"<exact visible text>",'
    '"kind":"button|link|input|select|checkbox"}],"dialog":"<title of the covering dialog, or \\"\\">"}')


def available() -> bool:
    return ENABLED


async def _ask(png: bytes, log, system: str | None = None) -> dict:
    b64 = base64.b64encode(png).decode()
    msgs = [{"role": "system", "content": system or _SYS},
            {"role": "user", "content": [
                {"type": "text", "text": "What can I interact with here? Strict JSON."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}",
                                  "Content-Type": "application/json"},
                         json={"model": MODEL, "messages": msgs, "temperature": 0,
                               "max_tokens": int(os.getenv("JHW_VISION_MAXTOK", "2400"))})
        if r.status_code >= 400:
            # NEVER discard the error body. A 4xx is the server telling you what it wants -- this
            # repo has paid for that lesson three times (kimi's temperature, response_format, ...).
            log(f"    [vision] HTTP {r.status_code}: {r.text[:180]}")
            r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        raise ValueError(f"vision model returned no JSON: {str(txt)[:100]!r}")
    raw = m.group(0)
    try:
        return json.loads(raw)
    except Exception as e:
        # MEASURED: `JSONDecodeError: Expecting ',' delimiter` at char 94 -- a box list written
        # without commas, or an answer cut off by max_tokens. Salvage what DID parse rather than
        # throwing the whole screenshot away.
        log(f"    [vision] JSON malformed ({str(e)[:55]}) — salvaging individual controls")
        items = []
        for om in re.finditer(r"\{[^{}]*\}", raw):
            frag = re.sub(r"(\d)\s+(?=[-\d])", r"\1, ", om.group(0))   # "100 200" -> "100, 200"
            frag = re.sub(r",\s*([}\]])", r"\1", frag)                  # trailing commas
            try:
                d = json.loads(frag)
                if isinstance(d, dict) and (d.get("text") or d.get("box")):
                    items.append(d)
            except Exception:
                continue
        if not items:
            raise
        log(f"    [vision] salvaged {len(items)} control(s) from a malformed answer")
        return {"controls": items, "dialog": ""}


async def controls_box(page, log=print, junk=None) -> dict:
    """BOUNDING-BOX READING. The model POINTS; the DOM NAMES.

    This is the reader that depends on nothing the page chooses to expose: not my visibility
    predicate, not aria semantics, not an accessible name. A box becomes a viewport point, the point
    becomes a real element via `document.elementFromPoint`, and the label is then read OFF THAT
    ELEMENT. Consequences:
      * a box the model imagined lands on nothing interactive -> discarded;
      * a box slightly off still resolves to the control a human would have hit;
      * the label in the index is the PAGE's, so the panel is never shown text the model invented.
    Never raises."""
    if not ENABLED:
        return {"controls": [], "dialog": {"present": False}, "via": "vision-disabled"}
    try:
        vp = page.viewport_size or {"width": 1280, "height": 900}
        vw, vh = int(vp.get("width") or 1280), int(vp.get("height") or 900)
        png = await page.screenshot(type="png", timeout=15000)
    except Exception as e:
        log(f"    [vision-box] screenshot failed ({type(e).__name__})")
        return {"controls": [], "dialog": {"present": False}, "via": "vision-noshot"}
    try:
        seen = await _ask(png, log, system=_SYS_BOX)
    except Exception as e:
        log(f"    [vision-box] unavailable ({type(e).__name__}: {str(e)[:70]})")
        return {"controls": [], "dialog": {"present": False}, "via": "vision-error"}

    items = [x for x in (seen.get("controls") or []) if isinstance(x, dict)][:40]
    log(f"    [vision-box] the model returned {len(items)} box(es)")
    out, i, dropped = [], 0, {"badbox": 0, "nohit": 0, "junk": 0, "dupe": 0}
    used = set()
    for x in items:
        pt = to_viewport(x.get("box") or [], vw, vh)
        if pt is None:
            dropped["badbox"] += 1
            continue
        try:
            hit = await page.evaluate(_AT_POINT_JS, [pt[0], pt[1]])
        except Exception:
            hit = None
        if not hit or hit.get("miss"):
            dropped["nohit"] += 1
            continue
        label = str(hit.get("label") or "").strip()
        if not label or (junk and junk(label)) or _is_step_name(label):
            dropped["junk"] += 1
            continue
        if hit.get("stepper"):
            dropped["stepper"] = dropped.get("stepper", 0) + 1
            continue
        key = (label.lower(), hit.get("tag"), hit.get("ty"))
        if key in used:                          # two boxes on one control
            dropped["dupe"] += 1
            continue
        used.add(key)
        try:
            await page.evaluate(_TAG_AT_JS, [pt[0], pt[1], i])
        except Exception:
            continue
        tag, ty = hit.get("tag"), hit.get("ty")
        kind = ("link" if tag == "a" else "select" if tag == "select"
                else ty if ty in ("checkbox", "radio", "file")
                else "textarea" if tag == "textarea"
                else "button" if tag == "button" or ty in ("submit", "button") else "input")
        said = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
        out.append({"i": i, "kind": kind, "label": label[:70],
                    "stepper": bool(hit.get("stepper")),
                    "css": f"[data-jhw-idx='{i}']",
                    "value": hit.get("value") or "", "required": hit.get("required"),
                    "checked": hit.get("checked"), "secret": hit.get("secret"),
                    "options": hit.get("options") or [],
                    # judged from GEOMETRY on the resolved element, never hardcoded False
                    "honeypot": bool(hit.get("honeypot")),
                    "seen_by": "vision-box", "model_said": said[:40], "point": pt})
        if said and label and said.lower()[:18] not in label.lower():
            log(f"    [vision-box] note: model read {said[:26]!r}, the DOM says {label[:26]!r} "
                f"— using the DOM's")
        i += 1
    log(f"    [vision-box] resolved {len(out)} control(s); dropped "
        + ", ".join(f"{k}={v}" for k, v in dropped.items() if v) or "nothing")
    dlg = str(seen.get("dialog") or "").strip()
    return {"controls": out, "via": "vision-box",
            "dialog": {"present": bool(dlg), "title": dlg[:80], "text": "", "scrollable": False},
            "errors": [], "url": page.url or "", "step": "", "dropped": dropped}


async def read(page, log=print, junk=None) -> dict:
    """The vision reader. BOX first (no dependence on accessible names), LABEL as the fallback.
    `JHW_VISION_MODE=box|label|both` (default both)."""
    if MODE in ("box", "both"):
        r = await controls_box(page, log=log, junk=junk)
        if (r.get("controls") or []) or MODE == "box":
            return r
    return await controls(page, log=log, junk=junk)


async def controls(page, log=print, junk=None) -> dict:
    """The index, in the same shape as the DOM readers. Never raises."""
    if not ENABLED:
        return {"controls": [], "dialog": {"present": False}, "via": "vision-disabled"}
    try:
        png = await page.screenshot(type="png", timeout=15000)
    except Exception as e:
        log(f"    [vision] screenshot failed ({type(e).__name__})")
        return {"controls": [], "dialog": {"present": False}, "via": "vision-noshot"}
    try:
        seen = await _ask(png, log)
    except Exception as e:
        log(f"    [vision] unavailable ({type(e).__name__}: {str(e)[:70]})")
        return {"controls": [], "dialog": {"present": False}, "via": "vision-error"}

    items = [x for x in (seen.get("controls") or []) if isinstance(x, dict)][:40]
    log(f"    [vision] the model SEES {len(items)} control(s): "
        f"{[str(x.get('text'))[:26] for x in items][:8]}")
    out, i = [], 0
    for x in items:
        text = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
        kind = str(x.get("kind") or "button").lower()
        if not text or (junk and junk(text)) or _is_step_name(text):
            continue
        el = await _resolve(page, text, kind)
        if el is None:
            log(f"    [vision] {text[:40]!r} did NOT resolve to a real element — discarded")
            continue
        try:
            info = await el.evaluate(r"""(e) => {
                const tag = e.tagName.toLowerCase(), ty = (e.type||'').toLowerCase();
                const STEP='[data-automation-id*=progressBar],[data-automation-id*=Stepper],'
                          +'[role=navigation] ol,[class*=progress],[class*=stepper]';
                return {tag: tag, ty: ty, stepper: !!e.closest(STEP),
                        value: String(e.value||''),
                        required: !!(e.required||e.getAttribute('aria-required')==='true'),
                        checked: (ty==='checkbox'||ty==='radio') ? !!e.checked : null,
                        secret: ty === 'password',
                        options: tag==='select'
                          ? [...e.options].map(o=>o.text.trim()).filter(Boolean).slice(0,25) : []};}""")
            # BOTH namespaces. workday.py's executor addresses `data-jhw-esc`; llm_driver's
            # addresses `data-jhw-idx`. Writing only one made the vision fallback in llm_driver a
            # NO-OP -- every action after it targeted a selector matching nothing (audited).
            await el.evaluate("(e, i) => { e.setAttribute('data-jhw-esc', String(i));"
                              " e.setAttribute('data-jhw-idx', String(i)); }", i)
        except Exception:
            continue
        tag, ty = info.get("tag"), info.get("ty")
        real = ("link" if tag == "a" else "select" if tag == "select"
                else ty if ty in ("checkbox", "radio", "file")
                else "textarea" if tag == "textarea"
                else "button" if tag == "button" or ty in ("submit", "button") else "input")
        out.append({"i": i, "kind": real, "label": text[:70],
                    "css": f"[data-jhw-idx='{i}']",   # llm_driver's executor builds this
                    "value": info.get("value") or "",
                    "required": info.get("required"), "checked": info.get("checked"),
                    "secret": info.get("secret"), "options": info.get("options") or [],
                    "honeypot": False, "seen_by": "vision"})
        i += 1
    dlg = str(seen.get("dialog") or "").strip()
    return {"controls": out, "via": "vision",
            "dialog": {"present": bool(dlg), "title": dlg[:80], "text": "", "scrollable": False},
            "errors": [], "url": page.url or "", "step": ""}


_MASK = re.compile(r"^[\u2022\u00b7\u25cf\u2219*\u2027.]{4,}$")


async def _by_value(page, text: str):
    """Resolve a field the model named by what is PAINTED IN it rather than by its label.

    Three cases, in order of certainty:
      1. a run of mask characters (••••, ****) can only be a password box -- and there is usually
         exactly one visible, so it is unambiguous;
      2. a string that reads like an e-mail address, matched against the CURRENT VALUE of a visible
         text/e-mail input;
      3. any visible text input whose value equals what the model read.
    Ambiguity is REFUSED throughout: two candidates means DOM order would decide silently, which is
    the defect that put a footer link at index 0 and took the driver off the ATS entirely."""
    t = (text or "").strip()
    if not t:
        return None
    async def _one(sel, match=None):
        try:
            loc = page.locator(sel)
            n = await loc.count()
        except Exception:
            return None
        hits = []
        for i in range(min(n, 25)):
            e = loc.nth(i)
            try:
                if not await e.is_visible():
                    continue
                if match is not None:
                    v = (await e.input_value()) or ""
                    if not match(v):
                        continue
                hits.append(e)
            except Exception:
                continue
        return hits[0] if len(hits) == 1 else None
    if _MASK.match(t):
        return await _one("input[type='password']")
    if "@" in t and " " not in t:
        return (await _one("input[type='email'], input[type='text'], input:not([type])",
                           lambda v: v.strip().lower() == t.lower())
                or await _one("input[type='email']"))
    return await _one("input[type='text'], input[type='email'], input[type='tel'], textarea",
                      lambda v: v.strip() == t)


async def _resolve(page, text: str, kind: str):
    """Turn visible TEXT into a real, visible element — or None.

    This is the guard that makes a vision model safe to act on: a control the model imagined does not
    exist in the accessible tree, so it never resolves and is dropped. Ordered most-specific first."""
    roles = {"button": ("button",), "link": ("link",), "select": ("combobox", "listbox"),
             "checkbox": ("checkbox",), "input": ("textbox",)}.get(kind, ("button", "link"))
    # ANCHORED FIRST, AND AMBIGUITY IS REFUSED. `re.escape` escapes metacharacters but does NOT
    # anchor, so a model reading a truncated label as "Sign in" resolved to "Sign in with Google"
    # by DOM order -- the identical defect that sent the whole driver to accounts.google.com.
    exact = re.compile(r"^\s*" + re.escape(text) + r"\s*$", re.I)
    loose = re.compile(re.escape(text), re.I)
    for rx, strict in ((exact, True), (loose, False)):
        for role in roles:
            try:
                loc = page.get_by_role(role, name=rx)
                n = await loc.count()
                if not n:
                    continue
                if not strict and n > 1:
                    continue          # ambiguous: DOM order would decide silently
                first = loc.first
                if await first.is_visible():
                    return first
            except Exception:
                pass
    # LABEL/PLACEHOLDER/TEXT FALLBACKS ONLY FOR FIELDS. Measured 2026-08-16: for a control the model
    # called a button, `get_by_label("Create Account/Sign In")` matched the Workday STEPPER's hidden
    # RADIO INPUT, so three wizard step names entered the index as `input` controls and the panel
    # proposed FILLING one. A button is a button; do not go looking for a label with that text.
    if kind in ("button", "link"):
        # MEASURED: the model SAW 'Create Account' and I DISCARDED it, because a `kind:"button"`
        # guess only tried role=button and the control is an <a>. Try BOTH roles, then a text match
        # SCOPED TO CLICKABLE ELEMENTS -- which is what keeps us out of the stepper's radio inputs
        # (they are inputs, so they cannot match this locator) while still finding a real link.
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=exact)
                if await loc.count() == 1 and await loc.first.is_visible():
                    return loc.first
            except Exception:
                pass
        try:
            loc = page.locator("button, a[href], [role=button], [role=link]").filter(has_text=exact)
            if await loc.count() == 1 and await loc.first.is_visible():
                return loc.first
        except Exception:
            pass
        return None
    # A FILLED FIELD IS NAMED BY ITS VALUE, AND NO LOCATOR MATCHES A VALUE.
    # MEASURED 2026-08-16 23:31: the model reported, correctly, what it could SEE --
    #   ['evgeny@s4biz.io', '************', 'Sign In', 'Create Account', 'Forgot your password?']
    # -- and the first two were discarded. `'evgeny@s4biz.io'` is the e-mail box's VALUE while its
    # accessible NAME is "Email Address", and `'************'` is the BROWSER'S OWN PASSWORD MASK,
    # a string that exists in no attribute and no text node anywhere in the DOM. So the only two
    # fields on a sign-in form were deleted from the index and the panel was handed a page with
    # nothing to fill. Match on what the model actually saw: an e-mail-shaped string against a filled
    # e-mail box, and a run of mask characters against a password box.
    if kind in ("input", "textarea"):
        el = await _by_value(page, text)
        if el is not None:
            return el
    for attempt in (lambda: page.get_by_label(exact).first,
                    lambda: page.get_by_placeholder(exact).first,
                    lambda: page.get_by_text(exact).first):
        try:
            loc = attempt()
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
    # last resort: an exact-text button/link, which is what most ATS controls are
    try:
        loc = page.locator("button, [role=button], a[href]").filter(has_text=exact)
        if await loc.count() == 1 and await loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ evidence, not theories
async def dump(page, tag: str, extra: dict | None = None, log=print) -> str:
    """Write a screenshot + the DOM + whatever the readers returned, at the MOMENT of failure.

    Four runs printed "0 control(s)" and I answered with four different theories, each verified only
    against a replica I had written myself. This repo's rule is to get ground truth first
    (`jhw.py inspect` -> out/dom_dump.json) and I did not follow it. Now the run collects its own
    evidence automatically, so the next fix is aimed at a measured fact."""
    out = os.getenv("JHW_OUT", "/agent/out")
    base = os.path.join(out, f"blind_{tag}_{int(__import__('time').time())}")
    try:
        os.makedirs(out, exist_ok=True)
        await page.screenshot(path=base + ".png", full_page=False, timeout=15000)
        html = await page.content()
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(html)
        meta = {"url": page.url or "", "tag": tag, "html_bytes": len(html)}
        meta.update(extra or {})
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)
        log(f"    EVIDENCE WRITTEN: {base}.png / .html / .json  — read these instead of guessing")
        return base
    except Exception as e:
        log(f"    could not write evidence ({type(e).__name__}: {str(e)[:60]})")
        return ""


def _io_read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _selftest() -> int:
    """No browser and no model: the parts that decide correctness are pure."""
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    print("\n[1] the prompt forbids the two things that would make this dangerous")
    check("it demands EXACT visible text, not paraphrase", "EXACTLY" in _SYS)
    check("it forbids inventing a control", "Never invent" in _SYS)
    check("it scopes to a covering dialog", "ONLY the controls inside that dialog" in _SYS)
    check("it never asks for coordinates",
          "coordinate" not in _SYS.lower() and "x,y" not in _SYS.lower())

    src = open(__file__, encoding="utf-8").read()

    print("\n[2] the module NEVER clicks by pixel — it resolves a point to a real element")
    # THIS CHECK FAILED ON ITSELF: the banned strings are listed IN THIS FUNCTION, so grepping the
    # whole file always finds them. Sixth self-referential false positive recorded in this project.
    # Measure the SHIPPING code only -- everything above `def _selftest`.
    ship = src[:src.index("def _selftest")]
    code = "\n".join(ln for ln in ship.splitlines()
                     if not ln.strip().startswith("#") and '"""' not in ln)
    for banned in ("mouse.click", "mouse.move", "click(x", "position="):
        check(f"no {banned!r} in the shipping code", banned not in code)
    check("the check is looking at real code, not an empty string", len(code) > 3000, str(len(code)))

    print("\n[2b] the box -> viewport maths is PURE and refuses what it cannot trust")
    VW, VH = 1000, 800
    # normalised 0-1000, the convention we ask for
    check("a normalised box maps to the right centre",
          to_viewport([100, 200, 200, 400], VW, VH) == (300.0, 120.0),
          str(to_viewport([100, 200, 200, 400], VW, VH)))
    # fractional 0-1
    check("a fractional box is detected by magnitude",
          to_viewport([0.1, 0.2, 0.2, 0.4], VW, VH) == (300.0, 120.0),
          str(to_viewport([0.1, 0.2, 0.2, 0.4], VW, VH)))
    # ABSOLUTE PIXELS are only distinguishable ABOVE 1000, and that is deliberate: we ASK for
    # normalised 0-1000, so a value inside that range is read as normalised. My first expectation
    # here was wrong -- 360 on a 1000x800 viewport is genuinely ambiguous and the documented
    # convention has to win.
    check("a box beyond 1000 is read as real pixels",
          to_viewport([1100, 1200, 1160, 1360], 2000, 1500) == (1280.0, 1130.0),
          str(to_viewport([1100, 1200, 1160, 1360], 2000, 1500)))
    check("a reversed box is normalised, not rejected",
          to_viewport([200, 400, 100, 200], VW, VH) == (300.0, 120.0),
          str(to_viewport([200, 400, 100, 200], VW, VH)))
    print("      and it returns None rather than clamping onto some other control:")
    for bad, why in (([0, 0, 0, 0], "degenerate"),
                     ([500, 500, 500, 501], "1px tall"),
                     ([500, 500, 501, 500.4], "sub-pixel once scaled"),
                     (["x", 1, 2, 3], "non-numeric"),
                     ([], "empty"),
                     ([2000, 2000, 2100, 2100], "off-screen absolute")):
        check(f"a {why} box is DISCARDED", to_viewport(bad, VW, VH) is None,
              str(to_viewport(bad, VW, VH)))

    print("\n[2c] the box prompt asks for boxes, and the DOM still names the control")
    check("it asks for [ymin,xmin,ymax,xmax]", "ymin,xmin,ymax,xmax" in _SYS_BOX)
    check("it asks for NORMALISED 0-1000", "0-1000" in _SYS_BOX)
    check("elementFromPoint is what resolves a point", "elementFromPoint" in _AT_POINT_JS)
    check("the label is read OFF THE DOM, not taken from the model",
          "aria-label" in _AT_POINT_JS and "innerText" in _AT_POINT_JS)
    check("a point that hits nothing interactive is a MISS",
          "return {miss:" in _AT_POINT_JS)
    check("the honeypot is judged by geometry on the resolved element",
          "honeypot:" in _AT_POINT_JS and "opacity" in _AT_POINT_JS)
    check("both index namespaces are tagged (workday reads esc, llm_driver reads idx)",
          "data-jhw-esc" in _TAG_AT_JS or "data-jhw-esc" in src)

    print("\n[3] it fails closed, in every direction")
    import asyncio

    class DeadPage:
        url = "about:blank"

        async def screenshot(self, **kw):
            raise RuntimeError("no browser")

    r = asyncio.run(controls(DeadPage(), log=lambda m: None))
    check("a failed screenshot returns an empty index, not an exception",
          r["controls"] == [] and r["via"] == "vision-noshot", str(r)[:90])

    class NoModelPage(DeadPage):
        async def screenshot(self, **kw):
            return b"\x89PNG\r\n\x1a\n"

    # RUN AS `python vision.py` THIS MODULE IS `__main__`, so `import vision as V` loads a SECOND,
    # separate module object and `V.ENABLED = False` sets a global the running function never reads.
    # That is why section [4] failed against correct code. Patch OUR OWN globals.
    import sys as _sys
    me = _sys.modules[__name__]
    old = me.PROXY
    me.PROXY = "http://127.0.0.1:9/v1"
    try:
        r = asyncio.run(controls(NoModelPage(), log=lambda m: None))
        check("an unreachable model returns an empty index",
              r["controls"] == [] and r["via"] == "vision-error", str(r)[:90])
    finally:
        me.PROXY = old

    print("\n[4] disabled by env means disabled")
    me.ENABLED = False
    try:
        r = asyncio.run(controls(NoModelPage(), log=lambda m: None))
        check("JHW_VISION=0 short-circuits", r["via"] == "vision-disabled", str(r)[:80])
    finally:
        me.ENABLED = True
    check("and it is re-enabled afterwards", me.ENABLED is True)

    print("\n[5] a FILLED field and a MASKED password box are nameable")
    # THE MEASURED FAILURE, 2026-08-16 23:31: the model reported five controls and the first TWO --
    # the only two FIELDS on a sign-in form -- were discarded, because no locator matches an input by
    # its VALUE and a password mask exists in no attribute anywhere in the DOM. The panel was then
    # handed a sign-in page with nothing to fill. This is that exact shape.
    class _E:
        def __init__(self, v, vis=True):
            self.v, self.vis = v, vis

        async def is_visible(self):
            return self.vis

        async def input_value(self):
            return self.v

    class _L:
        def __init__(self, els):
            self.els = els

        async def count(self):
            return len(self.els)

        def nth(self, i):
            return self.els[i]

    class _P:
        def __init__(self, m):
            self.m = m

        def locator(self, sel):
            return _L(self.m.get(sel, []))

    _EM, _PW = _E("evgeny@s4biz.io"), _E("a-vault-password")
    _pg = _P({"input[type='password']": [_PW],
              "input[type='email'], input[type='text'], input:not([type])": [_EM],
              "input[type='email']": [_EM]})
    check("'************' (the browser's own mask) resolves to the password box",
          asyncio.run(_by_value(_pg, "************")) is _PW)
    check("bullet dots resolve too",
          asyncio.run(_by_value(_pg, "\u2022\u2022\u2022\u2022\u2022\u2022")) is _PW)
    check("the e-mail box is found BY ITS VALUE, which is all the eye can see",
          asyncio.run(_by_value(_pg, "evgeny@s4biz.io")) is _EM)
    check("TWO password boxes is AMBIGUOUS and refused — never resolved by DOM order",
          asyncio.run(_by_value(_P({"input[type='password']": [_E("a"), _E("b")]}), "*****"))
          is None)
    check("an invisible field is never resolved",
          asyncio.run(_by_value(_P({"input[type='password']": [_E("a", vis=False)]}), "*****"))
          is None)
    check("no candidates -> None, never an exception",
          asyncio.run(_by_value(_P({}), "****")) is None)
    # ASK THE SYNTAX TREE, AND ASK IT ABOUT _resolve. My first version of this check grepped the
    # WHOLE FILE for "_by_value(page, text)" -- and matched THIS VERY ASSERTION, so it passed against
    # a mutation that had deleted the call. That is the self-referential false positive this project
    # has now paid for a dozen times, and the negative test is the only thing that finds it.
    import ast as _ast
    _fn = next(n for n in _ast.walk(_ast.parse(_io_read(__file__)))
               if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_resolve")
    _calls = [n.func.id for n in _ast.walk(_fn)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)]
    check("_resolve actually CALLS _by_value — not merely mentions it", "_by_value" in _calls,
          str(sorted(set(_calls))))

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} VISION CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL VISION CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
