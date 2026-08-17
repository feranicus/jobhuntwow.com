"""Deterministic Workday driver — built on VERIFIED research (see agent/RESEARCH_ATS_AUTOMATION.md).

Implements the 5 researched fixes:
  1. RESUME PARSE WAIT — Workday parses the resume SERVER-SIDE and may overwrite fields. Delete any
     existing attachment first (else uploads stack), upload, then WAIT for the parse to complete before
     clicking Save and Continue. THIS was the bug that stalled every run.
  2. DEEP-LINK to {job}/apply/applyManually — skips `adventureButton`, which reference repos had to
     click twice (flaky). Workday bounces to auth, then returns to the apply flow.
  3. SELECTOR MAP AS DATA with fallback chains (selectors/workday.json) — tenants run different Workday
     UI versions; a single selector silently fails.
  4. SELECTOR = RUNTIME ASSERTION — on an unknown page or a persistent error we HALT and tell the human
     on Telegram. We never guess. (Kills "hallucinated success", the #1 agent failure mode.)
  5. LLM ONLY FOR FREE TEXT — screening answers via a text-only model. The LLM NEVER drives the browser.

SUBMITS at Review (authorised 2026-07-21, restated 2026-08-16) and claims success
ONLY on the site's own confirmation. JHW_AUTOSUBMIT=0 restores stop-at-Review.
"""
from __future__ import annotations
import asyncio, json, os, re, sys, time as _time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # /agent  (import ask)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                    # /agent/flows
from playwright.async_api import async_playwright
try:
    import ask
except Exception:
    ask = None
try:
    import autofill
except Exception:
    autofill = None

CDP_URL  = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
# Every Playwright action is capped at this. NOT the element-search timeouts (those are per call) --
# this is the ceiling that stops an inherited 30s default turning one dead field into half a minute.
ACTION_TIMEOUT = int(os.getenv("JHW_ACTION_TIMEOUT", "6000"))
# Run-scoped facts the whole adapter shares. Lists so a nested function can set them.
VISION_FIRST = os.getenv("JHW_VISION_FIRST", "1").lower() not in ("0", "false", "no")
_GOOGLE_DEAD = [False]      # set once we have bounced off an identity provider
_GOOGLE_TRIED = [False]     # a password is typed at most ONCE per run
_gate_url = [""]            # the apply-wizard URL, so we can get BACK to it after an IdP detour
ACCOUNTS = os.path.join(os.getenv("JHW_OUT", "/agent/out"), "ats_accounts.json")
SEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selectors", "workday.json")


def _load_selectors() -> dict:
    try:
        return {k: v for k, v in json.load(open(SEL_FILE, encoding="utf-8")).items() if not k.startswith("_")}
    except Exception as e:
        print(f"[wd] selector map missing ({e}) — using empty map", flush=True)
        return {}


SEL = _load_selectors()


def _chain(key) -> list:
    v = SEL.get(key, [])
    return v if isinstance(v, list) else [v]


# ---------- selector helpers (#3 fallback chains) --------------------------------------------------
async def _first(page, key, t=1500):
    """First VISIBLE locator in the fallback chain, else None."""
    for s in _chain(key):
        try:
            loc = page.locator(s).first
            await loc.wait_for(state="visible", timeout=t)
            return loc
        except Exception:
            continue
    return None


async def _present(page, key):
    """First PRESENT locator (visibility not required) — for hidden file inputs."""
    for s in _chain(key):
        try:
            loc = page.locator(s)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


async def _has(page, key, t=1200) -> bool:
    return (await _first(page, key, t)) is not None


async def _click(page, key, t=4000) -> bool:
    el = await _first(page, key, t)
    if not el:
        return False
    try:
        await el.click(timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(600); return True
    except Exception:
        return False


async def _fill(page, key, value, t=4000) -> bool:
    if not value:
        return False
    el = await _first(page, key, t)
    if not el:
        return False
    try:
        # explicit, because an inherited default is how 30s hides inside a 4s-looking call
        await el.click(timeout=ACTION_TIMEOUT)
        await el.fill(str(value), timeout=ACTION_TIMEOUT)
        return True
    except Exception:
        return False


async def _click_role(page, role, name_rx, t=3000) -> bool:
    """Text/ARIA fallback when a tenant's data-automation-id differs."""
    try:
        el = page.get_by_role(role, name=re.compile(name_rx, re.I)).first
        await el.wait_for(state="visible", timeout=t)
        await el.click(timeout=ACTION_TIMEOUT)
        await page.wait_for_timeout(600); return True
    except Exception:
        return False


async def _has_role(page, role, name_rx, t=1000) -> bool:
    try:
        return await page.get_by_role(role, name=re.compile(name_rx, re.I)).first.is_visible(timeout=t)
    except Exception:
        return False


# ---------- credentials ----------------------------------------------------------------------------
def _host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _load_accounts() -> dict:
    """`{host: {email,password}}` — DELEGATED. `flows/atscreds.py` is the single owner.

    THE OPEN ITEM THIS CLOSES, measured 2026-08-17. This function read `out/ats_accounts.json` itself
    and keyed by HOSTNAME (`kyndryl.wd5.myworkdayjobs.com`), while `atscreds` wrote the SAME FILE keyed
    by vendor TENANT (`kyndryl.wd5`). One fact, one file, two keys — so `atscreds.account()` looked up
    the tenant, found nothing, reported "no account recorded yet" and generated a FRESH password for a
    tenant whose working one sat three lines away. It failed silently, which is the worst property a
    credential store can have, and it is the "one value, several homes, the stale one wins" defect
    this project has paid for repeatedly.
    `atscreds.host_view()` exposes every record under its hostname aliases too, so all six call sites
    in this file keep working and nothing that already worked stops working."""
    try:
        import atscreds as _AC
        return _AC.host_view()
    except Exception as e:
        _log(f"    the account store is unavailable ({type(e).__name__}) — treating it as empty")
        return {}


def _save_account(host, email, password):
    """Write through the ONE owner, which migrates a legacy hostname key to the canonical tenant."""
    try:
        import atscreds as _AC
        _AC.remember(str(host) if "://" in str(host) else f"https://{host}/", email, password)
    except Exception as e:
        _log(f"    could not record the account ({type(e).__name__}: {str(e)[:60]})")


async def _ask(q: str, scope: str = "global") -> str:
    """Ask the human -- but ONLY if we have not already been told the answer.

    "it doesn't make sense to bother me if this is supposed to be a self-serviced agent running
    24/7" -- so every answer is remembered and replayed. `learned.py` refuses to store credentials
    (atscreds owns those), so a password is asked for every time BY DESIGN and never written into a
    replayable file."""
    prior = None
    try:
        import learned
        prior = learned.recall(q, scope)
    except Exception as e:
        _log(f"    learned-answer store unavailable ({type(e).__name__})")
    if prior:
        _log(f"    already know this one -> {str(prior)[:50]!r} (not asking you again)")
        return str(prior)
    if not ask:
        return ""
    try:
        reply = await ask.ask_human(q) or ""
    except Exception as e:
        _log(f"    could not reach you ({type(e).__name__})")
        return ""
    reply = str(reply).strip()
    if reply and not reply.upper().startswith("NO_ANSWER"):
        try:
            import learned
            learned.remember(q, reply, scope=scope, source="human")   # refuses secrets itself
        except Exception:
            pass
    return reply


async def _notify(m: str):
    if ask:
        try:
            await ask.notify(m)
        except Exception:
            pass


def _split_address(addr: str):
    line1, postal, city = addr, "", ""
    parts = [p.strip() for p in addr.split(",")]
    if parts:
        line1 = parts[0]
    rest = " ".join(parts[1:]) if len(parts) > 1 else ""
    m = re.search(r"\b(\d{4,6})\b", rest)
    if m:
        postal = m.group(1)
        city = re.sub(r"\(.*?\)", "", rest.replace(postal, "")).strip()
    return line1, postal, city


# ---------- IDENTITY PROVIDERS ---------------------------------------------------------------------
# MEASURED 2026-08-16: clicking "Sign in with Google" walked the driver onto
# accounts.google.com/v3/signin/challenge/pwd and the page loop then iterated SEVEN times over
# 230 SECONDS doing nothing, because nothing noticed we had left the ATS. `visible: 2 inputs` and
# `buttons: ['Next','Try another way']` is Google's password challenge, not a Workday form.
_IDP_RX = (r"accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|"
           r"\.okta\.com|\.onelogin\.com|\.pingidentity\.com|auth0\.com|linkedin\.com/(uas|checkpoint)")


def _on_idp(url: str) -> bool:
    return bool(re.search(_IDP_RX, (url or "").lower()))


async def _regate(page) -> bool:
    """Get back to the APPLY GATE, not merely back to the ATS.

    MEASURED 2026-08-16: `go_back` from accounts.google.com returned to the JOB POSTING page
    (`'Skip to main content', 'Sign In', 'Privacy Policy'`), so the panel was shown a page with no
    sign-in form on it and, quite reasonably, proposed clicking 'Sign In'. Coming back from an
    identity provider is not the same as being on the gate."""
    if not _gate_url[0]:
        return False
    if re.search(r"/apply", (page.url or ""), re.I):
        return True
    _log("    that is the job page, not the gate — re-entering the apply wizard")
    try:
        await page.goto(_gate_url[0], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
        await _enter_from_choice(page)
        await _reveal_email_login(page)
        _log(f"    back at the gate -> {(page.url or '')[:70]}")
        return True
    except Exception as e:
        _log(f"    could not re-enter the gate ({type(e).__name__}: {str(e)[:60]})")
        return False


async def _leave_idp(page, r: dict) -> bool:
    """We are on a third-party identity provider's sign-in page. TRY IT ONCE, then leave.

    THIS FUNCTION USED TO REFUSE OUTRIGHT, and the refusal was mine, not measured. The docstring
    argued that Google blocks CDP-driven sign-in, that a Workspace account is MFA-gated, and that the
    credential is too valuable to risk. Then the operator handed over his own recording
    (recordings/'accenture workday.py'), which walks `Sign in with Google` -> `Email or phone` ->
    `Next` -> `Enter your password` -> `Next` and arrives back on the ATS, signed in. A theory does
    not outrank a recording, and six runs were spent proving the theory expensive.
    WHAT SURVIVES OF THE OLD REASONING, because these parts were right:
      * it is tried ONCE per run (`_GOOGLE_TRIED`) -- repeated password attempts are how an account
        gets locked, and that mailbox gates every ATS verification e-mail we depend on;
      * a CHALLENGE is a STOP, not a retry: 2-step, a passkey, or "this browser or app may not be
        secure" all hand back to the human, who signs in once in noVNC and the persistent profile
        remembers it;
      * an ATS-LOCAL account is still preferred and still first in the ladder -- no third party, no
        MFA, reusable across every tenant.
    Only when the sign-in does not complete do we go back to the ATS and take the e-mail path."""
    if not _GOOGLE_DEAD[0] and os.getenv("JHW_ATS_ALLOW_SSO", "1").lower() \
            not in ("0", "false", "no"):
        if await _google_signin(page):
            _log("    signed in via Google — back on the ATS")
            await _regate(page)
            return True
    # It did not complete. NOW the answer is known for the rest of the run: withdraw the verb, or the
    # panel keeps proposing the one thing that cannot work and every round burns another ~80s.
    _GOOGLE_DEAD[0] = True
    try:
        import escalate as _ESC
        _ESC.withdraw("google_sso", "no usable session; it lands on the account chooser")
    except Exception:
        pass
    _log(f"LEFT THE ATS -> identity provider ({(page.url or '')[:60]}). Not typing a password here.")
    _log("    reason: Google blocks automated sign-in + MFA, and an ATS-local account is better.")
    _log("    Google SSO is now WITHDRAWN for the rest of this run (no live session in the profile).")
    # (the re-entry to the apply gate happens at the end of this function -- see _regate)
    for _ in range(4):
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=12000)
        except Exception:
            break
        await page.wait_for_timeout(1200)
        if not _on_idp(page.url or ""):
            await _regate(page)
            _log(f"    back on the ATS -> {(page.url or '')[:70]}")
            return True
    # go_back exhausted. Navigate STRAIGHT to the gate rather than giving up -- we know its URL.
    _log("    going back did not work — navigating directly to the apply gate")
    if await _regate(page):
        return True
    _log("    could not get back to the ATS at all")
    return False


# ---------- SUBMIT ---------------------------------------------------------------------------------
# The candidate authorised end-to-end submission (CLAUDE.md, 2026-07-21: "I already validated
# everything no need to bother the stakeholder again. please make it fully automated till the end")
# and restated it 2026-08-16: the agents must apply 24/7 on his behalf and only ask on Telegram when
# genuinely blocked. This module's docstring used to say "Never submits" -- that was inconsistent
# with the Phenom path, which has submitted since July.
# JHW_AUTOSUBMIT=0 restores stop-at-Review for a tenant you want to eyeball first.
AUTOSUBMIT = os.getenv("JHW_AUTOSUBMIT", "1").lower() not in ("0", "false", "no")


# A WORKDAY PROMPT IS A BUTTON WHOSE NAME CARRIES ITS CURRENT VALUE.
# From his intive recording, verbatim:
#     button("How Did You Hear About Us?").click()  ->  get_by_text("LinkedIn corporate page").click()
#     button("Country Georgia Required").click()    ->  get_by_text("Germany").click()
# So the handle is not a label beside a field: it IS the button, and after it is answered its name
# CHANGES to the answer ("Georgia" -> "Germany"). That is also the read-back this project insists on:
# a write is not done until it has been read back.
# MEASURED, intive 2026-08-17: the run left BOTH of these empty and the site said so —
#   "The field How Did You Hear About Us? is required and must have a value."
# and the log showed the buttons plainly: ['Select One', 'Georgia', 'Mobile', 'Next'].
_NAV = re.compile(r"^\s*(next|back|previous|zur[uü]ck|weiter|continue|save and continue|submit|"
                  r"absenden|apply|accept cookies|select file|close|delete|calendar|next month|"
                  r"sign in|create account|autofill|errors? found|error-)", re.I)
_PROMPT_RX = re.compile(r"select one|required$|^select\b", re.I)


# WHICH FIELDS DID THE SITE NAME? Workday says it twice, in two shapes:
#   "Error: The field How Did You Hear About Us? is required and must have a value."
#   "Error-Phone Number Enter a valid format for Phone Number."
_ERR_FIELD = re.compile(r"the field\s+(.+?)\s+is required|Error-([A-Za-z0-9 /?'&.-]{3,40})", re.I)


def error_fields(err: str) -> list:
    """The field names the site itself named. PURE, so it is a test and not a hope."""
    out = []
    for m in _ERR_FIELD.finditer(err or ""):
        nm = " ".join((m.group(1) or m.group(2) or "").split()).strip(" .:*")
        # `Error-<Field>` runs straight into the sentence that follows it, so a greedy capture yields
        # 'Phone Number Enter a valid format for Ph'. Used as a filter, a junk name that long matches
        # the wrong control — so cut at the prose the site appends.
        nm = re.split(r"\bthe field\b|\benter a\b|\bis required\b|\bmust have\b",
                      nm, flags=re.I)[0].strip(" .:*-")
        if 2 < len(nm) <= 44 and nm.lower() not in [o.lower() for o in out]:
            out.append(nm)
    return out


async def _phone_block(page, data: dict, r: dict) -> bool:
    """Country Phone Code + the NATIONAL number + device type — the three-control truth.

    MEASURED on intive: the number box wants `15785541545`, the country is a SEPARATE control, and the
    run left it on its default — the log printed `buttons: [... 'Georgia', 'Mobile', ...]`, Georgia,
    because nothing had set it. A number without its country code is not a phone number to Workday."""
    b = (data or {}).get("basics") or {}
    try:
        from greenhouse import phone_national as _natl
    except Exception:
        from flows.greenhouse import phone_national as _natl        # type: ignore
    natl = (b.get("phone_national") or "").strip() or _natl(b.get("phone", ""))
    want_country = (b.get("phone_country") or b.get("country") or "Germany").strip()
    did = False
    # THE COUNTRY FIRST: on several tenants setting it re-validates (and sometimes re-formats) the
    # number, so doing it afterwards would throw the number away.
    for key, want in (("phone_code", want_country), ("phone_device", b.get("phone_type", "Mobile"))):
        try:
            el = await _first(page, key, 2000)
            if el is None:
                continue
            cur = " ".join((((await el.get_attribute("aria-label")) or
                             (await el.inner_text()) or "").strip()).split())
            if want.lower() in (cur or "").lower():
                continue                                    # already right; never toggle a good value
            await el.click(timeout=ACTION_TIMEOUT)
            await page.wait_for_timeout(500)
            for how in ("option", "text"):
                try:
                    row = (page.get_by_role("option", name=want, exact=False).first if how == "option"
                           else page.get_by_text(want, exact=True).first)
                    if await row.count() and await row.is_visible():
                        await row.click(timeout=ACTION_TIMEOUT)
                        break
                except Exception:
                    continue
            await page.wait_for_timeout(400)
            now = " ".join((((await el.get_attribute("aria-label")) or
                             (await el.inner_text()) or "").strip()).split())
            ok = want.lower() in (now or "").lower()
            _log(f"    {key:<12} {'=' if ok else '?'} {want!r}" +
                 ("" if ok else f"  (it now reads {now[:34]!r})"))
            did |= ok
            if ok:
                r["filled"].append(key)
        except Exception as e:
            _log(f"    {key:<12} {type(e).__name__}")
    if natl:
        if await _fill(page, "phone", natl):
            back = ""
            try:
                el = await _first(page, "phone", 1500)
                back = (await el.input_value()) if el is not None else ""
            except Exception:
                pass
            _log(f"    phone        = {natl!r}" + (f"  (reads {back!r})" if back and back != natl else ""))
            r["filled"].append("phone")
            did = True
        else:
            _log("    phone        NOT FOUND on this page")
    return did


async def _repair_validation(page, data: dict, err: str, r: dict) -> bool:
    """The site named the broken fields. Fix THOSE, then let the loop press Next again."""
    named = error_fields(err)
    _log(f"    the site named: {named or '(nothing parseable)'}")
    fixed = False
    if any("phone" in n.lower() for n in named) or "phone" in (err or "").lower():
        fixed |= await _phone_block(page, data, r)
    if named:
        try:
            fixed |= bool(await _answer_prompts(page, data, only=named))
        except Exception as e:
            _log(f"    prompt repair: {type(e).__name__}: {e}")
    if not fixed:
        # HIS QUESTION, VERBATIM: *"why our system didn't ask me something like: Can you please put
        # proper phone number?"*. Because nothing asked — the loop only ever halted. A FORMAT error is
        # the most answerable question there is: the site has told us the field, we can read back what
        # is in it, and he knows what it should be. So ask, type the reply verbatim, and REMEMBER it,
        # so the next tenant never asks again.
        for nm in named:
            cur = ""
            try:
                el = page.get_by_role("textbox", name=nm, exact=False).first
                if await el.count():
                    cur = (await el.input_value()) or ""
            except Exception:
                pass
            reply = await _ask(
                f"Workday ({_host(page.url)}) rejects this value for *{nm}*:\n\n"
                f"`{cur or '(empty)'}`\n\nIts message: {(err or '')[:120]}\n\n"
                f"Reply with EXACTLY what I should type into that box.",
                scope=f"format:{nm.lower()}")
            reply = (reply or "").strip()
            if not reply:
                continue
            try:
                el = page.get_by_role("textbox", name=nm, exact=False).first
                if await el.count():
                    await el.fill(reply, timeout=ACTION_TIMEOUT)
                    back = (await el.input_value()) or ""
                    _log(f"    {nm}: your answer {reply!r} typed (reads {back!r})")
                    fixed |= back.strip() == reply
                    r["filled"].append(f"asked:{nm[:18]}")
            except Exception as e:
                _log(f"    could not type your answer into {nm!r}: {type(e).__name__}")
    return fixed


async def _answer_prompts(page, data: dict, only=None) -> int:
    """Answer every unanswered Workday PROMPT from the recorded human choice. Returns how many.

    THE ANSWER COMES FROM HIS OWN RECORDING FIRST, and that matters here more than anywhere: the
    profile says `how_did_you_hear: LinkedIn`, and on this tenant there IS NO option called
    "LinkedIn" — the row reads "LinkedIn corporate page". A value from the profile could never have
    been clicked. Only what a human actually chose on this vendor's form is usable."""
    try:
        from recordings import known_choice
    except Exception:
        try:
            from flows.recordings import known_choice          # type: ignore
        except Exception:
            return 0
    done = 0
    try:
        btns = page.get_by_role("button")
        n = min(await btns.count(), 60)
    except Exception:
        return 0
    for i in range(n):
        try:
            b = btns.nth(i)
            if not await b.is_visible():
                continue
            nm = ((await b.get_attribute("aria-label")) or (await b.inner_text()) or "").strip()
            nm = " ".join(nm.split())[:80]
            if not nm or _NAV.search(nm):
                continue
            # `only` = the field names the SITE named in its error. A bare value like 'Georgia' is not
            # prompt-shaped, so without this branch the country control could never be repaired.
            if only is not None:
                if not any(o.lower() in nm.lower() or nm.lower() in o.lower() for o in only):
                    continue
            elif not _PROMPT_RX.search(nm):
                continue
            want = known_choice("workday", nm)
            if not want:
                # The country prompt's name carries the CURRENT value ("Country Georgia Required"),
                # so an exact key will not match another tenant. Fall back to the QUESTION word.
                for probe in ("How Did You Hear About Us?", "Country"):
                    if probe.split()[0].lower() in nm.lower():
                        want = known_choice("workday", probe) or want
                        if want:
                            break
            if not want:
                _log(f"    prompt {nm[:44]!r}: no recorded human choice — leaving it to the ladder")
                continue
            await b.click(timeout=ACTION_TIMEOUT)
            await page.wait_for_timeout(600)
            picked = False
            for how in ("option", "text"):
                try:
                    row = (page.get_by_role("option", name=want, exact=False).first if how == "option"
                           else page.get_by_text(want, exact=True).first)
                    if await row.count() and await row.is_visible():
                        await row.click(timeout=ACTION_TIMEOUT)
                        picked = True
                        break
                except Exception:
                    continue
            await page.wait_for_timeout(500)
            # READ IT BACK: the button's own name becomes the answer.
            now = ""
            try:
                now = " ".join((((await b.get_attribute("aria-label")) or
                                 (await b.inner_text()) or "").strip()).split())[:80]
            except Exception:
                pass
            if picked and want.lower() in now.lower():
                _log(f"    prompt {nm[:40]!r} <- {want!r}  (RECORDED human choice, read back)")
                done += 1
            elif picked:
                _log(f"    prompt {nm[:40]!r} <- clicked {want!r} but it now reads {now[:40]!r}")
            else:
                _log(f"    prompt {nm[:40]!r}: {want!r} was not on the list this tenant offers")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception:
            continue
    return done


async def _submit_now(page, r: dict) -> bool:
    """Click Submit and PROVE it went through.

    Two rules carried over from llm_driver, both learned the hard way:
      1. NEVER claim submitted because a click threw no error. Read the page back and require the
         SITE's own confirmation. An unconfirmed submit is a FAILURE to report, not a detail.
      2. NEVER submit over a required-and-empty field. A half-filled application is worse than a
         paused one, because nothing in the log admits it.
    SUBMIT_JS/CONFIRM_JS are imported from llm_driver so the Submit-matching regex has ONE home;
    its anchored pattern is why 'Back', 'Save and Continue' and 'Next' can never match."""
    if not AUTOSUBMIT:
        _log("auto-submit disabled (JHW_AUTOSUBMIT=0) — stopping at Review for you")
        return False
    sn = await _snapshot(page)
    if sn.get("reqEmpty"):
        _log(f"REFUSING to submit: still required+empty -> {sn['reqEmpty']}")
        r["note"] += f" Refused to submit with empty required fields: {sn['reqEmpty']}."
        return False

    # PRE-SUBMIT AUDIT. Deterministic checks DECIDE; a two-vendor panel only advises and can never
    # block (a model hiccup must not cost a real application). Fails OPEN by design.
    try:
        from presubmit import audit as _presubmit
        from workday_wd import candidate_answers as _cand
        _out = os.getenv("JHW_OUT", "/agent/out")
        _docs = {f: os.path.isfile(os.path.join(_out, f))
                 for f in ("resume.pdf", "cover_letter.pdf")}
        _jd = ""
        try:
            _jd = json.load(open(os.path.join(_out, "job.json"), encoding="utf-8")).get("description", "")
        except Exception:
            pass
        _dec = await _presubmit(page, _cand(), _docs, _jd)
        if not _dec.get("allow"):
            _log(f"PRE-SUBMIT AUDIT BLOCKED the submission: {_dec.get('blocking')}")
            r["stage"] = "blocked"
            r["note"] += (" Pre-submit audit blocked it: " + "; ".join(_dec.get("blocking") or []) + ".")
            await _notify("Workday: NOT submitted — pre-submit audit blocked it. "
                          + "; ".join(_dec.get("blocking") or []))
            return False
        for _f in (_dec.get("soft") or []):
            _log(f"    note (not blocking): {_f}")
        if _dec.get("flags"):
            _log(f"    panel flagged {len(_dec['flags'])} thing(s) — submitting anyway, as designed")
            await _notify("Workday: submitting, but the review panel flagged: "
                          + " | ".join(_dec["flags"][:3]))
    except Exception as e:
        _log(f"pre-submit audit unavailable ({type(e).__name__}) — continuing to submit")
    try:
        from llm_driver import SUBMIT_JS, CONFIRM_JS
    except Exception as e:
        _log(f"cannot import the submit blobs from llm_driver ({e}) — not submitting")
        return False
    res = await page.evaluate(SUBMIT_JS) or {}
    if not res.get("ok"):
        _log(f"submit: {res.get('why')}")
        return False
    _log(f"clicking {res.get('label')!r} …")
    try:
        await page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)
    body, url = "", (page.url or "")
    try:
        body = await page.evaluate(CONFIRM_JS) or ""
    except Exception:
        pass
    ok = bool(re.search(r"thank you|thanks for (your )?appl|application (has been )?"
                        r"(submitted|received|sent)|successfully submitted|vielen dank|"
                        r"bewerbung .{0,20}(eingegangen|übermittelt)", body, re.I)) \
         or bool(re.search(r"confirm|thank|success|complete", url, re.I))
    snip = " ".join(body.split())[:150]
    if ok:
        r["submitted"] = True
        _log(f"SUBMITTED — the site says: {snip!r}")
    else:
        r["submitted"] = False
        _log(f"submit clicked but NO confirmation found. url={url[:80]} | page says: {snip!r}")
    return ok


# ---------- STEP LOGGING ---------------------------------------------------------------------------
# WHY THIS EXISTS: an Accenture run went SILENT FOR 183 SECONDS and then printed one line,
# "no Save-and-Continue button found — paused for the human", with filled=0. That message names
# what was ABSENT and says nothing about what was PRESENT, so it is unactionable -- the same defect
# CLAUDE.md records for validation messages without their field. The page loop ran ten iterations
# with no output at all. llm_driver has 56 prints and is debuggable; this adapter had 8.
_T0 = [0.0]


def _log(msg: str) -> None:
    el = _time.time() - (_T0[0] or _time.time())
    print(f"[wd {el:6.1f}s] {msg}", flush=True)


_SNAP_JS = r"""() => {
  const vis=(el)=>{const r=el.getBoundingClientRect();const s=getComputedStyle(el);
    return r.width>2&&r.height>2&&s.visibility!=='hidden'&&s.display!=='none';};
  const T=(e)=>(e.innerText||e.value||'').replace(/\s+/g,' ').trim().slice(0,38);
  const btns=[...document.querySelectorAll('button,[role=button],input[type=submit]')]
    .filter(vis).map(T).filter(Boolean);
  const stepEl=document.querySelector('[data-automation-id=progressBarActiveStep]');
  const lab=(e)=>{let t='';if(e.id){const l=document.querySelector('label[for="'+CSS.escape(e.id)+'"]');if(l)t=l.innerText;}
    return (t||e.getAttribute('aria-label')||e.name||e.type||'').replace(/\s+/g,' ').trim().slice(0,38);};
  const reqEmpty=[...document.querySelectorAll('input,select,textarea')].filter(vis).filter(e=>{
    const req=e.required||e.getAttribute('aria-required')==='true';
    const empty=(e.type==='checkbox'||e.type==='radio')?!e.checked:!String(e.value||'').trim();
    return req&&empty;}).map(lab).filter(Boolean);
  const files=[...document.querySelectorAll('input[type=file]')].length;
  return {step:(stepEl?stepEl.innerText:'').replace(/\s+/g,' ').trim().slice(0,64),
          h:((document.querySelector('h1,h2,h3')||{}).innerText||'').replace(/\s+/g,' ').trim().slice(0,64),
          buttons:[...new Set(btns)].slice(0,14),
          reqEmpty:[...new Set(reqEmpty)].slice(0,12),
          files, inputs:[...document.querySelectorAll('input,select,textarea')].filter(vis).length};
}"""


async def _snapshot(page) -> dict:
    """What the driver can SEE right now: stepper position, heading, every visible button, every
       required-and-empty field. Printed each iteration so a stall is never silent again."""
    try:
        return await page.evaluate(_SNAP_JS)
    except Exception as e:
        return {"step": "", "h": "", "buttons": [], "reqEmpty": [], "files": 0,
                "inputs": 0, "err": f"{type(e).__name__}: {e}"[:70]}


async def _log_page(page, tag: str) -> dict:
    sn = await _snapshot(page)
    _log(f"--- {tag} | {(page.url or '')[:78]}")
    if sn.get("err"):
        _log(f"    snapshot FAILED: {sn['err']}  (page mid-navigation?)"); return sn
    if sn.get("step"):
        _log(f"    stepper : {sn['step']}")
    if sn.get("h"):
        _log(f"    heading : {sn['h']}")
    _log(f"    visible : {sn.get('inputs',0)} inputs, {sn.get('files',0)} file input(s)")
    _log(f"    buttons : {sn.get('buttons') or '(none visible)'}")
    if sn.get("reqEmpty"):
        _log(f"    REQUIRED+EMPTY: {sn['reqEmpty']}")
    return sn


# ---------- THE ESCALATION RUNG: dialog-aware control index -----------------------------------------
# WHY THIS EXISTS. The candidate photographed a Workday SIGN-IN MODAL sitting over the job page and
# said "I think our llms do not even see it". He was right, and the log proves it: _SNAP_JS
# enumerated buttons from the WHOLE document, so it reported
#     buttons : ['English','Sign In','Search Careers','Introduce Yourself','Back to Job Posting']
# -- every one of those belongs to the page BEHIND the overlay. The modal's own controls were never
# in the list, and the dialog is TALLER THAN THE VIEWPORT so the email/password path sits below the
# fold. A driver cannot act on what it is not shown.
# So: when a modal is open it OWNS the page, and the index is scoped to it.
#
# The index is also the SAFETY CONTRACT for escalate.py. A model answers with an [index] that
# already exists, so it is structurally incapable of inventing a selector -- guessed Workday
# selectors have cost this project hours.
_CTRL_JS = r"""() => {
  // vis(): geometry + CLIPPING, not just display/visibility. Accenture's honeypot
  // (data-automation-id=beecatcher, name=website) is display:block + visibility:visible and hidden
  // by clip/clip-path, and its offsetParent is NOT null. A predicate that only asks about display
  // indexes every bot trap on the internet.
  const hp=(el,s,r)=>{
    if(r.width<2||r.height<2) return true;
    if(s.opacity==='0') return true;
    const cp=(s.clipPath||'').replace(/\s+/g,' ');
    if(/^polygon\(/.test(cp)&&!/[1-9]/.test(cp)) return true;
    const cl=(s.clip||'').replace(/\s+/g,' ');
    if(/^rect\(/.test(cl)&&!/[2-9]|\d\d/.test(cl)) return true;
    if(/beecatcher|honeypot|robots only/i.test(
        (el.getAttribute('data-automation-id')||'')+' '+(el.name||'')+' '+
        (el.getAttribute('aria-label')||''))) return true;
    return false;};
  // MEASURED 2026-08-16, and this ONE LINE cost four runs:
  //     if(el.closest('[aria-hidden="true"]')) return false;
  // When Workday opens the sign-in modal it marks the app container aria-hidden="true" -- that is
  // CORRECT modal practice, it hides the page behind from screen readers -- and the modal itself
  // renders INSIDE that container. So every control in the modal had an aria-hidden ancestor and was
  // rejected: `0 control(s) indexed`, three rounds running, while _SNAP_JS (which has no such check)
  // printed all seven buttons from the same page one line earlier. Two blobs, one page, opposite
  // answers -- that disagreement was the evidence and I should have compared them on run one.
  // aria-hidden is an ACCESSIBILITY hint, not a visibility fact. VISIBILITY IS GEOMETRY AND STYLE,
  // which is exactly how Playwright, Selenium and the WAI-ARIA spec all define it. The honeypot is
  // still caught, because a bot trap hides itself with clip/clip-path/zero area -- geometry.
  let seen=0, dropped={hidden:0,area:0,clipped:0,detached:0};
  const vis=(el)=>{seen++;
    const s=getComputedStyle(el);
    if(s.visibility==='hidden'||s.display==='none'){dropped.hidden++;return false;}
    const r=el.getBoundingClientRect();
    if(r.width<=0&&r.height<=0){dropped.area++;return false;}
    if(el.offsetParent===null&&s.position!=='fixed'&&s.position!=='sticky'){dropped.detached++;return false;}
    return true;};
  const T=(e)=>(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();

  // ---- find the TOPMOST modal. Workday uses role=dialog; some tenants only set aria-modal or a
  // data-automation-id containing 'Modal'/'popup'. Take the LAST visible match = topmost.
  // WHICH dialog owns the page. MEASURED 2026-08-16: this used to take cand[cand.length-1] -- the
  // LAST match in DOCUMENT ORDER -- and Workday renders several dialog containers. It picked an
  // EMPTY one, so `root.querySelectorAll` found nothing and the run printed
  //     0 control(s) indexed
  // three times while the page plainly showed 'Sign in with Google' and 'Sign in with email'. The
  // panel was then asked to act on NOTHING and, reasonably, guessed. Two changes:
  //   * a dialog only qualifies if it actually CONTAINS interactive controls;
  //   * among those, the one with the highest stacking order wins, then the most controls.
  const FOCUSABLE='button,[role=button],a[href],input,select,textarea';
  const zOf=(el)=>{let z=0,n=el;while(n&&n!==document.body){const v=parseInt(getComputedStyle(n).zIndex,10);
    if(!isNaN(v))z=Math.max(z,v);n=n.parentElement;}return z;};
  const cand=[...document.querySelectorAll(
      '[role=dialog],[aria-modal=true],[data-automation-id*=odal],[data-automation-id*=opup]')]
    .filter(vis).filter(d=>d.getBoundingClientRect().height>60)
    .map(d=>({d,n:[...d.querySelectorAll(FOCUSABLE)].filter(vis).length,z:zOf(d)}))
    .filter(x=>x.n>0)
    .sort((a,b)=>(b.z-a.z)||(b.n-a.n));
  const dlg=cand.length?cand[0].d:null;
  const root=dlg||document;

  // ---- label for a field: <label for>, aria-label, aria-labelledby, placeholder, then the
  // nearest ancestor's text. A validation message without its field is unactionable; so is a field
  // without its label.
  const lab=(e)=>{
    let t='';
    if(e.id){const l=document.querySelector('label[for="'+CSS.escape(e.id)+'"]');if(l)t=T(l);}
    if(!t&&e.getAttribute('aria-labelledby')){
      t=e.getAttribute('aria-labelledby').split(/\s+/)
         .map(id=>{const n=document.getElementById(id);return n?T(n):'';}).join(' ');}
    if(!t)t=e.getAttribute('aria-label')||'';
    if(!t)t=e.placeholder||'';
    if(!t){const w=e.closest('label,[data-automation-id]');if(w)t=T(w).slice(0,80);}
    if(!t)t=e.name||e.type||'';
    return t.replace(/\s+/g,' ').trim().slice(0,70);};

  // ---- clear stale indices. ENUM_JS shipped without this and a re-rendered node kept an old
  // data-jhw-idx, so an index could resolve to the WRONG element.
  document.querySelectorAll('[data-jhw-esc]').forEach(e=>e.removeAttribute('data-jhw-esc'));

  // SITE CHROME IS NOT AN ACTION. Measured 2026-08-16: the modal was not detected on one run, the
  // index fell back to the whole page, and 'myworkday.com' (the Workday branding link in the
  // FOOTER) landed at position 0 -- so the panel, which was correctly reasoning about Google SSO,
  // named index 0 and the driver left the ATS entirely. Navigation and footer branding can never
  // advance an application, so they do not belong in a list of things to do.
  // WIZARD STEP NAMES belong to the PROGRESS STEPPER, never to the action list. Measured: the vision
  // reader put 'Create Account/Sign In', 'My Information' and 'Sign In' into the index as `input`
  // controls (Workday renders the stepper as radio inputs with labels) and the panel proposed
  // FILLING one. Structure catches them too (`stepper:`), but a name check protects every reader.
  const JUNK=/^(myworkday(\.com)?|workday(,? inc\.?)?|english|deutsch|search careers|introduce yourself|back to job posting|privacy|terms|cookies?|cookie settings|help|accessibility|sitemap|©.*|all rights reserved.*|view all jobs|share|print|create account\s*\/?\s*sign in|my information|my experience|application questions|voluntary disclosures|self identify)$/i;
  const junk=(t)=>JUNK.test((t||'').replace(/\s+/g,' ').trim());
  const out=[]; let i=0;
  const push=(el,kind,extra)=>{el.setAttribute('data-jhw-esc',String(i));
    out.push(Object.assign({i:i++,kind:kind},extra));};

  // IS THIS CONTROL PART OF THE PROGRESS STEPPER? Clicking a stepper entry navigates BACKWARDS and
  // loses the filled page -- but the LABELS overlap with real forward controls ("Sign In" is both a
  // stepper entry and the header button that OPENS the gate). Measured 2026-08-16: the panel proposed
  // the header 'Sign In', which was the only way forward, and my label-based guard refused it.
  // So mark the STRUCTURE and let the validator key on that instead of on the words.
  const STEP='[data-automation-id*=progressBar],[data-automation-id*=Stepper],[role=navigation] ol,'
           + '[class*=progress],[class*=stepper],nav[aria-label*=rogress]';
  const inStepper=(el)=>!!el.closest(STEP);
  for(const el of root.querySelectorAll('button,[role=button],a[href],input,select,textarea')){
    if(!vis(el)) continue;
    const s=getComputedStyle(el);const r=el.getBoundingClientRect();
    const tag=el.tagName.toLowerCase();
    const ty=(el.type||'').toLowerCase();
    if(ty==='hidden') continue;
    const trap=hp(el,s,r);
    if(tag==='button'||el.getAttribute('role')==='button'||ty==='submit'||ty==='button'){
      const t=T(el)||el.value||el.getAttribute('aria-label')||'';
      if(!t||junk(t)) continue;
      push(el,'button',{label:t.slice(0,70),disabled:!!el.disabled,honeypot:trap,
                        stepper:inStepper(el)});
    } else if(tag==='a'){
      const t=T(el); if(!t||junk(t)) continue;
      push(el,'link',{label:t.slice(0,70),honeypot:trap,stepper:inStepper(el)});
    } else if(tag==='select'){
      push(el,'select',{label:lab(el),
        value:(el.selectedOptions&&el.selectedOptions[0]?el.selectedOptions[0].text:'').trim(),
        required:!!(el.required||el.getAttribute('aria-required')==='true'),
        options:[...el.options].map(o=>o.text.trim()).filter(Boolean).slice(0,25),
        honeypot:trap});
    } else if(ty==='checkbox'||ty==='radio'){
      push(el,ty,{label:lab(el),checked:!!el.checked,
        required:!!(el.required||el.getAttribute('aria-required')==='true'),honeypot:trap});
    } else if(ty==='file'){
      push(el,'file',{label:lab(el),honeypot:trap});
    } else {
      push(el,tag==='textarea'?'textarea':'input',{label:lab(el),value:String(el.value||''),
        required:!!(el.required||el.getAttribute('aria-required')==='true'),
        secret:(ty==='password'),
        // a suggestion-backed field stores an ARRAY; typed text can never satisfy it
        arr:!!(el.getAttribute('aria-autocomplete')||el.getAttribute('aria-owns')||
               el.getAttribute('role')==='combobox'),
        honeypot:trap});
    }
    if(i>=60) break;
  }
  // ---- the page's own error messages, so the panel argues from evidence
  const errs=[...root.querySelectorAll(
      '[role=alert],[data-automation-id*=rror],[class*=rror],[aria-live=assertive]')]
    .filter(vis).map(T).filter(t=>t&&t.length<200);
  let dinfo={present:false};
  if(dlg){
    const h=dlg.querySelector('h1,h2,h3,[role=heading]');
    dinfo={present:true,title:h?T(h).slice(0,80):'',
           text:T(dlg).slice(0,600),
           scrollable:dlg.scrollHeight>dlg.clientHeight+8,
           scrollTop:dlg.scrollTop,scrollHeight:dlg.scrollHeight,clientHeight:dlg.clientHeight};
    dlg.setAttribute('data-jhw-dlg','1');
  }
  // AN EMPTY INDEX IS A BROKEN READ, NOT AN EMPTY PAGE. If scoping to the dialog yielded nothing,
  // the page is strictly better than nothing -- and the caller is told so it can say which happened.
  // NEVER AGAIN A SILENT ZERO. If the index is empty the log must say what was looked at and why
  // every candidate was rejected -- four runs printed "0 control(s)" with no reason at all.
  return {controls:out,dialog:dinfo,scoped:!!dlg,seen:seen,dropped:dropped,
          dialogsSeen:document.querySelectorAll('[role=dialog],[aria-modal=true]').length,
          errors:[...new Set(errs)].slice(0,6),
          url:location.href,
          step:((document.querySelector('[data-automation-id=progressBarActiveStep]')||{}).innerText||'')
                .replace(/\s+/g,' ').trim().slice(0,64)};
}"""


async def _controls(page) -> dict:
    """The indexed, dialog-scoped view of the page. Never raises: a mid-navigation evaluate() is
       normal, not exceptional."""
    try:
        d = await page.evaluate(_CTRL_JS)
        return d if isinstance(d, dict) else {"controls": [], "dialog": {"present": False}}
    except Exception as e:
        _log(f"    control index unavailable ({type(e).__name__}) — page mid-navigation?")
        return {"controls": [], "dialog": {"present": False}, "errors": [], "url": "", "step": ""}


async def _fresh(page, sel: str, label: str = ""):
    """A live handle for a target, RE-RESOLVED if the tag has been re-rendered away.

    MEASURED 2026-08-16: `action click raised TimeoutError: waiting for locator
    ("[data-jhw-esc=...")`. The attribute is written at INDEX time and React re-renders it out of
    existence before the click lands, so addressing by attribute is a race the driver loses. On a
    miss, find the control again BY ITS LABEL -- which is what a human would do, and what Stagehand
    calls self-healing."""
    try:
        loc = page.locator(sel).first
        if await loc.count() and await loc.is_visible():
            return loc
    except Exception:
        pass
    if not label:
        return None
    _log(f"    the index tag is stale — re-finding {label[:40]!r} by label")
    rx = re.compile(r"^\s*" + re.escape(label.strip()) + r"\s*$", re.I)
    for attempt in (lambda: page.get_by_role("button", name=rx),
                    lambda: page.get_by_role("link", name=rx),
                    lambda: page.get_by_label(rx),
                    lambda: page.get_by_placeholder(rx),
                    lambda: page.get_by_text(rx)):
        try:
            loc = attempt()
            if await loc.count() == 1 and await loc.first.is_visible():
                _log("    re-found it")
                return loc.first
        except Exception:
            continue
    _log("    could not re-find it by label either")
    return None


async def _exec_action(page, act: dict) -> bool:
    """Execute an action the panel proposed AND escalate.validate() approved.

    Targets are addressed through `data-jhw-esc`, the attribute the index just wrote, so nothing
    here can act on an element the panel was not shown. Values are committed with the NATIVE SETTER
    and read back -- React keeps a value tracker on the node and a plain assignment leaves it stale,
    which is how a select could LOOK set while the form model stayed empty."""
    verb = str(act.get("verb") or "").lower()
    tgt = act.get("target")
    sel = f'[data-jhw-esc="{tgt}"]'
    try:
        if verb == "scroll_dialog":
            moved = await page.evaluate("""() => {const d=document.querySelector('[data-jhw-dlg]');
                if(!d) return false; const b=d.scrollTop; d.scrollTop=b+Math.max(240,d.clientHeight-80);
                return d.scrollTop>b;}""")
            await page.wait_for_timeout(500)
            _log(f"    scrolled the dialog ({'more revealed' if moved else 'already at the bottom'})")
            return bool(moved)
        if verb == "dismiss_dialog":
            for rx in (r"^\s*(close|schlie(ß|ss)en|got it|ok|continue|weiter|accept)\s*$", r"^\s*x\s*$"):
                if await _click_role(page, "button", rx, 1200):
                    await page.wait_for_timeout(700); _log("    dismissed the dialog"); return True
            try:
                await page.keyboard.press("Escape"); await page.wait_for_timeout(600)
                _log("    dismissed the dialog with Escape"); return True
            except Exception:
                return False
        if verb == "google_sso":
            if not await _click_role(page, "button", r"sign in with google", 2500):
                return False
            await page.wait_for_timeout(3500)
            return True
        if verb == "click":
            el = await _fresh(page, sel, str(act.get("expect") or act.get("_label") or ""))
            if el is None:
                return False
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.click(timeout=ACTION_TIMEOUT)
            await page.wait_for_timeout(900)
            return True
        if verb == "check":
            res = await page.evaluate("""(s) => {const e=document.querySelector(s);
                if(!e) return {ok:false,why:'gone'};
                if(e.checked) return {ok:true,why:'already set'};   // SET, never TOGGLE
                e.click(); return {ok:!!e.checked,why:'clicked'};}""", sel)
            await page.wait_for_timeout(600)
            _log(f"    check -> {res}")
            return bool(res.get("ok"))
        if verb in ("fill", "select"):
            val = str(act.get("value") or "")
            res = await page.evaluate("""([s,v]) => {
                let e=document.querySelector(s); if(!e) return {ok:false,why:'gone'};
                // MEASURED 2026-08-16: `TypeError: Illegal invocation`, THREE rounds running, on a
                // unanimous and CORRECT panel decision (fill the e-mail field). A native value setter
                // must be called with a matching `this`, and the vision reader had tagged the LABEL /
                // wrapper rather than the input -- so HTMLInputElement's setter was invoked on a
                // <div>. Walk to the real field first, and take the prototype FROM THE ELEMENT.
                const FIELD='input:not([type=hidden]),textarea,select';
                if(!e.matches(FIELD)){
                  const inner=e.querySelector(FIELD)
                    ||(e.closest('label,[data-automation-id]')||e).querySelector(FIELD);
                  if(inner) e=inner; else return {ok:false,why:'not a field, none inside it'};
                }
                const proto=e.tagName==='SELECT'?HTMLSelectElement.prototype
                          :(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype);
                const d=Object.getOwnPropertyDescriptor(proto,'value');
                if(!d||!d.set) return {ok:false,why:'no value setter on '+e.tagName};
                const set=d.set;
                if(e.tagName==='SELECT'){
                  const o=[...e.options].find(o=>o.text.trim().toLowerCase()===v.trim().toLowerCase())
                        ||[...e.options].find(o=>o.text.trim().toLowerCase().includes(v.trim().toLowerCase()));
                  if(!o) return {ok:false,why:'no such option'};
                  set.call(e,o.value);
                } else { set.call(e,v); }
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
                e.blur&&e.blur();
                const now=e.tagName==='SELECT'
                  ?(e.selectedOptions[0]?e.selectedOptions[0].text.trim():''):String(e.value||'');
                return {ok:!!now,why:'set',reads:now.slice(0,40)};}""", [sel, val])
            await page.wait_for_timeout(700)
            # A WRITE IS NOT DONE UNTIL IT HAS BEEN READ BACK -- third time this repo has paid for it.
            _log(f"    {verb} -> the field now reads {res.get('reads','')!r}"
                 if res.get("ok") else f"    {verb} FAILED: {res.get('why')}")
            return bool(res.get("ok"))
    except Exception as e:
        _log(f"    action {verb} raised {type(e).__name__}: {str(e)[:90]}")
        return False
    return False


def _fingerprint(idx: dict) -> str:
    """Cheap page identity, so 'did the action change anything' is answerable."""
    c = idx.get("controls") or []
    return f"{(idx.get('url') or '')[:90]}|{idx.get('step') or ''}|{len(c)}|" \
           f"{(c[0].get('label') if c else '')}|{(idx.get('dialog') or {}).get('title') or ''}"


def _facts_for_panel(data: dict) -> str:
    """The NON-SECRET facts the panel may use so it never asks for something we already own.

    CLAUDE.md, hard rule: "the engine must never ASK for a fact it already owns" -- candidate.md's
    screening defaults were on disk while the agent asked the human for them on Telegram.
    A CREDENTIAL IS NEVER INCLUDED. atscreds owns those and escalate.validate() refuses to type
    into a password field regardless of what the panel proposes."""
    b = (data or {}).get("basics", {}) or {}
    sd = (data or {}).get("screening_defaults", {}) or {}
    out = []
    for label, val in (("name", b.get("legal_name") or b.get("name")),
                       ("e-mail", b.get("email")),
                       ("phone", b.get("phone")),
                       ("address", b.get("address")),
                       ("country", b.get("country")),
                       ("linkedin", b.get("linkedin"))):
        if val:
            out.append(f"- {label}: {val}")
    for k, v in list(sd.items())[:12]:
        out.append(f"- {str(k).replace('_', ' ')}: {v}")
    out.append("- NEVER ask for or type a password: the vault fills credentials deterministically")
    return "\n".join(out)


async def _controls_pw(page) -> dict:
    """The control index built with PLAYWRIGHT'S OWN visibility, not my hand-rolled predicate.

    WHY A SECOND IMPLEMENTATION EXISTS. One line of my custom `vis()` -- an aria-hidden ancestor
    check -- rejected every control in the Workday modal and produced `0 control(s) indexed` for four
    consecutive runs, while `_SNAP_JS` printed all seven buttons from the same page. A single
    hand-written visibility rule is a single point of failure, and this project's own doctrine is
    that a driver must never be shown a page it cannot see.
    `locator.is_visible()` is Playwright's definition -- non-empty bounding box and not
    `visibility:hidden` -- the same rule Selenium and the WAI-ARIA spec use, and it is maintained by
    people who test it against real sites. It knows nothing about aria-hidden, which is correct:
    aria-hidden is an accessibility hint, not a visibility fact.
    Elements are tagged with `data-jhw-esc` exactly as the JS path does, so the executor is unchanged.
    """
    out = []
    try:
        await page.evaluate("() => document.querySelectorAll('[data-jhw-esc]')"
                            ".forEach(e => e.removeAttribute('data-jhw-esc'))")
        dlg = None
        for sel in ('[role=dialog][aria-modal=true]', '[role=dialog]', '[aria-modal=true]'):
            loc = page.locator(sel)
            for i in range(min(await loc.count(), 6)):
                d = loc.nth(i)
                if await d.is_visible() and await d.locator(
                        "button, [role=button], a[href], input, select, textarea").count() > 0:
                    dlg = d
                    break
            if dlg:
                break
        root = dlg or page
        cand = root.locator("button, [role=button], a[href], input:not([type=hidden]), "
                            "select, textarea")
        n = min(await cand.count(), 60)
        i = 0
        for k in range(n):
            el = cand.nth(k)
            try:
                if not await el.is_visible():
                    continue
                info = await el.evaluate(r"""(e) => {
                    const r = e.getBoundingClientRect();
                    const s = getComputedStyle(e);
                    const cp = (s.clipPath||'').replace(/\s+/g,' ');
                    const cl = (s.clip||'').replace(/\s+/g,' ');
                    const trap = (r.width<2||r.height<2) || s.opacity==='0'
                      || (/^polygon\(/.test(cp) && !/[1-9]/.test(cp))
                      || (/^rect\(/.test(cl) && !/[2-9]|\d\d/.test(cl))
                      || /beecatcher|robots only/i.test(
                           (e.getAttribute('data-automation-id')||'')+' '+(e.name||'')+' '+
                           (e.getAttribute('aria-label')||''));
                    const tag = e.tagName.toLowerCase();
                    const ty = (e.type||'').toLowerCase();
                    let kind = tag === 'a' ? 'link'
                             : (tag === 'select' ? 'select'
                             : (ty === 'checkbox' || ty === 'radio' ? ty
                             : (ty === 'file' ? 'file'
                             : (tag === 'textarea' ? 'textarea'
                             : (tag === 'button' || e.getAttribute('role') === 'button'
                                || ty === 'submit' || ty === 'button' ? 'button' : 'input')))));
                    let lab = '';
                    if (e.id) { const l = document.querySelector('label[for="'+CSS.escape(e.id)+'"]');
                                if (l) lab = l.innerText; }
                    if (!lab) lab = e.getAttribute('aria-label') || '';
                    if (!lab) lab = e.placeholder || '';
                    if (!lab && (kind==='button'||kind==='link'))
                        lab = (e.innerText||e.textContent||e.value||'');
                    if (!lab) { const w = e.closest('label,[data-automation-id]');
                                if (w) lab = (w.innerText||'').slice(0,80); }
                    if (!lab) lab = e.name || e.type || '';
                    const STEP='[data-automation-id*=progressBar],[data-automation-id*=Stepper],'
                             +'[role=navigation] ol,[class*=progress],[class*=stepper]';
                    return {kind, stepper: !!e.closest(STEP),
                            label: lab.replace(/\s+/g,' ').trim().slice(0,70),
                            value: kind==='select'
                              ? (e.selectedOptions && e.selectedOptions[0] ? e.selectedOptions[0].text.trim() : '')
                              : String(e.value||''),
                            required: !!(e.required || e.getAttribute('aria-required')==='true'),
                            checked: (ty==='checkbox'||ty==='radio') ? !!e.checked : null,
                            secret: ty === 'password',
                            options: kind==='select'
                              ? [...e.options].map(o=>o.text.trim()).filter(Boolean).slice(0,25) : [],
                            honeypot: trap};}""")
                if not info.get("label"):
                    continue
                if _is_junk(info["label"]):
                    continue
                await el.evaluate("(e, i) => e.setAttribute('data-jhw-esc', String(i))", i)
                info["i"] = i
                out.append(info)
                i += 1
            except Exception:
                continue
        dinfo = {"present": False}
        if dlg:
            try:
                dinfo = {"present": True,
                         "title": (await dlg.locator("h1,h2,h3,[role=heading]").first.inner_text()
                                   )[:80] if await dlg.locator("h1,h2,h3,[role=heading]").count()
                                   else "",
                         "text": (await dlg.inner_text())[:600],
                         "scrollable": await dlg.evaluate(
                             "d => d.scrollHeight > d.clientHeight + 8")}
                await dlg.evaluate("d => d.setAttribute('data-jhw-dlg','1')")
            except Exception:
                dinfo = {"present": True, "title": "", "text": "", "scrollable": False}
        return {"controls": out, "dialog": dinfo, "scoped": bool(dlg), "via": "playwright",
                "errors": [], "url": page.url or "", "step": ""}
    except Exception as e:
        _log(f"    playwright index failed ({type(e).__name__}: {str(e)[:60]})")
        return {"controls": [], "dialog": {"present": False}, "via": "playwright",
                "errors": [], "url": "", "step": ""}


_JUNK_PY = re.compile(
    r"^(myworkday(\.com)?|workday(,? inc\.?)?|english|deutsch|search careers|introduce yourself|"
    r"back to job posting|privacy|terms|cookies?|cookie settings|help|accessibility|sitemap|"
    r"©.*|all rights reserved.*|view all jobs|share|print)$", re.I)


_STEP_PY = re.compile(
    r"^\s*(create account\s*/?\s*sign in|my information|my experience|application questions|"
    r"voluntary disclosures|self identify)\s*$", re.I)


def _is_junk(label: str) -> bool:
    """Site chrome, in Python, mirroring the JS filter. Two homes for one rule is a defect, so this
       is asserted against the JS list by the DOM self-test rather than trusted."""
    t = re.sub(r"\s+", " ", label or "").strip()
    return bool(_JUNK_PY.match(t) or _STEP_PY.match(t))


async def _controls_sh(page) -> dict:
    """The index from STAGEHAND'S observe() — the third, independent source.

    The operator asked why this is Playwright and not Stagehand, and he is right about the PATTERN:
    observe() returns exactly "what can I do here, and the selector for it" — a MAINTAINED
    implementation of the thing I hand-rolled and then broke with one aria-hidden line.
    `flows/stagehand.py`'s own docstring already prescribed this: "call observe() to get a concrete
    selector, USE it, and persist it".
    HONEST SCOPE: a FALLBACK, not the primary. It costs an LLM round trip, and
    FOUR_MODEL_CONSENSUS.md §7.6 rule 2 forbids a model in a request path — the fill loop is one. So
    the two deterministic readers run first and this fires only when the page cannot be read at all,
    which is precisely where a maintained implementation earns an inference call."""
    try:
        import stagehand as SH
    except Exception:
        return {"controls": [], "dialog": {"present": False}, "via": "stagehand-missing"}
    try:
        acts = await SH.observe("every button, link and input field a user could interact with right "
                                "now, including inside any open dialog")
    except Exception as e:
        _log(f"    stagehand observe unavailable ({type(e).__name__}: {str(e)[:60]})")
        return {"controls": [], "dialog": {"present": False}, "via": "stagehand-error"}
    out, i = [], 0
    for a in (acts or [])[:40]:
        sel = str((a or {}).get("selector") or "").strip()
        desc = str((a or {}).get("description") or (a or {}).get("action") or "").strip()
        if not sel:
            continue
        try:
            el = page.locator(sel).first
            if not await el.is_visible():
                continue
            info = await el.evaluate(r"""(e) => {
                const tag = e.tagName.toLowerCase(), ty = (e.type||'').toLowerCase();
                return {tag: tag, ty: ty,
                        label: (e.getAttribute('aria-label')||e.innerText||e.placeholder||
                                e.value||e.name||'').replace(/\s+/g,' ').trim().slice(0,70),
                        value: String(e.value||''),
                        required: !!(e.required||e.getAttribute('aria-required')==='true'),
                        checked: (ty==='checkbox'||ty==='radio') ? !!e.checked : null,
                        secret: ty === 'password',
                        options: tag==='select'
                          ? [...e.options].map(o=>o.text.trim()).filter(Boolean).slice(0,25) : []};}""")
            label = (info.get("label") or desc)[:70]
            if not label or _is_junk(label):
                continue
            tag, ty = info.get("tag"), info.get("ty")
            kind = ("link" if tag == "a" else "select" if tag == "select"
                    else ty if ty in ("checkbox", "radio", "file")
                    else "textarea" if tag == "textarea"
                    else "button" if tag == "button" or ty in ("submit", "button") else "input")
            await el.evaluate("(e, i) => e.setAttribute('data-jhw-esc', String(i))", i)
            out.append({"i": i, "kind": kind, "label": label,
                        "value": info.get("value") or "", "required": info.get("required"),
                        "checked": info.get("checked"), "secret": info.get("secret"),
                        "options": info.get("options") or [], "honeypot": False})
            i += 1
        except Exception:
            continue
    return {"controls": out, "dialog": {"present": False}, "via": "stagehand",
            "errors": [], "url": page.url or "", "step": ""}


async def _readable_index(page, tries: int = 3) -> dict:
    """A control index that is the UNION of every sense, never the first one that answered.

    ROOT CAUSE, MEASURED 2026-08-16 23:31 — THIS FUNCTION IS WHY THE PANEL WAS SHOWN A SIGN-IN PAGE
    WITH NO FIELDS ON IT. It was `first-wins`: `if _n(vz) >= 2: return vz`. Vision looked at the gate
    and reported five controls, correctly and usefully:
        ['evgeny@s4biz.io', '************', 'Sign In', 'Create Account', 'Forgot your password?']
    Label resolution then discarded the first three (an input cannot be found by its VALUE, a
    password mask exists in no attribute at all, and 'Sign In' was the modal TITLE), leaving exactly
    2 — which cleared the threshold, so this returned and the union of ax+grid+deep+tab WAS NEVER
    CONSULTED. Ninety seconds later, on the next page, that same union found 8 controls. The two
    fields that mattered had been deleted from the index before four vendors ever saw it, and they
    were then blamed for guessing.
    THE RULE THIS BROKE IS ALREADY WRITTEN IN THIS REPOSITORY, one layer down, in `seers.merge`:
    *first-wins is what let one wrong predicate blind everything for a whole evening; a union cannot
    be blinded by one bad rule.* I applied it INSIDE the deterministic readers and left the top level
    a race. So:
      * EVERY reader runs and everything is UNIONED. Vision's job is to ADD what the DOM hides; it
        may never REMOVE what the DOM can see. The deterministic readers cost ~10ms and are free.
      * The floor is no longer a bare count. `_suspect()` asks the DOM a DIFFERENT question — how
        many visible inputs exist — and an index holding fewer fields than the page has is a BROKEN
        READ, not a short page. A sign-in form indexed with zero inputs must never be actionable.
      * SET-OF-MARK then LABELS the union: numbered boxes are painted over the controls we already
        hold handles to and the model answers with a NUMBER. Resolution is by construction, so the
        filled e-mail box and the masked password box that label mode cannot name are addressable
        anyway. See flows/som.py.
    COST: still the stall path only (`_escalate` calls this, once per round), so ~1-2s per stall and
    never per action. FOUR_MODEL_CONSENSUS.md §7.6 rule 2 holds. `JHW_VISION_FIRST=0` drops vision.
    """
    def _n(d):
        return len(d.get("controls") or [])

    def _why(d):
        """A zero must never be silent again. Four runs printed '0 control(s)' with no reason at all,
        and the reason was one line of my own visibility predicate."""
        if d.get("seen") is None:
            return ""
        drops = ", ".join(f"{k}={v}" for k, v in (d.get("dropped") or {}).items() if v)
        return (f" [looked at {d.get('seen')} element(s); rejected {drops or 'none'}; "
                f"{d.get('dialogsSeen', 0)} dialog container(s) present]")

    async def _reads():
        """Every sense, unioned. An exception in one reader can never cost us the others."""
        got = []
        if VISION_FIRST:
            try:
                import vision as VIS
                v = await VIS.read(page, log=_log, junk=_is_junk)
                if _n(v):
                    _log(f"    [eye] vision contributes {_n(v)} control(s) ({v.get('via')})")
                got.append(v)
            except Exception as e:
                _log(f"    vision reader unavailable ({type(e).__name__}) — the DOM senses stand")
        try:
            import seers as SEE
            got.append(await SEE.see(page, log=_log, junk=_is_junk))
        except Exception as e:
            _log(f"    seers unavailable ({type(e).__name__}: {str(e)[:60]})")
        try:
            got.append(await _controls(page))
        except Exception as e:
            _log(f"    custom index raised {type(e).__name__}")
        try:
            got.append(await _controls_pw(page))
        except Exception:
            pass
        return [g for g in got if g]

    async def _union():
        parts = await _reads()
        if not parts:
            return {"controls": [], "via": "none", "dialog": {"present": False}}
        try:
            import seers as SEE
            u = SEE.merge(parts, log=_log)
            # A dialog seen by ANY reader owns the page, and the custom index is the only reader that
            # scopes to it, so carry its scoping decision across the union.
            for pt in parts:
                if (pt.get("dialog") or {}).get("present"):
                    u["dialog"] = pt["dialog"]
                    break
            for k in ("seen", "dropped", "dialogsSeen"):
                for pt in parts:
                    if pt.get(k) is not None:
                        u.setdefault(k, pt[k])
            u["errors"] = next((pt.get("errors") for pt in parts if pt.get("errors")), [])
            u["url"] = page.url or ""
            return await SEE.stamp(page, u, log=_log)
        except Exception as e:
            _log(f"    union failed ({type(e).__name__}) — using the single best reader")
            return max(parts, key=_n)

    async def _suspect(idx):
        """Is this index LYING about the page? Ask a DIFFERENT question than any reader asked.

        Counting visible `input`s is not a visibility predicate and not an actionability judgement —
        it is arithmetic the browser does for us. An index with FEWER fields than the page plainly
        has is a broken read, and that is precisely the state that was handed to the panel: a
        sign-in form indexed with zero inputs. A short page is fine; a page whose fields have gone
        missing from the index is not."""
        try:
            real = await page.evaluate("""() => {
                let n = 0;
                document.querySelectorAll('input,textarea,select').forEach(e => {
                  const t = (e.getAttribute('type') || '').toLowerCase();
                  if (t === 'hidden' || t === 'submit' || t === 'button') return;
                  const r = e.getBoundingClientRect();
                  const st = getComputedStyle(e);
                  if (r.width > 2 && r.height > 2 && st.visibility !== 'hidden'
                      && st.display !== 'none' && parseFloat(st.opacity || '1') > 0.05) n++;
                });
                return n; }""")
        except Exception:
            return ""
        mine = sum(1 for c in (idx.get("controls") or [])
                   if c.get("kind") in ("input", "textarea", "select")
                   or (c.get("tag") or "") in ("input", "textarea", "select"))
        if real >= 2 and mine < real:
            return (f"the page has {real} visible field(s) and the index has {mine} — "
                    f"that is a BROKEN READ, not a short page")
        return ""

    idx = await _union()
    for k in range(tries):
        bad = await _suspect(idx)
        if _n(idx) >= 2 and not bad:
            break
        _log(f"    {_n(idx)} control(s) so far{_why(idx)}" + (f"; {bad}" if bad else ""))
        if k == tries - 1:
            break
        # A page that is mid-render is not a page. Wait, then read EVERY sense again.
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        if k == 1:
            # STAGEHAND is the expensive maintained reader; bring it in only once the cheap senses
            # have failed twice, and UNION it rather than letting it win outright.
            try:
                sh = await _controls_sh(page)
                if _n(sh):
                    import seers as SEE
                    idx = await SEE.stamp(page, SEE.merge([idx, sh], log=_log), log=_log)
                    _log(f"    stagehand observe() added to the union -> {_n(idx)}")
            except Exception:
                pass
            if _n(idx) < 2:
                _log("    still nothing readable — RELOADING the gate rather than asking the panel "
                     "to guess")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    _log(f"    reload failed ({type(e).__name__})")
        idx = await _union()

    _log(f"    INDEX: {_n(idx)} control(s) [{idx.get('via')}]"
         + (f", {idx.get('stamped')} addressable by attribute" if idx.get("stamped") else ""))

    # SET-OF-MARK. The list is the readers'; the JUDGEMENT is the eye's. Numbered boxes are painted
    # over controls we already hold handles to and the model answers with a NUMBER, so a filled
    # input and a masked password box are addressable even though neither can be named. It never
    # adds or removes a control, and on any failure the index comes back untouched.
    if _n(idx) >= 2:
        try:
            import som as SOM
            idx = await SOM.refine(
                page, idx,
                task=("This is an ATS application or sign-in page. Which marks are the e-mail box, "
                      "the password box and the button that submits this form? Is anything covering "
                      "the form?"),
                log=_log)
        except Exception as e:
            _log(f"    [som] unavailable ({type(e).__name__}) — index unchanged")

    if _n(idx) < 2:
        # EVIDENCE, NOT A SIXTH THEORY. Four runs printed "0 control(s)" and I answered each with a
        # different guess verified only against a replica I had written myself. The run now captures
        # its own ground truth -- screenshot + full DOM + what every reader returned.
        try:
            import vision as VIS
            await VIS.dump(page, "blind-index", {
                "union": _n(idx), "seen": idx.get("seen"), "dropped": idx.get("dropped"),
                "dialogsSeen": idx.get("dialogsSeen"), "via": idx.get("via"),
                "labels": [c.get("label") for c in (idx.get("controls") or [])]}, log=_log)
        except Exception:
            pass
    return idx



async def _escalate(page, reason: str, filled: list, facts: str = "", tried=None) -> dict:
    """RUNG 2 AND 3 OF THE CANDIDATE'S LADDER.

    The deterministic driver is stuck. Show three vendors the page -- INCLUDING any modal that owns
    it -- let them agree on ONE next action, execute it deterministically, and only wake the human
    when they cannot agree. Every human answer is remembered by _ask()/learned.py so the same
    question is never asked twice.

    Returns {"moved": bool, "asked": str|None, "act": {...}}. Never raises."""
    try:
        import escalate as ESC
    except Exception as e:
        _log(f"escalation unavailable ({type(e).__name__}) — asking you directly")
        return {"moved": False, "asked": await _ask(reason), "act": {}}

    _tried = list(tried or [])
    for rnd in range(1, ESC.MAX_ROUNDS + 1):
        # WE MAY NOT EVEN BE ON THE ATS. An unanchored click sent the browser to
        # accounts.google.com and the panel was then asked to unblock a Workday gate while looking
        # at Google's account chooser -- garbage in, guesses out. Check FIRST, every round.
        if _on_idp(page.url or ""):
            _log(f"    we are on an IDENTITY PROVIDER, not the ATS ({(page.url or '')[:60]}) — "
                 f"going back before consulting anyone")
            await _leave_idp(page, {"note": ""})
        idx = await _readable_index(page)
        ctrls, dlg = idx.get("controls") or [], idx.get("dialog") or {}
        _log(f"ESCALATING to the panel (round {rnd}/{ESC.MAX_ROUNDS}): {reason}")
        if dlg.get("present"):
            _log(f"    a MODAL owns the page: {(dlg.get('title') or '?')[:60]!r}"
                 f"{' (scrollable)' if dlg.get('scrollable') else ''}")
        # NAME WHAT IS PRESENT, NOT HOW MANY. "4 control(s) indexed" is unactionable -- exactly
        # the defect already recorded for validation messages without their field. When the panel
        # picked index 0 and the driver clicked the Workday FOOTER link, the log could not say
        # whether the panel was wrong or the index was; the list settles it in one line.
        for _c in ctrls[:14]:
            _log(f"      [{_c.get('i')}] {_c.get('kind')} {str(_c.get('label'))[:58]!r}"
                 + (" REQUIRED" if _c.get("required") else "")
                 + (" TRAP" if _c.get("honeypot") else ""))
        _log(f"    {len(ctrls)} control(s) indexed"
             f"{' INSIDE the modal' if dlg.get('present') else ''}"
             + (f", errors: {idx.get('errors')}" if idx.get("errors") else ""))
        before = _fingerprint(idx)

        # WITHDRAW A VERB THAT IS KNOWN DEAD. Google SSO landed on the account chooser every run;
        # once _leave_idp has fired we know the profile has no session, so offering it again just
        # burns 80s per round. The panel is TOLD, rather than being left to propose it and be refused.
        _t = list(_tried)
        if _GOOGLE_DEAD[0]:
            _t.append("Google SSO: NO live session in this browser profile — it lands on "
                      "accounts.google.com every time. Do NOT propose google_sso or clicking any "
                      "Google button; the goal is the ATS-LOCAL email+password account.")
        act = await ESC.next_action(reason, ctrls, dlg, url=idx.get("url") or "",
                                    step=idx.get("step") or "", errors=idx.get("errors") or [],
                                    facts=facts, tried=_t)
        if _GOOGLE_DEAD[0] and str(act.get("verb")) == "google_sso":
            _log("    refusing the panel's google_sso: already proven dead this run")
            act = {"verb": "click", "target": next(
                (c.get("i") for c in ctrls
                 if re.search(r"sign in with e-?mail", str(c.get("label") or ""), re.I)), None),
                "expect": "Sign in with email", "why": "google is dead; taking the email path"}
            if act.get("target") is None:
                act = {"verb": "ask_human", "question": reason,
                       "why": "google is dead and no email path is on screen"}
        verb = str(act.get("verb") or "")

        if verb == "ask_human":
            q = str(act.get("question") or reason)
            _log(f"    the panel could not decide ({act.get('why')}) — ASKING YOU: {q[:90]}")
            answer = await _ask(q)
            return {"moved": False, "asked": answer, "act": act}
        if verb == "give_up":
            _log(f"    the panel says stop: {act.get('reason')}")
            return {"moved": False, "asked": None, "act": act}

        # give the executor the label from the INDEX, so a stale tag can be re-found by name
        act["_label"] = next((str(c.get("label")) for c in ctrls
                              if str(c.get("i")) == str(act.get("target"))), "")
        ok = await _exec_action(page, act)
        after = _fingerprint(await _controls(page))
        label = next((str(c.get("label")) for c in ctrls
                      if str(c.get("i")) == str(act.get("target"))), "")
        note = f"{verb}[{act.get('target')}] {label[:40]!r}" + (
            f" = {str(act.get('value'))[:30]!r}" if act.get("value") else "")
        _tried.append(note + (" -> page unchanged" if after == before else " -> page moved"))
        if ok and after != before:
            _log(f"    PANEL UNBLOCKED IT: {note}  ({act.get('why')})")
            filled.append(f"panel:{verb}")
            return {"moved": True, "asked": None, "act": act}
        _log(f"    {note} did not move the page — the panel gets one more look with that fact")
    _log("    the panel could not unblock this page — ASKING YOU")
    return {"moved": False, "asked": await _ask(reason), "act": {}}


# ---------- page state -----------------------------------------------------------------------------
_ERR_JS = r"""
() => {
  // WHY THIS IS NOT A data-automation-id CHAIN. On the 23:31 run `_error_text` consulted four
  // `data-automation-id` selectors, found nothing, and the log therefore said "create bounced
  // (redirected to /login)" -- so the run BLAMED THE PASSWORD with no evidence at all, and the next
  // four minutes went on a credential that may have been perfectly fine. Workday renders validation
  // in several shapes across tenants (a banner, a role=alert, an aria-live region, a message beside
  // the field). Ask for all of them, and for anything that merely LOOKS like an error: a message we
  // cannot read is a message we will misattribute.
  const bits = [], seen = new Set();
  const vis = el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none'
           && parseFloat(st.opacity || '1') > 0.05;
  };
  const take = el => {
    if (!el || !vis(el)) return;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 320 || seen.has(t)) return;
    seen.add(t); bits.push(t);
  };
  [ "[data-automation-id='errorBanner']", "[data-automation-id='errorMessage']",
    "[data-automation-id='alertMessage']", "[data-automation-id='inputAlert']",
    "[data-automation-id*='rror']", "[role=alert]", "[aria-live=assertive]",
    "[aria-live=polite]", "[class*='Error']", "[class*='error']", "[class*='alert']",
    "[id*='error']"
  ].forEach(sel => { try { document.querySelectorAll(sel).forEach(take); } catch (e) {} });
  // aria-invalid names the FIELD that is wrong; the message is usually the node beside it.
  const fields = [];
  document.querySelectorAll("[aria-invalid='true']").forEach(el => {
    if (!vis(el)) return;
    let lab = el.getAttribute('aria-label') || '';
    if (!lab && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) lab = (l.innerText || '').trim();
    }
    fields.push(lab || el.getAttribute('name') || el.getAttribute('type') || 'a field');
  });
  return {messages: bits.slice(0, 6), invalid: fields.slice(0, 6)};
}
"""


async def _error_text(page) -> str:
    """Everything the page is complaining about, in its own words. Empty means it said nothing."""
    out = []
    try:
        d = await page.evaluate(_ERR_JS)
        out = list(d.get("messages") or [])
        inv = d.get("invalid") or []
        if inv:
            out.append("(rejected field(s): " + ", ".join(inv) + ")")
    except Exception:
        pass
    if not out:                                   # last resort: the original selector chain
        el = await _first(page, "error_banner", 500)
        if el:
            try:
                out = [re.sub(r"\s+", " ", (await el.inner_text()))]
            except Exception:
                out = ["error"]
    return " | ".join(x for x in out if x)[:320]


async def _google_signin(page) -> bool:
    """Sign in to Google the way the RECORDING does, and stop dead at anything it did not contain.

    Recorded shape: `Email or phone` -> `Next` -> `Enter your password` -> `Next`. That is the whole
    interaction; nothing else is attempted. THE HARD RULES, because this is the credential that gates
    the mailbox everything else depends on:
      * only on accounts.google.com -- never type it into a page that merely looks like Google;
      * ONCE per run. A second attempt on a wrong password is how an account gets locked;
      * a CHALLENGE (2-step, "verify it's you", "this browser or app may not be secure") is a STOP,
        not a retry. We report it and let the human sign in once in noVNC, which the persistent
        profile then remembers;
      * the password is never logged, never put in argv, and never sent to a model.
    """
    if _GOOGLE_TRIED[0]:
        _log("    Google sign-in already attempted once this run — not repeating it")
        return False
    _GOOGLE_TRIED[0] = True
    email = os.getenv("GOOGLE_EMAIL", "")
    pw = os.getenv("GOOGLE_PASSWORD", "")
    if not (email and pw):
        _log("    no GOOGLE_EMAIL/GOOGLE_PASSWORD in the environment — cannot use SSO")
        return False
    if "accounts.google.com" not in (page.url or "").lower():
        _log("    not on accounts.google.com — refusing to type the Google credential")
        return False
    async def _chal() -> str:
        try:
            t = ((await page.inner_text("body")) or "").lower()
        except Exception:
            return ""
        for rx, why in ((r"browser or app may not be secure", "Google blocked the automated browser"),
                        (r"2-step|two-step|verify it'?s you|enter the code|passkey",
                         "a 2-step / passkey challenge"),
                        (r"couldn'?t sign you in|try again later", "Google refused the sign-in")):
            if re.search(rx, t):
                return why
        return ""
    async def _shown(loc, t=3000) -> bool:
        try:
            await loc.wait_for(state="visible", timeout=t)
            return True
        except Exception:
            return False
    try:
        el = page.get_by_role("textbox", name=re.compile(r"email or phone", re.I)).first
        if await el.count() and await _shown(el, 3000):
            await el.fill(email, timeout=ACTION_TIMEOUT)
            await _click_role(page, "button", r"^\s*next\s*$", 3000)
            await page.wait_for_timeout(2500)
        why = await _chal()
        if why:
            _log(f"    STOPPING the Google path: {why}. Sign in ONCE in noVNC "
                 f"(http://localhost:9090) and the profile will remember it.")
            return False
        pel = page.get_by_role("textbox", name=re.compile(r"enter your password|^password$", re.I)).first
        if not (await pel.count() and await _shown(pel, 4000)):
            _log("    the Google password box never appeared — not proceeding")
            return False
        await pel.fill(pw, timeout=ACTION_TIMEOUT)          # never logged, never in argv
        await _click_role(page, "button", r"^\s*next\s*$", 3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        why = await _chal()
        if why:
            _log(f"    Google asked for more than a password: {why} — handing back to you")
            return False
        left = "accounts.google.com" not in (page.url or "").lower()
        _log(f"    Google sign-in {'completed, back on the ATS' if left else 'did not leave Google'}")
        return left
    except Exception as e:
        _log(f"    Google sign-in raised {type(e).__name__} — abandoning that path")
        return False


async def _through_gate(page) -> bool:
    """Past the gate — and if we are AUTHENTICATED but on the wrong page, get back to the wizard.

    MEASURED 2026-08-16 23:31, and this is the line that ended the run:
        [wd 202.9s] signed in, but this is the TALENT-POOL form, not the application
    `_past_gate` had just been taught to reject `introduceYourself`, correctly — that is Workday's
    talent-pool form and has nothing to do with the posting. But NOTHING NAVIGATED BACK. The caller
    spent its remaining rounds trying to reveal a sign-in form, sign in twice and click Create
    Account on a page that has none of those things, then woke the human. `_gate_url[0]` and
    `_regate()` both already existed and neither was called.
    DETECTING AND RECOVERING ARE TWO DIFFERENT JOBS. `_past_gate` stays a pure predicate and keeps
    its name; this wrapper owns the recovery, so all eleven call sites get it for free."""
    if await _past_gate(page):
        return True
    u = (page.url or "").lower()
    if not re.search(r"introduceyourself|talentcommunity|jobalert|createprofile|"
                     r"candidatehome", u):
        return False
    # That page is only reachable once authenticated, so we ARE signed in — just not where we need
    # to be. Re-enter the wizard and ask again.
    _log("    we ARE signed in (that page requires an account) — re-entering the apply wizard")
    if not await _regate(page):
        return False
    try:
        await _enter_from_choice(page)
    except Exception as e:
        _log(f"    could not leave the choice screen ({type(e).__name__})")
    return await _past_gate(page)


async def _past_gate(page) -> bool:
    """Are we ACTUALLY past the account gate?

    MEASURED 2026-08-16: this used to be `not _has(page,"password")` -- pure ABSENCE of evidence.
    After the Create Account submit, Workday navigated to `/login?redirect=...` which had not
    rendered yet (`visible : 0 inputs`), so "no password field" was read as "we are through". It
    returned True, `_save_account()` stored a password for an account that may not exist, and the
    run then sat on the login page. Absence of evidence is never a finding -- the oldest rule in
    this repo. Require POSITIVE evidence instead."""
    await page.wait_for_timeout(900)
    if await _has(page, "error_banner", 600):
        return False
    u = (page.url or "").lower()
    if re.search(r"/login\b|/signin\b|/authn\b", u):
        return False                                  # still on the gate, whatever has rendered
    # MEASURED: after signing in, Workday landed on `.../AccentureCareers/introduceYourself` -- the
    # TALENT-POOL form, not the job application -- and this returned True, so the driver "reached
    # Review" on a page that has nothing to do with the posting. Being signed in is not being in the
    # wizard.
    if re.search(r"introduceyourself|talentcommunity|jobalert|createprofile", u):
        _log(f"    signed in, but this is the TALENT-POOL form, not the application ({u[:60]})")
        return False
    if _on_idp(u):
        return False
    sn = await _snapshot(page)
    step = (sn.get("step") or "")
    btns = " ".join(sn.get("buttons") or []).lower()
    if re.search(r"create account/?sign in", step, re.I):
        return False                                  # stepper still says step 1
    if re.search(r"sign in with (google|email)|create account", btns):
        return False
    # POSITIVE: a later stepper position, or a real form / wizard control on screen.
    if re.search(r"my information|my experience|application questions|voluntary|review|"
                 r"autofill with resume", step, re.I):
        return True
    if int(sn.get("inputs") or 0) > 0 and re.search(r"save and continue|next|submit", btns):
        return True
    if await _has(page, "first_name", 800) or await _has(page, "review_page", 600):
        return True
    return False


async def _is_review(page) -> bool:
    """Are we on the REVIEW step of THIS application?

    MEASURED 2026-08-16: this fired on `introduceYourself`, which carries a bare `Submit` button, so
    the run reported `stage=review` with `filled=['panel:click']` on a page that was not the
    application at all. A Submit button is not evidence of a Review step -- the URL or the STEPPER is.
    Same rule as everywhere else in this file: require POSITIVE evidence of the thing you are
    claiming, not the presence of something correlated with it."""
    u = (page.url or "").lower()
    if re.search(r"introduceyourself|talentcommunity|jobalert|createprofile", u):
        return False
    if "review" in u:
        return True
    try:
        _sn = await _snapshot(page)
        if re.search(r"\breview\b", str(_sn.get("step") or ""), re.I):
            return True
    except Exception:
        pass
    if await _has(page, "review_page", 600):
        return True
    try:
        # ANCHORED BOTH ENDS: r"^\s*submit" also matches "Submit Another Application", which is on
        # the POST-submit confirmation page -- so a finished application was classified as Review and
        # the pre-submit audit ran against the wrong page (audited 2026-08-16).
        if await page.get_by_role(
                "button", name=re.compile(r"^\s*submit(\s+application)?\s*$", re.I)).count() > 0:
            return True
    except Exception:
        pass
    return False


async def _check_consent(page) -> bool:
    """Tick the 'By clicking the checkbox I consent…' banner deterministically."""
    if await _click(page, "acct_checkbox", 800):
        return True
    cbs = page.locator("input[type='checkbox']")
    try:
        n = await cbs.count()
    except Exception:
        n = 0
    for i in range(min(n, 12)):
        el = cbs.nth(i)
        try:
            if not await el.is_visible() or await el.is_checked():
                continue
            sig = ""
            eid = await el.get_attribute("id")
            if eid:
                lab = page.locator(f'label[for="{eid}"]')
                if await lab.count():
                    sig = await lab.first.inner_text()
            if not sig:
                sig = await el.evaluate("e => (e.closest('label,div,fieldset')?.innerText) || ''")
            if re.search(r"consent|agree|terms|privacy|acknowledge|certify", sig or "", re.I):
                await el.check(timeout=ACTION_TIMEOUT); return True
        except Exception:
            pass
    return False


# ---------- account --------------------------------------------------------------------------------
async def _try_signin(page, email, password) -> bool:
    # MEASURED: 70 SECONDS between "revealed the email/password form" and the panel being consulted,
    # on a gate whose password box was not on screen. Even at a 6s action cap, a dozen dead actions
    # is over a minute of silence. Ask ONE cheap question first: is there a password field at all?
    # If not, this function cannot succeed and the run should reach a reader that can SEE instead.
    if not await _has(page, "password", 1200):
        _log("    no password field on screen — not grinding a sign-in that cannot work")
        return False
    # MEASURED 2026-08-16, and this is what actually broke four runs: `r"sign\s*in"` is UNANCHORED,
    # so `get_by_role(name=...)` matched **"Sign in with Google"** as well as "Sign in with email",
    # and `.first` takes DOM order -- which on this gate is Google. So _try_signin navigated the
    # browser to accounts.google.com at ~36s and every reader afterwards was correctly reporting an
    # empty WORKDAY page while actually looking at Google's account chooser. The evidence was the
    # three links the new diagnostics finally printed:
    #   [0] 'Open Google Account Help Centre'  [1] 'Privacy Policy'  [2] 'Google Terms of Service'
    # My aria-hidden theory was wrong. THIS is the bug.
    _SIGNIN_ONLY = r"^\s*(sign\s*in|anmelden|einloggen)\s*$"
    if not await _click(page, "signin_link", 1200):
        await _click_role(page, "link", _SIGNIN_ONLY, 1200) \
            or await _click_role(page, "button", _SIGNIN_ONLY, 1200)
    await page.wait_for_timeout(800)
    await _fill(page, "email", email)
    await _fill(page, "password", password)
    # MEASURED 2026-08-16: the deterministic sign-in filled the form correctly and then FAILED to
    # press the button, because on this tenant it is labelled **"Submit"** -- and `_SIGNIN_ONLY` is
    # anchored to sign-in wording, deliberately, so it cannot hit "Sign in with Google". The panel
    # then clicked 'Submit' and we went straight through. So try the submit wordings too, each
    # ANCHORED so none of them can reach a third-party SSO control.
    if not await _click(page, "signin_submit", 2000):
        for _rx in (r"^\s*sign\s*in\s*$", r"^\s*submit\s*$", r"^\s*(anmelden|einloggen|absenden)\s*$",
                    r"^\s*(log\s*in|continue)\s*$"):
            if await _click_role(page, "button", _rx, 1500):
                _log(f"    pressed the sign-in button matching {_rx!r}")
                break
    await page.wait_for_timeout(2800)
    return await _through_gate(page)


async def _reveal_email_login(page) -> bool:
    """Workday's gate shows BUTTONS first: 'Sign in with Google' and 'Sign in with email'. The
       email/password inputs do not exist until the second one is clicked. Nothing did this, so the
       password-based paths below could never run."""
    if await _has(page, "password", 800):
        return True                                    # fields already on screen
    for rx in (r"sign in with email", r"^\s*sign in\s*$", r"use email"):
        if await _click_role(page, "button", rx, t=2000):
            await page.wait_for_timeout(2000)
            if await _has(page, "password", 2500):
                _log(f"    revealed the email/password form via '{rx}'")
                return True
    return False


async def _account(page, host, email, proposed_pw, filled) -> bool:
    _log("account gate: deciding how to get in (last-application -> Google SSO -> stored password "
         "-> create account -> ask the human)")
    # GOOGLE SSO IS NO LONGER WITHDRAWN, AND THE REASON IS EVIDENCE.
    # I withdrew it across six runs on my own reasoning -- "MFA-gated", "CDP-driven Google sign-in
    # risks a hold on the mailbox" -- and NONE of that was measured. The operator's own recording
    # (recordings/'accenture workday.py', 2026-08-17) shows the whole path completing in a headful
    # Chrome, in this exact order:
    #     Accept Cookies -> Autofill with Resume -> Sign in with Google
    #     -> textbox "Email or phone"      -> button "Next"
    #     -> textbox "Enter your password" -> button "Next"
    #     -> back to .../apply/autofillWithResume, signed in
    # So the flow works, the credentials are already in agent/.env for exactly this purpose, and my
    # blanket refusal is a large part of why we never got through. A theory does not outrank a
    # recording. It stays SECOND in the ladder (an ATS-local account is still better: no third party,
    # no MFA, reusable across tenants), and `JHW_ATS_ALLOW_SSO=0` restores the refusal.
    if os.getenv("JHW_ATS_ALLOW_SSO", "1").lower() in ("0", "false", "no"):
        try:
            import escalate as _ESC
            _ESC.withdraw("google_sso", "disabled for this run by JHW_ATS_ALLOW_SSO=0")
            _GOOGLE_DEAD[0] = True
            _log("    third-party SSO is disabled for this run (JHW_ATS_ALLOW_SSO=0)")
        except Exception:
            pass
    else:
        _log("    Google SSO is AVAILABLE (the recording proves the path); ATS-local is still first")
    acc = _load_accounts().get(host) or {}
    stored = acc.get("password")
    # THE STORED ADDRESS USED TO WIN UNCONDITIONALLY, which makes changing the configured mailbox a
    # no-op: out/ats_accounts.json still held feranicus@s4biz.io for this tenant from an earlier run,
    # so a run configured for evgeny@s4biz.io would have signed in as the OLD address and the
    # verification code would have gone somewhere we cannot read. Prefer the CONFIGURED address (it
    # is the mailbox we can read) and keep the stored one as a second key to try, because it may be
    # the address an account genuinely exists under.
    login_email = acc.get("email") or email
    alt_email = ""
    if acc.get("email") and email and acc["email"].lower() != email.lower():
        _log(f"    the store has {acc['email']} for this tenant but this run is configured for "
             f"{email} — trying the configured address FIRST, the stored one as a fallback")
        login_email, alt_email = email, acc["email"]

    # ORDER REVERSED 2026-08-16, on evidence. Google SSO used to be tried FIRST and it walked the
    # driver onto accounts.google.com's password challenge, where it span for 230s. An ATS-LOCAL
    # email+password account has no MFA, no third-party lockout risk, and is reusable across every
    # Workday tenant -- so it is strictly better for unattended 24/7 running and it goes first.
    if await _click_role(page, "button", r"use my last application", 1500):
        await page.wait_for_timeout(2000)
        if await _through_gate(page):
            filled.append("account:last-application"); return True

    # Google SSO is attempted ONLY if the tenant offers no email path, and only when the persistent
    # profile is ALREADY signed in (one-time manual login in noVNC). If it bounces us to a password
    # or MFA challenge we back out rather than typing GOOGLE_PASSWORD -- see _leave_idp().
    _google_only = (await _has_role(page, "button", r"sign in with google")
                    and not await _has_role(page, "button", r"sign in with email"))
    if _google_only:
        _log("    this gate offers Google ONLY — trying the persisted session")
        await _click_role(page, "button", r"sign in with google", 2500)
        await page.wait_for_timeout(3500)
        if await _through_gate(page):
            filled.append("account:google-sso"); return True
        if _on_idp(page.url or ""):
            await _leave_idp(page, {"note": ""})

    # Reveal the email/password form; every path below needs those inputs to exist.
    _revealed = await _reveal_email_login(page)
    if not _revealed:
        _log("    could not reveal an email/password form on this gate")

    # THE UNATTENDED PATH, ATTEMPTED PROPERLY. Measured: across six runs there was never a
    # "switched to Create Account" line -- the single `_click_role(page,"button",r"create account")`
    # probe found nothing (on this tenant it is a LINK inside the revealed form, and it is sometimes
    # worded "Create Account" / "Sign Up" / "New user"), so the one path that can complete without a
    # human was never even tried. Try every shape, and say which one worked.
    async def _open_create_account() -> bool:
        for role, rx in (("link", r"^\s*create account\s*$"), ("button", r"^\s*create account\s*$"),
                         ("link", r"create an account|sign up|new user|register"),
                         ("button", r"create an account|sign up|new user|register")):
            if await _click_role(page, role, rx, 1800):
                await page.wait_for_timeout(2200)
                _log(f"    opened Create Account via {role} {rx!r}")
                return True
        # last resort: a plain text link the roles missed
        try:
            el = page.get_by_text(re.compile(r"^\s*create account\s*$", re.I)).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=ACTION_TIMEOUT)
                await page.wait_for_timeout(2200)
                _log("    opened Create Account via a text link")
                return True
        except Exception:
            pass
        _log("    no Create Account control found on this gate")
        return False

    if stored and await _try_signin(page, login_email, stored):
        _log("    signed in with the stored password")
        filled.append("account:signin"); return True
    if stored and alt_email and await _try_signin(page, alt_email, stored):
        _log(f"    signed in as the stored address {alt_email}")
        filled.append("account:signin"); return True

    # If we are on the Sign In form and have no stored password, switch to Create Account. Without
    # this the run could only ever ask the human, which is not "apply 24/7 on my behalf".
    # THE ANSWER TO "why not just create an account". `out/ats_accounts.json` held a password for
    # this tenant under the OLD address, so `stored` was truthy and the `and not stored` below
    # skipped the create path ENTIRELY on every run -- while signing in with that stale credential
    # produced exactly what the screenshot shows: "You may have entered the wrong email address or
    # password". A credential that has just FAILED is not a reason to refuse to register.
    on_create = await _has(page, "verify_pw", 1200) or await _has(page, "acct_checkbox", 800)
    if not on_create:
        if await _open_create_account():
            on_create = await _has(page, "verify_pw", 2000) or await _has(page, "password", 1500)
            _log(f"    Create Account form {'found' if on_create else 'NOT found'}")
    if on_create:
        _log(f"    creating an account for {email} with the vault password (remembered for reuse)")
        # A WRITE IS NOT DONE UNTIL IT HAS BEEN READ BACK -- this repo's own rule, and I broke it
        # here. MEASURED: all three fills and the submit logged at the SAME timestamp (42.2s), i.e.
        # every `_fill` returned False instantly, nothing checked, and a BLANK form was submitted.
        # Workday bounced it to /login and the run blamed the credential.
        _wrote = {}
        for key, val, label in (("email", email, "e-mail"),
                                ("password", proposed_pw, "password"),
                                ("verify_pw", proposed_pw, "verify password")):
            ok = await _fill(page, key, val)
            got = ""
            if ok:
                el = await _first(page, key, 1200)     # same lookup _fill used
                if el:
                    try:
                        got = await el.input_value(timeout=ACTION_TIMEOUT)
                    except Exception:
                        pass
            # the READ-BACK is the evidence, not the fill returning without throwing
            _wrote[key] = bool(ok and got)
            _log(f"      {label}: " + ("FIELD NOT FOUND" if not ok
                                       else f"reads {len(got)} char(s)" if got
                                       else "WRITTEN BUT READS EMPTY"))
        if not (_wrote.get("email") and _wrote.get("password")):
            # DO NOT SUBMIT A BLANK FORM. Submitting one produces a bounce that looks exactly like a
            # rejected credential, which is how six runs blamed the password.
            _log("    the create form did not accept our values — NOT submitting a blank form")
            _create_blocked = True
        else:
            _create_blocked = False
            # THE RETURN VALUE WAS DISCARDED AT EVERY CALL SITE. Workday's own documentation says a
            # candidate "must click the Acknowledge checkbox before completing account setup", and
            # submitting without it bounces straight back to /login — which is EXACTLY the 23:31
            # symptom, and it was blamed on the password. Not finding the tick is worth recording
            # before we submit, the same way not accepting our values is.
            _consent_ok = await _check_consent(page)
            if not _consent_ok:
                _log("    NO consent checkbox found on the create form — if this tenant requires "
                     "one, the submit will bounce and it will NOT be the password's fault")
            if not await _click(page, "create_submit", 2000):
                await _click_role(page, "button", r"^\s*create\s*account\s*$")
        await page.wait_for_timeout(2800)
        if await _through_gate(page):
            _save_account(host, email, proposed_pw); filled.append("account:create")
            _log("    account created and we are past the gate")
            return True
        # The commonest reason Create Account bounces: the account ALREADY EXISTS (an earlier run,
        # or you registered by hand). Same vault password, so just sign in with it.
        _err = (await _error_text(page)) or ""
        if _err:
            # SAY WHAT THE SITE SAID. Six runs guessed at this because the old reader found nothing.
            _log(f"    the site says: {_err[:220]}")
        elif not _consent_ok:
            _log("    the site said nothing, and we could not tick a consent box — that, not the "
                 "password, is the likeliest reason this bounced")
        _sn2 = await _snapshot(page)
        if re.search(r"already (exist|in use|registered)|account with this e-?mail", _err, re.I) \
           or re.search(r"/login\b", (page.url or "").lower()):
            _log(f"    create bounced ({_err[:70] or 'redirected to /login'}) — trying SIGN IN "
                 f"with the same vault password")
            await _reveal_email_login(page)          # best effort; do NOT gate the attempt on it
            if await _try_signin(page, email, proposed_pw):
                _save_account(host, email, proposed_pw); filled.append("account:signin")
                _log("    signed in with the vault password")
                return True
        # Workday often requires the emailed verification code before the account is usable.
        if re.search(r"verif|confirm your e-?mail|code", (_err + " " + " ".join(
                _sn2.get("buttons") or []) + " " + " ".join(_sn2.get("reqEmpty") or [])), re.I):
            # READ IT OUT OF THE MAILBOX. This is the single dependency that stood between the
            # engine and unattended 3am running, and it was never a HUMAN dependency -- it is a
            # MAILBOX dependency. Configure JHW_MAIL_USER/JHW_MAIL_PASS once and no code is ever
            # asked for again. The Telegram ask stays as the last rung, for an unconfigured mailbox.
            _log("    this tenant wants an EMAIL VERIFICATION CODE")
            code = ""
            try:
                import mailcode as MC
                if MC.configured():
                    _log("    reading it from the mailbox (waiting up to 90s for it to arrive)")
                    code = MC.fetch_code(wait_s=90, log=_log)
                else:
                    _log(f"    mailbox not configured — {MC.why_not()}")
            except Exception as e:
                _log(f"    mailbox reader unavailable ({type(e).__name__})")
            if not code:
                code = await _ask(f"Workday ({host}) emailed a verification code to {email}. "
                                  f"Reply with the 6-digit code and I will finish the application.")
            if code and not code.startswith("NO_ANSWER"):
                digits = re.sub(r"\D", "", code)[:8]
                for key in ("verify_code", "code", "password"):
                    if await _fill(page, key, digits):
                        break
                # ANCHORED: r"...|continue|..." also matches "Continue with Google", which would
                # leave the ATS -- the same defect class that sent this driver to accounts.google.com.
                await _click_role(page, "button",
                                  r"^\s*(verify|submit|continue|next|weiter|bestätigen)\s*$", t=2500)
                await page.wait_for_timeout(2500)
                if await _through_gate(page):
                    _save_account(host, email, proposed_pw)
                    filled.append("account:verified"); return True

    # RUNG 2 OF THE LADDER, BEFORE RUNG 3. Every deterministic path above has failed, so this is a
    # genuine stall -- exactly where a panel is cheap and a human is expensive. The three vendors see
    # the page INCLUDING the sign-in MODAL that owns it (the candidate's screenshot: a dialog over the
    # job page, taller than the viewport, whose controls _SNAP_JS never listed because it enumerated
    # the whole document). They may reveal the email form, scroll the dialog, dismiss an
    # informational overlay, or use the persisted Google session -- and they are refused outright if
    # they reach for a password field, because the vault owns credentials, not the panel.
    # I WAS TELLING THE PANEL TO TRY GOOGLE. The old version of this text said the profile "may
    # ALREADY hold a Google session, so 'Sign in with Google' can succeed with no password" -- so of
    # course every round proposed Google, and every round lost ~80s on the account chooser. The
    # session does exist (the chooser showed the candidate's name), but the account is MFA-gated and
    # CDP-driven Google sign-in risks a hold on the mailbox everything else depends on. The goal is
    # the ATS-LOCAL account, so say that and nothing else.
    _facts = (f"- the ATS login e-mail is {login_email}\n"
              f"- an ATS-local password already exists in the vault; deterministic code fills it. "
              f"NEVER ask for it and NEVER fill a password field yourself\n"
              f"- DO NOT use Google, Microsoft or LinkedIn sign-in. They are MFA-gated and are not "
              f"an option in this run, whatever the page offers\n"
              f"- THE GOAL: reveal the e-mail form ('Sign in with email'), then CREATE AN ACCOUNT "
              f"with that e-mail if no account exists. That is the only unattended path\n"
              f"- getting onto step 2 of 6 ('My Information') is success")
    esc = await _escalate(page, f"cannot get past the {host} Workday sign-in gate by myself",
                          filled, facts=_facts,
                          tried=["stored/vault password sign-in", "create account",
                                 "'use my last application'"])
    if esc.get("moved"):
        # RE-TRY THE DETERMINISTIC PATHS. The panel's job is to unblock the page, not to sign in:
        # once the email/password form is actually on screen the vault password is the right key.
        if await _through_gate(page):
            _log("    the panel got us through the gate")
            return True
        if stored and await _try_signin(page, login_email, stored):
            _log("    panel revealed the form; signed in with the stored password")
            filled.append("account:signin"); return True
        if await _try_signin(page, email, proposed_pw):
            _save_account(host, email, proposed_pw)
            _log("    panel revealed the form; signed in with the vault password")
            filled.append("account:signin"); return True

    # A SECOND PANEL ROUND BEFORE ANY HUMAN. The first round unblocked the page (it clicked
    # 'Sign in with Google') and the deterministic retries still failed, and the OLD code went
    # straight to Telegram at that point -- which is the opposite of "run at 3am without me".
    # Round two is told exactly what already failed, so it cannot propose the same thing, and it can
    # now see the Google page it landed on and take us back to the ATS-local path.
    if not esc.get("asked"):
        _tried2 = ["stored/vault password sign-in", "create account", "'use my last application'",
                   f"the panel's own {esc.get('act', {}).get('verb', 'action')} — page moved but the "
                   f"sign-in still failed"]
        if _on_idp(page.url or ""):
            _log("    we are on an identity provider, not the ATS — going back before round 2")
            await _leave_idp(page, {"note": ""})
            _tried2.append("Google SSO: no live session in this profile, it wants a password/MFA")
        esc2 = await _escalate(page, f"still cannot get past the {host} Workday sign-in gate; the "
                                     f"ATS-LOCAL email+password account is the goal, not Google",
                               filled, facts=_facts, tried=_tried2)
        if esc2.get("moved"):
            if await _through_gate(page):
                _log("    round 2 got us through the gate")
                return True
            # The commonest reason the vault password fails is that no account exists YET. Create it;
            # with the mailbox configured that completes itself.
            if await _reveal_email_login(page):
                if stored and await _try_signin(page, login_email, stored):
                    filled.append("account:signin"); return True
                if await _try_signin(page, email, proposed_pw):
                    _save_account(host, email, proposed_pw)
                    filled.append("account:signin"); return True
            if await _click_role(page, "button", r"create account", t=2000) or \
               await _click_role(page, "link", r"create account", t=1500):
                await page.wait_for_timeout(2500)
                await _fill(page, "email", email)
                await _fill(page, "password", proposed_pw)
                await _fill(page, "verify_pw", proposed_pw)
                await _check_consent(page)
                if not await _click(page, "create_submit", 2000):
                    await _click_role(page, "button", r"create\s*account")
                await page.wait_for_timeout(2800)
                if await _through_gate(page):
                    _save_account(host, email, proposed_pw)
                    _log("    round 2 created the account and we are through")
                    filled.append("account:create"); return True
        if esc2.get("asked"):
            esc = esc2

    # RUNG 3: only now is the human worth waking. `esc["asked"]` is already the human's reply if the
    # panel chose to ask -- do not ask twice for the same thing.
    # `_intent` MUST exist on every path. MEASURED, and it killed the run:
    #   workday driver error: cannot access local variable '_intent' where it is not associated
    # When the PANEL asked the human (`esc["asked"]` non-empty) the ask block below was skipped, so
    # `_intent` was never assigned and the `if _intent == "SSO"` further down raised. A reply that
    # arrives by a different route is still a reply -- parse it the SAME way.
    _intent = "UNKNOWN"
    pw = esc.get("asked") or ""
    if pw:
        try:
            import converse as CV
            _d = CV.parse(pw, ["CREATE", "RESET", "PASSWORD", "SKIP"])
            _intent, pw = _d.get("intent", "UNKNOWN"), (_d.get("value") or "")
            _log(f"    your reply to the panel -> INTENT {_intent} ({_d.get('why')})")
            if _intent in ("CREATE", "RESET", "SKIP", "STOP", "SSO"):
                pw = ""          # not a credential: fall into the intent handlers below
        except Exception as e:
            _log(f"    could not parse your reply ({type(e).__name__})")
    if not pw and _intent in ("UNKNOWN", "PASSWORD"):
        # A REAL CONVERSATION WITH CLEAR ACTIONS. His words, and he is right: an open "reply with the
        # PASSWORD" prompt cannot hold a conversation. Twice tonight he answered with an
        # INSTRUCTION -- "Use google sso", "no lets create one" -- and the code typed it into the
        # password box. Now the ask OFFERS the actions and the reply is parsed into an INTENT from a
        # closed set, each of which the engine actually performs.
        # THE FOURTH MODEL WRITES THE MESSAGE. kimi-k2.6 turns the machine state into one human
        # sentence plus the numbered options; if it is unavailable, or if it drops an option, the
        # deterministic text is sent instead. It can make the conversation clearer, never narrower.
        try:
            import converse as CV
            _opts = ["CREATE", "RESET", "PASSWORD", "SKIP"]
            _q = CV.ask_text(
                f"I cannot get into {host} for this Accenture job.\n"
                f"The e-mail I am using is {login_email}. What should I do?",
                _opts)
            try:
                import speaker as SPK
                _q = await SPK.say({"problem": f"I cannot sign in to {host} for the Accenture job.",
                                    "employer": "Accenture", "email": login_email,
                                    "tried": ["the vault password", "creating an account",
                                              "Google sign-in (withdrawn: MFA)"],
                                    # NOTE: `ctrls` belongs to _escalate's scope, not this one.
                                    # I referenced it here and the ruff F821 gate caught it -- the
                                    # 17th invented/out-of-scope name this session. The speaker does
                                    # not need the control list to write the message.
                                    "votes": []},
                                   _opts, CV.DEFAULT_LABELS, log=_log) or _q
            except Exception as e:
                _log(f"    the speaker is unavailable ({type(e).__name__}) — sending the plain ask")
        except Exception:
            CV, _opts, _q = None, [], (
                f"JobHuntWOW is stuck at the Workday login for {host}. Reply with the PASSWORD "
                f"for {login_email}, or say 'create an account' / 'reset the password' / 'skip'.")
        _log(f"    ASKING YOU on Telegram, with options: {_opts or 'free text'}")
        reply = await _ask(_q)
        # NOBODY IS AWAKE IS NOT THE SAME ANSWER AS "STOP".
        # MEASURED 2026-08-16 23:31: the ask went out, nothing came back, and the run reported
        # `ok=False stage=account` -- so an absent human became a failed application, on a system
        # whose stated purpose is to run at 3am unattended. The panel is the rung BELOW the human for
        # exactly this case; asking it for a decision costs ~7s and it has already been right at this
        # gate every single round. So: if no reply arrived, tell the panel the human is unreachable,
        # tell it what has ALREADY been tried so it cannot re-propose a dead end, and act.
        if not (reply or "").strip():
            _w = 0.0
            try:
                _w = float(ask.waited().get("waited") or 0)
            except Exception:
                pass
            _log(f"    no reply after {int(_w)}s — the panel decides rather than the run failing")
            _auto = await _escalate(
                page,
                "NOBODY IS AVAILABLE TO ASK. Get through this Workday account gate on your own: "
                "the goal is an ATS-LOCAL email+password account, never a third-party SSO.",
                filled, facts=_facts,
                tried=["the stored/vault password", "revealing the e-mail form", "Create Account",
                       "asking the human — no reply, so do not propose asking again"])
            if _auto.get("moved"):
                if await _through_gate(page):
                    _log("    the panel got us through the gate with nobody watching")
                    filled.append("account:panel-autonomous")
                    return True
                _log("    the panel moved the page but we are not through yet — continuing")
            # LAST AUTONOMOUS ROUNG: registration is the ONE path that needs no prior credential and
            # no third party, and with the mailbox configured it completes itself.
            if await _reveal_email_login(page) or True:
                if await _open_create_account():
                    _log("    registering on our own, since nobody answered")
                    _ok = True
                    for key, val in (("email", email), ("password", proposed_pw),
                                     ("verify_pw", proposed_pw)):
                        if not await _fill(page, key, val):
                            _ok = False
                    if _ok and await _check_consent(page) is not None:
                        if not await _click(page, "create_submit", 2000):
                            await _click_role(page, "button", r"^\s*create\s*account\s*$")
                        await page.wait_for_timeout(3000)
                        if await _through_gate(page):
                            _save_account(host, email, proposed_pw)
                            filled.append("account:create-autonomous")
                            _log("    registered unattended and we are through the gate")
                            return True
                        _e2 = await _error_text(page)
                        if _e2:
                            _log(f"    the site says: {_e2[:200]}")
        pw, _intent = "", "UNKNOWN"
        if CV:
            # DETERMINISTIC PARSE FIRST, and the speaker only rescues what it cannot read. read_reply
            # enforces that ordering and re-validates the speaker's answer against the closed set.
            try:
                import speaker as SPK
                d = await SPK.read_reply(reply, _opts, log=_log)
            except Exception:
                d = CV.parse(reply, _opts)
            _intent, pw = d.get("intent", "UNKNOWN"), d.get("value", "")
            _log(f"    you said {str(reply)[:44]!r} -> INTENT {_intent} ({d.get('why')})")
        else:
            pw = (reply or "").strip()

        # ---- ACT ON THE INTENT. Each branch DOES something; none of them types an instruction.
        if _intent == "SKIP":
            _log("    skipping this employer, as you asked")
            filled.append("account:skipped")
            return False
        if _intent == "STOP":
            _log("    stopping, as you asked")
            return False
        if _intent == "CREATE":
            _log("    you said CREATE — opening Create Account and registering with the vault "
                 "password")
            await _reveal_email_login(page)
            if await _open_create_account():
                for key, val in (("email", email), ("password", proposed_pw),
                                 ("verify_pw", proposed_pw)):
                    await _fill(page, key, val)
                await _check_consent(page)
                if not await _click(page, "create_submit", 2000):
                    await _click_role(page, "button", r"^\s*create\s*account\s*$")
                await page.wait_for_timeout(3000)
                if await _through_gate(page):
                    _save_account(host, email, proposed_pw)
                    _log("    account created on your instruction and we are through")
                    filled.append("account:create-human"); return True
                _log("    the create submit did not get us through; checking for a code e-mail")
                try:
                    import mailcode as MC
                    if MC.configured():
                        code = MC.fetch_code(wait_s=90, log=_log)
                        if code:
                            digits = re.sub(r"\D", "", code)[:8]
                            for k in ("verify_code", "code", "password"):
                                if await _fill(page, k, digits):
                                    break
                            await _click_role(page, "button",
                                              r"^\s*(verify|submit|continue|next)\s*$", t=2500)
                            await page.wait_for_timeout(2500)
                            if await _through_gate(page):
                                _save_account(host, email, proposed_pw)
                                filled.append("account:verified"); return True
                    else:
                        _log(f"    and I cannot read the mailbox: {MC.why_not()}")
                except Exception as e:
                    _log(f"    mailbox reader unavailable ({type(e).__name__})")
            return False
        if _intent == "RESET":
            _log("    you said RESET — clicking 'Forgot your password?'")
            if await _click_role(page, "button", r"forgot your password", 2500) \
               or await _click_role(page, "link", r"forgot your password", 2000):
                await _fill(page, "email", login_email)
                await _click_role(page, "button", r"^\s*(submit|continue|send|reset)\s*$", t=2500)
                await page.wait_for_timeout(2500)
                _log("    reset requested. I cannot choose a new password from the e-mail link "
                     "unattended, so tell me the new one when you have set it.")
            return False

    pw = (pw or "").strip()
    if pw and not pw.upper().startswith("NO_ANSWER"):
        if _intent == "SSO" or (re.search(r"google|sso|oauth", pw, re.I) and len(pw) < 40):
            _log("    you told me to use Google SSO — trying the persisted session")
            if await _click_role(page, "button", r"sign in with google", 2500):
                await page.wait_for_timeout(3500)
                if await _through_gate(page):
                    filled.append("account:google-sso-human"); return True
                if _on_idp(page.url or ""):
                    await _leave_idp(page, {"note": ""})
                    _log("    Google wanted a password/MFA challenge — backing out rather than "
                         "typing your Google password (that account gates your mailbox)")
            _log("    Google SSO did not get us in; the profile has no live session")
            return False
        # THE SHORT-CIRCUIT THREW HIS ANSWER AWAY. `_reveal_email_login(page) and _try_signin(...)`
        # means that when the form could not be revealed -- which is exactly what happens on a page
        # that read as 0 controls -- the password he had just typed was NEVER TRIED. He said the run
        # "disregarded what I sent in the telegram", and he was right.
        _revealed_now = await _reveal_email_login(page)
        if not _revealed_now:
            _log("    the sign-in form is not on screen — reloading the gate to get it back")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
            except Exception:
                pass
            _revealed_now = await _reveal_email_login(page)
        for _who in [e for e in (login_email, alt_email, email) if e]:
            if await _try_signin(page, _who, pw):
                _save_account(host, _who, pw)
                _log(f"    signed in as {_who} with the password you sent")
                filled.append("account:signin-human"); return True
        _log(f"    tried your password against "
             f"{[e for e in (login_email, alt_email, email) if e]} — none of them got in")
    return False


# ---------- #1 THE FIX: resume upload + server-side parse wait --------------------------------------
async def _upload_resume(page, path: str, r: dict) -> bool:
    """Workday parses the resume SERVER-SIDE. Delete any existing attachment (uploads stack otherwise),
       upload to the HIDDEN input (set_input_files has zero actionability checks — the documented path),
       then WAIT until it's actually attached before advancing."""
    fi = await _present(page, "file_input")
    if not fi:
        return False
    if await _has(page, "delete_file", 700):                 # existing attachment -> remove first
        await _click(page, "delete_file", 1500)
        await page.wait_for_timeout(1500)
        fi = await _present(page, "file_input") or fi
    try:
        await fi.set_input_files(path)
    except Exception as e:
        r["note"] += f" upload:{e};"
        return False
    # WAIT for the parse: the delete-file control appearing means the file is attached & processed.
    for _ in range(25):
        await page.wait_for_timeout(1000)
        if await _has(page, "delete_file", 400):
            r["filled"].append("resume")
            print("[wd] resume attached + parsed", flush=True)
            return True
    r["note"] += " resume uploaded but parse not confirmed;"
    r["filled"].append("resume?")
    return True


# ---------- #5 LLM ONLY FOR FREE TEXT ---------------------------------------------------------------
async def _answer_choices(page, profile, answer_fn) -> int:
    if not answer_fn:
        return 0
    answered = 0
    groups = page.locator("[data-automation-id='radioGroup'], fieldset")
    try:
        n = await groups.count()
    except Exception:
        n = 0
    for i in range(min(n, 15)):
        g = groups.nth(i)
        try:
            if not await g.is_visible():
                continue
            radios = g.locator("input[type='radio']")
            rc = await radios.count()
            if rc == 0 or any([await radios.nth(j).is_checked() for j in range(rc)]):
                continue
            qtext = (await g.inner_text())[:300]
            labs = g.locator("label")
            opts = []
            for j in range(min(await labs.count(), 8)):
                t = (await labs.nth(j).inner_text()).strip()
                if t:
                    opts.append(t)
            choice = await answer_fn(qtext, opts, profile)
            if choice and choice.strip().upper().startswith("UNKNOWN"):
                choice = await _ask(f"Application question needs your answer:\n{qtext}\nOptions: {opts}")
            if choice:
                for j in range(min(await labs.count(), 8)):
                    t = (await labs.nth(j).inner_text()).strip()
                    if t and (t.lower() in choice.lower() or choice.lower() in t.lower()):
                        await labs.nth(j).click(); answered += 1; break
        except Exception:
            pass
    return answered


def _apply_urls(ats_url: str) -> list:
    """Candidate deep-links. Workday's canonical apply URL carries a locale segment (/en-US/) that the
       LinkedIn-supplied URL lacks; without it Workday bounces you back to the posting."""
    base = ats_url.split("?")[0].rstrip("/")
    if "/apply" in base:
        return [base]
    urls = []
    m = re.match(r"(https?://[^/]+)(/.*)", base)
    if m and "/en-US/" not in base:
        urls.append(m.group(1) + "/en-US" + m.group(2) + "/apply/applyManually")
    urls.append(base + "/apply/applyManually")
    return urls


# The four buttons Workday shows on the apply CHOICE screen. MEASURED on the live Accenture req
# R00329883 (Chrome MCP, 2026-08-16): each is a real href that adds an /en-US/ locale segment and one
# more path segment -- /apply/applyManually, /apply/autofillWithResume, /apply/useMyLastApplication.
# applyManually = 6 wizard steps, autofillWithResume = 7 (it inserts a resume step). Both then gate
# at step 1 (Create Account/Sign In), so the step index is a function of entry path AND auth state.
_CHOICE_RX = r"apply manually|autofill with resume|use my last application|apply with linkedin"
_WIZARD_RX = r"/apply/(applymanually|autofillwithresume|usemylastapplication|applywithlinkedin)"


async def _in_apply_flow(page) -> bool:
    """Are we actually inside the multi-step apply wizard (not bounced back to the job posting)?

    THE BUG THIS FIXES, measured 2026-08-16: the test used to be `"/apply" in url`. The deep-link
    is `.../apply?source=jb_1`, which CONTAINS "/apply" -- but that URL renders the CHOICE SCREEN,
    with FOUR buttons and ZERO inputs. So this returned True, the caller skipped its entire
    click-through branch, and the page loop spun for 46s on a screen that has no form:
        buttons : ['English','Sign In','Search Careers','Introduce Yourself',
                   'Autofill with Resume','Apply Manually','Use My Last Application']
        filled=[]
    The wizard is ONE SEGMENT DEEPER. Match that, or require real form evidence."""
    u = (page.url or "").lower()
    if re.search(_WIZARD_RX, u):
        return True
    for k in ("password", "my_info_page", "my_exp_page", "review_page", "next"):
        if await _has(page, k, 600):
            return True
    return False


async def _visible_buttons(page) -> list:
    return (await _snapshot(page)).get("buttons") or []


async def _enter_from_choice(page) -> bool:
    """Click out of the apply CHOICE screen into the wizard.

    Default is **Apply Manually**: it is the deterministic 6-step path and it does not depend on a
    previous application existing. RECORDINGS_FINDINGS.md shows the candidate's own recording using
    "Use My Last Application" (which prefills and is faster) -- select it with JHW_WD_ENTRY=last,
    or the resume-autofill path with JHW_WD_ENTRY=autofill."""
    btns = " ".join(await _visible_buttons(page)).lower()
    if not re.search(_CHOICE_RX, btns):
        return False
    pref = (os.getenv("JHW_WD_ENTRY", "manual") or "manual").strip().lower()
    order = {"last":     [r"use my last application", r"apply manually", r"autofill with resume"],
             "autofill": [r"autofill with resume", r"apply manually", r"use my last application"],
             }.get(pref, [r"apply manually", r"use my last application", r"autofill with resume"])
    _log(f"apply CHOICE screen detected (entry preference: {pref})")
    for rx in order:
        if await _click_role(page, "button", rx, t=3000) or await _click_role(page, "link", rx, t=1500):
            _log(f"    clicked '{rx}' — entering the wizard")
            await page.wait_for_timeout(3500)
            _log(f"    now at {(page.url or '')[:78]}")
            return True
    _log("    NONE of the choice buttons could be clicked")
    return False


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    wd = [p for p in ctx.pages if re.search(r"myworkday|workday", (p.url or ""), re.I)]
    page = wd[-1] if wd else (ctx.pages[-1] if ctx.pages else await ctx.new_page())
    for p in wd[:-1]:                                        # close stale tabs from earlier runs
        try: await p.close()
        except Exception: pass
    # MEASURED 2026-08-16: 175 SECONDS passed between "revealed the email/password form" and the
    # panel being consulted, on a page that was never going to accept those keys. Cause: `_fill`
    # waits only 4s to FIND an element, but the `.click()`/`.fill()` that follows carries no timeout
    # and therefore inherits Playwright's 30-SECOND default -- so every non-actionable field burned
    # half a minute in silence. llm_driver has capped this at 8s since it was written; this adapter
    # never did. A stall must reach the panel in seconds, not minutes, or "runs at 3am" is fiction.
    page.set_default_timeout(ACTION_TIMEOUT)
    ctx.set_default_timeout(ACTION_TIMEOUT)
    return browser, ctx, page


async def drive(creds: dict, data: dict, resume_path: str = "", answer_fn=None, ats_url: str = "") -> dict:
    r = {"ok": False, "stage": "start", "filled": [], "note": "", "needs_llm": False}
    b = data.get("basics", {})
    legal = (b.get("legal_name") or b.get("name") or "").split()
    first = legal[0] if legal else ""
    last = " ".join(legal[1:]) if len(legal) > 1 else ""
    line1, postal, city = _split_address(b.get("address", ""))
    _T0[0] = _time.time()
    _log(f"START recorded Workday adapter :: {(ats_url or '')[:78]}")
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)

        # #2 DEEP-LINK to the manual apply flow, trying the /en-US/ locale variant first.
        r["stage"] = "deeplink"
        if ats_url:
            for u in _apply_urls(ats_url):
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    r["note"] += f" deeplink:{e};"; continue
                if await _in_apply_flow(page):
                    print(f"[wd] deep-link OK -> {page.url[:90]}", flush=True); break
                print(f"[wd] deep-link bounced ({u[:70]}…)", flush=True)
        if not re.search(r"myworkday|workday", (page.url or ""), re.I):
            r["note"] = f"not on Workday ({page.url})"; return r
        await page.bring_to_front()

        r["stage"] = "cookie"
        if not await _click(page, "cookie", 2500):
            await _click_role(page, "button", r"accept cookies|allow all cookies|accept all")
        if await _has(page, "already_applied", 800):
            r["ok"] = True; r["stage"] = "already_applied"
            r["note"] = "Workday says you have already applied to this job."
            return r

        # VERIFY we're in the wizard; if the deep-link bounced, click through: Apply -> Apply Manually.
        r["stage"] = "enter_flow"
        if not await _in_apply_flow(page):
            # FIRST: the choice screen, which is what the deep-link actually lands on. The old code
            # went straight to looking for a button called "Apply" -- which does not exist here.
            await _enter_from_choice(page)
        if not await _in_apply_flow(page):
            if await _has(page, "apply", 3000):
                print("[wd] not in the apply flow — clicking Apply …", flush=True)
                await _click(page, "apply")
                await page.wait_for_timeout(3000)
                await _click(page, "apply_manual", 4000)
                await page.wait_for_timeout(2500)
                print(f"[wd] after Apply -> {page.url[:90]}", flush=True)
            if not await _in_apply_flow(page):
                r["note"] = f"could not enter the Workday apply flow from {page.url[:90]}"
                await _notify(r["note"]); return r

        r["stage"] = "account"
        host = _host(page.url)
        _gate_url[0] = page.url or ""        # so an IdP detour can be undone (see _leave_idp)
        await _log_page(page, "after cookies / apply-flow check")
        # THE GATE IS BUTTONS BEFORE IT IS FIELDS. Measured 2026-08-16: step 1 of 6 renders
        #   buttons: [... 'Sign in with Google', 'Sign in with email']   inputs: 0
        # The password box only appears AFTER clicking "Sign in with email". The old test was
        # `_has(page,"password")`, so it logged "absent -> already authenticated" and SKIPPED the
        # entire _account() handler -- which already knew how to do all of this. Same class of bug
        # as `"/apply" in url`: detecting the wrong evidence for the right state.
        _sn = await _snapshot(page)
        _btxt = " ".join(_sn.get("buttons") or []).lower()
        _gate = (bool(await _has(page, "password", 1200))
                 or bool(re.search(r"create account/?sign in", (_sn.get("step") or ""), re.I))
                 or bool(re.search(r"sign in with (google|email)|create account", _btxt)))
        _log(f"auth gate: {'PRESENT -> handling it' if _gate else 'not detected -> already authenticated'}"
             + (f"  [stepper: {_sn.get('step','')[:40]}]" if _sn.get("step") else ""))
        if _gate:
            if not await _account(page, host, creds["email"], creds["password"], r["filled"]):
                r["note"] = (f"Could not pass the Workday account gate for {host}. Reset the password via "
                             "'Forgot your password?' then: python jhw.py atspw <host> '<pw>' <login-email>")
                await _notify(r["note"])
                return r

        # ---- page loop: fill -> upload(+parse wait) -> answer -> Save and Continue ----
        r["stage"] = "pages"
        last_err = ""
        _repaired: set = set()      # one targeted repair per DISTINCT error, never a loop
        for _i in range(10):
            await page.wait_for_timeout(1500)
            sn = await _log_page(page, f"page {_i + 1}/10")
            if _on_idp(page.url or ""):
                # 230s / 7 iterations were burned here once. Break out on the FIRST sighting.
                if not await _leave_idp(page, r):
                    r["stage"] = "blocked"
                    r["note"] = ("Stopped on a third-party sign-in page ("
                                 + (page.url or "")[:70] + "). I do not type your Google/Microsoft "
                                 "password into an automated browser: it is MFA-gated and Google "
                                 "flags it. Sign in ONCE in noVNC (the profile keeps the session), "
                                 "or let me create an ATS-local account instead.")
                    await _notify("Workday: hit a Google/Microsoft sign-in page. Sign in ONCE in "
                                  "noVNC at http://localhost:9090/vnc.html and re-run — the "
                                  "profile keeps the session. I will not type your Google password.")
                    return r
                continue
            # THE GATE CAN COME BACK. Measured: Create Account bounced to /login and _account()
            # was only ever called ONCE, before this loop, so nothing could recover. Retry it here,
            # capped so a permanently broken gate cannot spin.
            _bl = " ".join(sn.get("buttons") or []).lower()
            if re.search(r"sign in with (google|email)|create account", _bl) \
               or re.search(r"/login\b", (page.url or "").lower()):
                r["_gate_tries"] = int(r.get("_gate_tries") or 0) + 1
                if r["_gate_tries"] <= 2:
                    _log(f"the account gate is back (attempt {r['_gate_tries']}/2) — handling it")
                    if await _account(page, host, creds["email"], creds["password"], r["filled"]):
                        continue
                else:
                    _log("the account gate keeps coming back — stopping instead of spinning")
                    r["stage"] = "blocked"
                    r["note"] = (f"Cannot get past the {host} account gate. Sign in ONCE in noVNC "
                                 f"(http://localhost:9090/vnc.html) and re-run — the profile keeps "
                                 f"the session. Vault credentials are stored for {creds['email']}.")
                    await _notify(r["note"])
                    return r
            if re.search(_CHOICE_RX, _bl):
                # Bounced back to (or never left) the choice screen. Recover instead of spinning.
                if await _enter_from_choice(page):
                    continue
            if await _is_review(page):
                r["stage"] = "review"
                _log(f"REVIEW reached. filled={sorted(set(r['filled'])) or '[]'}")
                sent = await _submit_now(page, r)
                r["ok"] = True
                if sent:
                    r["stage"] = "submitted"
                    r["note"] = ("SUBMITTED. Filled: " + ", ".join(sorted(set(r["filled"]))) + ".")
                    await _notify(f"Workday: APPLICATION SUBMITTED. Filled: "
                                  f"{', '.join(sorted(set(r['filled'])))}.")
                else:
                    r["note"] = (r["note"] or "") + (
                        " Reached Review; submission NOT confirmed — check noVNC. Filled: "
                        + ", ".join(sorted(set(r["filled"]))) + ".")
                    await _notify("Workday: reached Review but the submit was NOT confirmed. "
                                  "Please check noVNC.")
                return r

            # My Information (only if this tenant shows it)
            if await _has(page, "first_name", 3000):
                _log("My Information detected — filling from candidate.md")
                # THE PHONE MUST BE THE NATIONAL NUMBER, DIGITS ONLY — and his intive recording is
                # him discovering that the hard way: `+49 157 8551545`, then `+49 1578551545`, then
                # `1578551545`, each REJECTED, until `15785541545` was accepted. We sent
                # `+49 15785541545` and the site answered "Enter a valid format for Phone Number."
                # This is the IDENTICAL fact already measured on Greenhouse, and `phone_national()`
                # already existed there — the Workday path simply never called it. One rule, one home.
                try:
                    from greenhouse import phone_national as _natl
                except Exception:
                    from flows.greenhouse import phone_national as _natl   # type: ignore
                _phone = _natl(b.get("phone", "")) or b.get("phone", "")
                for _key, _val in (("first_name", first), ("last_name", last), ("addr1", line1),
                                   ("city", city), ("postal", postal), ("phone", _phone)):
                    if not _val:
                        _log(f"    {_key:<11} SKIP (no value in the profile)"); continue
                    if await _fill(page, _key, _val):
                        r["filled"].append("address" if _key == "addr1" else _key)
                        _log(f"    {_key:<11} = {str(_val)[:38]}")
                    else:
                        _log(f"    {_key:<11} NOT FOUND on this page")

            # #1 resume BEFORE the generic autofill (Workday's parse overwrites fields)
            if resume_path and os.path.exists(resume_path) and not any(
                    x.startswith("resume") for x in r["filled"]):
                await _upload_resume(page, resume_path, r)

            if autofill:
                try:
                    r["filled"] += (await autofill.fill_page(page, data))["filled"]
                except Exception as e:
                    r["note"] += f" autofill:{e};"
            # THE PHONE BLOCK RUNS LAST, DELIBERATELY. The generic autofill above fills by label and
            # ran AFTER the careful fill, so it overwrote `15785541545` with the raw international
            # value and put the same string in `Phone Extension` — visible in his screenshot, and the
            # reason the format error survived a fix that was already in the code. Order is the fix.
            try:
                await _phone_block(page, data, r)
            except Exception as e:
                _log(f"    phone block: {type(e).__name__}: {e}")
            await _check_consent(page)
            try:
                p = await _answer_prompts(page, data)
                if p:
                    r["filled"].append(f"prompt:{p}")
            except Exception as e:
                r["note"] += f" prompt:{e};"
            try:
                a = await _answer_choices(page, data, answer_fn)
                if a: r["filled"].append(f"answered:{a}")
            except Exception as e:
                r["note"] += f" answer:{e};"


            # advance
            if not await _click(page, "next", 4000):
                if not await _click_role(page, "button", r"save\s*and\s*continue|^\s*next\s*$"):
                    # NAME WHAT IS PRESENT. "not found" alone is unactionable, and it is what made a
                    # 183s stall undiagnosable. The visible buttons say immediately whether we are
                    # sitting on the apply CHOICE screen, an auth gate, or a real form page.
                    sn = await _snapshot(page)
                    btns = sn.get("buttons") or []
                    _log("STUCK: no Save-and-Continue / Next button on this page.")
                    _log(f"    buttons actually visible: {btns or '(none)'}")
                    if sn.get("reqEmpty"):
                        _log(f"    still required+empty   : {sn['reqEmpty']}")
                    _hint = ""
                    _bl = " ".join(btns).lower()
                    if "apply manually" in _bl or "autofill with resume" in _bl:
                        _hint = ("this is the apply CHOICE screen, not a form — the deep-link did not "
                                 "enter the wizard")
                    elif "sign in" in _bl or "create account" in _bl:
                        _hint = "this is the ACCOUNT GATE — sign in once in noVNC so the profile keeps the session"
                    elif not btns:
                        _hint = "no buttons visible at all — the page is probably still rendering"
                    if _hint:
                        _log(f"    diagnosis: {_hint}")
                    # RUNG 2: do not stop on a stall -- put it to the three vendors first. This
                    # is the SAME stall path presubmit.py deliberately stays off: rare, already
                    # terminal, and the alternative is a human. A modal blocking the form (a cookie
                    # wall, a "your session will expire" overlay) is the commonest cause and is
                    # exactly what the dialog-scoped index makes visible.
                    _esc = await _escalate(
                        page,
                        f"no Save-and-Continue / Next button on {(page.url or '')[:70]}"
                        + (f" — {_hint}" if _hint else ""),
                        r["filled"],
                        facts=_facts_for_panel(data),
                        tried=["clicked next / save-and-continue by selector and by role"])
                    if _esc.get("moved"):
                        _log("    the panel unblocked the page — continuing the application")
                        continue
                    r["note"] += (f" STUCK on {(page.url or '')[:60]} — no Save-and-Continue. "
                                  f"Visible buttons: {btns[:6]}." + (f" {_hint}." if _hint else ""))
                    await _notify("Workday stuck: no Save-and-Continue. Visible buttons: "
                                  f"{btns[:6]}." + (f" {_hint}." if _hint else ""))
                    break
            await page.wait_for_timeout(2500)

            # #4 assertion: a REPEATING validation error means we cannot fix it -> HALT + tell the human
            err = await _error_text(page)
            if err:
                print(f"[wd] validation error: {err}", flush=True)
                if err == last_err:
                    # HIS POINT, AND HE IS RIGHT: *"a fixable format error becomes a hard stop"*. The
                    # halt was correct given what had been TRIED, and what had been tried was
                    # incomplete — the site NAMED the two broken fields and nothing read the names.
                    # One repair attempt per distinct error, then the old behaviour exactly.
                    if err not in _repaired:
                        _repaired.add(err)
                        try:
                            if await _repair_validation(page, data, err, r):
                                _log("    repair attempted — trying Next again")
                                last_err = ""
                                continue
                        except Exception as e:
                            _log(f"    repair raised {type(e).__name__}: {e} — halting as before")
                    r["stage"] = "blocked"
                    r["note"] = f"Workday validation error we cannot resolve: '{err}'. Stopped for the human."
                    await _notify(r["note"])
                    return r
                last_err = err
            else:
                last_err = ""

        _log(f"page loop finished. filled={sorted(set(r['filled'])) or '[]'}")
        r["ok"] = True
        if r["stage"] != "blocked":
            r["stage"] = "paused"
        r["note"] = r["note"] or ("Advanced through the form (" + ", ".join(sorted(set(r["filled"]))) +
                                  "). Stopped before Submit — finish in noVNC.")
        return r
    except Exception as e:
        r["note"] = f"workday driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


def _selftest() -> int:
    r"""Selector hygiene, provable with no browser.

    WHY: `r"sign\s*in"` was UNANCHORED, so Playwright's `get_by_role(name=...)` matched
    "Sign in with Google" and `.first` took DOM order -- which navigated the driver to
    accounts.google.com and made every page reader look at the wrong site for 70 seconds. The rule is
    a PROPERTY, not a line: no click-by-role regex may match a third-party SSO button unless that is
    what the call site is FOR."""
    import re as _re
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    THIRD_PARTY = ["Sign in with Google", "Sign in with LinkedIn", "Sign in with Microsoft",
                   "Continue with Google", "Apply with LinkedIn", "Continue with LinkedIn"]
    src = open(__file__, encoding="utf-8").read()
    # SHIPPING CODE ONLY. Section [5] below names the banned string, so scanning the whole file
    # finds it in this very function -- the SEVENTH self-referential false positive in this project.
    ship = src[:src.index("def _selftest")]
    # STRIP BOTH COMMENT SYNTAXES. This file embeds JavaScript, so a `//` comment explaining a
    # REMOVED line still contains the banned string -- which is how section [5] failed against
    # correct code. Eighth self-referential false positive recorded in this project; the fix is
    # always the same and always cheaper than the guess.
    code = "\n".join(ln for ln in ship.splitlines()
                     if not ln.strip().startswith("#") and not ln.strip().startswith("//"))
    check("the scan is looking at real code, not an empty slice", len(code) > 20000, str(len(code)))

    print("\n[1] no click-by-role regex may reach a third-party SSO button by accident")
    offenders = []
    for m in _re.finditer(r'_click_role\(\s*page\s*,\s*"(?:button|link)"\s*,\s*r?"([^"]+)"', code):
        pat = m.group(1)
        if "google" in pat.lower() or "linkedin" in pat.lower():
            continue                     # deliberate: the call site exists to click exactly that
        try:
            rx = _re.compile(pat, _re.I)
        except _re.error:
            continue
        hit = [d for d in THIRD_PARTY if rx.search(d)]
        if hit:
            offenders.append((pat, hit))
    check("every non-Google regex is narrow enough", not offenders, str(offenders))

    print("\n[2] the sign-in click is anchored")
    check("_SIGNIN_ONLY exists", "_SIGNIN_ONLY" in code)
    rx = _re.compile(r"^\s*(sign\s*in|anmelden|einloggen)\s*$", _re.I)
    check("it matches 'Sign In'", bool(rx.match("Sign In")))
    check("it does NOT match 'Sign in with Google'", not rx.match("Sign in with Google"))
    check("it does NOT match 'Sign in with email'", not rx.match("Sign in with email"))

    print("\n[3] the panel is never consulted while we are on an identity provider")
    import ast as _ast
    t = _ast.parse(src)
    esc = next(n for n in _ast.walk(t)
               if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_escalate")
    seg = _ast.get_source_segment(src, esc)
    check("_escalate checks _on_idp before reading the page", "_on_idp(page.url" in seg)
    check("and leaves the IdP", "_leave_idp(" in seg)

    print("\n[4] every Playwright action is capped (an inherited 30s default is a 30s bet)")
    uncapped = []
    for m in _re.finditer(r"await el\.(click|fill|check)\(", code):
        i, depth = m.end(), 1
        while i < len(code) and depth:
            depth += (code[i] == "(") - (code[i] == ")")
            i += 1
        if "timeout" not in code[m.start():i]:
            uncapped.append(code[m.start():i][:40])
    check("no un-capped el.click/fill/check", not uncapped, str(uncapped))
    check("the page default is set at connect", "page.set_default_timeout(ACTION_TIMEOUT)" in code)

    print("\n[5] the aria-hidden rejection stays out of the visibility predicate")
    check("gone", 'closest(\'[aria-hidden="true"]\')' not in code)

    print("\n[6] ONE account store — the dual-keying open item, closed 2026-08-17")
    # THE PROPERTY: this adapter must never touch the credential file itself. Two writers with
    # different key styles (hostname here, vendor tenant in atscreds) is how a WORKING password became
    # invisible and a fresh one was generated over it, silently.
    # MEASURE THE SHIPPING SLICE ONLY. `"open(ACCOUNTS" not in src` matched THIS VERY ASSERTION,
    # because src is the whole file -- the thirteenth self-referential false positive in this project.
    # The selftest is not the subject; the code above it is.
    _ship = src[:src.index("def _selftest(")]
    _wcode = re.sub(r"#.*$", "", _ship, flags=re.M)
    _wcode = re.sub(r'\"\"\"(?:.|\n)*?\"\"\"', "", _wcode)
    check("the measured slice is the real module", len(_wcode) > 20000)
    check("the adapter never opens the credential file itself", "open(ACCOUNTS" not in _wcode)
    import ast as _aw
    _wt = _aw.parse(src)
    for _fn in ("_load_accounts", "_save_account"):
        _b = _aw.get_source_segment(src, next(n for n in _aw.walk(_wt)
                                    if isinstance(n, _aw.FunctionDef) and n.name == _fn))
        check(f"{_fn}() delegates to the single owner", "atscreds" in _b)

    # THE SITE NAMED THE BROKEN FIELDS AND NOTHING READ THE NAMES (intive, 2026-08-17). This is the
    # verbatim error string from that run. If it stops resolving to those two field names, the
    # error-driven repair silently degrades back to the hard stop it replaced.
    _real = ("Error: The field How Did You Hear About Us? is required and must have a value. | "
             "Error: Enter a valid format for Phone Number. | Error-How Did You Hear About Us? The "
             "field How Did You Hear About Us? is required and must have a value. Error-Phone Number "
             "Enter a valid format for Phone Number. |")
    _f = error_fields(_real)
    check("the site's own error names How Did You Hear About Us?",
          any(x.lower() == "how did you hear about us?" for x in _f))
    check("...and Phone Number", any(x.lower() == "phone number" for x in _f))
    check("no junk name long enough to match the wrong control", all(len(x) <= 44 for x in _f))
    check("nothing is invented from a clean page",
          error_fields("") == [] and error_fields("everything is fine") == [])
    check("the phone block sets the COUNTRY before the number",
          _wcode.index('"phone_code"') < _wcode.index('await _fill(page, "phone", natl)'))

    # THE GENERIC FILLER RAN AFTER THE CAREFUL FILL AND UNDID IT (intive, from his screenshot:
    # `Phone Number` AND `Phone Extension` both reading '+49 157 85541545'). Two properties, pinned:
    # a label matcher may not claim a phone-adjacent control, and the value it sends is the NATIONAL
    # number. Both are in autofill.py, which every ATS path uses, so they are asserted here where the
    # suite actually runs.
    try:
        import re as _re
        import autofill as _AF
        _rules = [(_re.compile(rx, _re.I), k) for rx, k in _AF.RULES]

        def _k(lbl):
            for rx, k in _rules:
                if rx.search(lbl):
                    return k
            return ""

        for _lbl, _want in (("Phone Number*", "phone"), ("Mobile Phone", "phone"),
                            ("Phone Extension", ""), ("Phone Ext", ""),
                            ("Country Phone Code*", ""), ("Phone Device Type*", "")):
            check(f"autofill routes {_lbl!r} -> {_want!r}", _k(_lbl) == _want)
        check("autofill sends the NATIONAL number, never '+49 …'",
              _AF.profile_values({"phone": "+49 15785541545"}).get("phone") == "15785541545")
        check("the phone block runs AFTER the generic autofill, so nothing can overwrite it",
              _wcode.index("autofill.fill_page") < _wcode.index("await _phone_block(page, data, r)"))
    except Exception as _e:
        check(f"autofill contracts runnable ({type(_e).__name__}: {_e})", False)

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} WORKDAY SELECTOR CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL WORKDAY SELECTOR CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    d = json.load(open(os.getenv("JHW_DATA", "/agent/templates/resume_data.json"), encoding="utf-8"))
    c = {"email": d["basics"]["email"], "password": "Test!Only1"}
    print(json.dumps(asyncio.run(drive(c, d, "/agent/out/resume.pdf")), indent=2))
