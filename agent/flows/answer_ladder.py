#!/usr/bin/env python3
"""Answer ladder for closed-list and short-text ATS questions.

Order (cheapest / most certain first):
  1. Profile / screening_defaults (candidate.md)
  2. Deterministic class rules (COMPLIANCE_NO → No, WORK_AUTH → Yes, …)
  3. Learned answers from prior successful submits
  4. 3-LLM panel over the REAL option list (quorum of 2)
  5. Human (Telegram) — only if still empty and required

A model never invents an option that is not on the list.
Sensitive EEO fields never come from a model — only profile or human.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from question_class import (
        COMPLIANCE_NO,
        COMPLIANCE_YES,
        CONDITIONAL_DETAIL,
        CONSENT,
        EEO_SENSITIVE,
        HOW_HEARD,
        SPONSORSHIP,
        WORK_AUTH,
        classify,
        is_yes_no_options,
        normalize_question,
        pick_yes_no,
    )
except ImportError:
    from flows.question_class import (  # type: ignore
        COMPLIANCE_NO,
        COMPLIANCE_YES,
        CONDITIONAL_DETAIL,
        CONSENT,
        EEO_SENSITIVE,
        HOW_HEARD,
        SPONSORSHIP,
        WORK_AUTH,
        classify,
        is_yes_no_options,
        normalize_question,
        pick_yes_no,
    )

# Prefer shared learned store next to agent out/
_LEARNED_PATHS = [
    Path(os.getenv("JHW_LEARNED_ANSWERS", "")),
    Path("/agent/out/learned_answers.json"),
    Path("out/learned_answers.json"),
    Path(__file__).resolve().parent.parent / "out" / "learned_answers.json",
]


def _log(msg: str, log: Optional[Callable] = None) -> None:
    line = f"    [ladder] {msg}"
    if log:
        try:
            log(line)
        except TypeError:
            log(line)
    else:
        print(line, flush=True)


def load_profile_facts(data: dict | None = None, candidate_path: str | None = None) -> dict:
    """Flatten screening_defaults + basics into a simple key→value map."""
    facts: dict[str, str] = {}
    data = data or {}

    basics = data.get("basics") or data.get("identity") or {}
    if isinstance(basics, dict):
        for k, v in basics.items():
            if v is not None and str(v).strip():
                facts[str(k).lower()] = str(v).strip()

    sd = data.get("screening_defaults") or {}
    if isinstance(sd, dict):
        for k, v in sd.items():
            if v is not None and str(v).strip():
                facts[str(k).lower().replace("_", " ")] = str(v).strip()
                facts[str(k).lower()] = str(v).strip()

    # candidate.md fallback
    paths = []
    if candidate_path:
        paths.append(Path(candidate_path))
    paths += [
        Path("/agent/candidate.md"),
        Path("candidate.md"),
        Path(__file__).resolve().parent.parent / "candidate.md",
    ]
    for p in paths:
        try:
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            # screening_defaults block
            if "## screening_defaults" in text:
                block = text.split("## screening_defaults", 1)[1]
                block = re.split(r"\n##\s+", block)[0]
                for line in block.splitlines():
                    m = re.match(r"^\s*-\s*([^:]+):\s*[\"']?(.*?)[\"']?\s*$", line)
                    if not m:
                        continue
                    k = m.group(1).strip().lower()
                    v = m.group(2).strip()
                    if v and not v.startswith("#"):
                        facts[k] = v
                        facts[k.replace(" ", "_")] = v
            # identity / contact snippets
            for key, rx in (
                ("full_name", r"full_name:\s*(.+)"),
                ("email", r"email:\s*(\S+@\S+)"),
                ("phone_national", r"phone_national:\s*[\"']?(\d+)"),
                ("country", r"country:\s*[\"']?([A-Za-z ]+)"),
                ("how_did_you_hear", r"how_did_you_hear:\s*[\"']?([^\"'\n#]+)"),
            ):
                m = re.search(rx, text, re.I)
                if m and key not in facts:
                    facts[key] = m.group(1).strip().strip("\"'")
            break
        except Exception:
            continue
    return facts


def _truthy_yes(val: str) -> bool:
    return bool(re.match(r"^\s*(yes|true|y|1)\b", str(val), re.I))


def _truthy_no(val: str) -> bool:
    return bool(re.match(r"^\s*(no|false|n|0)\b", str(val), re.I))


def profile_answer(question: str, cls: str, options: list, facts: dict) -> Optional[str]:
    """Map profile facts to an answer when possible."""
    q = (question or "").lower()
    # Direct key hits in facts
    for k, v in facts.items():
        if len(k) < 4:
            continue
        if k in q or (len(k) > 8 and any(w in q for w in k.split() if len(w) > 4)):
            if options and is_yes_no_options(options):
                if _truthy_yes(v):
                    return pick_yes_no(options, True)
                if _truthy_no(v):
                    return pick_yes_no(options, False)
            if options:
                for o in options:
                    if str(v).lower() in str(o).lower() or str(o).lower() in str(v).lower():
                        return str(o)
            return str(v)

    # Class-specific profile keys
    if cls == WORK_AUTH:
        for k in ("work authorisation", "work authorization", "legally authorised to work",
                  "legally authorized to work", "work_authorized"):
            if k in facts:
                return pick_yes_no(options, _truthy_yes(facts[k])) if options else (
                    "Yes" if _truthy_yes(facts[k]) else "No"
                )
        # default authorized if country is DE/EU and no contradiction
        return pick_yes_no(options, True) if options else "Yes"

    if cls == SPONSORSHIP:
        for k in ("require sponsorship", "visa sponsorship", "requires_visa_sponsorship",
                  "needs_sponsorship"):
            if k in facts:
                return pick_yes_no(options, _truthy_yes(facts[k])) if options else (
                    "Yes" if _truthy_yes(facts[k]) else "No"
                )
        return pick_yes_no(options, False) if options else "No"

    if cls == HOW_HEARD:
        for k in ("how did you hear about us", "how_did_you_hear", "source"):
            if k in facts:
                want = facts[k]
                if options:
                    for o in options:
                        if want.lower() in str(o).lower() or str(o).lower() in want.lower():
                            return str(o)
                    # LinkedIn variants
                    if re.search(r"linkedin", want, re.I):
                        for o in options:
                            if re.search(r"linkedin", str(o), re.I):
                                return str(o)
                return want

    if cls == COMPLIANCE_NO:
        for k in ("former_employee_of_company", "worked_for_company_before",
                  "previously_applied", "current_employee_of_company",
                  "bound_by_noncompete_or_confidentiality", "non_compete_restrictions"):
            if k in facts and _truthy_yes(facts[k]):
                return pick_yes_no(options, True) if options else "Yes"
        return pick_yes_no(options, False) if options else "No"

    if cls == COMPLIANCE_YES:
        return pick_yes_no(options, True) if options else "Yes"

    if cls == EEO_SENSITIVE:
        # Only from explicit profile keys — never invent
        mapping = [
            (r"gender|\bsex\b", ("legal gender", "gender")),
            (r"veteran", ("protected veteran", "veteran status")),
            (r"disability", ("disability",)),
            (r"hispanic|latino", ("hispanic or latino",)),
            (r"race|ethnicity", ("race", "ethnicity")),
            (r"pronoun", ("pronoun",)),
        ]
        for rx, keys in mapping:
            if re.search(rx, q, re.I):
                for k in keys:
                    if k in facts:
                        val = facts[k]
                        if options:
                            for o in options:
                                if val.lower() in str(o).lower() or str(o).lower() in val.lower():
                                    return str(o)
                        return val
        return None  # force human / skip model

    if cls == CONDITIONAL_DETAIL:
        return "N/A — answered No to the question above."

    if cls == CONSENT:
        return pick_yes_no(options, True) if options else "Yes"

    return None


def class_rule_answer(cls: str, options: list) -> Optional[str]:
    """Deterministic defaults when profile had nothing specific."""
    if cls == COMPLIANCE_NO:
        return pick_yes_no(options, False) if options else "No"
    if cls == COMPLIANCE_YES:
        return pick_yes_no(options, True) if options else "Yes"
    if cls == WORK_AUTH:
        return pick_yes_no(options, True) if options else "Yes"
    if cls == SPONSORSHIP:
        return pick_yes_no(options, False) if options else "No"
    if cls == CONDITIONAL_DETAIL:
        return "N/A — answered No to the question above."
    if cls == CONSENT:
        return pick_yes_no(options, True) if options else "Yes"
    return None


def _learned_path() -> Optional[Path]:
    for p in _LEARNED_PATHS:
        if not p or str(p) in ("", "."):
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return None


def load_learned() -> dict:
    p = _learned_path()
    if not p or not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_learned(key: str, answer: str, meta: dict | None = None) -> None:
    p = _learned_path()
    if not p:
        return
    data = load_learned()
    data[key] = {"answer": answer, **(meta or {})}
    try:
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def learned_answer(question: str, options: list) -> Optional[str]:
    store = load_learned()
    key = normalize_question(question)
    hit = store.get(key) or store.get(classify(question) + "::" + key[:80])
    if not hit:
        # class-level reuse for compliance
        cls = classify(question)
        for k, v in store.items():
            if k.startswith(cls + "::") and isinstance(v, dict) and v.get("answer"):
                ans = str(v["answer"])
                if options:
                    for o in options:
                        if ans.lower() == str(o).lower():
                            return str(o)
                else:
                    return ans
        return None
    ans = hit.get("answer") if isinstance(hit, dict) else str(hit)
    if not ans:
        return None
    if options:
        for o in options:
            if str(ans).lower() == str(o).lower() or str(ans).lower() in str(o).lower():
                return str(o)
        return None  # learned value not in this tenant's list
    return str(ans)


async def panel_answer(
    question: str,
    options: list,
    facts: dict,
    log: Optional[Callable] = None,
) -> Optional[str]:
    """3-LLM quorum over real options only. EEO_SENSITIVE is refused."""
    cls = classify(question)
    if cls == EEO_SENSITIVE:
        _log("panel refused EEO_SENSITIVE (profile or human only)", log)
        return None
    if not options:
        return None
    try:
        import choose as CH
        facts_txt = "\n".join(f"{k}: {v}" for k, v in list(facts.items())[:40])
        # Prefer choose.panel if present
        if hasattr(CH, "panel"):
            r = await CH.panel(question, list(options), facts=facts_txt, log=log or print)
            val = (r or {}).get("value") or ""
            return val or None
    except Exception as e:
        _log(f"choose.panel unavailable ({type(e).__name__})", log)

    # Lightweight inline panel via escalate-style proxy
    try:
        import asyncio
        import httpx

        proxy = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
        token = os.getenv("AGENT_PROXY_TOKEN", "none")
        models = [m.strip() for m in os.getenv(
            "JHW_PANEL", "jhw-answer,jhw-answer2,jhw-answer3").split(",") if m.strip()]
        numbered = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
        facts_txt = "\n".join(f"{k}: {v}" for k, v in list(facts.items())[:30])
        prompt = (
            f"CANDIDATE FACTS:\n{facts_txt}\n\n"
            f"QUESTION: {question}\n\nOPTIONS:\n{numbered}\n\n"
            f"Reply with ONLY the number of the best option (1-{len(options)}), or NONE."
        )

        async def one(model: str) -> str:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(
                    f"{proxy}/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": model,
                        "temperature": 0.0,
                        "max_tokens": 8,
                        "messages": [
                            {"role": "system", "content": "Pick one option number only."},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                r.raise_for_status()
                return (r.json()["choices"][0]["message"].get("content") or "").strip()

        raws = await asyncio.gather(*[one(m) for m in models], return_exceptions=True)
        votes: dict[str, int] = {}
        for raw in raws:
            if isinstance(raw, Exception):
                continue
            m = re.search(r"\b(\d+)\b", str(raw))
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(options):
                val = str(options[idx])
                votes[val] = votes.get(val, 0) + 1
        if not votes:
            return None
        best, n = max(votes.items(), key=lambda kv: kv[1])
        if n >= 2:
            _log(f"panel quorum {n} -> {best[:40]!r}", log)
            return best
        _log(f"panel no quorum {votes}", log)
        return None
    except Exception as e:
        _log(f"panel error {type(e).__name__}", log)
        return None


async def resolve(
    question: str,
    options: list | None = None,
    data: dict | None = None,
    *,
    required: bool = True,
    ask_human: Any = None,
    log: Optional[Callable] = None,
) -> dict:
    """Run the full ladder. Returns {answer, source, class}."""
    options = list(options or [])
    cls = classify(question)
    facts = load_profile_facts(data)
    _log(f"Q class={cls} required={required} opts={len(options)} | {question[:70]!r}", log)

    # 1) Profile
    ans = profile_answer(question, cls, options, facts)
    if ans:
        _log(f"source=profile -> {ans[:50]!r}", log)
        return {"answer": ans, "source": "profile", "class": cls}

    # 2) Class rule
    ans = class_rule_answer(cls, options)
    if ans:
        _log(f"source=rule -> {ans[:50]!r}", log)
        return {"answer": ans, "source": "rule", "class": cls}

    # 3) Learned
    ans = learned_answer(question, options)
    if ans:
        _log(f"source=learned -> {ans[:50]!r}", log)
        return {"answer": ans, "source": "learned", "class": cls}

    # 4) Panel (not for EEO)
    if options and cls != EEO_SENSITIVE:
        ans = await panel_answer(question, options, facts, log=log)
        if ans:
            save_learned(
                normalize_question(question),
                ans,
                {"class": cls, "source": "panel"},
            )
            return {"answer": ans, "source": "panel", "class": cls}

    # 5) Human only if required
    if required and ask_human:
        try:
            prompt = question
            if options:
                prompt += "\nOptions: " + " | ".join(str(o) for o in options[:12])
            reply = await ask_human(prompt)
            if reply and str(reply).strip():
                ans = str(reply).strip()
                if options:
                    for o in options:
                        if ans.lower() in str(o).lower() or str(o).lower() in ans.lower():
                            ans = str(o)
                            break
                save_learned(normalize_question(question), ans, {"class": cls, "source": "human"})
                _log(f"source=human -> {ans[:50]!r}", log)
                return {"answer": ans, "source": "human", "class": cls}
        except Exception as e:
            _log(f"human ask failed {type(e).__name__}", log)

    _log("source=none", log)
    return {"answer": "", "source": "none", "class": cls}
