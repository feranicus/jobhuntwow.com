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
                    out[m.group(1).strip()] = m.group(2).strip().strip('"')
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
    """Ashby often uses placeholder or nearby label, not perfect a11y names."""
    if not value:
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
    filled = []
    files = []
    if resume_path and os.path.isfile(resume_path):
        files.append(resume_path)
    if cover_path and os.path.isfile(cover_path):
        files.append(cover_path)
    # also look next to resume for cover_letter.pdf
    if resume_path and not cover_path:
        sib = os.path.join(os.path.dirname(resume_path), "cover_letter.pdf")
        if os.path.isfile(sib):
            files.append(sib)
    if not files:
        return filled
    try:
        inputs = page.locator("input[type=file]")
        n = await inputs.count()
        for i, path in enumerate(files):
            if i >= n:
                break
            await inputs.nth(i).set_input_files(path)
            _log(f"  uploaded {os.path.basename(path)} -> file input #{i}")
            filled.append(f"upload:{os.path.basename(path)}")
            await page.wait_for_timeout(600)
    except Exception as e:
        _log(f"  upload failed: {type(e).__name__}: {e}")
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
        opt = page.get_by_role("option")
        if await opt.count() == 0:
            # listbox items
            opt = page.locator("[role=option], [class*='option']")
        if await opt.count() == 0:
            await page.keyboard.press("Enter")
            _log(f"  typeahead typed {query!r} (no option list)")
            return True
        target = pick_contains or query
        chosen = None
        for i in range(min(await opt.count(), 12)):
            t = (await opt.nth(i).inner_text() or "").strip()
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
        _log(f"  typeahead failed: {type(e).__name__}")
        return False


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
            val = value or ""
            if len(val.strip()) < 40:
                try:
                    from recordings import known_value
                    rec = known_value("ashby", lab[:60]) or ""
                    if len(rec.strip()) >= 40:
                        _log(f"  essay~{label_rx[:30]!r}: model gave {len(val.strip())} chars — "
                             f"using the RECORDED answer ({len(rec)} chars)")
                        val = rec
                except Exception:
                    pass
            if len(val.strip()) < 40:
                _log(f"  essay~{label_rx[:30]!r}: only {len(val.strip())} chars available — "
                     f"NOT typing a stub, leaving it for the ladder")
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
            el = page.get_by_role("checkbox", name=name, exact=False).first
            if not await el.count():
                el = page.get_by_role("radio", name=name, exact=False).first
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
    log(f"  {done} recorded choice(s) applied (not a bulk tick)")
    return done


async def _submit(page, r: dict) -> bool:
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
        await btn.first.click(timeout=8000)
        _log("clicked Submit Application")
        await page.wait_for_timeout(3000)
        body = ""
        try:
            body = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0,5000)") or ""
        except Exception:
            pass
        url = page.url or ""
        ok = bool(re.search(
            r"thank you|thanks for|application (has been )?(submitted|received|sent)|"
            r"successfully submitted|we (have )?received|congratulat",
            body, re.I)) or bool(re.search(r"success|confirm|thank", url, re.I))
        if ok:
            r["submitted"] = True
            r["stage"] = "submitted"
            _log(f"SUBMITTED — site confirmed")
        else:
            r["submitted"] = False
            r["stage"] = "review"
            _log(f"submit clicked, confirmation unclear. url={url[:80]}")
        return ok
    except Exception as e:
        _log(f"submit failed: {type(e).__name__}: {e}")
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
        await page.wait_for_timeout(2000)

        # ---- uploads first (Ashby shows them at top)
        r["filled"] += await _upload_files(page, resume_path, cover)

        # ---- contact
        name = b.get("full_name") or f"{b.get('first','')} {b.get('last','')}".strip()
        if await _fill_by_label(page, r"first and last name|full name|^name$", name):
            r["filled"].append("name")
        email = b.get("email") or ""
        if await _fill_by_label(page, r"^e-?mail", email):
            r["filled"].append("email")
        # ONE OWNER for the phone rule. This file had its own, and it produced '5785541545' —
        # it stripped `+49 1` instead of `+49`, losing the leading 1 of the national number. The
        # measured truth (his Workday recording, four attempts) is 15785541545.
        try:
            from greenhouse import phone_national as _natl
        except Exception:
            from flows.greenhouse import phone_national as _natl      # type: ignore
        phone = str(b.get("phone") or "")
        phone_nat = _natl(phone) or re.sub(r"[^\d]", "", phone)
        if await _fill_by_label(page, r"phone|mobile|tel", phone_nat or phone):
            r["filled"].append("phone")

        # ---- location typeahead
        loc = b.get("location") or b.get("city") or ""
        # THE RECORDED ANSWER IS THE COUNTRY. His codegen types "Germany" into `Start typing...` and
        # clicks the `Germany` option; `loc.split(",")[0]` yields the CITY (Friedberg), which is not an
        # option in that list at all. Prefer the recorded choice, then the profile's country.
        city = ""
        try:
            from recordings import known_choice
            city = known_choice("ashby", "Start typing...") or ""
        except Exception:
            pass
        city = city or str((data.get("basics") or {}).get("country") or "").strip() \
            or (loc.split(",")[-1].strip() if loc else "")
        if city and await _typeahead(page, city, city):
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

        essay_labels = [
            (r"what about n8n|caught your attention|why (this|the) role|why (do you )?want",
             "What about n8n and the role caught your attention?"),
            (r"enterprise sales methodology|rollout|enablement",
             "Tell us about one enterprise sales methodology rollout"),
            # REMOVED an invented third question: `r"tell us about|describe a time|example of"` is
            # unanchored and matches the REAL "Tell us about one enterprise sales methodology rollout"
            # that the entry above has already answered — so it either no-ops and inflates `filled`,
            # or overwrites a correct answer with a fabricated one. Anchored to the real label only.
            (r"^tell us about one enterprise",
             "Tell us about a relevant example from your experience"),
        ]
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
            val = await _ans(lab)
            if not val:
                continue
            if await _answer_text(page, re.escape(lab[:40]), val) or await _fill_by_label(page, re.escape(lab[:40]), val):
                r["filled"].append(f"req:{lab[:24]}")

        blank = await _required_empty(page)
        if blank:
            _log(f"still required+empty after deterministic pass: {blank}")
            r["unresolved"] = blank
            # one LLM/human pass for leftovers only — never submit yet
            for lab in blank[:5]:
                val = ""
                if answer_fn:
                    try:
                        val = str(await answer_fn(lab, [], data) or "")
                    except Exception:
                        pass
                if not val and asker:
                    try:
                        val = str(await asker(lab) or "")
                    except Exception:
                        pass
                if not val and ask:
                    try:
                        val = str(await ask.ask_human(f"Ashby form needs: {lab}") or "")
                    except Exception:
                        pass
                if val:
                    await _fill_by_label(page, re.escape(lab[:40]), val)
                    r["filled"].append(f"asked:{lab[:20]}")

        blank = await _required_empty(page)
        r["unresolved"] = blank
        if blank:
            r["ok"] = True
            r["stage"] = "paused"
            r["note"] = f"Ashby filled {r['filled']}; still required: {blank}. Stopped before Submit."
            _log(r["note"])
            return r

        r["ok"] = True
        try:
            _t = await _tick_required(page, r)
            if _t:
                _log(f"  {_t} required box(es) ticked")
        except Exception as e:
            _log(f"  tick pass: {type(e).__name__}: {e}")
        await _submit(page, r)
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

    print("=" * 50)
    if fails:
        print(f"[X] {len(fails)} failed")
        return 1
    print("ALL ASHBY CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
