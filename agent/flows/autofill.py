"""Generic ATS autofill — the 'password-manager' engine, done ours + open.

Maps a candidate profile onto whatever form fields a page shows, using the SAME heuristics a password
manager uses: name / id / autocomplete / placeholder / aria-label / associated <label> text. Purely
deterministic — no LLM, no vision, no screenshots. Works on any ATS (Workday, Taleo, SuccessFactors,
Personio, HiBob, Greenhouse, Lever) as the fast baseline; per-ATS adapters add high-confidence overlays.

fill_page(page, profile) -> {"filled": [...], "unmapped_required": [{label,type,selector}], "checkboxes": n}
"""
from __future__ import annotations
import re

# (regex over a field's combined text signals, profile key). Order = priority (first match wins).
RULES = [
    (r"first[\s_-]*name|given[\s_-]*name|\bfname\b|legalname.*first",            "first_name"),
    (r"last[\s_-]*name|surname|family[\s_-]*name|\blname\b|legalname.*last",     "last_name"),
    (r"pre(ferred|ferred)?[\s_-]*name",                                          "first_name"),
    (r"e-?mail",                                                                 "email"),
    (r"confirm.*e-?mail|verify.*e-?mail",                                        "email"),
    (r"phone|mobile|\btel(ephone)?\b|contact.*number",                          "phone"),
    (r"address.*line.?1|addressline1|street[\s_-]*address|\bstreet\b|\baddress1\b", "addr1"),
    (r"address.*line.?2|\baddress2\b|apt|suite|\bunit\b",                        "addr2"),
    (r"\bcity\b|town|locality|municipal",                                        "city"),
    (r"post(al)?[\s_-]*code|\bzip\b|\bpostcode\b",                               "postal"),
    (r"\bstate\b|province|\bregion\b|county",                                    "region"),
    (r"\bcountry\b",                                                             "country"),
    (r"linked-?in",                                                              "linkedin"),
    (r"web[\s_-]*site|portfolio|personal.*(site|url)|\bhomepage\b",              "website"),
    (r"full[\s_-]*name|(^|[^a-z])name([^a-z]|$)|your[\s_-]*name",                "full_name"),
]

FILLABLE = ("input[type='text']", "input[type='email']", "input[type='tel']",
            "input[type='url']", "input[type='number']", "input:not([type])", "textarea")


def profile_values(basics: dict) -> dict:
    legal = (basics.get("legal_name") or basics.get("name") or "").split()
    first = legal[0] if legal else ""
    last = " ".join(legal[1:]) if len(legal) > 1 else ""
    addr = basics.get("address", "")
    line1, city, postal = addr, "", ""
    parts = [p.strip() for p in addr.split(",")]
    if parts:
        line1 = parts[0]
    rest = " ".join(parts[1:]) if len(parts) > 1 else ""
    m = re.search(r"\b(\d{4,6})\b", rest)
    if m:
        postal = m.group(1)
        city = re.sub(r"\(.*?\)", "", rest.replace(postal, "")).strip()
    return {
        "first_name": first, "last_name": last, "full_name": basics.get("name", ""),
        "email": basics.get("email", ""), "phone": basics.get("phone", ""),
        "addr1": line1, "addr2": "", "city": city, "postal": postal,
        "region": "Hessen", "country": "Germany",
        "linkedin": basics.get("linkedin", ""),
        "website": (basics.get("websites", [""]) or [""])[0],
    }


async def _signals(page, el) -> str:
    sig = []
    for attr in ("name", "id", "placeholder", "aria-label", "autocomplete", "data-automation-id"):
        try:
            v = await el.get_attribute(attr)
        except Exception:
            v = None
        if v:
            sig.append(v)
    eid = None
    try:
        eid = await el.get_attribute("id")
    except Exception:
        pass
    if eid:
        try:
            lab = page.locator(f'label[for="{eid}"]')
            if await lab.count():
                sig.append(await lab.first.inner_text())
        except Exception:
            pass
    return " ".join(sig).lower()


def _match_key(signal: str) -> str:
    for rx, key in RULES:
        if re.search(rx, signal):
            return key
    return ""


async def fill_page(page, profile: dict) -> dict:
    """Fill every mappable field on the CURRENT page from the profile. Returns a report."""
    vals = profile_values(profile.get("basics", {}))
    out = {"filled": [], "unmapped_required": [], "checkboxes": 0}

    # 1) text-like inputs + textareas
    for sel in FILLABLE:
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 60)):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                if (await el.input_value()) not in ("", None):   # don't clobber existing values
                    continue
            except Exception:
                continue
            sig = await _signals(page, el)
            key = _match_key(sig)
            val = vals.get(key, "")
            if key and val:
                try:
                    await el.click(); await el.fill(str(val))
                    out["filled"].append(key)
                except Exception:
                    pass
            else:
                required = "required" in sig or "*" in sig
                if required:
                    out["unmapped_required"].append({"label": sig[:80], "type": "text"})

    # 2) consent / agreement checkboxes
    cb = page.locator("input[type='checkbox']")
    try:
        cn = await cb.count()
    except Exception:
        cn = 0
    for i in range(min(cn, 20)):
        el = cb.nth(i)
        try:
            if not await el.is_visible() or await el.is_checked():
                continue
            sig = await _signals(page, el)
            if re.search(r"consent|agree|terms|privacy|acknowledge|certify|authoriz", sig):
                await el.check()
                out["checkboxes"] += 1
        except Exception:
            pass

    return out
