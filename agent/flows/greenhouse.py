#!/usr/bin/env python3
"""GREENHOUSE — written from two REAL human recordings, not from guesses.

SOURCES OF TRUTH (both codegen, both committed as sanitised knowledge in flows/knowledge/greenhouse.json):
  * `job-boards.greenhouse.io/tanium/jobs/7800463`  — full application through Submit
  * `job-boards.greenhouse.io/gitlab/jobs/8603849002` — contact block + phone country flyout

WHAT THE RECORDINGS CORRECTED, and every one of these was a live defect in the previous adapter:

1. **`First Name` NEEDS `exact=True`.** The old code used `re.compile("first name", re.I)`, which is
   an unanchored prefix match, so it ALSO matched **"Preferred First Name"** and `.first` resolved by
   DOM order. This project has already paid for exactly this class at the Workday gate, where
   `r"sign\\s*in"` matched "Sign in with Google" and took the driver off the ATS entirely.

2. **THE PHONE MUST BE THE NATIONAL NUMBER, DIGITS ONLY.** The Accenture recording shows the human
   fighting that box four times -- `+49 157 8551545`, `+49 1578551545`, `1578551545` -- collecting
   `Error Phone Number`, until `15785541545` was accepted. Our profile stores `+49 15785541545`,
   which is one of the values that FAILED. Greenhouse behaves the same way: the country code comes
   from the flyout, never from the text. No amount of model reasoning recovers this fact; only a
   recording contains it.

3. **`Location (City)` TAKES THE CITY ALONE.** He first typed `Friedberg 61169`, got no option, and
   retyped `Friedberg` -- then `Friedberg, Hesse, Germany` appeared. It is an async react-select, so
   the query has to be something the server can match.

4. **THE PHONE COUNTRY IS A SEPARATE FLYOUT**: `group "Phone" -> "Toggle flyout"`, then
   `combobox "Country" (exact)` typed as `ger`, then `option "Germany +"`.

5. **UPLOADS GO THROUGH THE `Attach` BUTTON**, twice: `group "Resume/CV*" -> label "Attach"` for the
   CV, then the next remaining `Attach` for the cover letter. The old adapter reached straight for
   `input[type=file]` and never attached a cover letter at all.

6. **THE SUBMIT BUTTON IS CALLED `Submit application`** -- not "Submit", not "Apply". The old adapter
   stopped before submitting even though the candidate authorised end-to-end submission on
   2026-07-21. `JHW_AUTOSUBMIT=0` restores stop-at-review.

7. **CUSTOM QUESTIONS ARE react-select** (`Select...`) plus free text. A react-select has no `<select>`
   element, so `select_option` can never work on it: you click the input, type, and pick a
   `role=option`.

ORDER OF ANSWERS, cheapest and most certain first, and this is the whole point of the exercise:
    1. flows/knowledge/greenhouse.json   what a HUMAN successfully entered on this ATS
    2. learned.py                        what we answered before and got through with
    3. candidate.md / resume_data.json   the profile
    4. the 3-LLM panel                   only for something none of the above covers
    5. the human on Telegram             last
"""
from __future__ import annotations
import asyncio
import os
import re
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.async_api import async_playwright                            # noqa: E402

try:
    import recordings as KB
except Exception:                                                            # pragma: no cover
    KB = None
try:
    import learned as LN
except Exception:                                                            # pragma: no cover
    LN = None

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
ACTION_TIMEOUT = int(os.getenv("JHW_ACTION_TIMEOUT", "6000"))
AUTOSUBMIT = os.getenv("JHW_AUTOSUBMIT", "1").lower() not in ("0", "false", "no")
ATS = "greenhouse"

# THE ACCESSIBLE NAMES A HUMAN ACTUALLY CLICKED, and nothing else may be targeted.
# Every entry is traceable to a line in one of the two recordings. This lives here, once, so the
# adapter and its guard cannot disagree about what is evidence and what is a guess -- the "one value,
# several homes, the stale one wins" defect this project has paid for repeatedly.
NAMES = {
    "First Name":           "tanium+gitlab  textbox, exact=True (prefix of 'Preferred First Name')",
    "Last Name":            "tanium+gitlab  textbox",
    "Preferred First Name": "tanium         textbox",
    "Email":                "tanium+gitlab  textbox",
    "Phone":                "tanium+gitlab  textbox — NATIONAL digits only",
    "Country":              "tanium+gitlab  combobox, exact=True, typed 'ger' -> option 'Germany +'",
    "Toggle flyout":        "gitlab         label inside group 'Phone'",
    "Location (City)":      "tanium         combobox -> option 'Friedberg, Hesse, Germany'",
    "Website":              "tanium         textbox",
    "LinkedIn Profile":     "tanium+gitlab  textbox",
    "Resume/CV":            "tanium+gitlab  group name (rendered 'Resume/CV*')",
    "Cover Letter":         "tanium         second Attach control",
    "Attach":               "tanium+gitlab  the upload control inside a group",
    "Apply":                "tanium+gitlab  button that reveals the form",
    "Submit application":   "tanium         the FINAL button (not 'Submit', not 'Apply')",
}

_T0 = [0.0]


def _log(msg: str) -> None:
    import time
    if not _T0[0]:
        _T0[0] = time.time()
    print(f"[gh {time.time() - _T0[0]:6.1f}s] {msg}", flush=True)


# ---------------------------------------------------------------- measured value rules
def phone_national(raw: str) -> str:
    """`+49 157 8551545` -> `1578551545`. DIGITS ONLY, NO COUNTRY CODE.

    MEASURED, from the Accenture recording: three formats were rejected with `Error Phone Number`
    and only the bare national number was accepted. The country is chosen in the flyout, so
    repeating it in the text box is what breaks validation. Pure, so it is testable without a
    browser."""
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return ""
    for cc in ("0049", "49"):                     # Germany, written either way
        if d.startswith(cc) and len(d) > len(cc) + 6:
            d = d[len(cc):]
            break
    return d.lstrip("0")


def city_only(raw: str) -> str:
    """`Friedberg 61169` -> `Friedberg`; `Friedberg (Hessen)` -> `Friedberg`.

    MEASURED: typing the postcode with the city returned NO options from the async react-select. The
    server matches on a place name."""
    s = re.sub(r"[,(].*$", "", raw or "")
    s = re.sub(r"\b\d{4,6}\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- small, honest primitives
async def _vis(loc, t: int = 2500) -> bool:
    try:
        await loc.wait_for(state="visible", timeout=t)
        return True
    except Exception:
        return False


async def _fill_exact(page, name: str, value: str, role: str = "textbox") -> bool:
    """Fill by EXACT accessible name. Exact, because `First Name` is a prefix of
    `Preferred First Name` and `.first` would silently take whichever the DOM lists first."""
    if not value:
        return False
    for exact in (True, False):
        try:
            loc = page.get_by_role(role, name=name, exact=exact)
            if await loc.count() == 0:
                continue
            el = loc.first
            if not await _vis(el):
                continue
            await el.click(timeout=ACTION_TIMEOUT)
            await el.fill(str(value), timeout=ACTION_TIMEOUT)
            await _think("field")
            got = (await el.input_value()) or ""
            # A WRITE IS NOT DONE UNTIL IT HAS BEEN READ BACK. This repo's own rule, paid for three
            # times: `fill()` returning without throwing proves only that Playwright typed.
            if got.strip():
                _log(f"    {_pretty(name)!r} = {got[:44]!r}"
                 + ("" if exact else "   (loose match)"))
                return True
            _log(f"    {name!r} WRITTEN BUT READS EMPTY")
        except Exception:
            continue
    return False


async def _react_select(page, name: str, query: str, pick_rx: str = "", role: str = "combobox",
                        exact: bool = False) -> bool:
    """Drive a react-select: click, type, then click a real `role=option`.

    There is no `<select>` behind these, so `select_option` can never work. The option is chosen by
    ROLE so we never click a stray div that merely contains the text."""
    if not query:
        return False
    try:
        loc = page.get_by_role(role, name=name, exact=exact) if exact else \
            page.get_by_role(role, name=re.compile(name, re.I))
        if await loc.count() == 0:
            return False
        el = loc.first
        if not await _vis(el):
            return False
        await el.click(timeout=ACTION_TIMEOUT)
        await el.fill(query, timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(900)                 # async options
        rx = re.compile(pick_rx or ("^" + re.escape(query)), re.I)
        opt = page.get_by_role("option", name=rx)
        if await opt.count() == 0:
            # THE MEASURED RECOVERY: he typed "Friedberg 61169", got nothing, and retyped the city
            # alone. A shorter query is the fix, never a longer one.
            short = query.split()[0]
            if short and short != query:
                await el.fill(short, timeout=ACTION_TIMEOUT)
                await page.wait_for_timeout(900)
                opt = page.get_by_role("option", name=re.compile("^" + re.escape(short), re.I))
        if await opt.count() == 0:
            _log(f"    {_pretty(name)!r}: typed {query!r} and NO option appeared")
            return False
        chosen = (await opt.first.inner_text()) or ""
        await opt.first.click(timeout=ACTION_TIMEOUT)
        _log(f"    {_pretty(name)!r} -> {chosen.strip()[:44]!r}")
        return True
    except Exception as e:
        _log(f"    {name!r}: {type(e).__name__}")
        return False


async def _pick_place(page, label_rx: str, query: str, kb_label: str = "") -> bool:
    """A place typeahead: type the CITY, then take the option the HUMAN picked, not the first match.

    ONE HOME for this rule. There are three Friedbergs in Germany and the recording names the one he
    lives in, so a `^Friedberg` prefix match is a coin toss that has already gone out on a real
    application once."""
    want = ""
    if KB is not None and kb_label:
        want = KB.known_choice(ATS, kb_label) or ""
    pick = ("^" + re.escape(want)) if want else ""
    if want:
        _log(f"    place: querying {query!r} and preferring the recorded {want!r}")
    if await _react_select(page, label_rx, query, pick_rx=pick):
        return True
    # the recorded option may not exist on this tenant; fall back to the plain query
    return bool(want) and await _react_select(page, label_rx, query)


async def _attach(page, group_rx: str, path: str) -> bool:
    """Upload through the `Attach` control, the way a human does it.

    The recording is explicit: click the group's Attach BUTTON, then set files on the Attach INPUT.
    Reaching straight for `input[type=file]` works on some tenants and not on others, so try the
    recorded path first and keep the direct one as the fallback."""
    if not (path and os.path.exists(path)):
        return False
    try:
        grp = page.get_by_role("group", name=re.compile(group_rx, re.I))
        if await grp.count():
            g = grp.first
            try:
                await g.locator("button").filter(has_text=re.compile("attach", re.I)) \
                    .first.click(timeout=2500)
            except Exception:
                pass
            for cand in (g.get_by_label(re.compile("attach", re.I)),
                         g.locator("input[type='file']")):
                if await cand.count():
                    await cand.first.set_input_files(path, timeout=ACTION_TIMEOUT)
                    _log(f"    attached {os.path.basename(path)} to {group_rx}")
                    return True
    except Exception:
        pass
    try:                                              # last resort: the first free file input
        fi = page.locator("input[type='file']")
        n = await fi.count()
        for i in range(n):
            one = fi.nth(i)
            if await one.input_value() == "":
                await one.set_input_files(path, timeout=ACTION_TIMEOUT)
                _log(f"    attached {os.path.basename(path)} to file input #{i}")
                return True
    except Exception as e:
        _log(f"    attach failed: {type(e).__name__}")
    return False


# EVERY react-select ON THE PAGE, with its LABEL, its CURRENT VALUE and whether it is REQUIRED.
# THIS IS THE GAP THAT ENDED THE OKX RUN. Greenhouse renders a choice field as a `.select__control`
# div plus a hidden input -- so it is not a `<select>`, it has no `role=combobox` until focused, and a
# loop over textboxes cannot see it. The run therefore printed
#   REQUIRED+EMPTY: ['Country*', '', 'Location (City)*', '', 'How did you hear about us?*', '']
# and gave up, never having attempted any of the three. (The blank entries are the hidden inputs, and
# they are why the message read like nonsense.)
_SELECTS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('.select__control, [class*="select__control"]').forEach((ctl, i) => {
    const r = ctl.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    // the LABEL: walk out to the field wrapper and take its label/legend text
    let host = ctl, lab = '';
    for (let k = 0; k < 6 && host; k++) {
      host = host.parentElement;
      if (!host) break;
      const l = host.querySelector('label, legend, .label');
      if (l && l.innerText && l.innerText.trim()) { lab = l.innerText.trim(); break; }
    }
    if (!lab) {
      const ai = ctl.querySelector('input[aria-label]');
      lab = ai ? (ai.getAttribute('aria-label') || '') : '';
    }
    lab = lab.replace(/\s+/g, ' ').trim();
    if (!lab || seen.has(lab)) return;
    seen.add(lab);
    // the CURRENT VALUE: react-select paints it as .select__single-value / .select__multi-value
    const sv = ctl.querySelector('.select__single-value, [class*="single-value"], '
                                 + '.select__multi-value, [class*="multi-value"]');
    const val = sv ? (sv.innerText || '').replace(/\s+/g, ' ').trim() : '';
    const hid = ctl.parentElement ? ctl.parentElement.querySelector('input[type=hidden]') : null;
    const req = /\*/.test(lab)
                || !!(ctl.querySelector('input[aria-required="true"], input[required]'))
                || !!(hid && hid.required);
    ctl.setAttribute('data-jhw-sel', String(out.length));
    out.push({i: out.length, label: lab.replace(/\s*\*$/, '').trim(), raw: lab,
              value: val, required: req, top: Math.round(r.top)});
  });
  return out;
}
"""

# THE OPTIONS a select is currently offering. Read AFTER it has been opened, never guessed.
_OPTIONS_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('[role="option"], .select__option, [class*="select__option"]')
    .forEach(o => {
      const r = o.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      const t = (o.innerText || '').replace(/\s+/g, ' ').trim();
      if (t && t.length < 120 && !out.includes(t)) out.push(t);
    });
  return out.slice(0, 60);
}
"""


async def _selects(page) -> list:
    try:
        return await page.evaluate(_SELECTS_JS)
    except Exception:
        return []


async def _open_select(page, idx: int) -> list:
    """Open one select and report the options it ACTUALLY offers.

    THE RECORDED WAY, and it is the opposite of what the old code did. The human wrote:
        page.locator(".select__control").first.click()
        page.get_by_role("option", name="Germany +").click()
    -- CLICK the control, THEN pick from the list. My version typed `ger` into it and logged
    `typed 'ger' and NO option appeared`, because this widget does not filter on input the way the
    Location box does. Click first; type only if clicking alone gives us nothing to choose from."""
    try:
        ctl = page.locator(f'[data-jhw-sel="{idx}"]')
        if await ctl.count() == 0:
            return []
        await ctl.first.click(timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(650)
        opts = await page.evaluate(_OPTIONS_JS)
        return [o for o in opts if o.lower() not in ("select...", "select", "")]
    except Exception:
        return []


async def _filter_options(page, idx: int, query: str) -> list:
    """Type into an OPEN select and re-read what it now offers.

    WHY THIS IS NECESSARY. react-select VIRTUALISES a long list: for a ~250-entry country dropdown
    only the first few dozen `[role=option]` nodes exist in the DOM at any moment. So reading the
    options and looking for Germany can never find it -- which is exactly what happened, and the
    chooser then matched the recorded query `ger` against the truncated list and picked al-GER-ia.
    The human's own fix in the gitlab recording is to TYPE: `combobox("Country").fill("ger")` and then
    click `option("Germany +")`. Typing on the FOCUSED element avoids having to work out which of the
    page's inputs belongs to this control."""
    try:
        await page.keyboard.type(query[:24], delay=25)
        await page.wait_for_timeout(750)
        opts = await page.evaluate(_OPTIONS_JS)
        return [o for o in opts if o.lower() not in ("select...", "select", "")]
    except Exception:
        return []


async def _choose_option(page, idx: int, value: str) -> bool:
    """Click the option whose text is `value`, then READ THE SELECT BACK."""
    try:
        opt = page.get_by_role("option", name=value, exact=True)
        if await opt.count() == 0:
            opt = page.get_by_role("option", name=re.compile("^" + re.escape(value), re.I))
        if await opt.count() == 0:
            opt = page.locator(".select__option, [class*='select__option']").filter(
                has_text=re.compile("^" + re.escape(value) + "$", re.I))
        if await opt.count() == 0:
            return False
        await opt.first.click(timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(450)
        # A WRITE IS NOT DONE UNTIL IT HAS BEEN READ BACK -- this repo's rule, and react-select is
        # exactly the widget that shows a value it never committed.
        now = await _selects(page)
        got = next((x["value"] for x in now if x["i"] == idx), "")
        if got:
            return True
        got2 = next((x["value"] for x in now if norm_lab(x["label"]) and value[:12].lower()
                     in (x["value"] or "").lower()), "")
        return bool(got2)
    except Exception:
        return False


async def _think(kind: str = "action") -> None:
    """Bounded human-like think-time. Politeness, not evasion — see flows/conduct.py."""
    try:
        import conduct as CD
        await CD.pause(kind)
    except Exception:
        pass


def _pretty(rx: str) -> str:
    """A field name a human can read. The log printed `'location \\(city\\)|^location'` as if that
    were the field -- a diagnostic that looks like a bug is a diagnostic that gets ignored."""
    t = re.split(r"\|", rx or "")[0]
    t = re.sub(r"\\(.)", r"\1", t).lstrip("^").rstrip("$").strip()
    return t or (rx or "")


def norm_lab(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*·•]+", " ", s or "")).strip().lower()


async def _errors(page) -> list:
    """What the page is complaining about, in its own words. Never infer a cause we can read."""
    try:
        return await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll("[role=alert],[aria-live],[class*='error'],[class*='Error']")
              .forEach(e => {
                const r = e.getBoundingClientRect();
                const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
                if (r.width > 1 && r.height > 1 && t && t.length < 200 && !out.includes(t))
                  out.push(t);
              });
            return out.slice(0, 8); }""")
    except Exception:
        return []


async def _required_empty(page, selects: list | None = None) -> list:
    """Required fields that are still blank. POSITIVE evidence, which is what a submit guard needs."""
    try:
        out = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll("input,textarea,select").forEach(e => {
              const t = (e.getAttribute('type') || '').toLowerCase();
              if (['hidden','submit','button','file'].includes(t)) return;
              // A react-select KEEPS A HIDDEN/PROXY INPUT beside its visible control and does not
              // necessarily write the chosen value into it. MEASURED 2026-08-17: Country, Location
              // and How-did-you-hear were all three FILLED -- the read-back confirmed each one -- and
              // this loop still reported them empty, so the run refused to submit a complete form.
              // The select enumerator is authoritative for these; skip them here.
              if (e.closest('.select__control, .select, [class*="select__control"]')) return;
              const req = e.required || e.getAttribute('aria-required') === 'true';
              if (!req) return;
              const r = e.getBoundingClientRect();
              if (r.width < 2 || r.height < 2) return;
              if ((e.value || '').trim()) return;
              let lab = e.getAttribute('aria-label') || '';
              if (!lab && e.id) {
                const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
                if (l) lab = (l.innerText || '').trim();
              }
              const nm = (lab || e.name || t).replace(/\\s+/g, ' ').trim().slice(0, 60);
              // A NAMELESS ENTRY IS NOISE. react-select keeps a hidden input beside its control, and
              // reporting it produced `['Country*', '', 'Location (City)*', '', ...]` -- a message
              // that reads like a bug even though the fields named in it were real.
              if (nm && !out.includes(nm)) out.push(nm);
            });
            return out.slice(0, 12); }""")
    except Exception:
        out = []
    # react-selects have no `value` on any visible input, so the JS above cannot see them.
    for sel in (selects or []):
        if sel.get("required") and not (sel.get("value") or "").strip():
            nm = (sel.get("label") or "").strip()
            if nm and nm not in out:
                out.append(nm)
    return out


_SD_CACHE: dict = {}


def _screening_defaults() -> dict:
    """`## screening_defaults` from userdata/candidate.md as {question-fragment: answer}.

    These are the candidate's OWN WRITTEN ANSWERS, including the EEOC declarations he chose in his
    recording. `choose.rule()` only applies one when its answer is actually an option on the page, so
    a stale entry can never type something the form does not offer."""
    if _SD_CACHE:
        return _SD_CACHE
    for cand in ("/agent/userdata/candidate.md",
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "userdata", "candidate.md")):
        try:
            with open(cand, encoding="utf-8") as fh:
                txt = fh.read()
        except Exception:
            continue
        blk = txt.split("## screening_defaults", 1)
        if len(blk) < 2:
            continue
        for line in blk[1].splitlines():
            line = line.strip()
            if line.startswith("##"):
                break
            m = re.match(r"^-\s*([^:]{2,60}):\s*(.*?)\s*$", line)
            if m and not line.startswith("#"):
                # STRIP THE INLINE COMMENT AND THE QUOTES. candidate.md is written for a HUMAN to
                # read, so its lines look like `- willing_to_accept_travel_assignment: "Yes"  #
                # ALWAYS Yes`. Keeping that verbatim put `Yes"  # ALWAYS Yes` into a form field --
                # measured, not hypothetical.
                val = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip('"').strip("'").strip()
                if val:
                    _SD_CACHE[m.group(1).strip().lower()] = val
        break
    return _SD_CACHE


def _ats_email(b: dict) -> str:
    """The address to REGISTER with — the mailbox we can actually read.

    MEASURED: the run filled `feranicus@s4biz.io` (resume_data.json's contact address) while the
    recording uses `evgeny@s4biz.io`, which is `JHW_MAIL_USER` — the inbox this engine can open to
    read a verification code. Registering with an address we cannot read is how a code lands out of
    reach. Same decision `agent.py::_ats_email` already makes for Workday; the résumé keeps its own
    address for documents."""
    return (os.getenv("JHW_ATS_EMAIL") or os.getenv("JHW_MAIL_USER")
            or b.get("email", "") or "")


def _facts(data: dict) -> str:
    """The few candidate facts a reviewer needs to choose between real options. No secrets."""
    b = (data or {}).get("basics", {}) or {}
    rows = [("name", b.get("legal_name") or b.get("name")), ("email", b.get("email")),
            ("country", b.get("country")), ("city", b.get("city")),
            ("work authorisation", b.get("work_authorisation") or "EU citizen, no sponsorship "
                                                                 "required"),
            ("relocate", b.get("relocate")), ("travel", b.get("travel")),
            ("notice", b.get("notice_period")), ("gender", b.get("gender")),
            ("found the role via", "LinkedIn")]
    return "\n".join(f"- {k}: {v}" for k, v in rows if v)


def _answer(label: str, data: dict, employer: str = "") -> str:
    """The ladder. Knowledge -> learned -> profile. Deterministic, no model, no network."""
    if KB is not None:
        v = KB.known_value(ATS, label)
        if v and not v.startswith("<redacted"):
            return v
    if LN is not None:
        try:
            v = LN.recall(label, employer=employer) or ""
            if v:
                return v
        except Exception:
            pass
    b = (data or {}).get("basics", {}) or {}
    n = re.sub(r"\s+", " ", (label or "").lower()).strip(" *:")
    prof = {
        "website": b.get("website", ""), "linkedin profile": b.get("linkedin", ""),
        "linkedin": b.get("linkedin", ""), "email": b.get("email", ""),
        "github": b.get("github", ""),
    }
    return prof.get(n, "")


# ---------------------------------------------------------------- the flow
async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    ctx.set_default_timeout(ACTION_TIMEOUT)
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    for p in ctx.pages:
        if "greenhouse.io" in (p.url or "").lower():
            page = p
            break
    page.set_default_timeout(ACTION_TIMEOUT)
    return browser, ctx, page


async def drive(data: dict, resume_path: str = "", ats_url: str = "", answer_fn=None,
                cover_path: str = "", asker=None) -> dict:
    """`answer_fn` writes FREE TEXT with a model. `asker` ASKS THE HUMAN. They are not the same thing.

    THE OPERATOR'S QUESTION, and he was right to be suspicious: *"why did our 3 LLMs and Kimi create
    themselves answers and continued?"* On the OKX run nothing had -- no model ran at all. But the
    first version of this fix passed `llm_answer` (a MODEL) in as the last rung, which would have let
    a model answer *"Are you a protected veteran?"* and carry on. That is exactly the failure he was
    describing, and it would have been mine. A declaration about oneself goes to the HUMAN or to a
    written rule, never to a model -- `choose.SENSITIVE` refuses it and `asker` is a different
    parameter so the two can never be confused again."""
    r = {"ok": False, "stage": "start", "filled": [], "note": "", "errors": []}
    b = (data or {}).get("basics", {}) or {}
    legal = (b.get("legal_name") or b.get("name") or "").split()
    first = legal[0] if legal else ""
    last = " ".join(legal[1:]) if len(legal) > 1 else ""
    employer = re.search(r"greenhouse\.io/([\w-]+)/", ats_url or "")
    employer = employer.group(1) if employer else ""
    if not cover_path:
        for c in ("/agent/out/cover_letter.pdf", "/agent/out/cover.pdf"):
            if os.path.exists(c):
                cover_path = c
                break

    # AUDIT TRAIL — IMPROVEMENTS.md §4.4, and the operator's own instruction: "each new job
    # submission and new questions are a learning curve and experience". Every field records WHERE
    # its value came from; `memory.finish()` promotes the answers to LEARNED only when the SITE
    # confirms the submission. Nothing here may ever raise: bookkeeping must not cost an application.
    _t0 = time.time()
    _run = 0
    try:
        import memory as MEM
        _run = MEM.start_run(ats_url, ATS, employer)
    except Exception as _e:
        _log(f"    [memory] unavailable ({type(_e).__name__}) — running without the audit trail")
        MEM = None

    def _rec(label: str, value, source: str) -> None:
        """One field, its value, and the rung that produced it."""
        if not (MEM and _run):
            return
        try:
            MEM.field(_run, label, str(value), source)
        except Exception:
            pass

    pw = await async_playwright().start()
    browser = None
    try:
        browser, ctx, page = await _connect(pw)
        if ats_url and "greenhouse.io" not in (page.url or "").lower():
            await page.goto(ats_url, wait_until="domcontentloaded", timeout=45000)
        await page.bring_to_front()
        await page.wait_for_timeout(1200)
        _log(f"START greenhouse :: {(page.url or '')[:88]}")
        # CONDUCT: are we allowed to touch this employer right now, and is anything in the way?
        try:
            import conduct as CD
        except Exception:
            CD = None
        if CD is not None:
            _gate = CD.check(ats_url or page.url or "")
            if not _gate["ok"]:
                r["stage"] = "rate-limited"
                r["note"] = (f"NOT applying: {_gate['why']}. Try again in "
                             f"{_gate['wait_s'] // 60}min. This protects the candidate from being "
                             f"blocked by the employers he is applying to.")
                _log("    " + r["note"])
                return r
            CD.record(ats_url or page.url or "")
            _ch = await CD.challenge(page, log=_log)
            if _ch["kind"]:
                r["stage"] = "challenge"
                r["note"] = f"a bot challenge is in the way ({_ch['kind']}: {_ch['evidence']})"
                # WE DO NOT TRY TO GET PAST IT. Hand the live browser to the human, who is watching
                # it in noVNC anyway, and say exactly why.
                if asker:
                    try:
                        await asker(CD.handover_text(_ch["kind"], page.url or ats_url or ""))
                    except Exception:
                        pass
                _log("    " + r["note"] + " — handed to you; I will not attempt to defeat it")
                return r
        if KB is not None:
            _log(f"    knowledge: {len(KB.labels(ATS))} recorded field name(s) for this ATS")

        # COOKIES FIRST. Every recording begins with one, and a consent overlay intercepts clicks.
        for nm in (r"accept cookies", r"accept all", r"^accept$", r"agree"):
            try:
                bt = page.get_by_role("button", name=re.compile(nm, re.I)).first
                if await bt.count() and await _vis(bt, 1200):
                    await bt.click(timeout=2500)
                    _log(f"    dismissed the cookie banner ({nm})")
                    break
            except Exception:
                pass

        r["stage"] = "apply"
        try:
            # ANCHOR BOTH ENDS. `^\s*apply\b` also matches "Apply with LinkedIn", and `.first`
            # resolves ties by DOM order -- the exact class that took the Workday driver off the ATS
            # when `r"sign\s*in"` matched "Sign in with Google". The repo-wide selector guard caught
            # this one before it ever ran.
            ap = page.get_by_role(
                "button",
                name=re.compile(r"^\s*apply( now| for this job| to this job)?\s*$", re.I)).first
            if await ap.count() and await _vis(ap, 2000):
                await ap.click(timeout=3000)
                await page.wait_for_timeout(1500)
                _log("    clicked Apply")
        except Exception:
            pass                                     # many boards show the form directly

        # ---- CONTACT. Exact names, from the recordings.
        r["stage"] = "contact"
        for nm, val, key in (("First Name", first, "first_name"),
                             ("Last Name", last, "last_name"),
                             ("Preferred First Name", b.get("preferred_name", ""), "preferred"),
                             ("Email", _ats_email(b), "email")):
            if await _fill_exact(page, nm, val):
                r["filled"].append(key)
                _rec(nm, val, "profile")
        # gitlab words the preferred-name question differently
        if "preferred" not in r["filled"] and b.get("preferred_name"):
            try:
                el = page.get_by_role("textbox", name=re.compile(r"name you'?d prefer", re.I)).first
                if await el.count() and await _vis(el, 1500):
                    await el.fill(b["preferred_name"], timeout=ACTION_TIMEOUT)
                    r["filled"].append("preferred")
            except Exception:
                pass

        # ---- PHONE. Country in the flyout, national digits in the box.
        r["stage"] = "phone"
        try:
            fly = page.get_by_role("group", name=re.compile(r"^phone", re.I)) \
                      .get_by_label(re.compile(r"toggle flyout", re.I)).first
            if await fly.count() and await _vis(fly, 1500):
                await fly.click(timeout=2500)
                await page.wait_for_timeout(400)
        except Exception:
            pass
        # THE PHONE COUNTRY IS LEFT TO THE CHOICE LOOP, deliberately.
        # This used to type `ger` here and log `typed 'ger' and NO option appeared` on every run --
        # because that widget does not filter on input, and the recording shows the human CLICKING the
        # control and picking `Germany +`. The choice loop does exactly that, with the real option list
        # and the recorded answer, and it got `Germany +49` in 1.3s. A futile attempt that logs a
        # scary line on every run is worse than no attempt: it trains you to read past the log.
        nat = phone_national(b.get("phone", ""))
        if nat and await _fill_exact(page, "Phone", nat):
            r["filled"].append("phone")
            _rec("Phone", nat, "recorded")
            _log(f"    phone sent as the NATIONAL number {nat!r} "
                 f"(the recorded fact: +49… is rejected)")

        # ---- LOCATION. City alone.
        # ---- LOCATION. City alone as the QUERY, and the RECORDED option as the answer.
        # MEASURED ON A SUBMITTED APPLICATION, 2026-08-17: this chose 'Friedberg, Bavaria, Germany'
        # -- the wrong Friedberg, on a form that then went to a real employer. It took the first
        # option matching `^Friedberg`, and there are three. His recording says
        # 'Friedberg, Hesse, Germany'; that is not a preference, it is where he lives. I had added the
        # recorded-option preference to the CHOICE-FIELD loop and not to this path, which runs
        # earlier -- so the fix existed and never applied. One rule, and it must be in both places
        # (or, better, in the one helper both call: `_pick_place()`).
        r["stage"] = "location"
        cty = city_only(b.get("city", ""))
        _loc_done = False
        if cty:
            _loc_done = await _pick_place(page, r"location \(city\)|^location", cty,
                                          "Location (City)")
        if _loc_done:
            r["filled"].append("location")
            _rec("Location (City)", cty, "recorded")
        elif cty:
            # IT FAILED SILENTLY TWICE AND NOBODY HEARD. On Alpega this printed
            #   'location (city)': typed 'Friedberg' and NO option appeared   (x2)
            # and then the run simply moved on, so a REQUIRED field was left empty and the reason was
            # buried in the middle of the log. The choice loop below enumerates every required
            # react-select, so leaving it to that loop is the fix -- and saying so out loud is the
            # other half, because a failure nobody escalates is a failure nobody fixes.
            _log(f"    'Location (City)' did not resolve from {cty!r} on this tenant — leaving it to "
                 f"the choice loop, which reads the options the page really offers")

        # ---- FILES.
        r["stage"] = "files"
        if await _attach(page, r"resume/?cv", resume_path):
            r["filled"].append("resume")
            _rec("Resume/CV", os.path.basename(resume_path or ""), "documents")
            await page.wait_for_timeout(1500)
        if cover_path and await _attach(page, r"cover letter", cover_path):
            r["filled"].append("cover_letter")
            _rec("Cover letter", os.path.basename(cover_path or ""), "documents")
            await page.wait_for_timeout(1200)

        # ---- LINKS + anything else we already know the answer to.
        r["stage"] = "details"
        for nm, key in (("Website", "website"), ("LinkedIn Profile", "linkedin")):
            v = _answer(nm, data, employer)
            if v and await _fill_exact(page, nm, v):
                r["filled"].append(key)
                _rec(nm, v, "profile")

        # ---- WHATEVER IS LEFT. Every still-empty visible textbox gets one deterministic attempt
        # from the ladder; only what the ladder cannot answer is escalated.
        r["stage"] = "questions"
        # ONE HOME, LOADED ONCE. `defaults` used to be built inside the CHOICE block, which runs
        # AFTER this one -- so the free-text path could not see the candidate's written answers at all
        # (the ruff F821 gate caught the reference). Salary and start date live in there.
        defaults = dict(_screening_defaults())
        defaults.update((data or {}).get("screening_defaults") or {})
        unanswered = []
        try:
            boxes = page.get_by_role("textbox")
            for i in range(min(await boxes.count(), 40)):
                el = boxes.nth(i)
                try:
                    if not await el.is_visible() or (await el.input_value() or "").strip():
                        continue
                    lab = ((await el.get_attribute("aria-label"))
                           or (await el.get_attribute("placeholder")) or "").strip()
                    if not lab:
                        continue
                    v = _answer(lab, data, employer)
                    if v:
                        await el.fill(str(v), timeout=ACTION_TIMEOUT)
                        _log(f"    {lab[:44]!r} = {str(v)[:40]!r}   (from knowledge/profile)")
                        r["filled"].append("q:" + lab[:24])
                        _rec(lab, v, "knowledge")
                    else:
                        unanswered.append(lab[:70])
                except Exception:
                    continue
        except Exception:
            pass
        if unanswered:
            # THE ALPEGA RUN ENDED HERE, and not because these were unanswerable. `answer_fn(lab)`
            # was called with ONE argument while `agent.llm_answer(question, options, profile)` takes
            # THREE, so every call raised TypeError -- and `except Exception: v = ""` swallowed it.
            # Six questions, no model call, no log line, and a run that looked like it had nothing to
            # do. A caught exception that produces no output is indistinguishable from success.
            # Now: a WRITTEN FACT first (start date, salary, travel are in candidate.md), then an
            # answer written from HIS OWN CV + cover letter + the job description, then YOU.
            _log(f"    {len(unanswered)} free-text question(s) left: {unanswered[:3]}")
            try:
                import essay as ES
            except Exception as e:
                ES = None
                _log(f"    essay writer unavailable ({type(e).__name__})")
            _job = ES.load_job() if ES else {}
            _cov = ES.load_cover() if ES else ""
            if _cov:
                _log(f"    answering from his own documents "
                     f"({len((data or {}).get('experience') or [])} roles, "
                     f"{len(_cov)} chars of cover letter, JD {_job.get('title', '?')[:34]!r})")
            for lab in unanswered[:8]:
                v, via = "", ""
                if ES is not None:
                    # `answer_fn` and `asker` below were both protected; THIS was not, and it is the
                    # one that raised. `KeyError: slice(None, 18, None)` came out of essay.context()
                    # slicing a dict, left drive() entirely, and the choice fields, the panel and the
                    # Telegram ask were never reached. Belt and braces: essay.write() can no longer
                    # raise, AND its call site is guarded, because the rule is that NOTHING in this
                    # loop may abort the run before the human rung.
                    try:
                        got = await ES.write(lab, data, defaults=defaults, job=_job, cover=_cov,
                                             log=_log)
                        v, via = got.get("value", ""), got.get("via", "")
                    except Exception as e:
                        _log(f"    [essay] call site error ({type(e).__name__}: {str(e)[:70]}) — "
                             f"this question goes to you")
                        v, via = "", "error"
                if not v and answer_fn:
                    # the legacy short-answer model, called with its REAL signature this time
                    try:
                        v = await answer_fn(lab, [], data) or ""
                        via = "llm_answer"
                        if v.strip().upper() in ("UNKNOWN", "N/A", ""):
                            v = ""
                    except Exception as e:
                        _log(f"    answer_fn({lab[:26]!r}) raised {type(e).__name__}: "
                             f"{str(e)[:60]}")
                        v = ""
                if not v and asker:
                    # NOTHING COULD ANSWER IT -> ASK, rather than end the run. This is the rung the
                    # Alpega run never reached.
                    try:
                        rep = await asker(
                            f"One free-text answer needed to finish this application.\n\n"
                            f"Q: {lab}\n\nReply with the answer in your own words.")
                    except Exception:
                        rep = ""
                    v, via = (rep or "").strip(), "human"
                # THE FILL ITSELF CAN RAISE. A Playwright timeout on one textarea would abort the
                # whole run and lose the other five answers — the same class as the essay crash, one
                # step later. My own AST check caught this; nothing in this loop may be unguarded.
                _put = False
                if v:
                    try:
                        _put = await _fill_exact(page, lab, str(v))
                    except Exception as e:
                        _log(f"    could not type the answer to {_pretty(lab)[:34]!r} "
                             f"({type(e).__name__}) — carrying on")
                if _put:
                    _log(f"    {_pretty(lab)[:44]!r} answered ({via}, {len(str(v))} chars)")
                    r["filled"].append("q:" + lab[:24])
                    _rec(lab, v, via)
                    if LN is not None and via in ("documents", "human", "llm_answer"):
                        try:
                            LN.remember(lab, str(v), employer=employer)
                        except Exception:
                            pass
                elif not v:
                    _log(f"    {_pretty(lab)[:44]!r} STILL UNANSWERED")

        # ---- EVERY CHOICE FIELD. This is the block whose absence ended the OKX run.
        # It enumerates the react-selects, OPENS each one to read the options it really offers, and
        # sends the question down the ladder in flows/choose.py: recorded human answer -> learned ->
        # a rule the candidate wrote -> the 3-LLM panel voting over THOSE options -> asking you with
        # them as numbered choices. A model can only ever return a string we showed it, so it cannot
        # invent an answer; and if nothing can decide, YOU are asked instead of the run dying.
        r["stage"] = "choices"
        try:
            import choose as CH
        except Exception as e:
            CH = None
            _log(f"    chooser unavailable ({type(e).__name__}) — choice fields will be skipped")
        if CH is not None:
            facts = _facts(data)
            # `defaults` is loaded once, before the free-text block, so both paths see the same
            # written answers.
            for _round in range(2):                      # a choice can reveal a new required field
                sels = await _selects(page)
                todo = [x for x in sels
                        if x.get("required") and not (x.get("value") or "").strip()]
                if not todo:
                    break
                _log(f"    {len(todo)} choice field(s) still to answer: "
                     f"{[x['label'][:26] for x in todo]}")
                for sel in todo:
                    label, idx = sel["label"], sel["i"]
                    opts = await _open_select(page, idx)
                    if not opts:
                        # It may be a TYPEAHEAD that shows nothing until you type -- which is exactly
                        # what Location (City) does. Try the value we know, then the city rule.
                        seed = _answer(label, data, employer) or ""
                        if re.search(r"location|city|town", label, re.I):
                            seed = city_only(seed or b.get("city", "") or
                                             (KB.known_value(ATS, "Location (City)") if KB else ""))
                        if seed:
                            # PREFER THE OPTION HE ACTUALLY PICKED. The run chose
                            # 'Friedberg, Bavaria, Germany' -- the wrong Friedberg -- because it took
                            # the first option matching `^Friedberg`. His recording says
                            # 'Friedberg, Hesse, Germany', and that is not a preference, it is where
                            # he lives.
                            if await _pick_place(page, re.escape(label[:24]), seed, label):
                                r["filled"].append("sel:" + label[:22])
                                continue
                        _log(f"    {label[:40]!r}: opened it and it offered NO options")
                        continue
                    _log(f"    {label[:40]!r} offers {len(opts)} option(s): {opts[:6]}"
                         + ("  (a long list — it is VIRTUALISED, so this is only what is rendered)"
                            if len(opts) >= 25 else ""))
                    # A LONG LIST IS TRUNCATED BY THE WIDGET, NOT BY THE PAGE. Narrow it with the
                    # answer we already believe in before asking anyone to choose from a partial list.
                    if len(opts) >= 25:
                        seed = (KB.known_choice(ATS, label) if KB else "") \
                            or CH.rule(label, [], defaults) or _answer(label, data, employer) \
                            or (b.get("country", "") if re.search(r"country", label, re.I) else "")
                        seed = re.sub(r"\s*\+\d+\s*$", "", str(seed)).strip()
                        if seed:
                            narrowed = await _filter_options(page, idx, seed)
                            if narrowed:
                                _log(f"    typed {seed[:18]!r} -> {len(narrowed)} option(s): "
                                     f"{narrowed[:5]}")
                                opts = narrowed
                    got = await CH.decide(label, opts, ats=ATS, employer=employer, facts=facts,
                                          defaults=defaults, asker=asker, log=_log)
                    if got["value"] and await _choose_option(page, idx, got["value"]):
                        _log(f"    {label[:40]!r} <- {got['value'][:40]!r}  (via {got['via']})")
                        r["filled"].append("sel:" + label[:22])
                        _rec(label, got["value"], got["via"])
                        if LN is not None and got["via"] in ("panel", "human"):
                            try:
                                LN.remember(label, got["value"], employer=employer)
                            except Exception:
                                pass
                    elif got["value"]:
                        _log(f"    {label[:40]!r}: could not click {got['value'][:30]!r}")
                    else:
                        _log(f"    {label[:40]!r}: UNANSWERED — nothing decided it and nobody "
                             f"answered")
                        try:                              # close the menu so it cannot swallow taps
                            await page.keyboard.press("Escape")
                        except Exception:
                            pass

        # ---- SUBMIT, and only on POSITIVE evidence that the form is complete.
        r["stage"] = "review"
        r["errors"] = await _errors(page)
        _sels_now = await _selects(page)
        missing = await _required_empty(page, _sels_now)
        if r["errors"]:
            _log(f"    the page says: {r['errors'][:3]}")
        if missing:
            _log(f"    REQUIRED+EMPTY: {missing}")
        if not AUTOSUBMIT:
            r["ok"] = True
            r["note"] = ("filled (" + ", ".join(sorted(set(r["filled"]))) +
                         "); JHW_AUTOSUBMIT=0 so it stopped at review.")
            return r
        if missing:
            r["ok"] = False
            r["note"] = ("REFUSING to submit: still required+empty -> " + ", ".join(missing))
            _log("    " + r["note"])
            return r
        try:
            sb = page.get_by_role("button", name=re.compile(r"^\s*submit application\s*$", re.I))
            if await sb.count() == 0:
                sb = page.get_by_role("button", name=re.compile(r"^\s*submit\s*$", re.I))
            if await sb.count() == 0:
                r["ok"] = True
                r["note"] = "no Submit button found; the form is filled and waiting in noVNC."
                return r
            await _think("submit")
            await sb.first.click(timeout=ACTION_TIMEOUT)
            _log("    clicked Submit application")
        except Exception as e:
            r["note"] = f"submit click failed: {type(e).__name__}"
            return r
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        body = ""
        try:
            body = ((await page.inner_text("body")) or "")[:4000].lower()
        except Exception:
            pass
        url = (page.url or "").lower()
        # AN UNCONFIRMED SUBMIT IS A FAILURE TO REPORT, NOT A DETAIL TO SWALLOW. A click that throws
        # no error is not evidence -- the SITE has to say so.
        if re.search(r"thank you|application (submitted|received)|we('| ha)ve received", body) \
           or re.search(r"confirm|thank", url):
            r["ok"] = True
            r["stage"] = "submitted"
            r["note"] = "SUBMITTED — the site confirmed it."
            _log("    " + r["note"])
        else:
            r["ok"] = False
            r["stage"] = "submit-unconfirmed"
            r["errors"] = await _errors(page)
            r["note"] = (f"Submit was clicked but the site did not confirm. url={url[:80]} "
                         f"errors={r['errors'][:2]} first160={body[:160]!r}")
            _log("    " + r["note"])
        return r
    except Exception as e:
        # A CRASH MUST NOT SWALLOW THE WORK. The Alpega run reported only
        # `greenhouse driver error: KeyError: slice(None, 18, None)` and lost everything else: nine
        # fields WERE filled, and the operator could not tell that from the message. Report what was
        # done, name the stage it died in, and -- if the human is reachable -- tell him, because a
        # crash is exactly when he most needs to know.
        import traceback
        r["note"] = (f"greenhouse driver error at stage={r['stage']}: {type(e).__name__}: {e}. "
                     f"Filled before the error: {', '.join(sorted(set(r['filled']))) or 'nothing'}")
        _log("    " + r["note"])
        _log("    " + traceback.format_exc().strip().splitlines()[-1])
        if asker:
            try:
                await asker(f"The Greenhouse run hit an internal error at stage `{r['stage']}`:\n\n"
                            f"{type(e).__name__}: {str(e)[:200]}\n\n"
                            f"{len(set(r['filled']))} field(s) were filled first. The form is still "
                            f"open at http://localhost:9090/vnc.html if you want to finish it.")
            except Exception:
                pass
        return r
    finally:
        # CLOSE THE RUN FIRST. This is the last thing we know about it and the first thing the next
        # run needs — and `finish()` is what promotes an answer to LEARNED, but ONLY when the site
        # confirmed the submission. Nothing weaker is evidence.
        if MEM and _run:
            try:
                MEM.finish(_run, stage=r.get("stage", ""), ok=bool(r.get("ok")),
                           ms=int((time.time() - _t0) * 1000), log=_log)
            except Exception as _e:
                _log(f"    [memory] could not close the run ({type(_e).__name__})")
        # DO NOT close the browser: it is the operator's sandbox Chrome, shared over CDP, and he is
        # watching it in noVNC. Only the CONNECTION is ours to drop.
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("\n[1] the phone rule, which is the single most expensive fact in the recordings")
    for raw, want in (("+49 15785541545", "15785541545"), ("+49 157 85541545", "15785541545"),
                      ("0049 157 8551545", "1578551545"), ("015785541545", "15785541545"),
                      ("15785541545", "15785541545"), ("+49 (0) 157 855 41545", "15785541545"),
                      ("", "")):
        got = phone_national(raw)
        ck(got == want, f"{raw!r:26} -> {got!r:14} (want {want!r})")
    ck(phone_national("+1 415 555 0100") == "14155550100",
       "a non-German number keeps its country code rather than being mangled")

    print("\n[2] the city rule — a postcode in the query returns NO options")
    for raw, want in (("Friedberg 61169", "Friedberg"), ("Friedberg (Hessen)", "Friedberg"),
                      ("Friedberg", "Friedberg"), ("61169 Friedberg", "Friedberg"),
                      ("Frankfurt am Main", "Frankfurt am Main")):
        got = city_only(raw)
        ck(got == want, f"{raw!r:24} -> {got!r}")

    print("\n[3] every name this adapter targets is one a HUMAN actually clicked")
    # MEASURE ARGUMENT POSITION, not string shape. Two earlier versions of this check were wrong in
    # opposite directions: a regex over `_fill_exact(page, "…"` saw 2 of 15 names (vacuous), and a
    # "looks like a label" heuristic then flagged `Germany` (a country VALUE) and a log message. The
    # property is "what do we AIM a locator at", so ask the syntax tree exactly that -- including the
    # names bound by a `for nm, val, key in (...)` loop, which is where most of them live.
    import ast as _ast
    src = open(__file__, encoding="utf-8").read()
    ship = src[:src.index("def _selftest(")]
    tree = _ast.parse(ship)
    # EVERY helper that aims a locator at a name must be listed, or the check silently measures less
    # of its subject than it claims. `_pick_place` was added later and this list was not updated -- the
    # count dropped from 11 to 9 and the floor caught it, which is why the floor is here.
    HELPERS = {"_fill_exact", "_react_select", "_attach", "_pick_place"}
    used = set()
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id in HELPERS:
            for a in n.args[1:]:
                if isinstance(a, _ast.Constant) and isinstance(a.value, str):
                    used.add(a.value.strip())
                    break
        # a loop that feeds a helper: every first element of the iterated tuples is a target name
        if isinstance(n, _ast.For) and isinstance(n.iter, _ast.Tuple):
            calls = {c.func.id for c in _ast.walk(n)
                     if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)}
            if calls & HELPERS:
                for row in n.iter.elts:
                    if isinstance(row, _ast.Tuple) and row.elts and \
                            isinstance(row.elts[0], _ast.Constant) and \
                            isinstance(row.elts[0].value, str):
                        used.add(row.elts[0].value.strip())
    # regex-shaped targets are matched loosely on purpose (tenants decorate labels); check the STEM
    # Compare on ALPHANUMERICS ONLY. My first stem stripped `/?` as a unit and turned `resume/?cv`
    # into `resumecv`, which then matched nothing -- so the check failed a correct target. Punctuation
    # is exactly what differs between a regex, a decorated label and a plain name.
    def _key(t):
        t = re.split(r"[|(]", t)[0]
        return re.sub(r"[^a-z0-9]", "", t.lower())
    # DIRECTIONAL containment. The target must be CONTAINED IN a recorded name, never the reverse:
    # `resumecv` in `resumecv` is fine and `locationcity` in `locationcity` is fine, but a target of
    # `E-Mail Address` CONTAINS the recorded `Email` and is still a name no human ever clicked. My
    # previous version allowed both directions and stopped catching exactly that mutation.
    unknown = sorted({t for t in used
                      if not any(_key(t) and _key(t) in _key(k) for k in NAMES)})
    ck(len(used) >= 10, f"the check sees every targeted name: {len(used)} found -> {sorted(used)}")
    ck(not unknown, f"every targeted name traces to a recording ({len(used)} checked)"
       if not unknown else f"INVENTED, in no recording: {unknown}")
    ck("Submit application" in ship, "the submit button is the recorded 'Submit application'")
    ck("exact=True" in ship, "exact matching is used (First Name vs Preferred First Name)")

    print("\n[4] the answer ladder is knowledge-first and needs no model")
    lad = src[src.index("def _answer("):src.index("# ------", src.index("def _answer("))]
    ck(lad.index("KB.known_value") < lad.index("LN.recall"),
       "recorded human answers outrank remembered ones")
    ck("LN.recall" in lad and lad.index("LN.recall") < lad.index('"website"'),
       "remembered answers outrank the raw profile")
    for banned in ("chat/completions", "httpx", "escalate", "openai"):
        ck(banned not in lad, f"no {banned} in the ladder — it is deterministic")

    print("\n[5] a react-select is never driven with select_option")
    # STRIP COMMENTS AND DOCSTRINGS FIRST. The naive version matched the sentence in `_react_select`'s
    # OWN DOCSTRING explaining why select_option cannot work -- a check aimed at prose instead of
    # code, for the thirteenth time in this project.
    code = re.sub(r"#.*$", "", ship, flags=re.M)
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)
    ck("select_option" not in code,
       "select_option appears in no CODE: there is no <select> behind a react-select")
    ck(len(code) > 6000, f"the stripped slice is still the real module ({len(code)} chars)")
    ck('get_by_role("option"' in ship,
       "options are chosen by ROLE, never by clicking a div that contains the text")

    print("\n[6] submit is guarded by POSITIVE evidence, both ways")
    ck("_required_empty(page" in ship, "required-and-empty is measured before submitting")
    ck("_required_empty(page, _sels_now)" in ship,
       "and the react-selects are included in that measurement — the OKX gap")
    print("\n[8] the four defects the OKX run measured, each closed")
    ck("closest('.select__control" in ship,
       "a react-select's proxy input is NOT counted as an empty required field")
    ck("_filter_options(page, idx" in ship,
       "a virtualised long list is narrowed by TYPING before anyone chooses from it")
    ck("KB.known_choice(ATS, label)" in ship,
       "a choice field uses the OPTION he clicked, never the query he typed")
    ck("_ats_email(b)" in ship, "registration uses the mailbox we can actually read")
    # ONE HOME FOR THE PLACE RULE, asserted by the AST. It went out on a SUBMITTED application as
    # 'Friedberg, Bavaria, Germany' because the preference lived in the choice loop and the location
    # block ran earlier without it. A rule in two places is a rule that drifts.
    import ast as _ast2
    _t = _ast2.parse(ship)
    _drive = next(n for n in _ast2.walk(_t)
                  if isinstance(n, _ast2.AsyncFunctionDef) and n.name == "drive")
    _calls = [n for n in _ast2.walk(_t) if isinstance(n, _ast2.Call)
              and isinstance(n.func, _ast2.Name) and n.func.id == "_pick_place"]
    ck(len(_calls) == 2, f"_pick_place is called from BOTH paths ({len(_calls)} call sites)")
    # THE PRECISE PROPERTY: the option PREFERENCE is expressed as `pick_rx`, and only `_pick_place`
    # may build one. `drive()` still legitimately calls `known_choice` to get a SEED to type into a
    # virtualised list -- my first version of this check forbade that too and failed correct code.
    _dsrc = _ast2.get_source_segment(ship, _drive)
    ck("pick_rx" not in _dsrc,
       "only _pick_place builds the option preference — drive() cannot drift out of sync again")
    ck("pick_rx=pick" in ship, "the typeahead prefers the exact option he picked (Hesse, not Bavaria)")

    # THE ALPEGA FAILURE, AS TWO PROPERTIES. Both were greps in my shell until now, and a guard that
    # lives in a shell is not a guard.
    _dsrc2 = _ast2.get_source_segment(ship, _drive)
    ck("except Exception:\n                        v = \"\"" not in _dsrc2,
       "no bare except swallows the free-text model call (that is what made it silent)")
    ck("answer_fn(lab, [], data)" in _dsrc2,
       "answer_fn is called with its REAL 3-argument signature")
    # REACHABILITY, NOT PRESENCE. `"await asker(" in src` is true even when the branch guarding it has
    # been changed to `if False:` -- which is exactly the mutation I ran, and it slipped through. Ask
    # the syntax tree whether the call sits under a test that actually depends on `asker`.
    def _reachable(tree, fname):
        for nd in _ast2.walk(tree):
            if not (isinstance(nd, _ast2.Call) and isinstance(nd.func, _ast2.Name)
                    and nd.func.id == fname):
                continue
            for anc in _ast2.walk(tree):
                if isinstance(anc, _ast2.If) and any(nd is c for c in _ast2.walk(anc)):
                    names = {x.id for x in _ast2.walk(anc.test) if isinstance(x, _ast2.Name)}
                    if fname in names:
                        return True
            return False        # the call exists but no test depends on the callable
        return False
    _dtree = _ast2.parse(_dsrc2.strip())
    # NOTHING IN THE FREE-TEXT LOOP MAY BE AN UNGUARDED AWAIT. One exception there ended the Alpega
    # run and took nine already-filled fields with it. Ask the tree, per await, whether it sits inside
    # a Try; a string search cannot answer that.
    _loop = _dsrc2[_dsrc2.index("for lab in unanswered"):_dsrc2.index('r["stage"] = "choices"')]
    _lt = _ast2.parse("async def _f():\n" + "\n".join("    " + ln for ln in _loop.splitlines()))
    _aw = [n for n in _ast2.walk(_lt) if isinstance(n, _ast2.Await)]
    _in_try = {id(x) for tr in _ast2.walk(_lt) if isinstance(tr, _ast2.Try)
               for x in _ast2.walk(tr) if isinstance(x, _ast2.Await)}
    _bare = [_ast2.unparse(a)[:46] for a in _aw if id(a) not in _in_try]
    ck(not _bare, (f"every await in the free-text loop is inside a try ({len(_aw)} awaits)"
                   if not _bare else f"UNGUARDED await(s): {_bare}"))

    ck(_reachable(_dtree, "asker"),
       "the ASK rung is REACHABLE — guarded by a test on `asker`, not by a constant")
    ck("ES.write(" in _dsrc2,
       "a free-text question reaches his own DOCUMENTS before anything else")
    # STRIP COMMENTS FIRST. The comment above the loop QUOTES `answer_fn(lab)` while explaining why
    # the one-argument call was the bug -- so an ordering comparison matched the prose instead of the
    # code and failed a correct file. That is the same self-referential false positive this project
    # has now hit a dozen times, and stripping comments is always the fix.
    _code2 = re.sub(r"#.*$", "", _dsrc2, flags=re.M)
    ck(_code2.index("ES.write(") < _code2.index("answer_fn(lab"),
       "his own documents are tried BEFORE any short-answer model")

    ck("asker=asker" in ship and "asker=answer_fn" not in ship,
       "the last rung is the HUMAN, never a model: a self-declaration must not be generated")
    ck('r["note"] = ("REFUSING to submit' in ship, "and it REFUSES rather than sending a blank form")
    ck("thank you|application (submitted|received)" in ship,
       "success is claimed only on the SITE's own confirmation")
    ck("submit-unconfirmed" in ship, "an unconfirmed submit is reported as a failure")

    print("\n[7] the operator's browser is never closed out from under him")
    fin = ship[ship.rindex("finally:"):]
    ck("browser.close()" in fin and "ctx.close()" not in fin and "context.close()" not in fin,
       "only the CDP connection is dropped; the shared Chrome and its context survive")

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} GREENHOUSE CONTRACT(S) BROKEN")
        return 1
    print("ALL GREENHOUSE CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    import json
    d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"),
                       encoding="utf-8"))
    print(json.dumps(asyncio.run(drive(
        d, os.getenv("JHW_RESUME", "/agent/out/resume.pdf"),
        os.getenv("JHW_URL", "https://job-boards.greenhouse.io/okx/jobs/7699600003"))), indent=2))
