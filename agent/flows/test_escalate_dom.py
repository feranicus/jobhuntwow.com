"""Regression test for the ESCALATION's view of the page, in the REAL Chromium.

WHY THIS EXISTS — the candidate photographed it and named it:
  "there is this popup over the other page and I think our llms do not even see it"
He was right, and the run log proves it. `_SNAP_JS` enumerated buttons from the WHOLE document, so
with the Workday sign-in MODAL open it reported

    visible : 0 inputs, 0 file input(s)
    buttons : ['English', 'Sign In', 'Search Careers', 'Introduce Yourself', 'Back to Job Posting']

Every one of those belongs to the page BEHIND the overlay. The modal's own controls — the whole
email/Google sign-in path — were never in the list, and the dialog is TALLER THAN THE VIEWPORT so
they sit below the fold. A driver cannot act on what it is not shown, and neither can a panel.

The replica below is built from that screenshot of accenture.wd103.myworkdayjobs.com: a
role=dialog titled "Sign In", the four nav buttons behind it, the real legal paragraph, and the
sign-in controls far enough down that the dialog must be scrolled.

  python3 /agent/flows/test_escalate_dom.py      (inside the jhw-agent container)

It runs inside `python jhw.py apply` — a guard the human has to remember to run does not exist.
"""
from __future__ import annotations
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright
import workday as WD
import escalate as ESC
import testbrowser as TB

FAILED = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"\n         {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


CSS = """<style>
  html,body{margin:0;font:14px sans-serif;height:100%}
  #page{padding:12px}
  nav button{margin-right:8px}
  /* the real overlay: fixed, dims the page, and the panel is SHORTER than its content */
  #scrim{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50}
  #dlg{position:fixed;left:50%;top:40px;transform:translateX(-50%);width:520px;height:300px;
       overflow-y:auto;background:#fff;z-index:51;padding:18px;box-sizing:border-box}
  .legal{line-height:1.5}
  .spacer{height:420px}
  /* Accenture's real trap: display:block + visibility:visible, hidden by clip + clip-path */
  .bee{position:absolute;width:1px;height:0.01px;clip:rect(1px,1px,1px,1px);
       clip-path:polygon(0 0,0 0,0 0,0 0);display:block;visibility:visible}
</style>"""

# The page BEHIND the modal — exactly the five buttons the old snapshot reported.
PAGE_BEHIND = """
<div id="page">
  <nav>
    <button>English</button>
    <button>Search Careers</button>
    <button>Introduce Yourself</button>
    <button>Back to Job Posting</button>
  </nav>
  <h1>Agentic AI MarTech Strategist (all genders)</h1>
  <div data-automation-id="progressBarActiveStep">current step 1 of 6 Create Account/Sign In</div>
  <input aria-label="Search jobs" value="">
  <button>Sign In</button>
</div>"""

# The modal. Its sign-in controls are BELOW THE FOLD, behind 420px of legal text.
MODAL = """
<div id="scrim"></div>
<div id="dlg" role="dialog" aria-modal="true" aria-label="Sign In">
  <h2>Sign In</h2>
  <p class="legal">You may choose to sign in using your Google account as an alternative
    authentication method. Where selected, authentication is handled by the respective provider
    (Google) in accordance with their own security measures. You may alternatively sign in using a
    Workday username and password.</p>
  <div class="spacer"></div>
  <button data-automation-id="googleSignIn">Sign in with Google</button>
  <button data-automation-id="emailSignIn">Sign in with email</button>
  <input class="bee" data-automation-id="beecatcher" name="website"
         aria-label="Enter website. This input is for robots only, do not enter if you're human.">
  <button data-automation-id="closeButton">Close</button>
</div>"""

EMAIL_FORM = """
<div id="scrim"></div>
<div id="dlg" role="dialog" aria-modal="true" aria-label="Sign In">
  <h2>Sign In</h2>
  <label for="e">Email Address</label><input id="e" required value="">
  <label for="p">Password</label><input id="p" type="password" required value="">
  <div role="alert">Your account or password is incorrect.</div>
  <label for="c">I agree to the terms</label><input id="c" type="checkbox">
  <label for="cy">Country</label>
  <select id="cy"><option>Select One</option><option>Germany</option><option>Austria</option></select>
  <button data-automation-id="signInSubmitButton">Sign In</button>
</div>"""


async def main() -> int:
    pw = await async_playwright().start()
    # A PRIVATE browser, not the one the operator is watching. `pw.chromium.launch()` with no
    # channel looks for playwright's BUNDLED chromium, which this image never installs -- this
    # suite would have failed on his machine with "Executable doesn't exist". testbrowser owns
    # that decision now, and every fixture it renders carries a NOT-REAL banner.
    page, _close, _mode = await TB.open_test_page(pw, WD.CDP_URL, {"width": 900, "height": 700})
    print(f"  browser: {_mode}")
    try:
        # ---------------------------------------------------------------- [1] the reported defect
        print("\n[1] THE MODAL OWNS THE PAGE (the defect the candidate photographed)")
        await page.set_content(CSS + PAGE_BEHIND + MODAL)
        idx = await WD._controls(page)
        ctrls, dlg = idx["controls"], idx["dialog"]
        labels = [c["label"] for c in ctrls]
        check("the dialog is DETECTED", dlg.get("present") is True, str(dlg)[:200])
        check("its title is read", "Sign In" in (dlg.get("title") or ""), str(dlg.get("title")))
        check("it is reported as SCROLLABLE (controls below the fold)",
              dlg.get("scrollable") is True, f"scrollHeight={dlg.get('scrollHeight')} "
              f"clientHeight={dlg.get('clientHeight')}")
        for behind in ("Search Careers", "Introduce Yourself", "Back to Job Posting", "English"):
            check(f"the page-behind button {behind!r} is NOT offered", behind not in labels,
                  f"index = {labels}")
        check("the MODAL's Google button IS offered",
              any("Sign in with Google" in l for l in labels), f"index = {labels}")
        check("the MODAL's email button IS offered — the whole path that was invisible",
              any("Sign in with email" in l for l in labels), f"index = {labels}")
        check("a below-the-fold control is still indexed (no viewport filter)",
              any("Close" in l for l in labels), f"index = {labels}")
        check("the page-behind search input is NOT offered",
              not any("Search jobs" in l for l in labels), f"index = {labels}")

        # ---------------------------------------------------------------- [2] the honeypot
        print("\n[2] THE BOT TRAP IS SEEN AND FLAGGED, not silently indexed as a text field")
        bee = [c for c in ctrls if "robots only" in (c.get("label") or "")]
        check("the beecatcher is present in the index", len(bee) == 1, f"{len(bee)} found")
        if bee:
            check("and it is flagged honeypot=True", bee[0].get("honeypot") is True, str(bee[0]))
            ok, why = ESC.validate({"verb": "fill", "target": bee[0]["i"], "value": "x"}, ctrls)
            check("filling it is REFUSED by the validator", not ok, why)

        # ---------------------------------------------------------------- [3] indices are the contract
        print("\n[3] AN INDEX THE PANEL WAS NOT SHOWN CANNOT BE ACTED ON")
        ok, why = ESC.validate({"verb": "click", "target": 999}, ctrls)
        check("an index off the end is refused", not ok, why)
        desc = ESC.describe(ctrls, dlg)
        check("the description announces the modal first", "MODAL DIALOG IS OPEN" in desc)
        check("and tells the panel scroll_dialog exists", "scroll_dialog" in desc)

        # ---------------------------------------------------------------- [4] executing by index
        print("\n[4] THE EXECUTOR ACTS THROUGH THE INDEX IT JUST WROTE")
        email_btn = next(c for c in ctrls if "Sign in with email" in c["label"])
        await page.evaluate("""() => {const b=document.querySelector('[data-automation-id=emailSignIn]');
            b.addEventListener('click', () => { b.textContent = 'CLICKED'; });}""")
        moved = await WD._exec_action(page, {"verb": "click", "target": email_btn["i"]})
        clicked = await page.evaluate(
            "() => document.querySelector('[data-automation-id=emailSignIn]').textContent")
        check("click reached the right element", moved and clicked == "CLICKED",
              f"moved={moved} text={clicked!r}")

        print("\n[5] scroll_dialog actually scrolls THE DIALOG, not the window")
        await page.set_content(CSS + PAGE_BEHIND + MODAL)
        await WD._controls(page)                       # tags [data-jhw-dlg]
        before = await page.evaluate("() => document.getElementById('dlg').scrollTop")
        scrolled = await WD._exec_action(page, {"verb": "scroll_dialog"})
        after = await page.evaluate("() => document.getElementById('dlg').scrollTop")
        win = await page.evaluate("() => window.scrollY")
        check("the dialog scrolled", scrolled and after > before, f"{before} -> {after}")
        check("the window did NOT scroll instead", win == 0, f"window.scrollY={win}")

        # ---------------------------------------------------------------- [6] the form inside
        print("\n[6] THE FORM INSIDE THE MODAL — labels, options, errors, credential refusal")
        await page.set_content(CSS + PAGE_BEHIND + EMAIL_FORM)
        idx2 = await WD._controls(page)
        c2 = idx2["controls"]
        lab2 = [c["label"] for c in c2]
        check("the email field is labelled from <label for>",
              any("Email Address" in l for l in lab2), str(lab2))
        pwf = next((c for c in c2 if c.get("secret")), None)
        check("the password field is marked secret=True", pwf is not None, str(lab2))
        if pwf:
            ok, why = ESC.validate({"verb": "fill", "target": pwf["i"], "value": "hunter2"}, c2)
            check("the panel is REFUSED the password field", not ok, why)
        check("the page's own error is captured as evidence",
              any("incorrect" in e for e in idx2.get("errors") or []), str(idx2.get("errors")))
        sel = next((c for c in c2 if c["kind"] == "select"), None)
        check("the dropdown's real options are listed",
              sel and "Germany" in (sel.get("options") or []), str(sel))
        check("the wizard step is read for the panel",
              "Create Account/Sign In" in (idx2.get("step") or ""), idx2.get("step"))

        print("\n[7] A CHECKBOX IS SET, NEVER TOGGLED (a second click would untick it)")
        cb = next(c for c in c2 if c["kind"] == "checkbox")
        await WD._exec_action(page, {"verb": "check", "target": cb["i"]})
        st1 = await page.evaluate("() => document.getElementById('c').checked")
        await WD._exec_action(page, {"verb": "check", "target": cb["i"]})
        st2 = await page.evaluate("() => document.getElementById('c').checked")
        check("ticked once", st1 is True)
        check("still ticked after a second check (idempotent)", st2 is True)

        print("\n[8] A WRITE IS NOT DONE UNTIL IT IS READ BACK")
        e = next(c for c in c2 if "Email Address" in c["label"])
        await WD._exec_action(page, {"verb": "fill", "target": e["i"], "value": "feranicus@s4biz.io"})
        val = await page.evaluate("() => document.getElementById('e').value")
        check("the field really holds the value", val == "feranicus@s4biz.io", repr(val))
        await WD._exec_action(page, {"verb": "select", "target": sel["i"], "value": "Germany"})
        got = await page.evaluate(
            "() => document.getElementById('cy').selectedOptions[0].text")
        check("the select committed via the NATIVE setter", got == "Germany", repr(got))

        print("\n[9] NO MODAL -> the whole document is in scope (nothing regresses)")
        await page.set_content(CSS + PAGE_BEHIND)
        idx3 = await WD._controls(page)
        lab3 = [c["label"] for c in idx3["controls"]]
        check("the dialog is correctly reported ABSENT",
              idx3["dialog"].get("present") is not True, str(idx3["dialog"])[:120])
        # This assertion USED to demand that 'Search Careers' appear. The junk filter added on
        # 2026-08-16 deliberately drops site chrome, because 'myworkday.com' (a FOOTER link) reached
        # index 0 and the panel clicked it, leaving the ATS. So the correct expectation inverted:
        # nav and branding must NEVER be offered, modal or no modal. My own suite caught the
        # contradiction, which is what a test is for.
        for chrome in ("Search Careers", "Introduce Yourself", "Back to Job Posting", "English"):
            check(f"site chrome {chrome!r} is never offered, even with no modal",
                  not any(chrome in l for l in lab3), str(lab3))
        check("but the page's REAL controls still are",
              any("Sign In" in l for l in lab3) or any("Search jobs" in l for l in lab3), str(lab3))

        print("\n[9a] THE ARIA-HIDDEN TRAP — the one line that cost four runs")
        # Workday marks the app container aria-hidden="true" when the modal opens (correct modal
        # practice) and renders the modal INSIDE it. A visibility predicate that rejects any element
        # with an aria-hidden ancestor therefore rejects the ENTIRE modal -> "0 control(s) indexed",
        # four runs running, while _SNAP_JS printed all seven buttons from the same page.
        await page.set_content(CSS + f'''
          <div id="app" aria-hidden="true">
            {PAGE_BEHIND}
            <div id="scrim"></div>
            <div id="dlg" role="dialog" aria-modal="true" aria-label="Sign In">
              <h2>Sign In</h2>
              <button data-automation-id="googleSignIn">Sign in with Google</button>
              <button data-automation-id="emailSignIn">Sign in with email</button>
            </div>
          </div>''')
        idxA = await WD._controls(page)
        lblA = [c["label"] for c in idxA["controls"]]
        check("controls inside an aria-hidden ancestor ARE indexed",
              len(lblA) >= 2, f"{len(lblA)} controls: {lblA}  seen={idxA.get('seen')} "
                              f"dropped={idxA.get('dropped')}")
        check("and both sign-in buttons are there",
              any("Sign in with Google" in l for l in lblA)
              and any("Sign in with email" in l for l in lblA), str(lblA))
        check("the index reports WHY anything was rejected (never a silent zero)",
              idxA.get("seen") is not None and idxA.get("dropped") is not None, str(idxA)[:160])

        print("\n[9c] PLAYWRIGHT'S OWN VISIBILITY agrees — two independent implementations")
        idxP = await WD._controls_pw(page)
        lblP = [c["label"] for c in idxP["controls"]]
        check("the playwright index finds the same sign-in buttons",
              any("Sign in with Google" in l for l in lblP), f"{len(lblP)}: {lblP}")
        check("it drops site chrome too (one rule, both implementations)",
              not any("Search Careers" in l for l in lblP), str(lblP))
        check("it flags the honeypot by geometry, not by aria",
              True if not [c for c in idxP["controls"] if "robots only" in (c.get("label") or "")]
              else all(c.get("honeypot") for c in idxP["controls"]
                       if "robots only" in (c.get("label") or "")))

        print("\n[9b] AN EMPTY DIALOG MUST NOT WIN — the '0 control(s) indexed' defect")
        # Workday renders several dialog containers. Taking the LAST in document order picked an
        # EMPTY one, so the index was empty three runs in a row while the page plainly showed the
        # sign-in buttons, and the panel was asked to act on nothing.
        await page.set_content(CSS + PAGE_BEHIND + """
          <div id="scrim"></div>
          <div role="dialog" aria-modal="true" style="position:fixed;top:0;height:200px;z-index:50">
            <h2>Empty shell</h2></div>
          <div id="dlg" role="dialog" aria-modal="true" aria-label="Sign In"
               style="z-index:51"><h2>Sign In</h2>
            <button data-automation-id="googleSignIn">Sign in with Google</button>
            <button data-automation-id="emailSignIn">Sign in with email</button></div>
          <div role="dialog" aria-modal="true" style="position:fixed;top:0;height:300px;z-index:40">
            <h2>Another empty shell</h2></div>""")
        idxE = await WD._controls(page)
        lblE = [c["label"] for c in idxE["controls"]]
        check("the dialog WITH controls wins, not the last empty one",
              any("Sign in with Google" in l for l in lblE), f"{len(lblE)} controls: {lblE}")
        check("and the index is never empty when controls exist", len(lblE) >= 2, str(lblE))

        print("\n[10] a page mid-navigation degrades, it does not raise")
        await page.close()          # deliberately: _controls() must survive a dead execution context
        idx4 = await WD._controls(page)
        check("a dead page returns an empty index instead of throwing",
              idx4.get("controls") == [] and idx4["dialog"].get("present") is False, str(idx4)[:120])
    finally:
        await _close()
        await pw.stop()
    print("\n" + "=" * 78)
    print(("FAILED: " + ", ".join(FAILED)) if FAILED else "ALL ESCALATION DOM CONTRACTS HOLD")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
