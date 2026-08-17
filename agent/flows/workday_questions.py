"""Application-question fill for Workday (dates, salary). Intive 2026-08-17."""
from __future__ import annotations
import re
from datetime import date, timedelta

_START_RX = re.compile(
    r"desired start|available.?start|start date|earliest start|"
    r"when (can|could) you (start|begin)|date you (can|could) start", re.I)
_SALARY_RX = re.compile(
    r"desired salary|salary expectation|expected (salary|pay|comp)|compensation|remuneration", re.I)
_DATE_PH = re.compile(r"mm\s*[/.\-]\s*dd|dd\s*[/.\-]\s*mm|yyyy", re.I)

def notice_weeks(defaults):
    raw = ""
    for k, v in (defaults or {}).items():
        if "notice" in str(k).lower() and str(v).strip():
            raw = str(v).lower(); break
    if not raw: return 4
    m = re.search(r"(\d+)\s*week", raw)
    if m: return max(1, int(m.group(1)))
    m = re.search(r"(\d+)\s*month", raw)
    if m: return max(4, int(m.group(1)) * 4)
    if "immediate" in raw or "asap" in raw: return 1
    return 4

def iso_start(defaults=None):
    for k, v in (defaults or {}).items():
        if re.search(r"available_start|start.?date|earliest.?start", str(k), re.I):
            s = str(v).strip().strip('"')
            if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
                try:
                    if date.fromisoformat(s) > date.today():
                        return s
                except ValueError:
                    pass
    return (date.today() + timedelta(weeks=notice_weeks(defaults))).isoformat()

def format_for_placeholder(iso, placeholder=""):
    """Render the date in the ORDER AND SEPARATOR the box advertises.

    MEASURED 2026-08-17: the incoming version matched only `/` and `-`, so a German tenant's
    `DD.MM.YYYY` fell through to the final return and produced `09/14/2026` — wrong order AND wrong
    separator, typed into a real employer's form where 09 would be read as the day. The separator has
    to come from the placeholder too, not be assumed."""
    y, m, d = iso.split("-")
    ph = re.sub(r"\s+", "", (placeholder or "")).lower()
    sep = next((c for c in ph if c in "/.-"), "/")
    if ph.startswith("yyyy"):
        return iso if sep == "-" else sep.join((y, m, d))
    if re.search(r"dd\s*[/.\-]\s*mm", ph):
        return sep.join((d, m, y))
    if re.search(r"mm\s*[/.\-]\s*dd", ph):
        return sep.join((m, d, y))
    return f"{m}/{d}/{y}"          # no usable hint -> US order, Workday's default

_PLACEHOLDER = re.compile(r"^\s*(ask|tbd|n/?a|none|unknown|\?+|-+)\s*$", re.I)


def salary_text(defaults):
    """His written figure, or '' so the ladder asks him. A PLACEHOLDER IS NOT AN ANSWER.

    `candidate.md` really does say `salary_expectation: ASK` under logistics, and the first cut of
    this function returned that string — which would have typed the word ASK into an employer's
    salary box. The guard has to sit on the RESULT: I first put it on one branch only, and the
    `essay.deterministic` path walked straight past it."""
    try:
        import essay as ES
        v = ES.deterministic("What is your desired Salary?", defaults or {}, None)
        if v and not _PLACEHOLDER.match(str(v)):
            return v
    except Exception:
        pass
    for k, v in (defaults or {}).items():
        if re.search(r"salary|compensation", str(k), re.I) and str(v).strip().strip('"'):
            raw = str(v).strip().strip('"')
            # 'ASK' IS NOT AN ANSWER — and `candidate.md` says exactly that under `logistics`
            # (`salary_expectation: ASK`). Returning it would type the word ASK into an employer's
            # salary box. A placeholder means the ladder must ask HIM, so return nothing.
            if re.fullmatch(r"(ask|tbd|n/?a|unknown|\?+|-+)", raw, re.I):
                continue
            digits = re.sub(r"[^\d]", "", raw)
            return f"{int(digits):,} EUR gross per annum".replace(",", ".") if digits else raw
    return ""

def screening_defaults(data=None):
    out = {}
    try:
        from workday_wd import candidate_answers
        out.update(candidate_answers() or {})
    except Exception:
        pass
    if data:
        sd = data.get("screening_defaults") or {}
        if isinstance(sd, dict):
            out.update({str(k): v for k, v in sd.items() if v not in (None, "")})
    return out

def named_fields_from_error(err):
    found = []
    for rx in (r"field\s+(.+?)\s+is required", r"Error\s*[-–—]\s*(.+?)(?:\s*\||$)"):
        for m in re.finditer(rx, err or "", re.I):
            t = re.sub(r"\s+The fie.*$", "", re.sub(r"\s+", " ", m.group(1))).strip(" .:")
            if t and len(t) > 2: found.append(t)
    uniq, low = [], []
    for f in found:
        fl = f.lower()
        if any(fl == u or fl.startswith(u) or u.startswith(fl) for u in low):
            continue
        uniq.append(f); low.append(fl)
    return uniq

async def _label_of(page, el):
    try:
        return (await el.evaluate("""e => {
          const n = t => (t||'').replace(/\\s+/g,' ').trim();
          if (e.getAttribute('aria-label')) return n(e.getAttribute('aria-label'));
          if (e.id) { const l = document.querySelector('label[for="'+CSS.escape(e.id)+'"]');
            if (l) return n(l.innerText); }
          const w = e.closest('fieldset,[role=group],div,li');
          if (w) { const lab = w.querySelector('label,legend,h3,h4');
            if (lab) return n(lab.innerText).slice(0,120); }
          return n(e.getAttribute('placeholder')||e.name||'');
        }""")) or ""
    except Exception:
        return ""

async def _fill_date_box(page, el, iso, placeholder, log):
    shown = format_for_placeholder(iso, placeholder)
    got = ""
    try:
        await el.evaluate("(e) => e.setAttribute('data-jhw-q','9001')")
        import llm_driver as LD
        got = await LD._commit_date(page, "[data-jhw-q='9001']", shown, "start date")
        if not (got or "").strip():
            got = await LD._pick_date_in_calendar(page, "[data-jhw-q='9001']", shown, "start date")
    except Exception as e:
        log(f"    date helper: {type(e).__name__}: {e}")
    if not (got or "").strip():
        try:
            await el.click(timeout=4000)
            await el.fill(shown, timeout=4000)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(400)
            got = await el.input_value()
        except Exception as e:
            log(f"    date type failed: {type(e).__name__}")
            return False
    ok = bool((got or "").strip())
    log(f"    start_date   wrote {shown!r}  read-back {got!r}  ({'ok' if ok else 'EMPTY'})")
    return ok

async def fill_page_questions(page, data, r, log=print):
    defaults = screening_defaults(data)
    wrote = 0
    r.setdefault("filled", []); r.setdefault("unresolved", [])
    try:
        boxes = page.locator("input:visible, textarea:visible")
        n = min(await boxes.count(), 30)
    except Exception:
        return 0
    iso, sal = iso_start(defaults), salary_text(defaults)
    for i in range(n):
        el = boxes.nth(i)
        try:
            if not await el.is_visible(): continue
            typ = (await el.get_attribute("type") or "").lower()
            if typ in ("hidden","file","checkbox","radio","submit","button","password"): continue
            ph = await el.get_attribute("placeholder") or ""
            lab = await _label_of(page, el)
            blob = f"{lab} {ph}"
            try: cur = (await el.input_value() or "").strip()
            except Exception: cur = ""
            if cur and cur.upper() not in ("MM/DD/YYYY","DD/MM/YYYY","YYYY-MM-DD"):
                continue
            if _START_RX.search(blob) or _DATE_PH.search(ph) or typ == "date":
                if await _fill_date_box(page, el, iso, ph, log):
                    r["filled"].append("start_date"); wrote += 1
                continue
            if _SALARY_RX.search(blob) and sal:
                await el.click(timeout=3000); await el.fill(sal, timeout=4000)
                back = (await el.input_value() or "").strip()
                if back:
                    log(f"    salary       = {back[:60]!r}")
                    r["filled"].append("salary"); wrote += 1
        except Exception:
            continue
    try:
        import workday_wd as WD
        extra = await WD.wd_sweep(page, defaults, r)
        if extra: wrote += extra
    except Exception as e:
        log(f"    wd_sweep: {type(e).__name__}: {e}")
    return wrote

async def repair_named_errors(page, data, r, err, log=print):
    names = named_fields_from_error(err)
    if names: log(f"    the site named: {names}")
    if any(_START_RX.search(n) or "date" in n.lower() for n in names) or _START_RX.search(err or ""):
        n = await fill_page_questions(page, data, r, log=log)
        log(f"    repair wrote {n} field(s) after start-date error")
        # IT RETURNED TRUE HAVING WRITTEN NOTHING. `repair wrote 0 field(s)` and then `True`, so the
        # caller believed the field was fixed, SKIPPED its own calendar attempt, and pressed Next
        # again — nine times. A function that reports success for work it did not do is worse than one
        # that fails: it disables every fallback behind it.
        return n > 0
    n = await fill_page_questions(page, data, r, log=log)
    return n > 0

def _selftest() -> int:
    import datetime as _dt
    fails = []

    def ck(name, ok, d=""):
        print(("  OK   " if ok else "  FAIL ") + name + (f"   :: {d}" if not ok else ""))
        if not ok:
            fails.append(name)

    print("[1] the date is rendered in the order AND separator the BOX advertises")
    iso = "2026-09-16"
    for ph, want in (("MM/DD/YYYY", "09/16/2026"), ("mm/dd/yyyy", "09/16/2026"),
                     ("DD.MM.YYYY", "16.09.2026"), ("DD/MM/YYYY", "16/09/2026"),
                     ("DD-MM-YYYY", "16-09-2026"), ("YYYY-MM-DD", "2026-09-16"),
                     ("", "09/16/2026")):
        got = format_for_placeholder(iso, ph)
        ck(f"{ph or '(no hint)':<12} -> {want}", got == want, got)

    print("\n[2] the start date comes from HIS notice period, and is never in the past")
    for notice, lo, hi in (("1 month", 25, 35), ("2 months", 55, 65), ("immediately", 0, 20),
                           ("", 20, 40)):
        d = _dt.date.fromisoformat(iso_start({"notice_period": notice}))
        days = (d - _dt.date.today()).days
        ck(f"notice {(notice or '(unset)'):<12} -> {d} ({days}d)", lo <= days <= hi, days)
    ck("never a date in the past",
       _dt.date.fromisoformat(iso_start({"notice_period": "1 month"})) > _dt.date.today())

    print("\n[3] the site's own error names the field, ONCE")
    real = ("Error: The field What is your desired start date? is required and must have a value. | "
            "Error-What is your desired start date? The field What is your desired start date? is "
            "required and must have a value.")
    n = named_fields_from_error(real)
    ck("the start-date field is named", any("desired start date" in x.lower() for x in n), n)
    ck("...and he is asked about it once, not twice", len(n) == 1, n)
    ck("nothing is invented from a clean page", named_fields_from_error("") == [])

    print("\n[4] the salary answer is HIS written figure, never a model's guess")
    ck("from his screening defaults", "150" in salary_text({"salary_expectation_eur": "150000"}))
    ck("and 'ASK' is never typed into a form", "ASK" not in salary_text({"salary_expectation": "ASK"}))

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} WORKDAY-QUESTION CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL WORKDAY QUESTION CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--logic" in sys.argv else 0)