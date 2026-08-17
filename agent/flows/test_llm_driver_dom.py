"""Regression test for the two DOM primitives that kept breaking on Phenom (NTT).

Runs the REAL ENUM_JS / PICK_JS from llm_driver.py against a static replica of the NTT
application page inside the sandbox Chrome. It exists because the same defect shipped four
times: a bare list of error strings ('should be array' x2) gave the model no way to know WHICH
field was invalid, so it "repaired" a healthy native <select> 8 times in a row while the real
offender - the State/Province/Region typeahead - was never touched.

  python3 /agent/flows/test_llm_driver_dom.py     (inside the jhw-agent container)
"""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.async_api import async_playwright
from llm_driver import ENUM_JS, PICK_JS, FILL_JS, SELECT_JS, CDP_URL
import testbrowser as TB

# A replica of what the screenshot actually shows: page nav (the decoys that PICK_JS used to
# "pick"), a healthy country <select>, and a typeahead whose error renders AFTER its input.
PAGE = """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<nav><a href="#">Sign up</a> <a href="#">My Information</a> <a href="#">My Experience</a>
     <a href="#">Voluntary Disclosures</a> <a href="#">Review</a></nav>
<div class="form-group"><label for="country">Country/Region Code:*</label>
  <select id="country" name="country">
    <option value="">Select an option</option><option value="DE">Germany</option>
    <option value="FR">France</option></select></div>
<div class="form-group"><label for="city">City:*</label>
  <input id="city" name="city" type="text" value="Frankfurt"></div>
<div class="form-group" style="position:relative">
  <label for="state">State/Province/Region:*</label>
  <input id="state" name="state" type="text" role="combobox" aria-autocomplete="list"
         aria-expanded="false" value="Hessen">
  <span class="error">should be array</span>
  <ul id="sugg" style="position:absolute;top:44px;left:0;width:240px;display:none;
      list-style:none;margin:0;padding:0;background:#fff;border:1px solid #ccc">
    <li>Hesse</li><li>Lower Saxony</li><li>Bavaria</li></ul>
</div>
<div class="form-group"><label for="zip">Zip/Postal Code:*</label>
  <input id="zip" name="zip" type="text" value=""></div>
<div class="form-group"><label for="start">What date would you be available to begin?</label>
  <input id="start" name="start" type="tel" placeholder="dd-mm-yyyy" value=""></div>
<script>
(() => {
  const inp = document.getElementById('state'), ul = document.getElementById('sugg');
  // The real widget does NOT filter as you type; it just opens. That is why matching had to
  // move from "the list narrows to one" to "search the open list by text".
  inp.addEventListener('input', () => { ul.style.display = 'block'; });
  inp.addEventListener('click', () => { ul.style.display = 'block'; });
  [...ul.children].forEach(li => li.addEventListener('click', () => {
      inp.value = li.textContent; ul.style.display = 'none';
      const e = document.querySelector('.error'); if (e) e.remove(); }));
})();
</script></body></html>"""


CAL = """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<label for="start">What date would you be available to begin?</label>
<input id="start" type="tel" placeholder="dd-mm-yyyy" readonly value="">
<div class="react-datepicker" id="cal" style="display:none;position:absolute;top:40px;left:0;
     background:#fff;border:1px solid #ccc;padding:6px">
  <select id="mon"><option>July</option><option>August</option><option>September</option></select>
  <select id="yr"><option>2026</option><option>2027</option></select>
  <div id="days"></div>
</div>
<script>
(() => {
  const inp = document.getElementById('start'), cal = document.getElementById('cal');
  const mon = document.getElementById('mon'), yr = document.getElementById('yr');
  const days = document.getElementById('days');
  const NAMES = ['July', 'August', 'September'];
  const DOW = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  const ord = (d) => d + (d > 3 && d < 21 ? 'th' : {1:'st',2:'nd',3:'rd'}[d % 10] || 'th');
  const MINM = 'July', MIND = 22;                 // the site's min-date: today
  function draw() {
    days.innerHTML = '';
    const m = mon.value, y = yr.value;
    for (let d = 1; d <= 30; d++) {
      const el = document.createElement('span');
      const dis = (m === MINM && d < MIND);
      const wd = DOW[d % 7];
      el.textContent = String(d);
      el.className = 'react-datepicker__day' + (dis ? ' react-datepicker__day--disabled' : '');
      el.setAttribute('aria-label',
        (dis ? 'Not available ' : 'Choose ') + wd + ', ' + m + ' ' + ord(d) + ', ' + y);
      el.style.cssText = 'display:inline-block;width:24px;height:20px';
      if (!dis) el.addEventListener('click', () => {
        inp.value = String(d).padStart(2,'0') + '-' +
                    String(NAMES.indexOf(m) + 7).padStart(2,'0') + '-' + y;
        cal.style.display = 'none';
      });
      days.appendChild(el);
    }
  }
  mon.addEventListener('change', draw); yr.addEventListener('change', draw);
  inp.addEventListener('click', () => { cal.style.display = 'block'; draw(); });
  draw();
})();
</script></body></html>"""

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


async def main() -> int:
    pw = await async_playwright().start()
    # NOT the operator's profile context. See flows/testbrowser.py: this suite's fixtures were
    # appearing in noVNC and one of them paints "Thank you for your application."
    page, _close, _mode = await TB.open_test_page(pw, CDP_URL)
    print(f"  browser: {_mode}")
    try:
        await page.set_content(PAGE)
        snap = await page.evaluate(ENUM_JS)

        errs = snap.get("errors") or []
        arr = [e for e in errs if "should be" in (e.get("msg") or "")]
        check("the 'should be array' error is found", len(arr) == 1, str([e.get("msg") for e in errs]))
        owner = (arr[0].get("label") if arr else "") or ""
        check("it is attributed to State/Province/Region, NOT Country",
              "state" in owner.lower(), f"owner={owner!r}")
        check("the error carries a usable selector for that field",
              bool(arr and arr[0].get("css")), f"css={arr[0].get('css') if arr else None!r}")
        check("the field is recognised as array-typed (typeahead)",
              bool(arr and arr[0].get("arr")))
        check("the typed-but-unselected text is reported back",
              bool(arr and arr[0].get("val") == "Hessen"), f"val={arr[0].get('val') if arr else None!r}")

        state_el = next((e for e in snap.get("els", []) if e.get("label", "").lower().startswith("state")), None)
        check("ENUM_JS flags the typeahead with arr:true", bool(state_el and state_el.get("arr")))

        # PICK_JS must ignore the page nav and choose from the list under the anchor.
        await page.click("#state")
        await page.wait_for_timeout(200)
        res = await page.evaluate(PICK_JS, {"want": "Hessen", "anchor": "#state", "first": False})
        opts = res.get("options") or []
        check("PICK_JS ignores the page navigation",
              not any(o in ("Sign up", "My Information", "Review") for o in opts), str(opts[:6]))
        check("PICK_JS matches the site's own spelling ('Hessen' -> 'Hesse')",
              res.get("picked") == "Hesse", f"picked={res.get('picked')!r}")

        snap2 = await page.evaluate(ENUM_JS)
        check("clicking the suggestion clears 'should be array'",
              not [e for e in (snap2.get("errors") or []) if "should be" in (e.get("msg") or "")])

        # No match at all -> take the first option rather than stalling (candidate's rule).
        await page.set_content(PAGE)
        await page.click("#state")
        await page.fill("#state", "")
        await page.type("#state", "Atl", delay=40)      # the driver types; typing is what opens it
        await page.wait_for_timeout(300)
        res2 = await page.evaluate(PICK_JS, {"want": "Atlantis", "anchor": "#state", "first": True})
        check("an unavailable answer falls back to the FIRST option",
              res2.get("picked") == "Hesse",
              f"picked={res2.get('picked')!r} debug={res2.get('debug')}")
        check("the fallback never picks a neighbouring FORM FIELD",
              "Zip/Postal Code:*" not in (res2.get("options") or []), str((res2.get("options") or [])[:6]))

        # The NTT date box is input[type=tel]; a type whitelist made FILL_JS miss it entirely.
        await page.set_content(PAGE)
        rf = await page.evaluate(FILL_JS, {"label": "What date would you be available to begin?",
                                           "value": "05-09-2026", "placeholder": "dd-mm-yyyy"})
        check("FILL_JS reaches a date box that is not input[type=text]",
              bool(rf.get("ok")) and await page.input_value("#start") == "05-09-2026",
              f"{rf.get('why') or ''} {rf.get('saw') or ''}")

        # A select must be left holding the value it was given (React's tracker defeated).
        rs = await page.evaluate(SELECT_JS, {"label": "Country/Region Code:*", "want": "Germany"})
        check("SELECT_JS actually commits the value",
              bool(rs.get("ok")) and bool(rs.get("verified"))
              and await page.input_value("#country") == "DE",
              f"picked={rs.get('picked')!r} verified={rs.get('verified')}")
        # A date must be COMMITTED, not just typed: Escape reverted the picker and left the box
        # empty while the driver logged "ok".
        await page.set_content(PAGE)
        from llm_driver import _commit_date
        got = await _commit_date(page, "#start", "20-08-2026", "start date")
        check("_commit_date leaves the value IN the date box", got == "20-08-2026", f"reads {got!r}")

        # NTT's date box is READ-ONLY with a min-date: text can never fill it, only the calendar.
        await page.set_content(CAL)
        from llm_driver import _pick_date_in_calendar
        got = await _pick_date_in_calendar(page, "#start", "20-08-2026", "start date")
        check("the calendar driver reaches a date in a LATER month", got == "20-08-2026",
              f"reads {got!r}")
        await page.set_content(CAL)
        blocked = await _pick_date_in_calendar(page, "#start", "01-07-2026", "start date")
        check("a disabled (past) day is never clicked", blocked == "", f"reads {blocked!r}")

        # Full automation: the driver must find Submit and only claim success on CONFIRMATION.
        await page.set_content(
            "<!doctype html><html><body><button>BACK</button>"
            "<button id='s'>SUBMIT</button>"
            "<script>(()=>{document.getElementById('s').addEventListener('click',()=>{"
            "document.body.innerHTML='<p>Thank you for your application.</p>';});})();</script>"
            "</body></html>")
        from llm_driver import SUBMIT_JS, CONFIRM_JS
        s = await page.evaluate(SUBMIT_JS)
        check("SUBMIT_JS finds and clicks the Submit button (not BACK)",
              s.get("ok") and s.get("label", "").upper() == "SUBMIT", str(s))
        await page.wait_for_timeout(200)
        body = await page.evaluate(CONFIRM_JS)
        import re as _re
        check("a confirmation page is recognised as proof of submission",
              bool(_re.search(r"thank you|application (has been )?(submitted|received)", body, _re.I)),
              body[:60])
        await page.set_content("<!doctype html><html><body><button>Next</button></body></html>")
        s2 = await page.evaluate(SUBMIT_JS)
        check("no Submit button -> it does NOT pretend to have submitted",
              not s2.get("ok"), str(s2))

        # A checkbox must be SET, never toggled: clicking an already-ticked box unticks it, which
        # is what silently blocked Next on NTT's Voluntary Disclosures page.
        await page.set_content(
            "<!doctype html><html><body><label for='ag'>I Agree</label>"
            "<input id='ag' type='checkbox'></body></html>")
        from llm_driver import CHECK_JS
        a = await page.evaluate(CHECK_JS, {"sel": "#ag"})
        check("CHECK_JS ticks an unticked box",
              a.get("ok") and a.get("checked") and not a.get("already"), str(a))
        b = await page.evaluate(CHECK_JS, {"sel": "#ag"})
        check("CHECK_JS leaves an already-ticked box ALONE",
              b.get("already") and b.get("checked") and await page.is_checked("#ag"), str(b))

        # A page mid-render looks EXACTLY like a finished one: no errors, no missing fields.
        # The driver must not be able to read that as "nothing left to do".
        await page.set_content("<!doctype html><html><body><div>loading</div></body></html>")
        thin = await page.evaluate(ENUM_JS)
        check("a half-rendered page yields too few elements to act on",
              len(thin.get("els") or []) < 8, f"{len(thin.get('els') or [])} elements")
        check("...and it must NOT look like a review page", not thin.get("review"),
              str(thin.get("review")))

        # "choose yes or no, it doesn't really matter" - nothing may stay on the placeholder.
        await page.set_content(PAGE)
        from llm_driver import SWEEP_JS, SELECT_DEFAULTS
        swept = await page.evaluate(SWEEP_JS, SELECT_DEFAULTS)
        check("SWEEP_JS answers a dropdown left on 'Select an option'",
              any(s.get("picked") for s in swept) and await page.input_value("#country") != "",
              str(swept))
        check("the swept value actually sticks", all(s.get("verified") for s in swept), str(swept))
        # ------------------------------------------------------------------ honeypot
        # REPLICA OF THE REAL TRAP, byte-for-byte from the live Accenture Workday
        # sign-in page (measured 2026-08-16 via Chrome MCP). Every style value below
        # was READ off the real element, not invented:
        #   rect w=1  h=0.010009765625        position=absolute   display=block
        #   visibility=visible  opacity=1     tabIndex=0          offsetParent NOT null
        #   clip=rect(1px, 1px, 1px, 1px)     clip-path=polygon(0px 0px, 0px 0px, ...)
        # The OLD ENUM_JS vis() (width>0 && height>0 && visibility!=='hidden'
        # && display!=='none') returned TRUE for this, so the trap was indexed and shown
        # to the model as an ordinary text input. Filling it self-identifies as a robot and
        # the application is silently discarded -- worse than any stall, because nothing
        # in the log ever admits it happened.
        await page.set_content(
            "<!doctype html><html><head><style>"
            ".hpwrap{position:relative;height:0}"
            ".hp{position:absolute;width:1px;height:0.01px;clip:rect(1px,1px,1px,1px);"
            "clip-path:polygon(0 0,0 0,0 0,0 0)}"
            ".fixedbtn{position:fixed;bottom:10px;right:10px;width:120px;height:40px}"
            "</style></head><body>"
            "<label for='e'>Email Address*</label>"
            "<input data-automation-id='email' id='e' type='text' style='width:300px;height:32px'>"
            "<label for='p'>Password*</label>"
            "<input data-automation-id='password' id='p' type='password' style='width:300px;height:32px'>"
            "<div class='hpwrap'><label for='hp' class='hp'>Enter website. This input is for "
            "robots only, do not enter if you're human.</label>"
            "<input data-automation-id='beecatcher' id='hp' name='website' type='text' class='hp'></div>"
            "<button class='fixedbtn' data-automation-id='signInSubmitButton'>Sign In</button>"
            "</body></html>")
        # ENUM_JS is already imported at module scope (line 15). Re-importing it HERE made it a
        # function-local for the whole of main(), so the FIRST use at the top of this function
        # raised UnboundLocalError -- a defect I introduced and this harness caught.
        snap = await page.evaluate(ENUM_JS)
        els = snap.get("els", [])
        # ENUM_JS emits css/label/text and does NOT carry data-automation-id (verified against
        # its own out.push shape), and cssOf() yields "#id". The Sign In button has no id and
        # no label, so its ONLY identity is `text` -- read all three or this check is aimed at
        # a field that is always empty and can never fail.
        blob = " ".join((e.get("css") or "") + "|" + (e.get("label") or "") + "|" +
                        (e.get("text") or "") for e in els).lower()
        check("ENUM_JS does NOT index the beecatcher honeypot",
              "robots only" not in blob, blob[:220])
        check("exactly the 3 REAL controls are indexed, not 4",
              len(els) == 3, f"{len(els)} :: {blob[:220]}")
        check("the real Email field IS still indexed", "email address" in blob, blob[:220])
        check("the real Password field IS still indexed", "password" in blob, blob[:220])
        # position:fixed has a null offsetParent too. If the guard rejected on that alone it
        # would delete every docked modal button from the index -- so prove it does not.
        check("a position:fixed Sign In button is NOT rejected as hidden",
              "sign in" in blob, blob[:220])

    finally:
        await _close()
        await pw.stop()
    print(("FAILED: " + ", ".join(FAILED)) if FAILED else "ALL DOM CONTRACTS HOLD")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
