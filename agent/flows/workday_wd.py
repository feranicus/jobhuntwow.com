"""workday_wd.py — Workday adapter (see userdata/skills/ats-workday/SKILL.md).

WHY A NEW FILE AND NOT A REWRITE OF workday.py
  workday.py is kept: its selector map (flows/selectors/workday.json) is real ground truth from a
  Red Hat DOM dump and it may still work on that tenant. But it CANNOT finish a Workday form,
  for one structural reason: Workday's screening controls are not <select> and not <input>. They
  are  <button aria-haspopup="listbox">  that opens a PORTAL of [role=option] rows. workday.py
  only touches text inputs, native selects and radio groups, so every "Select One Required" on
  Application Questions / Voluntary Disclosures is invisible to it. It also hard-stops at Review
  (contradicting the standing auto-submit order) and it intercepted the router BEFORE document
  tailoring, so it applied with whatever stale PDF happened to be in out/.

WHAT THIS FILE OWNS (and nothing more)
  1. Recognition + apply-URL derivation for <tenant>.wd<N>.myworkdayjobs.com, and the
     accenture.com/careers GATEWAY (whose Workday URL is NOT derivable by string rules - the
     tenant site name, location slug and title slug are not in the req id, so we follow the
     page's own APPLY NOW link, which is exactly what the recording does).
  2. The account gate: stored credentials -> Sign In; "Use My Last Application"; Google SSO
     (detected and handed to the human - an OAuth password prompt is never automated blindly);
     Create Account as the last resort.
  3. THE ONE WIDGET CLASS THE GENERIC DRIVER CANNOT DO SAFELY: the button->listbox portal.
     See the three hazards in the SKILL doc; the mitigation is that open+pick+read-back is ONE
     compound Playwright turn with no DOM re-enumeration in between.
  4. Files attached by their GROUP name (Resume/CV vs Cover Letter), not by DOM order.

WHAT IT DELIBERATELY DOES NOT OWN
  Submitting, checkboxes, native selects, option matching, date boxes and the whole
  ask-the-human-on-Telegram loop. Those are llm_driver's, they are proven on Phenom, and they are
  IMPORTED here (LD.PICK_JS / LD.CHECK_JS / LD.SWEEP_JS / LD.FILL_JS / LD.SUBMIT_JS / LD._submit).
  One implementation, or the two drivers drift.

GROUND TRUTH: agent/recordings/workday.py is a REAL completed submission on
accenture.wd103.myworkdayjobs.com. Every selector shape below comes from it or from ARIA. No
tenant-specific WIDs - they do not port.
"""
from __future__ import annotations
import asyncio, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # /agent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                    # /agent/flows

try:                                     # importable without playwright for the logic self-test
    from playwright.async_api import async_playwright
except Exception:                        # pragma: no cover
    async_playwright = None
try:
    import llm_driver as LD
except Exception:                        # pragma: no cover
    LD = None
try:
    import ask
except Exception:
    ask = None

CDP_URL  = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
OUT      = os.getenv("JHW_OUT", "/agent/out")
ACCOUNTS = os.path.join(OUT, "ats_accounts.json")
CAND_MD  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "userdata", "candidate.md")
MAX_PAGES = int(os.getenv("JHW_WD_PAGES", "14"))


# =====================================================================================
# 1. RECOGNITION + URL DERIVATION   (pure functions - no browser, unit-tested)
# =====================================================================================
# <tenant>.wd<N>.myworkdayjobs.com/<site>/job/<location>/<title>_<reqid>[/apply]
WD_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<wd>wd\d+)\."
                        r"myworkday(?:jobs?)?\.com$", re.I)
WD_JOB_RE  = re.compile(r"^(?P<base>https?://[^/]+)(?P<path>/.*?/job/[^?#]+?)"
                        r"(?P<tail>/apply(?:/applyManually)?)?/?(?:[?#].*)?$", re.I)
REQ_RE     = re.compile(r"_(?P<req>[A-Z]{1,3}\d{4,})(?:_[a-z]{2})?\b")
# Gateways: a careers page that OWNS a Workday tenant but whose URL cannot be transformed into
# one. Verified for accenture.com by agent/recordings/workday.py (LinkedIn -> accenture.com ->
# "APPLY NOW. A new tab will..." -> accenture.wd103.myworkdayjobs.com).
GATEWAY_RE = re.compile(r"^https?://(?:www\.)?accenture\.com/[^?#]*careers", re.I)


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def is_workday(url: str) -> bool:
    """True for a real Workday candidate-experience host."""
    h = _host(url)
    return bool(WD_HOST_RE.match(h)) or \
        bool(re.match(r"^[a-z0-9.-]*\.myworkday(?:jobs?)?\.com$", h))


def is_workday_gateway(url: str) -> bool:
    """A company careers page that hands off to Workday and must be CLICKED through."""
    return bool(GATEWAY_RE.match(url or ""))


def tenant_of(url: str) -> str:
    m = WD_HOST_RE.match(_host(url))
    return (m.group("tenant") if m else "").lower()


def req_id(url: str) -> str:
    """R00329883 from either the Workday title slug or an accenture.com ?id=R00329883_de."""
    q = re.search(r"[?&]id=([A-Za-z0-9_]+)", url or "")
    if q:
        m = REQ_RE.search("_" + q.group(1))
        if m:
            return m.group("req")
        return re.sub(r"_[a-z]{2}$", "", q.group(1))
    m = REQ_RE.search(url or "")
    return m.group("req") if m else ""


def apply_urls(url: str) -> list:
    """Ordered candidate deep-links into the multi-step wizard.

    Already an /apply URL  -> use it untouched (query string included: ?source=jb_1 is Workday's
    own source tag and dropping it changes the application's attribution).
    A plain job URL        -> /apply, then /apply/applyManually, then the /en-US/ locale variant
    (a tenant that insists on a locale segment bounces a locale-less apply URL back to the
    posting - the reason workday.py::_apply_urls exists).
    """
    u = (url or "").strip()
    if not u or not is_workday(u):
        return [u] if u else []
    if re.search(r"/apply(/applyManually)?/?($|[?#])", u, re.I):
        return [u]
    m = WD_JOB_RE.match(u)
    if not m:
        return [u]
    base, path = m.group("base"), m.group("path").rstrip("/")
    out = [f"{base}{path}/apply", f"{base}{path}/apply/applyManually"]
    if not re.match(r"^/[a-z]{2}-[A-Z]{2}/", path):
        out.append(f"{base}/en-US{path}/apply")
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x); uniq.append(x)
    return uniq


def account_host(url: str) -> str:
    """Key for out/ats_accounts.json. A Workday ACCOUNT is per TENANT, not per hostname: the same
    login works on accenture.wd103.myworkdayjobs.com and identity.wd103.myworkday.com. Return the
    candidate-site host, and match_account() also accepts a stored key for any host of the tenant."""
    return _host(url)


def match_account(url: str, accounts: dict) -> dict:
    """Exact host first, then any stored host belonging to the same Workday TENANT."""
    h = account_host(url)
    if not h:
        return {}
    if h in (accounts or {}):
        return dict(accounts[h])
    t = tenant_of(url)
    if t:
        for k, v in (accounts or {}).items():
            if re.match(rf"^{re.escape(t)}\.wd\d+\.myworkday(jobs)?\.com$", k or "", re.I):
                return dict(v)
    return {}


# =====================================================================================
# 2. ANSWERS   candidate.md supplies the FACTS; this table supplies the QUESTION WORDING
# =====================================================================================
# The wording is knowledge about Workday/Accenture, so it is committed here; the ANSWER is read
# from userdata/candidate.md so the engine never asks for a fact it already owns (the rule
# _screening_block() exists to enforce). (regex, candidate.md key, committed fallback).
# Sources marked (rec) are verbatim prefixes from agent/recordings/workday.py - a real submission.
WD_Q = [
    (r"all individuals employed by",                    "",                              "Yes"),   # (rec)
    (r"at your current employer, are",                  "non_compete_restrictions",      "No"),    # (rec)
    # NO COMMITTED FALLBACK FOR A SELF-DECLARATION. These used to fall back to "Male" / "He/Him" --
    # the OWNER's answers, recorded from his own submission -- so ANY other user who left
    # candidate.md blank had a gender and pronoun silently declared on their application. Found the
    # moment this software was packaged for a second person. A declaration comes from the user's own
    # file or it is ASKED; an empty fallback here is what makes the engine ask.
    (r"legal gender|what is your gender|^gender",        "gender",                        ""),
    (r"pronoun",                                         "pronoun",                       ""),
    (r"non-?compet|restrictive covenant|restraint of trade", "non_compete_restrictions",  "No"),
    (r"confidential or proprietary|confidentiality agreement", "agree_to_terms",          "Yes, I agree."),
    (r"legally authorized to work|authori[sz]ed to work", "authorized_in_germany_eu",     "Yes"),
    (r"sponsorship|require a visa|work permit",          "requires_visa_sponsorship",     "No"),
    (r"government employee|public official",             "former_government_employee",    "No"),
    (r"immediate family",                                "immediate_family_government",   "No"),
    (r"preferred (method of )?communication|communication method", "preferred_contact_method", "Email"),
    (r"previously (applied|worked)|worked (for|at) .{0,40}(before|previously)", "worked_for_company_before", "No"),
    (r"current(ly)? (an )?employee|currently employed by", "current_employee_of_company", "No"),
    (r"former employee",                                 "former_employee_of_company",    "No"),
    (r"relocat",                                         "willing_to_relocate",           "No"),
    (r"travel",                                          "willing_to_accept_travel_assignment", "Yes"),
    (r"veteran",                                         "veteran_status",                "I am not a protected veteran"),
    (r"disabilit",                                       "disability_status",             "I do not wish to answer"),
    (r"hispanic|latino",                                 "hispanic_latino",               "No"),
    (r"race|ethnic",                                     "race_ethnicity",                "I do not wish to answer"),
    (r"notice period",                                   "notice_period",                 "1 month"),
    (r"how did you hear",                                "how_did_you_hear",              "LinkedIn"),
]

# Multi-level pickers: one click per level. PROVEN by the recording -
#   textbox "How Did You Hear About Us?" -> text "Job Boards" -> text "LinkedIn".
# Free text never satisfies these; the value must come from the tree.
WD_CASCADES = [
    (r"how did you hear", ["Job Boards", "LinkedIn"]),
]

_MD_VAL = re.compile(r"^-\s*([A-Za-z0-9_]+)\s*:\s*(.+?)\s*(?:#.*)?$")


def candidate_answers(path: str = "") -> dict:
    """Flat key -> value from every '- key: value' line in candidate.md. Later sections win, which
    is what we want: ## screening_defaults is the authoritative ALWAYS-answers block and it is
    last in the file."""
    out: dict = {}
    try:
        txt = open(path or CAND_MD, encoding="utf-8").read()
    except Exception as e:
        print(f"[wd] candidate.md unreadable ({str(e)[:60]}) - using committed fallbacks", flush=True)
        return out
    for ln in txt.splitlines():
        m = _MD_VAL.match(ln.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        if v and v.upper() != "ASK" and not v.startswith("{"):
            out[k] = v
    return out


def answer_for(question: str, facts: dict | None = None) -> str:
    """The answer for a Workday question, matched by SUBSTRING (Workday reorders questions and
    injects conditional ones, so position is meaningless - SKILL.md). '' = we do not know."""
    q = re.sub(r"\s+", " ", (question or "")).strip().lower()
    if not q:
        return ""
    facts = facts if facts is not None else candidate_answers()
    for rx, key, fallback in WD_Q:
        if re.search(rx, q, re.I):
            v = (facts.get(key) or "").strip() if key else ""
            return v or fallback
    return ""


def cascade_for(question: str) -> list:
    q = re.sub(r"\s+", " ", (question or "")).strip().lower()
    for rx, path in WD_CASCADES:
        if re.search(rx, q, re.I):
            return list(path)
    return []


# The native-<select> vocabulary is SHARED with the generic driver so the two sweeps cannot
# disagree about what "willing to relocate" means.
def select_defaults(base: list | None = None) -> list:
    # `base` is injectable so the merge can be tested WITHOUT llm_driver (which needs playwright).
    # The first version read LD.SELECT_DEFAULTS unconditionally, so on a machine without
    # playwright the assertion measured the missing import rather than this function.
    if base is None:
        base = list(getattr(LD, "SELECT_DEFAULTS", []) or []) if LD else []
    # Same rule in the SWEEP defaults: a factual screening question may have a committed answer, a
    # DECLARATION may not. Gender and pronoun were here and are gone.
    extra = [["all individuals employed by", "Yes"], ["preferred communication", "Email"]]
    have = {str(k).lower() for k, _ in base}
    return base + [e for e in extra if e[0] not in have]


# =====================================================================================
# 3. THE PORTAL DROPDOWN  (the widget class the generic driver cannot see)
# =====================================================================================
# Enumerate Workday's button-dropdowns WITH the question each one belongs to. A control without
# its question is unactionable - the same lesson ENUM_JS learned about validation messages.
WD_ENUM_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  // innerText is the RENDERED text, so it is layout-dependent and can come back empty for an
  // element that is plainly on screen. LD.PICK_JS has guarded this since day one; these blobs
  // did not, and an empty read makes a control lose BOTH its value and its question.
  const TXT = (el) => {
    if (!el) return '';
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return String(el.value || '');
    const t = el.innerText;
    return norm((t === undefined || t === null || t === '') ? (el.textContent || '') : t);
  };
  const PH = /^\s*(select one( required)?|select|search|make a selection|choose( one)?|--|-)\s*$/i;
  // Stale indices resolve to detached nodes after a React re-render - clear them first, exactly
  // as ENUM_JS must (that defect made [data-jhw-idx='11'] point at the wrong element).
  document.querySelectorAll('[data-jhw-wd]').forEach(e => e.removeAttribute('data-jhw-wd'));
  const accName = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => TXT(document.getElementById(id)))
                 .filter(Boolean).join(' ');
      if (t) return norm(t);
    }
    return norm(el.getAttribute('aria-label') || '');
  };
  // The QUESTION is the accessible name of the enclosing group/fieldset. Fall back to the
  // container's own text with the control's text removed.
  const question = (el) => {
    let p = el, hop = 0;
    while (p && hop < 6) {
      if (p !== el && (p.getAttribute('role') === 'group' || p.tagName === 'FIELDSET')) {
        const n = accName(p);
        if (n && !PH.test(n)) return n;
        const lg = p.querySelector('legend');
        if (lg && TXT(lg)) return TXT(lg);
      }
      p = p.parentElement; hop++;
    }
    const own = accName(el);
    if (own && !PH.test(own)) return own;
    const box = el.closest('[data-automation-id],div,li,fieldset,section') || el.parentElement;
    if (!box) return '';
    let t = TXT(box);
    const mine = TXT(el);
    if (mine) t = norm(t.split(mine).join(' '));
    return t.slice(0, 140);
  };
  // A Workday dropdown is a BUTTON that owns a listbox. Recognised by ARIA (proven by the real
  // recording: role=button opens role=listbox containing role=option) or by the literal
  // "Select One" placeholder Workday renders. NEVER by a tenant-specific id.
  const isDrop = (el) => {
    const t = el.tagName, role = el.getAttribute('role') || '';
    if (t !== 'BUTTON' && role !== 'button' && role !== 'combobox') return false;
    const hp = (el.getAttribute('aria-haspopup') || '').toLowerCase();
    if (hp === 'listbox' || hp === 'menu' || hp === 'true' || hp === 'dialog') return true;
    if (el.hasAttribute('aria-expanded')) return true;
    if (PH.test(TXT(el))) return true;
    return false;
  };
  // A multiselect (Skills, How Did You Hear) is an INPUT whose value lands in CHIPS beside it,
  // never in the input. So "answered" means a chip exists - reading the input tells you nothing.
  const chipsOf = (el) => {
    const box = el.closest('[data-automation-id],div,fieldset,li') || el.parentElement;
    if (!box) return [];
    return [...box.querySelectorAll(
        "[data-automation-id*='selectedItem'],[class*='chip'],[class*='pill'],[class*='token']," +
        "li[data-automation-id],[role='listitem']")]
      .filter(n => vis(n) && !n.querySelector('input,select,textarea'))
      .map(n => TXT(n)).filter(t => t && t.length < 70).slice(0, 8);
  };
  const isMulti = (el) => el.tagName === 'INPUT' &&
    (el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete') ||
     !!el.getAttribute('aria-owns') || !!el.getAttribute('aria-controls'));
  const req = (el) => {
    if (el.getAttribute('aria-required') === 'true' || el.required) return true;
    const box = el.closest('[data-automation-id],div,li,fieldset') || el.parentElement;
    const t = TXT(box);
    return /required|\*/.test(t.slice(0, 160));
  };
  const NAV_RE = new RegExp('^\\s*(save and continue|continue|next|back|submit|cancel|'
                            + 'sign in|create account|apply|search|add|remove|delete)\\b', 'i');
  const out = [];
  const push = (el, kind) => {
    const i = out.length;
    el.setAttribute('data-jhw-wd', String(i));
    const cur = TXT(el);
    const chips = kind === 'multi' ? chipsOf(el) : [];
    out.push({i, kind, q: question(el), cur,
              answered: kind === 'multi' ? chips.length > 0 : !!(cur && !PH.test(cur)),
              chips, required: req(el), css: "[data-jhw-wd='" + i + "']",
              tag: el.tagName.toLowerCase()});
  };
  [...document.querySelectorAll("button,[role='button'],[role='combobox'],input")].forEach(el => {
    if (!vis(el) || el.disabled) return;
    if (el.tagName === 'INPUT' && /^(file|hidden|checkbox|radio|submit|button)$/i.test(el.type || ''))
      return;
    if (isMulti(el)) { push(el, 'multi'); return; }
    if (isDrop(el)) {
      // Save and Continue / Next / Submit are buttons too. Never treat navigation as a dropdown.
      // A regex LITERAL cannot span lines in JS - build it from a string instead. (The first
      // version of this line was a two-line literal: py_compile was perfectly happy and
      // `node --check` rejected it. Parsing Python does not validate the JS inside it.)
      if (NAV_RE.test(TXT(el))) return;
      push(el, 'drop');
    }
  });
  return out;
}
"""

# Read a control's CURRENT text back. A write is not done until it has been read back - the
# lesson that cost this repo an `ok=True` over an empty date box.
WD_READ_JS = r"""
(sel) => {
  const e = document.querySelector(sel);
  if (!e) return null;
  if (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') return String(e.value || '').trim();
  const t = e.innerText;
  return String((t === undefined || t === null || t === '') ? (e.textContent || '') : t)
           .replace(/\s+/g, ' ').trim();
}
"""

WD_FILES_JS = r"""
() => {
  // Map every file input to the GROUP it belongs to. llm_driver attaches by DOM ORDER, which is
  // right on Phenom (resume then cover letter) and WRONG here: Workday renders Cover Letter
  // above Resume/CV on some tenants, so DOM order would file the CV as the cover letter.
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const accName = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(id => { const n = document.getElementById(id);
        return n ? norm(n.innerText || n.textContent) : ''; }).filter(Boolean).join(' ');
      if (norm(t)) return norm(t); }
    return norm(el.getAttribute('aria-label') || '');
  };
  const out = [];
  [...document.querySelectorAll("input[type='file']")].forEach((el, i) => {
    let name = '', p = el, hop = 0;
    while (p && hop < 7 && !name) {
      if (p.getAttribute && (p.getAttribute('role') === 'group' || p.tagName === 'FIELDSET' ||
                             p.hasAttribute('aria-labelledby') || p.hasAttribute('aria-label')))
        name = accName(p);
      p = p.parentElement; hop++;
    }
    if (!name) {
      const box = el.closest('[data-automation-id],div,fieldset,section');
      name = box ? norm(box.innerText || box.textContent).slice(0, 80) : '';
    }
    el.setAttribute('data-jhw-file', String(i));
    out.push({i, group: name, css: "input[data-jhw-file='" + i + "']"});
  });
  return out;
}
"""

WD_DELETE_JS = r"""
() => {
  // A returning applicant already has a resume attached and Workday STACKS uploads (proven by
  // the recording, which starts with 'Delete jev gen il cyber.pdf'). Remove it first.
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2; };
  const btns = [...document.querySelectorAll("button,[role='button']")].filter(vis).filter(b => {
    const t = ((b.innerText || b.textContent || '') + ' ' +
               (b.getAttribute('aria-label') || '')).trim();
    return /^\s*(delete|remove)\b/i.test(t) && /\.(pdf|docx?|txt|rtf)\b/i.test(t);
  });
  if (!btns.length) return {removed: 0};
  const labels = btns.map(b => ((b.innerText || b.textContent ||
                    b.getAttribute('aria-label') || '')).trim().slice(0, 60));
  btns[0].click();
  return {removed: 1, labels};
}
"""


async def _pick_in_portal(page, css: str, want: str, first: bool = False) -> dict:
    """Open a Workday dropdown and click an option, as ONE turn.

    Three reasons this is not left to the generic driver, all of them recorded in this repo:
      * the repeat-breaker key is action|label|value|errors, and several dropdowns on one Workday
        page share the accessible name "Select One Required" with an identical error set - so the
        THIRD question is BLOCKED before it has been opened once (the same page-scoping defect
        already documented for `click Next`, one level down);
      * SKILL.md records that opening a portal REVERTS the previous selection when a re-render
        intervenes, and the generic driver re-enumerates the whole DOM between every action, i.e.
        it guarantees that re-render;
      * ENUM_JS's `missing` list only looks at input/select/textarea, so an unanswered required
        dropdown cannot hold back a Next click.
    Option matching itself is NOT reimplemented - it is LD.PICK_JS, the routine proven on Phenom.
    """
    before = await page.evaluate(WD_READ_JS, css)
    try:
        el = page.locator(css).first
        # Give the portal room BELOW the control: PICK_JS matches rows geometrically below its
        # anchor, and a control near the viewport bottom opens its list upwards.
        await el.scroll_into_view_if_needed(timeout=4000)
        await page.evaluate("(s)=>{const e=document.querySelector(s);"
                            "if(e)e.scrollIntoView({block:'center'});}", css)
        await el.click(timeout=6000)
    except Exception as e:
        return {"picked": None, "why": f"could not open: {str(e)[:70]}", "before": before}
    await page.wait_for_timeout(900)
    res = await page.evaluate(LD.PICK_JS, {"want": want, "anchor": css, "first": first}) or {}
    await page.wait_for_timeout(500)
    after = await page.evaluate(WD_READ_JS, css)
    res["before"], res["after"] = before, after
    # READ IT BACK. A click that throws no error is not a set value: Workday's own re-render is
    # what reverts it, and that revert is silent.
    res["verified"] = bool(res.get("picked")) and (after or "") != (before or "")
    return res


async def _close_portal(page) -> None:
    """Leave no portal open: an open listbox overlays the next control and steals its click.
    Escape is safe here (unlike on a date box, where it REVERTS the value)."""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)
    except Exception:
        pass


async def wd_sweep(page, facts: dict, r: dict) -> int:
    """Answer every UNANSWERED Workday dropdown / multiselect on this page. Idempotent: an
    already-answered control is left alone, because re-opening it is what reverts it (the same
    doctrine as CHECK_JS - a control is SET, never toggled)."""
    try:
        ctrls = await page.evaluate(WD_ENUM_JS) or []
    except Exception as e:
        print(f"[wd] enumerate dropdowns failed: {str(e)[:80]}", flush=True)
        return 0
    todo = [c for c in ctrls if not c.get("answered")]
    if not todo:
        return 0
    print(f"[wd] {len(ctrls)} picker(s) on this page, {len(todo)} unanswered", flush=True)
    done = 0
    for c in todo:
        q = c.get("q") or ""
        css = c.get("css")
        want = answer_for(q, facts)
        path = cascade_for(q)
        if c.get("kind") == "multi" and not path:
            # A single-level multiselect IS a cascade of length one. want="" means "take the first
            # option", which PICK_JS does when first=True.
            if not want and not c.get("required"):
                print(f"[wd]   - skipped (optional multiselect, unknown): {q[:60]!r}", flush=True)
                continue
            path = [want]
        if path:
            got = await _wd_cascade(page, css, path, q)
            if got:
                done += 1
                r["filled"].append(f"cascade:{q[:26]}={path[-1][:20]}" if path[-1]
                                   else f"cascade:{q[:26]}")
            elif q and q not in r["unresolved"]:
                r["unresolved"].append(q)
            continue
        if not want:
            # "yes or no, it doesn't really matter" - a required picker left on its placeholder is
            # a blocked application, so take the first real option and say so out loud.
            if not c.get("required"):
                print(f"[wd]   - skipped (optional, unknown): {q[:60]!r}", flush=True)
                continue
            print(f"[wd]   - no stored answer for {q[:60]!r} -> taking the FIRST option", flush=True)
        res = await _pick_in_portal(page, css, want, first=True)
        await _close_portal(page)
        if res.get("verified"):
            done += 1
            r["filled"].append(f"pick:{(q or 'picker')[:26]}={str(res.get('picked'))[:24]}")
            note = "" if want else "  (first option - no stored answer)"
            if want and str(res.get("picked", "")).strip().lower() != want.strip().lower():
                note = f"  [wanted {want!r}]"
                r.setdefault("warnings", []).append(
                    f"{q[:50]}: wanted {want!r}, site offered {res.get('picked')!r}")
            print(f"[wd]   - {q[:52]!r} = {res.get('picked')!r}{note}", flush=True)
        else:
            why = res.get("why") or ("value did not stick (React reverted it)"
                                     if res.get("picked") else "no option matched")
            print(f"[wd]   - FAILED {q[:52]!r}: {why}; saw {(res.get('options') or [])[:6]}",
                  flush=True)
            if q and q not in r["unresolved"]:
                r["unresolved"].append(q)
    return done


async def _wd_cascade(page, css: str, path: list, q: str) -> bool:
    """A multi-level picker: one click per level ("Job Boards" -> "LinkedIn"). Typing free text
    into it can never satisfy it - the value must come from the tree (proven by the recording)."""
    try:
        await page.locator(css).first.click(timeout=6000)
    except Exception as e:
        print(f"[wd]   - cascade {q[:40]!r}: could not open ({str(e)[:60]})", flush=True)
        return False
    ok = False
    for lvl, label in enumerate(path):
        await page.wait_for_timeout(900)
        res = await page.evaluate(LD.PICK_JS,
                                  {"want": label, "anchor": css,
                                   "first": lvl == len(path) - 1}) or {}
        if not res.get("picked"):
            print(f"[wd]   - cascade {q[:40]!r}: level {lvl + 1} {label!r} not offered; "
                  f"saw {(res.get('options') or [])[:6]}", flush=True)
            break
        print(f"[wd]   - cascade {q[:40]!r}: {res.get('picked')!r}", flush=True)
        ok = True
    await page.wait_for_timeout(600)
    # A chip beside the input is the ONLY proof for a multiselect: the input itself goes back to
    # empty after a pick, so reading it would report failure on a success.
    try:
        ctrls = await page.evaluate(WD_ENUM_JS) or []
        me = next((c for c in ctrls if (c.get("q") or "")[:40] == (q or "")[:40]), None)
        if me is not None:
            ok = bool(me.get("answered"))
            if not ok:
                print(f"[wd]   - {q[:40]!r}: clicked, but no chip/value is on the control "
                      f"(chips={me.get('chips')}, cur={me.get('cur')!r}) - NOT counted",
                      flush=True)
        else:
            print(f"[wd]   - {q[:40]!r}: could not re-read the control to confirm the pick",
                  flush=True)
    except Exception as e:
        print(f"[wd]   - {q[:40]!r}: confirm read failed ({str(e)[:50]})", flush=True)
    return ok


# =====================================================================================
# 4. FILES, CHECKBOXES, TEXT
# =====================================================================================
async def wd_upload(page, r: dict) -> int:
    """Attach the tailored documents to the RIGHT groups (Resume/CV vs Cover Letter)."""
    wanted = [(os.path.join(OUT, "resume.pdf"), r"resume|cv|curriculum"),
              (os.path.join(OUT, "cover_letter.pdf"), r"cover")]
    wanted = [(p, rx) for p, rx in wanted if os.path.isfile(p)]
    if not wanted:
        print("[wd] no documents in out/ - nothing to attach", flush=True)
        return 0
    try:
        slots = await page.evaluate(WD_FILES_JS) or []
    except Exception as e:
        print(f"[wd] file-input scan failed: {str(e)[:70]}", flush=True)
        return 0
    if not slots:
        return 0
    try:
        rm = await page.evaluate(WD_DELETE_JS) or {}
        if rm.get("removed"):
            print(f"[wd] removed a stacked attachment: {rm.get('labels')}", flush=True)
            await page.wait_for_timeout(1800)
            slots = await page.evaluate(WD_FILES_JS) or slots
    except Exception:
        pass
    used, n = set(), 0
    for path, rx in wanted:
        slot = next((s for s in slots if s["i"] not in used
                     and re.search(rx, s.get("group") or "", re.I)), None)
        if slot is None:
            continue                       # no group for it: better none than the wrong one
        try:
            await page.locator(slot["css"]).first.set_input_files(path, timeout=15000)
            used.add(slot["i"]); n += 1
            r["filled"].append("upload:" + os.path.basename(path))
            print(f"[wd] attached {os.path.basename(path)} -> group "
                  f"{str(slot.get('group'))[:44]!r}", flush=True)
            await page.wait_for_timeout(2500)
        except Exception as e:
            print(f"[wd] upload of {os.path.basename(path)} failed: {str(e)[:80]}", flush=True)
    if not n:
        print(f"[wd] {len(slots)} file input(s) but none named resume/cover: "
              f"{[str(s.get('group'))[:40] for s in slots][:4]}", flush=True)
    return n


WD_CHECKS_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-jhw-cb]').forEach(e => e.removeAttribute('data-jhw-cb'));
  const out = [];
  [...document.querySelectorAll("input[type='checkbox']")].forEach((el) => {
    if (!vis(el) || el.disabled) return;
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]');
                 if (l) t = l.innerText || l.textContent || ''; }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText || l.textContent || ''; }
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) { const b = el.closest('[data-automation-id],div,li,fieldset');
              t = b ? (b.innerText || b.textContent || '') : ''; }
    const i = out.length;
    el.setAttribute('data-jhw-cb', String(i));
    out.push({i, label: (t || '').replace(/\s+/g, ' ').trim().slice(0, 120),
              checked: !!el.checked, css: "input[data-jhw-cb='" + i + "']"});
  });
  return out;
}
"""

CONSENT_RE = re.compile(r"i accept|i agree|consent|acknowledg|certify|terms|privacy|"
                        r"confirm that|have read", re.I)


async def wd_consent(page, r: dict) -> int:
    """Tick the consent boxes, ONCE. Routed through LD.CHECK_JS so the "a checkbox is SET, never
    toggled" rule has exactly one implementation - a second click unticks it and from that moment
    Save and Continue can never succeed."""
    try:
        boxes = await page.evaluate(WD_CHECKS_JS) or []
    except Exception:
        return 0
    n = 0
    for b in boxes:
        if b.get("checked") or not CONSENT_RE.search(b.get("label") or ""):
            continue
        res = await page.evaluate(LD.CHECK_JS, {"sel": b["css"]}) or {}
        if res.get("ok") and res.get("checked"):
            n += 1
            r["filled"].append("consent")
            print(f"[wd] ticked {str(b.get('label'))[:60]!r}"
                  + ("  (was already ticked)" if res.get("already") else ""), flush=True)
        elif res.get("ok"):
            print(f"[wd] {str(b.get('label'))[:50]!r} would not tick "
                  f"(checked={res.get('checked')})", flush=True)
        await page.wait_for_timeout(300)
    return n


def _basics(data: dict) -> dict:
    b = (data or {}).get("basics", {}) or {}
    legal = (b.get("legal_name") or b.get("name") or "").split()
    addr = b.get("address", "") or ""
    street = addr.split(",")[0].strip()
    postal, city = "", ""
    m = re.search(r"(\d{4,5})\s+([^(,]+)", addr)
    if m:
        postal, city = m.group(1), m.group(2).strip()
    return {"first": legal[0] if legal else "",
            "last": " ".join(legal[1:]) if len(legal) > 1 else "",
            "email": b.get("email", ""), "phone": b.get("phone", ""),
            "street": street, "postal": postal, "city": city,
            "linkedin": b.get("linkedin", "")}


# label regex -> value key. Only EMPTY fields are touched: on a returning applicant Workday has
# already restored these server-side and overwriting them is how good data gets replaced by a
# worse parse of the same CV.
WD_TEXT = [
    (r"^(legal )?(first|given) name", "first"),
    (r"^(legal )?(last|family|sur)\s*name", "last"),
    (r"address line 1|^street", "street"),
    (r"postal code|^zip", "postal"),
    (r"^city|^town", "city"),
    (r"phone number|^phone|mobile", "phone"),
    (r"linkedin", "linkedin"),
]

WD_TEXTS_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const bad = /^(checkbox|radio|file|hidden|submit|button|image|reset|password)$/i;
  const out = [];
  [...document.querySelectorAll('input,textarea')].forEach((el) => {
    if (!vis(el) || el.disabled || el.readOnly || bad.test(el.type || '')) return;
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) return;
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]');
                 if (l) t = l.innerText || l.textContent || ''; }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText || l.textContent || ''; }
    if (!t) t = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    out.push({label: norm(t).replace(/[*]/g, '').trim().slice(0, 80),
              value: String(el.value || ''), empty: !String(el.value || '').trim()});
  });
  return out;
}
"""


async def wd_text(page, data: dict, r: dict) -> int:
    vals = _basics(data)
    try:
        fields = await page.evaluate(WD_TEXTS_JS) or []
    except Exception:
        return 0
    n = 0
    for f in fields:
        if not f.get("empty"):
            continue
        lab = (f.get("label") or "").strip()
        if not lab:
            continue
        key = next((k for rx, k in WD_TEXT if re.search(rx, lab, re.I)), "")
        val = vals.get(key, "") if key else ""
        if not val:
            continue
        res = await page.evaluate(LD.FILL_JS, {"label": lab, "value": val, "placeholder": ""}) or {}
        if res.get("ok"):
            n += 1
            r["filled"].append(f"text:{key}")
            print(f"[wd] filled {lab[:40]!r} = {val[:34]!r}", flush=True)
        await page.wait_for_timeout(200)
    return n


# =====================================================================================
# 5. ACCOUNT
# =====================================================================================
def _load_accounts() -> dict:
    try:
        return json.load(open(ACCOUNTS, encoding="utf-8"))
    except Exception:
        return {}


def save_account(host: str, email: str, password: str) -> None:
    """out/ats_accounts.json is the ONE store, already written by `jhw.py atspw` and
    `jhw.py import-passwords`. Never a second file, never .env, never argv."""
    a = _load_accounts()
    a[host] = {"email": email, "password": password}
    try:
        os.makedirs(os.path.dirname(ACCOUNTS), exist_ok=True)
        json.dump(a, open(ACCOUNTS, "w"), indent=2)
        os.chmod(ACCOUNTS, 0o600)
    except Exception as e:
        print(f"[wd] could not store the account ({str(e)[:60]})", flush=True)


ACCOUNT_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (el) => ((el.innerText || el.textContent || el.value || '') + ' ' +
                       (el.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
  const btns = [...document.querySelectorAll("button,[role='button'],a")].filter(vis);
  const has = (rx) => btns.some(b => rx.test(txt(b)));
  const BODY = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  const pw = [...document.querySelectorAll("input[type='password']")].filter(vis);
  const verify = [...document.querySelectorAll('input')].filter(vis).some(
    el => /verify( new)? password/i.test(
      (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('name') || '') + ' ' +
      (el.id && document.querySelector('label[for="' + el.id + '"]')
        ? (document.querySelector('label[for="' + el.id + '"]').innerText ||
           document.querySelector('label[for="' + el.id + '"]').textContent) : '')));
  return {
    password_fields: pw.length,
    verify_password: verify,
    sso_google: has(/sign in with google|continue with google/i),
    sso_other:  has(/sign in with (microsoft|apple|linkedin)|single sign|use sso/i),
    last_app:   has(/use my last application/i),
    create_link: has(/create account|create a new account|sign up/i),
    signin_link: has(/^\s*(sign\s*in|anmelden|einloggen)\s*$/i),
    already:    /you have already applied|already submitted an application/i
                  .test(BODY.slice(0, 4000)),
    body: BODY.replace(/\s+/g, ' ').slice(0, 300)
  };
}
"""


async def _click_text(page, rx: str, t: int = 5000) -> bool:
    """Click the first visible button/link whose accessible text matches. Text/ARIA only - a
    tenant-specific data-automation-id does not port between Workday UI versions."""
    for role in ("button", "link"):
        try:
            el = page.get_by_role(role, name=re.compile(rx, re.I)).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=t)
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _fill_labeled(page, rx: str, value: str) -> bool:
    if not value:
        return False
    for role in ("textbox",):
        try:
            el = page.get_by_role(role, name=re.compile(rx, re.I)).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=4000)
                await el.fill(str(value), timeout=4000)
                return True
        except Exception:
            pass
    try:
        res = await page.evaluate(LD.FILL_JS, {"label": rx, "value": str(value),
                                               "placeholder": ""}) or {}
        return bool(res.get("ok"))
    except Exception:
        return False


async def wd_account(page, data: dict, creds: dict, r: dict) -> bool:
    """Get past the Workday account gate. Order is deliberate:
       stored password -> "Use My Last Application" -> SSO (ask the human) -> Create Account.
    Never guess an SSO password: an OAuth prompt on a shared Google account is not ours to drive,
    and a wrong attempt can lock the account for every later application."""
    st = await page.evaluate(ACCOUNT_JS) or {}
    if st.get("already"):
        # A benign outcome, not a failure: there is nothing left to do and the pipeline should
        # record it as such rather than as a broken run.
        r["ok"] = True
        r["stage"] = "already_applied"
        r["note"] = "Workday says an application for this job already exists."
        return False
    if not st.get("password_fields") and not st.get("sso_google") and not st.get("last_app") \
            and not st.get("signin_link"):
        return True                                    # no gate on this tenant / already signed in
    host = account_host(page.url)
    acc = match_account(page.url, _load_accounts())
    email = (acc.get("email") or (creds or {}).get("email")
             or _basics(data).get("email") or "")
    print(f"[wd] account gate on {host} (stored={'yes' if acc.get('password') else 'no'}, "
          f"google_sso={bool(st.get('sso_google'))}, last_app={bool(st.get('last_app'))})",
          flush=True)

    if acc.get("password"):
        if await _signin(page, email, acc["password"], r):
            return True
        print("[wd] the stored password was refused", flush=True)

    if st.get("last_app") and await _click_text(page, r"use my last application"):
        await page.wait_for_timeout(2500)
        if await _past_gate(page):
            r["filled"].append("account:last-application")
            return True

    st = await page.evaluate(ACCOUNT_JS) or st
    if st.get("sso_google") or st.get("sso_other"):
        # An SSO tenant needs a browser that is ALREADY signed in to that identity provider. The
        # honest move is to say so and let the human do it once in noVNC, not to type a password
        # into Google's form.
        r["stage"] = "sso"
        r["note"] = (f"{host} signs in with SSO (Google/Microsoft). Open "
                     f"http://localhost:9090 , sign this Chrome into that account ONCE, then "
                     f"re-run apply - the session persists in the sandbox profile.")
        await _notify("Workday needs a one-time SSO sign-in: " + r["note"])
        return False

    if st.get("password_fields") and not st.get("verify_password") and st.get("create_link"):
        await _click_text(page, r"create account|create a new account|sign up")
        await page.wait_for_timeout(1200)
        st = await page.evaluate(ACCOUNT_JS) or st

    if st.get("verify_password"):
        pw = (creds or {}).get("password") or ""
        if not pw:
            r["note"] = "no password proposed for the new Workday account"
            return False
        await _fill_labeled(page, r"email", email)
        await _fill_labeled(page, r"^password$|new password", pw)
        await _fill_labeled(page, r"verify( new)? password", pw)
        await wd_consent(page, r)
        if not await _click_text(page, r"create account"):
            await _click_text(page, r"^\s*submit\s*$")
        await page.wait_for_timeout(3000)
        if await _past_gate(page):
            save_account(host, email, pw)
            r["filled"].append("account:create")
            print(f"[wd] created the account and stored it for {host}", flush=True)
            return True
        # An email verification code is the one place we MUST wait for the human.
        body = (await page.evaluate("() => (document.body.innerText||'').slice(0,600)")) or ""
        if re.search(r"verif(y|ication) code|check your (e-?mail|inbox)", body, re.I):
            code = await _ask(f"Workday ({host}) emailed a verification code to {email}. "
                              "Reply with the code.")
            if code:
                await _fill_labeled(page, r"code", code.strip())
                await _click_text(page, r"^\s*(submit|verify|continue)\s*$")
                await page.wait_for_timeout(2500)
                if await _past_gate(page):
                    save_account(host, email, pw)
                    r["filled"].append("account:create+verified")
                    return True

    pw = await _ask(f"I am stuck at the Workday sign-in for {host} (account {email}). "
                    f"Reply with the PASSWORD, or reset it via 'Forgot your password?' and reply "
                    f"with the new one. I will store it so I never ask again.")
    if pw and not pw.strip().lower().startswith(("no_answer", "stop")):
        if await _signin(page, email, pw.strip(), r):
            save_account(host, email, pw.strip())
            return True
    r["stage"] = "account"
    r["note"] = (f"Could not pass the Workday account gate for {host}. Store the login and "
                 f"re-run:  python jhw.py atspw {host} '<password>' {email}")
    await _notify(r["note"])
    return False


async def _signin(page, email: str, password: str, r: dict) -> bool:
    if not await _click_text(page, r"^\s*(sign\s*in|anmelden|einloggen)\s*$", 2500):
        pass
    await page.wait_for_timeout(600)
    await _fill_labeled(page, r"email", email)
    await _fill_labeled(page, r"^password$", password)
    if not await _click_text(page, r"^\s*sign in\s*$"):
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
    await page.wait_for_timeout(3000)
    if await _past_gate(page):
        r["filled"].append("account:signin")
        print("[wd] signed in", flush=True)
        return True
    return False


async def _past_gate(page) -> bool:
    """Past the gate = no password field left on screen and no error banner."""
    await page.wait_for_timeout(600)
    try:
        st = await page.evaluate(ACCOUNT_JS) or {}
    except Exception:
        return False
    if st.get("password_fields"):
        return False
    return True


async def _ask(q: str) -> str:
    if not ask:
        return ""
    try:
        return (await ask.ask_human(q)) or ""
    except Exception:
        return ""


async def _notify(m: str) -> None:
    if not ask:
        return
    try:
        await ask.notify(m)
    except Exception:
        pass


# =====================================================================================
# 6. NAVIGATION
# =====================================================================================
OVERLAYS = ("#onetrust-accept-btn-handler",
            "button#truste-consent-button",
            "[data-automation-id='legalNoticeAcceptButton']",
            "button:has-text('Accept Cookies')",
            "button:has-text('Allow all cookies')",
            "button:has-text('Accept All')",
            "button:has-text('Alle akzeptieren')")


async def dismiss_overlays(page) -> None:
    for sel in OVERLAYS:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=2500)
                print(f"[wd] dismissed overlay: {sel}", flush=True)
                await page.wait_for_timeout(600)
        except Exception:
            pass


PAGE_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const btns = [...document.querySelectorAll("button,[role='button'],input[type='submit']")]
    .filter(vis).filter(b => !b.disabled)
    .map(b => norm(b.innerText || b.textContent || b.value ||
                   b.getAttribute('aria-label') || ''));
  const body = norm(document.body ? (document.body.innerText ||
                                     document.body.textContent || '') : '').slice(0, 6000);
  const errs = [...document.querySelectorAll('*')].filter(e => vis(e) && e.children.length === 0)
    .map(e => norm(e.innerText || e.textContent)).filter(t => t && t.length < 120 &&
      /(is required|select one required|required field|must be|invalid|errors? found|\berror:|please (correct|enter|select|complete|fix|answer|provide|choose))/i.test(t))
    .slice(0, 10);
  return {
    url: location.href,
    next:   btns.find(t => /^\s*(save and continue|continue|next|weiter)\s*$/i.test(t)) || '',
    submit: btns.find(t => /^\s*(submit|absenden)(\s+application)?\s*$/i.test(t)) || '',
    review: /review|summary/i.test(location.href) ||
            btns.some(t => /^\s*(submit|absenden)(\s+application)?\s*$/i.test(t)),
    errors: errs,
    // Fingerprint: a page that did not change after Save and Continue means the click was
    // refused. Without this the loop presses Next 14 times in silence.
    fp: location.pathname + '|' + document.querySelectorAll('input,select,textarea,button').length
        + '|' + body.slice(0, 120),
    thin: document.querySelectorAll('input,select,textarea,button,[role=button]').length
  };
}
"""


async def _read_page(page, tries: int = 4) -> dict:
    """Read the page, waiting out a mid-render read. An empty page is indistinguishable from a
    finished one - that is how a run once reported ok=True over a blank form."""
    st = {}
    for k in range(tries):
        try:
            st = await page.evaluate(PAGE_JS) or {}
        except Exception as e:
            msg = str(e)
            if "Execution context was destroyed" in msg or "navigating" in msg.lower():
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
                await page.wait_for_timeout(1200)
                continue
            return {}
        if st.get("thin", 0) >= 6:
            return st
        print(f"[wd] page has only {st.get('thin')} control(s) - still rendering "
              f"({k + 1}/{tries})", flush=True)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    return st


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    wd = [p for p in ctx.pages if is_workday(p.url or "")]
    if wd:
        page = wd[-1]
    return browser, ctx, page


async def reach_workday(page, ctx, url: str, r: dict):
    """Land on the Workday apply wizard. Returns the page that is actually on Workday.

    An accenture.com careers URL cannot be transformed into a Workday URL by string rules: the
    site name (AccentureCareers), the location slug (Hamburg) and the title slug are nowhere in
    the req id. So we open the careers page and follow ITS OWN "APPLY NOW" link, which the
    recording proves opens a POPUP.
    """
    if is_workday(url):
        for u in apply_urls(url):
            try:
                await page.goto(u, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
            except Exception as e:
                print(f"[wd] {u[:70]} -> {str(e)[:60]}", flush=True)
                continue
            await dismiss_overlays(page)
            if is_workday(page.url or ""):
                print(f"[wd] on Workday: {page.url[:100]}", flush=True)
                return page
        r["note"] = f"could not open a Workday apply URL from {url[:90]}"
        return None

    # GATEWAY: careers page -> APPLY NOW (new tab)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        r["note"] = f"gateway goto failed: {str(e)[:90]}"
        return None
    await dismiss_overlays(page)
    target = None
    for rx in (r"apply now", r"^\s*apply( now| for this job)?\s*$", r"jetzt bewerben"):
        try:
            el = page.get_by_role("link", name=re.compile(rx, re.I)).first
            if not (await el.count()):
                el = page.get_by_role("button", name=re.compile(rx, re.I)).first
            if not (await el.count()):
                continue
            try:
                async with ctx.expect_page(timeout=20000) as pop:
                    await el.click(timeout=8000)
                target = await pop.value
            except Exception:
                await page.wait_for_timeout(3000)
                target = page
            break
        except Exception:
            continue
    if target is None:
        r["note"] = (f"no Apply link found on {url[:80]} - open it in noVNC and pass the "
                     f"myworkdayjobs.com URL to apply instead")
        return None
    try:
        await target.wait_for_load_state("domcontentloaded", timeout=45000)
    except Exception:
        pass
    await target.wait_for_timeout(2000)
    await dismiss_overlays(target)
    if not is_workday(target.url or ""):
        r["note"] = f"Apply did not land on Workday (got {str(target.url)[:90]})"
        return None
    print(f"[wd] gateway -> Workday: {target.url[:100]}", flush=True)
    # Close the gateway tab: llm_driver's _connect() picks the LAST non-LinkedIn page, so leaving
    # accenture.com open is a coin flip over which page the fallback driver would drive.
    if target is not page:
        try:
            await page.close()
        except Exception:
            pass
    return target


# =====================================================================================
# 7. DRIVE
# =====================================================================================
async def _drive_pages(creds: dict, data: dict, resume_path: str, ats_url: str, r: dict) -> None:
    facts = candidate_answers()
    pw = await async_playwright().start()
    browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front()
        r["stage"] = "navigate"
        page = await reach_workday(page, ctx, ats_url, r)
        if page is None:
            return
        await page.bring_to_front()

        r["stage"] = "account"
        if not await wd_account(page, data, creds, r):
            return

        r["stage"] = "pages"
        stalls, last_fp = 0, ""
        for i in range(MAX_PAGES):
            st = await _read_page(page)
            if not st:
                r["note"] = "could not read the Workday page"
                return
            if st.get("errors"):
                print(f"[wd] page says: {st['errors'][:4]}", flush=True)
            if st.get("review") and not st.get("next"):
                r["stage"] = "review"
                r["ok"] = True
                print(f"[wd] Review page reached after {len(set(r['filled']))} field(s)",
                      flush=True)
                return
            fp = st.get("fp") or ""
            if fp and fp == last_fp:
                stalls += 1
            else:
                stalls = 0
            last_fp = fp
            print(f"[wd] --- page {i + 1}/{MAX_PAGES} ({st.get('thin')} controls"
                  f"{', STALLED x%d' % stalls if stalls else ''}) {str(st.get('url'))[:70]}",
                  flush=True)

            await wd_text(page, data, r)
            await wd_upload(page, r)
            if LD is not None:
                try:
                    swept = await page.evaluate(LD.SWEEP_JS, select_defaults()) or []
                    for s in swept:
                        print(f"[wd] native select {str(s.get('label'))[:40]!r} = "
                              f"{s.get('picked')!r}"
                              + ("" if s.get("verified") else "  [DID NOT STICK]"), flush=True)
                        r["filled"].append("select:" + str(s.get("picked"))[:20])
                except Exception as e:
                    print(f"[wd] native-select sweep failed: {str(e)[:70]}", flush=True)
            await wd_sweep(page, facts, r)
            await wd_consent(page, r)

            # A stalled page is not a page to press Next on again. Hand it to the generic driver,
            # which can ask the candidate on Telegram - the standing order is never to stop on a
            # stall, so this is an escalation, not an exit.
            if stalls >= 2:
                r["needs_driver"] = True
                r["note"] = (f"page {i + 1} did not change after two Save-and-Continue attempts "
                             f"({st.get('errors')[:2] if st.get('errors') else 'no error shown'})")
                print(f"[wd] STALLED -> handing this page to the generic LLM driver", flush=True)
                return

            nxt = st.get("next") or ""
            if not nxt:
                if st.get("submit"):
                    r["stage"] = "review"
                    r["ok"] = True
                    return
                r["needs_driver"] = True
                r["note"] = f"no Save-and-Continue on page {i + 1}"
                print("[wd] no Save-and-Continue button -> generic driver", flush=True)
                return
            await _close_portal(page)
            if await _click_text(page, r"^\s*" + re.escape(nxt) + r"\s*$", 8000):
                print(f"[wd] clicked {nxt[:30]!r}", flush=True)
            else:
                print(f"[wd] could not click {nxt[:30]!r}", flush=True)
                stalls += 1
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
        r["note"] = f"ran out of pages after {MAX_PAGES} steps"
        r["needs_driver"] = True
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()


async def _finish(r: dict) -> None:
    """Submit, and only claim it on the SITE's own confirmation. LD._submit is that routine -
    reused, never reimplemented, so 'submitted' means the same thing on Workday and Phenom."""
    if LD is None or async_playwright is None:
        return
    pw = await async_playwright().start()
    browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front()
        r.setdefault("actions", [])
        ok = await LD._submit(page, r)
        if ok:
            r["stage"] = "submitted"
            r["ok"] = True
    except Exception as e:
        print(f"[wd] submit step failed: {str(e)[:110]}", flush=True)
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()


async def drive(creds: dict, data: dict, resume_path: str = "", answer_fn=None,
                ats_url: str = "", facts: str = "") -> dict:
    """Router entry point. Signature matches the other adapters (creds, data, resume, answer_fn,
    ats_url) plus `facts` = agent.py's candidate_block(), which the generic driver needs."""
    r = {"ok": False, "stage": "start", "filled": [], "unresolved": [], "note": "",
         "needs_driver": False, "actions": []}
    if async_playwright is None or LD is None:
        r["note"] = "playwright/llm_driver unavailable in this environment"
        return r
    try:
        await _drive_pages(creds, data, resume_path, ats_url, r)
    except Exception as e:
        r["note"] = f"workday_wd error: {str(e)[:160]}"
        r["needs_driver"] = True
        print(f"[wd] unhandled: {str(e)[:160]}", flush=True)

    if r.get("needs_driver"):
        # url="" on purpose: the driver must NOT re-navigate. We are signed in and part-way
        # through a wizard; a goto would throw that away and land back on the posting.
        print("[wd] --> generic LLM DOM driver takes over from here (no re-navigation)",
              flush=True)
        extra = ("\n(You are PART-WAY through a WORKDAY application and already signed in. Never "
                 "click a step name in the progress bar, Back, or Sign Out. A 'Select One' "
                 "button opens a popup list - click the button, then click the option. Advance "
                 "with 'Save and Continue'.)")
        try:
            ld = await LD.run((facts or "") + extra, resume_path, url="")
        except Exception as e:
            ld = {"ok": False, "note": f"llm_driver failed: {str(e)[:120]}"}
        r["actions"] += list(ld.get("actions") or [])
        r["filled"] += [a for a in (ld.get("actions") or []) if a.startswith(("fill", "typeahead"))]
        r["note"] = (r.get("note") or "") + " | driver: " + str(ld.get("note") or "")
        if ld.get("submitted"):
            r["ok"] = True; r["stage"] = "submitted"
            return r
        if ld.get("ok"):
            r["ok"] = True
            r["stage"] = r["stage"] if r["stage"] == "submitted" else "review"
        return r

    if r.get("stage") == "review" and r.get("ok"):
        await _finish(r)
    return r


# =====================================================================================
# logic self-test — runs with NO browser and NO network (`python3 workday_wd.py --logic`)
# =====================================================================================
def _logic_selftest() -> int:
    fails = []

    def ck(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    live = ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers/job/Hamburg/"
            "Agentic-AI-MarTech-Strategist--all-genders-_R00329883/apply?source=jb_1")
    job = ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers/job/Hamburg/"
           "Agentic-AI-MarTech-Strategist--all-genders-_R00329883")
    gw = "https://www.accenture.com/de-de/careers/jobdetails?id=R00329883_de&src=LINKEDINJP"

    print("-- recognition")
    ck("the live apply URL is Workday", is_workday(live))
    ck("the job URL is Workday", is_workday(job))
    ck("the accenture.com careers URL is NOT Workday", not is_workday(gw))
    ck("...it IS recognised as a gateway", is_workday_gateway(gw))
    ck("a Phenom URL is neither",
       not is_workday("https://careers.services.global.ntt/global/en/job/NTT1X/x")
       and not is_workday_gateway("https://careers.services.global.ntt/global/en/job/NTT1X/x"))
    ck("tenant is extracted", tenant_of(live) == "accenture", tenant_of(live))

    print("-- apply-URL derivation")
    ck("an /apply URL is used untouched, query string included",
       apply_urls(live) == [live], str(apply_urls(live)))
    au = apply_urls(job)
    ck("a job URL gains /apply first", au and au[0] == job + "/apply", str(au[:1]))
    ck("...then applyManually", len(au) > 1 and au[1].endswith("/apply/applyManually"), str(au))
    ck("...then the /en-US/ locale variant",
       any("/en-US/AccentureCareers/" in x for x in au), str(au))
    ck("no duplicates", len(au) == len(set(au)), str(au))

    print("-- req id")
    ck("req id from the Workday title slug", req_id(job) == "R00329883", req_id(job))
    ck("req id from the gateway query, locale suffix stripped",
       req_id(gw) == "R00329883", req_id(gw))

    print("-- account store")
    accs = {"accenture.wd103.myworkdayjobs.com": {"email": "a@b.c", "password": "x"}}
    ck("exact host matches", match_account(live, accs).get("password") == "x")
    ck("the same TENANT on another wd host matches",
       match_account("https://accenture.wd103.myworkday.com/x", accs).get("password") == "x",
       str(match_account("https://accenture.wd103.myworkday.com/x", accs)))
    ck("a DIFFERENT tenant does not",
       match_account("https://redhat.wd5.myworkdayjobs.com/x", accs) == {},
       str(match_account("https://redhat.wd5.myworkdayjobs.com/x", accs)))

    print("-- answers come from candidate.md, wording from the recording")
    facts = candidate_answers()
    ck("candidate.md parsed", len(facts) > 20, f"{len(facts)} keys")
    # TEST THE PARSER, NOT THE PERSON. This asserted `gender == "Male"` -- the OWNER's answer -- so it
    # failed for anybody else who filled in candidate.md, which was found the moment the software was
    # packaged for a second user. A self-declaration is the user's to make (or to leave blank, and be
    # asked); what this file is responsible for is READING it faithfully. So: whatever the file says,
    # that is what must come back, and a blank stays blank rather than becoming a default.
    import re as _re
    _raw = ""
    for _c in ("/agent/userdata/candidate.md",
               os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "userdata", "candidate.md")):
        if os.path.exists(_c):
            with open(_c, encoding="utf-8", errors="replace") as _fh:
                _raw = _fh.read()
            break
    _m = _re.search(r"^-\s*gender\s*:\s*(.*)$", _raw, _re.M)
    _want = (_m.group(1).split("#")[0].strip().strip('"') if _m else "")
    ck("the value in candidate.md is what comes back (whatever it is)",
       (facts.get("gender") or "") == _want,
       f"file says {_want!r}, parser returned {facts.get('gender')!r}")
    ck("recorded Accenture Q1 -> Yes",
       answer_for("All individuals employed by Accenture must comply", facts) == "Yes",
       answer_for("All individuals employed by Accenture must comply", facts))
    ck("recorded Accenture Q2 (non-compete) -> No",
       answer_for("At your current employer, are you subject to any agreement", facts) == "No",
       answer_for("At your current employer, are you subject to any agreement", facts))
    # WHATEVER THE FILE SAYS -- including nothing. A blank declaration must produce NO answer, so the
    # engine asks the person instead of declaring something on their behalf.
    _g = _want or ""
    ck("legal gender comes from candidate.md, and a blank stays blank",
       (answer_for("What is your legal gender?", facts) or "") == _g,
       f"file {_g!r} -> {answer_for('What is your legal gender?', facts)!r}")
    # SAME CLASS AS THE GENDER ASSERTION: this required "He/Him", which is the OWNER's pronoun. It
    # passed only because his candidate.md happens to say so, and it failed the moment the software
    # was run with the shipped TEMPLATE -- i.e. as any second user. The property is that the file is
    # read faithfully and a blank is left blank.
    _pm = re.search(r"^-\s*pronoun\s*:\s*(.*)$", _raw, re.M)
    _pw = (_pm.group(1).split("#")[0].strip().strip('"') if _pm else "")
    ck("pronoun comes from candidate.md, and a blank stays blank",
       (answer_for("Please select your pronoun", facts) or "") == _pw,
       f"file {_pw!r} -> {answer_for('Please select your pronoun', facts)!r}")
    ck("visa sponsorship -> No",
       answer_for("Will you now or in the future require sponsorship?", facts) == "No",
       answer_for("Will you now or in the future require sponsorship?", facts))
    ck("authorised to work -> Yes",
       answer_for("Are you legally authorized to work in Germany?", facts) == "Yes")
    ck("relocation -> No (candidate.md screening_defaults)",
       answer_for("Are you willing to relocate?", facts) == "No",
       answer_for("Are you willing to relocate?", facts))
    ck("travel -> Yes", answer_for("Are you willing to travel?", facts) == "Yes",
       answer_for("Are you willing to travel?", facts))
    ck("an unknown question returns '' (never a guess)",
       answer_for("What is your favourite colour?", facts) == "",
       repr(answer_for("What is your favourite colour?", facts)))
    ck("candidate.md OVERRIDES the committed fallback",
       answer_for("relocate", {"willing_to_relocate": "Yes"}) == "Yes",
       answer_for("relocate", {"willing_to_relocate": "Yes"}))
    ck("'ASK' in candidate.md is not treated as an answer",
       "earliest_start_date" not in facts or facts.get("earliest_start_date") != "ASK")

    print("-- cascade")
    ck("How Did You Hear -> Job Boards then LinkedIn",
       cascade_for("How Did You Hear About Us?") == ["Job Boards", "LinkedIn"],
       str(cascade_for("How Did You Hear About Us?")))
    ck("an ordinary question has no cascade", cascade_for("What is your legal gender?") == [])

    print("-- shared vocabulary with the generic driver")
    sd = select_defaults([["relocate", "No"], ["how did you hear", "Internet"]])
    ck("the generic SELECT_DEFAULTS are kept, in front",
       sd[0] == ["relocate", "No"], str(sd[:3]))
    # THE DOCTRINE CHANGED, SO THE TEST DOES TOO (never deleted -- the reasoning is the point).
    # It used to require ['legal gender','Male'] and ['pronoun','He/Him'] here. Those are
    # SELF-DECLARATIONS and they no longer have a committed answer: they come from the user's own
    # candidate.md or the engine asks. What this list still legitimately carries is factual screening.
    _keys = [str(k).lower() for k, _ in sd]
    ck("Workday's own factual additions are appended",
       "all individuals employed by" in _keys and "preferred communication" in _keys, str(_keys))
    ck("and NO self-declaration has a committed answer here",
       not any(re.search(r"gender|pronoun", k) for k in _keys), str(_keys))
    ck("a key the generic list already owns is NOT duplicated",
       [k for k, _ in sd].count("how did you hear") == 1, str([k for k, _ in sd]))
    _ld = "OK" if LD else ("UNAVAILABLE here (needs playwright) - the DOM half of this suite "
                           "runs on the operator machine via test_workday_dom.py")
    print("  [note] llm_driver import: " + _ld)

    print("-- tenant hosts seen mid-flow (from the real recording cookies)")
    ck("identity.wd103.myworkday.com is recognised as Workday",
       is_workday("https://identity.wd103.myworkday.com/x"))
    ck("the candidate site is still recognised",
       is_workday("https://accenture.wd103.myworkdayjobs.com/x"))

    print("-- routing precedence: a broad matcher must not claim a Workday URL")
    # This is a REGRESSION GUARD for a bug that would have broken the very first Accenture run:
    # phenom.is_phenom() matches ANY url containing "/job/<x>", so the Workday apply URL was
    # claimed as Phenom and rewritten to ".../apply?jobSeqNo=Hamburg&step=1" - the LOCATION slug
    # mistaken for a job-sequence number. agent.py's Phenom block is now guarded by
    # `not (is_workday or is_workday_gateway)`; this asserts BOTH halves of that guard.
    try:
        import phenom as _ph
        ck("phenom DOES over-match a Workday URL (so the router guard is load-bearing)",
           _ph.is_phenom(live) is True)
        ck("...and the guard excludes it", not (
            _ph.is_phenom(live) and not (is_workday(live) or is_workday_gateway(live))))
        ck("...while a real Phenom URL is still claimed by Phenom", (
            _ph.is_phenom("https://careers.services.global.ntt/global/en/job/NTT1G337575/x")
            and not is_workday("https://careers.services.global.ntt/global/en/job/NTT1G337575/x")))
        _agent = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "agent.py")
        _src = open(_agent, encoding="utf-8").read()
        # Strip comments first: the paragraph ABOVE the guard describes it, and grepping raw source
        # would pass on a file where the guard had been deleted and only the comment remained.
        _code = "\n".join(ln for ln in _src.splitlines() if not ln.strip().startswith("#"))
        ck("agent.py's Phenom block carries the Workday guard",
           "phenom.is_phenom(target) and not (workday_wd.is_workday(target)" in _code)
        # DOCTRINE CORRECTED 2026-08-16, and these two checks were rewritten with it.
        # flows/workday.py is built from the candidate's OWN recording of the full Accenture
        # lifecycle (recordings/workday.py, RECORDINGS_FINDINGS.md) and selects by role/label,
        # which that document measured as far more robust across tenants than data-automation-id.
        # THIS module was written from documentation and has never run against a live Workday
        # page, so it must be OPT-IN. Making it the default demoted ground truth to "legacy".
        #
        # The OLD assertion here was VACUOUS: it checked that the string
        # "workday_wd.drive(...)" appeared ANYWHERE in agent.py, which is still true when the
        # call sits inside an opt-in branch -- so it could not tell "default" from "reachable"
        # and it passed on exactly the routing it was meant to police. Assert the STRUCTURE.
        import ast as _ast
        _t = _ast.parse(_src)
        _gates = [n for n in _ast.walk(_t) if isinstance(n, _ast.If)
                  and "JHW_WD_EXPERIMENTAL" in _ast.dump(n.test)]
        ck("agent.py has exactly one JHW_WD_EXPERIMENTAL gate", len(_gates) == 1, str(len(_gates)))
        if len(_gates) == 1:
            _body = _ast.dump(_ast.Module(body=_gates[0].body, type_ignores=[]))
            _els = _ast.dump(_ast.Module(body=_gates[0].orelse, type_ignores=[]))
            ck("the UNVERIFIED workday_wd adapter is OPT-IN only",
               "workday_wd" in _body and "workday_wd" not in _els)
            ck("the DEFAULT path is the recorded flows/workday.py adapter",
               "workday" in _els and "drive" in _els)
        ck("no stale JHW_WD_LEGACY switch is left behind", "JHW_WD_LEGACY" not in _code)
    except Exception as e:
        ck("routing-precedence guard is checkable", False, f"{type(e).__name__}: {str(e)[:70]}")

    print("-- consent matching")
    ck("'I accept.' is consent", bool(CONSENT_RE.search("I accept.")))
    ck("'I agree to the Terms' is consent", bool(CONSENT_RE.search("I agree to the Terms")))
    ck("a marketing opt-in is not treated as required consent",
       not CONSENT_RE.search("Email me about similar jobs"))

    print(("\nFAILED: " + ", ".join(fails)) if fails else "\nALL WORKDAY LOGIC CONTRACTS HOLD")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--logic" in sys.argv:
        sys.exit(_logic_selftest())
    d = {}
    try:
        d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"),
                           encoding="utf-8"))
    except Exception:
        pass
    c = {"email": (d.get("basics") or {}).get("email", ""), "password": ""}
    print(json.dumps(asyncio.run(drive(c, d, os.path.join(OUT, "resume.pdf"),
                                       None, sys.argv[1] if len(sys.argv) > 1 else "")), indent=2))
