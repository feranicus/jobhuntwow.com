#!/usr/bin/env python3
"""LEARNED ANSWERS — what the human told us once, reused forever.

THE PROBLEM THIS SOLVES, in the candidate's words: "it doesn't make sense to bother me if this is
supposed to be a self-serviced agent running 24/7". Today every stall asks again, even for a
question that was answered an hour ago on a different employer's form. At 400 applications that is
the difference between a working agent and a chat partner.

SCOPES, and the distinction is load-bearing:
  * "global"  -- a screening answer that is TRUE OF THE CANDIDATE, so it transfers to every ATS
                 ("are you legally authorised to work in Germany?"). Recalled across all employers.
  * <tenant>  -- an answer that is only true for one employer. Recalled only there.

SECRETS ARE REFUSED, DELIBERATELY. `flows/atscreds.py` is the one owner of credentials: chmod 600,
gitignored, redacted in every log path. If a password landed in here it would be a SECOND home for
a credential, in a file whose whole purpose is to be replayed into forms -- the exact "one value,
two homes" defect this repo has paid for repeatedly. `remember()` therefore refuses anything whose
QUESTION asks for a secret, and says so.

Every answer records WHERE it came from and WHEN, because an answer the candidate gave in July may
need re-confirming in January and a store you cannot date is a store you cannot audit.
"""
from __future__ import annotations
import json, os, re, stat, sys, time

OUT = os.getenv("JHW_OUT", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out"))
STORE = os.getenv("JHW_LEARNED", os.path.join(OUT, "learned_answers.json"))

# A question asking for one of these is a CREDENTIAL question -> atscreds owns it, not us.
# "code" MUST be qualified. Accenture's real screening question is "All individuals employed by
# Accenture must comply with its CODE OF BUSINESS ETHICS. Do you agree?" -- a bare /code/ would
# refuse to remember that answer forever and the agent would ask about it on every application.
_SECRET_Q = re.compile(r"\bpassword\b|\bpasswort\b|\bsecret\b|\bapi[_ -]?key\b|\btoken\b|"
                       r"\bcredential|\bpin\b|\botp\b|\b2fa\b|\bmfa\b|"
                       r"(?:verification|confirmation|security|access|one[\s-]?time|"
                       r"\d[\s-]?digit)[\s-]*code", re.I)
# ...and a value that merely LOOKS like a secret is refused too, whatever the question said.
# A PASSWORD HAS NO SPACES; PROSE DOES.
# MEASURED 2026-08-17: the old pattern `.{12,}` allowed spaces, so it flagged
#   'EUR 150.000 gross per annum'      (upper, lower, digit, '.', >12 chars)
#   'A 40-person delivery programme.'
# as credentials — which meant the salary answer and any free-text answer containing a number could
# NEVER be learned, silently. Same class as the job URL that once matched a password heuristic.
# The discriminator is (a) no whitespace and (b) punctuation that does NOT occur in a URL, a date or a
# money amount: `(){}~!#$%^&*` — never `. - / @ : + _`. Verified in both directions.
_SECRET_STRONG = re.compile(r"[(){}~!#$%^&*]")
_SECRET_V = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)\S{8,64}$")


def _norm(q: str) -> str:
    """Normalise a question so the SAME question from a different employer hits the same key.
    Strips urls, hostnames, digits and punctuation -- otherwise 'stuck at the login for
    accenture.wd103...' and '...for siemens.wd3...' would be two different questions."""
    s = (q or "").lower()
    # PARENTHETICALS FIRST. Our asks read "Workday (accenture.wd103.myworkdayjobs.com) asks: ..." and
    # the host regex below only removes the ".com" tail, so "accenture wd" survived and the SAME
    # question from Siemens scored 0.87 -- under the fuzzy threshold, so it was never recalled.
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\b[\w-]+\.(com|net|org|io|de|co\.uk|ai)\b\S*", " ", s)
    s = re.sub(r"\b[\w.-]+@[\w.-]+\b", " ", s)
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^a-z ]+", " ", s)
    # British/American spelling must not split one question into two memories:
    # "legally authorised" vs "legally authorized" is the same question.
    s = re.sub(r"(is|iz)(e|ed|es|ing|ation)\b", r"z\2", s)
    s = re.sub(r"\bwd\b", " ", s)          # leftover Workday tenant marker
    return " ".join(s.split())[:160]


def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    # in-place write, never mv: a bind-mounted FILE pins its inode.
    with open(STORE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, sort_keys=True, ensure_ascii=False)
    try:
        os.chmod(STORE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def is_secret(question: str, answer: str = "") -> bool:
    a = (answer or "").strip()
    return bool(_SECRET_Q.search(question or "")) or bool(
        _SECRET_V.match(a) and _SECRET_STRONG.search(a))


# A near-miss must still hit: an ATS rewords the same question slightly per tenant. 0.92 is tight
# enough that unrelated questions do not collide, and a fuzzy hit is LOGGED so it stays auditable.
FUZZ = float(os.getenv("JHW_LEARNED_FUZZ", "0.92"))


# ---------------------------------------------------------------------------- the ONE store
# STORAGE IS DELEGATED to flows/memory.py (SQLite). This file keeps what it is the authority on:
# the CREDENTIAL RULE (`is_secret`) and what counts as the SAME QUESTION (`_norm`, FUZZ). It used to
# own a JSON file as well, while memory.py owned the same fact in SQLite -- one value, two homes, and
# the stale one wins. That is the defect this project has paid for with ENRICH_MODELS (four homes)
# and with the ATS credential store (two key schemes in one file). `memory.py` imports
# `out/learned_answers.json` ONCE, automatically, so nothing already learned is lost, and it leaves
# the JSON on disk untouched -- destroying a store to tidy a format is the worse trade.

def _scope(scope: str, employer) -> str:
    """SCOPE AND EMPLOYER ARE THE SAME ARGUMENT, and callers use both spellings.

    MEASURED 2026-08-17: `choose.py` and `greenhouse.py` call `recall(q, employer=...)` while this
    module only ever declared `scope=` -- so every call raised TypeError, was swallowed by the
    adapter's surrounding `except`, and THE LEARNED RUNG OF THE ANSWER LADDER WAS SILENTLY DEAD in
    the Greenhouse path. It never once returned a remembered answer. Accepting both spellings is
    the honest fix: renaming one caller would leave the other free to break again."""
    return (employer or scope or "global")


def recall(question: str, scope: str = "global", employer=None) -> str:
    """The best answer we have, or "" -- delegated to the one store."""
    try:
        import memory as MEM
        return MEM.recall(question, _scope(scope, employer))
    except Exception as e:
        print(f"[learned] recall unavailable ({type(e).__name__}: {str(e)[:60]})", flush=True)
        return ""


def remember(question: str, answer: str, scope: str = "global", employer=None,
             source: str = "human") -> bool:
    """Store an answer. Returns False (and stores NOTHING) for a credential -- atscreds owns those.
    The refusal is decided HERE, by `is_secret`, so the credential rule has exactly one definition."""
    if is_secret(question, answer):
        print("[learned] REFUSED to store a credential-shaped answer — atscreds owns secrets",
              flush=True)
        return False
    try:
        import memory as MEM
        return MEM.remember(question, answer, scope=_scope(scope, employer), source=source)
    except Exception as e:
        print(f"[learned] remember unavailable ({type(e).__name__}: {str(e)[:60]})", flush=True)
        return False


def forget(question: str, scope: str = "global", employer=None) -> bool:
    try:
        import memory as MEM
        return MEM.forget(question, _scope(scope, employer))
    except Exception:
        return False


def stats() -> dict:
    """{scope: count} — what we know, per employer."""
    try:
        import memory as MEM
        return MEM.scopes()
    except Exception:
        return {}


def _selftest() -> int:
    import tempfile
    fails = []

    def ck(name, ok, d=""):
        print(("  [PASS] " if ok else "  [FAIL] ") + name +
              (f"  :: {str(d)[:90]}" if not ok else ""))
        if not ok:
            fails.append(name)

    def _json_state():
        try:
            st = os.stat(STORE)
            return (st.st_size, int(st.st_mtime))
        except OSError:
            return None

    _json_before = _json_state()

    with tempfile.TemporaryDirectory() as td:
        # point the ONE store at a throwaway database, and at a legacy file that does not exist so
        # the import step cannot drag his real answers into a test.
        os.environ["JHW_MEMORY"] = os.path.join(td, "memory.sqlite")
        os.environ["JHW_LEARNED"] = os.path.join(td, "nothing-here.json")
        import importlib
        import memory as MEM
        importlib.reload(MEM)

        print("-- the same question from a DIFFERENT employer must hit the same memory")
        q1 = ("Workday (accenture.wd103.myworkdayjobs.com) asks: are you legally authorised to "
              "work in Germany?")
        q2 = ("Workday (siemens.wd3.myworkdayjobs.com) asks: are you legally authorized to "
              "work in Germany?")
        ck("stored", remember(q1, "Yes") is True)
        ck("recalled verbatim", recall(q1) == "Yes", recall(q1))
        ck("recalled from a different tenant, different spelling", recall(q2) == "Yes", recall(q2))

        print("-- a tenant-scoped answer is only true where it applies")
        remember("how did you hear about us", "Career Fair", employer="accenture.wd103")
        ck("tenant answer wins where it applies",
           recall("how did you hear about us", employer="accenture.wd103") == "Career Fair",
           recall("how did you hear about us", employer="accenture.wd103"))
        remember("how did you hear about us", "LinkedIn")
        ck("the global answer is used elsewhere",
           recall("how did you hear about us", employer="okx") == "LinkedIn",
           recall("how did you hear about us", employer="okx"))

        print("-- SECRETS ARE REFUSED (atscreds is the one owner)")
        ck("a password question is refused",
           remember("Reply with its PASSWORD for the Workday login", "hunter2") is False)
        ck("...and nothing was stored",
           not recall("Reply with its PASSWORD for the Workday login"))
        ck("a verification code is refused",
           remember("reply with the 6-digit code", "123456") is False)
        ck("a secret-SHAPED value is refused even with an innocent question",
           remember("what should I put here", "@Q4sD9igY!mzW5u@TNtv7Ib") is False)
        ck("but a normal answer with punctuation is still stored",
           remember("preferred method of communication?", "E-mail") is True)
        ck("a SALARY is not a credential (it was, until 2026-08-17)",
           remember("salary expectations?", "EUR 150.000 gross per annum") is True)
        ck("Accenture's real 'CODE OF BUSINESS ETHICS' question is NOT refused",
           remember("All individuals employed by Accenture must comply with its Code of Business "
                    "Ethics. Do you agree?", "Yes") is True)
        ck("...and it is recalled",
           recall("must comply with its Code of Business Ethics. Do you agree?") == "Yes")
        ck("a totally unrelated question does NOT fuzzy-match onto it",
           not recall("what is your notice period in months?"),
           recall("what is your notice period in months?"))

        print("-- housekeeping, and the JSON store is NOT written any more")
        ck("forget() removes it", forget(q1) and not recall(q1))
        ck("stats() counts per scope", stats().get("global", 0) >= 1, stats())
        # NOT "the file does not exist" — it usually DOES, from before the delegation, and a check
        # that demands its absence fails on every real machine. The property is that we no longer
        # WRITE it: memory.py imports it once and leaves it alone.
        ck("this module no longer writes the json store",
           _json_state() == _json_before, f"{_json_before} -> {_json_state()}")
        ck("...because the one store holds it", os.path.exists(os.environ["JHW_MEMORY"]))

        print("-- EVERY CALLER'S SPELLING WORKS (the rung was dead because one did not)")
        src = ""
        for f in ("choose.py", "greenhouse.py", "workday.py"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f)
            src += open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        import inspect
        for fn in (recall, remember, forget):
            sig = inspect.signature(fn).parameters
            for kw in ("scope", "employer"):
                ck(f"{fn.__name__}() accepts {kw}=", kw in sig)
        ck("callers do use employer=", "employer=employer" in src)

    print("\n" + ("LEARNED: " + ", ".join(fails) if fails
                  else "ALL LEARNED-ANSWER CONTRACTS HOLD"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--logic" in sys.argv else 0)
