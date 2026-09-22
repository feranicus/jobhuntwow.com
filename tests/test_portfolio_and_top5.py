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
import re
import sys
import tempfile
import threading
import time

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
    # SPLIT THE QUERY STRING. Passing `/runlog/x?after=0` as the PATH matched the route with a
    # run_id of "x?after=0" -- an unknown run -- so the poller got 200 with an empty list and
    # the check read it as "the log does not stream". The harness was wrong, not the product,
    # and a harness defect that reads as a product defect is the expensive kind.
    _p, _sep, _q = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": _p, "raw_path": _p.encode(),
             "query_string": _q.encode(),
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

# ============================================================ 9. import from a PDF or Word file
import io                                                                       # noqa: E402


def multipart(filename, content, ctype):
    bound = "jhwboundary12345"
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n' % (bound, filename, ctype)).encode()
    return head + content + ('\r\n--%s--\r\n' % bound).encode(), \
        ("multipart/form-data; boundary=%s" % bound).encode()


def upload(filename, content, ctype, cookie=None):
    body, ct = multipart(filename, content, ctype)
    out = {"body": b"", "status": 0}
    hdrs = [(b"host", b"jobhuntwow.com"), (b"content-type", ct)]
    if cookie:
        hdrs.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "https", "path": "/api/electronic/portfolio/upload",
             "raw_path": b"/api/electronic/portfolio/upload", "query_string": b"",
             "headers": hdrs, "client": ("203.0.113.9", 1), "server": ("jobhuntwow.com", 443)}
    d = {"x": False}

    async def receive():
        if d["x"]:
            return {"type": "http.disconnect"}
        d["x"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.new_event_loop().run_until_complete(app(scope, receive, send))
    return out


PORTFOLIO_DOC = ("Colt SD-WAN Rollout   2023 - 2024\n"
       "Client: Colt Technology Services\n"
       "Role: Programme Manager\n"
       "Stack: SD-WAN, Cisco Viptela, Prince2\n"
       "Migrated 40 sites across EMEA from MPLS to SD-WAN in eleven months.\n"
       "- Cut WAN spend by 22 percent, EUR 1.1M a year\n"
       "- Zero unplanned outages across every cutover window\n"
       "\n"
       "SOC Automation Platform 2022 - 2023\n"
       "Technologies: Python, Grafana, Loki\n"
       "Built the alerting a 12-person SOC runs on.\n"
       "* Mean time to detect fell from 4 hours to 11 minutes\n")

check(upload("p.txt", PORTFOLIO_DOC.encode(), "text/plain")["status"] == 401,
      "an anonymous caller cannot import a portfolio")

before = json.loads(call("GET", "/api/electronic/portfolio", cookie=mine)["body"])["count"]
r = upload("portfolio.txt", PORTFOLIO_DOC.encode(), "text/plain", mine)
got = json.loads(r["body"])
check(r["status"] == 200 and [p["title"] for p in got["proposed"]]
      == ["Colt SD-WAN Rollout", "SOC Automation Platform"],
      "a text portfolio is split into its projects",
      str([p["title"] for p in got.get("proposed", [])]))
after = json.loads(call("GET", "/api/electronic/portfolio", cookie=mine)["body"])["count"]
check(after == before,
      "IMPORT PROPOSES, IT DOES NOT SAVE - the store is unchanged until he presses save",
      "%d -> %d" % (before, after))
check("NOTHING IS SAVED YET" in got["note"], "and the answer says so in words")

# A REAL .docx, written by python-docx, through the real extraction path.
import docx                                                                     # noqa: E402
_d = docx.Document()
for _line in PORTFOLIO_DOC.split("\n"):
    _d.add_paragraph(_line)
_buf = io.BytesIO()
_d.save(_buf)
rw = upload("portfolio.docx", _buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", mine)
gw = json.loads(rw["body"])
check(rw["status"] == 200 and len(gw["proposed"]) == 2 and gw["kind"] == "docx",
      "a real WORD file is read and split", "%s %s" % (rw["status"], str(gw)[:80]))

# A REAL .pdf, written by reportlab, through pypdf.
from reportlab.pdfgen import canvas                                             # noqa: E402
_buf = io.BytesIO()
_cv = canvas.Canvas(_buf)
_y = 800
for _line in PORTFOLIO_DOC.split("\n"):
    _cv.drawString(60, _y, _line)
    _y -= 14
_cv.save()
rp = upload("portfolio.pdf", _buf.getvalue(), "application/pdf", mine)
gp = json.loads(rp["body"])
check(rp["status"] == 200 and len(gp["proposed"]) == 2 and gp["kind"] == "pdf",
      "and so is a real PDF - the format he actually has", "%s %s" % (rp["status"], str(gp)[:80]))
check(gp["proposed"][0]["stack"] == ["SD-WAN", "Cisco Viptela", "Prince2"],
      "with the stack that makes it selectable for a posting",
      str(gp["proposed"][0]["stack"]))

# An image-only PDF (no text layer) must SAY SO rather than propose an empty portfolio.
_buf = io.BytesIO()
_cv = canvas.Canvas(_buf)
_cv.rect(50, 50, 200, 200, fill=1)
_cv.save()
rs = upload("scan.pdf", _buf.getvalue(), "application/pdf", mine)
gs = json.loads(rs["body"])
check(rs["status"] == 200 and gs["proposed"] == []
      and ("scan or an image" in gs["note"] or "no project could be split" in gs["note"]),
      "a scanned PDF is named as a scan, not proposed as nothing-went-wrong",
      gs.get("note", "")[:70])

# And the imported items really are selectable afterwards, once he saves them.
call("PUT", "/api/electronic/portfolio", {"items": gp["proposed"]}, mine)
pv = json.loads(call("POST", "/api/electronic/portfolio/preview", {"text": JD["text"]},
                     mine)["body"])
check([x["title"] for x in pv["selected"]] == ["Colt SD-WAN Rollout"],
      "an IMPORTED project is picked by a posting exactly like a typed one",
      str(pv["selected"])[:80])

# RESTORE WHAT THE EARLIER SECTIONS STORED. A suite that leaves the fixture changed makes the next
# section fail for a reason that has nothing to do with the thing it tests - and the two checks it
# broke first time round read like product defects, which is exactly how a green suite loses its
# authority.
call("PUT", "/api/electronic/portfolio", {"items": ITEMS}, mine)

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

# ============================================================ 10. the run log: live, and a file
RUNID = "run-suite-1"
_seen = {"lines": [], "polls": 0}


def _poller():
    nxt = 0
    for _ in range(80):
        rr = call("GET", "/api/electronic/runlog/%s?after=%d" % (RUNID, nxt), cookie=mine)
        if rr["status"] == 200:
            dd = json.loads(rr["body"])
            _seen["polls"] += 1
            _seen["lines"] += dd.get("lines") or []
            nxt = dd.get("next") or nxt
            if dd.get("done"):
                return
        time.sleep(0.05)


# The chain runs for real against a stubbed transport, so the lines come from the real code path.
async def _poster(payload, timeout):
    sysmsg = (payload.get("messages") or [{}])[0].get("content", "")
    usermsg = (payload.get("messages") or [{}, {}])[-1].get("content", "")
    if "adversarial reviewer" in sysmsg:
        body = {"resume": {"flags": []}, "cover": {"flags": []}}
    elif '"reasons"' in usermsg:
        body = five(5)
    elif '"paragraphs"' in usermsg:
        body = {"salutation": "Hiring Team", "paragraphs": ["p" * 500, "q" * 500], "closing": "S"}
    else:
        body = {"summary": "s" * 400, "skills": ["a"], "highlights": ["h" * 200],
                "experience": [{"title": "PM", "company": "Colt",
                                "bullets": ["b" * 300, "c" * 300]}],
                "earlier": [], "all_employers": ["Colt"], "keywords_matched": [], "gaps": []}
    return {"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 300}}


_real_tailor = RC.tailor


async def _tailor_stub(ptxt, jd, **kw):
    kw["poster"] = _poster
    return await _real_tailor(ptxt, jd, **kw)


electronic.RC.tailor = _tailor_stub
try:
    _t = threading.Thread(target=_poller, daemon=True)
    _t.start()
    rg = call("POST", "/api/electronic/generate",
              {"jd": JD, "profile": PROFILE, "cover_format": "letter", "run_id": RUNID}, mine)
    _t.join(timeout=8)
    man = json.loads(rg["body"]) if rg["status"] == 200 else {}
    check(rg["status"] == 200, "a run with a run_id still returns its manifest", str(rg["status"]))
    check(_seen["polls"] > 0 and len(_seen["lines"]) > 10,
          "THE LOG STREAMS WHILE THE RUN IS IN FLIGHT - the poller saw it as it happened",
          "%d line(s) in %d poll(s)" % (len(_seen["lines"]), _seen["polls"]))
    text = "\n".join(_seen["lines"])
    check("PROGRESS:" in text, "it carries progress lines")
    check("[chain]" in text and "[author]" in text,
          "it names the model chain and who authored")
    check("[draft]" in text and ("ACCEPTED" in text or "REJECTED" in text),
          "and every draft with its verdict - the line that makes a bad run diagnosable")
    check('"evt": "tailor_start"' in text and '"evt": "tailor_done"' in text,
          "with structured events in the same shape the rest of the estate emits")
    check("[truth-check]" in text, "the truth-check result is in the log, not only in the manifest")

    # THE FILE, beside the documents.
    logname = man.get("run_log") or ""
    check(bool(logname) and logname in (man.get("files") or []),
          "the run log is an ARTIFACT, listed with the DOCX and the PDF", str(man.get("files")))
    lp = os.path.join(electronic.job_dir(ME, man.get("job_id", "")), logname or "x")
    check(os.path.exists(lp) and os.path.getsize(lp) > 500,
          "and it is on disk beside them",
          "%s %s" % (os.path.exists(lp), os.path.getsize(lp) if os.path.exists(lp) else 0))
    ondisk = open(lp, encoding="utf-8").read() if os.path.exists(lp) else ""
    check(ondisk.splitlines()[0].startswith("JobHuntWOW run log"),
          "with a header naming the employer, the role and the time",
          (ondisk.splitlines() or [""])[0][:70])
    check("[draft]" in ondisk and '"evt": "tailor_done"' in ondisk,
          "and the whole run in it, not a summary")
    rd = call("GET", "/api/electronic/artifacts/%s/%s" % (man.get("job_id"), logname), cookie=mine)
    check(rd["status"] == 200 and b"JobHuntWOW run log" in rd["body"],
          "it downloads through the same artifacts route as the documents", str(rd["status"]))

    # IT IS HIS.
    r_other = call("GET", "/api/electronic/runlog/%s?after=0" % RUNID, cookie=theirs)
    d_other = json.loads(r_other["body"])
    check(r_other["status"] == 200 and d_other["lines"] == [] and not d_other["known"],
          "ANOTHER account is told nothing about the run", str(d_other))
    check(call("GET", "/api/electronic/runlog/%s" % RUNID)["status"] == 401,
          "and an anonymous caller is refused")
    d_unknown = json.loads(call("GET", "/api/electronic/runlog/never-existed",
                                cookie=mine)["body"])
    check(d_unknown["known"] is False and d_unknown["lines"] == [],
          "an unknown or expired run answers known:false, never a 404 the page must special-case")
finally:
    electronic.RC.tailor = _real_tailor

# ============================================================ 11. READ THE DELIVERED ARTIFACT
# He chose Top 5 and got a PDF with a header, "Dear Hiring Team", "Sincerely" and NOTHING between
# them. The model wrote five reasons, the contract accepted them, documents.py knows how to draw
# them -- and `cover_struct` in the generate handler carried only `paragraphs`, so the body was
# dropped on the floor between the two. Every check above passed while that shipped, because they
# all stopped at the struct. This one goes the whole way to the FILE.
FIVE = five(5)


async def _five_poster(payload, timeout):
    sysmsg = (payload.get("messages") or [{}])[0].get("content", "")
    usermsg = (payload.get("messages") or [{}, {}])[-1].get("content", "")
    if "adversarial reviewer" in sysmsg:
        body = {"resume": {"flags": []}, "cover": {"flags": []}}
    elif '"reasons"' in usermsg:
        body = FIVE
    else:
        body = {"summary": "s" * 400, "skills": ["a"], "highlights": ["h" * 200],
                "experience": [{"title": "PM", "company": "Colt",
                                "bullets": ["b" * 300, "c" * 300]}],
                "earlier": [], "all_employers": ["Colt"], "keywords_matched": [], "gaps": []}
    return {"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 300}}


async def _tailor_five(ptxt, jd, **kw):
    kw["poster"] = _five_poster
    return await _real_tailor(ptxt, jd, **kw)


electronic.RC.tailor = _tailor_five
try:
    rr = call("POST", "/api/electronic/generate",
              {"jd": dict(JD, title="Senior Technical Customer Success Manager",
                          company="Anaconda"),
               "profile": PROFILE, "cover_format": "top5"}, mine)
    man5 = json.loads(rr["body"]) if rr["status"] == 200 else {}
    check(rr["status"] == 200, "a top-5 run completes", str(rr["status"]))

    jdir = electronic.job_dir(ME, man5.get("job_id", ""))
    # 1. the STRUCT that was handed to the renderer, as it was stored
    saved = {}
    tpath = os.path.join(jdir, "tailored.json")
    if os.path.exists(tpath):
        saved = json.load(open(tpath, encoding="utf-8"))
    cov = (saved.get("cover") or {})
    check(len(cov.get("reasons") or []) == 5,
          "the FIVE REASONS reach the struct the renderer is given",
          "reasons=%d keys=%s" % (len(cov.get("reasons") or []), sorted(cov)[:8]))
    check(bool(cov.get("opening")) and bool(cov.get("close")),
          "and so do the opening and the close around them")

    # 2. the RENDERED BLOCKS - what the DOCX and the PDF are actually built from
    blocks = DOC.struct_to_blocks(cov, "cover")
    lis = [t for k, t in blocks if k == "li"]
    body_chars = sum(len(t) for k, t in blocks if k in ("li", "p"))
    check(len(lis) == 5, "the rendered document carries five numbered points", "got %d" % len(lis))
    check(body_chars > 400,
          "AND IT HAS A BODY AT ALL - the letter he got was 414 characters of letterhead and "
          "nothing else", "%d chars of body" % body_chars)

    # 3. the FILE on disk, read back
    covers = [f for f in (man5.get("files") or []) if f.startswith("cover_letter_")
              and f.endswith(".docx")]
    check(bool(covers), "a cover letter file was written", str(man5.get("files")))
    if covers:
        import zipfile
        with zipfile.ZipFile(os.path.join(jdir, covers[0])) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", "", xml)
        head = FIVE["reasons"][0]["headline"][:28]
        check(head in text,
              "THE DOCX ITSELF contains the first reason - the artifact, not the struct",
              head)
        check(all(r["headline"][:20] in text for r in FIVE["reasons"]),
              "and all five of them")
finally:
    electronic.RC.tailor = _real_tailor

print("-" * 78)
print("%d checks run, %d failed" % (_ran[0], len(_fails)))
if _fails:
    print("FAILED: " + ", ".join(_fails))
sys.exit(1 if _fails else 0)
