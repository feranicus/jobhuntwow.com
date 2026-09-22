"""The project portfolio, and the TOP-5 cover letter. Both asked for by name, both tested WIRED.

  "add option to add project portfolio and take items from it to include in Cover letter or in
   resume specifically important for position description"
  "make cover letter in TOP 5 format ... this needs to be an optional bullet point if a stakeholder
   wants to make a top 5 cover letter he needs to choose this option"

What is proven here, in the order the request makes:
  1. a project the posting asks for reaches the PROMPT, verbatim, as the candidate wrote it
  2. a project it does not ask for does NOT
  3. choosing top5 changes what is asked of the model, and the contract then requires FIVE
  4. four reasons and six reasons are both REJECTED - a top-5 letter that is not five is a
     different document that happens to parse
  5. the five survive the audit round: a revision may not turn them back into prose
  6. the five are RENDERED, numbered, in the file that gets attached
  7. the portfolio is his: anonymous callers are refused and the caller cannot name whose to read

No network, no model, no spend: the transport is stubbed and every assertion is on what the code
would have sent.
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ["ALERTS_ENABLED"] = "0"
os.environ["JHW_METER_DB"] = os.path.join(tempfile.mkdtemp(), "meter.sqlite")

from app import portfolio                                                       # noqa: E402
from app import resume_consensus as RC                                          # noqa: E402
from app import documents as DOC                                                # noqa: E402

_fails, _ran = [], [0]


def check(cond, name, detail=""):
    _ran[0] += 1
    print(("  ok    " if cond else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not cond:
        _fails.append(name)


ME = "portfolio-test@example.com"
# A REALISTIC PROFILE. The first version was 48 characters and only cleared the endpoint's
# "too thin to tailor from" floor because the portfolio block was appended to it -- so the run
# WITHOUT the portfolio 400'd, and two checks then passed on an empty capture. The floor counting
# the portfolio is correct (a project he wrote IS his real experience); the fixture was the bug.
PROFILE = ("Evgeny Ferani - senior project manager, 12 years. Colt Technology Services 2021-2024: "
           "led EMEA network programmes. Prince2 practitioner, PMP. Earlier: S4Biz, security "
           "operations. Languages: English, German, Russian.")
ITEMS = [
    {"title": "Colt SD-WAN rollout", "org": "Colt", "role": "Project Manager",
     "period": "2023-2024", "stack": ["SD-WAN", "Cisco", "Prince2"],
     "summary": "Ran a 40-site SD-WAN migration across EMEA.",
     "achievements": ["Cut WAN spend 22 percent (EUR 1.1M a year)",
                      "Zero unplanned outages during cutover"]},
    {"title": "Bakery ecommerce shop", "org": "side project", "role": "Developer",
     "stack": ["React", "Stripe"], "summary": "A small online shop for a local bakery.",
     "achievements": ["Launched in six weeks"]},
]
JD = {"title": "Project Manager", "company": "Acme", "location": "Berlin",
      "text": ("We need a Project Manager to lead an SD-WAN migration across EMEA sites. "
               "Cisco and Prince2 required. You will run vendor cutovers and report to the board.")}

portfolio.DATA_DIR = os.environ["DATA_DIR"]
portfolio.save(ME, ITEMS)
sel = portfolio.select(portfolio.load(ME), JD["text"])

# ============================================================ 1 + 2. what reaches the prompt
prompt = RC.cover_user("PROFILE TEXT" + portfolio.block(sel), JD, "letter")
check("Cut WAN spend 22 percent (EUR 1.1M a year)" in prompt,
      "the achievement he wrote reaches the prompt VERBATIM")
check("Bakery" not in prompt, "and a project this posting does not ask for does not")
check("never invent a project" in prompt,
      "the block tells the model the projects are facts, not a licence to invent")

# ============================================================ 3. the format changes the ask
top5_prompt = RC.cover_user("PROFILE", JD, "top5")
check("TOP 5 REASONS" in top5_prompt.upper() and '"reasons"' in top5_prompt,
      "choosing top5 asks for five ranked reasons, not paragraphs")
check("EXACTLY FIVE" in top5_prompt.upper(), "and says exactly five, in the rules")
check('"paragraphs"' in prompt and "TOP 5" not in prompt.upper(),
      "while the default letter is unchanged - the option is opt-in, never the new default")


def five(n=5, why="Ran the 40-site SD-WAN migration at Colt and cut WAN spend 22 percent."):
    return {"salutation": "Hiring Team", "opening": "Five reasons, ranked by your own requirements.",
            "reasons": [{"headline": "Reason number %d about the role" % i, "why": why}
                        for i in range(1, n + 1)],
            "close": "Happy to walk through any of them.", "closing": "Sincerely,"}


# ============================================================ 4. five, or it is not a top-5 letter
ok5, why5 = RC.contract_ok_cover(RC.normalise_cover(five(5)), None, "top5")
ok4, why4 = RC.contract_ok_cover(RC.normalise_cover(five(4)), None, "top5")
ok6, why6 = RC.contract_ok_cover(RC.normalise_cover(five(6)), None, "top5")
check(ok5, "five reasons pass the contract", why5)
check(not ok4 and "5" in why4, "FOUR is refused", why4)
check(not ok6, "and so is six", why6)
thin = RC.normalise_cover({"salutation": "x", "reasons": [{"headline": "a", "why": ""}] * 5})
check(not RC.contract_ok_cover(thin, None, "top5")[0],
      "five headlines with no proof are refused - a list of claims is not evidence")
# THE FLOOR IS THE FORMAT'S OWN. A five-reason letter is legitimately shorter than 250-350 words
# of prose; judged against the prose floor, a good one reads as THIN. Both directions are asserted:
# a realistic five passes, five stubs do not.
check(RC.cover_depth(RC.normalise_cover(five(5))) > RC.MIN_COVER_TOP5
      and RC.MIN_COVER_TOP5 < RC.MIN_COVER,
      "the top-5 floor is its own number, measured against the document it guards",
      "depth %d, floor %d (prose floor %d)" % (RC.cover_depth(RC.normalise_cover(five(5))),
                                               RC.MIN_COVER_TOP5, RC.MIN_COVER))
stub5 = RC.normalise_cover({"salutation": "x", "opening": "", "close": "",
                            "reasons": [{"headline": "Reason %d" % i, "why": "Did things."}
                                        for i in range(1, 6)]})
check(not RC.contract_ok_cover(stub5, None, "top5")[0],
      "and five stubs still fail it", str(RC.contract_ok_cover(stub5, None, "top5")[1]))
# the normaliser has to survive the other shape a model returns
plain = RC.normalise_cover({"reasons": ["Led the migration - 40 sites, 22 percent saved"] * 5})
check(len(plain["reasons"]) == 5 and plain["reasons"][0]["headline"],
      "a model that returns plain strings instead of objects is still understood")

# ============================================================ 5. the audit may not change the format
before = RC.normalise_cover(five(5))
prose = RC.normalise_cover({"salutation": "Hiring Team",
                            "paragraphs": ["p" * 400, "q" * 400, "r" * 400], "closing": "Sincerely,"})
ok, why = RC.revision_ok("cover", before, prose, profile="PROFILE", cover_format="top5")
check(not ok, "a revision that turns the five back into prose is REFUSED", why)
ok2, _ = RC.revision_ok("cover", before, RC.normalise_cover(five(5)), profile="PROFILE",
                        cover_format="top5")
check(ok2, "while a genuine revision of the five is accepted")

# ============================================================ 6. and they are RENDERED
blocks = DOC.struct_to_blocks(RC.normalise_cover(five(5)), "cover")
lis = [t for k, t in blocks if k == "li"]
check(len(lis) == 5, "the file carries five bullets", "got %d" % len(lis))
check(all(t.startswith("%d." % (i + 1)) for i, t in enumerate(lis)),
      "numbered 1..5, because the whole point of the format is that they can be counted",
      str(lis[:1]))
d = tempfile.mkdtemp()
DOC.cover_docx(RC.normalise_cover(five(5)), os.path.join(d, "c.docx"))
DOC.cover_pdf(RC.normalise_cover(five(5)), os.path.join(d, "c.pdf"))
check(os.path.getsize(os.path.join(d, "c.docx")) > 5000
      and os.path.getsize(os.path.join(d, "c.pdf")) > 800,
      "both the DOCX and the PDF are written, not just the struct")

# ============================================================ 7. it is HIS portfolio
from app.main import app                                                        # noqa: E402
from app import auth, users                                                     # noqa: E402

users.create_user(ME, "Sup3rSecret!pass")
users.create_user("someone-else@example.com", "Sup3rSecret!pass")
mine = "%s=%s" % (auth.SESSION_COOKIE, auth.make_session(ME))
theirs = "%s=%s" % (auth.SESSION_COOKIE, auth.make_session("someone-else@example.com"))


def call(method, path, body=None, cookie=None):
    out = {"body": b"", "status": 0}
    hdrs = [(b"host", b"jobhuntwow.com"), (b"content-type", b"application/json")]
    if cookie:
        hdrs.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": hdrs, "client": ("203.0.113.9", 1), "server": ("jobhuntwow.com", 443)}
    payload = json.dumps(body or {}).encode()
    sent = {"x": False}

    async def receive():
        if sent["x"]:
            return {"type": "http.disconnect"}
        sent["x"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.new_event_loop().run_until_complete(app(scope, receive, send))
    return out


check(call("GET", "/api/electronic/portfolio")["status"] == 401,
      "an anonymous caller cannot read a portfolio")
check(call("PUT", "/api/electronic/portfolio", {"items": ITEMS})["status"] == 401,
      "nor write one")
r = call("PUT", "/api/electronic/portfolio", {"items": ITEMS}, mine)
check(r["status"] == 200 and json.loads(r["body"])["count"] == 2, "he can store his own")
r = call("GET", "/api/electronic/portfolio", cookie=theirs)
check(r["status"] == 200 and json.loads(r["body"])["count"] == 0,
      "and ANOTHER account sees its own empty portfolio, never his",
      json.loads(r["body"])["count"] and "LEAK" or "")
r = call("POST", "/api/electronic/portfolio/preview", {"text": JD["text"]}, mine)
got = json.loads(r["body"])
check(r["status"] == 200 and len(got["selected"]) == 1
      and got["selected"][0]["title"] == "Colt SD-WAN rollout",
      "the preview names which project this posting picks, before anything is spent",
      str(got)[:90])
check(bool(got["selected"][0]["matched"]),
      "and WHY it picked it - a derived fact is labelled as derived",
      str(got["selected"][0]["matched"]))

# ============================================================ 8. the endpoint actually uses them
seen = {}


async def fake_tailor(ptxt, jd, **kw):
    seen["ptxt"] = ptxt
    seen["cover_format"] = kw.get("cover_format")
    return {"resume": {"summary": "s" * 400,
                       "experience": [{"title": "PM", "company": "Colt",
                                       "bullets": ["b" * 300, "c" * 300]}],
                       "highlights": ["h" * 200], "all_employers": ["Colt"]},
            "cover": RC.normalise_cover(five(5)),
            "authors": {"resume": "m1", "cover": "m1"}, "attempts": [],
            "audit": {"auditor": "m2", "auditor_vendor": "v2"}, "audit_rounds": 1,
            "audit_refused": False, "quality": {}, "errors": {}, "elapsed_ms": 1}

from app import electronic                                                      # noqa: E402
_real = electronic.RC.tailor
electronic.RC.tailor = fake_tailor
try:
    r = call("POST", "/api/electronic/generate",
             {"jd": JD, "profile": PROFILE,
              "cover_format": "top5"}, mine)
    man = json.loads(r["body"]) if r["status"] == 200 else {}
    check(r["status"] == 200, "the endpoint answers", str(r["status"]) + " " + str(r["body"][:120]))
    check("Cut WAN spend 22 percent (EUR 1.1M a year)" in seen.get("ptxt", ""),
          "THE PROJECT REACHED THE WRITER - the portfolio is wired, not merely stored")
    check("Bakery" not in seen.get("ptxt", ""), "and the irrelevant project did not")
    check(seen.get("cover_format") == "top5",
          "the format he chose reached the chain", str(seen.get("cover_format")))
    check(man.get("cover_format") == "top5", "and the manifest records which format was used")
    check([x["title"] for x in (man.get("portfolio_used") or [])] == ["Colt SD-WAN rollout"],
          "the manifest names the projects used", str(man.get("portfolio_used"))[:100])
    seen.clear()
    r2 = call("POST", "/api/electronic/generate",
              {"jd": JD, "profile": PROFILE,
               "use_portfolio": False}, mine)
    # ASSERT THE CALL HAPPENED FIRST. Without this line the two checks below passed on an empty
    # `seen` after a 400 - "the portfolio did not reach the writer" is trivially true when nothing
    # reached the writer at all. A check that cannot fail is not a check.
    check(r2["status"] == 200 and bool(seen.get("ptxt")),
          "the second run reached the writer at all", "%s %s" % (r2["status"], r2["body"][:110]))
    check("Cut WAN spend" not in seen.get("ptxt", "x") ,
          "unticking the box really keeps the portfolio out")
    check(seen.get("cover_format") == "letter", "and the default format is the classic letter")
finally:
    electronic.RC.tailor = _real

print("-" * 78)
print("%d checks run, %d failed" % (_ran[0], len(_fails)))
if _fails:
    print("FAILED: " + ", ".join(_fails))
sys.exit(1 if _fails else 0)
