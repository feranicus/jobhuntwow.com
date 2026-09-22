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

    print("portfolio selftest: %d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
