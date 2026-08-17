#!/usr/bin/env python3
"""ESCALATE — when the single driver model is stuck, THREE vendors decide in seconds.

THE LADDER, verbatim from the candidate:
  1. deepseek-3.2 fills things with stagehand
  2. if it is stuck it switches to 3 LLMs, they decide what to do, FAST     <- this module
  3. if they still cannot decide they ask me on telegram
  4. my answer is entered on the form
  5. we move forward -- and it is REMEMBERED (learned.py) so step 3 never repeats

WHY A PANEL HERE AND NOT PER ACTION. FOUR_MODEL_CONSENSUS.md §7.6 rule 2 is "do not put a model in
a request path, ever", and the fill loop IS one: ~30 DOM actions per application at 570-1004ms
each. A panel per action costs 4x the tokens, 2-4x the wall clock and 4x the 429 surface and buys
nothing, because a wrong click simply fails to advance the page and the repeat-breaker catches it.
A STALL is the opposite: it is rare, it is already terminal, and the only alternative is waking a
human at 3am. So the panel runs ONLY on the stall path.

GOVERNANCE (identical to presubmit.py, for the same reasons):
  * DETERMINISTIC CODE EXECUTES. The panel may only NAME a verb from a CLOSED set, and every
    target must already exist in the control index. A model can never invent a selector -- the
    guessed-Workday-selector lesson cost hours.
  * QUORUM, NOT A VOICE. >=2 of 3 must agree on the same (verb,target) or we ask the human.
    One bold model cannot drive the browser.
  * FAIL OPEN, DOWNWARD. Any exception / timeout / 429 -> ask the human, which is exactly what
    would have happened without this module. The panel can only ever REDUCE how often you are woken.
  * IT NEVER SEES OR TYPES A CREDENTIAL. No password reaches a prompt, and `fill` is refused on any
    password field. atscreds owns secrets; learned.py refuses to store them.
  * IT CANNOT NAVIGATE BACKWARDS. Clicking the Workday stepper ('My Information', 'Review') loses
    the page just filled -- already recorded as a hard rule for the generic driver.

MODELS by ROLE ALIAS so backend/app/llm.py stays the single home for model choice:
  jhw-answer  -> deepseek-3.2      (805ms measured on our real action-JSON task)
  jhw-answer2 -> mistral-3-14B     (570ms, DIFFERENT vendor)
  jhw-answer3 -> llama-4-maverick  (1004ms, DIFFERENT vendor again)
Three vendors = no shared failure domain: a provider-wide 429 cannot silence the panel.
"""
from __future__ import annotations
import asyncio, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
PANEL_MODELS = [m.strip() for m in os.getenv(
    "JHW_ESCALATE_MODELS", "jhw-answer,jhw-answer2,jhw-answer3").split(",") if m.strip()]
ENABLED  = os.getenv("JHW_ESCALATE", "1").lower() not in ("0", "false", "no")
TIMEOUT  = float(os.getenv("JHW_ESCALATE_TIMEOUT", "20"))
MAX_TOK  = int(os.getenv("JHW_ESCALATE_MAXTOK", "260"))
# A stall that the panel "fixed" and which stalls again on the SAME reason is not fixed. Cap it.
MAX_ROUNDS = int(os.getenv("JHW_ESCALATE_ROUNDS", "3"))

# ------------------------------------------------------------------ the CLOSED verb set
# Anything outside this set is refused before it can reach the browser. Growing it is a deliberate
# code change with a test, never something a model can talk its way into.
VERBS = {
    "click":          ("target",),           # `expect` (the label echo) is CHECKED when present
    "fill":           ("target", "value"),  # type into an indexed text field
    "select":         ("target", "value"),  # choose an option in an indexed <select>
    "check":          ("target",),          # tick an indexed checkbox/radio (idempotent: SET, never toggle)
    "scroll_dialog":  (),                   # the modal is taller than the viewport
    "dismiss_dialog": (),                   # close an informational overlay that is blocking the form
    "google_sso":     (),                   # use the persisted Google session (never types a password)
    "ask_human":      ("question",),        # the honest answer when they cannot agree
    "give_up":        ("reason",),
}
# Verbs that move the browser. Everything else is inert.
ACTING = {"click", "fill", "select", "check", "scroll_dialog", "dismiss_dialog", "google_sso"}

# Verbs the CALLER has proven cannot work in this run. Populated by the adapter (see workday.py's
# _GOOGLE_DEAD): a proposal naming one is dropped BEFORE votes are counted, so the vendors that
# proposed something viable can still form a quorum.
WITHDRAWN: dict = {}


def withdraw(verb: str, why: str) -> None:
    WITHDRAWN[str(verb).lower()] = why


def restore_all() -> None:
    WITHDRAWN.clear()

# CLAUDE.md, hard rule: the Workday stepper navigates BACKWARDS and loses the filled page.
# BACKWARDS NAVIGATION. Clicking a Workday PROGRESS-STEPPER entry loses the page just filled.
# MEASURED 2026-08-16: this used to be a LABEL match including "sign in", and it refused the panel's
# proposal to click the header 'Sign In' button on the job page -- which was the ONLY way forward at
# that moment. The stepper and the real controls SHARE LABELS, so the label cannot be the test.
# `stepper: true` comes from the DOM (`[data-automation-id*=progressBar]` and friends) and is what
# the refusal keys on now. Only words that are unambiguous in every context stay here.
_BACKWARDS_RX = re.compile(r"^\s*(back|zur(ü|ue)ck|previous|go back)\s*$", re.I)
# These are stepper entries when they ARE in the stepper, and legitimate controls when they are not.
_STEPPER_LABEL_RX = re.compile(
    r"^\s*(my information|my experience|application questions|voluntary disclosures|review|"
    r"sign up|create account/?sign in)\s*$", re.I)
# Never type into these, whatever the panel says.
_SECRET_FIELD_RX = re.compile(r"password|passwort|verify.?password|secret|token|api.?key", re.I)


def _log(m: str) -> None:
    print(f"[escalate] {m}", flush=True)


# ------------------------------------------------------------------ describing the page
def describe(controls: list, dialog: dict | None = None, limit: int = 40) -> str:
    """Render the control index the panel is allowed to choose from.

    The INDEX is the contract: a model answers with a number that already exists, so it is
    structurally incapable of inventing a selector."""
    lines = []
    if dialog and dialog.get("present"):
        lines.append(f'A MODAL DIALOG IS OPEN and it owns the page: "{(dialog.get("title") or "")[:80]}"')
        if dialog.get("text"):
            lines.append(f'  dialog text: {(dialog.get("text") or "")[:400]}')
        if dialog.get("scrollable"):
            lines.append("  the dialog SCROLLS — controls may exist below the fold "
                         "(verb scroll_dialog reveals them)")
        lines.append("  Every control listed below is INSIDE that dialog. The page behind it is inert.")
    for c in controls[:limit]:
        bits = [f"[{c.get('i')}]", str(c.get("kind") or "?"), json.dumps(str(c.get("label") or "")[:60])]
        if c.get("value"):
            bits.append(f"value={json.dumps(str(c['value'])[:40])}")
        elif c.get("kind") in ("input", "textarea", "select"):
            bits.append("EMPTY")
        if c.get("required"):
            bits.append("REQUIRED")
        if c.get("checked") is True:
            bits.append("CHECKED")
        if c.get("options"):
            bits.append("options=" + json.dumps([str(o)[:24] for o in c["options"]][:8]))
        lines.append("  " + " ".join(bits))
    if not controls:
        lines.append("  (no interactive control is visible — the page may still be rendering)")
    return "\n".join(lines)


# ------------------------------------------------------------------ validation (PURE)
def validate(act: dict, controls: list) -> tuple:
    """(ok, why). PURE, so it is testable with no browser and no models.

    This is the layer that makes a model safe to listen to. It is deliberately paranoid: every
    failure mode below has already cost this project real time on a real ATS."""
    if not isinstance(act, dict):
        return False, "not an object"
    verb = str(act.get("verb") or "").strip().lower()
    if verb not in VERBS:
        return False, f"verb {verb!r} is not in the allowed set"
    # A VERB CAN BE WITHDRAWN FOR THE REST OF A RUN. Measured: after bouncing off the Google account
    # chooser, `google_sso` was still proposed every round -- and because it was merely UNWANTED
    # rather than INVALID it split the vote and produced "no quorum", waking the human instead of
    # letting the remaining two vendors agree on something that works.
    if verb in WITHDRAWN:
        return False, f"verb {verb!r} was withdrawn for this run: {WITHDRAWN[verb]}"
    for field in VERBS[verb]:
        if not str(act.get(field) or "").strip() and act.get(field) != 0:
            return False, f"verb {verb!r} needs {field!r}"
    if verb not in ("click", "fill", "select", "check"):
        return True, "ok"

    # every acting verb must name a control that ALREADY EXISTS
    try:
        idx = int(act.get("target"))
    except Exception:
        return False, f"target {act.get('target')!r} is not an index"
    hit = next((c for c in controls if int(c.get("i", -1)) == idx), None)
    if hit is None:
        return False, f"target [{idx}] is not on the page (invented)"
    label = str(hit.get("label") or "")
    kind = str(hit.get("kind") or "")

    if hit.get("honeypot"):
        return False, f"[{idx}] {label!r} is a BOT TRAP — filling it discards the application"
    # FAIL CLOSED ON AN UNKNOWN. The stagehand and vision readers hardcoded honeypot=False, so a
    # trap surfaced by either was allowed to be FILLED -- and filling Accenture's `beecatcher`
    # silently discards the application with nothing in the log. A reader that cannot judge must not
    # be taken as judging "safe".
    if verb == "fill" and "honeypot" not in hit and re.search(
            r"robots only|beecatcher|enter website", label, re.I):
        return False, f"[{idx}] {label!r} looks like a BOT TRAP and the reader did not check"
    if verb == "click":
        if _BACKWARDS_RX.match(label.strip()):
            return False, f"clicking {label!r} always goes BACKWARDS"
        # STRUCTURE decides: a control INSIDE the progress stepper navigates backwards, whatever it
        # is called. A control with the same words outside it may be the only way forward.
        if hit.get("stepper"):
            return False, (f"[{idx}] {label!r} is a PROGRESS-STEPPER entry — clicking it navigates "
                           f"backwards and loses the filled page")
        # A reader that cannot report structure (vision, stagehand) falls back to the label, but only
        # for wizard-step names that are never a forward control.
        if hit.get("stepper") is None and _STEPPER_LABEL_RX.match(label.strip()):
            return False, f"{label!r} is a wizard step name — clicking it navigates backwards"
    # THE BINDING CHECK. MEASURED 2026-08-16 on the live Accenture gate: all three vendors reasoned
    # correctly about Google SSO ("Try Google pre-authenticated login") and all named index 0 --
    # and index 0 was 'myworkday.com', the Workday branding link in the FOOTER. The driver left the
    # ATS entirely. The models' INTENT was right; nothing verified that the INDEX they named was the
    # thing they meant. So a click must now ECHO the label it believes it is pressing, and
    # deterministic code refuses the action when the echo does not match the index.
    # A THIRD-PARTY SSO BUTTON IS NOT A WAY IN. Withdrawing the `google_sso` verb is not enough:
    # measured, the panel simply proposed `click` on the same button instead. If SSO is withdrawn,
    # clicking the control that starts it is withdrawn too.
    if verb == "click" and "google_sso" in WITHDRAWN and re.search(
            r"\b(google|microsoft|linkedin|apple|facebook)\b", label, re.I):
        return False, (f"[{idx}] {label!r} starts a third-party sign-in, which is withdrawn for this "
                       f"run — the ATS-local account is the goal")
    if verb in ("click", "fill", "select", "check"):
        exp = str(act.get("expect") or "").strip()
        if exp and label and not _label_matches(exp, label):
            return False, (f"it said it was acting on {exp!r} but [{idx}] is {label!r} — the index "
                           f"does not match its own stated intent")
        # `expect` was made MANDATORY for a click after the footer-link incident. Measured over three
        # live runs: the models omit it most of the time, so EVERY click was refused and the panel
        # could not act at all -- a guard that blocks the only useful verb is worse than the risk it
        # covers, especially now the index is modal-scoped and chrome-filtered. So it VERIFIES when
        # given and merely warns when absent.
        if verb == "click" and not exp and label:
            act["_unverified"] = True
    if verb == "fill":
        if kind not in ("input", "textarea"):
            return False, f"[{idx}] is a {kind}, not a text field"
        if _SECRET_FIELD_RX.search(label) or hit.get("secret"):
            return False, f"[{idx}] {label!r} is a credential field — the vault fills those, not the panel"
    if verb == "select":
        if kind != "select":
            return False, f"[{idx}] is a {kind}, not a dropdown"
        opts = [str(o) for o in (hit.get("options") or [])]
        want = str(act.get("value") or "")
        if opts and not any(want.strip().lower() == o.strip().lower() for o in opts):
            near = [o for o in opts if want.strip().lower() in o.strip().lower()]
            if not near:
                return False, f"{want!r} is not one of this dropdown's options"
            act["value"] = near[0]           # snap to the site's own wording
    if verb == "check" and kind not in ("checkbox", "radio"):
        return False, f"[{idx}] is a {kind}, not a checkbox"
    if verb == "check" and hit.get("checked") is True:
        return False, f"[{idx}] is ALREADY ticked — clicking it again would UNTICK it"
    return True, "ok"


# ------------------------------------------------------------------ quorum (PURE)
def _norm_label(s: str) -> str:
    """Hyphens, dots and apostrophes are DELETED, not turned into spaces, so "e-mail" == "email"
    and "Inc." == "Inc". Everything else non-alphanumeric becomes a space."""
    t = re.sub(r"[-_.'\u2019]", "", (s or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def _label_matches(expected: str, actual: str) -> bool:
    """Does the label the model echoed describe the control at that index? PURE.

    Deliberately tolerant of wording ("Sign in with Google" vs "sign in with google account") and
    intolerant of a different control ('Sign in with Google' vs 'myworkday.com'). Tolerance matters:
    an over-strict match refuses correct actions, which pushes the run to the human -- the outcome
    this whole module exists to avoid."""
    a, b = _norm_label(expected).strip(), _norm_label(actual).strip()
    if not a or not b:
        return True                              # nothing to compare against
    if a == b:
        return True
    ta = [w for w in a.split() if len(w) >= 3]
    tb = [w for w in b.split() if len(w) >= 3]
    # A ONE-WORD ECHO IS INHERENTLY AMBIGUOUS and must match exactly. Audited failures:
    # "Submit" vs "Submit Another Application", "Next" vs "Next Steps", "Delete" vs
    # "Delete resume.pdf" -- every one a real control pair, every one previously ACCEPTED.
    if len(ta) < 2:
        return False
    # A multi-word echo may be missing trailing words the model did not read ("Sign in with Google"
    # vs "Sign in with Google account"), but every distinctive word it DID give must be present and
    # the page's label may not add more than two.
    # CONTAINMENT IN EITHER DIRECTION, because the mismatch can go both ways: the model may read a
    # truncated button ("Sign in with Google" for "Sign in with Google account") or describe it more
    # fully than the page does. What is NOT allowed is a DIFFERENT distinctive word -- "email"
    # against "google" fails both directions, which is the case this guard exists for.
    if all(w in tb for w in ta) and len([w for w in tb if w not in ta]) <= 2:
        return True
    if all(w in ta for w in tb) and len([w for w in ta if w not in tb]) <= 2:
        return True
    return False
    # AUDITED 2026-08-16 and it FAILED THE CASE IT EXISTS FOR: a token-intersection fallback
    # accepted `expect="Sign in with email"` against label `"Sign in with Google"` (they share
    # {sign, with}), and `"Save and Continue"` against `"Save for Later"` (they share {save}). The
    # guard was built to stop "right intent, wrong index" and would have waved through exactly that.
    # ONE-DIRECTIONAL CONTAINMENT ONLY, and never the other way round: `expect` may be SHORTER than
    # the page's label (a model reading a truncated button), but it must not name a DIFFERENT
    # control. "submit" in "submit another application" is still wrong, so containment also requires
    # the label to start with it.



_DATEISH = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|"
                     r"as soon as possible|asap|immediately)\s*$", re.I)


def _our_date() -> str:
    """OUR start date, from his notice period. Never a model's invention, never in the past."""
    try:
        from workday_questions import iso_start, screening_defaults
        return iso_start(screening_defaults())
    except Exception:
        import datetime as _d
        return (_d.date.today() + _d.timedelta(days=30)).isoformat()


def _looks_like_date(v: str) -> bool:
    """Is this value a date (or a hand-wave meaning 'a date')? PURE."""
    return bool(_DATEISH.match(str(v or "")))


def _key(act: dict) -> str:
    """The identity of a proposed action, for vote counting.

    MEASURED 2026-08-16: two vendors both said `google_sso`, one with `target:3` and one without, and
    this keyed them as DIFFERENT actions -- so a real 2/3 agreement was reported as
    `NO QUORUM (no majority (google_sso[3]x1, google_sso[None]x1))` and the human was woken. A verb
    that takes NO arguments must be keyed on the verb ALONE; anything else the model volunteers is
    noise, not identity."""
    verb = str(act.get("verb") or "").lower()
    if not VERBS.get(verb):                       # verb takes no arguments (google_sso, scroll, ...)
        return verb
    tgt = str(act.get("target") or "")
    val = re.sub(r"\s+", " ", str(act.get("value") or "")).strip().lower()
    # A DATE IS A FACT WE OWN, SO ITS VALUE IS NOT PART OF THE IDENTITY.
    # MEASURED, intive 2026-08-17, four rounds in a row:
    #     jhw-answer  -> fill[0] '2024-12-01'
    #     jhw-answer2 -> fill[0] '2024-06-01'
    #     jhw-answer3 -> fill[0] '2024-09-20'
    #     NO QUORUM (no majority (fill[0]x1, fill[0]x1, fill[0]x1))
    # All three agreed on the VERB and the FIELD and differed only in a date they invented — every one
    # of them in the PAST, which the form rejects anyway. Keying on the value made a unanimous 3/3
    # agreement about WHICH FIELD read as total disagreement, and woke the human four times.
    # The models decide the field; deterministic code supplies the value (see `_date_value`).
    if verb == "fill" and _looks_like_date(val):
        return f"{verb}|{tgt}|<date>"
    return f"{verb}|{tgt}|{val}" if verb in ("fill", "select") else f"{verb}|{tgt}"


def quorum(props: list, need: int = 2) -> dict:
    """>=`need` reviewers proposing the SAME (verb,target[,value]) wins. PURE.

    A single voice is never enough: this is the whole difference between a panel and one model with
    three hats. On a tie the earlier-listed action wins only if it has the votes; otherwise the
    honest answer is that they did not agree."""
    votes: dict = {}
    for p in props:
        if not isinstance(p, dict) or not p.get("verb"):
            continue
        k = _key(p)
        e = votes.setdefault(k, {"n": 0, "act": p, "models": []})
        e["n"] += 1
        e["models"].append(p.get("model") or "?")
    if not votes:
        return {"agreed": False, "votes": 0, "why": "no usable proposal"}
    best = max(votes.values(), key=lambda e: e["n"])
    if best["n"] < need:
        spread = ", ".join(f'{v["act"].get("verb")}[{v["act"].get("target")}]x{v["n"]}'
                           for v in votes.values())
        return {"agreed": False, "votes": best["n"], "why": f"no majority ({spread})"}
    act = dict(best["act"])
    if str(act.get("verb")).lower() == "fill" and _looks_like_date(act.get("value")):
        act["value"] = _our_date()
        act["value_from"] = "candidate.md notice period (the models only chose the field)"
    act["votes"] = best["n"]
    act["models"] = best["models"]
    return {"agreed": True, "votes": best["n"], "act": act, "why": "quorum"}


# ------------------------------------------------------------------ the panel
_SYS = (
    "You are one of THREE independent reviewers unblocking an automated job application that has "
    "STALLED. You are not chatting; you emit ONE action.\n"
    "RULES:\n"
    "1. Choose a target ONLY by the [index] shown. Never invent a selector, an id or a label.\n"
    "2. Allowed verbs: click, fill, select, check, scroll_dialog, dismiss_dialog, google_sso, "
    "ask_human, give_up. Nothing else exists.\n"
    "3. If a MODAL DIALOG is open it owns the page: act inside it, or dismiss it, or scroll it. "
    "Ignore controls belonging to the page behind it.\n"
    "4. Never fill a password or any credential field. Never click a progress-stepper entry "
    "(My Information, Review, Back) — that navigates backwards and destroys work.\n"
    "5. Prefer the smallest action that makes the page move forward.\n"
    "6. If the page genuinely needs a fact only the candidate knows, answer with ask_human and put "
    "ONE short question in `question`.\n"
    "7. For click/fill/select/check you MUST also return `expect` = the exact label text shown for "
    "that [index]. It is checked against the page: if it does not match, your action is DISCARDED. "
    "This exists because a panel once reasoned correctly about a Google button and named an index "
    "that was a footer link.\n"
    'Answer STRICT JSON and nothing else: {"verb":"...","target":<index>,"expect":"<label at that '
    'index>","value":"...","question":"...","reason":"<12 words>"}. Omit fields the verb does not '
    "need.")


async def _ask_model(model: str, prompt: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}",
                                  "Content-Type": "application/json"},
                         json={"model": model,
                               "messages": [{"role": "system", "content": _SYS},
                                            {"role": "user", "content": prompt}],
                               "temperature": 0, "max_tokens": MAX_TOK})
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        raise ValueError(f"{model} did not return JSON: {str(txt)[:80]!r}")
    d = json.loads(m.group(0))
    out = {"model": model, "verb": str(d.get("verb") or "").strip().lower(),
           "reason": str(d.get("reason") or "")[:90]}
    for k in ("target", "value", "question"):
        if d.get(k) not in (None, ""):
            out[k] = d[k]
    return out


def build_prompt(reason: str, controls: list, dialog: dict | None,
                 url: str = "", step: str = "", errors: list | None = None,
                 facts: str = "", tried: list | None = None) -> str:
    parts = [f"THE STALL: {reason}"]
    if url:
        parts.append(f"URL: {url[:140]}")
    if step:
        parts.append(f"WIZARD STEP: {step}")
    if errors:
        parts.append("THE PAGE'S OWN ERRORS:\n" + "\n".join(f"  - {str(e)[:140]}" for e in errors[:6]))
    if tried:
        parts.append("ALREADY TRIED AND IT DID NOT WORK (do not repeat):\n"
                     + "\n".join(f"  - {str(t)[:110]}" for t in tried[:8]))
    parts.append("WHAT IS ON SCREEN:\n" + describe(controls, dialog))
    if facts:
        parts.append("FACTS ABOUT THE CANDIDATE (authoritative — use these instead of asking):\n"
                     + facts[:1800])
    parts.append("Emit ONE action as strict JSON.")
    return "\n\n".join(parts)


async def panel(prompt: str, models: list | None = None) -> list:
    """All three vendors in PARALLEL. ~1-2s total, not 3x one model.
    Every failure returns fewer proposals, never an exception -- the caller must still work with
    one answer or none."""
    ms = models or PANEL_MODELS
    res = await asyncio.gather(*[_ask_model(m, prompt) for m in ms], return_exceptions=True)
    out = []
    for m, x in zip(ms, res):
        if isinstance(x, dict):
            out.append(x)
            _log(f"  {m:12s} -> {x.get('verb')}"
                 + (f"[{x.get('target')}]" if x.get("target") not in (None, "") else "")
                 + (f" {str(x.get('value'))[:28]!r}" if x.get("value") else "")
                 + f"   ({x.get('reason','')})")
        else:
            _log(f"  {m:12s} -> did not answer ({type(x).__name__}: {str(x)[:60]})")
    return out


async def next_action(reason: str, controls: list, dialog: dict | None = None, **kw) -> dict:
    """THE RUNG. Returns a VALIDATED action, or an ask_human fallback.

    Never raises. Never returns something the browser should not do."""
    if not ENABLED:
        return {"verb": "ask_human", "question": reason, "why": "panel disabled (JHW_ESCALATE=0)"}
    t0 = time.time()
    prompt = build_prompt(reason, controls, dialog, **kw)
    try:
        props = await panel(prompt)
    except Exception as e:                      # defence in depth; panel() already swallows
        _log(f"panel unavailable ({type(e).__name__}) — falling through to you")
        return {"verb": "ask_human", "question": reason, "why": f"panel error {type(e).__name__}"}

    # Drop proposals that would be unsafe BEFORE counting votes: a bad action must not win a
    # majority and then be refused, because that reads as "the panel agreed and we ignored it".
    ok_props, refused = [], []   # refused is read by the honest-failure message below
    for p in props:
        good, why = validate(p, controls)
        if good:
            ok_props.append(p)
        else:
            refused.append(f"{p.get('model')}: {why}")
    for r in refused:
        _log(f"  REFUSED {r}")

    q = quorum(ok_props)
    dt = (time.time() - t0) * 1000
    # AN HONEST FAILURE MESSAGE. It used to say "nobody answered" when two of three HAD answered and
    # both had been refused -- which sends the next investigation after a network problem that does
    # not exist. Report what actually happened.
    if not q.get("agreed") and not ok_props:
        q["why"] = (f"{len(props)} of {len(PANEL_MODELS)} answered, and every proposal was refused: "
                    + "; ".join(refused)) if props else \
                   f"none of {len(PANEL_MODELS)} vendors answered (429 / timeout / unreachable)"
    if q.get("agreed"):
        act = q["act"]
        act["why"] = f"{act['votes']}/{len(PANEL_MODELS)} vendors agreed in {dt:.0f}ms"
        _log(f"QUORUM {act['votes']}/{len(PANEL_MODELS)} ({','.join(act.get('models') or [])}) "
             f"-> {act.get('verb')}"
             + (f"[{act.get('target')}]" if act.get("target") not in (None, "") else "")
             + f"   {dt:.0f}ms")
        return act
    # No majority. If they all want to ask you, that IS the answer -- use their question.
    asks = [p for p in ok_props if p.get("verb") == "ask_human" and p.get("question")]
    if asks:
        return {"verb": "ask_human", "question": str(asks[0]["question"])[:400],
                "why": f"the panel could not act ({q.get('why')}) and asks you this"}
    _log(f"NO QUORUM ({q.get('why')}) after {dt:.0f}ms — asking you")
    return {"verb": "ask_human", "question": reason, "why": q.get("why") or "no quorum"}


# ------------------------------------------------------------------ self-test
def _selftest() -> int:
    fails = []

    def check(name, cond, extra=""):
        print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
        if not cond:
            fails.append(name)

    CTRL = [
        {"i": 0, "kind": "button", "label": "Sign in with Google"},
        {"i": 1, "kind": "button", "label": "Sign in with email"},
        {"i": 2, "kind": "input", "label": "Email Address", "value": "", "required": True},
        {"i": 3, "kind": "input", "label": "Password", "value": "", "required": True},
        {"i": 4, "kind": "button", "label": "My Information"},
        {"i": 5, "kind": "select", "label": "Country", "options": ["Germany", "Austria"]},
        {"i": 6, "kind": "checkbox", "label": "I agree to the terms", "checked": False},
        {"i": 7, "kind": "checkbox", "label": "Consent", "checked": True},
        {"i": 8, "kind": "input", "label": "Enter website", "honeypot": True},
    ]
    print("\n[1] the closed verb set")
    check("an unknown verb is refused", not validate({"verb": "navigate", "target": 0}, CTRL)[0])
    check("a verb missing its argument is refused", not validate({"verb": "fill", "target": 2}, CTRL)[0])
    check("click on a real control is allowed",
          validate({"verb": "click", "target": 1, "expect": "Sign in with email"}, CTRL)[0])

    print("\n[2] a model can never invent a target")
    check("an index that is not on the page is refused",
          not validate({"verb": "click", "target": 99, "expect": "whatever"}, CTRL)[0])
    check("a non-numeric target is refused",
          not validate({"verb": "click", "target": "the sign in button", "expect": "x"}, CTRL)[0])

    print("\n[3] the hard safety rules")
    ok, why = validate({"verb": "fill", "target": 3, "value": "hunter2"}, CTRL)
    check("filling a PASSWORD field is refused", not ok, why)
    ok, why = validate({"verb": "click", "target": 4, "expect": "My Information"}, CTRL)
    check("a wizard-step LABEL is refused when structure is unknown", not ok, why)
    # MEASURED 2026-08-16: the panel proposed clicking the header 'Sign In' on the job page -- the
    # only way forward -- and the old label-based rule refused it. Structure decides now.
    HDR = [{"i": 0, "kind": "button", "label": "Sign In", "stepper": False},
           {"i": 1, "kind": "button", "label": "Sign In", "stepper": True},
           {"i": 2, "kind": "button", "label": "Save and Continue", "stepper": False}]
    ok, why = validate({"verb": "click", "target": 0, "expect": "Sign In"}, HDR)
    check("the header 'Sign In' (NOT in the stepper) is ALLOWED", ok, why)
    ok, why = validate({"verb": "click", "target": 1, "expect": "Sign In"}, HDR)
    check("the stepper 'Sign In' entry is REFUSED", not ok, why)
    check("'Back' is refused whatever the structure says",
          not validate({"verb": "click", "target": 0, "expect": "Sign In"},
                       [{"i": 0, "kind": "button", "label": "Back", "stepper": False}])[0])
    check("'Save and Continue' is always allowed",
          validate({"verb": "click", "target": 2, "expect": "Save and Continue"}, HDR)[0])
    ok, why = validate({"verb": "fill", "target": 8, "value": "x"}, CTRL)
    check("filling the HONEYPOT is refused", not ok, why)
    ok, why = validate({"verb": "check", "target": 7}, CTRL)
    check("ticking an ALREADY-ticked box is refused (it would untick)", not ok, why)
    check("ticking an unticked box is allowed", validate({"verb": "check", "target": 6}, CTRL)[0])
    check("fill on a button is refused", not validate({"verb": "fill", "target": 1, "value": "x"}, CTRL)[0])
    check("select on an input is refused", not validate({"verb": "select", "target": 2, "value": "x"}, CTRL)[0])

    print("\n[3b] THE LIVE DEFECT, 2026-08-16: right intent, wrong index")
    # All three vendors reasoned about Google SSO and all named index 0. Index 0 was the Workday
    # FOOTER link, so the driver left the ATS. The intent was right; the binding was unchecked.
    LIVE = [{"i": 0, "kind": "link", "label": "myworkday.com"},
            {"i": 1, "kind": "button", "label": "Sign in with Google"},
            {"i": 2, "kind": "button", "label": "Sign in with email"}]
    ok, why = validate({"verb": "click", "target": 0, "expect": "Sign in with Google",
                        "reason": "Try Google pre-authenticated login"}, LIVE)
    check("a click whose echoed label does NOT match the index is refused", not ok, why)
    ok, why = validate({"verb": "click", "target": 1, "expect": "Sign in with Google"}, LIVE)
    check("the SAME intent aimed at the right index is allowed", ok, why)
    # DOCTRINE CHANGED 2026-08-16, on evidence, and the old assertion is kept here as a comment so
    # the reason is not lost: `expect` USED to be mandatory for a click. Measured over three live
    # runs, the models omit it most of the time, so EVERY click was refused
    # ("REFUSED jhw-answer3: verb 'click' needs 'expect'") and the panel could not act at all. A
    # guard that blocks the only useful verb costs more than the risk it covers -- especially now the
    # index is modal-scoped and chrome-filtered, which is what actually caused the footer-link
    # incident. So it VERIFIES when present and flags `_unverified` when absent.
    act = {"verb": "click", "target": 0, "reason": "Google"}
    ok, why = validate(act, LIVE)
    check("a click with no echoed label is ALLOWED but marked unverified",
          ok and act.get("_unverified") is True, f"ok={ok} act={act}")
    check("and a WRONG echo is still refused (the guard that matters)",
          not validate({"verb": "click", "target": 0,
                        "expect": "Sign in with Google"}, LIVE)[0])
    check("tolerant of wording: 'sign in with google account' still matches",
          validate({"verb": "click", "target": 1,
                    "expect": "sign in with google account"}, LIVE)[0])
    check("_label_matches rejects a different control",
          not _label_matches("Sign in with Google", "myworkday.com"))
    # THE CASES A SUBAGENT AUDIT FOUND THIS GUARD ACCEPTING. Every one is a real control pair from
    # a Workday gate or form, and every one would have been executed.
    for exp, act in (("Sign in with email", "Sign in with Google"),
                     ("Sign in with Google", "Sign in with email"),
                     ("Save and Continue", "Continue with Google"),
                     ("Save and Continue", "Save for Later"),
                     ("Submit", "Submit Another Application"),
                     ("Next", "Next Steps"),
                     ("Delete", "Delete resume.pdf")):
        check(f"{exp!r} must NOT match {act!r}", not _label_matches(exp, act))
    for exp, act in (("Sign in with Google", "Sign in with Google account"),
                     ("Save and Continue", "  save and continue  "),
                     ("Sign in with e-mail", "Sign in with email"),
                     ("I Agree", "I Agree")):
        check(f"{exp!r} still matches {act!r}", _label_matches(exp, act))
    check("_label_matches accepts the same control worded differently",
          _label_matches("Sign in with e-mail", "Sign in with email"))
    check("an empty label cannot be checked, so it is not refused on that basis",
          _label_matches("anything", ""))

    print("\n[4] select snaps to the site's own wording")
    a = {"verb": "select", "target": 5, "value": "german"}
    ok, _ = validate(a, CTRL)
    check("a case/substring match is accepted and normalised", ok and a["value"] == "Germany", a.get("value"))
    check("an option that does not exist is refused",
          not validate({"verb": "select", "target": 5, "value": "Uzbekistan"}, CTRL)[0])

    print("\n[5] quorum needs TWO vendors, not one")
    p1 = {"model": "a", "verb": "click", "target": 1, "expect": "Sign in with email"}
    p2 = {"model": "b", "verb": "click", "target": 1, "expect": "Sign in with email"}
    p3 = {"model": "c", "verb": "click", "target": 0, "expect": "Sign in with Google"}
    q = quorum([p1, p2, p3])
    check("2 of 3 agreeing wins", q["agreed"] and q["act"]["target"] == 1 and q["votes"] == 2)
    check("the winning action records WHO voted", set(q["act"]["models"]) == {"a", "b"})
    q = quorum([p1, p3, {"model": "c2", "verb": "dismiss_dialog"}])
    check("three different answers = NO quorum", not q["agreed"], q.get("why"))
    check("one lone voice is never enough", not quorum([p1])["agreed"])
    check("nobody answering is not agreement", not quorum([])["agreed"])
    q = quorum([{"model": "a", "verb": "fill", "target": 2, "value": "x@y.z"},
                {"model": "b", "verb": "fill", "target": 2, "value": "X@Y.Z"}])
    check("fill agreement is case-insensitive on the value", q["agreed"])
    q = quorum([{"model": "a", "verb": "fill", "target": 2, "value": "x@y.z"},
                {"model": "b", "verb": "fill", "target": 2, "value": "someone else"}])
    check("same field, DIFFERENT value is not agreement", not q["agreed"])

    print("\n[6] the description is what makes indices the contract")
    d = describe(CTRL, {"present": True, "title": "Sign In", "text": "You may choose to sign in…",
                        "scrollable": True})
    check("the modal is announced first", "MODAL DIALOG IS OPEN" in d)
    check("the dialog title is shown", "Sign In" in d)
    check("scrollability is announced", "scroll_dialog" in d)
    check("every control is indexed", all(f"[{i}]" in d for i in range(9)))
    check("the honeypot is not hidden from the panel", "Enter website" in d)
    check("an empty required field is marked", "REQUIRED" in d and "EMPTY" in d)
    check("options are listed for a dropdown", "Germany" in d)

    print("\n[7] a stalled page with nothing on it is described honestly")
    check("no controls is stated, not implied", "no interactive control" in describe([], None))

    print("\n[8] FAIL OPEN, DOWNWARD — a dead panel wakes the human, it never guesses")
    async def _dead():
        import escalate as E
        old = E.PANEL_MODELS
        E.PANEL_MODELS = ["nope-1", "nope-2", "nope-3"]
        E.PROXY = "http://127.0.0.1:9/v1"          # nothing listens; connect fails fast
        try:
            return await E.next_action("stuck at the login", CTRL, None)
        finally:
            E.PANEL_MODELS = old
    act = asyncio.get_event_loop().run_until_complete(_dead()) if sys.version_info < (3, 10) \
        else asyncio.run(_dead())
    check("an unreachable panel falls through to ask_human", act.get("verb") == "ask_human", act)
    check("and it says why", bool(act.get("why")))

    print("\n[9] the panel's own proposals are validated BEFORE they can win a vote")
    async def _unsafe():
        import escalate as E
        async def fake(prompt, models=None):
            return [{"model": "a", "verb": "fill", "target": 3, "value": "hunter2"},
                    {"model": "b", "verb": "fill", "target": 3, "value": "hunter2"},
                    {"model": "c", "verb": "click", "target": 1,
                     "expect": "Sign in with email"}]
        old = E.panel
        E.panel = fake
        try:
            return await E.next_action("stuck", CTRL, None)
        finally:
            E.panel = old
    act = asyncio.run(_unsafe())
    check("2 vendors agreeing on an UNSAFE action does NOT execute it",
          act.get("verb") != "fill", act)
    check("it degrades to asking the human instead", act.get("verb") == "ask_human", act)

    print("\n[9b] the failure message must say what actually happened")
    async def _all_refused():
        import escalate as E
        async def fake(prompt, models=None):
            return [{"model": "a", "verb": "click", "target": 0, "expect": "Sign in with Google"},
                    {"model": "b", "verb": "click", "target": 0, "expect": "Sign in with Google"}]
        old = E.panel
        E.panel = fake
        try:
            return await E.next_action("stuck", [{"i": 0, "kind": "link",
                                                  "label": "myworkday.com"}], None)
        finally:
            E.panel = old
    act = asyncio.run(_all_refused())
    check("'nobody answered' is NOT claimed when they did answer",
          "answered" in (act.get("why") or "") and "every proposal was refused" in (act.get("why") or ""),
          act.get("why"))

    print("\n[10] a safe majority DOES execute")
    async def _safe():
        import escalate as E
        async def fake(prompt, models=None):
            return [{"model": "a", "verb": "click", "target": 1, "expect": "Sign in with email",
                     "reason": "reveal the email form"},
                    {"model": "b", "verb": "click", "target": 1, "expect": "sign in with e-mail",
                     "reason": "email path"},
                    {"model": "c", "verb": "dismiss_dialog"}]
        old = E.panel
        E.panel = fake
        try:
            return await E.next_action("stuck", CTRL, None)
        finally:
            E.panel = old
    act = asyncio.run(_safe())
    check("the majority action is returned", act.get("verb") == "click" and int(act["target"]) == 1, act)
    check("with the vote count recorded", act.get("votes") == 2)

    print("\n[11] when they cannot act they hand YOU their own question")
    async def _asks():
        import escalate as E
        async def fake(prompt, models=None):
            return [{"model": "a", "verb": "ask_human", "question": "Which email do you use here?"},
                    {"model": "b", "verb": "click", "target": 0, "expect": "Sign in with Google"},
                    {"model": "c", "verb": "scroll_dialog"}]
        old = E.panel
        E.panel = fake
        try:
            return await E.next_action("stuck", CTRL, None)
        finally:
            E.panel = old
    act = asyncio.run(_asks())
    check("their question is used, not the raw stall text",
          act.get("verb") == "ask_human" and "email" in (act.get("question") or "").lower(), act)

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} ESCALATION CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL ESCALATION CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
