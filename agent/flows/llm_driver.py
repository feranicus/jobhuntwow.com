"""Server-side text-DOM LLM driver — 'page-agent, but ours' (no CDN, no screenshots, no vision).

When deterministic stalls, this finishes the ATS form by asking our DO model (via the droplet proxy)
for ONE action at a time over a compact TEXT view of the page's interactive elements, executing each
with Playwright. When it needs a value it doesn't have, it asks the candidate on TELEGRAM and feeds the
reply back into the model. Never submits.
"""
from __future__ import annotations
import asyncio, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright
import httpx
try:
    import ask
except Exception:
    ask = None

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
PROXY   = os.getenv("JHW_PROXY_BASE", "http://host.docker.internal:8000/v1")
TOKEN   = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL   = os.getenv("JHW_LLMDRIVER_MODEL", "jhw-driver")

# index every visible interactive element and tag it with data-jhw-idx so we can act on it by index
ENUM_JS = r"""
() => {
  const vis=(el)=>{const r=el.getBoundingClientRect();const s=getComputedStyle(el);
    return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
  const lab=(el)=>{let t='';if(el.id){const l=document.querySelector(`label[for="${el.id}"]`);if(l)t=l.innerText;}
    if(!t){const l=el.closest('label');if(l)t=l.innerText;}
    return (t||el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').replace(/\s+/g,' ').trim().slice(0,70);};
  const sel='input,textarea,select,button,[role=button],[role=combobox],[role=option],a[href]';
  const out=[];
  [...document.querySelectorAll(sel)].forEach((el)=>{
    if(!vis(el))return;
    if(el.tagName==='INPUT'&&el.type==='file')return;   // file inputs handled by Playwright, not the LLM
    const idx=out.length; el.setAttribute('data-jhw-idx',idx);
    out.push({i:idx,tag:el.tagName.toLowerCase(),type:el.getAttribute('type')||'',
      role:el.getAttribute('role')||'',label:lab(el),
      text:(el.innerText||'').replace(/\s+/g,' ').trim().slice(0,50),
      val:(el.value||'').slice(0,40)});
  });
  const review=/review|summary/i.test(location.href) ||
    !![...document.querySelectorAll('button,[role=button]')].find(b=>/^\s*submit/i.test(b.innerText||''));
  return {url:location.href, review, els:out};
}
"""

SYS = ("You are filling a job application for a candidate, one step at a time. You receive the CURRENT "
       "page's interactive elements as JSON (each with an index i) plus the candidate facts. Reply with "
       "ONLY a JSON object for the SINGLE next action:\n"
       '{"action":"fill|click|ask|done","i":<index>,"value":"<text>","question":"<for ask>","why":"<short>"}\n'
       "Rules: fill text inputs from the facts (name, email, phone, address, etc.). To answer a dropdown, "
       "click it (action click) — its options will appear next turn, then click the right option. Click "
       "'Save and Continue'/'Next' to advance once a page's required fields are done. Click 'Add' only if a "
       "REQUIRED section is empty. Tick required consent checkboxes (click). If you need a value that is NOT "
       "in the facts (visa/sponsorship, salary, notice period, a custom question), use action ask with a clear "
       "question. When you see a Review/Submit summary page, use action done. NEVER click Submit.")


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


async def _llm(messages) -> str:
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                         json={"model": MODEL, "messages": messages, "temperature": 0.1})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def _connect(pw):
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    for p in ctx.pages:
        u = (p.url or "").lower()
        if u.startswith("http") and "linkedin.com" not in u:
            page = p
    return browser, ctx, page


async def run(facts: str, resume_path: str = "", max_steps: int = 25) -> dict:
    r = {"ok": False, "steps": 0, "note": "", "actions": []}
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front()
        for step in range(max_steps):
            r["steps"] = step + 1
            await page.wait_for_timeout(1200)
            try:
                snap = await page.evaluate(ENUM_JS)
            except Exception as e:
                r["note"] = f"enumerate failed: {e}"; break
            if snap.get("review"):
                r["ok"] = True; r["note"] = "reached Review"; break
            els = snap.get("els", [])
            msg = [{"role": "system", "content": SYS},
                   {"role": "user", "content": f"FACTS:\n{facts}\n\nELEMENTS:\n{json.dumps(els)[:7000]}\n\nNext action?"}]
            try:
                act = _parse(await _llm(msg))
            except Exception as e:
                r["note"] = f"llm error: {e}"; break
            a = (act.get("action") or "").lower()
            r["actions"].append(a + (f":{act.get('i')}" if "i" in act else ""))
            if a == "done":
                r["ok"] = True; r["note"] = "model reported done"; break
            if a == "ask":
                q = act.get("question", "The application needs a value.")
                reply = (await ask.ask_human(q)) if ask else ""
                facts += f"\n(Q: {q} -> A: {reply or 'no answer'})"
                continue
            i = act.get("i")
            sel = f"[data-jhw-idx='{i}']"
            try:
                if a == "fill":
                    await page.fill(sel, str(act.get("value", "")))
                elif a == "click":
                    await page.click(sel)
                else:
                    continue
            except Exception as e:
                facts += f"\n(action {a} on {i} failed: {e})"
            await page.wait_for_timeout(600)
        r["note"] = r["note"] or f"stopped after {r['steps']} steps"
        return r
    except Exception as e:
        r["note"] = f"llm_driver error: {e}"; return r
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    print(asyncio.run(run("Name: Test Candidate; email test@example.com", max_steps=5)))
