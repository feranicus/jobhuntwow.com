"""Ground-truth DOM inspector — connect to the sandbox Chrome over CDP and dump the REAL form fields,
buttons, and data-automation-ids of whatever ATS page is currently open. Use this to write selectors
that actually match a given tenant instead of guessing.

    python jhw.py inspect              # dump the front ATS tab
Output goes to stdout AND to out/dom_dump.json for exact selectors.
"""
from __future__ import annotations
import asyncio, json, os
from playwright.async_api import async_playwright

CDP_URL = os.getenv("JHW_CDP_URL", "http://127.0.0.1:9222").replace("localhost", "127.0.0.1")
OUT = os.getenv("JHW_OUT", "/agent/out")

JS = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelFor = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) t = l.innerText; }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t) { const w = el.closest('[data-automation-id]'); if (w) t = (w.getAttribute('aria-label')||''); }
    return (t||'').replace(/\s+/g,' ').trim().slice(0,80);
  };
  const pick = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    dai: el.getAttribute('data-automation-id') || '',
    name: el.getAttribute('name') || '',
    id: el.id || '',
    ph: el.getAttribute('placeholder') || '',
    aria: el.getAttribute('aria-label') || '',
    label: labelFor(el),
    text: (el.innerText||'').replace(/\s+/g,' ').trim().slice(0,60),
    vis: vis(el),
  });
  const inputs = [...document.querySelectorAll('input,textarea,select')].filter(vis).map(pick);
  const buttons = [...document.querySelectorAll('button,[role="button"],a')].filter(vis)
      .map(pick).filter(b => b.text || b.dai || b.aria);
  const dais = [...new Set([...document.querySelectorAll('[data-automation-id]')]
      .map(e => e.getAttribute('data-automation-id')))].slice(0, 120);
  return { url: location.href, title: document.title, inputs, buttons, dais };
}
"""


async def run() -> dict:
    pw = await async_playwright().start(); browser = None
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = None
        for p in ctx.pages:                       # prefer a real ATS tab (not LinkedIn/blank)
            u = (p.url or "").lower()
            if u.startswith("http") and "linkedin.com" not in u and "about:blank" not in u:
                page = p
        page = page or (ctx.pages[-1] if ctx.pages else await ctx.new_page())
        await page.bring_to_front()
        data = await page.evaluate(JS)
        os.makedirs(OUT, exist_ok=True)
        json.dump(data, open(os.path.join(OUT, "dom_dump.json"), "w"), indent=2, ensure_ascii=False)
        # readable summary
        print(f"\nURL:   {data['url']}\nTITLE: {data['title']}\n")
        print("=== INPUTS (visible) ===")
        for f in data["inputs"]:
            print(f"  [{f['tag']}/{f['type']}] dai='{f['dai']}' name='{f['name']}' id='{f['id']}' "
                  f"label='{f['label']}' ph='{f['ph']}'")
        print("\n=== BUTTONS / LINKS (visible) ===")
        for b in data["buttons"][:40]:
            print(f"  [{b['tag']}] text='{b['text']}' dai='{b['dai']}' aria='{b['aria']}'")
        print("\n=== data-automation-id values on page ===")
        print("  " + ", ".join(d for d in data["dais"] if d))
        print(f"\n[OK] full dump -> {OUT}/dom_dump.json")
        return data
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(run())
