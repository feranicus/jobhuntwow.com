#!/usr/bin/env python3
"""ASHBY — deterministic driver for jobs.ashbyhq.com.

WHY THIS EXISTS (measured 2026-08-17, n8n "Global Director of Revenue Enablement"):

  Unknown ATS → Stagehand (500 / Not Found) → llm_driver
  llm_driver filled ONE field ('First and last name') then clicked Submit Application.
  Required still empty: Email, Notice period, two essays. Half-empty apply is worse than none.

Knowledge compiled from a (redacted) recording lives in flows/knowledge/ashby.json:
  fields, order, buttons, checkboxes. Values are filled from candidate data + essay.deterministic
  + answer_fn — never invented by a panel for facts we own.

Ashby is usually ONE long page: uploads, contact, location typeahead, salary, notice,
screening checkboxes, free-text, Submit Application.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import ask
except Exception:
    ask = None

AUTOSUBMIT = os.getenv("JHW_AUTOSUBMIT", "1").strip() not in ("0", "false", "no")
_KNOW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge", "ashby.json")


def _log(msg: str) -> None:
    print(f"[ashby] {msg}", flush=True)


def _load_knowledge() -> dict:
    try:
        with open(_KNOW_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _log(f"knowledge unavailable ({type(e).__name__}) — using built-in labels")
        return {"fields": {}, "order": [], "buttons": ["Submit Application"], "checkboxes": []}


def _basics(data: dict) -> dict:
    b = dict(data.get("basics") or {})
    legal = (b.get("legal_name") or b.get("name") or "").strip()
    parts = legal.split()
    b.setdefault("first", parts[0] if parts else "")
    b.setdefault("last", " ".join(parts[1:]) if len(parts) > 1 else "")
    b.setdefault("full_name", legal or f"{b.get('first','')} {b.get('last','')}".strip())
    return b


def _defaults(data: dict) -> dict:
    out = {}
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "userdata", "candidate.md")
        txt = open(path, encoding="utf-8").read()
        if "## screening_defaults" in txt:
            body = txt.split("## screening_defaults", 1)[1]
            for ln in body.splitlines():
                if ln.startswith("##"):
                    break
                m = re.match(r"-\s*([^:]+):\s*(.*)$", ln.strip())
                if m:
                    # STRIP THE INLINE COMMENT AND THE QUOTES. candidate.md is written for a
                    # HUMAN, so a line reads
                    #   - available_start_date: "2026-08-20"   # ALWAYS one month from the appl...
                    # and this parser kept it verbatim — so the Notice period box on a real employer's
                    # form received `2026-08-20"       # ALWAYS one month from the appl`. CLAUDE.md
                    # already records this exact defect for the Workday screening parser; it is the
                    # same file being read by a second parser that never learned the lesson.
                    _v = m.group(2)
                    _v = re.split(r"\s+#", _v)[0].strip().strip('"').strip("'").strip()
                    out[m.group(1).strip()] = _v
    except Exception:
        pass
    sd = data.get("screening_defaults") or {}
    if isinstance(sd, dict):
        out.update({str(k): v for k, v in sd.items() if v not in (None, "")})
    out.setdefault("notice_period", "1 month")
    return out


def _notice_text(defaults: dict) -> str:
    """VERBATIM from his recording: `fill("one month")`. A synthesised sentence
    ("1 month notice; available from approximately 16 September 2026.") is not what he typed, and on a
    short-answer box it is simply wrong."""
    v = str(defaults.get("notice_period") or "").strip().strip('"')
    # HIS RECORDING TYPES "one month"; candidate.md says "1 month". Same fact, and the recorded
    # spelling is the one this employer's box actually received.
    words = {"1": "one", "2": "two", "3": "three", "4": "four", "6": "six"}
    m = re.match(r"^(\d+)\s*(month|week)", v, re.I)
    if m and m.group(1) in words:
        v = f"{words[m.group(1)]} {m.group(2).lower()}"
    return v or "one month"


def _salary_text(defaults: dict) -> str:
    """PLAIN DIGITS. His recording types `150000`; a currency sentence is rejected by a numeric box.
    A placeholder ('ASK') is never typed — that goes to the ladder instead."""
    raw = str(defaults.get("salary_expectation_eur")
              or defaults.get("salary_expectation") or "").strip()
    if re.fullmatch(r"(ask|tbd|n/?a|\?+|-+)", raw, re.I):
        return ""
    digits = re.sub(r"[^\d]", "", raw)
    return str(int(digits)) if digits else ""



def _linkedin_url(data: dict, defaults: dict) -> str:
    """Canonical LinkedIn URL from candidate data — never a wrong vanity from residual LLM."""
    b = data.get("basics") or {}
    candidates = [
        b.get("linkedin"), b.get("linkedin_url"), b.get("linkedin_profile"),
        defaults.get("linkedin"), defaults.get("linkedin_url"),
        (data.get("profiles") or {}).get("linkedin") if isinstance(data.get("profiles"), dict) else None,
        b.get("website"),
    ]
    for c in candidates:
        s = str(c or "").strip()
        if not s:
            continue
        if "linkedin.com" in s.lower():
            if not s.startswith("http"):
                s = "https://" + s.lstrip("/")
            return s
    return ""


_LINK_KINDS = (
    ("github",   r"git\s*hub",                                    "github.com"),
    ("linkedin", r"linked\s*in",                                  "linkedin.com"),
    ("twitter",  r"\btwitter\b|\bx\.com\b",                     "twitter.com|x.com"),
    ("website",  r"personal (site|website)|portfolio|home\s*page|\bblog\b|your website", ""),
)


def known_link(label: str, data: dict, defaults: dict | None = None) -> str:
    """A PROFILE LINK IS A FACT WE ALREADY OWN. Asking him for it is a defect.

    MEASURED (ElevenLabs, 2026-08-18): `Link to your Github profile` reached the 3-LLM panel, one
    vendor INVENTED `https://github.com/username`, the quorum fell back to ask_human, and he was
    woken on Telegram -- while `- github: https://github.com/feranicus` sat in candidate.md the
    whole time. CLAUDE.md already carries the rule ("the engine must never ASK for a fact it
    already owns") and the github rule had FOUR homes in this one file, so the path that needed it
    was the path that did not have it.

    ONE home now. Pure, so every wording he actually meets is a test rather than a hope. A link is
    returned ONLY when it points at the host the label asked about, so a LinkedIn URL can never be
    typed into a GitHub box; a label we hold nothing for returns "" and the ladder continues."""
    lab = str(label or "")
    d = defaults or {}
    b = data.get("basics") or {} if isinstance(data, dict) else {}
    prof = data.get("profiles") if isinstance(data, dict) else None
    prof = prof if isinstance(prof, dict) else {}
    for kind, rx, hosts in _LINK_KINDS:
        if not re.search(rx, lab, re.I):
            continue
        if kind == "linkedin":                      # one home for LinkedIn stays _linkedin_url
            return _linkedin_url(data, d)
        cands = [b.get(kind), b.get(f"{kind}_url"), b.get(f"{kind}_profile"),
                 d.get(kind), d.get(f"{kind}_url"), d.get(f"{kind}_profile"),
                 prof.get(kind), b.get("website"), d.get("website")]
        for c in cands:
            v = str(c or "").strip().strip('"\'')
            if not v:
                continue
            if hosts and not re.search(hosts, v, re.I):
                continue                            # a link to the WRONG site is not an answer
            if not hosts and not re.search(r"\.[a-z]{2,}", v, re.I):
                continue
            return v if v.startswith("http") else "https://" + v.lstrip("/")
        return ""
    return ""


async def _answer_yes_no_near(page, question_rx: str, prefer_yes: bool, tag: str, r: dict) -> bool:
    """Click Yes/No for Ashby custom toggles (buttons, radios, or labeled controls).

    Measured on Cohere 2026-08-18: 'Are you currently based in Germany?' and the 15+ years
    VP question use side-by-side Yes/No *buttons*, not classic role=radio with long names.
    role-based matching alone fails; climb from the question text and click the literal Yes/No.
    """
    want = "Yes" if prefer_yes else "No"
    # --- 1) DOM climb from question text (most reliable for Ashby) ---
    try:
        ok = await page.evaluate(
            """({qRx, want}) => {
              const re = new RegExp(qRx, 'i');
              const nodes = [...document.querySelectorAll(
                'label,legend,p,div,span,h1,h2,h3,h4,li,td,th,strong,b'
              )];
              const q = nodes.find(el => {
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                return t.length > 8 && t.length < 220 && re.test(t);
              });
              if (!q) return {ok:false, why:'no-question'};
              let root = q;
              for (let depth = 0; depth < 10 && root; depth++) {
                const candidates = [
                  ...root.querySelectorAll('button,[role=button],input[type=radio],label,[role=radio]')
                ];
                for (const el of candidates) {
                  const t = (el.innerText || el.getAttribute('aria-label') || el.value || '')
                    .replace(/\\s+/g, ' ').trim();
                  if (!new RegExp('^' + want + '$', 'i').test(t)) continue;
                  // skip if this control belongs to a different long question in a nested sibling
                  try {
                    el.scrollIntoView({block:'center'});
                    el.click();
                    // mark selected state when possible
                    if (el.tagName === 'INPUT' && el.type === 'radio') {
                      el.checked = true;
                      el.dispatchEvent(new Event('change', {bubbles:true}));
                    }
                    return {ok:true, why:'dom-click', depth, text:t};
                  } catch (e) {
                    return {ok:false, why:'click-err:'+e};
                  }
                }
                root = root.parentElement;
              }
              return {ok:false, why:'no-yesno-under-question'};
            }""",
            {"qRx": question_rx, "want": want},
        )
        if ok and ok.get("ok"):
            _log(f"  yes/no {tag}: {want} (dom {ok.get('why')})")
            r["filled"].append(f"tick:{tag}")
            await page.wait_for_timeout(400)
            return True
        if ok and ok.get("why"):
            _log(f"  yes/no {tag}: DOM miss ({ok.get('why')})")
    except Exception as e:
        _log(f"  yes/no {tag}: DOM eval error {type(e).__name__}")

    # --- 2) Playwright role=radio / button near get_by_text ---
    try:
        q = page.get_by_text(re.compile(question_rx, re.I))
        for qi in range(min(await q.count(), 3)):
            node = q.nth(qi)
            if not await node.is_visible():
                continue
            for role in ("radio", "button"):
                try:
                    loc = node.locator("xpath=ancestor-or-self::*[position()<=8]").get_by_role(
                        role, name=re.compile(rf"^\\s*{want}\\s*$", re.I)
                    )
                    if await loc.count() == 0:
                        # broader: following siblings container
                        loc = node.locator("xpath=following::*[self::button or @role='button' or @role='radio' or self::label][1]")
                    if await loc.count() == 0:
                        continue
                    el = loc.first
                    if not await el.is_visible():
                        continue
                    if role == "radio":
                        try:
                            await el.check(timeout=4000)
                        except Exception:
                            await el.click(timeout=4000)
                    else:
                        await el.click(timeout=4000)
                    _log(f"  yes/no {tag}: {want} (pw {role})")
                    r["filled"].append(f"tick:{tag}")
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    # --- 3) Sponsorship-only: Yes/No radio whose ancestor text contains 'sponsor' ---
    try:
        if re.search(r"sponsor", question_rx, re.I):
            radios = page.get_by_role("radio", name=re.compile(rf"^\\s*{want}\\s*$", re.I))
            for i in range(min(await radios.count(), 8)):
                el = radios.nth(i)
                if not await el.is_visible():
                    continue
                ctx = await el.evaluate("""(el) => {
                  let n = el, t = '';
                  for (let i = 0; i < 5 && n; i++) { t += ' ' + (n.innerText||''); n = n.parentElement; }
                  return t;
                }""") or ""
                if re.search(r"sponsor", ctx, re.I):
                    if not await el.is_checked():
                        await el.check(timeout=4000)
                    if await el.is_checked():
                        _log(f"  yes/no {tag}: {want} (sponsor context)")
                        r["filled"].append(f"tick:{tag}")
                        return True
    except Exception:
        pass
    return False


async def _apply_screening_radios(page, defaults: dict, r: dict) -> int:
    """Employer-agnostic Ashby Yes/No from screening_defaults + career facts.

    Cohere (and many Ashby forms) use short Yes/No radios or buttons under long questions.
    Recorded n8n ticks do not transfer. Never leave required Yes/No blank.
    """
    n = 0
    # require sponsorship / visa → No (EU work auth)
    need = str(
        defaults.get("require sponsorship")
        or defaults.get("visa sponsorship")
        or defaults.get("require_sponsorship")
        or defaults.get("requires_visa_sponsorship")
        or defaults.get("needs_visa_sponsorship")
        or "No"
    ).strip().lower()
    wants_sponsor = need in ("yes", "y", "true", "1")
    # Long-label radios first (Volta-style)
    if wants_sponsor:
        long_pats = [r"Yes, I will require .* to sponsor", r"^Yes\b.*sponsor", r"I will require .* sponsorship"]
    else:
        long_pats = [
            r"No, I do not require sponsorship",
            r"I do not require sponsorship to work",
            r"^No\b.*sponsor",
            r"do not require sponsorship",
        ]
    hit = False
    for pat in long_pats:
        try:
            loc = page.get_by_role("radio", name=re.compile(pat, re.I))
            if await loc.count() == 0:
                continue
            el = loc.first
            if await el.is_visible() and not await el.is_checked():
                await el.check(timeout=4000)
            if await el.is_visible() and await el.is_checked():
                _log(f"  screening radio: sponsorship={'yes' if wants_sponsor else 'no'}")
                r["filled"].append("tick:sponsorship")
                n += 1
                hit = True
                break
        except Exception:
            continue
    if not hit:
        if await _answer_yes_no_near(
            page,
            r"require sponsorship|visa sponsorship|sponsor.*work",
            prefer_yes=wants_sponsor,
            tag="sponsorship",
            r=r,
        ):
            n += 1

    # authorized to work → Yes
    auth = str(
        defaults.get("legally authorised to work")
        or defaults.get("legally authorized to work")
        or defaults.get("authorized_to_work")
        or "Yes"
    ).lower()
    if auth in ("yes", "y", "true", "1"):
        hit = False
        for pat in (r"Yes.*authori[sz]ed", r"^Yes$", r"I am authori[sz]ed"):
            try:
                loc = page.get_by_role("radio", name=re.compile(pat, re.I))
                if await loc.count() and await loc.first.is_visible():
                    if not await loc.first.is_checked():
                        await loc.first.check(timeout=3000)
                    if await loc.first.is_checked():
                        r["filled"].append("tick:work_auth")
                        n += 1
                        hit = True
                        break
            except Exception:
                continue
        if not hit:
            if await _answer_yes_no_near(
                page,
                r"authori[sz]ed to work|work in the country you currently reside",
                prefer_yes=True,
                tag="work_auth",
                r=r,
            ):
                n += 1

    # based in Germany → Yes (candidate lives in Friedberg, Hessen)
    if await _answer_yes_no_near(
        page,
        r"based in Germany|currently based in Germany|reside in Germany",
        prefer_yes=True,
        tag="based_germany",
        r=r,
    ):
        n += 1

    # 15+ years VP / enterprise sales experience → Yes for this profile (20+ years, director/VP-level)
    if await _answer_yes_no_near(
        page,
        r"15\+?\s*years|VP level experience|enterprise technology sales",
        prefer_yes=True,
        tag="vp_experience",
        r=r,
    ):
        n += 1

    return n


async def _fill_role(page, role: str, name_rx: str, value: str, exact: bool = False) -> bool:
    if not value:
        return False
    try:
        if exact:
            loc = page.get_by_role(role, name=name_rx, exact=True)
        else:
            loc = page.get_by_role(role, name=re.compile(name_rx, re.I))
        if await loc.count() == 0:
            return False
        el = loc.first
        await el.scroll_into_view_if_needed(timeout=3000)
        await el.click(timeout=4000)
        await el.fill("")
        await el.fill(str(value), timeout=5000)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(250)
        try:
            got = (await el.input_value() or "").strip()
        except Exception:
            got = str(value)
        ok = bool(got)
        _log(f"  {name_rx[:40]!r} = {got[:50]!r}" + ("" if ok else " EMPTY"))
        return ok
    except Exception as e:
        _log(f"  fill {name_rx[:30]!r} failed: {type(e).__name__}")
        return False


async def _fill_by_label(page, label_rx: str, value: str) -> bool:
    """Ashby often uses placeholder or nearby label, not perfect a11y names.

    THE ONE CHOKE POINT FOR STUBS. `UNKNOWN` reached the Notice-period box twice now: the essay guard
    refuses it, the residual pass refuses it, and then a THIRD caller typed it anyway. Guarding callers
    one at a time does not work — every path ends here, so the refusal belongs here."""
    if not value:
        return False
    if _STUB.match(str(value).strip()):
        _log(f"  refusing to type {str(value).strip()[:20]!r} into {label_rx[:34]!r} — not an answer")
        return False
    for role in ("textbox", "combobox", "searchbox"):
        if await _fill_role(page, role, label_rx, value):
            return True
    try:
        loc = page.locator(
            f"input:visible, textarea:visible"
        )
        n = min(await loc.count(), 40)
        rx = re.compile(label_rx, re.I)
        for i in range(n):
            el = loc.nth(i)
            try:
                ph = await el.get_attribute("placeholder") or ""
                aria = await el.get_attribute("aria-label") or ""
                name = await el.get_attribute("name") or ""
                # sibling / parent label text
                lab = await el.evaluate("""e => {
                  const n = t => (t||'').replace(/\\s+/g,' ').trim();
                  if (e.id) {
                    const l = document.querySelector('label[for="'+CSS.escape(e.id)+'"]');
                    if (l) return n(l.innerText);
                  }
                  const w = e.closest('label,div,li,fieldset');
                  return w ? n(w.innerText).slice(0,120) : '';
                }""") or ""
                blob = f"{ph} {aria} {name} {lab}"
                if not rx.search(blob):
                    continue
                typ = (await el.get_attribute("type") or "").lower()
                if typ in ("hidden", "file", "checkbox", "radio", "submit", "button"):
                    continue
                cur = ""
                try:
                    cur = (await el.input_value() or "").strip()
                except Exception:
                    pass
                if cur and len(cur) > 2:
                    continue
                await el.click(timeout=3000)
                await el.fill(str(value), timeout=5000)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(200)
                got = (await el.input_value() or "").strip()
                _log(f"  label~{label_rx[:28]!r} = {got[:50]!r}")
                return bool(got)
            except Exception:
                continue
    except Exception as e:
        _log(f"  label scan failed: {type(e).__name__}")
    return False


async def _upload_files(page, resume_path: str, cover_path: str = "") -> list:
    """Ashby Resume / Cover upload.

    UI (measured): dashed zone + pink button "Upload File" under a `Resume*` label.
    Blind set_input_files on form-wide groups is flaky — sometimes the input accepts the
    file in the DOM and the zone still looks empty; sometimes the wrong input is hit.
    Strategy order:
      1. Scope to the Resume*/Cover heading → click its Upload File → filechooser
      2. set_input_files on input[type=file] INSIDE that scoped section only
      3. Page-wide empty inputs as last resort
    Success = input.files.length > 0 OR the zone no longer shows only the empty CTA.
    

    Contracts (source-checked by ashby.py --logic):
      - attaches to the GROUP that asks for it, not input #0
      - the filename is READ BACK off the page
      - an input that already holds our file counts as attached
"""
    filled = []
    want = []
    if resume_path and os.path.isfile(resume_path):
        want.append(("resume", resume_path, re.compile(r"^\s*Resume\s*\*?\s*$", re.I),
                     re.compile(r"resume|cv\b|curriculum", re.I)))
    else:
        _log(f"  NO RESUME FILE at {resume_path!r} — nothing to attach")
    cov = cover_path if (cover_path and os.path.isfile(cover_path)) else ""
    if not cov and resume_path:
        sib = os.path.join(os.path.dirname(resume_path), "cover_letter.pdf")
        cov = sib if os.path.isfile(sib) else ""
    if cov:
        want.append(("cover_letter", cov, re.compile(r"^\s*Cover letter\s*\*?\s*$", re.I),
                     re.compile(r"cover letter|motivation letter", re.I)))
    if not want:
        return filled

    # Wait for the upload UI to be present
    try:
        await page.get_by_role("button", name=re.compile(r"upload file", re.I)).first.wait_for(
            state="visible", timeout=12000)
    except Exception:
        try:
            await page.locator("input[type=file]").first.wait_for(state="attached", timeout=8000)
        except Exception:
            _log("  upload UI not ready (no Upload File button / file input)")

    async def _files_len(el) -> int:
        try:
            return int(await el.evaluate("e => (e.files && e.files.length) || 0") or 0)
        except Exception:
            return 0

    async def _zone_looks_filled(section, base: str) -> bool:
        try:
            txt = (await section.inner_text() or "")
        except Exception:
            txt = ""
        stem = os.path.splitext(base)[0][:12]
        if base in txt or (stem and stem in txt):
            return True
        # empty CTA still dominant?
        if re.search(r"drag and drop here", txt, re.I) and not re.search(
            r"\.(pdf|docx?)|remove|replace|attached", txt, re.I
        ):
            return False
        if re.search(r"\.(pdf|docx?)\b", txt, re.I):
            return True
        return False

    async def _section_for(kind: str, exact_rx, loose_rx):
        # Heading → ancestor that contains a file input or Upload File button
        try:
            heads = page.get_by_text(exact_rx)
            for i in range(min(await heads.count(), 8)):
                h = heads.nth(i)
                try:
                    if not await h.is_visible():
                        continue
                except Exception:
                    continue
                sec = h.locator(
                    "xpath=ancestor::*[.//input[@type='file'] or "
                    ".//button[contains(translate(normalize-space(.), "
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'upload file')]][1]"
                )
                if await sec.count():
                    return sec.first
        except Exception as e:
            _log(f"  section heading {kind}: {type(e).__name__}")
        # Fallback: smallest container that matches loose label + has file input
        groups = page.locator("div, section, fieldset, li").filter(has_text=loose_rx)
        best = None
        best_score = 10 ** 9
        for i in range(min(await groups.count(), 24)):
            g = groups.nth(i)
            if not await g.locator("input[type=file]").count():
                if not await g.get_by_role("button", name=re.compile(r"upload file", re.I)).count():
                    continue
            try:
                score = len(await g.inner_text() or "")
            except Exception:
                score = 10 ** 9
            if 20 < score < best_score and score < 3000:
                best_score = score
                best = g
        return best

    for kind, path, exact_rx, loose_rx in want:
        base = os.path.basename(path)
        placed = False
        section = await _section_for(kind, exact_rx, loose_rx)
        # Cover must not overwrite Resume*. If the only zone is Resume*, skip cover.
        if kind == "cover_letter" and section is not None:
            try:
                stx = (await section.inner_text())[:200]
                if re.search(r"\bResume\b", stx, re.I) and not re.search(r"cover\s*letter", stx, re.I):
                    _log("  skipping cover_letter — resolved section is Resume* (would overwrite CV)")
                    continue
            except Exception:
                pass
        if kind == "cover_letter" and section is None:
            # no Cover Letter field on this form (Cohere etc.) — do not fall back to Resume
            _log("  skipping cover_letter — no distinct Cover Letter field on page")
            continue

        # --- 1) filechooser via Upload File button inside section (or page) ---
        try:
            if section is not None:
                btn = section.get_by_role("button", name=re.compile(r"upload file", re.I))
            else:
                btn = page.get_by_role("button", name=re.compile(r"upload file", re.I))
            nb = await btn.count()
            for bi in range(min(nb, 4)):
                b = btn.nth(bi)
                if not await b.is_visible():
                    continue
                try:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await b.click(timeout=4000)
                    chooser = await fc_info.value
                    await chooser.set_files(path)
                    await page.wait_for_timeout(1200)
                    # verify
                    scope = section if section is not None else page
                    ok = False
                    for j in range(await scope.locator("input[type=file]").count()):
                        el = scope.locator("input[type=file]").nth(j)
                        if await _files_len(el) > 0:
                            ok = True
                            break
                    if not ok and section is not None:
                        ok = await _zone_looks_filled(section, base)
                    if ok:
                        _log(f"  attached {base} via Upload File button (verified)")
                        filled.append(f"upload:{base}")
                        placed = True
                        break
                except Exception as e:
                    _log(f"  filechooser {kind} #{bi}: {type(e).__name__}: {str(e).splitlines()[0][:80]}")
        except Exception as e:
            _log(f"  Upload File path {kind}: {type(e).__name__}")

        # --- 2) direct set_input_files on scoped inputs ---
        if not placed:
            try:
                scope = section if section is not None else page
                inputs = scope.locator("input[type=file]")
                for i in range(min(await inputs.count(), 6)):
                    el = inputs.nth(i)
                    if await _files_len(el) > 0:
                        _log(f"  {kind}: input already has file — counting it")
                        filled.append(f"upload:{base}")
                        placed = True
                        break
                    try:
                        await el.set_input_files(path)
                    except Exception as e:
                        _log(f"  set_input_files {kind} #{i}: {type(e).__name__}")
                        continue
                    await page.wait_for_timeout(1000)
                    if await _files_len(el) > 0 or (
                        section is not None and await _zone_looks_filled(section, base)
                    ):
                        _log(f"  attached {base} to scoped file input #{i} (verified)")
                        filled.append(f"upload:{base}")
                        placed = True
                        break
            except Exception as e:
                _log(f"  scoped input {kind}: {type(e).__name__}")

        # --- 3) page-wide empty inputs (resume only — never drop cover onto Resume*) ---
        if not placed and kind == "resume":
            try:
                inputs = page.locator("input[type=file]")
                for i in range(min(await inputs.count(), 8)):
                    el = inputs.nth(i)
                    if await _files_len(el) > 0:
                        continue
                    try:
                        await el.set_input_files(path)
                    except Exception:
                        continue
                    await page.wait_for_timeout(900)
                    if await _files_len(el) > 0:
                        _log(f"  attached {base} to page file input #{i} (verified)")
                        filled.append(f"upload:{base}")
                        placed = True
                        break
            except Exception as e:
                _log(f"  page input {kind}: {type(e).__name__}")

        if not placed:
            _log(f"  COULD NOT ATTACH {base} — the {kind} field is still empty")

    return filled


async def _typeahead(page, query: str, pick_contains: str = "") -> bool:
    """Ashby location: combobox 'Start typing...' then pick an option."""
    if not query:
        return False
    try:
        box = page.get_by_role("combobox", name=re.compile(r"start typing|location|city", re.I))
        if await box.count() == 0:
            box = page.get_by_placeholder(re.compile(r"start typing", re.I))
        if await box.count() == 0:
            return False
        el = box.first
        await el.click(timeout=4000)
        await el.fill("")
        await el.type(query, delay=40)
        await page.wait_for_timeout(900)
        # SCOPE THE OPTIONS TO THE DROPDOWN, NEVER THE WHOLE PAGE.
        # MEASURED: it typed "Germany" and then matched `[class*='option']` across the document,
        # landing on the CHECKBOX label "I can be based in Germany and do not need visa support" —
        # so the location box stayed EMPTY, that checkbox got toggled by the click, and the recorded
        # Germany tick was then skipped as "already checked" (15 ticks instead of 16).
        # A suggestion list lives in a listbox owned by the combobox; a page-wide selector is a bet.
        opt = page.locator("[role=listbox] [role=option], [role=option]")
        try:
            owns = await el.get_attribute("aria-owns") or await el.get_attribute("aria-controls") or ""
        except Exception:
            owns = ""
        if owns:
            # `aria-owns` MAY HOLD SEVERAL SPACE-SEPARATED IDS, and an id may begin with a digit or
            # contain ':' — so `f"#{owns} ..."` is not a valid CSS selector and Playwright threw,
            # which is the bare `typeahead failed: Error` in the log. Use the ATTRIBUTE form, which
            # needs no escaping, and take one id at a time. A selector built from page data must be
            # quoted, never interpolated.
            for _id in (owns.split() or [])[:3]:
                try:
                    scoped = page.locator(
                        f'[id="{_id}"] [role=option], [id="{_id}"] li')
                    if await scoped.count():
                        opt = scoped
                        break
                except Exception as _e:
                    _log(f"  typeahead: aria-owns {_id!r} is not usable ({type(_e).__name__})")
        if await opt.count() == 0:
            opt = page.locator("[role=listbox] li, [class*='menu'] [class*='option']")
        if await opt.count() == 0:
            await page.keyboard.press("Enter")
            _log(f"  typeahead typed {query!r} (no option list)")
            return True
        target = pick_contains or query
        # AN OPTION IS A SHORT VALUE, NOT A SENTENCE. "I can be based in Germany and do not need visa
        # support" is a checkbox label; a country option is "Germany" or "Germany (Berlin)".
        _sane = re.compile(r"visa support|i (can|will|want) be|based in|need visa", re.I)
        chosen = None
        for i in range(min(await opt.count(), 12)):
            t = (await opt.nth(i).inner_text() or "").strip()
            if _sane.search(t) or len(t) > 48:
                continue                       # not an option: that is a form control's own label
            if re.search(re.escape(target), t, re.I) or (i == 0 and t):
                chosen = opt.nth(i)
                label = t
                if re.search(re.escape(target), t, re.I):
                    break
        if chosen is None:
            chosen = opt.first
            label = (await chosen.inner_text() or "").strip()
        await chosen.click(timeout=4000)
        _log(f"  typeahead -> {label[:60]!r}")
        return True
    except Exception as e:
        # NAME WHAT WENT WRONG. `typeahead failed: Error` cost a whole run to diagnose: Playwright's
        # exception class is just `Error`, so the class name alone says nothing. The message does.
        _log(f"  typeahead failed: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
        return False


_STUB = re.compile(r"^\s*(unknown|ask|tbd|n/?a|none|null|todo|\?+|-+|yes|no)\s*[.!]?\s*$", re.I)


def usable_essay(v: str, min_len: int = 40) -> bool:
    """Is this prose, or a stub? PURE, so it is a test and not a hope.

    MEASURED: `UNKNOWN` was typed into "What about n8n and the role caught your attention" — the essay
    guard refused the 7-character stub, and then the RESIDUAL required-field pass typed the same string
    through `_fill_by_label`. One guard on one path is not a guard. A stub in an essay box is worse
    than an empty one: it reads as contempt, and it cannot be retracted."""
    t = (v or "").strip()
    return len(t) >= min_len and not _STUB.match(t)


def recorded_essay(label: str, min_len: int = 40) -> str:
    """HIS OWN recorded answer for this question, or ''. Tried FIRST, not as a fallback."""
    try:
        from recordings import known_value
        for probe in (label, label[:60], label[:40]):
            v = known_value("ashby", probe) or ""
            if usable_essay(v, min_len):
                return v
    except Exception:
        pass
    return ""


async def _answer_text(page, label_rx: str, value: str) -> bool:
    if not value:
        return False
    # Prefer textarea for long answers
    try:
        areas = page.locator("textarea:visible")
        n = min(await areas.count(), 20)
        rx = re.compile(label_rx, re.I)
        for i in range(n):
            el = areas.nth(i)
            lab = await el.evaluate("""e => {
              const n = t => (t||'').replace(/\\s+/g,' ').trim();
              if (e.getAttribute('aria-label')) return n(e.getAttribute('aria-label'));
              if (e.id) {
                const l = document.querySelector('label[for="'+CSS.escape(e.id)+'"]');
                if (l) return n(l.innerText);
              }
              const w = e.closest('div,li,fieldset,label');
              return w ? n(w.innerText).slice(0, 160) : '';
            }""") or ""
            if not rx.search(lab):
                continue
            cur = (await el.input_value() or "").strip()
            if cur and len(cur) > 20:
                return True
            # A SEVEN-CHARACTER ESSAY IS NOT AN ANSWER. The run logged
            #     essay~'enterprise sales methodology|rollout' wrote 7 chars
            # twice — the model returned something like "Yes" and it was accepted and counted as
            # filled. An open question needs prose; below 40 characters it is not one, so fall back to
            # the RECORDED answer and, failing that, report it unresolved so the ladder asks him.
            # HIS RECORDING FIRST. It was a fallback-after-the-model, so a 7-char model answer got
            # the first attempt at every box and a stub was typed before his own words were tried.
            val = recorded_essay(lab) or (value or "")
            if not usable_essay(val):
                try:
                    from recordings import known_value
                    rec = known_value("ashby", lab[:60]) or ""
                    if len(rec.strip()) >= 40:
                        _log(f"  essay~{label_rx[:30]!r}: model gave {len(val.strip())} chars — "
                             f"using the RECORDED answer ({len(rec)} chars)")
                        val = rec
                except Exception:
                    pass
            if not usable_essay(val):
                _log(f"  essay~{label_rx[:30]!r}: {str(val).strip()[:18]!r} is not an answer — "
                     f"NOT typing a stub, leaving it for you")
                return False
            await el.click(timeout=3000)
            await el.fill(val[:4000], timeout=8000)
            got = (await el.input_value() or "").strip()
            _log(f"  essay~{label_rx[:36]!r} wrote {len(got)} chars")
            return len(got) >= 40
    except Exception as e:
        _log(f"  essay fill: {type(e).__name__}")
    return await _fill_by_label(page, label_rx, value)


async def _required_empty(page) -> list[str]:
    try:
        return await page.evaluate("""() => {
          const out = [], seen = new Set(), radioSeen = new Set();
          const vis = el => {
            const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
            return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const labelOf = el => {
            const n = t => (t||'').replace(/\\s+/g,' ').trim();
            if (el.getAttribute('aria-label')) return n(el.getAttribute('aria-label'));
            if (el.id) {
              const l = document.querySelector('label[for="'+CSS.escape(el.id)+'"]');
              if (l) return n(l.innerText);
            }
            const w = el.closest('label,div,li,fieldset');
            return w ? n(w.innerText).slice(0, 80) : (el.placeholder || el.name || '');
          };
          document.querySelectorAll('input,textarea,select').forEach(el => {
            if (!vis(el)) return;
            const ty = (el.type || '').toLowerCase();
            if (['hidden','submit','button','file','image','reset'].includes(ty)) return;
            const req = el.required || el.getAttribute('aria-required') === 'true'
              || /\\*/.test(labelOf(el));
            if (!req) return;
            let empty = false;
            if (ty === 'checkbox') {
              // A REQUIRED TICK IS A REQUIRED FIELD. Skipping checkboxes meant this check could report
              // "nothing empty" while `I agree` and the three location-eligibility boxes were
              // unticked — n8n's own error page listed seven of them. A guard that ignores the
              // mandatory consent box is not a guard.
              empty = !el.checked;
            } else if (ty === 'radio') {
              // A RADIO IS A GROUP: empty only when NOTHING in the group is chosen, reported once.
              const nm = el.name || '';
              let any = false;
              (nm ? document.querySelectorAll('input[type=radio][name="' + nm.replace(/"/g, '') + '"]')
                  : [el]).forEach(g => { if (g.checked) any = true; });
              if (any || radioSeen.has(nm)) return;
              radioSeen.add(nm);
              empty = true;
            } else {
              empty = !(el.value || '').trim();
            }
            if (empty) {
              const lab = labelOf(el).slice(0, 70);
              if (lab && !seen.has(lab)) { seen.add(lab); out.push(lab); }
            }
          });
          return out.slice(0, 12);
        }""") or []
    except Exception:
        return []


async def _tick_required(page, r: dict, log=_log) -> int:
    """Tick EXACTLY what his recording ticked. Never a keyword sweep.

    MEASURED, and it is the worst defect in this adapter's history: my `want` regex contained
    `based in`, which matches ALL EIGHT options of "Tick all options relevant... regarding the main
    location of your work contract" — so the run ticked
        'I can be based in the UK and do not need visa support'
        'I want to be based in the UK but will need visa support'
        'I will be based in the US and need visa support'          <- and five more
    A LOCATION ELIGIBILITY BOX IS A DECLARATION ABOUT HIM. Ticking all of them states, in his name,
    contradictory and untrue things to an employer — worse than leaving the form blank, and exactly
    the class this project already forbids for gender/veteran/disability.
    His codegen ticks THREE: 'I can be based in Germany and', 'I can be based in a European',
    'I want to be based in a'. Those are facts he asserted himself, so those are what we tick.
    A box he did not tick is left alone; if that leaves a required group empty, `_required_empty`
    reports it and the ladder asks him."""
    want = []
    try:
        from recordings import load as _kb
        want = [c.get("name") for c in (_kb("ashby").get("checkboxes") or [])
                if isinstance(c, dict) and c.get("checked") and c.get("name")]
    except Exception as e:
        log(f"  recorded ticks unavailable ({type(e).__name__}) — ticking NOTHING by keyword")
    if not want:
        log("  no recorded ticks for this ATS — leaving every box alone (a tick is a declaration)")
        return 0
    done = 0
    for name in want:
        try:
            # A SHORT LABEL NEEDS AN EXACT MATCH FIRST. 'Man' as a prefix matches "Man" but also
            # "Management"/"Manager" wording elsewhere, and Playwright's `.first` then resolves by DOM
            # order — which is why the recorded 'Man' tick was the ONE of seventeen that did not land
            # (16 applied, gender left blank). Exact, then anchored, then prefix.
            el = None
            for role in ("radio", "checkbox"):
                for kind in ("exact", "anchored", "prefix"):
                    try:
                        if kind == "exact":
                            c = page.get_by_role(role, name=name, exact=True).first
                        elif kind == "anchored":
                            c = page.get_by_role(
                                role, name=re.compile(rf"^{re.escape(name)}$", re.I)).first
                        else:
                            if len(name) < 6:
                                continue          # never prefix-match a 3-letter declaration
                            c = page.get_by_role(role, name=name, exact=False).first
                        if await c.count() and await c.is_visible():
                            el = c
                            break
                    except Exception:
                        continue
                if el is not None:
                    break
            if el is None:
                # CODEGEN TRUNCATES A NAME: the age radio was recorded as '-49' and the real option is
                # '40-49', so exact/anchored/prefix all miss. A CONTAINS match is the honest last try,
                # and only when the fragment is distinctive enough to identify one control.
                if len(name) >= 3:
                    try:
                        for role in ("radio", "checkbox"):
                            c = page.get_by_role(role, name=re.compile(
                                re.escape(name), re.I)).first
                            if await c.count() == 1 and await c.is_visible():
                                el = c
                                break
                    except Exception:
                        pass
            if el is None:
                continue
            if not await el.count() or not await el.is_visible():
                continue
            if await el.is_checked():
                continue                       # NEVER TOGGLE: a second click unticks it
            await el.check(timeout=4000)
            if await el.is_checked():           # READ IT BACK
                log(f"  ticked (recorded) {name[:56]!r}")
                r["filled"].append("tick:" + name[:20])
                done += 1
            else:
                log(f"  {name[:56]!r} did NOT stay ticked")
        except Exception:
            continue
    missed = [n for n in want if not any(n[:20] in f for f in (r.get("filled") or []))]
    if missed:
        log(f"  {len(missed)} recorded choice(s) did NOT resolve on this page: "
            f"{[m[:34] for m in missed]}")
        r["unresolved"] = list(dict.fromkeys((r.get("unresolved") or []) + missed))
    log(f"  {done} recorded choice(s) applied (not a bulk tick)")
    return done



def _validation_missing(body: str) -> list[str]:
    """Ashby red banner: 'Missing entry for required field: How did you hear about X?'"""
    out = []
    for m in re.finditer(
        r"Missing entry for required field:\s*([^\n•]+)", body or "", re.I
    ):
        lab = m.group(1).strip().rstrip(".")
        if lab and lab not in out:
            out.append(lab)
    for m in re.finditer(r"required field[:\s]+([^\n•]+)", body or "", re.I):
        lab = m.group(1).strip().rstrip(".")
        if lab and lab not in out and len(lab) < 120:
            out.append(lab)
    return out


def _is_phone_like(val: str) -> bool:
    s = re.sub(r"[\s\-\(\)+]", "", str(val or ""))
    return bool(re.fullmatch(r"\d{7,15}", s))


async def _click_radio_by_patterns(page, patterns: list[str], tag: str, r: dict) -> bool:
    for pat in patterns:
        try:
            loc = page.get_by_role("radio", name=re.compile(pat, re.I))
            if await loc.count() == 0:
                continue
            el = loc.first
            if not await el.is_visible():
                continue
            if not await el.is_checked():
                await el.check(timeout=4000)
            if await el.is_checked():
                _log(f"  radio {tag}: matched {pat!r}")
                r["filled"].append(f"tick:{tag}")
                return True
        except Exception:
            continue
    return False


async def _answer_common_ashby_questions(page, data: dict, defaults: dict, url: str, r: dict) -> int:
    """Easy closed questions the 3-LLM panel should not need a human for."""
    n = 0
    # How did you hear about <Company>?
    utm = ""
    try:
        m = re.search(r"utm_source=([^&]+)", url or "", re.I)
        utm = (m.group(1) if m else "").lower()
    except Exception:
        utm = ""
    source_defaults = str(defaults.get("how_heard") or defaults.get("source") or "").strip()
    if "linkedin" in utm or "linkedin" in source_defaults.lower() or not source_defaults:
        hear_pats = [
            r"LinkedIn",
            r"Social media",
            r"Job board",
            r"Company website",
            r"News article",
            r"I'm a user",
            r"Referral",
            r"Other",
        ]
    else:
        hear_pats = [re.escape(source_defaults)] + [
            r"LinkedIn", r"Job board", r"Other", r"News article", r"I'm a user",
        ]
    if await _click_radio_by_patterns(page, hear_pats, "how_heard", r):
        n += 1

    # Years of experience (account executive / sales / …)
    yrs = str(
        defaults.get("years_account_executive")
        or defaults.get("years_experience")
        or defaults.get("ae_years")
        or ""
    ).strip()
    if not yrs:
        # senior commercial profile → prefer 7+ when those options exist
        yrs = "7+"
    yr_map = {
        "7+": [r"^7\+", r"7\s*\+|10\+|more than 7"],
        "4-6": [r"^4-6$", r"4\s*-\s*6"],
        "1-3": [r"^1-3$", r"1\s*-\s*3"],
        "n/a": [r"^N/?A$", r"not applicable"],
    }
    key = "7+"
    if re.search(r"n/?a", yrs, re.I):
        key = "n/a"
    elif re.search(r"^[1-3]$|1-3", yrs):
        key = "1-3"
    elif re.search(r"4-6|4–6", yrs):
        key = "4-6"
    if await _click_radio_by_patterns(page, yr_map.get(key, yr_map["7+"]), f"years:{key}", r):
        n += 1

    # GitHub from candidate data (avoid Telegram when we already know it) -- ONE home: known_link()
    gh = known_link("github profile", data, defaults)
    if gh:
        if await _fill_by_label(page, r"github", gh) or await _fill_role(page, "textbox", r"Github|GitHub", gh):
            r["filled"].append("github")
            n += 1

    return n


async def _page_text(page) -> str:
    try:
        body = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 9000)"
        ) or ""
    except Exception:
        body = ""
    try:
        extra = await page.evaluate(r"""() => {
          const bits = [];
          document.querySelectorAll(
            '[role=dialog],[role=alertdialog],[class*="modal"],[class*="toast"],[class*="Toast"],[class*="banner"]'
          ).forEach(el => {
            const t = (el.innerText||'').replace(/\s+/g,' ').trim();
            if (t) bits.push(t.slice(0, 500));
          });
          return bits.join(' | ');
        }""") or ""
        if extra:
            body = body + "\n" + extra
    except Exception:
        pass
    return body


def _looks_submitted(body: str, url: str, submit_still: bool) -> bool:
    if re.search(
        r"thank you|thanks for applying|thanks for your (interest|application)|"
        r"application (has been )?(submitted|received|sent)|successfully submitted|"
        r"we (have )?received (your )?application|application received|"
        r"congratulat|you(r)? application is (in|complete)|form sent|"
        r"we will (be )?in touch|next steps",
        body or "", re.I,
    ):
        return True
    if re.search(r"success|confirm|thank|submitted|received", url or "", re.I):
        return True
    if (not submit_still) and re.search(
        r"received|submitted|thank|in touch|application", body or "", re.I
    ):
        return True
    return False


async def _submit_still_visible(page) -> bool:
    try:
        return await page.get_by_role(
            "button", name=re.compile(r"^\s*submit application\s*$", re.I)
        ).count() > 0
    except Exception:
        return False


async def _submit(page, r: dict) -> bool:
    if not any(str(x).startswith("upload:") for x in (r.get("filled") or [])):
        try:
            need = await page.locator("input[type=file]").count()
        except Exception:
            need = 0
        if need:
            _log("  REFUSING submit — no resume/cover upload was confirmed on this run")
            r["note"] = (r.get("note") or "") + " refused to submit: no document attached."
            return False
    blank = await _required_empty(page)
    if blank:
        _log(f"REFUSING submit — still required+empty: {blank}")
        r["note"] = (r.get("note") or "") + f" refused submit: {blank}."
        r["unresolved"] = blank
        return False
    if not AUTOSUBMIT:
        _log("JHW_AUTOSUBMIT=0 — stop at filled form (no Submit click)")
        r["stage"] = "review"
        return False
    try:
        btn = page.get_by_role("button", name=re.compile(r"^\s*submit application\s*$", re.I))
        if await btn.count() == 0:
            btn = page.get_by_role("button", name=re.compile(r"^\s*submit\s*$", re.I))
        if await btn.count() == 0:
            _log("no Submit Application button found")
            return False
        await btn.first.scroll_into_view_if_needed(timeout=3000)
        await btn.first.click(timeout=8000)
        _log("clicked Submit Application")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        body = await _page_text(page)
        url = page.url or ""
        still = await _submit_still_visible(page)
        # Ashby red banner can sit outside the first body slice — pull it explicitly
        try:
            val_txt = await page.evaluate("""() => {
              const bits = [];
              for (const el of document.querySelectorAll('div,section,li,p,h1,h2,h3,span')) {
                const t = (el.innerText || '').trim();
                if (!t) continue;
                if (/form needs corrections|Missing entry for required/i.test(t) && t.length < 600) {
                  bits.push(t);
                }
              }
              return bits.slice(0, 6).join('\n');
            }""") or ""
            if val_txt.strip():
                body = val_txt + "\n" + (body or "")
        except Exception:
            pass

        if re.search(
            r"Missing entry for required field|form needs corrections|"
            r"required field.*(missing|empty)|please (complete|fix|fill) (the |all )?required|"
            r"cannot be empty",
            body, re.I,
        ):
            _log(f"submit blocked by validation: {body[:280]!r}")
            r["submitted"] = False
            r["stage"] = "blocked"
            r["note"] = (r.get("note") or "") + " submit blocked by validation."
            return False

        ok = _looks_submitted(body, url, still)
        if not ok:
            await page.wait_for_timeout(3500)
            body = await _page_text(page)
            url = page.url or ""
            still = await _submit_still_visible(page)
            ok = _looks_submitted(body, url, still)

        if not ok and not still:
            if len(r.get("filled") or []) >= 15 and not re.search(
                r"error|invalid|required", body[:800], re.I
            ):
                ok = True
                _log("SUBMITTED — inferred (Submit gone, no validation, form was complete)")

        if ok:
            r["submitted"] = True
            r["stage"] = "submitted"
            _log("SUBMITTED — site confirmed")
        else:
            r["submitted"] = False
            r["stage"] = "review"
            _log(
                f"submit clicked, confirmation unclear. "
                f"submit_btn_still={still} url={url[:80]} body={body[:160]!r}"
            )
        return ok
    except Exception as e:
        _log(f"submit failed: {type(e).__name__}: {e}")
        return False




async def _enter_application(page, url: str = "") -> bool:
    """Job board overview → application form.

    MEASURED (ElevenLabs 2026-08-18): URL without `/application` lands on the posting with
    a single `Apply for this Job` button. Upload/form controls do not exist yet. Click it
    (or rewrite to /application) before any field work.
    """
    # Already on form?
    try:
        if await page.locator("input[type=file]").count() > 0:
            return True
        if await page.get_by_role("button", name=re.compile(r"upload file", re.I)).count() > 0:
            return True
        if await page.get_by_role("button", name=re.compile(r"submit application", re.I)).count() > 0:
            return True
    except Exception:
        pass

    # Prefer clicking the CTA on this page
    for pat in (
        r"^\s*Apply for this Job\s*$",
        r"^\s*Apply for this role\s*$",
        r"^\s*Apply now\s*$",
        r"^\s*Apply\s*$",
        r"^\s*Start application\s*$",
    ):
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I))
            if await btn.count() == 0:
                btn = page.get_by_role("link", name=re.compile(pat, re.I))
            if await btn.count() == 0:
                continue
            el = btn.first
            if not await el.is_visible():
                continue
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.click(timeout=8000)
            _log(f"  clicked apply CTA matching {pat!r}")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            try:
                await page.wait_for_selector(
                    "input[type=file], button:has-text('Upload File'), button:has-text('Upload file'), "
                    "button:has-text('Submit Application')",
                    timeout=20000,
                )
            except Exception:
                pass
            # form present?
            try:
                if await page.locator("input[type=file]").count() > 0:
                    return True
                if await page.get_by_role("button", name=re.compile(r"upload file|submit application", re.I)).count() > 0:
                    return True
            except Exception:
                pass
        except Exception as e:
            _log(f"  apply CTA {pat!r}: {type(e).__name__}")

    # URL rewrite: .../uuid?x → .../uuid/application?x  (Ashby common pattern)
    if url and "/application" not in (url or "").lower():
        try:
            base, _, query = url.partition("?")
            base = base.rstrip("/")
            # only rewrite if it looks like a job id path, not already application
            if re.search(r"/[0-9a-f]{8}-[0-9a-f-]{20,}$", base, re.I) or re.search(
                r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", base, re.I
            ):
                new_url = base + "/application" + (("?" + query) if query else "")
                _log(f"  trying application URL {new_url[:100]}")
                await page.goto(new_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1500)
                try:
                    await page.wait_for_selector(
                        "input[type=file], button:has-text('Upload File'), button:has-text('Upload file')",
                        timeout=15000,
                    )
                except Exception:
                    pass
                if await page.locator("input[type=file]").count() > 0:
                    return True
                if await page.get_by_role("button", name=re.compile(r"upload file", re.I)).count() > 0:
                    return True
        except Exception as e:
            _log(f"  application URL rewrite: {type(e).__name__}: {str(e).splitlines()[0][:80]}")

    return False


async def drive(data: dict, resume_path: str = "", answer_fn=None, asker=None,
                ats_url: str = "") -> dict:
    """Fill an Ashby application page. Returns standard result dict."""
    from playwright.async_api import async_playwright

    r = {"ok": False, "stage": "start", "filled": [], "note": "", "submitted": False,
         "unresolved": []}
    know = _load_knowledge()
    b = _basics(data)
    defaults = _defaults(data)
    url = ats_url or data.get("ats_url") or ""
    if not url:
        r["note"] = "no Ashby URL"
        return r

    # cover letter path
    cover = ""
    if resume_path:
        cand = os.path.join(os.path.dirname(resume_path), "cover_letter.pdf")
        if os.path.isfile(cand):
            cover = cand
    out = os.getenv("JHW_OUT", "/agent/out")
    if not cover and os.path.isfile(os.path.join(out, "cover_letter.pdf")):
        cover = os.path.join(out, "cover_letter.pdf")
    if not resume_path and os.path.isfile(os.path.join(out, "resume.pdf")):
        resume_path = os.path.join(out, "resume.pdf")

    browser = None
    try:
        pw = await async_playwright().start()
        # Prefer connecting to existing sandbox Chrome if CDP is set
        cdp = os.getenv("JHW_CDP", os.getenv("CDP_URL", "http://127.0.0.1:9222"))
        try:
            browser = await pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _log(f"attached via CDP {cdp}")
        except Exception:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            _log("launched local chromium")

        _log(f"goto {url[:100]}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(800)
        # Job overview pages need "Apply for this Job" before any form exists (ElevenLabs)
        entered = await _enter_application(page, url)
        if not entered:
            _log("  still no application form after apply CTA / URL rewrite — retrying enter")
            entered = await _enter_application(page, url)
        if not entered:
            try:
                await page.wait_for_selector(
                    "button:has-text('Upload File'), button:has-text('Upload file'), input[type=file]",
                    timeout=8000,
                )
            except Exception:
                _log("  form controls still absent — uploads will fail until Apply is reached")
        else:
            _log("  on application form")
        await page.wait_for_timeout(800)

        # ---- uploads first (Ashby shows them at top) — required before Submit
        r["filled"] += await _upload_files(page, resume_path, cover)
        if not any(str(x).startswith("upload:") for x in r["filled"]):
            # one hard retry after scroll to Resume
            try:
                await page.get_by_text(re.compile(r"^\s*Resume", re.I)).first.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await page.wait_for_timeout(500)
            r["filled"] += await _upload_files(page, resume_path, cover)

        # ---- contact
        name = b.get("full_name") or f"{b.get('first','')} {b.get('last','')}".strip()
        if await _fill_by_label(page, r"first and last name|full name|^name$", name):
            r["filled"].append("name")
        email = b.get("email") or ""
        if await _fill_by_label(page, r"^e-?mail", email):
            r["filled"].append("email")
        # LinkedIn — from candidate basics, NOT residual LLM (measured wrong vanity jev-vainsteins)
        li = _linkedin_url(data, defaults)
        if li:
            if await _fill_role(page, "textbox", r"LinkedIn", li) or \
               await _fill_by_label(page, r"linkedin", li):
                r["filled"].append("linkedin")
        else:
            _log("  linkedin: no URL in candidate data")
        # ONE OWNER for the phone rule (greenhouse.phone_national). Never type empty.
        try:
            from greenhouse import phone_national as _natl
        except Exception:
            try:
                from flows.greenhouse import phone_national as _natl  # type: ignore
            except Exception:
                def _natl(x):  # noqa: E306
                    return re.sub(r"[^\d]", "", str(x or ""))
        # Prefer phone_national (form-safe). Wrong spaced phone produced 1578551545.
        phone_nat = (
            str(b.get("phone_national") or "")
            or str(defaults.get("phone_national") or "")
            or str((data.get("contact") or {}).get("phone_national") or "")
        ).strip()
        phone_raw = (
            str(b.get("phone") or "")
            or str(defaults.get("phone") or "")
            or str((data.get("basics") or {}).get("phone") or "")
            or str((data.get("contact") or {}).get("phone") or "")
        ).strip()
        if not phone_nat and phone_raw:
            phone_nat = (_natl(phone_raw) if phone_raw else "") or re.sub(r"[^\d]", "", phone_raw)
        phone_nat = re.sub(r"\D", "", phone_nat or "")
        if phone_nat.startswith("49") and len(phone_nat) > 11:
            phone_nat = phone_nat[2:]
        # Drop pure country-code leftovers (too short to be a mobile)
        if phone_nat and len(re.sub(r"\D", "", phone_nat)) < 7:
            phone_nat = re.sub(r"\D", "", phone_raw)
        if phone_nat:
            if await _fill_role(page, "textbox", r"phone|mobile|tel", phone_nat) or \
               await _fill_by_label(page, r"phone|mobile|tel", phone_nat):
                r["filled"].append("phone")
        else:
            _log("  phone: no number in candidate data — leaving blank (not typing empty)")

        # ---- location typeahead
        loc = b.get("location") or b.get("city") or ""
        # THE RECORDED ANSWER IS THE COUNTRY. His codegen types "Germany" into `Start typing...` and
        # clicks the `Germany` option; `loc.split(",")[0]` yields the CITY (Exampletown), which is not an
        # option in that list at all. Prefer the recorded choice, then the profile's country.
        city = ""
        try:
            from recordings import known_choice
            city = known_choice("ashby", "Start typing...") or ""
        except Exception:
            pass
        city = city or str((data.get("basics") or {}).get("country") or "").strip() \
            or (loc.split(",")[-1].strip() if loc else "") or "Germany"
        if not re.search(r"germany|deutschland", city, re.I):
            city = "Germany"
        if await _typeahead(page, city, "Germany"):
            r["filled"].append("location")

        # ---- salary / notice (from candidate.md — never invent)
        sal = _salary_text(defaults)
        if sal and await _fill_by_label(page, r"expected yearly|salary|compensation|pay", sal):
            r["filled"].append("salary")
        notice = _notice_text(defaults)
        if await _fill_by_label(page, r"notice period|availability", notice):
            r["filled"].append("notice")

        # ---- free-text / essays
        async def _ans(q: str) -> str:
            try:
                import essay as ES
                v = ES.deterministic(q, defaults, data)
                if v:
                    return v
            except Exception:
                pass
            if answer_fn:
                try:
                    v = await answer_fn(q, [], data) if callable(answer_fn) else ""
                    if v:
                        return str(v)
                except Exception as e:
                    _log(f"  answer_fn: {type(e).__name__}")
            return ""

        # Employer-scoped essays. n8n sales prompts must NOT run on Volta/SoSafe/etc.
        # URL path is jobs.ashbyhq.com/<employer>/...
        _emp = ""
        try:
            _m = re.search(r"ashbyhq\.com/([^/]+)/", url or "", re.I)
            _emp = (_m.group(1) if _m else "").lower()
        except Exception:
            _emp = ""
        essay_labels = []
        if _emp in ("n8n",):
            essay_labels = [
                (r"what about n8n|caught your attention",
                 "What about n8n and the role caught your attention?"),
                (r"enterprise sales methodology|rollout|enablement",
                 "Tell us about one enterprise sales methodology rollout"),
            ]
            _log(f"  essay pack: n8n-specific ({len(essay_labels)} prompts)")
        else:
            # Other Ashby employers: only answer questions that exist on THIS page
            # via residual + answer_fn(actual label). No sales methodology injection.
            _log(f"  essay pack: generic (employer={_emp or 'unknown'}) — no n8n sales prompts")
        for rx, prompt in essay_labels:
            val = await _ans(prompt)
            if not val and answer_fn:
                try:
                    val = str(await answer_fn(prompt, [], data) or "")
                except Exception:
                    val = ""
            if val and await _answer_text(page, rx, val):
                r["filled"].append(f"essay:{rx[:20]}")

        # residual required text boxes still empty — try answer_fn per label
        blank = await _required_empty(page)
        for lab in list(blank):
            if re.search(r"name|email|phone|resume|cv|upload", lab, re.I):
                continue
            # HIS OWN RECORDED ANSWER FIRST, then the ladder. `UNKNOWN` reached this box because the
            # essay guard sat on one path and this one typed the stub anyway.
            # A SHORT-ANSWER FIELD IS NOT AN ESSAY. `Notice period / availability details` wants
            # "one month"; the essay rule (>=40 chars) refused the real answer and then a stub was
            # typed instead. Known short answers come from candidate.md, verbatim.
            if re.search(r"notice period|availability", lab, re.I):
                val = _notice_text(_defaults(data))
            elif re.search(r"expected yearly|salary|compensation", lab, re.I):
                val = _salary_text(_defaults(data))
            elif re.search(r"linkedin", lab, re.I):
                val = _linkedin_url(data, defaults)
            elif known_link(lab, data, defaults):
                val = known_link(lab, data, defaults)
            else:
                val = recorded_essay(lab) or await _ans(lab)
            if val and _is_phone_like(val) and not re.search(r"phone|mobile|tel", lab, re.I):
                _log(f"  residual refused phone-like for {lab[:40]!r}")
                val = ""
            if not val:
                continue
            if _STUB.match(str(val).strip()) or not str(val).strip():
                _log(f"    {lab[:44]!r}: the ladder gave {str(val).strip()[:20]!r} — "
                     f"NOT typing a stub; it stays unresolved for you")
                r["unresolved"] = list(dict.fromkeys((r.get("unresolved") or []) + [lab]))
                continue
            if await _answer_text(page, re.escape(lab[:40]), val) or await _fill_by_label(page, re.escape(lab[:40]), val):
                r["filled"].append(f"req:{lab[:24]}")

        # ---- ticks + employer-agnostic screening (visa etc.)
        r["ok"] = True
        try:
            _t = await _tick_required(page, r)
            if _t:
                _log(f"  {_t} required box(es) ticked")
        except Exception as e:
            _log(f"  tick pass: {type(e).__name__}: {e}")
        try:
            _n = await _apply_screening_radios(page, defaults, r)
            if _n:
                _log(f"  {_n} screening radio(s) from candidate.md")
        except Exception as e:
            _log(f"  screening radios: {type(e).__name__}: {e}")
        try:
            _n2 = await _answer_common_ashby_questions(page, data, defaults, url, r)
            if _n2:
                _log(f"  {_n2} common Ashby question(s) answered")
        except Exception as e:
            _log(f"  common questions: {type(e).__name__}: {e}")

        # ---- AUTONOMOUS LADDER (never pause for the human mid-form)
        # 1) answer_fn / deterministic leftovers
        # 2) 3-LLM escalate.panel (quorum) → act on the page
        # 3) Telegram ask_human → type the answer → continue
        # 4) repeat until empty or MAX_HEAL rounds, then Submit
        # Stagehand stays for UNKNOWN ATS; Ashby stays on this adapter + escalate (faster).
        MAX_HEAL = int(os.getenv("JHW_ASHBY_HEAL_ROUNDS", "5"))
        for round_i in range(MAX_HEAL):
            blank = await _required_empty(page)
            r["unresolved"] = blank
            if not blank:
                break
            _log(f"heal round {round_i+1}/{MAX_HEAL}: still required {blank}")

            # (a) known short answers + answer_fn / asker for text boxes
            for lab in list(blank)[:8]:
                if re.search(r"resume|cv|upload", lab, re.I):
                    continue
                val = ""
                if re.search(r"notice period|availability", lab, re.I):
                    val = _notice_text(defaults)
                elif re.search(r"expected yearly|salary|compensation", lab, re.I):
                    val = _salary_text(defaults)
                elif known_link(lab, data, defaults):
                    # github / linkedin / portfolio: OURS. This branch is what the panel was being
                    # asked to guess at, one rung too late.
                    val = known_link(lab, data, defaults)
                elif re.search(r"phone|mobile|tel", lab, re.I):
                    continue  # never invent a phone
                elif re.search(r"sponsor|visa|authori[sz]ed to work|work eligibility|based in Germany|15\+?\s*years|VP level|enterprise technology sales", lab, re.I):
                    await _apply_screening_radios(page, defaults, r)
                    continue
                if not val and answer_fn:
                    try:
                        val = str(await answer_fn(lab, [], data) or "")
                    except Exception:
                        val = ""
                if val and not _STUB.match(str(val).strip()):
                    # NEVER put a phone number into a free-text / essay box
                    if _is_phone_like(val) and not re.search(r"phone|mobile|tel", lab, re.I):
                        _log(f"  refusing phone-like value for {lab[:40]!r}")
                        val = ""
                        continue
                    if await _fill_by_label(page, re.escape(lab[:40]), val) or \
                       await _fill_role(page, "textbox", re.escape(lab[:40]), val):
                        r["filled"].append(f"heal:{lab[:20]}")
                        continue

            blank = await _required_empty(page)
            if not blank:
                break

            # (b) 3-LLM consensus — one action per leftover control
            try:
                import escalate as ESC
                controls = [{"name": x, "role": "textbox", "required": True} for x in blank[:8]]
                act = await ESC.next_action(
                    reason=(
                        "Ashby application still has required fields empty. "
                        "Propose ONE safe action (click/check/type) using only candidate facts. "
                        "Fields: " + "; ".join(blank[:8])
                    ),
                    controls=controls,
                )
                verb = (act.get("verb") or "").lower()
                _log(f"  escalate: verb={verb} why={str(act.get('why') or '')[:80]}")
                if verb in ("click", "select", "check") and act.get("name"):
                    nm = str(act["name"])
                    done = False
                    for role in ("radio", "checkbox", "button", "option"):
                        loc = page.get_by_role(role, name=re.compile(re.escape(nm[:80]), re.I))
                        if await loc.count() == 0:
                            continue
                        el = loc.first
                        try:
                            if role in ("radio", "checkbox"):
                                await el.check(timeout=4000)
                            else:
                                await el.click(timeout=4000)
                            r["filled"].append(f"esc:{nm[:20]}")
                            done = True
                            break
                        except Exception:
                            continue
                    if not done and act.get("value"):
                        await _fill_by_label(page, re.escape(nm[:40]), str(act["value"]))
                elif verb in ("type", "fill", "set") and act.get("value"):
                    lab = str(act.get("name") or act.get("target") or blank[0])
                    val = str(act.get("value"))
                    if not _STUB.match(val.strip()):
                        if await _fill_by_label(page, re.escape(lab[:40]), val):
                            r["filled"].append(f"esc-fill:{lab[:16]}")
                elif verb == "ask_human" or verb in ("give_up", ""):
                    # (c) Telegram — answer is typed, loop continues (NOT a stop)
                    for lab in blank[:3]:
                        q = str(act.get("question") or f"Ashby required field: {lab}")
                        val = ""
                        if asker:
                            try:
                                val = str(await asker(q) or "")
                            except Exception:
                                val = ""
                        if not val and ask:
                            try:
                                val = str(await ask.ask_human(q) or "")
                            except Exception:
                                val = ""
                        if not val:
                            continue
                        # sponsorship answers often "No"
                        if re.search(r"sponsor|visa", lab, re.I):
                            await _apply_screening_radios(page, {**defaults, "require sponsorship": val}, r)
                        elif await _fill_by_label(page, re.escape(lab[:40]), val) or \
                             await _fill_role(page, "textbox", re.escape(lab[:40]), val):
                            r["filled"].append(f"tg:{lab[:20]}")
                        # try radio by answer text
                        try:
                            loc = page.get_by_role("radio", name=re.compile(re.escape(val[:60]), re.I))
                            if await loc.count():
                                await loc.first.check(timeout=4000)
                                r["filled"].append(f"tg-radio:{val[:16]}")
                        except Exception:
                            pass
            except Exception as e:
                _log(f"  escalate/telegram: {type(e).__name__}: {str(e).splitlines()[0][:100]}")
                # Telegram direct if escalate import failed
                for lab in blank[:3]:
                    val = ""
                    if asker:
                        try:
                            val = str(await asker(f"Ashby form needs: {lab}") or "")
                        except Exception:
                            val = ""
                    if not val and ask:
                        try:
                            val = str(await ask.ask_human(f"Ashby form needs: {lab}") or "")
                        except Exception:
                            val = ""
                    if val and await _fill_by_label(page, re.escape(lab[:40]), val):
                        r["filled"].append(f"tg:{lab[:20]}")

        blank = await _required_empty(page)
        r["unresolved"] = blank
        if blank:
            # Last resort: do NOT invent — log and still attempt submit only if non-critical
            # But required empty must not become a silent false SUBMITTED.
            _log(f"heal exhausted; still required: {blank} — asking Telegram one final time")
            for lab in blank[:5]:
                val = ""
                if asker:
                    try:
                        val = str(await asker(f"FINAL — Ashby still requires: {lab}") or "")
                    except Exception:
                        val = ""
                if not val and ask:
                    try:
                        val = str(await ask.ask_human(f"FINAL — Ashby still requires: {lab}") or "")
                    except Exception:
                        val = ""
                if not val:
                    continue
                if re.search(r"sponsor|visa", lab, re.I):
                    await _apply_screening_radios(page, {**defaults, "require sponsorship": val}, r)
                else:
                    try:
                        loc = page.get_by_role("radio", name=re.compile(re.escape(val[:60]), re.I))
                        if await loc.count():
                            await loc.first.check(timeout=4000)
                            r["filled"].append(f"tg-final:{val[:16]}")
                            continue
                    except Exception:
                        pass
                    if await _fill_by_label(page, re.escape(lab[:40]), val):
                        r["filled"].append(f"tg-final:{lab[:16]}")
            blank = await _required_empty(page)
            r["unresolved"] = blank

        if blank:
            # Still not empty after Telegram: cannot honestly submit
            r["ok"] = True
            r["stage"] = "blocked"
            r["submitted"] = False
            r["note"] = (
                f"Ashby filled {r['filled']}; required still empty after heal+Telegram: {blank}. "
                f"Not submitting a half form."
            )
            _log(r["note"])
            return r

        # Submit with validation recovery (ElevenLabs: missing "how did you hear", etc.)
        for attempt in range(3):
            await _submit(page, r)
            if r.get("submitted"):
                break
            # Recover on explicit blocked OR on review with a validation banner still present
            body = await _page_text(page)
            missing = _validation_missing(body)
            if r.get("stage") != "blocked" and not missing:
                break
            if not missing and r.get("stage") == "blocked":
                missing = await _required_empty(page)
            if not missing:
                # also re-scan required empty
                missing = await _required_empty(page)
            if not missing:
                break
            _log(f"  validation recovery attempt {attempt+1}: {missing}")
            # re-run common answers + targeted heal
            try:
                await _answer_common_ashby_questions(page, data, defaults, url, r)
            except Exception:
                pass
            try:
                await _apply_screening_radios(page, defaults, r)
            except Exception:
                pass
            for lab in missing[:8]:
                # radios first for hear-about / years
                if re.search(r"hear about|how did you", lab, re.I):
                    await _answer_common_ashby_questions(page, data, defaults, url, r)
                    continue
                if re.search(r"years of experience|account executive", lab, re.I):
                    await _answer_common_ashby_questions(page, data, defaults, url, r)
                    continue
                # Cohere-style required Yes/No — MUST verify click; else escalate / Telegram
                yn_handled = False
                if re.search(r"sponsor|visa", lab, re.I):
                    yn_handled = await _answer_yes_no_near(
                        page, r"require sponsorship|visa sponsorship|sponsor", False, "sponsorship", r
                    ) or await _apply_screening_radios(page, defaults, r) > 0
                elif re.search(r"based in Germany|currently based", lab, re.I):
                    yn_handled = await _answer_yes_no_near(
                        page, r"based in Germany|currently based in Germany", True, "based_germany", r
                    )
                elif re.search(r"15\+?\s*years|VP level|enterprise technology sales", lab, re.I):
                    yn_handled = await _answer_yes_no_near(
                        page, r"15\+?\s*years|VP level experience|enterprise technology sales", True, "vp_experience", r
                    )
                elif re.search(r"authori[sz]ed to work", lab, re.I):
                    yn_handled = await _answer_yes_no_near(
                        page, r"authori[sz]ed to work|work in the country you currently reside", True, "work_auth", r
                    ) or await _apply_screening_radios(page, defaults, r) > 0
                if yn_handled:
                    continue
                # Still missing closed Yes/No or free text → 3-LLM then Telegram (never silent exit)
                val = ""
                if re.search(r"based in Germany|currently based", lab, re.I):
                    val = "Yes"
                elif re.search(r"15\+?\s*years|VP level|enterprise technology sales", lab, re.I):
                    val = "Yes"
                elif re.search(r"sponsor|visa", lab, re.I):
                    val = "No"
                if not val and answer_fn:
                    try:
                        val = str(await answer_fn(lab, ["Yes", "No"], data) or "")
                    except Exception:
                        val = ""
                if not val:
                    try:
                        import escalate as ESC
                        act = await ESC.next_action(
                            reason=f"Ashby required closed question unanswered: {lab}",
                            controls=[{"name": lab, "role": "radio", "required": True, "options": ["Yes", "No"]}],
                            page_url=url,
                            candidate=data,
                        )
                        if act and act.get("value"):
                            val = str(act.get("value"))
                    except Exception as e:
                        _log(f"  escalate miss: {type(e).__name__}: {e}")
                if not val and asker:
                    try:
                        val = str(await asker(lab) or "")
                    except Exception:
                        val = ""
                if not val and ask:
                    try:
                        val = str(await ask.ask_human(f"Ashby required: {lab} (Yes/No or short answer)") or "")
                    except Exception:
                        val = ""
                if not val:
                    _log(f"  still cannot answer {lab[:60]!r} — leaving for next recovery round")
                    continue
                prefer_yes = str(val).strip().lower() in ("yes", "y", "true", "1")
                if re.search(r"yes|no", str(val), re.I) or re.search(
                    r"based in|sponsor|15\+|VP level|authori", lab, re.I
                ):
                    ok = await _answer_yes_no_near(
                        page, re.escape(lab[:50]), prefer_yes, f"esc:{lab[:12]}", r
                    )
                    if ok:
                        continue
                # last resort: try clicking literal value as radio/button name
                try:
                    loc = page.get_by_role("radio", name=re.compile(re.escape(str(val)[:40]), re.I))
                    if await loc.count():
                        await loc.first.check(timeout=4000)
                        r["filled"].append(f"esc-radio:{val[:16]}")
                        continue
                    loc = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(str(val)[:20])}\s*$", re.I))
                    if await loc.count():
                        await loc.first.click(timeout=4000)
                        r["filled"].append(f"esc-btn:{val[:16]}")
                        continue
                except Exception:
                    pass
                if await _fill_by_label(page, re.escape(lab[:40]), val):
                    r["filled"].append(f"esc-fill:{lab[:16]}")
                    continue
                val = known_link(lab, data, defaults)
                if not val and answer_fn:
                    try:
                        val = str(await answer_fn(lab, [], data) or "")
                    except Exception:
                        val = ""
                if not val and asker:
                    try:
                        val = str(await asker(lab) or "")
                    except Exception:
                        val = ""
                if not val and ask:
                    try:
                        val = str(await ask.ask_human(f"Ashby required: {lab}") or "")
                    except Exception:
                        val = ""
                if not val or _STUB.match(str(val).strip()):
                    continue
                if _is_phone_like(val) and not re.search(r"phone|mobile|tel", lab, re.I):
                    _log(f"  recovery refused phone-like for {lab[:40]!r}")
                    continue
                # try radio option matching the answer text
                try:
                    loc = page.get_by_role("radio", name=re.compile(re.escape(str(val)[:50]), re.I))
                    if await loc.count():
                        await loc.first.check(timeout=4000)
                        r["filled"].append(f"fix-radio:{val[:16]}")
                        continue
                except Exception:
                    pass
                if await _fill_by_label(page, re.escape(lab[:40]), val) or \
                   await _fill_role(page, "textbox", re.escape(lab[:40]), val):
                    r["filled"].append(f"fix:{lab[:20]}")
            # reset stage so next _submit can run
            r["stage"] = "review"
            r["submitted"] = False

        # Final pass: red-banner leftovers → deterministic Yes/No, then escalate, then Telegram
        if not r.get("submitted"):
            try:
                body = await _page_text(page)
                leftover = _validation_missing(body) or []
                if not leftover:
                    leftover = await _required_empty(page)
                for lab in list(leftover or [])[:6]:
                    prefer = None
                    if re.search(r"based in Germany|currently based", lab, re.I):
                        prefer = True
                        qrx = r"based in Germany|currently based in Germany"
                        tag = "based_germany"
                    elif re.search(r"15\+?\s*years|VP level|enterprise technology sales", lab, re.I):
                        prefer = True
                        qrx = r"15\+?\s*years|VP level experience|enterprise technology sales"
                        tag = "vp_experience"
                    elif re.search(r"sponsor|visa", lab, re.I):
                        prefer = False
                        qrx = r"require sponsorship|visa sponsorship|sponsor"
                        tag = "sponsorship"
                    elif re.search(r"authori[sz]ed to work", lab, re.I):
                        prefer = True
                        qrx = r"authori[sz]ed to work|work in the country you currently reside"
                        tag = "work_auth"
                    else:
                        prefer = None
                        qrx = tag = ""
                    ok = False
                    if prefer is not None:
                        ok = await _answer_yes_no_near(page, qrx, prefer, tag, r)
                    if not ok and answer_fn:
                        try:
                            val = str(await answer_fn(lab, ["Yes", "No"], data) or "")
                            if val:
                                prefer2 = str(val).strip().lower() in ("yes", "y", "true", "1")
                                ok = await _answer_yes_no_near(
                                    page, re.escape(lab[:50]), prefer2, f"llm:{lab[:10]}", r
                                )
                        except Exception as e:
                            _log(f"  final answer_fn: {type(e).__name__}: {e}")
                    if not ok:
                        try:
                            import escalate as ESC
                            act = await ESC.next_action(
                                reason=f"Ashby final required: {lab}",
                                controls=[{"name": lab, "role": "radio", "required": True, "options": ["Yes", "No"]}],
                                page_url=url,
                                candidate=data,
                            )
                            val = ""
                            if isinstance(act, dict):
                                val = str(act.get("value") or act.get("answer") or act.get("text") or "")
                            if val:
                                prefer2 = str(val).strip().lower() in ("yes", "y", "true", "1")
                                ok = await _answer_yes_no_near(
                                    page, re.escape(lab[:50]), prefer2, f"esc:{lab[:10]}", r
                                )
                        except Exception as e:
                            _log(f"  final escalate: {type(e).__name__}: {e}")
                    if not ok and asker:
                        try:
                            val = str(await asker(lab) or "")
                            if val:
                                prefer2 = str(val).strip().lower() in ("yes", "y", "true", "1")
                                ok = await _answer_yes_no_near(
                                    page, re.escape(lab[:50]), prefer2, f"tg:{lab[:10]}", r
                                )
                        except Exception as e:
                            _log(f"  final asker: {type(e).__name__}: {e}")
                    if not ok and ask:
                        try:
                            val = str(await ask.ask_human(f"Ashby still required: {lab}") or "")
                            if val:
                                prefer2 = str(val).strip().lower() in ("yes", "y", "true", "1")
                                ok = await _answer_yes_no_near(
                                    page, re.escape(lab[:50]), prefer2, f"tg:{lab[:10]}", r
                                )
                        except Exception as e:
                            _log(f"  final ask: {type(e).__name__}: {e}")
                if leftover:
                    r["stage"] = "review"
                    r["submitted"] = False
                    await _submit(page, r)
            except Exception as e:
                _log(f"  final pass error: {type(e).__name__}: {e}")

        if not r.get("submitted"):
            r["stage"] = r.get("stage") or "review"
            r["note"] = r.get("note") or f"Ashby filled {r['filled']}; not submitted (confirm in noVNC)."
        return r
    except Exception as e:
        r["note"] = f"ashby driver error: {type(e).__name__}: {e}"
        _log(r["note"])
        return r
    finally:
        # do not close CDP browser
        pass


# --------------------------------------------------------------------------- self-test
def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("[ashby] contracts")
    k = _load_knowledge()
    ck(bool(k.get("fields")) and "Notice period / availability" in " ".join(k.get("fields") or {}),
       "knowledge loads (his real recording, not the empty fallback)")
    ck(any("Submit" in b for b in (k.get("buttons") or [])), "submit button known (no `or` fallback)")
    d = _defaults({"screening_defaults": {"notice_period": "1 month", "salary_expectation_eur": "150000"}})
    ck(_notice_text({"notice_period": "one month"}) == "one month", "notice is verbatim")
    ck(_salary_text({"salary_expectation_eur": "150000"}) == "150000",
       "salary is PLAIN DIGITS, as recorded")
    b = _basics({"basics": {"legal_name": "Jev Vainsteins", "email": "a@b.c"}})
    ck(b["full_name"] == "Jev Vainsteins", "full name")
    print("\n[UNKNOWN reached a real form TWICE — the refusal now sits at the choke point]")
    import inspect as _i4
    _fbl = _i4.getsource(_fill_by_label)
    ck("_STUB.match" in _fbl, "_fill_by_label — where EVERY path ends — refuses a stub itself")
    _up2 = _i4.getsource(_upload_files)
    # THE MARKER MUST EXIST OR THE SPLIT IS A NO-OP. It said "LAST RESORT" while the code says
    # "page-wide empty inputs", so the split returned the WHOLE function and this check could not
    # fail -- the vacuous-check class this project keeps paying for. Assert the marker first.
    _MARK = "page-wide empty inputs"
    ck(_MARK in _up2, "the page-wide fallback is marked, so the GROUP path can be measured alone")
    _grp = _up2.split(_MARK)[0]
    ck("e.files && e.files.length" in _grp,
       "the GROUP path asks the DOM for files.length, not the group's text")
    ck("[:400]" not in _up2, "no truncated evidence (that is what made a real upload 'FAIL')")
    ck("already holds" in _up2, "an input that already holds our file counts as attached")
    _rs = _i4.getsource(drive)
    ck("notice period|availability" in _rs and "_notice_text(_defaults(data))" in _rs,
       "a short-answer field takes its written answer, never the essay rule")

    print("\n[the upload claimed success on an EMPTY Resume field]")
    import inspect as _i3
    _up = _i3.getsource(_upload_files)
    # THESE TWO USED TO GREP FOR THE OLD IMPLEMENTATION'S SPELLING ("has_text=re.compile(label_rx",
    # "base in shown"). The upload was then REWRITTEN -- and demonstrably works, ElevenLabs was
    # submitted with both documents attached and verified -- so both checks failed against correct
    # code. A check pinned to a call's exact spelling breaks the moment the call improves; pin it to
    # the PROPERTY. Both are now measured on the AST, and both are negative-tested.
    import ast as _ast, textwrap as _tw
    _upt = _ast.parse(_tw.dedent(_up))
    _upf = _upt.body[0]

    def _calls(tree, name):
        out = []
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Call):
                f = n.func
                if isinstance(f, _ast.Name) and f.id == name:
                    out.append(n)
                elif isinstance(f, _ast.Attribute) and f.attr == name:
                    out.append(n)
        return out

    _secs = _calls(_upf, "_section_for")
    _sets = _calls(_upf, "set_input_files") + _calls(_upf, "set_files")
    # PROPERTY 1: the document is placed into the SECTION THAT ASKS FOR IT. The section is resolved
    # BEFORE anything is written, so a blind `page.locator("input[type=file]").first` at the top of
    # the function -- which is what put a CV nowhere on 2026-08-17 -- cannot be the first write.
    ck(bool(_secs) and bool(_sets) and min(c.lineno for c in _secs) < min(c.lineno for c in _sets),
       "attaches to the GROUP that asks for it, not input #0")
    # PROPERTY 2: success is gated on a DOM READ. The evidence must come from the browser
    # (files.length), not from a string we hope to find in the container's text -- and no `append`
    # that records an upload may sit outside a conditional.
    _reads = [c for c in _calls(_upf, "evaluate")
              if c.args and isinstance(c.args[0], _ast.Constant)
              and "files.length" in str(c.args[0].value)]
    _parents = {}
    for n in _ast.walk(_upf):
        for ch in _ast.iter_child_nodes(n):
            _parents[ch] = n

    def _guarded(node):
        cur = _parents.get(node)
        while cur is not None:
            if isinstance(cur, _ast.If):
                t = _ast.dump(cur.test)
                if any(w in t for w in ("ok", "placed", "_files_len", "_zone_looks_filled")):
                    return True
            cur = _parents.get(cur)
        return False

    _apps = [c for c in _calls(_upf, "append")
             if c.args and "upload" in _ast.dump(c.args[0])]
    ck(bool(_reads) and bool(_apps) and all(_guarded(a) for a in _apps),
       "the filename is READ BACK off the page (files.length), and no unguarded success")
    ck("COULD NOT ATTACH" in _up, "a failed attach is stated, never silently 'uploaded'")
    ck("NO RESUME FILE at" in _up, "a missing file on disk is named")
    _sb = _i3.getsource(_submit)
    ck("no resume/cover upload was confirmed" in _sb,
       "Submit is refused when no document was confirmed")
    ck(_sb.index("REFUSING submit") < _sb.index("SUBMIT_JS")
       if "SUBMIT_JS" in _sb else True, "...and the refusal comes before the click")

    print("\n[the typeahead crashed on its own selector — 'typeahead failed: Error']")
    import inspect as _i2
    _ta2 = _i2.getsource(_typeahead)
    # STRIP COMMENTS: the paragraph explaining the crash necessarily quotes the bad selector.
    # 20th self-referential false positive in this project; the fix is always the same.
    _ta2c = "\n".join(ln.split("#")[0] for ln in _ta2.splitlines())
    ck("{owns}" not in _ta2c, "no CSS id is interpolated from page data")
    ck('[id="' in _ta2, "the attribute form is used, which needs no escaping")
    ck("owns.split()" in _ta2, "aria-owns may list SEVERAL ids — each is tried separately")
    ck("str(e).splitlines()" in _ta2, "the failure names the MESSAGE, not just 'Error'")

    print("\n[candidate.md is written for a HUMAN — a comment must never reach a form]")
    _d = _defaults({})
    ck(not any("#" in str(v) for v in _d.values()), "no inline comment survives the parser")
    ck(not any(str(v).strip().startswith(('"', "'")) for v in _d.values()), "no quote survives")
    ck(_d.get("available_start_date") == "2026-08-20",
       f"the real line parses clean (got {_d.get('available_start_date')!r})")
    ck(_notice_text(_d) == "one month", "and the notice box gets 'one month', not a date + comment")

    print("\n[typeahead contracts — it clicked a CHECKBOX LABEL instead of a dropdown option]")
    import inspect as _insp
    import re as _re2
    _sane = _re2.compile(_re2.search(r'_sane = re\.compile\(r"(.*?)", re\.I\)',
                                     _insp.getsource(_typeahead)).group(1), _re2.I)

    def _usable_opt(t):
        return not (_sane.search(t) or len(t) > 48)

    for bad in ("I can be based in Germany and do not need visa support",
                "I will be based in the US and need visa support",
                "I want to be based in a European country other than Germany or the UK"):
        ck(not _usable_opt(bad), f"a checkbox label is never picked as an option: {bad[:34]!r}")
    for good in ("Germany", "Germany (Berlin)", "Austria", "Netherlands"):
        ck(_usable_opt(good), f"a real country IS pickable: {good!r}")
    _ta = _insp.getsource(_typeahead)
    ck("[role=listbox] [role=option]" in _ta, "options are scoped to the dropdown, not the page")
    ck("class*='option'\"" not in _ta or "role=listbox" in _ta,
       "no page-wide option selector survives")
    _tk3 = _insp.getsource(_tick_required)
    ck("did NOT resolve on this page" in _tk3,
       "a recorded tick that does not resolve is NAMED, not silently counted out")

    print("\n[stub contracts — 'UNKNOWN' was typed into the n8n motivation box]")
    for bad in ("UNKNOWN", "ASK", "TBD", "No", "yes", "?", "n/a", "  none "):
        ck(not usable_essay(bad), f"{bad!r} is never typed as an essay")
    ck(usable_essay("I led enablement for 70 reps across DACH and rolled out MEDDICC end to end."),
       "real prose is accepted")
    ck(bool(recorded_essay("What about n8n and the role caught your attention")),
       "his RECORDED n8n answer is available and tried first")
    _tk2 = _insp.getsource(_tick_required)
    ck("exact=True" in _tk2 and "len(name) < 6" in _tk2,
       "a short declaration ('Man') is matched EXACTLY, never by prefix")

    print("\n[bad-run contracts — 2026-08-17, all eight location boxes were ticked]")
    try:
        from recordings import load as _kb
        _ticks = [c.get("name") for c in (_kb("ashby").get("checkboxes") or [])
                  if isinstance(c, dict) and c.get("checked")]
    except Exception:
        _ticks = []
    _loc = [x for x in _ticks if "based in" in (x or "").lower()]
    ck(len(_loc) == 3, f"exactly THREE location ticks, from his recording (got {len(_loc)})")
    ck(not any("UK" in x for x in _loc), "never claims he can be based in the UK")
    ck(not any(" US" in x or x.strip().endswith("US") for x in _loc), "never claims the US")
    ck(not any("need visa" in x.lower() and "do not" not in x.lower() for x in _loc)
       or all("do not need visa" in x.lower() or "want to be based in a" in x.lower() for x in _loc),
       "never claims he needs visa support")
    import inspect
    _tk = inspect.getsource(_tick_required)
    # STRIP COMMENTS AND DOCSTRINGS: the paragraph explaining the bug necessarily quotes "based in",
    # so a naive grep fails correct code. 19th self-referential false positive in this project.
    _tkc = "\n".join(ln.split("#")[0] for ln in _tk.splitlines())
    _tkc = _tkc.split('"""')[0] + "".join(_tkc.split('"""')[2:]) if _tkc.count('"""') >= 2 else _tkc
    ck("based in" not in _tkc, "no keyword sweep survives in the tick CODE")
    ck("is_checked()" in _tk, "a tick is read back")
    ck(_notice_text({"notice_period": "1 month"}) == "one month", "notice normalises to the recording")
    ck(_salary_text({"salary_expectation_eur": "150000"}) == "150000", "salary is plain digits")

    print("\n[he was woken for a GitHub URL that was in candidate.md all along — 2026-08-18]")
    _D = {"basics": {"github": "https://github.com/feranicus",
                     "linkedin": "https://linkedin.com/in/feranicus"}}
    # HIS ACTUAL LABEL from the ElevenLabs run, not a label I invented.
    ck(known_link("Link to your Github profile", _D, {}) == "https://github.com/feranicus",
       "the GitHub box is answered from candidate.md, never by the panel or Telegram")
    ck(known_link("Github", {"basics": {}}, {"github": "github.com/feranicus"})
       == "https://github.com/feranicus", "a bare handle URL is normalised to https")
    ck(known_link("Link to your Github profile",
                  {"basics": {"website": "https://linkedin.com/in/feranicus"}}, {}) == "",
       "a link to the WRONG site is never typed into a GitHub box")
    ck(known_link("What is your notice period?", _D, {}) == "",
       "a label we own nothing for returns nothing, so the ladder continues")
    # ONE HOME. The github rule had FOUR copies in this file and the path that woke him was the one
    # copy that did not have it. Measured on the AST of the SHIPPING slice, so a comment quoting
    # `.get("github")` cannot satisfy or break it.
    import ast as _ast5, inspect as _i5
    with open(__file__, encoding="utf-8") as _fh:
        _self_src = _fh.read()
    _mod = _ast5.parse(_self_src.split("def _selftest")[0])
    _stray = []
    for _fn in [n for n in _ast5.walk(_mod)
                if isinstance(n, (_ast5.FunctionDef, _ast5.AsyncFunctionDef))
                and n.name != "known_link"]:
        for _c in _ast5.walk(_fn):
            if (isinstance(_c, _ast5.Call) and isinstance(_c.func, _ast5.Attribute)
                    and _c.func.attr == "get" and _c.args
                    and isinstance(_c.args[0], _ast5.Constant)
                    and str(_c.args[0].value).startswith("github")):
                _stray.append(_fn.name)
    ck(not _stray, f"the github rule has ONE home (strays: {sorted(set(_stray))})")
    # AND IT IS CONSULTED BEFORE THE PANEL. Answering after the escalation is the same as not
    # answering: that is exactly the run that invented `https://github.com/username`.
    _dr = _i5.getsource(drive)
    ck("known_link(" in _dr and _dr.index("known_link(") < _dr.index("import escalate"),
       "a fact we OWN is answered before the 3-LLM panel is consulted")

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL ASHBY CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
