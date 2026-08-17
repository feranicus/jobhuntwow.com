#!/usr/bin/env python3
"""RECORDINGS -> KNOWLEDGE. What the candidate did by hand, compiled into facts the engine obeys.

WHY THIS EXISTS, in the operator's words: *"we were working two and a half days on something that it
took me like a minute to record."* He is right, and the reason is not that recording is magic. It is
that a recording is GROUND TRUTH about three things our engine was guessing at:

  1. THE REAL ACCESSIBLE NAMES. `Resume/CV*`, `Location (City)`, `How Did You Hear About Us?`,
     `Land Select One`, `Submit application`. Every hour lost at the Accenture gate was spent
     inventing names; the recording simply contains them.
  2. THE VALUE A FIELD WILL ACTUALLY ACCEPT. The Accenture recording shows him fighting the phone
     box FIVE TIMES -- `+49 157 8551545`, `+49 1578551545`, `1578551545` -- collecting
     `Error Phone Number`, until `15785541545` was accepted. Our profile stores `+49 15785541545`,
     which is one of the values that FAILED. No amount of model reasoning recovers that; only the
     recording says it.
  3. THE ORDER AND THE ENTRY PATH. Accept Cookies -> Autofill with Resume -> Sign in with Google.
     Our driver defaulted to Apply Manually and refused Google outright.

**THE RAW RECORDINGS MUST NEVER BE COMMITTED.** Playwright codegen writes every typed value into the
file, and the Accenture one contains the candidate's real Google password in cleartext. So this module
COMPILES them into a sanitised knowledge file that carries labels, option choices, quirks and ordering
but NO secrets, and that file is what ships. `redact()` is applied to every value on the way in, and
`compile_dir()` refuses to emit a knowledge file that still matches a secret shape -- verified, not
hoped for.

WHAT IT IS NOT: a replay engine. Replaying codegen line-for-line breaks on the first re-render, and
these files are full of the human's own corrections (twenty `ArrowLeft` presses, a `Delete` clicked
eighteen times). What survives compilation is the DECLARATIVE part: this ATS calls that field this
name, and this value works.
"""
from __future__ import annotations
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(HERE, "knowledge")

# ---------------------------------------------------------------- secrets, on the way IN
# A codegen file contains EVERY typed value. The Accenture recording contains the candidate's real
# Google password. Redaction is by SHAPE, deliberately: matching a specific password would leave the
# next one exposed, and this file has to be safe against a recording nobody has read yet.
_SECRET_ENV = ("GOOGLE_PASSWORD", "LINKEDIN_PASSWORD", "JHW_MAIL_PASS", "TELEGRAM_BOT_TOKEN",
               "AGENT_PROXY_TOKEN", "DO_INFERENCE_KEY", "OPENAI_API_KEY")
# high-entropy, punctuation-heavy, no spaces: the shape of a generated password, not of an answer
# A PASSWORD NEEDS "STRONG" PUNCTUATION -- something that does NOT occur in a URL, a hostname, a
# path or an e-mail. My first version required only "any non-word character", and it therefore
# REDACTED `itzen.ai`, which is the candidate's own website and a real answer. CLAUDE.md already
# records this exact class ("a job URL matched the high-entropy, no-spaces password heuristic ...
# URLs, e-mails and bare hosts are now excluded BY SHAPE before entropy is measured") and I remade
# it. `.` `-` `/` `:` `@` `_` `+` are URL characters; `(){}~!#$%^&*` are not.
_PW_STRONG = re.compile(r"[^\w\s.\-/@:+]")
_PW_SHAPE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)\S{8,64}$")
_URLISH = re.compile(r"^(https?://|www\.|mailto:)"
                     r"|^[\w.+-]+@[\w-]+\.[a-z]{2,}$"
                     r"|^[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9-]{1,63})+(/\S*)?$", re.I)
_LABEL_SECRET = re.compile(r"pass\s*word|passwort|\bpin\b|secret|token|api[\s_-]?key|otp|"
                           r"security code", re.I)


def redact(label: str, value: str) -> str:
    """`value` unless it is, or could be, a credential. Fails CLOSED."""
    v = (value or "").strip()
    if not v:
        return v
    if _LABEL_SECRET.search(label or ""):
        return "<redacted:by-label>"
    for name in _SECRET_ENV:                       # anything that IS a live secret in this env
        got = os.getenv(name)
        if got and len(got) > 5 and got in v:
            return "<redacted:matches-env>"
    if _URLISH.match(v):                           # a URL or an e-mail is never a password
        return v
    if (" " not in v and _PW_SHAPE.match(v) and _PW_STRONG.search(v)
            and not v.isdigit()):
        # A long punctuation-heavy token with no spaces. A real free-text answer has spaces; a
        # password does not. Fail closed: a redacted answer costs a re-ask, a leaked one is forever.
        return "<redacted:password-shape>"
    return v


# ---------------------------------------------------------------- parsing the codegen
_ACTS = ("fill", "click", "check", "press", "set_input_files", "select_option", "dblclick",
         "type", "uncheck")

_STEP = re.compile(
    r"""(?P<obj>page\d*)\s*
        (?P<chain>(?:\.\s*(?:get_by_\w+|locator|filter|first|nth|last)\s*\((?:[^()]|\([^()]*\))*\)
                   |\.\s*(?:first|last))*)
        \.\s*(?P<act>%s)\s*\((?P<arg>(?:[^()]|\([^()]*\))*)\)""" % "|".join(_ACTS),
    re.X)
_BYROLE = re.compile(r"get_by_role\(\s*[\"'](?P<role>\w+)[\"']\s*(?:,\s*name\s*=\s*"
                     r"(?P<q>[\"'])(?P<name>(?:[^\"'\\]|\\.)*)(?P=q))?")
_BYLABEL = re.compile(r"get_by_(?P<kind>label|placeholder|title|text)\(\s*(?P<q>[\"'])"
                      r"(?P<name>(?:[^\"'\\]|\\.)*)(?P=q)")
_STRARG = re.compile(r"^\s*(?P<q>[\"'])(?P<v>(?:[^\"'\\]|\\.)*)(?P=q)")
_GOTO = re.compile(r"""(?P<obj>page\d*)\.goto\(\s*(?P<q>["'])(?P<url>[^"']+)(?P=q)""")

# WHICH ATS AM I LOOKING AT. Keyed on the URL, because that is the one thing a recording always has.
_ATS = [
    ("greenhouse", r"greenhouse\.io|job-boards\.greenhouse"),
    ("workday", r"myworkdayjobs\.com|\.wd\d+\."),
    ("phenom", r"careers\.nttdata|phenom"),
    ("lever", r"jobs\.lever\.co"),
    ("smartrecruiters", r"smartrecruiters\.com"),
    ("ashby", r"jobs\.ashbyhq\.com"),
    ("pinpoint", r"careers\.[\w-]+\.org/postings|pinpointhq"),
    ("icims", r"icims\.com|careers-[\w-]+\.icims"),
    ("linkedin", r"linkedin\.com/jobs|linkedin\.com/in/"),
]


def ats_of(urls: list) -> str:
    blob = " ".join(urls).lower()
    for name, rx in _ATS:
        if re.search(rx, blob):
            return name
    return "unknown"


# Playwright codegen emits exactly one of these two entry points, in either language. Our adapters
# are `async def apply(...)` / `async def drive(...)` and match neither.
_CODEGEN_SIG = re.compile(
    r"def\s+test_\w*\s*\(\s*page\s*:\s*Page\s*\)"          # python, sync pytest style
    r"|async\s+def\s+run\s*\(\s*playwright\s*:\s*Playwright" # python, async style
    r"|test\s*\(\s*['\"][^'\"]*['\"]\s*,\s*async\s*\(\s*\{\s*page"   # typescript
    r"|import\s+\{\s*test\s*,\s*expect\s*\}\s+from\s+['\"]@playwright/test")


def _is_codegen(txt: str) -> bool:
    return bool(_CODEGEN_SIG.search(txt or ""))


def _unesc(s: str) -> str:
    return s.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n").replace("\\\\", "\\")


def parse(src: str) -> dict:
    """One codegen file -> {ats, urls, steps:[{role,name,act,value}]}. Never executes anything."""
    urls = [m.group("url") for m in _GOTO.finditer(src)]
    steps = []
    for m in _STEP.finditer(src):
        chain, act, arg = m.group("chain") or "", m.group("act"), (m.group("arg") or "").strip()
        role, name, how = "", "", ""
        r = _BYROLE.search(chain)
        if r:
            role, name, how = r.group("role"), _unesc(r.group("name") or ""), "role"
        else:
            lm = _BYLABEL.search(chain)
            if lm:
                role, name, how = "", _unesc(lm.group("name")), lm.group("kind")
        if not name:
            continue                                  # a raw CSS locator is not knowledge
        val = ""
        sm = _STRARG.match(arg)
        if sm:
            val = _unesc(sm.group("v"))
        steps.append({"role": role, "name": name.strip(), "how": how, "act": act,
                      "value": redact(name, val)})
    return {"ats": ats_of(urls), "urls": urls, "steps": steps}


# ---------------------------------------------------------------- compiling to knowledge
# The human's own corrections are in these files: twenty ArrowLefts, a Delete clicked eighteen times,
# a value typed four ways. Only the LAST value a field received is the one that was accepted, and
# navigation keystrokes carry no information at all.
_NOISE_ACTS = {"press", "dblclick", "type"}


def compile_choices(rec: dict) -> dict:
    """label -> the OPTION the human actually clicked. NOT what he typed to find it.

    MEASURED 2026-08-17, and it put ALGERIA in a German application. `known_value("Country")`
    returned `'ger'` -- the SEARCH TEXT he typed to filter a 250-country list -- and the chooser then
    resolved that against the options it could see. Germany was not among them (react-select
    virtualises, so only the first few dozen are in the DOM), `ger` matched nothing by prefix, and the
    substring pass found it inside al-GER-ia. One hit, so it looked unambiguous, and a wrong country
    went into a real employer's form.
    A FILTER QUERY AND A CHOSEN OPTION ARE DIFFERENT KINDS OF FACT. codegen records them one after the
    other -- `combobox("Country").fill("ger")` then `option("Germany +").click()` -- so the pairing is
    right there in the file and simply has to be read. Pairs are also formed from a bare label click
    followed by an option click, which is how `How did you hear about us?*` -> `LinkedIn` appears."""
    out, pending = {}, ""
    for st in rec.get("steps") or []:
        nm, act, role, how = st["name"], st["act"], st["role"], st["how"]
        if role == "option" and act == "click":
            if pending:
                out[pending] = st["value"] or nm      # the option's NAME is the answer
                pending = ""
            continue
        # A FILLED TEXTBOX HAS ALREADY BEEN ANSWERED, so its label must not stay pending. In the OKX
        # recording the sequence is
        #     textbox("Email").fill("evgeny@s4biz.io")
        #     .select__control.first.click()          <- a raw CSS locator, so it carries no name
        #     option("Germany +").click()
        # and the naive version paired `Email -> Germany +`. Only a COMBOBOX (whose fill is a search
        # query) or a bare LABEL/TEXT click can be the question an option answers.
        if role == "combobox" or how in ("text", "label"):
            pending = nm
        elif act == "fill":
            pending = ""                              # answered in place; it awaits no option
    return out


def compile_steps(recs: list) -> dict:
    """Many recordings of one ATS -> the declarative facts."""
    fields, options, files, buttons, checks, order = {}, {}, [], [], [], []
    for rec in recs:
        for st in rec["steps"]:
            nm, act, val, role = st["name"], st["act"], st["value"], st["role"]
            if act == "set_input_files":
                if val and val not in files:
                    files.append({"name": nm, "file": val})
                continue
            if act == "fill" and val:
                # LAST WRITE WINS. In the Accenture recording the phone box was filled four times and
                # only the fourth was accepted; the first three produced `Error Phone Number`. The
                # final value is the only one that is a fact.
                fields[nm] = {"value": val, "role": role or "textbox", "how": st["how"]}
                continue
            if act in ("check", "uncheck"):
                if nm not in [c["name"] for c in checks]:
                    checks.append({"name": nm, "checked": act == "check"})
                continue
            if act == "click" and role in ("option", "radio") or st["how"] == "text":
                options.setdefault(nm, 0)
                options[nm] += 1
                continue
            if act == "click" and role in ("button", "link"):
                if nm not in buttons:
                    buttons.append(nm)
            if act in _NOISE_ACTS:
                continue
            if nm not in order:
                order.append(nm)
    choices: dict = {}
    for rec in recs:
        for k, v in compile_choices(rec).items():
            choices.setdefault(k, v)
    return {"fields": fields, "options": sorted(options), "files": files, "choices": choices,
            "buttons": buttons, "checkboxes": checks, "order": order}


def compile_dir(src_dir: str, out_dir: str = KB_DIR, log=print) -> dict:
    """Every recording in a directory -> one sanitised knowledge file per ATS. Returns a summary."""
    os.makedirs(out_dir, exist_ok=True)
    by_ats: dict = {}
    seen = []
    for p in sorted(glob.glob(os.path.join(src_dir, "*.py"))
                    + glob.glob(os.path.join(src_dir, "*.ts"))):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except Exception as e:
            log(f"  [rec] {os.path.basename(p)}: unreadable ({type(e).__name__})")
            continue
        # ONLY A HUMAN RECORDING IS GROUND TRUTH. The first run of this compiler swept up OUR OWN
        # ADAPTERS out of the same folder -- flows/greenhouse.py, flows/workday.py, flows/icims.py --
        # and would have committed our INVENTED selectors as if a human had proved them. That is
        # exactly backwards: the whole point of this file is to replace guesses with what worked.
        # Codegen has two entry signatures and nothing else in this project has either.
        if not _is_codegen(txt):
            log(f"  [rec] {os.path.basename(p):44s} SKIPPED — not a codegen recording "
                f"(our own source is not evidence)")
            continue
        rec = parse(txt)
        if not rec["steps"]:
            continue
        rec["source"] = os.path.basename(p)
        by_ats.setdefault(rec["ats"], []).append(rec)
        seen.append((os.path.basename(p), rec["ats"], len(rec["steps"])))
    out = {}
    for ats, recs in by_ats.items():
        kb = compile_steps(recs)
        kb["ats"] = ats
        kb["sources"] = [r["source"] for r in recs]
        kb["job_urls"] = sorted({u for r in recs for u in r["urls"]})[:8]
        blob = json.dumps(kb, ensure_ascii=False)
        # A KNOWLEDGE FILE THAT STILL CARRIES A SECRET MUST NOT BE WRITTEN. Verified, not assumed:
        # these are committed, and the raw recordings are gitignored precisely because they are not.
        leaked = [n for n in _SECRET_ENV
                  if (os.getenv(n) or "") and len(os.getenv(n) or "") > 5
                  and (os.getenv(n) or "") in blob]
        if leaked:
            log(f"  [rec] REFUSING to write {ats}.json — it still contains {leaked}")
            continue
        with open(os.path.join(out_dir, ats + ".json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(kb, indent=2, ensure_ascii=False, sort_keys=True))
        out[ats] = kb
        log(f"  [rec] {ats}.json  {len(kb['fields'])} field(s), {len(kb['options'])} option(s), "
            f"{len(kb['buttons'])} button(s), {len(kb['files'])} upload(s)  <- {kb['sources']}")
    for nm, ats, n in seen:
        log(f"        {nm:44s} -> {ats:16s} {n:>3} step(s)")
    return out


_CACHE: dict = {}


def load(ats: str) -> dict:
    """The committed knowledge for one ATS, or {}. Never raises: a missing KB is not an outage."""
    if ats in _CACHE:
        return _CACHE[ats]
    try:
        with open(os.path.join(KB_DIR, f"{ats}.json"), encoding="utf-8") as fh:
            _CACHE[ats] = json.load(fh)
    except Exception:
        _CACHE[ats] = {}
    return _CACHE[ats]


def known_value(ats: str, label: str) -> str:
    """The value a HUMAN successfully put in this field on this ATS. '' if we have never seen it.

    This is the highest-confidence answer available anywhere in the system: it is not inferred, not
    generated and not a default -- it is what worked. `_norm` is loose about punctuation and the
    required-marker (`Resume/CV*` vs `Resume/CV`) because ATSs decorate labels differently per tenant.
    """
    kb = load(ats)
    tgt = _norm(label)
    if not tgt:
        return ""
    for nm, d in (kb.get("fields") or {}).items():
        if _norm(nm) == tgt:
            return d.get("value", "")
    for nm, d in (kb.get("fields") or {}).items():        # containment, longest match first
        n = _norm(nm)
        if n and (n in tgt or tgt in n) and abs(len(n) - len(tgt)) <= 12:
            return d.get("value", "")
    return ""


def _norm(s: str) -> str:
    s = re.sub(r"[*·:•]+", " ", (s or "").lower())
    s = re.sub(r"\(required\)|\brequired\b|\boptional\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def known_choice(ats: str, label: str) -> str:
    """The OPTION a human picked for this question, or ''. Never a search query.

    Separate from `known_value` on purpose: for a choice field the recorded FILL is the text he typed
    to filter, and using it as the answer is what produced 'Algeria +213' on a German application."""
    kb = load(ats)
    tgt = _norm(label)
    if not tgt:
        return ""
    ch = kb.get("choices") or {}
    for nm, v in ch.items():
        if _norm(nm) == tgt:
            return v
    for nm, v in ch.items():
        n = _norm(nm)
        if n and (n in tgt or tgt in n) and abs(len(n) - len(tgt)) <= 14:
            return v
    return ""


def labels(ats: str) -> list:
    return sorted((load(ats).get("fields") or {}).keys())


def buttons(ats: str) -> list:
    return list(load(ats).get("buttons") or [])


def _selftest() -> int:
    fails = []

    def ck(c, m):
        print(("  OK   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    print("\n[1] the parser reads the REAL shapes codegen emits")
    src = '''
page.goto("https://job-boards.greenhouse.io/tanium/jobs/7800463")
page.get_by_role("button", name="Apply").click()
page.get_by_role("textbox", name="First Name", exact=True).fill("Evgeny")
page.get_by_role("textbox", name="Phone").fill("15785541545")
page.get_by_role("combobox", name="Country").fill("ger")
page.get_by_role("option", name="Germany +").click()
page.get_by_label("Resume/CV*").locator("button").filter(has_text="Attach").click()
page.get_by_role("group", name="Resume/CV*").get_by_label("Attach").set_input_files("CV.pdf")
page.get_by_role("button", name="Submit application").click()
'''
    r = parse(src)
    ck(r["ats"] == "greenhouse", f"the ATS is identified from the URL: {r['ats']}")
    names = [s["name"] for s in r["steps"]]
    ck("First Name" in names and "Phone" in names, f"named fields extracted: {names[:6]}")
    ck(any(s["act"] == "set_input_files" for s in r["steps"]), "an upload step is recognised")
    kb = compile_steps([r])
    ck(kb["fields"].get("Phone", {}).get("value") == "15785541545",
       "the phone VALUE is captured verbatim")
    ck("Submit application" in kb["buttons"], f"the submit button is captured: {kb['buttons']}")
    ck(any("CV.pdf" in f["file"] for f in kb["files"]), "the uploaded file is captured")

    print("\n[2] LAST WRITE WINS — the human's failed attempts are not facts")
    # This is the real Accenture phone sequence: three rejected values, then the accepted one.
    src2 = '''
page1.goto("https://accenture.wd103.myworkdayjobs.com/en-US/AccentureCareers/job/x/apply")
page1.get_by_role("textbox", name="Phone Number").fill("+49 157 8551545")
page1.get_by_role("textbox", name="Phone Number").fill("+49 1578551545")
page1.get_by_role("textbox", name="Phone Number").fill("1578551545")
page1.get_by_role("button", name="Error Phone Number").click()
page1.get_by_role("textbox", name="Phone Number").fill("15785541545")
'''
    r2 = parse(src2)
    ck(r2["ats"] == "workday", f"workday recognised: {r2['ats']}")
    kb2 = compile_steps([r2])
    ck(kb2["fields"]["Phone Number"]["value"] == "15785541545",
       f"only the ACCEPTED value survives: {kb2['fields']['Phone Number']['value']!r}")

    print("\n[3] a credential in a recording NEVER reaches the knowledge file")
    os.environ["GOOGLE_PASSWORD"] = "w@m6(}--@~j)xDpj"
    try:
        ck(redact("Enter your password", "w@m6(}--@~j)xDpj").startswith("<redacted"),
           "redacted by LABEL")
        ck(redact("some other box", "w@m6(}--@~j)xDpj").startswith("<redacted"),
           "redacted because it MATCHES THE LIVE ENV even under an innocent label")
        ck(redact("anything", "Xk9#mQ2$vLp8!wZr").startswith("<redacted"),
           "redacted by SHAPE — a password we have never seen is still refused")
        src3 = ('page1.goto("https://x.wd103.myworkdayjobs.com/a/apply")\n'
                'page1.get_by_role("textbox", name="Enter your password").fill'
                '("w@m6(}--@~j)xDpj")\n')
        ck("w@m6" not in json.dumps(parse(src3)), "no trace of it anywhere in the parsed output")
    finally:
        del os.environ["GOOGLE_PASSWORD"]

    print("\n[4] and a REAL ANSWER is never mistaken for a credential")
    for lbl, v in (("Email", "evgeny@s4biz.io"),
                   ("LinkedIn Profile", "https://www.linkedin.com/in/feranicus/"),
                   ("Website", "itzen.ai"),
                   ("Website", "www.cybergod.ai"),
                   ("Portfolio", "https://jobhuntwow.com/electronic"),
                   ("GitHub", "github.com/feranicus"),
                   ("Address Line 1", "Herman J Bach Weg"),
                   ("City", "Friedberg"),
                   ("Overall Result (GPA)", "84"),
                   ("Phone", "15785541545"),
                   ("Postal Code", "61169"),
                   ("How did you hear about us?", "Linkedin"),
                   ("Describe a specific instance", "I had build www.cybergod.ai from scratch "
                                                    "myself because there was a clear pain point")):
        ck(redact(lbl, v) == v, f"kept: {lbl} = {v[:38]}")

    print("\n[5] known_value() is loose about decoration, strict about identity")
    global _CACHE
    _CACHE = {"greenhouse": {"fields": {"Resume/CV*": {"value": "CV.pdf"},
                                        "Location (City)": {"value": "Friedberg"},
                                        "Phone": {"value": "15785541545"}}}}
    ck(known_value("greenhouse", "Resume/CV") == "CV.pdf", "the required-marker * is ignored")
    ck(known_value("greenhouse", "resume/cv *") == "CV.pdf", "case and spacing ignored")
    ck(known_value("greenhouse", "Location (City)") == "Friedberg", "exact match")
    ck(known_value("greenhouse", "Salary Expectation") == "", "an unknown field returns nothing")
    ck(known_value("nosuchats", "Phone") == "", "an ATS with no knowledge returns nothing")
    _CACHE = {}

    print("\n[6] a recording is DATA, never code — nothing here executes it")
    src_self = open(__file__, encoding="utf-8").read()
    ship = src_self[:src_self.index("def _selftest(")]
    for banned in ("exec(", "eval(", "subprocess", "importlib", "__import__"):
        ck(banned not in ship, f"no {banned} in the shipping code")
    ck(len(ship) > 3000, f"the measured slice is the real module ({len(ship)} chars)")

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} RECORDING-KNOWLEDGE CONTRACT(S) BROKEN")
        return 1
    print("ALL RECORDING-KNOWLEDGE CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    if "--compile" in sys.argv:
        i = sys.argv.index("--compile")
        d = sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.join(HERE, "..", "recordings")
        compile_dir(os.path.abspath(d))
        raise SystemExit(0)
    print(__doc__)
