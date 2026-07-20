"""jd_ingest.py — turn ANY job input into clean JD text + metadata.

Contract (every function returns the SAME dict shape):

    {"status": "ok" | "needs_paste" | "needs_local_fetch" | "error",
     "source": "text" | "greenhouse" | "lever" | "smartrecruiters" | "ashby" |
               "workday" | "html" | "blocked" | "error",
     "url": "<input url or ''>",
     "title": str, "company": str, "location": str,
     "text": str,          # clean JD body ('' unless status == ok)
     "note": str}          # human-readable explanation (why we could not fetch, etc.)

HONESTY RULE (project standing rule): we NEVER fabricate JD content. If a host blocks
datacenter IPs (LinkedIn / Indeed / Glassdoor) we say so and ask for a paste — we do not
guess, and we do not return a half-scraped consent page as if it were the job description.

Tiers, in descending reliability:
  1. text        — the user pasted it. Always works. This is the PRIMARY path.
  2. public JSON — Greenhouse / Lever / SmartRecruiters / Ashby / Workday CXS. Structured,
                   unauthenticated, stable. Real title/company/location.
  3. generic HTML— any company career page. schema.org JSON-LD first, then tag-strip.
  4. blocked     — LinkedIn / Indeed / Glassdoor / StepStone / Xing: return needs_paste.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any
from urllib.parse import urlparse, urlsplit

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_BYTES = 3000000            # don't swallow a 50 MB page
MAX_TEXT = 40000               # JD text we keep (plenty; prompts cap lower)

# Hosts that actively block datacenter IPs / require a logged-in session.
# Scraping these from a server returns a login wall or a 999 - never a JD.
BLOCKED = (
    "linkedin.com", "indeed.com", "indeed.co.uk", "indeed.de", "glassdoor.com",
    "glassdoor.de", "glassdoor.co.uk", "ziprecruiter.com", "monster.com",
    "monster.de", "stepstone.de", "xing.com", "dice.com",
)


# --------------------------------------------------------------------------- helpers
def _blank(status: str = "error", source: str = "error", url: str = "", note: str = "") -> dict:
    return {"status": status, "source": source, "url": url,
            "title": "", "company": "", "location": "", "text": "", "note": note}


def _clean(s: Any) -> str:
    """Collapse whitespace, unescape entities, coerce anything to str."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = _html.unescape(s)
    s = s.replace(" ", " ").replace("​", "")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


_BLOCK_TAGS = ("p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "ul", "ol", "table")


def html_to_text(raw: str) -> str:
    """Strip HTML to readable text. No bs4/lxml dependency - deterministic regex pass.

    Drops script/style/noscript/svg/nav/header/footer/form, turns block tags into
    newlines and list items into '- ' bullets, then unescapes entities.
    """
    if not raw:
        return ""
    s = raw
    # Some ATS APIs return HTML-ESCAPED markup: Greenhouse's `content` is "&lt;p&gt;..." not
    # "<p>...". Verified against boards-api.greenhouse.io/v1/boards/stripe/jobs/7954688 (2026-07).
    # Strip-then-unescape would leak literal "<p>" INTO the JD text, so unescape first when the
    # payload has no real tags. Bounded loop: never more than two passes.
    for _ in range(2):
        if "<" not in s and "&lt;" in s:
            s = _html.unescape(s)
        else:
            break
    for tag in ("script", "style", "noscript", "svg", "iframe", "template",
                "nav", "header", "footer", "form", "select"):
        s = re.sub(r"<" + tag + r"\b[^>]*>.*?</" + tag + r"\s*>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<li\b[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"</?(" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = s.replace(" ", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _meta(raw: str, *names: str) -> str:
    """Pull a <meta property/name=...> content value."""
    for n in names:
        m = re.search(r"<meta[^>]+(?:property|name)\s*=\s*[\"']" + re.escape(n) + r"[\"'][^>]*>",
                      raw, re.I)
        if m:
            c = re.search(r"content\s*=\s*[\"'](.*?)[\"']", m.group(0), re.S | re.I)
            if c and c.group(1).strip():
                return _clean(c.group(1))
    return ""


def _jsonld_jobposting(raw: str) -> dict:
    """schema.org JobPosting in a <script type=application/ld+json> block.

    Most decent career pages emit this (Google for Jobs requires it), so it is by far
    the best generic signal - real title/company/location/description, no guessing.
    """
    pat = r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
    for m in re.finditer(pat, raw, re.S | re.I):
        blob = m.group(1).strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, dict):
                t = cur.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(str(x).lower() == "jobposting" for x in types if x):
                    return cur
                if "@graph" in cur:
                    stack.append(cur["@graph"])
    return {}


def _addr(node: Any) -> str:
    """Best-effort location string out of a schema.org jobLocation node."""
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return _clean(node) if node else ""
    a = node.get("address") or node
    if isinstance(a, list):
        a = a[0] if a else {}
    if isinstance(a, str):
        return _clean(a)
    if not isinstance(a, dict):
        return ""
    flat = []
    for p in (a.get("addressLocality"), a.get("addressRegion"), a.get("addressCountry")):
        if isinstance(p, dict):
            p = p.get("name") or p.get("addressCountry")
        if p:
            flat.append(_clean(p))
    return ", ".join(flat)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _looks_like_jd(text: str) -> bool:
    """Cheap sanity gate so we don't hand a cookie banner to the LLM as a JD."""
    if len(text) < 400:
        return False
    low = text.lower()
    hits = sum(1 for k in ("responsibilit", "qualificat", "experience", "you will",
                           "requirements", "about the role", "skills", "we offer",
                           "aufgaben", "profil", "wir bieten", "erfahrung")
               if k in low)
    return hits >= 2


# --------------------------------------------------------------------------- primary path
def _guess_title_company(body: str) -> tuple:
    """Very conservative header sniff. Returns ('','') rather than a bad guess."""
    title = ""
    company = ""
    for ln in body.split("\n")[:12]:
        ln = ln.strip(" #*-–—")
        if not ln or len(ln) > 120:
            continue
        m = re.match(r"^(?:job\s*)?title\s*[:\-]\s*(.+)$", ln, re.I)
        if m and not title:
            title = _clean(m.group(1))
            continue
        m = re.match(r"^(?:company|employer|organisation|organization)\s*[:\-]\s*(.+)$", ln, re.I)
        if m and not company:
            company = _clean(m.group(1))
            continue
        if not title and 8 <= len(ln) <= 90 and not ln.endswith((".", ":")):
            title = _clean(ln)
    return title, company


def from_text(text: str, title: str = "", company: str = "", location: str = "",
              url: str = "") -> dict:
    """Pasted JD. ALWAYS works - this is the path we tell users to prefer."""
    body = _clean(text)
    if len(body) < 40:
        return _blank("error", "text", url,
                      "The pasted job description is too short to tailor against "
                      "(need at least ~40 characters of real JD text).")
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT]
    guess_t, guess_c = _guess_title_company(body)
    return {"status": "ok", "source": "text", "url": url,
            "title": _clean(title) or guess_t,
            "company": _clean(company) or guess_c,
            "location": _clean(location),
            "text": body,
            "note": "Parsed from pasted text."}


# --------------------------------------------------------------------------- ATS parsers
_RE_GREENHOUSE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?(?P<board>[A-Za-z0-9_.-]+)"
    r"(?:/jobs/(?P<jid>\d+))?", re.I)
_RE_GH_QS = re.compile(r"[?&](?:gh_jid|for)=(?P<v>[A-Za-z0-9_.-]+)", re.I)
_RE_LEVER = re.compile(r"jobs\.(?:eu\.)?lever\.co/(?P<co>[A-Za-z0-9_.-]+)/(?P<jid>[A-Za-z0-9-]+)", re.I)
_RE_SR = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?:[^/]*/)?(?P<co>[A-Za-z0-9_.-]+)/(?P<jid>\d+)", re.I)
_RE_ASHBY = re.compile(r"jobs\.ashbyhq\.com/(?P<org>[A-Za-z0-9_.-]+)/(?P<jid>[A-Za-z0-9-]+)", re.I)
_RE_WD = re.compile(
    r"(?P<host>[a-z0-9-]+\.(?:wd\d+\.)?myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"(?P<site>[A-Za-z0-9_-]+)/(?:job/)?(?P<path>.+)$", re.I)


async def _get(client, url: str):
    return await client.get(url, headers={"User-Agent": UA,
                                          "Accept": "application/json, text/html;q=0.9"})


async def _greenhouse(client, url: str):
    m = _RE_GREENHOUSE.search(url)
    board = m.group("board") if m else ""
    jid = (m.group("jid") if m else "") or ""
    if not jid:
        q = _RE_GH_QS.search(url)
        if q and q.group("v").isdigit():
            jid = q.group("v")
    if not board or not jid:
        return None
    api = "https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s?questions=false" % (board, jid)
    r = await _get(client, api)
    if r.status_code != 200:
        return _blank("error", "greenhouse", url,
                      "Greenhouse public API returned HTTP %s for board '%s' job %s. "
                      "Paste the JD text instead." % (r.status_code, board, jid))
    d = r.json()
    txt = html_to_text(d.get("content") or "")
    loc = ""
    if isinstance(d.get("location"), dict):
        loc = d["location"].get("name", "")
    return {"status": "ok" if txt else "needs_paste", "source": "greenhouse", "url": url,
            "title": _clean(d.get("title")), "company": _clean(board.replace("-", " ").title()),
            "location": _clean(loc), "text": txt[:MAX_TEXT],
            "note": "Greenhouse public board API (unauthenticated)."}


async def _lever(client, url: str):
    m = _RE_LEVER.search(url)
    if not m:
        return None
    co, jid = m.group("co"), m.group("jid")
    api = "https://api.lever.co/v0/postings/%s/%s?mode=json" % (co, jid)
    r = await _get(client, api)
    if r.status_code != 200:
        return _blank("error", "lever", url,
                      "Lever public API returned HTTP %s for %s/%s. Paste the JD text instead."
                      % (r.status_code, co, jid))
    d = r.json()
    body = d.get("descriptionPlain") or html_to_text(d.get("description") or "")
    for lst in d.get("lists") or []:
        body += "\n\n" + _clean(lst.get("text")) + "\n" + html_to_text(lst.get("content") or "")
    body += "\n\n" + (d.get("additionalPlain") or html_to_text(d.get("additional") or ""))
    cats = d.get("categories") or {}
    return {"status": "ok", "source": "lever", "url": url,
            "title": _clean(d.get("text")), "company": _clean(co.replace("-", " ").title()),
            "location": _clean(cats.get("location")), "text": _clean(body)[:MAX_TEXT],
            "note": "Lever public postings API (unauthenticated)."}


async def _smartrecruiters(client, url: str):
    m = _RE_SR.search(url)
    if not m:
        return None
    co, jid = m.group("co"), m.group("jid")
    api = "https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (co, jid)
    r = await _get(client, api)
    if r.status_code != 200:
        return _blank("error", "smartrecruiters", url,
                      "SmartRecruiters public API returned HTTP %s. Paste the JD text instead."
                      % r.status_code)
    d = r.json()
    sec = (d.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        node = sec.get(key) or {}
        htitle = _clean(node.get("title"))
        txt = html_to_text(node.get("text") or "")
        if txt:
            parts.append((htitle + "\n" if htitle else "") + txt)
    loc = d.get("location") or {}
    locs = ", ".join(_clean(loc.get(k)) for k in ("city", "region", "country") if loc.get(k))
    return {"status": "ok", "source": "smartrecruiters", "url": url,
            "title": _clean(d.get("name")),
            "company": _clean((d.get("company") or {}).get("name") or co),
            "location": locs, "text": _clean("\n\n".join(parts))[:MAX_TEXT],
            "note": "SmartRecruiters public postings API (unauthenticated)."}


async def _ashby(client, url: str):
    m = _RE_ASHBY.search(url)
    if not m:
        return None
    org, jid = m.group("org"), m.group("jid")
    api = "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % org
    r = await _get(client, api)
    if r.status_code != 200:
        return _blank("error", "ashby", url,
                      "Ashby public job-board API returned HTTP %s for '%s'. "
                      "Paste the JD text instead." % (r.status_code, org))
    d = r.json()
    jobs = d.get("jobs") or []
    hit = next((j for j in jobs if str(j.get("id")) == jid), None)
    if hit is None:
        hit = next((j for j in jobs if jid in str(j.get("jobUrl", ""))), None)
    if hit is None:
        return _blank("needs_paste", "ashby", url,
                      "Ashby board '%s' is public but posting '%s' was not in the feed "
                      "(%d open roles listed) - it may be closed or internal. Paste the JD text."
                      % (org, jid, len(jobs)))
    body = hit.get("descriptionPlain") or html_to_text(hit.get("descriptionHtml") or "")
    return {"status": "ok", "source": "ashby", "url": url,
            "title": _clean(hit.get("title")),
            "company": _clean(d.get("name") or org),
            "location": _clean(hit.get("location")),
            "text": _clean(body)[:MAX_TEXT],
            "note": "Ashby public job-board API (unauthenticated)."}


async def _workday(client, url: str):
    """Workday CXS JSON. Public for most tenants, but a tenant can disable it - so a
    failure here is reported honestly, never patched over with a guess."""
    m = _RE_WD.search(url)
    if not m:
        return None
    host, site, path = m.group("host"), m.group("site"), m.group("path")
    tenant = host.split(".")[0]
    path = path.split("?")[0].strip("/")
    api = "https://%s/wday/cxs/%s/%s/job/%s" % (host, tenant, site, path)
    try:
        r = await client.get(api, headers={"User-Agent": UA, "Accept": "application/json"})
    except Exception as e:
        return _blank("needs_paste", "workday", url,
                      "Workday CXS request failed (%s). Paste the JD text." % type(e).__name__)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return _blank("needs_paste", "workday", url,
                      "Workday CXS endpoint returned HTTP %s for tenant '%s' - this tenant does "
                      "not expose the public JSON. Paste the JD text." % (r.status_code, tenant))
    try:
        d = (r.json() or {}).get("jobPostingInfo") or {}
    except Exception:
        d = {}
    body = html_to_text(d.get("jobDescription") or "")
    return {"status": "ok" if body else "needs_paste", "source": "workday", "url": url,
            "title": _clean(d.get("title")), "company": _clean(tenant.replace("-", " ").title()),
            "location": _clean(d.get("location")), "text": body[:MAX_TEXT],
            "note": "Workday CXS public JSON."}


async def _generic_html(client, url: str) -> dict:
    """Any career page. JSON-LD JobPosting first (reliable), then tag-stripped body."""
    try:
        r = await _get(client, url)
    except Exception as e:
        return _blank("error", "html", url,
                      "Could not fetch the page (%s: %s). Paste the job description text instead."
                      % (type(e).__name__, e))
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("location", "")
        same = _host(loc) == _host(url) if loc.startswith("http") else True
        return _blank("needs_paste", "html", url,
                      "The page redirected (HTTP %s -> %s). We do not follow redirects off-host; "
                      "paste the JD text instead." % (r.status_code, loc[:120])) if not same else \
            _blank("needs_local_fetch", "html", url,
                   "The page redirected on-host (HTTP %s). Open it in your browser and paste the "
                   "final job description text." % r.status_code)
    if r.status_code >= 400:
        return _blank("needs_paste", "html", url,
                      "The page returned HTTP %s. Many career sites block server-side fetches - "
                      "paste the job description text instead." % r.status_code)
    raw = r.text[:MAX_BYTES]

    ld = _jsonld_jobposting(raw)
    if ld:
        body = html_to_text(str(ld.get("description") or ""))
        org = ld.get("hiringOrganization") or {}
        company = _clean(org.get("name") if isinstance(org, dict) else org)
        if body and len(body) > 200:
            return {"status": "ok", "source": "html", "url": url,
                    "title": _clean(ld.get("title")), "company": company,
                    "location": _addr(ld.get("jobLocation")),
                    "text": body[:MAX_TEXT],
                    "note": "schema.org JobPosting (JSON-LD) found on the page."}

    body = html_to_text(raw)
    title = _meta(raw, "og:title", "twitter:title")
    if not title:
        t = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        title = _clean(t.group(1)) if t else ""
    company = _meta(raw, "og:site_name") or _host(url).replace("www.", "").split(".")[0].title()
    if not _looks_like_jd(body):
        return _blank("needs_paste", "html", url,
                      "Fetched the page but could not find a job description in it (likely "
                      "JavaScript-rendered or behind a consent wall). Paste the JD text - "
                      "that path always works.")
    return {"status": "ok", "source": "html", "url": url,
            "title": title, "company": company, "location": "",
            "text": body[:MAX_TEXT],
            "note": "Extracted from page HTML (best-effort). Verify the title/company fields."}


# --------------------------------------------------------------------------- entry point
async def from_url(url: str) -> dict:
    """Fetch + extract a JD from a URL. Never raises; always returns the contract dict."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return _blank("error", "error", url, "URL must start with http:// or https://")
    try:
        p = urlparse(url)
    except Exception:
        return _blank("error", "error", url, "Malformed URL.")
    host = (p.hostname or "").lower()
    if not host:
        return _blank("error", "error", url, "Malformed URL (no host).")

    if any(host == b or host.endswith("." + b) for b in BLOCKED):
        return _blank(
            "needs_paste", "blocked", url,
            "%s blocks server-side fetches (datacenter IPs get a login wall or HTTP 999). "
            "We will not guess at a job description. Open the posting in your browser, copy the "
            "job description text and paste it - that path always works." % host)

    try:
        # follow_redirects=False: a redirect to a login/consent domain is not our JD.
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            for parser in (_greenhouse, _lever, _smartrecruiters, _ashby, _workday):
                name = parser.__name__.strip("_")
                try:
                    out = await parser(client, url)
                except Exception as e:
                    out = _blank("needs_paste", name, url,
                                 "%s parse failed (%s: %s). Paste the JD text."
                                 % (name, type(e).__name__, e))
                if out is not None:
                    return out
            return await _generic_html(client, url)
    except Exception as e:
        return _blank("error", "error", url,
                      "Fetch failed (%s: %s). Paste the JD text instead." % (type(e).__name__, e))


async def ingest(text: str = "", url: str = "") -> dict:
    """One door: prefer pasted text (always reliable), else fetch the URL."""
    if text and len(text.strip()) >= 40:
        return from_text(text, url=url)
    if url:
        return await from_url(url)
    return _blank("error", "error", "", "Provide either `text` (pasted JD) or `url`.")
