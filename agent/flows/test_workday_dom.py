"""Regression test for the Workday DOM primitives (workday_wd.py), against static replicas.

WHY THIS EXISTS
  Workday's screening controls are NOT <select> and NOT <input>. They are
  <button aria-haspopup="listbox"> opening a PORTAL of options. Three defects in this repo's
  history are one keystroke away from repeating on that widget:
    * a control enumerated without the QUESTION it belongs to is unactionable (the Phenom
      "should be array" loop: 27 steps attacking a healthy field);
    * a value that is written and not READ BACK is not set (the date box that reported ok=True
      while empty on screen) - Workday additionally REVERTS a selection on re-render;
    * a control that is TOGGLED rather than SET breaks under retry (the consent checkbox that a
      second click unticked, after which Next could never succeed).
  Every replica below is built from agent/recordings/workday.py - a REAL completed submission on
  accenture.wd103.myworkdayjobs.com - or from ARIA. Nothing here is a guessed tenant selector.

  python3 /agent/flows/test_workday_dom.py     (inside the jhw-agent container)

It runs inside `python jhw.py apply` (see jhw.py::_dom_selftest) - a guard the human has to
remember to run does not exist.
"""
from __future__ import annotations
import asyncio, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright
import llm_driver as LD
import workday_wd as W
import testbrowser as TB

CSS = """<style>
 body{margin:0;font:14px sans-serif}
 .q{position:relative;margin:0 0 90px 0;padding:4px;width:520px}
 .lb{position:absolute;top:34px;left:0;width:300px;background:#fff;border:1px solid #ccc;
     margin:0;padding:0;list-style:none;z-index:9}
 .lb li,.lb div{padding:5px;height:18px}
 button{height:26px;min-width:180px;text-align:left}
 input{height:22px;width:260px}
</style>"""

# ---------------------------------------------------------------- Application Questions (recorded)
# The three questions and their recorded answers are verbatim from the real submission:
#   group "All individuals employed by"      -> Yes
#   group "At your current employer, are"     -> No
#   a bare "Select One Required"              -> (recorded Yes; here the preferred-communication
#                                                question, so the answer comes from candidate.md)
# "Add" carries aria-expanded on some tenants and MUST NOT be mistaken for a dropdown.
QUESTIONS = CSS + """
<h2>Application Questions</h2>
<div class="q" role="group" aria-labelledby="l1">
  <div id="l1">All individuals employed by Accenture must comply with its Code of Business Ethics. Do you agree? *</div>
  <button id="b1" aria-haspopup="listbox" aria-expanded="false" aria-label="Select One Required">Select One</button>
  <ul class="lb" id="o1" role="listbox" style="display:none">
    <li role="option">Yes</li><li role="option">No</li></ul>
</div>
<div class="q" role="group" aria-labelledby="l2">
  <div id="l2">At your current employer, are you subject to a non-competition agreement? *</div>
  <button id="b2" aria-haspopup="listbox" aria-expanded="false" aria-label="Select One Required">Select One</button>
  <ul class="lb" id="o2" role="listbox" style="display:none">
    <li role="option">Yes</li><li role="option">No</li></ul>
</div>
<div class="q" role="group" aria-labelledby="l3">
  <div id="l3">What is your preferred method of communication? *</div>
  <button id="b3" aria-haspopup="listbox" aria-expanded="false" aria-label="Select One Required">Select One</button>
  <ul class="lb" id="o3" role="listbox" style="display:none">
    <li role="option">Email</li><li role="option">Phone</li><li role="option">Post</li></ul>
</div>
<button id="add" aria-expanded="false">Add</button>
<button id="nav">Save and Continue</button>
<script>
(() => {
  const wire = (b, l) => {
    const btn = document.getElementById(b), box = document.getElementById(l);
    btn.addEventListener('click', () => {
      const open = box.style.display === 'block';
      box.style.display = open ? 'none' : 'block';
      btn.setAttribute('aria-expanded', String(!open));
    });
    [...box.children].forEach(li => li.addEventListener('click', () => {
      btn.textContent = li.textContent;      // the label is the only proof React took the value
      box.style.display = 'none';
      btn.setAttribute('aria-expanded', 'false');
    }));
  };
  wire('b1','o1'); wire('b2','o2'); wire('b3','o3');
})();
</script>"""

# A dropdown that accepts the click and then REVERTS on the next tick - Workday's documented
# behaviour when a re-render intervenes. It must be reported as NOT verified, never as a success.
REVERT = CSS + """
<div class="q" role="group" aria-labelledby="lr">
  <div id="lr">What is your legal gender? *</div>
  <button id="br" aria-haspopup="listbox" aria-expanded="false">Select One</button>
  <ul class="lb" id="orr" role="listbox" style="display:none">
    <li role="option">Male</li><li role="option">Female</li></ul>
</div>
<script>
(() => {
  const btn = document.getElementById('br'), box = document.getElementById('orr');
  btn.addEventListener('click', () => { box.style.display = 'block'; });
  [...box.children].forEach(li => li.addEventListener('click', () => {
    btn.textContent = li.textContent; box.style.display = 'none';
    setTimeout(() => { btn.textContent = 'Select One'; }, 120);   // the revert
  }));
})();
</script>"""

# Voluntary Disclosures. Gender's options are PLAIN TEXT (the recording used get_by_text for it)
# and Pronoun's are role=option - both shapes occur, so both are exercised. The consent checkbox
# is the visually-hidden-but-not-display:none input Workday actually renders.
DISCLOSE = CSS + """
<h2>Voluntary Disclosures</h2>
<div class="q" role="group" aria-labelledby="lg">
  <div id="lg">Please Select Your Pronoun *</div>
  <button id="bp" aria-haspopup="listbox" aria-expanded="false">Select One</button>
  <ul class="lb" id="op" role="listbox" style="display:none">
    <li role="option">He/Him</li><li role="option">She/Her</li><li role="option">They/Them</li></ul>
</div>
<div class="q">
  <label style="display:inline-flex;align-items:center">
    <input id="acc" type="checkbox" style="width:14px;height:14px;opacity:0.01">
    <span>I accept.</span></label>
</div>
<div class="q">
  <label style="display:inline-flex;align-items:center">
    <input id="mkt" type="checkbox" style="width:14px;height:14px">
    <span>Email me about similar jobs</span></label>
</div>
<button id="nav2">Save and Continue</button>
<script>
(() => {
  const btn = document.getElementById('bp'), box = document.getElementById('op');
  btn.addEventListener('click', () => { box.style.display = 'block'; });
  [...box.children].forEach(li => li.addEventListener('click', () => {
    btn.textContent = li.textContent; box.style.display = 'none'; }));
})();
</script>"""

# "How Did You Hear About Us?" - a CASCADE, and a MULTISELECT: the pick lands in a CHIP and the
# input goes back to EMPTY. Reading the input would report failure on a success.
CASCADE = CSS + """
<div class="q" role="group" aria-labelledby="lh">
  <div id="lh">How Did You Hear About Us? *</div>
  <div id="chips"></div>
  <input id="hh" role="combobox" aria-autocomplete="list" aria-expanded="false" value="">
  <ul class="lb" id="oh" style="display:none"></ul>
</div>
<button id="nav3">Save and Continue</button>
<script>
(() => {
  const inp = document.getElementById('hh'), box = document.getElementById('oh');
  const chips = document.getElementById('chips');
  const draw = (items) => { box.innerHTML = ''; items.forEach(t => {
      const li = document.createElement('li'); li.setAttribute('role','option');
      li.textContent = t; li.addEventListener('click', () => {
        if (t === 'Job Boards') { draw(['LinkedIn','Indeed','StepStone']); return; }
        const c = document.createElement('span');
        c.className = 'chip'; c.textContent = t;
        c.style.cssText = 'display:inline-block;padding:2px 6px;border:1px solid #999';
        chips.appendChild(c);
        inp.value = '';                      // the input CLEARS - the chip is the only evidence
        box.style.display = 'none';
      });
      box.appendChild(li); });
    box.style.display = 'block'; };
  const open = () => draw(['Job Boards','Referral','Career Fair']);
  inp.addEventListener('click', open); inp.addEventListener('input', open);
})();
</script>"""

# Resume/CV DELIBERATELY SECOND. llm_driver attaches by DOM ORDER (right on Phenom), so on this
# page DOM order would file the CV as the cover letter. The group name is the mapping.
FILES = CSS + """
<h2>My Experience</h2>
<div class="q" role="group" aria-labelledby="lc"><div id="lc">Cover Letter</div>
  <input type="file" id="f_cover"></div>
<div class="q" role="group" aria-labelledby="lr2"><div id="lr2">Resume/CV</div>
  <button id="del" aria-label="Delete jev gen il cyber.pdf">Delete jev gen il cyber.pdf</button>
  <input type="file" id="f_resume"></div>
<button id="nav4">Save and Continue</button>
<script>
(() => { document.getElementById('del').addEventListener('click', (e) => {
   e.target.remove(); }); })();
</script>"""

# My Information. City is ALREADY FILLED: on a returning applicant Workday restores these
# server-side and overwriting them replaces good data with a worse parse of the same CV.
MYINFO = CSS + """
<h2>My Information</h2>
<div class="q"><label for="fn">First Name *</label><input id="fn" value=""></div>
<div class="q"><label for="ln">Last Name *</label><input id="ln" value=""></div>
<div class="q"><label for="a1">Address Line 1 *</label><input id="a1" value=""></div>
<div class="q"><label for="pc">Postal Code *</label><input id="pc" value=""></div>
<div class="q"><label for="ci">City *</label><input id="ci" value="Frankfurt am Main"></div>
<div class="q"><label for="ph">Phone Number *</label><input id="ph" value=""></div>
<button id="nav5">Save and Continue</button>"""

SKILLS = CSS + """
<div class="q" role="group" aria-labelledby="ls">
  <div id="ls">Skills *</div>
  <div id="chips2"></div>
  <input id="sk" role="combobox" aria-autocomplete="list" aria-expanded="false" value="">
  <ul class="lb" id="os" style="display:none"></ul>
</div>
<button>Save and Continue</button>
<script>
(() => {
  const inp = document.getElementById('sk'), box = document.getElementById('os');
  const chips = document.getElementById('chips2');
  const open = () => { box.innerHTML = '';
    ['Kubernetes','Zero Trust','Terraform'].forEach(t => {
      const li = document.createElement('li'); li.setAttribute('role','option');
      li.textContent = t;
      li.addEventListener('click', () => {
        const c = document.createElement('span'); c.className = 'chip'; c.textContent = t;
        c.style.cssText = 'display:inline-block;padding:2px 6px;border:1px solid #999';
        chips.appendChild(c); inp.value = ''; box.style.display = 'none'; });
      box.appendChild(li); });
    box.style.display = 'block'; };
  inp.addEventListener('click', open); inp.addEventListener('input', open);
})();
</script>"""

REVIEW = CSS + """<h2>Review</h2><p>Please review your application.</p>
<button>Back</button><button id="sub">Submit</button>"""

FAILED: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


async def main() -> int:
    facts = W.candidate_answers()
    data = {}
    try:
        data = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"),
                             encoding="utf-8"))
    except Exception as e:
        print(f"  [note] resume_data.json unreadable ({str(e)[:50]}) - text-fill checks reduced")
    pw = await async_playwright().start()
    # 2026-08-16: this used to open a tab in browser.contexts[0] -- the operator's REAL persistent
    # profile -- so these fixtures appeared in noVNC and he reasonably asked whether the
    # "Thank you for your application." on his screen was a real submission. It was this suite.
    page, _close, _mode = await TB.open_test_page(pw, W.CDP_URL)
    print(f"  browser: {_mode}")
    try:
        # ---------------------------------------------------------- 1. enumerate + attribute
        print("-- Application Questions: enumeration and question attribution")
        await page.set_content(QUESTIONS)
        ctrls = await page.evaluate(W.WD_ENUM_JS) or []
        drops = [c for c in ctrls if c.get("kind") == "drop"]
        check("all three button-dropdowns are found", len(drops) == 3,
              f"{len(drops)} of {len(ctrls)} controls")
        check("'Save and Continue' is NOT treated as a dropdown",
              not any("save and continue" in (c.get("cur") or "").lower() for c in ctrls))
        check("'Add' (aria-expanded, not a picker) is excluded",
              not any((c.get("cur") or "").strip().lower() == "add" for c in ctrls),
              str([c.get("cur") for c in ctrls]))
        qs = [(c.get("q") or "") for c in drops]
        check("each control carries ITS OWN question, not the neighbour's",
              any("all individuals employed by" in q.lower() for q in qs)
              and any("at your current employer" in q.lower() for q in qs)
              and any("preferred method of communication" in q.lower() for q in qs),
              str([q[:38] for q in qs]))
        check("the placeholder 'Select One' reads as UNANSWERED",
              all(not c.get("answered") for c in drops),
              str([c.get("answered") for c in drops]))
        check("all three are seen as required", all(c.get("required") for c in drops),
              str([c.get("required") for c in drops]))

        # ---------------------------------------------------------- 2. the sweep answers them
        print("-- the sweep answers them from candidate.md and reads the value BACK")
        r = {"filled": [], "unresolved": []}
        n = await W.wd_sweep(page, facts, r)
        check("all three were answered", n == 3, f"{n} answered, filled={r['filled']}")
        got = {i: await page.inner_text(f"#b{i}") for i in (1, 2, 3)}
        check("Q1 (code of ethics) = Yes   [recorded answer]", got[1].strip() == "Yes", str(got))
        check("Q2 (non-competition)  = No   [recorded answer]", got[2].strip() == "No", str(got))
        check("Q3 (communication)    = Email [candidate.md]", got[3].strip() == "Email", str(got))
        check("nothing was left unresolved", not r["unresolved"], str(r["unresolved"]))

        print("-- and it is IDEMPOTENT: an answered control is never re-opened")
        again = await W.wd_sweep(page, facts, {"filled": [], "unresolved": []})
        check("a second sweep changes nothing (re-opening is what reverts it)", again == 0,
              f"{again} touched")
        got2 = {i: await page.inner_text(f"#b{i}") for i in (1, 2, 3)}
        check("...and the answers survived the second sweep", got2 == got, str(got2))

        # ---------------------------------------------------------- 3. a revert is NOT a success
        print("-- a value React reverts must be reported as NOT verified")
        await page.set_content(REVERT)
        res = await W._pick_in_portal(page, "#br", "Male", first=True)
        check("the option was clicked", res.get("picked") == "Male", str(res.get("picked")))
        await page.wait_for_timeout(400)
        res2 = await W._pick_in_portal(page, "#br", "Male", first=True)
        await page.wait_for_timeout(400)
        now = (await page.inner_text("#br")).strip()
        check("the reverted control is not silently accepted",
              now == "Select One" and not (res2.get("verified") and now == "Male"),
              f"button reads {now!r}, verified={res2.get('verified')}")
        rr = {"filled": [], "unresolved": []}
        await W.wd_sweep(page, facts, rr)
        await page.wait_for_timeout(400)
        check("a reverting control is recorded as UNRESOLVED, not as filled",
              bool(rr["unresolved"]), f"unresolved={rr['unresolved']} filled={rr['filled']}")

        # ---------------------------------------------------------- 4. Voluntary Disclosures
        print("-- Voluntary Disclosures: role=option picker + the styled consent checkbox")
        await page.set_content(DISCLOSE)
        rd = {"filled": [], "unresolved": []}
        await W.wd_sweep(page, facts, rd)
        check("pronoun = He/Him  [recorded answer]",
              (await page.inner_text("#bp")).strip() == "He/Him",
              await page.inner_text("#bp"))
        nc = await W.wd_consent(page, rd)
        check("'I accept.' is ticked", await page.is_checked("#acc"), f"{nc} ticked")
        check("a marketing opt-in is NOT ticked", not await page.is_checked("#mkt"))
        nc2 = await W.wd_consent(page, {"filled": []})
        check("a second pass does not UNTICK it (set, never toggled)",
              await page.is_checked("#acc") and nc2 == 0, f"{nc2} touched on the second pass")

        # ---------------------------------------------------------- 5. cascade + chips
        print("-- How Did You Hear About Us?: two-level cascade, answer lands in a CHIP")
        await page.set_content(CASCADE)
        pre = await page.evaluate(W.WD_ENUM_JS) or []
        me = next((c for c in pre if "how did you hear" in (c.get("q") or "").lower()), None)
        check("the multiselect is enumerated as kind=multi", bool(me) and me.get("kind") == "multi",
              str(me and me.get("kind")))
        check("with no chip yet it reads UNANSWERED", bool(me) and not me.get("answered"))
        ok = await W._wd_cascade(page, me["css"], ["Job Boards", "LinkedIn"], me["q"])
        check("the cascade reports success", ok)
        check("the chip carries the answer",
              (await page.inner_text("#chips")).strip() == "LinkedIn",
              await page.inner_text("#chips"))
        check("the INPUT is empty - so reading it would have reported failure",
              (await page.input_value("#hh")) == "")
        post = await page.evaluate(W.WD_ENUM_JS) or []
        me2 = next((c for c in post if "how did you hear" in (c.get("q") or "").lower()), None)
        check("a chip is what makes it ANSWERED", bool(me2) and me2.get("answered"),
              str(me2 and me2.get("chips")))

        print("-- a SINGLE-LEVEL multiselect: proof is the chip, never the input")
        # Regression: _pick_in_portal proves a value by reading the CONTROL'S LABEL back, which is
        # right for a <button> and WRONG here - the input clears, so before == after == "" and a
        # good answer was recorded as UNRESOLVED. wd_sweep routes a multi through the chip check.
        await page.set_content(SKILLS)
        rk = {"filled": [], "unresolved": []}
        nk = await W.wd_sweep(page, facts, rk)
        check("the multiselect was answered (first option, nothing stored for 'Skills')",
              nk == 1, f"{nk} answered, filled={rk['filled']}")
        check("a chip is present", (await page.inner_text("#chips2")).strip() != "",
              await page.inner_text("#chips2"))
        check("the input is EMPTY, and that was NOT read as failure",
              (await page.input_value("#sk")) == "" and not rk["unresolved"],
              f"unresolved={rk['unresolved']}")

        # ---------------------------------------------------------- 6. files by GROUP
        print("-- files are mapped by GROUP NAME, not DOM order")
        await page.set_content(FILES)
        slots = await page.evaluate(W.WD_FILES_JS) or []
        check("both file inputs are found", len(slots) == 2, str(slots))
        by = {s["i"]: (s.get("group") or "") for s in slots}
        check("input 0 is the Cover Letter group", "cover" in by.get(0, "").lower(), str(by))
        check("input 1 is the Resume/CV group", re.search(r"resume|cv", by.get(1, ""), re.I) is not None,
              str(by))
        resume_slot = next((s for s in slots
                            if re.search(r"resume|cv|curriculum", s.get("group") or "", re.I)), None)
        check("so the RESUME is destined for input #1, not #0 (DOM order would be wrong)",
              bool(resume_slot) and resume_slot["i"] == 1, str(resume_slot))
        rm = await page.evaluate(W.WD_DELETE_JS) or {}
        check("a stacked attachment is found by its 'Delete <file>.pdf' label",
              rm.get("removed") == 1 and any(".pdf" in x.lower() for x in (rm.get("labels") or [])),
              str(rm))
        # A real attach, end to end, into the group-mapped input.
        tmp = "/tmp/jhw_wd_test_resume.pdf"
        try:
            open(tmp, "wb").write(b"%PDF-1.4\n% test fixture\n")
            await page.locator(resume_slot["css"]).first.set_input_files(tmp)
            nm = await page.evaluate(
                "() => { const e=document.querySelector(\"input[data-jhw-file='1']\");"
                " return e && e.files && e.files[0] ? e.files[0].name : ''; }")
            check("set_input_files actually lands the file in the Resume/CV input",
                  nm == os.path.basename(tmp), f"input holds {nm!r}")
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

        # ---------------------------------------------------------- 7. text, only where empty
        print("-- My Information: known text fields, and a PRE-FILLED field left alone")
        await page.set_content(MYINFO)
        rt = {"filled": []}
        cnt = await W.wd_text(page, data, rt)
        check("empty known fields were filled", cnt >= 4, f"{cnt} filled: {rt['filled']}")
        check("first name came from the LEGAL name",
              (await page.input_value("#fn")).strip() == "Evgeny",
              await page.input_value("#fn"))
        check("postal code was parsed out of the address",
              (await page.input_value("#pc")).strip() == "61169",
              await page.input_value("#pc"))
        check("a PRE-FILLED field is not overwritten",
              (await page.input_value("#ci")) == "Frankfurt am Main",
              await page.input_value("#ci"))

        # ---------------------------------------------------------- 8. page state
        print("-- page state: advance button, review detection, thin-page guard")
        await page.set_content(MYINFO)
        st = await page.evaluate(W.PAGE_JS) or {}
        check("'Save and Continue' is found as the advance button",
              (st.get("next") or "").lower().startswith("save and continue"), str(st.get("next")))
        check("a form page is NOT a review page", not st.get("review"), str(st.get("review")))
        await page.set_content(REVIEW)
        st2 = await page.evaluate(W.PAGE_JS) or {}
        check("the Review page is detected", st2.get("review") is True)
        # The error regex used to match a bare "please ", so the Review page's own
        # "Please review your application." was reported as a validation error.
        check("the Review page's own prose is NOT reported as a validation error",
              not st2.get("errors"), str(st2.get("errors")))
        check("...with Submit found and Back ignored",
              (st2.get("submit") or "").lower() == "submit" and not st2.get("next"),
              f"submit={st2.get('submit')!r} next={st2.get('next')!r}")
        await page.set_content(CSS + "<div>Errors Found</div>"
                               + "<div class='q'><label for='e1'>Country *</label>"
                                 "<input id='e1'><div>Select One Required</div></div>"
                                 "<button>Save and Continue</button>")
        ste = await page.evaluate(W.PAGE_JS) or {}
        check("a REAL Workday validation error is still caught",
              any(re.search(r"errors found|select one required", e, re.I)
                  for e in (ste.get("errors") or [])), str(ste.get("errors")))

        await page.set_content("<!doctype html><html><body><div>loading</div></body></html>")
        st3 = await page.evaluate(W.PAGE_JS) or {}
        check("a half-rendered page is recognised as too thin to act on",
              st3.get("thin", 99) < 6, f"{st3.get('thin')} controls")
        check("...and it does NOT look like a review page", not st3.get("review"))
        check("the fingerprint differs between two different pages",
              (st.get("fp") or "") != (st2.get("fp") or ""))

        # ---------------------------------------------------------- 9. submit, with evidence
        print("-- submission is claimed ONLY on the site's own confirmation (LD._submit)")
        await page.set_content(REVIEW + """<script>
        (() => { document.getElementById('sub').addEventListener('click', () => {
           document.body.innerHTML = '<p>Thank you for your application.</p>'; }); })();</script>""")
        rs = {"actions": []}
        ok = await LD._submit(page, rs)
        check("Submit is clicked and the confirmation is believed",
              ok and rs.get("submitted") is True, str(rs))
        await page.set_content(CSS + "<button>Back</button><button id='sub'>Submit</button>")
        rs2 = {"actions": []}
        ok2 = await LD._submit(page, rs2)
        check("a Submit click with NO confirmation is reported as NOT submitted",
              (not ok2) and rs2.get("submitted") is False, str(rs2))
    finally:
        await _close()
        await pw.stop()
    print(("FAILED: " + ", ".join(FAILED)) if FAILED else "ALL WORKDAY DOM CONTRACTS HOLD")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
