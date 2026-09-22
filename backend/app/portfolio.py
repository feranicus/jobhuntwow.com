"""THE PROJECT PORTFOLIO: his projects, kept once, and the ones THIS posting is about picked out.

WHY IT IS A STORE AND NOT ANOTHER UPLOAD BOX. `/evidence/upload` already accepts a document per
run, which means re-attaching the same case studies for every application -- and zero manual data
entry is the product promise. A portfolio is a fact about the candidate, not about one job: it is
written once and read by every tailoring run afterwards.

WHY THE SELECTION IS ARITHMETIC AND NOT A MODEL. Which projects matter for a posting is decided by
counting the posting's own words against each project's stack, tags, title and summary. That is
deterministic, free, instant, and it CANNOT invent a project -- the closed-set safety contract the
rest of this codebase uses for anything a model is allowed to name. A model is the fourth thing
consulted here, and for this question it is not needed at all: the answer is in the two texts.

WHY THE MATCH IS SHOWN. Every selected project carries WHICH posting terms matched it. A derived
fact is labelled as derived; the candidate can see why a project was included and drop it.

WHAT IT MAY NEVER DO. Nothing here writes prose. It hands the tailor VERBATIM text the candidate
wrote about work the candidate did, under the same rule as the rest of the profile: the model may
re-angle and compress those facts, and may never add an employer, a date, a metric or a project.
"""
import json
import os
import re
import time
import uuid

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAX_ITEMS = int(os.environ.get("JHW_PORTFOLIO_MAX", "60"))
# How many projects may reach the prompt. The JD and the profile must still fit in the window, and
# five strong, relevant projects beat twenty that dilute them.
DEFAULT_PICK = int(os.environ.get("JHW_PORTFOLIO_PICK", "5"))

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{1,}")
# Words that match everything and therefore prove nothing. A term that appears in every posting
# cannot be evidence that THIS project fits THIS posting.
_STOP = {
    "and", "the", "for", "with", "you", "your", "our", "we", "will", "are", "have", "has", "from",
    "that", "this", "their", "they", "who", "what", "when", "where", "how", "all", "any", "can",
    "job", "role", "work", "working", "team", "teams", "years", "year", "experience", "skills",
    "ability", "strong", "good", "excellent", "knowledge", "understanding", "including", "such",
    "new", "other", "more", "most", "well", "also", "must", "should", "would", "about", "into",
    "per", "via", "using", "use", "used", "within", "across", "over", "under", "between",
    "company", "business", "customer", "customers", "client", "clients", "project", "projects",
    "management", "manager", "support", "services", "service", "solutions", "solution",
}


def _tenant(email: str) -> str:
    """Stable, filesystem-safe id. Same derivation auth.tenant_key uses, imported when available so
    the two can never drift into two different directories for one person."""
    try:
        from .auth import tenant_key
        return tenant_key(email)
    except Exception:
        import hashlib
        return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def path_for(email: str) -> str:
    return os.path.join(str(DATA_DIR), "users", _tenant(email), "portfolio.json")


def _clean_item(raw) -> dict:
    """One project, bounded and typed. USER-EDITED JSON: its shapes are not ours to assume."""
    d = raw if isinstance(raw, dict) else {}

    def s(k, n=200):
        return str(d.get(k) or "").strip()[:n]

    def lst(k, n=24, each=160):
        v = d.get(k)
        if isinstance(v, str):
            v = [x for x in re.split(r"[,;\n]", v)]
        if not isinstance(v, list):
            v = []
        return [str(x).strip()[:each] for x in v if str(x).strip()][:n]

    item = {
        # A UNIQUE id, because two projects added in the same millisecond got the SAME one and the
        # second overwrote the first everywhere the id is the key. Measured: the API returned two
        # items whose ids differed in no digit.
        "id": s("id", 40) or ("p%d%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6])),
        "title": s("title"),
        "org": s("org"),
        "role": s("role"),
        "period": s("period", 60),
        "url": s("url", 300),
        "stack": lst("stack"),
        "tags": lst("tags"),
        "summary": s("summary", 1200),
        "achievements": lst("achievements", 12, 400),
        # WHERE THIS CAME FROM. An imported item says so, and keeps saying so after a save/load
        # round trip -- six months later "did I write this or did a parser guess it?" has an answer.
        "source": s("source", 120),
    }
    return item


def load(email: str) -> list:
    try:
        with open(path_for(email), encoding="utf-8") as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    items = d.get("items") if isinstance(d, dict) else d
    return [_clean_item(x) for x in (items or []) if isinstance(x, dict)][:MAX_ITEMS]


def save(email: str, items) -> list:
    """Write the whole portfolio atomically. Returns what was stored, so the UI renders the TRUTH
    rather than the optimistic copy it sent."""
    clean = [_clean_item(x) for x in (items or []) if isinstance(x, dict)]
    clean = [c for c in clean if c["title"] or c["summary"]][:MAX_ITEMS]
    p = path_for(email)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"updated": int(time.time()), "items": clean}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)                     # atomic: a half-written portfolio is never read
    return clean


def terms(text: str) -> set:
    """The words worth matching on: lowercased, de-duplicated, stop-words and noise removed."""
    return {w.lower() for w in _WORD.findall(str(text or ""))
            if len(w) > 2 and w.lower() not in _STOP}


def _item_terms(item: dict) -> set:
    """Weighted by WHERE the word sits: an explicit stack/tag entry is a deliberate claim, a word
    in a summary is prose. Both count; the caller's score reflects the difference."""
    t = set()
    for k in ("stack", "tags"):
        for v in item.get(k) or []:
            t |= terms(v)
    t |= terms(item.get("title"))
    t |= terms(item.get("role"))
    return t


def score(item: dict, jd_terms: set) -> tuple:
    """(score, matched_terms). Higher is more relevant. Deterministic and explainable."""
    hard = _item_terms(item) & jd_terms                       # named stack / tags / title
    soft = (terms(item.get("summary")) |
            terms(" ".join(item.get("achievements") or []))) & jd_terms
    soft -= hard
    return (2 * len(hard) + len(soft)), sorted(hard) + sorted(soft)


def select(items, jd_text, limit=None, min_score=1):
    """The projects THIS posting is about, strongest first.

    A project with NO overlap is not included: padding a cover letter with irrelevant work is the
    behaviour this feature exists to replace. An empty result is an honest answer and the caller
    says so rather than quietly sending nothing.
    """
    limit = DEFAULT_PICK if limit is None else int(limit)
    jt = terms(jd_text)
    out = []
    for it in (items or []):
        sc, matched = score(it, jt)
        if sc >= min_score:
            out.append({"item": it, "score": sc, "matched": matched[:12]})
    out.sort(key=lambda r: (-r["score"], str(r["item"].get("title") or "")))
    return out[:limit]


def block(selected) -> str:
    """The text appended to the profile. VERBATIM the candidate's own words, labelled as facts.

    Never a summary written by us: the tailor's rule is that the profile is the only permitted
    source of facts, so anything that reaches it has to BE the profile.
    """
    if not selected:
        return ""
    lines = ["", "", "PROJECT PORTFOLIO - WRITTEN BY THE CANDIDATE, TRUE, AND SELECTED BECAUSE THE "
             "POSTING ASKS FOR THIS WORK. Use these where they answer a requirement; re-angle and "
             "compress freely; never invent a project, a client, a date or a number that is not "
             "here."]
    for r in selected:
        it = r["item"]
        head = " | ".join([x for x in (it.get("title"), it.get("role"), it.get("org"),
                                       it.get("period")) if x])
        lines.append("")
        lines.append("* %s" % (head or "(untitled project)"))
        if it.get("stack"):
            lines.append("  stack: %s" % ", ".join(it["stack"]))
        if it.get("url"):
            lines.append("  link: %s" % it["url"])
        if it.get("summary"):
            lines.append("  %s" % it["summary"])
        for a in (it.get("achievements") or []):
            lines.append("  - %s" % a)
        if r.get("matched"):
            lines.append("  (selected because the posting names: %s)" % ", ".join(r["matched"][:8]))
    return "\n".join(lines)


def used(selected) -> list:
    """What the manifest and the UI show: which projects went in, and why."""
    return [{"id": r["item"].get("id"), "title": r["item"].get("title"),
             "org": r["item"].get("org"), "score": r["score"], "matched": r["matched"]}
            for r in (selected or [])]


# =================================================================================================
# IMPORT FROM A FILE. "I have my project portfolio as PDF and it needs to add word and pdf files."
#
# Zero manual data entry is the product promise, so a portfolio that already exists as a document
# must not have to be retyped project by project. The text is extracted by the caller (electronic.
# extract_text handles PDF/DOCX/TXT/MD) and split HERE, deterministically.
#
# WHAT IT PROPOSES, IT DOES NOT SAVE. Parsing somebody's layout is guesswork by nature -- a PDF
# built in two columns, a table, a designed CV -- so the items come back as a PROPOSAL the page
# shows him for review, and nothing reaches the store until he presses save. That is the same rule
# the rest of this codebase obeys: deterministic code may propose, the human decides the side
# effect. Every proposed item carries `source`, so six months later it is obvious where a line came
# from, and NOTHING is invented: every field is a substring of the file he uploaded.
_DATE = re.compile(
    r"((?:19|20)\d{2}\s*[-–—/]\s*(?:(?:19|20)\d{2}|present|now|current|today)"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(?:19|20)\d{2}"
    r"\s*[-–—/]\s*(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*"
    r"(?:19|20)\d{2}|present|now|current))", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022\u2023\u25aa\u25cf\u00b7>]|\d+[.)])\s+(.*\S)\s*$")
_LABEL = re.compile(r"^\s*(stack|tech|technologies|tools|skills|technology|methods?|tags?)\s*[:\-]\s*(.+)$", re.I)
_ROLE_LABEL = re.compile(r"^\s*(role|position|title|my role)\s*[:\-]\s*(.+)$", re.I)
_ORG_LABEL = re.compile(r"^\s*(client|customer|employer|company|organisation|organization|for)\s*[:\-]\s*(.+)$", re.I)
_URL = re.compile(r"https?://\S+")
# A heading is short, not a sentence, and not a bullet. Measured against real portfolio exports:
# titles run 2-9 words and rarely end in a full stop.
_HEAD_MAX_WORDS = int(os.environ.get("JHW_PORTFOLIO_HEAD_WORDS", "12"))


def _looks_like_heading(line: str) -> bool:
    t = str(line or "").strip()
    if not t or len(t) > 120:
        return False
    if _BULLET.match(t) or _LABEL.match(t) or _ROLE_LABEL.match(t) or _ORG_LABEL.match(t):
        return False
    words = t.split()
    if len(words) > _HEAD_MAX_WORDS:
        return False
    if t.endswith((".", ";", ",", ":")) and not t.endswith("..."):
        return False
    # ALL CAPS, Title Case, or a line followed by a date range - all three are how a person writes
    # a project title in a document.
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        return True
    if bool(_DATE.search(t)):
        return True
    return t[:1].isupper() and len(words) <= _HEAD_MAX_WORDS


def parse_text(text: str, source: str = "") -> dict:
    """Split an uploaded portfolio into PROPOSED items. Returns {items, note, chars, headings}.

    Deterministic, stdlib only, no model, no network: this runs on a file the candidate chose to
    upload, and a parser that silently rewrites his words would be worse than no parser at all.
    """
    raw = str(text or "")
    lines = [ln.rstrip() for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    chars = len(raw.strip())
    if chars < 200:
        # A SCANNED PDF IS IMAGES, and pypdf returns almost nothing for it. Say that plainly
        # instead of proposing an empty portfolio and letting him wonder what went wrong.
        return {"items": [], "chars": chars, "headings": 0,
                "note": ("only %d characters of text could be read from this file - it is most "
                         "likely a scan or an image-only PDF. Copy the text in and paste it, or "
                         "export a text-based PDF." % chars)}

    blocks, cur = [], None
    for ln in lines:
        if not ln.strip():
            continue
        if _looks_like_heading(ln):
            if cur is not None and not cur["body"]:
                # TWO HEADINGS IN A ROW: the first was a SECTION LABEL ("MY PROJECT PORTFOLIO",
                # "Selected work"), not a project. Measured: it swallowed the first real project as
                # its body and proposed the section title as the project. Replace it instead.
                cur["head"] = ln.strip()
                continue
            cur = {"head": ln.strip(), "body": []}
            blocks.append(cur)
        elif cur is None:
            cur = {"head": "", "body": [ln]}
            blocks.append(cur)
        else:
            cur["body"].append(ln)

    items = []
    for b in blocks:
        head = b["head"]
        body = b["body"]
        if not head and not body:
            continue
        period = ""
        m = _DATE.search(head)
        if m:
            period = m.group(1).strip()
            head = (head[:m.start()] + " " + head[m.end():]).strip(" -–—|,·\t")
        title, org, role, url, stack, prose, ach = head, "", "", "", [], [], []
        for ln in body:
            mb = _BULLET.match(ln)
            ml = _LABEL.match(ln)
            mr = _ROLE_LABEL.match(ln)
            mo = _ORG_LABEL.match(ln)
            mu = _URL.search(ln)
            if mu and not url:
                url = mu.group(0)
            if ml:
                stack += [x.strip() for x in re.split(r"[,;/|]", ml.group(2)) if x.strip()]
                continue
            if mr and not role:
                role = mr.group(2).strip()
                continue
            if mo and not org:
                org = mo.group(2).strip()
                continue
            if mb:
                ach.append(mb.group(1).strip())
                continue
            if not period:
                md = _DATE.search(ln)
                if md:
                    period = md.group(1).strip()
            prose.append(ln.strip())
        # A block with a title and NOTHING else is a section header ("Projects", "Portfolio"),
        # not a project. Dropping it beats proposing an empty row he has to delete.
        if not (prose or ach or stack or url):
            continue
        # AND A BLOCK WITH NO TITLE IS NOT A PROJECT EITHER unless it carries structure of its own
        # (bullets, a stack, a link). One wall of prose with no headings would otherwise come back
        # as a single untitled row holding the whole document, which is worse than saying so.
        if not title and not (ach or stack or url):
            continue
        item = _clean_item({
            "title": title, "org": org, "role": role, "period": period, "url": url,
            "stack": stack, "summary": " ".join(prose)[:1200],
            "achievements": ach,
        })
        item["source"] = ("imported from %s" % source)[:120] if source else "imported"
        items.append(item)

    items = items[:MAX_ITEMS]
    if not items:
        return {"items": [], "chars": chars, "headings": len(blocks),
                "note": ("%d characters were read but no project could be split out of them. The "
                         "file may be one long block of prose - add the projects below by hand, or "
                         "put each project under its own short title." % chars)}
    return {"items": items, "chars": chars, "headings": len(blocks),
            "note": ("%d project(s) read from %s. NOTHING IS SAVED YET - check them, fix anything "
                     "the file laid out oddly, then press Save portfolio."
                     % (len(items), source or "the file"))}


# --------------------------------------------------------------------------- self-test
def _selftest() -> int:
    import tempfile
    global DATA_DIR
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ((" - " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    DATA_DIR = tempfile.mkdtemp()
    me = "him@example.com"

    ck("an empty portfolio reads as empty, not as an error", load(me) == [])

    items = [
        {"title": "Colt SD-WAN rollout", "org": "Colt", "role": "Project Manager",
         "period": "2023-2024", "stack": ["SD-WAN", "Cisco", "Prince2"],
         "summary": "Ran a 40-site SD-WAN migration across EMEA.",
         "achievements": ["Cut WAN spend 22% (EUR 1.1M/yr)", "Zero unplanned outages in cutover"]},
        {"title": "Bakery ecommerce shop", "org": "side project", "role": "Developer",
         "stack": ["React", "Stripe"], "summary": "A small online shop for a local bakery.",
         "achievements": ["Launched in 6 weeks"]},
        {"title": "SOC automation", "org": "S4Biz", "role": "Lead",
         "stack": ["Python", "Grafana", "Loki"],
         "summary": "Built alerting and dashboards for a security operations centre.",
         "achievements": ["Mean time to detect down from 4h to 11min"]},
    ]
    stored = save(me, items)
    ck("what was saved is what is read back", len(load(me)) == 3 and len(stored) == 3)
    ck("every item gets an id even when the caller sent none",
       all(i["id"] for i in load(me)))
    many = save(me, [{"title": "t%d" % i} for i in range(30)])
    ck("and the ids are UNIQUE even when 30 are created in the same millisecond",
       len({i["id"] for i in many}) == 30, "%d unique of %d" % (len({i["id"] for i in many}), len(many)))
    save(me, items)

    jd = ("We are hiring a Project Manager to lead an SD-WAN migration across our EMEA sites. "
          "Cisco experience and Prince2 required. You will manage vendor cutovers.")
    sel = select(load(me), jd)
    ck("the relevant project is selected", sel and sel[0]["item"]["title"] == "Colt SD-WAN rollout",
       str([r["item"]["title"] for r in sel]))
    ck("and it names WHY it was selected (a derived fact is labelled)",
       any(t in ("sd-wan", "cisco", "prince2", "emea", "migration") for t in sel[0]["matched"]),
       str(sel[0]["matched"]))
    ck("an unrelated project is NOT padded in",
       all(r["item"]["title"] != "Bakery ecommerce shop" for r in sel),
       str([r["item"]["title"] for r in sel]))

    # A posting about something he has never done must return NOTHING, not the nearest thing.
    sel2 = select(load(me), "Seeking a maritime welding inspector for offshore rigs.")
    ck("a posting we have no project for selects nothing at all", sel2 == [], str(sel2))

    b = block(sel)
    ck("the block carries the candidate's OWN words verbatim",
       "Cut WAN spend 22% (EUR 1.1M/yr)" in b)
    ck("and nothing else: no project that was not selected",
       "Bakery" not in b)
    ck("an empty selection produces an empty block, never a placeholder", block([]) == "")
    ck("the block tells the model it may not invent", "never invent a project" in b)

    u = used(sel)
    ck("`used` reports id, title and the matched terms for the manifest",
       u and set(u[0]) >= {"id", "title", "score", "matched"})

    # bounds
    save(me, [{"title": "x" * 500, "summary": "y" * 5000,
               "achievements": ["z" * 900] * 50, "stack": ["s"] * 100}])
    one = load(me)[0]
    ck("a hostile item is bounded on every field",
       len(one["title"]) <= 200 and len(one["summary"]) <= 1200
       and len(one["achievements"]) <= 12 and len(one["stack"]) <= 24,
       "%d/%d/%d/%d" % (len(one["title"]), len(one["summary"]), len(one["achievements"]),
                        len(one["stack"])))
    save(me, [{"title": "t%d" % i} for i in range(MAX_ITEMS + 25)])
    ck("and the portfolio itself is capped", len(load(me)) == MAX_ITEMS, str(len(load(me))))
    save(me, [{"title": "", "summary": ""}, {"title": "keeper"}])
    ck("an empty row is dropped rather than stored", [i["title"] for i in load(me)] == ["keeper"])

    # ---------------------------------------------------------------- import from a file
    doc = """MY PROJECT PORTFOLIO

Colt SD-WAN Rollout   2023 - 2024
Client: Colt Technology Services
Role: Programme Manager
Stack: SD-WAN, Cisco Viptela, Prince2
Migrated 40 sites across EMEA from MPLS to SD-WAN in eleven months.
- Cut WAN spend by 22 percent, EUR 1.1M a year
- Zero unplanned outages across every cutover window
https://example.com/case/colt

SOC Automation Platform  2022 - 2023
Role: Lead Engineer
Technologies: Python, Grafana, Loki
Built the alerting and dashboards a 12-person security operations centre runs on.
* Mean time to detect fell from 4 hours to 11 minutes
* 40 dashboards, one deployment pipeline

Bakery Ecommerce Shop
Stack: React, Stripe
A small online shop for a local bakery, built over six weekends.
"""
    got = parse_text(doc, "portfolio.pdf")
    titles = [i["title"] for i in got["items"]]
    ck("a real portfolio document splits into its projects", len(got["items"]) == 3, str(titles))
    ck("the section header is not proposed as a project", "MY PROJECT PORTFOLIO" not in titles,
       str(titles))
    first = got["items"][0] if got["items"] else {}
    ck("the title is read without its date range", first.get("title") == "Colt SD-WAN Rollout",
       repr(first.get("title")))
    ck("and the date range becomes the period", first.get("period", "").startswith("2023"),
       repr(first.get("period")))
    ck("labelled lines are read into their own fields",
       first.get("role") == "Programme Manager" and "Colt" in first.get("org", ""),
       "%r / %r" % (first.get("role"), first.get("org")))
    ck("the stack is split on its separators",
       set(first.get("stack") or []) == {"SD-WAN", "Cisco Viptela", "Prince2"},
       str(first.get("stack")))
    ck("bullets become achievements, prose becomes the summary",
       len(first.get("achievements") or []) == 2
       and "Migrated 40 sites" in first.get("summary", ""),
       str(first.get("achievements")))
    ck("a link in the block is kept", "example.com" in first.get("url", ""), first.get("url", ""))
    ck("a second bullet style (*) is read too",
       len(got["items"][1].get("achievements") or []) == 2,
       str(got["items"][1].get("achievements")))
    ck("every imported item says where it came from",
       all("portfolio.pdf" in i.get("source", "") for i in got["items"]))
    ck("and the note says plainly that nothing is saved yet",
       "NOTHING IS SAVED YET" in got["note"], got["note"][:60])
    # NOTHING IS INVENTED: every value the parser proposes is text from the file he uploaded.
    flat = doc.lower()
    invented = []
    for it in got["items"]:
        for v in ([it.get("title"), it.get("org"), it.get("role"), it.get("period"), it.get("url")]
                  + list(it.get("stack") or []) + list(it.get("achievements") or [])):
            if v and str(v).lower().strip() not in flat:
                invented.append(v)
    ck("NO field is invented - every one is a substring of the uploaded file", not invented,
       str(invented[:3]))

    scan = parse_text("   ", "scan.pdf")
    ck("an image-only PDF is named as such, not proposed as an empty portfolio",
       scan["items"] == [] and "scan or an image" in scan["note"], scan["note"][:60])
    prose = parse_text("x " * 300, "wall.pdf")
    ck("one wall of prose says so rather than guessing",
       prose["items"] == [] and "no project could be split" in prose["note"], prose["note"][:60])
    ck("the import round-trips through the store with its source intact",
       bool(save(me, got["items"])) and load(me)[0].get("source", "").startswith("imported"),
       load(me)[0].get("source", ""))
    save(me, items)

    print("portfolio selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
