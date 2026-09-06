#!/usr/bin/env python3
"""Classify ATS screening questions by TEXT, not by control name or employer.

Used by answer_ladder and workday_smart so every Workday / Greenhouse / Ashby
tenant shares one policy. New employers do not need new if/else trees.
"""
from __future__ import annotations

import re
from typing import Optional

# Classes the ladder understands.
IDENTITY = "IDENTITY"
WORK_AUTH = "WORK_AUTH"
SPONSORSHIP = "SPONSORSHIP"
COMPLIANCE_NO = "COMPLIANCE_NO"      # prefer No for this candidate unless profile overrides
COMPLIANCE_YES = "COMPLIANCE_YES"    # prefer Yes (e.g. eligible to work when authorized)
HOW_HEARD = "HOW_HEARD"
LOCATION = "LOCATION"
EXPERIENCE = "EXPERIENCE"
EEO_SENSITIVE = "EEO_SENSITIVE"      # gender, race, veteran, disability — profile or human only
CONSENT = "CONSENT"
CONDITIONAL_DETAIL = "CONDITIONAL_DETAIL"  # "if you answered yes, provide further detail"
OPEN_TEXT = "OPEN_TEXT"
UNKNOWN = "UNKNOWN"

# (class, compiled regex) — first match wins. Order matters.
_RULES: list[tuple[str, re.Pattern]] = [
    (CONDITIONAL_DETAIL, re.compile(
        r"if you answered yes|further detail|further information|"
        r"discuss with your recruiter|please explain|please describe|"
        r"provide (more |additional )?detail",
        re.I,
    )),
    (HOW_HEARD, re.compile(
        r"how did you hear|where did you hear|source of this|how did you find|"
        r"how were you referred",
        re.I,
    )),
    (WORK_AUTH, re.compile(
        r"legally (eligible|authorized|authorised) to work|"
        r"authorized to work|authorised to work|right to work|"
        r"work authori[sz]ation|eligible to work in",
        re.I,
    )),
    (SPONSORSHIP, re.compile(
        r"require\s+(visa\s+)?sponsorship|need(s)? sponsorship|"
        r"visa sponsorship|immigration sponsorship|sponsor you",
        re.I,
    )),
    (COMPLIANCE_NO, re.compile(
        r"previously (been )?worked for (this|the) (organization|company|employer)|"
        r"former employee|current employee of|"
        r"previously been considered for a role|"
        r"worked for .{0,40} before|"
        r"family member who is a (health care|healthcare) professional|"
        r"government official|"
        r"connected in any way to a health.?care professional|"
        r"uses .{0,20} products or services|"
        r"related by blood, adoption, or marriage|"
        r"related to a current employee|"
        r"non-?compete|"
        r"confidentiality agreement|"
        r"convicted of a (felony|crime)|criminal (record|conviction)|"
        r"conflict of interest",
        re.I,
    )),
    (COMPLIANCE_YES, re.compile(
        r"willing to (travel|accept travel)|"
        r"agree to (the )?terms|acknowledge|certify that the information|"
        r"I agree|consent to",
        re.I,
    )),
    (EEO_SENSITIVE, re.compile(
        r"\bgender\b|\bsex\b|hispanic|latino|race|ethnicity|veteran|"
        r"disability|pronoun|sexual orientation|lgbt",
        re.I,
    )),
    (LOCATION, re.compile(
        r"\bcountry\b|\bcity\b|postal|zip code|state/province|based in|"
        r"willing to relocate|location preference",
        re.I,
    )),
    (EXPERIENCE, re.compile(
        r"years of (experience|exp)|how many years|"
        r"\bSAP\b|S/4HANA|salesforce|leadership|managed a team|"
        r"highest level of education|degree",
        re.I,
    )),
    (IDENTITY, re.compile(
        r"first name|last name|given name|family name|full name|"
        r"e-?mail|phone number|linkedin",
        re.I,
    )),
    (OPEN_TEXT, re.compile(
        r"why (do you want|are you interested)|tell us about|"
        r"cover letter|motivation|what interests you",
        re.I,
    )),
]


def classify(question: str) -> str:
    """Return a class constant for this question text."""
    q = (question or "").strip()
    if not q or len(q) < 3:
        return UNKNOWN
    # Generic control labels are not questions
    if re.match(r"^(select one|yes|no|required)(\s|$)", q, re.I):
        return UNKNOWN
    for cls, rx in _RULES:
        if rx.search(q):
            return cls
    return UNKNOWN


def normalize_question(question: str) -> str:
    """Normalize for learned-store keys (employer-agnostic where possible)."""
    q = (question or "").lower()
    q = re.sub(r"\s+", " ", q).strip()
    # Strip employer-specific names for compliance patterns
    q = re.sub(r"\bwith s\s*&\s*n\b", "with the company", q)
    q = re.sub(r"\b(smith\s*\+?\s*nephew|intive|accenture|cohere)\b", "the company", q)
    q = re.sub(r"[*：:]+$", "", q).strip()
    return q[:160]


def is_yes_no_options(options: list) -> bool:
    if not options or len(options) > 6:
        return False
    joined = " | ".join(str(o) for o in options).lower()
    return bool(re.search(r"\byes\b", joined) and re.search(r"\bno\b", joined))


def pick_yes_no(options: list, prefer_yes: bool) -> Optional[str]:
    """Return the option string that means Yes or No from a real list."""
    if not options:
        return "Yes" if prefer_yes else "No"
    want = "yes" if prefer_yes else "no"
    for o in options:
        s = str(o).strip()
        if re.match(rf"^{want}\b", s, re.I) or s.lower() == want:
            return s
    # fuzzy
    for o in options:
        if want in str(o).lower():
            return str(o)
    return None
