"""resume_consensus.py — 4-model consensus for the Tailor: AUTHOR drafts, a DIFFERENT-VENDOR
AUDITOR reviews, the AUTHOR revises. Bounded rounds, deterministic guardrails.

WHY THIS EXISTS. The old Tailor made ONE call per document with a single cross-vendor fallback
(`electronic._ask("content", "content_fb", ...)`). Whatever the first model returned WAS the
document: nothing measured whether the answer was thin, nothing checked it against the JD, and a
well-formed nothing (`{}` parses fine) shipped as a finished resume. That is the same silent-quality
failure the sibling project measured and fixed with `_contract_ok` + a model chain + an
independent-vendor auditor, so the doctrine is ported here rather than re-invented.

THE SHAPE (deliberate, and each half is load-bearing):

  1. AUTHOR   walks the measured chain. A draft is accepted only if it passes the JSON CONTRACT
              *and* a DETERMINISTIC DEPTH FLOOR. A thin draft is a quality failure to REPORT and a
              trigger to try the next model - not something to render and hope.
  2. AUDITOR  a model that is NEVER the author and, where the chain allows, never the author's
              VENDOR. An audit by the same model is not a second opinion: it shares every blind
              spot and every failure mode. Ported from audit_fp.py::_pick_auditor.
  3. BOUND    the auditor may FLAG; it may never rewrite, delete or empty anything. Every flag is
              corroborated against DETERMINISTIC evidence (the profile text, the employer
              inventory, the JD text, the rendered bullets) before it is believed. Uncorroborated
              flags are recorded as `refused`, never silently dropped and never acted on.
  4. REVISE   only the AUTHOR edits its own document, and the revision is VERIFIED before it is
              accepted: it may not get thinner than 80 percent of the draft and it may not lose an
              employer. A revision that fails either test is REJECTED and the draft stands.

HARD RULES kept from the original implementation (they are the product, not decoration):
  * The model may re-angle, reorder, compress and rephrase facts that are IN the profile. It may
    never add an employer, date, degree, certification, metric or tool.
  * NEVER delete an employer to fit the page limit. Detailed bullets are for recent roles; every
    other employer still appears in `earlier` as one line.
  * Absence of evidence is a WARNING for the human, never a silent correction and never proof of
    fabrication - the profile may simply be terse.

NOTHING HERE IMPORTS fastapi. It imports `llm` LAZILY, inside the default poster, so the whole
module is testable with a stubbed poster and no network and no key. `backend/tests/
test_resume_consensus.py` is that test.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- the chain
# MEASURED chain, four vendors. Order is EVIDENCE, not taste (see the sibling project's
# compare_models.py / model_probe.py runs recorded in CLAUDE.md):
#   deepseek-3.2      fastest contract-valid model on this account; good prose. HEAD.
#   llama-4-maverick  Meta. A 429/outage is PROVIDER-WIDE, so the first backup must not share a
#                     failure domain with the head.
#   gemma-4-31B-it    Google. Measured ERRATIC on identical input (good / timeout / empty '{}'),
#                     so it is a third chance, not a peer.
#   kimi-k2.6         Moonshot. LAST, on measurement: it fails SLOWLY (it once burned 164s of a
#                     175s slice), and a model that fails slowly starves the ones behind it. It
#                     stays in the chain because a fourth vendor is a real hedge.
# NOT chained, deliberately: any thinking/reasoning model (kimi-k3, glm-5.x, qwen3.5-397b,
# deepseek-r1-distill). They emit chain-of-thought and break a strict-JSON contract.
CHAIN = ["deepseek-3.2", "llama-4-maverick", "gemma-4-31B-it", "kimi-k2.6"]

# PER-MODEL PARAMETER POLICY - constraints the API PUBLISHES, encoded so we do not pay a wasted
# round-trip to rediscover them on every single call.
#   kimi: answers HTTP 400 "temperature must be 0.6 for this model" to our normal temperature, and
#         "response_format type 'json_object' is not supported for this model". It also reasons
#         before answering, so thinking must stay suppressed - `chat_template_kwargs` is the ONLY
#         thing doing that, and blanket-stripping it on a 400 is what once produced 46,801
#         characters of rambling and a truncated non-answer.
# The required VALUE has changed under us before (1.0 -> 0.6), which is why `repair_payload` reads
# the number out of the error body rather than trusting this table. This entry is the fast path.
MODEL_PARAMS = {
    "kimi": {"temperature": 0.6, "_drop": ["response_format"]},
}

# vendor of a model id, so an auditor can be chosen from a DIFFERENT vendor than the author.
_VENDORS = (
    ("deepseek", "deepseek"), ("llama", "meta"), ("meta", "meta"),
    ("gemma", "google"), ("gemini", "google"), ("google", "google"),
    ("kimi", "moonshot"), ("moonshot", "moonshot"),
    ("glm", "zhipu"), ("zhipu", "zhipu"),
    ("qwen", "alibaba"), ("mistral", "mistral"), ("minimax", "minimax"),
    ("nemotron", "nvidia"), ("nvidia", "nvidia"),
    ("gpt-oss", "openai"), ("openai", "openai"), ("anthropic", "anthropic"),
)


def vendor(model: str) -> str:
    m = str(model or "").lower()
    for key, v in _VENDORS:
        if key in m:
            return v
    return "other"


def chain() -> list:
    """The effective chain. JHW_TAILOR_MODELS overrides; a divergence is announced, not hidden.

    A stale env var silently beating committed code is a class of bug this codebase has already
    paid for (four homes for one model chain). If the deployment disagrees with the repo, SAY SO -
    otherwise an evidence-based change to CHAIN has no effect and nobody knows.
    """
    env = os.environ.get("JHW_TAILOR_MODELS", "").strip()
    out = [m.strip() for m in env.split(",") if m.strip()] if env else list(CHAIN)
    if env and out != CHAIN:
        print("[warn] tailor: JHW_TAILOR_MODELS OVERRIDES the committed chain.\n"
              "         env  : %s\n         repo : %s\n       The env wins."
              % (",".join(out), ",".join(CHAIN)), file=sys.stderr)
    seen, chn = set(), []
    for m in out:
        if m not in seen:
            seen.add(m)
            chn.append(m)
    return chn


def model_params(model: str) -> dict:
    """Overrides this model REQUIRES, matched on an id prefix. May carry `_drop`: [keys]."""
    m = str(model or "").lower()
    for key, over in MODEL_PARAMS.items():
        if m.startswith(key):
            return json.loads(json.dumps(over))          # deep copy: callers mutate it
    return {}


class ModelHTTPError(Exception):
    """An HTTP error that CARRIES THE BODY.

    Discarding a 4xx body is exactly how "temperature must be 0.6 for this model" stayed invisible
    while a healthy, entitled model was written off as broken three times. A 4xx is the server
    telling you what it wants.
    """

    def __init__(self, status: int, body: str = ""):
        self.status = int(status)
        self.body = str(body or "")
        super().__init__("HTTP %d: %s" % (self.status, self.body[:300]))


def build_payload(model: str, system: str, user: str, *, max_tokens: int,
                  temperature: float = 0.3, json_mode: bool = True) -> dict:
    """Build the request. PURE and therefore testable - no network, no key, no clock.

    Two gateway facts are encoded here instead of being rediscovered per call:
      * per-model requirements (MODEL_PARAMS), including keys the model REFUSES to receive;
      * on this endpoint `max_tokens` and `response_format: json_object` are MUTUALLY EXCLUSIVE
        (observed on deepseek, llama AND gemma in one run, i.e. it is the gateway, not a model:
        "max_tokens cannot be set when response_format type is 'json_object'"). We keep the JSON
        contract and drop the ceiling, which is what the server advises and is the right way round
        on our own evidence: a ceiling cut lands MID-JSON and yields garbage, while wall-clock is
        still bounded by the per-call timeout and a timeout is a CLEAN failover.
    """
    payload: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        # suppress chain-of-thought: a thinking model cannot honour a strict-JSON contract.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    over = model_params(model)
    for k in over.pop("_drop", []):
        payload.pop(k, None)
    payload.update(over)
    if "response_format" in payload:
        payload.pop("max_tokens", None)
    return payload


def repair_payload(payload: dict, body: str) -> tuple:
    """Repair a rejected payload by WHAT THE SERVER NAMED. Returns (payload, [fixes]).

    Blanket-stripping fields until something works is how you disable a safeguard you did not know
    you had: popping `chat_template_kwargs` re-enables thinking, which is a far worse failure than
    the 400 it was meant to fix. So:
      * a named temperature is READ OUT OF THE BODY and re-sent (the required value has changed
        under us before, so the body outranks our own constant);
      * when the body names max_tokens, drop MAX_TOKENS - not response_format. Both fields are
        named in the gateway's message, so a first-match regex removes the JSON contract and keeps
        the ceiling the server just objected to, which is the exact inverse of the advice;
      * `chat_template_kwargs` is dropped ONLY if the server names it. Never speculatively.
    """
    b = str(body or "")
    fixes = []
    m = re.search(r"temperature must be ([0-9.]+)", b)
    if m:
        try:
            payload["temperature"] = float(m.group(1))
            fixes.append("temperature=" + m.group(1))
        except ValueError:
            pass
    if "max_tokens" in b:
        if payload.pop("max_tokens", None) is not None:
            fixes.append("dropped max_tokens (kept JSON mode)")
    elif "response_format" in b or not fixes:
        if payload.pop("response_format", None) is not None:
            fixes.append("dropped response_format")
    if "chat_template_kwargs" in b or "enable_thinking" in b:
        if payload.pop("chat_template_kwargs", None) is not None:
            fixes.append("dropped chat_template_kwargs")
    return payload, fixes


async def _default_poster(payload: dict, timeout: float) -> dict:
    """The real transport. Imported LAZILY so this module stays importable with no deps."""
    from . import llm
    try:
        return await llm.chat(payload, timeout=timeout)
    except Exception as e:                                   # translate into our carrying error
        st = getattr(e, "status", None)
        if st is None:
            raise
        raise ModelHTTPError(st, getattr(e, "body", "") or str(e))


async def call_model(model: str, system: str, user: str, *, max_tokens: int, timeout: float = 120.0,
                     temperature: float = 0.3, poster: Optional[Callable] = None) -> tuple:
    """One call to ONE named model. Returns (text, usage, finish_reason).

    A 400/422 is repaired ONCE from the server's own message and retried. Anything else (429/5xx)
    propagates so the caller can fail over to the next model in the chain - retrying the same model
    against an account-level quota cannot work.
    """
    post = poster or _default_poster
    payload = build_payload(model, system, user, max_tokens=max_tokens, temperature=temperature)
    try:
        d = await post(payload, timeout)
    except ModelHTTPError as e:
        if e.status not in (400, 422):
            raise
        print("[warn] tailor %s: HTTP %d from the API -> %s"
              % (model, e.status, e.body[:300].replace("\n", " ")), file=sys.stderr)
        payload, fixes = repair_payload(payload, e.body)
        print("[warn] tailor %s: retrying with %s" % (model, ", ".join(fixes) or "no change"),
              file=sys.stderr)
        d = await post(payload, timeout)
    ch = ((d or {}).get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    txt = msg.get("content") or msg.get("reasoning_content") or ""
    finish = ch.get("finish_reason")
    if finish == "length":
        # OUR ceiling truncated the answer mid-JSON. Say which it is, so the next person raises the
        # ceiling instead of blaming the model.
        print("[warn] tailor %s: OUTPUT TRUNCATED at max_tokens (finish_reason=length, %d chars) - "
              "this is OUR ceiling, not a model fault." % (model, len(txt)), file=sys.stderr)
    return txt, (d or {}).get("usage") or {}, finish


# --------------------------------------------------------------------------- JSON tolerance
def json_loose(txt: Any) -> dict:
    """Parse what models really emit: fenced blocks, prose around the JSON, a top-level [ {...} ].

    Tolerate any SHAPE; reject only an EMPTY answer (that is `contract_ok`'s job). A good answer
    thrown away by a strict parser costs the whole call.
    """
    if isinstance(txt, dict):
        return txt
    s = str(txt or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    a, b = s.find("{"), s.find("[")
    starts = [x for x in (a, b) if x >= 0]
    if starts:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[min(starts):])
            if isinstance(obj, dict):
                return obj
            if isinstance(obj, list):
                for x in obj:
                    if isinstance(x, dict):
                        return x
        except Exception:
            pass
    m = re.search(r"\{.*\}", s, re.S)                    # last resort: greediest object
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def _strs(v: Any, cap: int = 400) -> list:
    if not isinstance(v, list):
        v = [v] if isinstance(v, str) and v.strip() else []
    out = []
    for x in v:
        if isinstance(x, (str, int, float)) and str(x).strip():
            out.append(str(x).strip())
        elif isinstance(x, dict):                        # {"text": "..."} / {"bullet": "..."}
            for k in ("text", "bullet", "value", "name", "title"):
                if str(x.get(k) or "").strip():
                    out.append(str(x[k]).strip())
                    break
    return out[:cap]


def normalise_resume(d: dict) -> dict:
    """Coerce the resume answer into the shape every consumer assumes.

    One list inside `experience` raises AttributeError on `e.get("company")` and throws away an
    otherwise good answer. Flatten one level, keep the dicts, drop the rest.
    """
    d = dict(d or {})
    for key in ("experience", "earlier"):
        flat = []
        v = d.get(key)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    flat.append(x)
                elif isinstance(x, list):
                    flat.extend([y for y in x if isinstance(y, dict)])
        d[key] = flat
    for e in d["experience"]:
        e["bullets"] = _strs(e.get("bullets") or e.get("highlights"), 12)
    for key in ("skills", "highlights", "keywords_matched", "gaps", "all_employers"):
        d[key] = _strs(d.get(key))
    d["summary"] = str(d.get("summary") or "").strip()
    return d


def normalise_cover(d: dict) -> dict:
    d = dict(d or {})
    d["paragraphs"] = _strs(d.get("paragraphs") or d.get("body"), 8)
    d["salutation"] = str(d.get("salutation") or "").strip()
    d["closing"] = str(d.get("closing") or "").strip()
    return d


# --------------------------------------------------------------------------- quality floors
def resume_depth(d: dict) -> int:
    """Chars of SUBSTANTIVE prose in a resume answer - summary + highlights + role bullets.

    MEASURE DEPTH, NOT PRESENCE. The sibling project measured a model that "rewrote" every finding
    and produced a third of the prose: coverage read 100 percent and the deck was visibly thin,
    because the check asked whether an answer existed rather than whether it said anything.
    Skills and employer names are excluded on purpose: a list of 20 nouns is not depth.
    """
    d = d or {}
    n = len(str(d.get("summary") or ""))
    n += sum(len(str(h)) for h in (d.get("highlights") or []))
    for e in (d.get("experience") or []):
        if isinstance(e, dict):
            n += sum(len(str(b)) for b in (e.get("bullets") or []))
    return n


def cover_depth(d: dict) -> int:
    return sum(len(str(p)) for p in ((d or {}).get("paragraphs") or []))


# FLOORS FROM ARITHMETIC, not taste, and set at the LOW end so only a genuinely thin answer fails.
# Resume: the contract asks for a 3-4 sentence summary (~350 chars), 3-5 CAR highlights (~120 each
#   = 360-600) and 3-5 bullets on each recent role (~110 each). A real 3-role draft measures well
#   over 2000. 900 is roughly a summary plus one properly-bulleted role - below that the document
#   cannot be a tailored senior resume, whatever it claims.
# Cover: 250-350 words is ~1500-2100 chars. 700 is under half of the SHORT end, so it rejects a
#   stub and nothing else.
# The test asserts BOTH directions: a realistic draft passes, a stub fails. A floor nobody measured
# against real output is a number that eventually rejects good work.
MIN_RESUME = int(os.environ.get("JHW_TAILOR_MIN_RESUME", "900"))
MIN_COVER = int(os.environ.get("JHW_TAILOR_MIN_COVER", "700"))


def contract_ok_resume(d: Any, tokens_out=None) -> tuple:
    """Did the model ANSWER, or emit a well-formed nothing? `{}` parses perfectly."""
    if not isinstance(d, dict):
        return False, "not a JSON object (%s)" % type(d).__name__
    if tokens_out is not None and int(tokens_out) < 40:
        return False, "only %s completion tokens (empty answer)" % tokens_out
    exp = [e for e in (d.get("experience") or []) if isinstance(e, dict) and
           str(e.get("company") or "").strip()]
    if not exp:
        return False, "no experience entry carries a company (keys=%s)" % sorted(d)[:8]
    if not str(d.get("summary") or "").strip():
        return False, "no summary"
    dep = resume_depth(d)
    if dep < MIN_RESUME:
        return False, "THIN: %d chars of prose, floor is %d" % (dep, MIN_RESUME)
    return True, ""


def contract_ok_cover(d: Any, tokens_out=None) -> tuple:
    if not isinstance(d, dict):
        return False, "not a JSON object (%s)" % type(d).__name__
    if tokens_out is not None and int(tokens_out) < 40:
        return False, "only %s completion tokens (empty answer)" % tokens_out
    paras = [p for p in (d.get("paragraphs") or []) if str(p).strip()]
    if len(paras) < 2:
        return False, "fewer than 2 paragraphs"
    dep = cover_depth(d)
    if dep < MIN_COVER:
        return False, "THIN: %d chars of prose, floor is %d" % (dep, MIN_COVER)
    return True, ""


# --------------------------------------------------------------------------- deterministic truth
def profile_text(profile: Any) -> str:
    if isinstance(profile, str):
        return profile
    try:
        return json.dumps(profile, ensure_ascii=False, indent=1)
    except Exception:
        return str(profile)


def _norm(x: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(x or "").lower())


def rendered_employers(resume: dict) -> list:
    out = []
    for key in ("experience", "earlier"):
        for e in ((resume or {}).get(key) or []):
            if isinstance(e, dict):
                v = str(e.get("company") or e.get("employer") or "").strip()
                if v:
                    out.append(v)
    return out


def missing_employers(resume: dict) -> list:
    """Which employers did the model SEE but leave OUT of the document?

    The model returns its own inventory in `all_employers`; this compares that against what it
    actually rendered, so no fragile parsing of the source CV is needed. Containment, not equality:
    "Stars4Business" vs "Stars4Business OU" is one employer and a legal suffix must not be reported
    as a dropped role.

    THIS IS THE AUTHORITY on employer drops. The LLM auditor is a backstop that may FLAG but never
    overrule it - same doctrine as "recon is the ownership authority" in the sibling project.
    """
    seen = [str(x).strip() for x in ((resume or {}).get("all_employers") or []) if str(x).strip()]
    if not seen:
        return []
    rendered = [_norm(c) for c in rendered_employers(resume)]
    rendered = [r for r in rendered if r]
    out = []
    for c in seen:
        n = _norm(c)
        if not n:
            continue
        if not any(n in r or r in n for r in rendered):
            out.append(c)
    return out


_LEGAL = re.compile(r"\b(gmbh|ag|inc|ltd|llc|ou|se|bv|plc|kg|co|sa|as|oy|ab|srl|spa)\b")


def employer_in_profile(name: str, profile: Any) -> bool:
    """Is this organisation present in the supplied profile?

    ONE implementation, because two different answers to this question is how a guardrail ends up
    protecting a fabricated employer (it did - see revision_ok). Matched on the head token with
    legal forms stripped, so "Stars4Business OU" and "Stars4Business" are the same company.
    A name too short to discriminate is treated as PRESENT: fail towards keeping, never towards
    accusing the candidate of invention.
    """
    hay = re.sub(r"\s+", " ", profile_text(profile).lower())
    probe = _LEGAL.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())).strip()
    head = probe.split(" ")[0] if probe else ""
    if not head or len(head) <= 2:
        return True
    return head in hay


def truth_check(resume: dict, profile: Any) -> list:
    """Flag any organisation the profile does not contain.

    Reported as a WARNING for the human. Never silently "corrected" by rewriting prose, and never
    treated as proof of fabrication - the profile may simply be terse.
    """
    warns, seen = [], set()
    for e in ((resume or {}).get("experience") or []):
        if not isinstance(e, dict):
            continue
        v = str(e.get("company") or "").strip()
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        if not employer_in_profile(v, profile):
            warns.append("Employer '%s' is not present in the supplied profile - verify it before "
                         "sending." % v)
    return warns


# Words that make a bullet an OUTCOME rather than a duty. Used to corroborate the auditor's
# "this bullet carries no fact/number/outcome" flags deterministically, so a carpet-bombing
# auditor cannot condemn the whole document on taste.
_OUTCOME = re.compile(
    r"\b(reduc|cut|save|saved|saving|grew|grow|increas|improv|deliver|launch|won|win|"
    r"secur|migrat|scal|built|build|led|lead|drove|drive|achiev|exceed|halv|doubl|tripl|"
    r"eliminat|automat|consolidat|turnaround|closed|signed|retain|recover|avoided|shorten)", re.I)
_NUMBER = re.compile(r"\d")


def bullet_is_weak(text: str) -> bool:
    """No number AND no outcome verb -> it is a duty statement, not an achievement.

    Deliberately narrow. This corroborates a flag; it does not hunt for bullets on its own, and it
    can never cause a deletion.
    """
    t = str(text or "")
    if len(t.strip()) < 12:
        return True
    return not (_NUMBER.search(t) or _OUTCOME.search(t))


_STOP = set("""the a an and or of for to in on with by at from as is are was were be been being
this that these those their our your his her its it they we you i not no than then so such very
which who whom whose what when where how why all any both each few more most other some only own
same also into over under between during through across against about above after before while
including include included using use used based led lead new years year role roles team teams
company companies work worked working project projects experience candidate profile resume""".split())


def _hard_tokens(claim: str) -> tuple:
    """The parts of a claim that can be CHECKED, split into (numbers, word stems).

    A model PARAPHRASES - that is explicitly permitted ("you may re-angle, reorder, compress and
    rephrase"). So demanding exact words would corroborate almost nothing: measured, the profile
    says "platform migration" and the bullet says "Migrated", and an exact match therefore called a
    perfectly truthful bullet unverifiable. That would bury the human in noise warnings about their
    own real achievements, which is how a truth signal stops being read.
    So: numbers must match EXACTLY (a number is the thing that actually gets invented), and words
    match by a 5-character STEM, which survives ordinary morphology (migrat/ed, migrat/ion).
    """
    t = str(claim or "")
    nums = [n.rstrip(".,") for n in re.findall(r"\d[\d.,]*", t)]
    stems = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9&.+-]{3,}", t):
        lw = w.lower().strip(".+-&")
        if lw and lw not in _STOP and len(lw) >= 4:
            stems.append(lw[:5])
    return [n for n in nums if n], stems


def claim_is_corroborated(claim: str, profile_txt: str) -> bool:
    """True when the profile plainly supports the claim, i.e. the flag against it is NOISE.

    Conservative and in the SAFE direction: a claim with nothing checkable in it keeps its flag
    (unverifiable -> the human decides), and nothing here ever deletes anything. Its only power is
    to stop a reviewer's guess about a real achievement reaching the candidate as a warning.
    """
    nums, stems = _hard_tokens(claim)
    if not nums and not stems:
        return False
    hay = re.sub(r"\s+", " ", str(profile_txt or "").lower())
    for n in nums:
        # word-boundary on DIGITS: plain containment would find "4" inside "40" and corroborate an
        # invented figure against an unrelated one.
        if not re.search(r"(?<!\d)%s(?!\d)" % re.escape(n), hay):
            return False
    for st in stems:
        if st not in hay:
            return False
    return True


# --------------------------------------------------------------------------- prompts
_TRUTH = (
    "HARD RULES - these override everything else:\n"
    "1. NEVER invent, embellish or infer a fact about the candidate. Employers, job titles, dates, "
    "degrees, certifications, tools, team sizes, revenue and percentages may ONLY appear if they are "
    "present in the CANDIDATE PROFILE below. If the profile has no number for a claim, write the "
    "bullet WITHOUT a number rather than inventing one.\n"
    "2. You may re-angle, reorder, compress and rephrase what IS there so it speaks to this job.\n"
    "3. Mirror the job description's real vocabulary ONLY where it is truthfully the same thing the "
    "candidate has done. Do not adopt a JD keyword the candidate cannot back up.\n"
    "4. ATS-safe output: plain text, no tables, no columns, no graphics, no emoji, no markdown "
    "syntax inside field values.\n"
    "5. Every bullet must carry a FACT, a NUMBER or an OUTCOME. No filler, no adjectives-only lines.\n"
    "6. Output ONLY the requested JSON object. No prose before or after."
)

RESUME_SYS = ("You tailor an existing resume to a specific job. You are a truth-gated editor, not a "
              "copywriter with imagination.\n" + _TRUTH)

COVER_SYS = ("You write a concise, senior cover letter that a hiring manager will actually finish "
             "reading. Specific, no cliches, no fabrication.\n" + _TRUTH)

# Structure and length rules distilled from mainstream recruiter/executive-search guidance
# (Harvard/Yale career-office resume guides, LinkedIn Talent Solutions, Robert Half, Michael Page,
# Korn Ferry/Heidrick executive-CV advice). Where they differ (1 page vs 2) we take the senior-hire
# convention, because this product targets experienced candidates.
RESUME_RULES = (
    "RULES (follow ALL - these come from mainstream recruiter and executive-search guidance):\n"
    "1. LENGTH: 2 pages maximum for 10+ years, 1 page under 10 years. Be ruthless: the last 10-15\n"
    "   years carry detail, older roles collapse to one line each.\n"
    "2. ORDER: highlights, then summary, then experience (reverse-chronological), then skills,\n"
    "   then education. A recruiter's first screen is ~7 seconds - the top third must sell.\n"
    "3. ACHIEVEMENTS, NOT DUTIES: every bullet is an accomplishment with evidence, never a job\n"
    "   description. Start with a strong past-tense verb (Led, Built, Won, Cut, Scaled). Never use\n"
    "   'Responsible for'.\n"
    "4. QUANTIFY: at least half the bullets carry a number - EUR, percent, headcount, time saved,\n"
    "   deal size. If the profile has no number for a point, keep the point but do NOT invent one.\n"
    "5. BULLETS: 3-5 per recent role, max 2 lines each, no paragraphs.\n"
    "6. ATS: mirror the JD's exact terminology where the candidate genuinely has it. No tables,\n"
    "   columns, headers/footers, graphics or text boxes - they break ATS parsers.\n"
    "7. NO PRONOUNS, no 'References available on request'.\n"
    "8. TRUTH: every employer, title and date exactly as in the profile, in profile order. Never\n"
    "   merge, drop, re-date or promote a role. Re-angle emphasis only.\n"
    "9. NEVER DELETE AN EMPLOYER TO FIT THE PAGE LIMIT. Detailed bullets are for the recent and\n"
    "   most relevant roles; EVERY remaining employer still appears in \"earlier\" as a single\n"
    "   line (title, company, dates). A missing employer reads as an unexplained gap and gets\n"
    "   the candidate screened out - it is the worst possible failure of this document.\n"
)

COVER_RULES = (
    "RULES (mainstream recruiter guidance for cover letters):\n"
    "1. LENGTH: 250-350 words, ONE page, 3-4 short paragraphs. Never longer.\n"
    "2. OPENING: name the role and lead with the single most relevant proof point - no 'I am\n"
    "   writing to apply for'.\n"
    "3. MIDDLE: 2 paragraphs mapping the employer's top requirements to concrete, quantified\n"
    "   evidence from the profile. Show you understand THEIR problem, not your career story.\n"
    "4. CLOSE: brief, confident, a clear next step. No pleading, no salary, no apologies.\n"
    "5. NEVER repeat the resume verbatim, never invent, no generic filler ('team player',\n"
    "   'fast-paced environment', 'passionate about excellence').\n"
)


def resume_user(profile_txt: str, jd: dict) -> str:
    jd = jd or {}
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n"
        "%s\n\n"
        "TARGET JOB\n"
        "title: %s\ncompany: %s\nlocation: %s\n"
        "description:\n%s\n\n"
        "Return JSON with EXACTLY this shape:\n"
        "{\n"
        '  "summary": "3-4 sentence professional summary aimed at THIS job, true to the profile",\n'
        '  "skills": ["12-20 skills taken from the profile, ordered by relevance to the JD"],\n'
        '  "experience": [\n'
        '    {"title":"<exact title from the profile>", "company":"<exact company from the profile>",\n'
        '     "dates":"<exact dates from the profile, or empty>", "location":"<from profile, or empty>",\n'
        '     "bullets":["3-5 bullets re-angled to the JD. Same facts. Each carries a fact, number or outcome."]}\n'
        "  ],\n"
        '  "earlier": [\n'
        '    {"title":"<exact title>", "company":"<exact company>", "dates":"<exact dates>"}\n'
        '    // every OTHER employer from the profile - older or less relevant roles. ONE LINE each,\n'
        '    // no bullets. NEVER drop an employer to save space: demote it here instead.\n'
        "  ],\n"
        '  "all_employers": ["EVERY employer name that appears in the profile, in profile order"],\n'
        '  "keywords_matched": ["JD terms the candidate genuinely covers"],\n'
        '  "highlights": ["EXACTLY 3-5 career achievements, strongest first. Each is CAR: what the\n'
        '     situation/challenge was, what YOU did, and the RESULT with a number (percent, EUR, headcount,\n'
        '     time). These lead the resume - recruiters read them first. Facts only from the profile."],\n'
        '  "gaps": ["JD requirements the profile does NOT evidence - be honest, this is for the candidate only"]\n'
        "}\n"
        % (str(profile_txt or "")[:14000], jd.get("title", ""), jd.get("company", ""),
           jd.get("location", ""), (jd.get("text") or "")[:9000])) + RESUME_RULES


def cover_user(profile_txt: str, jd: dict) -> str:
    jd = jd or {}
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n%s\n\n"
        "TARGET JOB\ntitle: %s\ncompany: %s\nlocation: %s\ndescription:\n%s\n\n"
        "Return JSON with EXACTLY this shape:\n"
        "{\n"
        '  "salutation": "Hiring Team, or a named person if the JD gives one",\n'
        '  "paragraphs": [\n'
        '    "Opening: why this role at this company, and the single strongest true hook. 2-3 sentences.",\n'
        '    "Proof: the most relevant concrete evidence from the profile mapped to the top JD requirement.",\n'
        '    "Fit: what the candidate would do in the first months, grounded in what they have already done.",\n'
        '    "Close: short, direct, no grovelling."\n'
        "  ],\n"
        '  "closing": "Sincerely,"\n'
        "}\n"
        "Aim for 250-350 words in total. No sentence may contain a fact absent from the profile."
        % (str(profile_txt or "")[:9000], jd.get("title", ""), jd.get("company", ""),
           jd.get("location", ""), (jd.get("text") or "")[:6000])) + COVER_RULES


AUDIT_SYS = (
    "You are an INDEPENDENT adversarial reviewer of a tailored resume and cover letter. You did NOT "
    "write them. Your job is to find what is wrong with them, precisely and without mercy, using "
    "ONLY the candidate profile and the job description as ground truth.\n"
    "You REVIEW. You do NOT rewrite, do not produce a corrected document, and do not output any "
    "resume content. Output ONLY the requested JSON object."
)

AUDIT_USER = """CANDIDATE PROFILE (the ONLY source of truth about this person):
%(profile)s

TARGET JOB
title: %(title)s
company: %(company)s
description:
%(jd)s

THE DRAFT UNDER REVIEW (JSON):
%(draft)s

Review the draft against the profile and the job description. Report:

 * invented_facts - any employer, title, date, degree, certification, tool, team size, headcount,
   currency amount or percentage in the draft that is NOT in the profile. Quote the exact claim.
   This is the most serious category: the document goes out under the candidate's name.
 * employers_dropped - employers that appear in the profile but not anywhere in the draft's
   experience or earlier lists. A missing employer reads as an unexplained gap.
 * weak_bullets - bullets carrying no fact, no number and no outcome (a duty statement, a list of
   adjectives, or filler). Quote the bullet exactly as it appears in the draft.
 * missed_keywords - terms the job description asks for that the candidate DOES evidence in the
   profile but the draft never uses. Only terms the candidate can genuinely back up.
 * cover_issues - specific problems in the cover letter: generic filler, resume repetition,
   wrong length, weak opening, a claim absent from the profile.
 * scores - integers 0-10. Be strict: 10 means a senior recruiter would advance this candidate
   without edits.

Return ONLY this JSON:
{
  "verdict": "pass" or "revise",
  "invented_facts": [{"where": "resume.experience[0].bullets[1] or cover.paragraphs[2]",
                      "claim": "<exact quoted text>", "why": "<why the profile does not support it>"}],
  "employers_dropped": ["<employer name>"],
  "weak_bullets": [{"where": "<location>", "text": "<exact quoted bullet>", "why": "<what is missing>"}],
  "missed_keywords": ["<JD term the profile evidences but the draft omits>"],
  "cover_issues": ["<one specific problem per entry>"],
  "scores": {"truthfulness": 0, "jd_fit": 0, "impact": 0, "structure": 0, "cover_letter": 0},
  "notes": "one line, the single most important thing to fix"
}"""

REVISE_SYS = (
    "You are the author of this resume and cover letter. An INDEPENDENT reviewer has audited your "
    "draft. Apply the corrections that are correct, keep everything that was already good, and "
    "return the SAME JSON shape.\n" + _TRUTH + "\n"
    "ABSOLUTE LIMITS ON THIS EDIT:\n"
    "A. NEVER remove an employer, a role, a date or a section. If a claim is unsupported, REWRITE "
    "it without the unsupported part - do not delete the achievement.\n"
    "B. The revision must be at least as SUBSTANTIAL as the draft. Fixing a weak bullet means "
    "making it concrete, never deleting it.\n"
    "C. If you disagree with a review point because the profile DOES support the claim, keep the "
    "claim as it is. The reviewer can be wrong."
)


def revise_user(draft: dict, critique: dict, profile_txt: str, jd: dict, kind: str) -> str:
    jd = jd or {}
    return (
        "CANDIDATE PROFILE (the ONLY permitted source of facts):\n%s\n\n"
        "TARGET JOB: %s at %s\n\n"
        "YOUR DRAFT (JSON):\n%s\n\n"
        "THE INDEPENDENT REVIEW (corroborated points only - each one was checked against the "
        "profile, the job description and the draft itself):\n%s\n\n"
        "Return the COMPLETE corrected %s JSON, same keys as your draft, nothing else."
        % (str(profile_txt or "")[:12000], jd.get("title", ""), jd.get("company", ""),
           json.dumps(draft, ensure_ascii=False)[:14000],
           json.dumps(critique, ensure_ascii=False, indent=1)[:6000], kind))


# --------------------------------------------------------------------------- auditor selection
def pick_auditor(authors, chn=None, forced: str = "") -> Optional[str]:
    """A model that is NEVER an author and, where possible, never an author's VENDOR.

    An audit by the author is not a second opinion: it shares every blind spot and every failure
    mode, and a provider-wide 429 would take both down together. Returns None when no distinct
    model exists - REFUSING to audit is honest; faking independence is not.
    """
    auth = {str(a) for a in (authors if isinstance(authors, (list, tuple, set)) else [authors]) if a}
    vends = {vendor(a) for a in auth}
    chn = list(chn or chain())
    forced = str(forced or os.environ.get("JHW_TAILOR_AUDITOR", "") or "").strip()
    if forced and forced not in auth:
        return forced
    for m in chn:                                   # 1) different VENDOR, different id
        if m not in auth and vendor(m) not in vends:
            return m
    for m in chn:                                   # 2) at least a different id
        if m not in auth:
            return m
    for m in CHAIN:                                 # 3) the committed chain, outside the override
        if m not in auth and vendor(m) not in vends:
            return m
    for m in CHAIN:
        if m not in auth:
            return m
    return None


# --------------------------------------------------------------------------- bounding the audit
# A carpet-bombing auditor must not be able to condemn the document. These are the same numbers in
# spirit as the sibling project's FP-audit guardrail: an automatic process may narrow at the margin,
# it may never gut or empty the artifact.
MAX_WEAK_FRACTION = float(os.environ.get("JHW_TAILOR_MAX_WEAK", "0.6"))
MAX_INVENTED_FRACTION = float(os.environ.get("JHW_TAILOR_MAX_INVENTED", "0.5"))


def resume_bullets(resume: dict) -> list:
    """The lines the "every bullet carries a fact, a NUMBER or an OUTCOME" rule actually governs.

    MEASURED: an earlier version pooled resume bullets WITH cover-letter paragraphs and ran
    `bullet_is_weak` over the lot. A cover letter's CLOSE has no number and no outcome verb BY
    DESIGN - the recruiter rule for it is "brief, confident, a clear next step" - so a correct
    closing paragraph was corroborated as a weak bullet on every single run, and it also inflated
    the carpet-bomb denominator. Cover-letter prose has its own review channel (`cover_issues`).
    A rule must be applied to the thing it was written about.
    """
    out = []
    for e in ((resume or {}).get("experience") or []):
        if isinstance(e, dict):
            out += [str(b) for b in (e.get("bullets") or [])]
    out += [str(h) for h in ((resume or {}).get("highlights") or [])]
    return out


def _draft_bullets(resume: dict, cover: dict) -> list:
    """Everything a reviewer could quote back at us - used only to test whether a quote EXISTS."""
    return resume_bullets(resume) + [str(p) for p in ((cover or {}).get("paragraphs") or [])]


def bound_audit(audit: dict, resume: dict, cover: dict, profile_txt: str, jd: dict) -> dict:
    """Corroborate every flag DETERMINISTICALLY, then refuse the round if it would gut the document.

    THE DOCTRINE (ported from the FP auditor that once emptied a customer deck): an audit is a
    SIGNAL, not an authority. A flag is kept only where deterministic evidence agrees with it, and
    a refused flag is RECORDED rather than discarded, because the operator is entitled to see that
    the reviewer said something we chose not to act on.
    """
    a = audit if isinstance(audit, dict) else {}
    prof = str(profile_txt or "")
    jd_txt = str((jd or {}).get("text") or "") + " " + str((jd or {}).get("title") or "")
    jd_low = jd_txt.lower()
    bullets = resume_bullets(resume)                 # what the fact/number/outcome rule governs
    cover_paras = [str(x) for x in ((cover or {}).get("paragraphs") or [])]
    doc_low = json.dumps({"r": resume, "c": cover}, ensure_ascii=False).lower()
    exp_n = len([e for e in ((resume or {}).get("experience") or []) if isinstance(e, dict)])

    kept: dict = {"invented_facts": [], "employers_dropped": [], "weak_bullets": [],
                  "missed_keywords": [], "cover_issues": []}
    refused: list = []

    # 1) INVENTED FACTS. Keep the flag unless the profile PLAINLY supports the claim.
    for f in (a.get("invented_facts") or []):
        if not isinstance(f, dict):
            continue
        claim = str(f.get("claim") or "").strip()
        if not claim:
            refused.append({"kind": "invented_fact", "item": str(f)[:120],
                            "why": "the reviewer quoted no claim, so nothing can be checked"})
            continue
        if claim_is_corroborated(claim, prof):
            refused.append({"kind": "invented_fact", "item": claim[:200],
                            "why": "every checkable token of this claim IS in the profile"})
            continue
        kept["invented_facts"].append({"where": str(f.get("where") or "")[:120],
                                       "claim": claim[:400],
                                       "why": str(f.get("why") or "")[:300]})

    # 2) EMPLOYERS DROPPED. missing_employers() is the AUTHORITY; the reviewer is a backstop that
    #    can flag but not overrule it. The deterministic list is also UNIONED in, so an employer the
    #    reviewer missed is still caught.
    det = missing_employers(resume)
    det_norm = [_norm(x) for x in det]
    for e in (a.get("employers_dropped") or []):
        name = str(e or "").strip()
        if not name:
            continue
        n = _norm(name)
        if n and any(n in d or d in n for d in det_norm if d):
            if name not in kept["employers_dropped"]:
                kept["employers_dropped"].append(name)
        else:
            refused.append({"kind": "employer_dropped", "item": name[:120],
                            "why": "the deterministic employer inventory shows this employer IS in "
                                   "the document"})
    for d in det:
        if d not in kept["employers_dropped"]:
            kept["employers_dropped"].append(d)

    # 3) WEAK BULLETS. The quoted text must actually EXIST in the draft (a flag about a bullet that
    #    is not there is noise) AND the deterministic weakness test must agree.
    for w in (a.get("weak_bullets") or []):
        if not isinstance(w, dict):
            continue
        txt = str(w.get("text") or "").strip()
        if not txt:
            refused.append({"kind": "weak_bullet", "item": str(w)[:120],
                            "why": "the reviewer quoted no text"})
            continue
        hit = next((b for b in bullets if txt[:60].lower() in b.lower()), "")
        if not hit:
            if any(txt[:60].lower() in c.lower() for c in cover_paras):
                refused.append({"kind": "weak_bullet", "item": txt[:200],
                                "why": "this is cover-letter prose, reviewed under cover_issues - "
                                       "a letter's opening and close carry no number by design"})
            else:
                refused.append({"kind": "weak_bullet", "item": txt[:200],
                                "why": "this text does not appear in the draft"})
            continue
        if not bullet_is_weak(hit):
            refused.append({"kind": "weak_bullet", "item": txt[:200],
                            "why": "the bullet carries a number or an outcome verb"})
            continue
        kept["weak_bullets"].append({"where": str(w.get("where") or "")[:120], "text": txt[:400],
                                     "why": str(w.get("why") or "")[:300]})

    # 4) MISSED KEYWORDS. Three deterministic conditions, all required: the JD asks for it, the
    #    PROFILE evidences it, and the draft does not use it. That is what makes this field
    #    trustworthy enough to act on - a keyword the candidate cannot back up must never be added.
    prof_low = prof.lower()
    for k in (a.get("missed_keywords") or []):
        kw = str(k or "").strip()
        if not kw or len(kw) < 2:
            continue
        low = kw.lower()
        if low not in jd_low:
            refused.append({"kind": "missed_keyword", "item": kw[:80],
                            "why": "not present in the job description"})
        elif low not in prof_low:
            refused.append({"kind": "missed_keyword", "item": kw[:80],
                            "why": "the profile does not evidence it - adding it would be invention"})
        elif low in doc_low:
            refused.append({"kind": "missed_keyword", "item": kw[:80],
                            "why": "the draft already uses it"})
        elif kw not in kept["missed_keywords"]:
            kept["missed_keywords"].append(kw)

    kept["cover_issues"] = [str(x)[:300] for x in (a.get("cover_issues") or []) if str(x).strip()][:8]

    # 5) THE GUTTING GUARD. If the reviewer condemned most of the document, the likelier explanation
    #    is the reviewer, not the document. Keep the draft, record why, and change nothing.
    n_bul = len(bullets)
    weak_frac = (len(kept["weak_bullets"]) / float(n_bul)) if n_bul else 0.0
    inv_frac = (len(kept["invented_facts"]) / float(exp_n)) if exp_n else 0.0
    gut = ""
    if n_bul and weak_frac > MAX_WEAK_FRACTION:
        gut = ("the reviewer condemned %d of %d bullets (%.0f percent, cap %.0f percent)"
               % (len(kept["weak_bullets"]), n_bul, weak_frac * 100, MAX_WEAK_FRACTION * 100))
    elif exp_n and inv_frac > MAX_INVENTED_FRACTION:
        gut = ("the reviewer called %d claims invented against only %d roles (cap %.0f percent)"
               % (len(kept["invented_facts"]), exp_n, MAX_INVENTED_FRACTION * 100))
    if gut:
        refused.append({"kind": "whole_audit", "item": "round refused", "why": gut})
        return {"actionable": False, "refused_all": True, "refused_reason": gut,
                "kept": {"invented_facts": [], "employers_dropped": kept["employers_dropped"],
                         "weak_bullets": [], "missed_keywords": [], "cover_issues": []},
                "refused": refused, "counts": _counts(kept)}

    actionable = any(kept[k] for k in ("invented_facts", "employers_dropped", "weak_bullets",
                                       "missed_keywords", "cover_issues"))
    return {"actionable": bool(actionable), "refused_all": False, "refused_reason": "",
            "kept": kept, "refused": refused, "counts": _counts(kept)}


def _counts(kept: dict) -> dict:
    return {k: len(kept.get(k) or []) for k in ("invented_facts", "employers_dropped",
                                                "weak_bullets", "missed_keywords", "cover_issues")}


def _scores(audit: dict) -> dict:
    s = (audit or {}).get("scores")
    out = {}
    if isinstance(s, dict):
        for k in ("truthfulness", "jd_fit", "impact", "structure", "cover_letter"):
            v = s.get(k)
            try:
                out[k] = max(0, min(10, int(float(v))))
            except (TypeError, ValueError):
                continue
    return out


# --------------------------------------------------------------------------- revision verification
def revision_ok(kind: str, before: dict, after: Any, profile: Any = None) -> tuple:
    """May this revision replace the draft? Returns (ok, reason_if_not).

    THIS is the only place the document can actually change, so this is where the "never gut it"
    rule has to live. A revision is refused when it:
      * fails the contract or the depth floor;
      * loses more than 20 percent of the prose (fixing a weak bullet means making it concrete,
        never deleting it);
      * loses an employer that was rendered before.
    Refusing keeps the draft, which is always a legitimate document. Accepting a gutted rewrite is
    not recoverable once it has been rendered and sent.
    """
    if not isinstance(after, dict):
        return False, "revision is not a JSON object"
    if kind == "resume":
        ok, why = contract_ok_resume(after)
        if not ok:
            return False, "revision fails the contract: %s" % why
        d0, d1 = resume_depth(before), resume_depth(after)
        if d1 < int(d0 * 0.8):
            return False, ("revision is thinner than the draft (%d -> %d chars, more than 20 "
                           "percent lost)" % (d0, d1))
        b1 = {_norm(x) for x in rendered_employers(after) if _norm(x)}
        lost = []
        for c in rendered_employers(before):
            n = _norm(c)
            if not n or any(n in y or y in n for y in b1):
                continue
            # MEASURED, END TO END: an earlier version protected EVERY employer in the draft. The
            # author had invented "Google", the auditor caught it, and the corrected revision - the
            # one that REMOVED the fabrication and restored the real employer - was rejected for
            # "dropping" Google. The guard meant to protect the candidate shipped a fabricated
            # employer instead, and kept the real one missing. So the rule is: a revision may not
            # drop an employer THE PROFILE SUPPORTS. Removing one the profile does not support is
            # the entire point of the correction.
            # With no profile to check against, EVERY employer is protected (fail closed).
            # A BLANK profile is the same as no profile: we cannot tell, so everything is
            # protected. Testing only `is None` left a fail-OPEN hole - an empty profile string
            # would have let a revision drop every employer in the document.
            if profile is None or not str(profile).strip() or employer_in_profile(c, profile):
                lost.append(c)
        if lost:
            return False, "revision DROPPED employer(s): %s" % ", ".join(sorted(set(lost))[:6])
        return True, ""
    ok, why = contract_ok_cover(after)
    if not ok:
        return False, "revision fails the contract: %s" % why
    d0, d1 = cover_depth(before), cover_depth(after)
    if d1 < int(d0 * 0.8):
        return False, ("revision is thinner than the draft (%d -> %d chars, more than 20 percent "
                       "lost)" % (d0, d1))
    return True, ""


# --------------------------------------------------------------------------- the consensus run
ROUNDS = int(os.environ.get("JHW_TAILOR_ROUNDS", "2"))
TIMEOUT = float(os.environ.get("JHW_TAILOR_TIMEOUT_S", "150"))
MAX_TOK = {"resume": 6000, "cover": 2000, "audit": 3000, "revise": 6500}


async def _draft_one(kind: str, model: str, profile_txt: str, jd: dict, poster, timeout) -> dict:
    """One author attempt for one document. Never raises: the chain walker reads the report."""
    t0 = time.time()
    rep = {"model": model, "doc": kind, "ok": False, "why": "", "depth": 0,
           "ms": 0, "data": None}
    try:
        sysmsg = RESUME_SYS if kind == "resume" else COVER_SYS
        usermsg = (resume_user(profile_txt, jd) if kind == "resume"
                   else cover_user(profile_txt, jd))
        txt, usage, _fin = await call_model(model, sysmsg, usermsg, max_tokens=MAX_TOK[kind],
                                           timeout=timeout, poster=poster)
        raw = json_loose(txt)
        d = normalise_resume(raw) if kind == "resume" else normalise_cover(raw)
        tok = (usage or {}).get("completion_tokens")
        ok, why = (contract_ok_resume(d, tok) if kind == "resume" else contract_ok_cover(d, tok))
        rep.update({"ok": ok, "why": why,
                    "depth": resume_depth(d) if kind == "resume" else cover_depth(d),
                    "data": d if ok else None})
    except Exception as e:
        rep["why"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    rep["ms"] = int((time.time() - t0) * 1000)
    if not rep["ok"]:
        print("[tailor] %s draft REJECTED by %s: %s" % (kind, model, rep["why"]), file=sys.stderr)
    return rep


async def draft(profile_txt: str, jd: dict, *, chn=None, poster=None, timeout=None) -> dict:
    """Walk the chain until each document passes the contract AND the depth floor.

    A THIN draft is not accepted and rendered - it is a quality failure to report and a trigger to
    try the NEXT MODEL. Both documents are requested from the same model concurrently; if only one
    passes, the next model in the chain is asked for the one still missing, so a good resume is
    never thrown away because the cover letter came back empty.
    """
    chn = list(chn or chain())
    timeout = TIMEOUT if timeout is None else timeout
    out = {"resume": None, "cover": None, "authors": {"resume": None, "cover": None},
           "attempts": [], "errors": {}}
    for model in chn:
        need = [k for k in ("resume", "cover") if out[k] is None]
        if not need:
            break
        reps = await asyncio.gather(*[
            _draft_one(k, model, profile_txt, jd, poster, timeout) for k in need])
        for rep in reps:
            out["attempts"].append({k: rep[k] for k in ("model", "doc", "ok", "why", "depth", "ms")})
            if rep["ok"]:
                out[rep["doc"]] = rep["data"]
                out["authors"][rep["doc"]] = rep["model"]
            else:
                out["errors"][rep["doc"]] = rep["why"]
    for k in ("resume", "cover"):
        if out[k] is not None:
            out["errors"].pop(k, None)
    return out


async def audit_once(resume: dict, cover: dict, profile_txt: str, jd: dict, auditor: str, *,
                     poster=None, timeout=None) -> dict:
    """One adversarial review of both documents in ONE call. Never raises."""
    timeout = TIMEOUT if timeout is None else timeout
    draft_json = json.dumps({"resume": resume, "cover": cover}, ensure_ascii=False)[:16000]
    user = AUDIT_USER % {"profile": str(profile_txt or "")[:12000],
                         "title": (jd or {}).get("title", ""),
                         "company": (jd or {}).get("company", ""),
                         "jd": ((jd or {}).get("text") or "")[:7000],
                         "draft": draft_json}
    try:
        txt, _usage, _fin = await call_model(auditor, AUDIT_SYS, user, max_tokens=MAX_TOK["audit"],
                                            timeout=timeout, poster=poster)
        j = json_loose(txt)
        if not isinstance(j, dict) or not j:
            return {"_error": "auditor returned no usable JSON"}
        return j
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, str(e)[:200])}


async def _revise_one(kind: str, model: str, before: dict, critique: dict, profile_txt: str,
                      jd: dict, poster, timeout) -> dict:
    rep = {"doc": kind, "model": model, "applied": False, "why": ""}
    try:
        txt, _u, _f = await call_model(model, REVISE_SYS,
                                       revise_user(before, critique, profile_txt, jd, kind),
                                       max_tokens=MAX_TOK["revise"], timeout=timeout, poster=poster)
        raw = json_loose(txt)
        # the model may answer {"resume": {...}} or the bare document
        cand = raw.get(kind) if isinstance(raw.get(kind), dict) else raw
        cand = normalise_resume(cand) if kind == "resume" else normalise_cover(cand)
        ok, why = revision_ok(kind, before, cand, profile=profile_txt)
        if ok:
            rep.update({"applied": True, "data": cand})
        else:
            rep["why"] = why
    except Exception as e:
        rep["why"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    if not rep["applied"]:
        print("[tailor] %s revision REJECTED (%s) - the draft stands" % (kind, rep["why"]),
              file=sys.stderr)
    return rep


def _critique_for(kind: str, bound: dict) -> dict:
    """Split the corroborated critique into the part that belongs to each document."""
    k = bound.get("kept") or {}
    if kind == "resume":
        return {"invented_facts": [x for x in k.get("invented_facts") or []
                                   if not str(x.get("where", "")).lower().startswith("cover")],
                "employers_dropped": k.get("employers_dropped") or [],
                "weak_bullets": [x for x in k.get("weak_bullets") or []
                                 if not str(x.get("where", "")).lower().startswith("cover")],
                "missed_keywords": k.get("missed_keywords") or []}
    return {"invented_facts": [x for x in k.get("invented_facts") or []
                               if str(x.get("where", "")).lower().startswith("cover")],
            "cover_issues": k.get("cover_issues") or [],
            "missed_keywords": k.get("missed_keywords") or []}


def _has_work(kind: str, crit: dict) -> bool:
    return any(crit.get(k) for k in crit)


async def tailor(profile_txt: str, jd: dict, *, poster=None, chn=None, rounds=None,
                 timeout=None) -> dict:
    """AUTHOR -> AUDITOR -> AUTHOR REVISES, bounded. The whole consensus in one call.

    Returns the two documents plus a full, honest record of how they got there. Nothing is hidden:
    every rejected draft, every refused audit flag and every rejected revision is in the result so
    the manifest (and therefore the UI) can show it.
    """
    rounds = ROUNDS if rounds is None else int(rounds)
    rounds = max(0, min(3, rounds))                  # bounded for latency; 2 is the shipped default
    timeout = TIMEOUT if timeout is None else timeout
    t0 = time.time()

    d = await draft(profile_txt, jd, chn=chn, poster=poster, timeout=timeout)
    resume, cover = d["resume"], d["cover"]
    authors = d["authors"]
    audit_rec = {"auditor": None, "auditor_vendor": None, "authors": dict(authors),
                 "rounds": [], "scores": {}, "refused": False, "notes": "", "skipped": ""}

    if resume is None and cover is None:
        return {"resume": None, "cover": None, "authors": authors, "attempts": d["attempts"],
                "audit": audit_rec, "audit_rounds": 0, "audit_refused": False,
                "quality": _quality(resume, cover), "errors": d["errors"],
                "elapsed_ms": int((time.time() - t0) * 1000)}

    auditor = pick_auditor([a for a in authors.values() if a], chn)
    if not auditor:
        # REFUSE rather than fake independence. An audit by the author is not a second opinion.
        audit_rec["skipped"] = ("no model distinct from the author(s) %s was available - refusing "
                                "to audit rather than fake an independent review"
                                % ",".join(sorted({a for a in authors.values() if a})))
        print("[tailor] %s" % audit_rec["skipped"], file=sys.stderr)
        rounds = 0
    else:
        audit_rec["auditor"] = auditor
        audit_rec["auditor_vendor"] = vendor(auditor)

    for rnd in range(1, rounds + 1):
        raw = await audit_once(resume or {}, cover or {}, profile_txt, jd, auditor,
                               poster=poster, timeout=timeout)
        if raw.get("_error"):
            audit_rec["rounds"].append({"round": rnd, "error": raw["_error"]})
            print("[tailor] audit round %d failed: %s" % (rnd, raw["_error"]), file=sys.stderr)
            break
        bound = bound_audit(raw, resume or {}, cover or {}, profile_txt, jd)
        sc = _scores(raw)
        if sc:
            audit_rec["scores"] = sc
        rec = {"round": rnd, "verdict": str(raw.get("verdict") or "")[:20],
               "notes": str(raw.get("notes") or "")[:300], "scores": sc,
               "kept": bound["kept"], "counts": bound["counts"], "refused": bound["refused"],
               "refused_all": bound["refused_all"], "refused_reason": bound["refused_reason"],
               "revisions": []}
        audit_rec["notes"] = rec["notes"] or audit_rec["notes"]
        if bound["refused_all"]:
            audit_rec["refused"] = True
            audit_rec["rounds"].append(rec)
            print("[tailor] audit round %d REFUSED: %s" % (rnd, bound["refused_reason"]),
                  file=sys.stderr)
            break
        if not bound["actionable"]:
            rec["verdict"] = rec["verdict"] or "pass"
            audit_rec["rounds"].append(rec)
            break
        jobs = []
        for kind, cur in (("resume", resume), ("cover", cover)):
            if cur is None:
                continue
            crit = _critique_for(kind, bound)
            if not _has_work(kind, crit):
                continue
            jobs.append(_revise_one(kind, authors.get(kind) or auditor, cur, crit, profile_txt,
                                    jd, poster, timeout))
        for r in (await asyncio.gather(*jobs) if jobs else []):
            rec["revisions"].append({k: r[k] for k in ("doc", "model", "applied", "why")})
            if r["applied"]:
                if r["doc"] == "resume":
                    resume = r["data"]
                else:
                    cover = r["data"]
        audit_rec["rounds"].append(rec)
        if not any(x["applied"] for x in rec["revisions"]):
            break                                    # nothing changed -> another round cannot help

    return {"resume": resume, "cover": cover, "authors": authors, "attempts": d["attempts"],
            "audit": audit_rec, "audit_rounds": len(audit_rec["rounds"]),
            "audit_refused": bool(audit_rec["refused"]),
            "quality": _quality(resume, cover), "errors": d["errors"],
            "elapsed_ms": int((time.time() - t0) * 1000)}


def _quality(resume, cover) -> dict:
    rd = resume_depth(resume or {})
    cd = cover_depth(cover or {})
    bullets = resume_bullets(resume or {})           # NOT cover paragraphs - see resume_bullets()
    weak = [b for b in bullets if bullet_is_weak(b)]
    return {"resume_chars": rd, "cover_chars": cd,
            "resume_floor": MIN_RESUME, "cover_floor": MIN_COVER,
            "resume_thin": bool(resume) and rd < MIN_RESUME,
            "cover_thin": bool(cover) and cd < MIN_COVER,
            "bullets": len(bullets), "bullets_without_number_or_outcome": len(weak)}
