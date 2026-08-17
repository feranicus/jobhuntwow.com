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
# DEFAULT = the SHARED DOCKER NETWORK name, not host.docker.internal. Windows refused to bind
# 127.0.0.1:8000 (WSAEACCES, Hyper-V reserved range) so the host-port path is gone; compose sets
# JHW_PROXY_BASE explicitly, and a default that cannot work is a trap for anything run by hand.
PROXY   = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN   = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL   = os.getenv("JHW_LLMDRIVER_MODEL", "jhw-driver")

# index every visible interactive element and tag it with data-jhw-idx so we can act on it by index
ENUM_JS = r"""
() => {
  // HONEYPOT / BOT-TRAP GUARD. MEASURED on the live Accenture Workday form 2026-08-16:
  //   input[data-automation-id="beecatcher"] name="website"
  //   label "Enter website. This input is for robots only, do not enter if you're human."
  //   hidden by clip:rect(1px,1px,1px,1px) + clip-path:polygon(0 0,0 0,0 0,0 0), 1x0.01px,
  //   offsetParent===null -- BUT display:block, visibility:visible, tabIndex:0.
  // The OLD predicate was `r.width>0&&r.height>0&&visibility!=='hidden'&&display!=='none'`
  // and it returned TRUE, so the trap was enumerated and handed to the model as an ordinary
  // text input. display/visibility CANNOT see clip-hiding. Filling it self-identifies as a
  // robot, which is worse than any stall: the application is silently discarded.
  // NOTE offsetParent is null for position:fixed too, so that case is excluded explicitly or
  // every fixed modal button would vanish from the index.
  const vis=(el)=>{const r=el.getBoundingClientRect();const s=getComputedStyle(el);
    if(s.visibility==='hidden'||s.display==='none'||s.opacity==='0')return false;
    if(r.width<2||r.height<2)return false;                       // 1x0.01px traps
    if(el.offsetParent===null&&s.position!=='fixed'&&s.position!=='sticky')return false;
    // aria-hidden REMOVED 2026-08-16. I added it here in the same honeypot fix that added it to
    // workday.py's _CTRL_JS, and there it produced `0 control(s) indexed` on the live Accenture gate
    // for FOUR consecutive runs: Workday marks the app container aria-hidden="true" when a modal
    // opens (correct modal practice) and renders the modal INSIDE it, so every control in the modal
    // has an aria-hidden ancestor. THIS DRIVER IS THE ONE THAT SUBMITTED THE NTT PHENOM
    // APPLICATION, and it did so with a predicate that had no such check -- so the line was a
    // LATENT REGRESSION in the only flow that has ever worked end to end. aria-hidden is an
    // accessibility hint, not a visibility fact; the honeypot is caught by geometry below.
    const cp=(s.clipPath||'').replace(/\s+/g,' ');
    if(/^polygon\(/.test(cp)&&!/[1-9]/.test(cp))return false;     // polygon(0 0,0 0,...)
    const cl=(s.clip||'').replace(/\s+/g,' ');
    if(/^rect\(/.test(cl)&&!/[2-9]|\d\d/.test(cl))return false;   // rect(1px,1px,1px,1px)
    return true;};
  const lab=(el)=>{let t='';if(el.id){const l=document.querySelector(`label[for="${el.id}"]`);if(l)t=l.innerText;}
    if(!t){const l=el.closest('label');if(l)t=l.innerText;}
    return (t||el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').replace(/\s+/g,' ').trim().slice(0,70);};
  const cssOf=(el)=>{const q=(v)=>String(v).replace(/["\\]/g,'\\$&');
    if(el.id) return '#'+CSS.escape(el.id);
    if(el.name) return el.tagName.toLowerCase()+'[name="'+q(el.name)+'"]';
    const al=el.getAttribute('aria-label');
    if(al) return el.tagName.toLowerCase()+'[aria-label="'+q(al)+'"]';
    const ph=el.getAttribute('placeholder');
    if(ph) return el.tagName.toLowerCase()+'[placeholder="'+q(ph)+'"]';
    return '';};
  // TYPEAHEAD detection: a text box whose value must come from a suggestion list stores an
  // ARRAY, so typing into it can never satisfy the schema ("should be array").
  const isArr=(el)=>el.tagName==='INPUT' &&
    (el.getAttribute('role')==='combobox' || !!el.getAttribute('aria-autocomplete') ||
     !!el.getAttribute('aria-owns') || el.getAttribute('aria-expanded')!==null ||
     el.getAttribute('autocomplete')==='off' && !!el.getAttribute('aria-controls'));
  const sel='input,textarea,select,button,[role=button],[role=combobox],[role=option],a[href]';
  // Indices are re-assigned on every read. Without clearing the previous ones, a re-rendered
  // page keeps OLD data-jhw-idx attributes on detached/hidden nodes and `[data-jhw-idx='11']`
  // then resolves to the wrong element (or several) - which is exactly how the date field
  // timed out four times while being plainly visible on screen.
  document.querySelectorAll('[data-jhw-idx]').forEach(e=>e.removeAttribute('data-jhw-idx'));
  const out=[];
  [...document.querySelectorAll(sel)].forEach((el)=>{
    if(!vis(el))return;
    if(el.tagName==='INPUT'&&el.type==='file')return;   // file inputs handled by Playwright, not the LLM
    const idx=out.length; el.setAttribute('data-jhw-idx',idx);
    out.push({i:idx,tag:el.tagName.toLowerCase(),type:el.getAttribute('type')||'',
      role:el.getAttribute('role')||'',label:lab(el),
      text:(el.innerText||'').replace(/\s+/g,' ').trim().slice(0,50),
      val:(el.value||'').slice(0,40),
      checked:(el.type==='checkbox'||el.type==='radio')?!!el.checked:undefined,
      placeholder:el.getAttribute('placeholder')||'',
      arr:isArr(el)||undefined,
      css:cssOf(el),
      opts:(el.tagName==='SELECT'
            ? [...el.options].map(o=>(o.textContent||'').trim()).filter(Boolean).slice(0,60)
            : undefined)});
  });
  // Required-but-empty fields: the ONLY reliable definition of "not ready to advance".
  const missing=[];
  [...document.querySelectorAll('input,select,textarea')].forEach((el)=>{
    if(!vis(el))return;
    if(el.type==='hidden'||el.type==='file'||el.disabled)return;
    const req = el.required || el.getAttribute('aria-required')==='true' ||
                /\*/.test((el.closest('.form-group,.field,li,div')||{innerText:''}).innerText.slice(0,120));
    if(!req)return;
    const empty = (el.type==='checkbox'||el.type==='radio') ? !el.checked : !String(el.value||'').trim();
    if(empty){
      const idx = el.getAttribute('data-jhw-idx');
      missing.push({i: idx===null?null:Number(idx), label: lab(el).slice(0,60), type: el.type||el.tagName});
    }
  });
  // Validation messages, EACH TIED TO THE FIELD IT BELONGS TO. A bare list of strings
  // (['should be array','should be array']) is unactionable: the model guessed which field
  // it meant and guessed wrong 8 times in a row. The DOM knows; ask the DOM.
  const errNodes=[...document.querySelectorAll('*')]
    .filter(e=>vis(e)&&e.children.length===0)
    .map(e=>({el:e,t:(e.innerText||'').trim()}))
    .filter(o=>o.t&&o.t.length<90&&/(is required|should be|invalid|must be|enter all required)/i.test(o.t))
    .slice(0,12);
  const fields=[...document.querySelectorAll(
      'input:not([type=hidden]):not([type=file]),select,textarea')].filter(vis);
  const errors=errNodes.map(o=>{
    // A page-level SUMMARY ("Please enter all required fields:") belongs to NO field. Blaming
    // it on the nearest input made the driver attack "How did you hear about this position?"
    // twelve times while the field that was actually empty sat further down the form.
    if(/(enter all required|please correct|following errors|fix the errors)/i.test(o.t))
      return {msg:o.t, i:null, label:'', css:'', tag:'', arr:false, val:'', summary:true};
    // Otherwise: a validation message is rendered DIRECTLY UNDER its own field. Geometry is
    // reliable where DOM ancestry is not (the markup nests differently per widget).
    const mr=o.el.getBoundingClientRect();
    let f=null, best=1e9;
    for(const x of fields){
      const xr=x.getBoundingClientRect();
      const dy=mr.top-xr.bottom;
      if(dy<-4||dy>90)continue;                                   // must sit just below it
      if(xr.left>mr.right+40||xr.right<mr.left-40)continue;        // and line up horizontally
      if(dy<best){best=dy;f=x;}
    }
    if(!f){                                                        // fall back to the container
      let p=o.el,hop=0;
      while(p&&hop<5&&!f){
        const c=[...p.querySelectorAll('input:not([type=hidden]):not([type=file]),select,textarea')]
                .filter(vis);
        const before=c.filter(x=>x.compareDocumentPosition(o.el)&Node.DOCUMENT_POSITION_FOLLOWING);
        f=before.length?before[before.length-1]:null;
        p=p.parentElement;hop++;
      }
    }
    const idx=f?f.getAttribute('data-jhw-idx'):null;
    return {msg:o.t, i:idx===null||idx===undefined?null:Number(idx),
            label:f?lab(f):'', css:f?cssOf(f):'',
            tag:f?f.tagName.toLowerCase():'', arr:f?!!isArr(f):false,
            val:f?String(f.value||'').slice(0,40):'', summary:false};
  });
  const review=/review|summary/i.test(location.href) ||
    !![...document.querySelectorAll('button,[role=button]')].find(b=>/^\s*submit/i.test(b.innerText||''));
  return {url:location.href, review, els:out, missing, errors};
}
"""

FILL_JS = """
(args) => {
  const {label, value} = args;
  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const lab = norm(label).replace(/[*:]/g, '').slice(0, 40);
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  // Do NOT whitelist types: NTT's date box is not input[type=text] and the whitelist made
  // FILL_JS answer 'no input matched the label' three times on a field that was on screen.
  const bad = /^(checkbox|radio|file|hidden|submit|button|image|reset)$/i;
  const ins = [...document.querySelectorAll('input,textarea')]
    .filter(el => vis(el) && !bad.test(el.type || '') && !el.disabled && !el.readOnly);
  let target = null;
  for (const el of ins) {
    const around = norm((el.closest('div,li,tr,fieldset,section') || el.parentElement || {}).innerText);
    if (lab && around.includes(lab.slice(0, 25))) { target = el; break; }
  }
  if (!target && args.placeholder)
    target = ins.find(el => norm(el.getAttribute('placeholder')) === norm(args.placeholder)) || null;
  if (!target) return {ok: false, why: 'no input matched the label',
                       saw: ins.slice(0, 8).map(el => (el.getAttribute('placeholder') || el.name || el.type))};
  const proto = target.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement : window.HTMLInputElement;
  const d = Object.getOwnPropertyDescriptor(proto.prototype, 'value');
  try { d.set.call(target, value); } catch (e) { target.value = value; }
  target.dispatchEvent(new Event('input', {bubbles: true}));
  target.dispatchEvent(new Event('change', {bubbles: true}));
  target.dispatchEvent(new Event('blur', {bubbles: true}));
  return {ok: true, placeholder: target.getAttribute('placeholder') || ''};
}
"""


SELECT_JS = """
(args) => {
  const {label, want} = args;
  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const lab = norm(label).replace(/[*:]/g, '').slice(0, 40);
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  // Find the <select> whose surrounding text carries this question.
  const sels = [...document.querySelectorAll('select')].filter(vis);
  if (!sels.length) return {ok: false, why: 'no visible select'};
  let target = null;
  for (const el of sels) {
    const around = norm((el.closest('div,li,tr,fieldset,section') || el.parentElement || {}).innerText);
    if (lab && around.includes(lab.slice(0, 25))) { target = el; break; }
  }
  if (!target) target = sels.find(el => !el.value || /select an option/i.test(el.options[el.selectedIndex]?.text || ''));
  if (!target) return {ok: false, why: 'no select matched the label'};
  const opts = [...target.options].map(o => ({v: o.value, t: (o.text || '').trim()}));
  const real = opts.filter(o => o.v !== '' && !/^\\s*(select an option|--|please select)/i.test(o.t));
  if (!real.length) return {ok: false, why: 'only placeholder options'};
  const low = norm(want);
  let hit = real.find(o => norm(o.t) === low)
         || real.find(o => norm(o.t).startsWith(low.slice(0, 4)))
         || real.find(o => norm(o.t).includes(low) || low.includes(norm(o.t)))
         || real[0];                       // "just pick the first" - never leave it empty
  // A plain `target.value = x` sets what you SEE but React's value tracker then treats the
  // change event as a no-op, so the form model stays empty: the screenshot showed "Internet"
  // selected while the page kept insisting "This field is required" - forever. The native
  // setter defeats the tracker, exactly as FILL_JS already did for text inputs.
  const d = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value');
  try { d.set.call(target, hit.v); } catch (e) { target.value = hit.v; }
  if (target.value !== hit.v) {
    const k = [...target.options].findIndex(o => o.value === hit.v);
    if (k >= 0) target.selectedIndex = k;
  }
  target.dispatchEvent(new Event('input', {bubbles: true}));
  target.dispatchEvent(new Event('change', {bubbles: true}));
  target.dispatchEvent(new Event('blur', {bubbles: true}));
  return {ok: true, picked: hit.t, options: real.slice(0, 12).map(o => o.t),
          fallback: hit === real[0], verified: target.value === hit.v};
}
"""


PICK_JS = r"""
(args) => {
  const {want, anchor, first} = args;
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const a  = anchor ? document.querySelector(anchor) : null;
  const ar = a ? a.getBoundingClientRect() : null;
  // GEOMETRY, not DOM ancestry: the suggestion list renders directly UNDER its input (often in
  // a body-level portal). Without this the search matched the page nav and "picked" Sign up /
  // My Information / Review - which is exactly what the NTT run did, 3 times.
  const near = (el) => {
    if (!ar) return true;
    if (a && (el === a || el.contains(a))) return false;
    const r = el.getBoundingClientRect();
    return r.top >= ar.top - 4 && (r.top - ar.bottom) < 460 &&
           r.left < ar.right + 80 && r.right > ar.left - 80;
  };
  const sel = 'li,[role=option],[role=listitem],[class*=option],[class*=item],[class*=result],'
            + '[class*=suggest],[class*=menu] div,[class*=dropdown] div,div';
  const seen = new Set();
  // A row is a SUGGESTION, never a label or another form control. Without this the "first
  // option" fallback picked 'Zip/Postal Code:*' - the next field down the form.
  const rowOk = (el) => !el.closest('label') && el.tagName !== 'LABEL' &&
                        !el.querySelector('input,select,textarea,label');
  const collect = (root) => [...root.querySelectorAll(sel)]
    .filter(el => vis(el) && el.children.length <= 2 && rowOk(el) && near(el))
    .map(el => ({el, t: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()}))
    .filter(o => o.t && o.t.length > 0 && o.t.length < 70)
    .filter(o => !/^(should be|this field|is required|please enter)/i.test(o.t))
    .filter(o => !/[:*]$/.test(o.t))
    .filter(o => { const k = o.t.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; });
  // Prefer a real listbox if the widget rendered one; only then fall back to open geometry.
  let cands = [];
  const boxes = [...document.querySelectorAll(
      'ul,[role=listbox],[role=menu],[class*=suggest],[class*=autocomplete],[class*=dropdown]')]
    .filter(b => vis(b) && near(b));
  for (const b of boxes) { cands = collect(b); if (cands.length) break; }
  if (!cands.length) { seen.clear(); cands = collect(document); }
  if (!cands.length) return {picked: null, options: [], count: 0,
    debug: {boxes: boxes.length, anchor: !!a,
            anchorRect: ar ? {t: Math.round(ar.top), b: Math.round(ar.bottom),
                              l: Math.round(ar.left), r: Math.round(ar.right)} : null,
            rawRows: [...document.querySelectorAll(sel)].filter(vis).length,
            nearRows: [...document.querySelectorAll(sel)].filter(el => vis(el) && near(el)).length,
            rowOkRows: [...document.querySelectorAll(sel)]
                       .filter(el => vis(el) && near(el) && rowOk(el)).length}};
  const low = String(want || '').toLowerCase();
  let hit = null;
  if (low) {
    const st4 = low.slice(0, 4);
    hit = cands.find(o => o.t.toLowerCase() === low)
       || cands.find(o => o.t.toLowerCase().startsWith(st4))
       || cands.find(o => low.startsWith(o.t.toLowerCase().slice(0, 4)))
       || cands.find(o => o.t.toLowerCase().includes(low) || low.includes(o.t.toLowerCase()));
  }
  // The site may simply not offer the right answer (NTT has no "Hessen"). Standing instruction
  // from the candidate: take the FIRST option rather than stalling the whole application.
  if (!hit && first) hit = cands[0];
  if (!hit) return {picked: null, options: cands.slice(0, 15).map(o => o.t), count: cands.length};
  try { hit.el.scrollIntoView({block: 'center'}); } catch (e) {}
  hit.el.click();
  return {picked: hit.t, options: cands.slice(0, 15).map(o => o.t), count: cands.length,
          fallback: !low || hit === cands[0] && !(hit.t.toLowerCase().startsWith(low.slice(0, 4)))};
}
"""


SWEEP_JS = r"""
(defaults) => {
  // "are you willing to relocate - choose yes or no, it doesn't really matter" - so no select
  // is ever left on "Select an option". Known answers win; anything else takes the first real
  // option. A form that cannot advance because of an unanswered optional dropdown is a defect.
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const ph = /^\s*(select an option|please select|choose|--|-)\s*$/i;
  const set = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value');
    try { d.set.call(el, v); } catch (e) { el.value = v; }
    if (el.value !== v) { const k = [...el.options].findIndex(o => o.value === v);
                          if (k >= 0) el.selectedIndex = k; }
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur',   {bubbles: true}));
  };
  const done = [];
  for (const el of [...document.querySelectorAll('select')].filter(vis)) {
    if (el.disabled) continue;
    const cur = (el.options[el.selectedIndex] || {}).text || '';
    if (el.value && !ph.test(cur)) continue;                       // already answered
    const real = [...el.options].filter(o => o.value !== '' && !ph.test(o.text || ''));
    if (!real.length) continue;
    const around = norm((el.closest('div,li,tr,fieldset,section') || el.parentElement || {}).innerText);
    let want = null;
    for (const [kw, ans] of defaults) if (around.includes(kw)) { want = ans; break; }
    let hit = want ? real.find(o => norm(o.text) === norm(want)) : null;
    if (!hit) hit = real[0];
    set(el, hit.value);
    done.push({label: around.slice(0, 50), picked: (hit.text || '').trim(),
               guided: !!want, verified: el.value === hit.value});
  }
  return done;
}
"""

# Keyword -> answer for SWEEP_JS. These mirror `## screening_defaults` in userdata/candidate.md;
# anything not listed takes the first real option, because an unanswered dropdown must never be
# the reason an application cannot be submitted.
CHECK_JS = r"""
(a) => {
  const el = document.querySelector(a.sel);
  if (!el) return {ok: false, why: 'element not found'};
  if (el.type !== 'checkbox' && el.type !== 'radio') return {ok: false, why: 'not a checkbox'};
  // A CLICK TOGGLES. The model clicked the consent box on step 3, the site revalidated a tick
  // later and still showed "This field is required", so the model clicked it AGAIN - and
  // UNTICKED it. From then on Next could never succeed. Ticking is idempotent; clicking is not.
  if (el.checked) return {ok: true, already: true, checked: true};
  el.click();
  return {ok: true, already: false, checked: !!el.checked};
}
"""


SELECT_DEFAULTS = [
    ["relocate", "No"],
    ["travel assignment", "Yes"],
    ["willing to travel", "Yes"],
    ["worked for", "No"],
    ["current employee", "No"],
    ["noncompetition", "No"],
    ["non-compete", "No"],
    ["confidentiality agreement", "No"],
    ["sponsorship", "No"],
    ["visa", "No"],
    ["previously applied", "No"],
    ["criminal", "No"],
    ["how did you hear", "Internet"],
]

SYS = ("You are filling a job application for a candidate, one step at a time. You receive the CURRENT "
       "page's interactive elements as JSON (each with an index i) plus the candidate facts. Reply with "
       "ONLY a JSON object for the SINGLE next action:\n"
       '{"action":"fill|pick|select|click|ask|done","i":<index>,"value":"<text>","question":"<for ask>","why":"<short>"}\n'
       "For a <select> element use action SELECT and put the EXACT option text in value (the "
       "options are listed in its opts field). Never 'click' a native select.\n"
       "For a DATE field use available_start_date and match the placeholder format "
       "(e.g. dd-mm-yyyy). NEVER write 'ASAP' in a date field.\n"
       "NEVER choose a placeholder such as 'Select an option' or an empty value - that is not an "
       "answer. Pick a real option; if none matches the facts, pick the CLOSEST one.\n"
       "Each VALIDATION ERROR carries the LABEL of the field it belongs to. Act ONLY on THAT "
       "field. Never 'fix' an error by touching a different field that already has a value - "
       "that is a loop, not a fix.\n"
       "A field marked arr:true (or one whose error is 'should be array') is a TYPEAHEAD: it "
       "stores an ARRAY and is only satisfied by CLICKING a suggestion. Use action PICK. Typing "
       "into it can never work, no matter how many times you retry.\n"
       "If the site's list does not contain the exact answer, the closest option is acceptable "
       "and the FIRST option is acceptable - never stall an application over a missing option.\n"
       "A checkbox carries a `checked` field. If it is already true, DO NOT click it again - a "
       "second click UNTICKS it. Ignore a stale error about a box that is already ticked.\n"
       "Resume and cover letter are attached automatically - never ask the user to upload them.\n"
       "Rules: fill text inputs from the facts (name, email, phone, address, etc.). Use SELECT for "
       "native dropdowns; for custom comboboxes click to open, then click the right option. Click "
       "'Save and Continue'/'Next' to advance once a page's required fields are done. Click 'Add' only if a "
       "REQUIRED section is empty. Tick required consent checkboxes (click). If you need a value that is NOT "
       "in the facts (visa/sponsorship, salary, notice period, a custom question), use action ask with a clear "
       "question. When you see a Review/Submit summary page, use action done - the driver submits the "
       "application itself, so you must NOT click Submit yourself.")


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
    ACTION_TIMEOUT = int(os.getenv("JHW_ACTION_TIMEOUT", "8000"))
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    # AUDITED 2026-08-16: `page.set_default_timeout(8000)` was set INSIDE the per-step action loop, so
    # everything before the first iteration ran at Playwright's 30-SECOND default -- including
    # `_upload_files` (2 documents x 30s = 60s) and `_typeahead`'s 4-rung prefix ladder (4 x 30s =
    # 120s). Set it at CONNECT, on the CONTEXT as well as the page, or popups and new tabs stay at 30s.
    try:
        ctx.set_default_timeout(int(os.getenv("JHW_ACTION_TIMEOUT", "8000")))
    except Exception:
        pass
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    for p in ctx.pages:
        u = (p.url or "").lower()
        if u.startswith("http") and "linkedin.com" not in u:
            page = p
    page.set_default_timeout(ACTION_TIMEOUT)
    return browser, ctx, page



async def _typeahead(page, css: str, want: str, label: str = "") -> dict:
    """Fill a TYPEAHEAD (array-typed) field the only way it can be satisfied: type a prefix,
    wait for the suggestion list, CLICK a row. Typing alone leaves the component holding a
    string where the schema wants an array -> the site renders 'should be array' forever.

    Ladder of prefixes because the list is not always filtered the way you expect: the full
    value first, then 4/3/2 characters ("Hessen" -> "Hess" -> the site's own spelling "Hesse").
    If nothing matches at any prefix, take the FIRST row - the candidate's standing instruction
    is never to stall on a site whose list is simply missing the right answer.
    """
    want = (want or "").strip()
    ladder, seen = [], set()
    for p in (want, want[:4], want[:3], want[:2]):
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower()); ladder.append(p)
    if not ladder:
        ladder = [""]
    last = {}
    for n, pref in enumerate(ladder):
        try:
            await page.click(css, timeout=4000)
            await page.fill(css, "", timeout=4000)
            if pref:
                await page.type(css, pref, delay=90)
        except Exception as e:
            last = {"picked": None, "options": [], "why": f"could not type: {str(e)[:70]}"}
            continue
        await page.wait_for_timeout(1100)
        is_last = (n == len(ladder) - 1)
        res = await page.evaluate(PICK_JS, {"want": want, "anchor": css, "first": is_last}) or {}
        last = res
        if res.get("picked"):
            await page.wait_for_timeout(500)
            print(f"[llm_driver]      -> typeahead {label or css}: typed {pref!r} -> picked "
                  f"{res['picked']!r} of {res.get('count', 0)}"
                  + ("  [FIRST OPTION - site has no exact match]" if res.get("fallback") else ""),
                  flush=True)
            return res
    print(f"[llm_driver]      -> typeahead {label or css}: no suggestion matched {want!r}; "
          f"saw {(last.get('options') or [])[:8]}", flush=True)
    return last



DATE_JS = r"""
(a) => {
  const el = document.querySelector(a.sel);
  if (!el) return {ok: false, val: ''};
  const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  try { d.set.call(el, a.v); } catch (e) { el.value = a.v; }
  el.dispatchEvent(new Event('input',  {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  try { el.blur(); } catch (e) {}
  el.dispatchEvent(new Event('blur',   {bubbles: true}));
  return {ok: true, val: el.value || ''};
}
"""

READ_JS = "(s) => { const e = document.querySelector(s); return e ? (e.value || '') : ''; }"


async def _commit_date(page, sel: str, value: str, label: str = "") -> str:
    """Get a date INTO a picker-backed box and make it stay there.

    `Page.fill` opens the calendar overlay; Escape then closed it AND reverted the value, so the
    field was empty while the driver logged 'ok'. Blur commits where Escape reverts. Returns what
    the field actually holds afterwards - never an assumption.
    """
    try:
        got = (await page.evaluate(DATE_JS, {"sel": sel, "v": value}) or {}).get("val", "")
    except Exception:
        got = ""
    if got.strip():
        return got
    # The picker may only accept real keystrokes. Type, then commit with a blur (NOT Escape).
    try:
        await page.click(sel, timeout=4000)
        await page.fill(sel, "", timeout=4000)
        await page.type(sel, value, delay=70)
        await page.evaluate("(s)=>{const e=document.querySelector(s); if(e){e.blur();"
                            "e.dispatchEvent(new Event('blur',{bubbles:true}));}}", sel)
        await page.wait_for_timeout(500)
        got = await page.evaluate(READ_JS, sel) or ""
    except Exception as e:
        print(f"[llm_driver]      -> date keystroke fallback failed: {str(e)[:80]}", flush=True)
    return got


CAL_OPEN_JS = r"""
(a) => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const pops = [...document.querySelectorAll(
    '[class*=datepicker],[class*=DatePicker],[class*=calendar],[class*=Calendar],[role=dialog]')]
    .filter(vis);
  if (!pops.length) return {ok: false, why: 'no calendar is open'};
  // Wrappers nest ("...calendar-container" > "react-datepicker"), so "the last one in document
  // order" is a coin flip. Take the one that actually holds the controls.
  const pop = pops.map(p => ({p, n: p.querySelectorAll('select,[aria-label]').length}))
                  .sort((a, b) => b.n - a.n)[0].p;
  const setSel = (el, text) => {
    const hit = [...el.options].find(o => (o.text || '').trim().toLowerCase() === text.toLowerCase())
             || [...el.options].find(o => (o.text || '').trim().startsWith(text.slice(0, 3)));
    if (!hit) return false;
    const d = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value');
    try { d.set.call(el, hit.value); } catch (e) { el.value = hit.value; }
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  };
  const MON = /^(january|february|march|april|may|june|july|august|september|october|november|december)$/i;
  let month = null, year = null;
  for (const s of [...pop.querySelectorAll('select')].filter(vis)) {
    const t = [...s.options].map(o => (o.text || '').trim());
    if (t.some(x => MON.test(x))) month = s;
    else if (t.some(x => /^\d{4}$/.test(x))) year = s;
  }
  const okY = year  ? setSel(year,  String(a.year))  : false;
  const okM = month ? setSel(month, a.monthName)     : false;
  return {ok: true, year: okY, month: okM, hasMonth: !!month, hasYear: !!year};
}
"""

CAL_PICK_JS = r"""
(a) => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const pops = [...document.querySelectorAll(
    '[class*=datepicker],[class*=DatePicker],[class*=calendar],[class*=Calendar],[role=dialog]')]
    .filter(vis);
  if (!pops.length) return {ok: false, why: 'no calendar is open'};
  const pop = pops.map(p => ({p, n: p.querySelectorAll('[aria-label]').length}))
                  .sort((a, b) => b.n - a.n)[0].p;
  const cells = [...pop.querySelectorAll('[aria-label],[class*=day],td,[role=gridcell],[role=option]')]
    .filter(vis).filter(el => el.children.length <= 1);
  const al = (el) => el.getAttribute('aria-label') || '';
  // react-datepicker labels every cell: "Choose Thursday, August 20th, 2026" and disabled ones
  // "Not available Wednesday, July 1st, 2026". That is the reliable handle - the day NUMBER
  // alone appears in the leading/trailing greyed-out weeks too.
  let hit = cells.find(el => /^choose/i.test(al(el)) && al(el).includes(a.ord)
                             && al(el).toLowerCase().includes(a.monthName.toLowerCase())
                             && al(el).includes(String(a.year)));
  if (!hit) hit = cells.find(el => (el.innerText || '').trim() === String(a.day)
                                   && !/^not available/i.test(al(el))
                                   && !/disabled|outside|--outside/i.test(el.className || ''));
  if (!hit) return {ok: false, saw: cells.slice(0, 14).map(el => al(el) || (el.innerText || '').trim()),
    debug: {pops: pops.length, popClass: String(pop.className || '').slice(0, 60),
            popRect: (() => { const r = pop.getBoundingClientRect();
                              return {w: Math.round(r.width), h: Math.round(r.height)}; })(),
            rawCells: pop.querySelectorAll(
              '[aria-label],[class*=day],td,[role=gridcell],[role=option]').length,
            visCells: cells.length,
            sampleLabels: [...pop.querySelectorAll('[aria-label]')].slice(0, 6)
                          .map(e => e.getAttribute('aria-label'))}};
  try { hit.scrollIntoView({block: 'center'}); } catch (e) {}
  hit.click();
  return {ok: true, picked: al(hit) || (hit.innerText || '').trim()};
}
"""

CAL_HEAD_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const pops = [...document.querySelectorAll(
    '[class*=datepicker],[class*=DatePicker],[class*=calendar],[class*=Calendar],[role=dialog]')]
    .filter(vis);
  if (!pops.length) return {open: false};
  const pop = pops.map(p => ({p, n: p.querySelectorAll('[aria-label]').length}))
                  .sort((a, b) => b.n - a.n)[0].p;
  return {open: true, text: (pop.innerText || '').slice(0, 80)};
}
"""

CAL_NEXT_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const btn = [...document.querySelectorAll('button,[role=button],[class*=next],[aria-label]')]
    .filter(vis)
    .find(b => /next month|next|nächster/i.test(
      (b.getAttribute('aria-label') || '') + ' ' + (b.className || '')));
  if (!btn) return {ok: false};
  btn.click();
  return {ok: true};
}
"""

_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _ordinal(d: int) -> str:
    return "%d%s" % (d, "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th"))


async def _pick_date_in_calendar(page, sel: str, value: str, label: str = "") -> str:
    """Drive the CALENDAR, because the text box cannot be typed into.

    NTT's picker is react-datepicker with a min-date: days before today render greyed out and the
    input itself is read-only, so the native setter, `page.fill` and real keystrokes ALL leave it
    empty (measured: `date field now reads '' [STILL EMPTY]` three times). The only route a human
    has is the calendar - so that is the route we take: open it, set the month/year dropdowns in
    its header, then click the day cell by its aria-label.
    """
    # THE ORDER IS NOT ALWAYS DD/MM. This function assumed the German order and Workday's intive
    # tenant advertises `MM/DD/YYYY`, so `09/16/2026` was read as day=9 month=16 -> refused, and the
    # calendar never opened. Accept ISO too, and when the first number cannot be a month, it is a day.
    m = re.match(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\s*$", value or "")
    iso = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$", value or "")
    year = mon = day = 0
    if iso:
        year, mon, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    elif m:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12:
            day, mon = a, b          # 17/09/2026 -> unambiguously day-first
        else:
            mon, day = a, b          # 09/16/2026 -> MM/DD, which is what this tenant asks for
    else:
        return ""
    if not (1 <= mon <= 12 and 1 <= day <= 31):
        return ""
    name = _MONTHS[mon - 1]
    try:
        await page.click(sel, timeout=5000)
        await page.wait_for_timeout(600)
        head = await page.evaluate(CAL_OPEN_JS, {"monthName": name, "year": year}) or {}
        if not head.get("ok"):
            print(f"[llm_driver]      -> calendar: {head.get('why')}", flush=True)
            return ""
        await page.wait_for_timeout(500)
        if not head.get("month"):
            # No month dropdown (or it refused): walk the arrows, exactly as a human would.
            for _ in range(14):
                h = await page.evaluate(CAL_HEAD_JS) or {}
                if not h.get("open"):
                    break
                if name.lower() in (h.get("text") or "").lower() and str(year) in (h.get("text") or ""):
                    break
                if not (await page.evaluate(CAL_NEXT_JS) or {}).get("ok"):
                    break
                await page.wait_for_timeout(300)
        res = await page.evaluate(CAL_PICK_JS,
                                  {"day": day, "ord": _ordinal(day),
                                   "monthName": name, "year": year}) or {}
        if not res.get("ok"):
            print(f"[llm_driver]      -> calendar: no cell for {day} {name} {year}; "
                  f"saw {(res.get('saw') or [])[:6]} | {res.get('debug') or res.get('why')}",
                  flush=True)
            return ""
        print(f"[llm_driver]      -> calendar: clicked {res.get('picked')!r}", flush=True)
        await page.wait_for_timeout(600)
        return await page.evaluate(READ_JS, sel) or ""
    except Exception as e:
        print(f"[llm_driver]      -> calendar failed: {str(e)[:90]}", flush=True)
        return ""


SUBMIT_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const btn = [...document.querySelectorAll('button,[role=button],input[type=submit],a')]
    .filter(vis)
    .filter(b => !b.disabled)
    .find(b => /^\s*(submit|absenden|bewerbung absenden|submit application)\s*$/i
                 .test((b.innerText || b.value || '').trim()));
  if (!btn) return {ok: false, why: 'no Submit button on this page'};
  const label = (btn.innerText || btn.value || '').trim();
  try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
  btn.click();
  return {ok: true, label: label};
}
"""

CONFIRM_JS = """() => (document.body ? (document.body.innerText || '') : '').slice(0, 4000)"""

# The candidate's explicit instruction: "it needs to be fully automated ... no need to bother the
# stakeholder again". Set JHW_AUTOSUBMIT=0 to go back to stopping at Review.
AUTOSUBMIT = os.getenv("JHW_AUTOSUBMIT", "1").lower() not in ("0", "false", "no")


async def _submit(page, r: dict) -> bool:
    """Click Submit and PROVE it went through — and REFUSE while a required field is empty.

    MEASURED on Ashby (n8n, 2026-08-17). The driver printed the answer itself and submitted anyway:
        still required+empty: ['First and last name', 'Email', 'Notice period / availability details',
                               'What about n8n and the role caught your attention...', ...]
        step 1/45 fill i=5 'First and last name' = 'Jev Vainsteins'
        clicking 'Submit Application' ...
    One field filled out of fourteen, then Submit — and the site answered with a wall of
    "Missing entry for required field". A HALF-EMPTY APPLICATION SENT TO A REAL EMPLOYER IS WORSE THAN
    NO APPLICATION: it cannot be retracted and it is the candidate's name on it.
    The Workday path has had this guard since `presubmit.py` was written; this driver never got it.
    The docstring already claimed "never called while anything is unresolved" — a comment is not a
    guard."""
    try:
        idx = await page.evaluate(ENUM_JS) or {}
        blank = [e.get("label") for e in (idx.get("elements") or idx.get("controls") or [])
                 if isinstance(e, dict) and e.get("required")
                 and not str(e.get("val") or e.get("value") or "").strip()
                 and not e.get("checked") and not e.get("trap")]
        blank = [b for b in blank if b][:8]
        if blank:
            print(f"[llm_driver] REFUSING to submit: {len(blank)} required field(s) are still "
                  f"empty -> {blank}", flush=True)
            r["note"] = (r.get("note") or "") + \
                f" refused to submit: required+empty {blank}."
            r["unresolved"] = list(dict.fromkeys((r.get("unresolved") or []) + blank))
            return False
    except Exception as e:
        # FAIL OPEN, LOUDLY. A broken read must not block a complete application, but it must say so.
        print(f"[llm_driver] pre-submit check unavailable ({type(e).__name__}: {e}) — submitting",
              flush=True)
    if not AUTOSUBMIT:
        print("[llm_driver] auto-submit disabled (JHW_AUTOSUBMIT=0) - stopping at Review",
              flush=True)
        return False
    res = await page.evaluate(SUBMIT_JS) or {}
    if not res.get("ok"):
        print(f"[llm_driver] submit: {res.get('why')}", flush=True)
        return False
    print(f"[llm_driver] clicking {res.get('label')!r} ...", flush=True)
    r["actions"].append("submit")
    try:
        await page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)
    # EVIDENCE, not optimism: a click that throws no error is not a submitted application.
    body, url = "", ""
    try:
        body = await page.evaluate(CONFIRM_JS) or ""
        url = page.url or ""
    except Exception:
        pass
    ok = bool(re.search(r"thank you|thanks for (your )?appl|application (has been )?"
                        r"(submitted|received|sent)|successfully submitted|vielen dank|"
                        r"bewerbung .{0,20}(eingegangen|übermittelt)", body, re.I)) \
         or bool(re.search(r"confirm|thank|success|complete", url, re.I))
    snippet = " ".join(body.split())[:160]
    if ok:
        r["submitted"] = True
        print(f"[llm_driver] SUBMITTED - the site says: {snippet!r}", flush=True)
    else:
        r["submitted"] = False
        print("[llm_driver] submit clicked but NO confirmation found. url=" + url[:90]
              + f" | page says: {snippet!r}", flush=True)
    return ok


async def run(facts: str, resume_path: str = "",
              max_steps: int = int(os.getenv("JHW_STEPS_PER_TRY", "45")),
              url: str = "") -> dict:
    r = {"ok": False, "steps": 0, "note": "", "actions": [], "unresolved": [],
         "_tried": {}, "_array_fixed": {}, "_seen": {}, "_picked_at": {}, "_swept": False,
         "_page": None}
    pw = await async_playwright().start(); browser = None
    try:
        browser, ctx, page = await _connect(pw)
        await page.bring_to_front()
        # NAVIGATE FIRST. The driver used to inspect whatever tab happened to be open — on a fresh
        # sandbox that is about:blank, so the model correctly reported "no interactive elements"
        # and then said done. The job URL was never passed in at all.
        async def _upload_files():
            """Attach the tailored documents. File inputs are hidden from the LLM on purpose:
            a native file-picker cannot be driven in a container, so Playwright sets them directly.
            Phenom renders TWO inputs on step 1 - resume first, cover letter second (see
            userdata/skills/ats-phenom/SKILL.md), so DOM order is the mapping."""
            import os as _os
            outd = _os.getenv("JHW_OUT", "/agent/out")
            wanted = [p_ for p_ in (_os.path.join(outd, "resume.pdf"),
                                    _os.path.join(outd, "cover_letter.pdf")) if _os.path.isfile(p_)]
            if not wanted:
                return 0
            done = 0
            try:
                inputs = page.locator('input[type="file"]')
                n = await inputs.count()
                for k in range(min(n, len(wanted))):
                    try:
                        await inputs.nth(k).set_input_files(wanted[k])
                        print(f"[llm_driver] attached {_os.path.basename(wanted[k])} "
                              f"to file input #{k}", flush=True)
                        done += 1
                        await page.wait_for_timeout(1500)
                    except Exception as e:
                        print(f"[llm_driver] upload #{k} failed: {repr(e)[:90]}", flush=True)
            except Exception as e:
                print(f"[llm_driver] no file inputs found: {repr(e)[:90]}", flush=True)
            return done

        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
                # Cookie banners and chat widgets sit ON TOP of the form and swallow clicks.
                # Best-effort, never fatal.
                for sel in ("#onetrust-accept-btn-handler",
                            "button#truste-consent-button",
                            "button:has-text('Accept All')",
                            "button:has-text('Accept all')",
                            "button:has-text('Alle akzeptieren')",
                            "[aria-label='Close'][class*='chat']",
                            "button[aria-label*='lose chat']"):
                    try:
                        el = page.locator(sel).first
                        if await el.count() and await el.is_visible():
                            await el.click(timeout=2500)
                            print(f"[llm_driver] dismissed overlay: {sel}", flush=True)
                            await page.wait_for_timeout(600)
                    except Exception:
                        pass
                print(f"[llm_driver] navigated to {url[:90]}", flush=True)
                up = await _upload_files()
                if up:
                    r["actions"].append(f"upload:{up}")
            except Exception as e:
                r["note"] = f"goto failed: {e}"
                return r
        for step in range(max_steps):
            r["steps"] = step + 1
            await page.wait_for_timeout(1200)
            # A click that navigates destroys the JS execution context, so the NEXT evaluate
            # throws "Execution context was destroyed". That is expected Playwright behaviour on
            # a multi-step form - wait for the new document and retry instead of aborting the run.
            snap = None
            for attempt in range(3):
                try:
                    snap = await page.evaluate(ENUM_JS)
                    break
                except Exception as e:
                    msg = str(e)
                    if "Execution context was destroyed" in msg or "navigating" in msg.lower():
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=20000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1200)
                        print(f"[llm_driver] page navigated — re-reading the DOM "
                              f"(attempt {attempt + 2})", flush=True)
                        continue
                    r["note"] = f"enumerate failed: {e}"
                    snap = None
                    break
            if snap is None:
                if not r["note"]:
                    r["note"] = "could not read the page after navigation"
                print(f"[llm_driver] EXIT step {step + 1}: {r[chr(39)+chr(110)+chr(111)+chr(116)+chr(101)+chr(39)] or chr(39)+chr(39)}", flush=True)
                break
            els  = snap.get("els", [])
            # A click on Next destroys and rebuilds the page. Reading it 1.2s later can return an
            # almost EMPTY dom - and an empty page looks exactly like a finished one: no errors,
            # no missing fields, nothing to do. That is how the run reported ok=True with page 2
            # completely blank. Wait for the page to come back before asking anything.
            if len(els) < 8:
                if r.get("_thin", 0) < 4:
                    r["_thin"] = r.get("_thin", 0) + 1
                    print(f"[llm_driver] page has only {len(els)} interactive element(s) - it is "
                          f"still rendering; waiting ({r['_thin']}/4)", flush=True)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)
                    continue
                # THE WAITS ARE EXHAUSTED AND THE DOM STILL SHOWS NOTHING. Iterating on the same
                # reader again is what the operator correctly called madness -- so CHANGE THE SENSE:
                # screenshot the page and ask a vision model what a human would see. It shares none
                # of ENUM_JS's assumptions, so a DOM-layer defect (one aria-hidden line blinded this
                # very driver for a day) cannot blind it too. Labels only, resolved through
                # Playwright's accessible-name lookup; a control it imagines cannot resolve.
                try:
                    import vision as VIS
                    vz = await VIS.read(page, log=lambda m: print(f"[llm_driver]{m}", flush=True))
                    if (vz.get("controls") or []):
                        print(f"[llm_driver] VISION sees {len(vz['controls'])} control(s) the DOM "
                              f"reader could not — writing evidence and using them", flush=True)
                        await VIS.dump(page, "llmdriver-thin",
                                       {"dom_els": len(els),
                                        "vision": [c.get("label") for c in vz["controls"]]},
                                       log=lambda m: print(f"[llm_driver]{m}", flush=True))
                        # `css` is load-bearing: the executor builds [data-jhw-idx='i'] and the
                        # stale-index fallback reads `_css`. Without it every post-vision action
                        # targeted nothing (audited 2026-08-16).
                        els = [{"i": c["i"], "tag": c["kind"], "label": c["label"],
                                "css": c.get("css") or f"[data-jhw-idx='{c['i']}']",
                                "val": c.get("value") or "", "seen_by": "vision"}
                               for c in vz["controls"]]
                        snap = dict(snap or {}, els=els)
                    else:
                        await VIS.dump(page, "llmdriver-blind", {"dom_els": len(els)},
                                       log=lambda m: print(f"[llm_driver]{m}", flush=True))
                except Exception as e:
                    print(f"[llm_driver] vision fallback unavailable ({type(e).__name__})",
                          flush=True)
            else:
                r["_thin"] = 0
            miss = list(snap.get("missing") or [])
            errs = snap.get("errors") or []          # now [{msg,label,css,i,arr,val}, ...]
            emsg = [e.get("msg") if isinstance(e, dict) else str(e) for e in errs]
            if snap.get("review"):
                # Same evidence rule as "done": landing on something that LOOKS like a review
                # page without having filled anything means we never started. The NTT job page
                # itself matched this on step 0 (filled=[], "reached Review") - a false success.
                # EVIDENCE = FILLED FIELDS, not clicks. A click alone proved nothing: the
                # detector fired on the NTT job page and one "click:3" was accepted as a
                # completed application. Only a real fill counts, at any step.
                if emsg:
                    print(f"[llm_driver] 'review' claimed while the page still errors {emsg[:3]} "
                          "- continuing", flush=True)
                    facts += f"\n(The page still shows: {emsg[:3]}. Fix it.)"
                    continue
                filled = [x for x in r["actions"] if x.startswith("fill")]
                if not filled:
                    print("[llm_driver] 'review' matched but NOTHING has been filled yet "
                          "- ignoring it and continuing to drive the form", flush=True)
                else:
                    done_ok = await _submit(page, r)
                    r["ok"] = True
                    r["note"] = ("SUBMITTED after %d field(s) filled" % len(filled)) if done_ok \
                        else ("reached Review after %d field(s) filled (not submitted)"
                              % len(filled))
                    break
            # PAGE IDENTITY. Without it the key for `click Next` was identical on every step of
            # the form, so the two legitimate Next clicks on page 1 used up the budget and page
            # 2's Next was BLOCKED before it had been clicked even once. A new page is a fresh
            # start: new counters, and its own sweep.
            pkey = ((snap.get("url") or "")[:160] + "|" + str(len(els)) + "|"
                    + str((els[0].get("label") or els[0].get("text") or "") if els else ""))
            if pkey != r.get("_page"):
                if r.get("_page") is not None:
                    print(f"[llm_driver] --- new page ({len(els)} elements) - repeat counters "
                          "reset", flush=True)
                r["_page"] = pkey
                r["_seen"] = {}
                r["_swept"] = False
            recent = r["actions"][-8:]
            if emsg:
                print(f"[llm_driver] page errors: "
                      f"{[(e.get('label') or '?') + ' -> ' + e.get('msg') for e in errs if isinstance(e, dict)][:4]}",
                      flush=True)

            # ---------------------------------------------------------------- deterministic repair
            # "should be array" = a TYPEAHEAD holding typed TEXT instead of a SELECTED suggestion.
            # We now know WHICH field it is (ENUM_JS ties each message to its owner), so repair it
            # ourselves instead of asking the model to guess. Guessing cost 27 wasted steps on NTT:
            # it "fixed" Country/Region Code (a native select that was already correct) 8 times
            # while the real offender was State/Province/Region.
            # Stale entries made 'done' impossible: 'Hesse' (a suggestion ROW the model clicked)
            # and a field that had long since been fixed both sat in unresolved forever.
            r["unresolved"] = [u for u in r["unresolved"]
                               if any((e.get("label") if isinstance(e, dict) else "") == u for e in errs)
                               or any(m.get("label") == u for m in miss)]
            repaired = False
            for e in errs:
                if not isinstance(e, dict):
                    continue
                if e.get("summary") or not e.get("label"):
                    continue
                if "should be" not in (e.get("msg") or "").lower():
                    continue
                # The site revalidates a tick AFTER the suggestion is clicked, so the error is
                # still on the page for one read. Repairing on that read CLEARED the field we
                # had just filled correctly - twice, in the last run. Give it one round.
                if step - r["_picked_at"].get(e.get("label") or "", -9) <= 1:
                    print(f"[llm_driver] (error on {e.get('label')!r} is stale - just picked it; "
                          "letting the page revalidate)", flush=True)
                    await page.wait_for_timeout(900)
                    continue
                css = e.get("css") or (f"[data-jhw-idx='{e['i']}']" if e.get("i") is not None else "")
                lbl = e.get("label") or ""
                if not css or e.get("tag") == "select":
                    continue
                if r["_array_fixed"].get(lbl, 0) >= 3:
                    continue
                want = (r["_tried"].get(lbl) or e.get("val") or "").strip()
                if not want:
                    continue
                r["_array_fixed"][lbl] = r["_array_fixed"].get(lbl, 0) + 1
                print(f"[llm_driver] auto-repair: {lbl!r} is array-typed and holds typed text "
                      f"{want!r} -> re-doing it as a typeahead", flush=True)
                res = await _typeahead(page, css, want, lbl)
                r["actions"].append(f"typeahead:{lbl[:20]}")
                if res.get("picked"):
                    repaired = True
                    r["_picked_at"][lbl] = step
                    if lbl in r["unresolved"]:
                        r["unresolved"].remove(lbl)
                    if str(res["picked"]).strip().lower() != want.strip().lower():
                        r.setdefault("warnings", []).append(
                            f"{lbl}: wanted {want!r}, site offered {res['picked']!r}")
                elif lbl not in r["unresolved"]:
                    r["unresolved"].append(lbl)
            if repaired:
                continue          # re-read the page; the error list should have shrunk

            # A typeahead that still errors is NOT "filled", however much text sits in it.
            for e in errs:
                if isinstance(e, dict) and e.get("label") and not e.get("summary") and \
                        not any(m.get("label") == e["label"] for m in miss):
                    miss.append({"i": e.get("i"), "label": e["label"], "type": "typeahead"})
            if miss:
                print(f"[llm_driver] still required+empty: "
                      f"{[m.get('label') for m in miss][:6]}", flush=True)
            msg = [{"role": "system", "content": SYS},
                   {"role": "user", "content":
                    f"FACTS:\n{facts}\n\n"
                    f"VALIDATION ERRORS, EACH WITH THE FIELD IT BELONGS TO (fix these FIRST; "
                    f"do NOT touch any other field because of them):\n{json.dumps(errs)[:1200]}\n\n"
                    f"REQUIRED FIELDS STILL EMPTY (fill EVERY one before any Next/Submit):\n"
                    f"{json.dumps(miss)[:1500]}\n\n"
                    f"ACTIONS YOU ALREADY TOOK (do NOT repeat one that did not change anything):\n"
                    f"{json.dumps(recent)}\n\n"
                    f"ELEMENTS:\n{json.dumps(els)[:7000]}\n\nNext action?"}]
            try:
                act = _parse(await _llm(msg))
            except Exception as e:
                r["note"] = f"llm error: {e}"
                print(f"[llm_driver] EXIT step {step + 1}: LLM call failed: {str(e)[:140]}", flush=True)
                break
            a = (act.get("action") or "").lower()
            r["actions"].append(a + (f":{act.get('i')}" if "i" in act else ""))
            # OBSERVABILITY: 25 steps used to run in total silence. One line per step, with the
            # element's own label, so the run can be followed live and diffed afterwards.
            _lbl = ""
            try:
                _e = next((e for e in els if e.get("i") == act.get("i")), None)
                if _e:
                    _lbl = (_e.get("label") or _e.get("text") or _e.get("type") or "")[:44]
            except Exception:
                pass
            _val = str(act.get("value", ""))[:40]
            print(f"[llm_driver] step {step + 1:>2}/{max_steps} {a:<6} "
                  f"i={act.get('i')} {_lbl!r}"
                  + (f" = {_val!r}" if _val else "")
                  + (f"  why={str(act.get('why'))[:60]!r}" if act.get("why") else ""),
                  flush=True)
            obs_evt = {"evt": "driver_step", "step": step + 1, "action": a, "i": act.get("i"),
                       "label": _lbl, "url": snap.get("url", "")[:120]}
            try:
                print(json.dumps(obs_evt), flush=True)     # structured -> promtail -> Loki
            except Exception:
                pass
            # REPEAT-BREAKER. On NTT the model executed the IDENTICAL action
            # (select Country/Region Code = Germany) six times, and clicked Next seven times,
            # while the error set never changed once. An action that does not change the page is
            # not a strategy, it is a loop - so we refuse to run it a third time and say why.
            if a in ("fill", "select", "pick", "click"):
                key = f"{a}|{_lbl}|{_val}|{'|'.join(sorted(set(emsg)))}"
                r["_seen"][key] = r["_seen"].get(key, 0) + 1
                if r["_seen"][key] >= 3:
                    print(f"[llm_driver]      -> BLOCKED: {a} {_lbl!r} already tried "
                          f"{r['_seen'][key] - 1}x with no change on the page", flush=True)
                    facts += (f"\n(BANNED: {a} on {_lbl!r} with value {_val!r} - you have done it "
                              f"{r['_seen'][key] - 1} times and the page did not change. It is NOT "
                              "the cause of the error. Do something DIFFERENT or use ask.)")
                    r["actions"].append("blocked_repeat")
                    # An unanswered dropdown further down the form is usually what is really
                    # holding Next. Answer every leftover before bothering the human.
                    if not r["_swept"]:
                        r["_swept"] = True
                        got = await page.evaluate(SWEEP_JS, SELECT_DEFAULTS) or []
                        for g in got:
                            print(f"[llm_driver]      -> swept: {g['label'][:44]!r} = "
                                  f"{g['picked']!r}"
                                  + ("  (from your standing answers)" if g.get("guided") else
                                     "  (first option - it does not matter)")
                                  + ("" if g.get("verified") else "  [DID NOT STICK]"), flush=True)
                        if got:
                            facts += (f"\n(I answered the leftover dropdowns for you: "
                                      f"{[(g['label'][:28], g['picked']) for g in got]}.)")
                            await page.wait_for_timeout(800)
                            continue
                    # NEVER stop on a stall. The human is one Telegram message away.
                    if r["_seen"][key] >= 6:
                        if ask:
                            q = (f"I'm stuck on the {r.get('_site', 'application')} form.\n"
                                 f"Action '{a}' on '{_lbl}'"
                                 + (f" = '{_val}'" if _val else "")
                                 + f" has been refused {r['_seen'][key] - 1} times.\n"
                                 f"The page still says: {sorted(set(emsg))[:3]}\n\n"
                                 "What should I do? (tell me the value to use, or 'skip', "
                                 "or 'stop' to end the run)")
                            print("[llm_driver] stuck -> asking you on Telegram (the run keeps "
                                  "waiting, it does NOT stop)", flush=True)
                            reply = (await ask.ask_human(q)) or ""
                            if reply.strip().lower().startswith("stop"):
                                r["note"] = "you asked me to stop"
                                print(f"[llm_driver] EXIT: {r['note']}", flush=True)
                                break
                            facts += f"\n(GUIDANCE FROM THE CANDIDATE: {reply})"
                            r["_seen"][key] = 0        # your answer earns a clean slate
                            r["_swept"] = False
                            continue
                        r["note"] = f"stuck on {_lbl!r} and no Telegram channel to ask on"
                        print(f"[llm_driver] EXIT: {r['note']}", flush=True)
                        break
                    continue
            if _val and _lbl and a in ("fill", "pick", "select"):
                r["_tried"][_lbl] = _val

            if a == "done":
                # EVIDENCE REQUIRED. The model once answered "done" on step 0 and the run was
                # reported as a success in 3.5s having touched nothing. "done" is only credible
                # after we actually changed the page.
                if r["unresolved"]:
                    print(f"[llm_driver] 'done' rejected - never selected a value for "
                          f"{r['unresolved']}", flush=True)
                    facts += (f"\n(These fields still have NO selected value: {r['unresolved']}. "
                              "Use PICK with a SHORTER value, e.g. the first 3-4 letters.)")
                    continue
                blank = [lbl for lbl, v in r["_tried"].items()
                         if v and any(e.get("label") == lbl
                                      and not str(e.get("val") or "").strip() for e in els)]
                if blank:
                    # The date box read EMPTY on screen while the run was reported ok=True.
                    # A field we filled that is now blank is proof the value never committed.
                    print(f"[llm_driver] 'done' rejected - these fields are EMPTY although I "
                          f"filled them: {blank}", flush=True)
                    facts += (f"\n(These fields are EMPTY on the page even though a value was "
                              f"entered: {blank}. Re-enter them and check they hold the value.)")
                    for lbl in blank:
                        r["_seen"] = {k: v for k, v in r["_seen"].items() if lbl not in k}
                    continue
                if emsg:
                    print(f"[llm_driver] 'done' rejected - page still shows {emsg[:3]}", flush=True)
                    facts += (f"\n(You said done but the page still shows {emsg[:3]}. "
                              "That field is NOT accepted yet - fix it.)")
                    continue
                # There is still a Next button -> there are still pages. "done" here means the
                # model looked at a half-rendered page and gave up; the application is not
                # finished until the site shows Review/Submit.
                nxt = [e for e in els if re.match(
                    r"\s*(next|continue|save and continue|weiter|speichern)",
                    str(e.get("text") or e.get("label") or ""), re.I)]
                if nxt and not snap.get("review"):
                    print(f"[llm_driver] 'done' rejected - the page still offers "
                          f"{str(nxt[0].get('text') or nxt[0].get('label'))[:20]!r}, so this is "
                          "not the last step", flush=True)
                    facts += ("\n(You said done but there is still a Next button on the page. The "
                              "application is only finished at the Review/Submit step. Fill this "
                              "page's fields and click Next.)")
                    continue
                did = [x for x in r["actions"] if x.startswith("fill")]
                if not did:
                    r["note"] = "model said done before filling any field - rejected"
                    facts += ("\n(You claimed done without filling any field. That is wrong: "
                              "fill the required empty fields first, then continue.)")
                    if r.get("_done_rejected"):
                        r["ok"] = False
                        r["note"] = "model insisted it was done with nothing filled"
                        break
                    r["_done_rejected"] = True
                    continue
                done_ok = await _submit(page, r)
                r["ok"] = True
                r["note"] = ("SUBMITTED after %d action(s)" % len(did)) if done_ok \
                    else ("model reported done after %d action(s) (not submitted)" % len(did))
                break
            if a == "ask":
                q = act.get("question", "The application needs a value.")
                reply = (await ask.ask_human(q)) if ask else ""
                facts += f"\n(Q: {q} -> A: {reply or 'no answer'})"
                continue
            i = act.get("i")
            sel = f"[data-jhw-idx='{i}']"
            # React re-renders between enumerate and act and drops data-jhw-idx, so the locator
            # waited the full 30s default and failed - 14 times in a row on the questions page.
            # Fall back to a stable selector (#id / [name] / [aria-label]) and fail fast.
            _el = next((e for e in els if e.get("i") == i), None)
            _css = (_el or {}).get("css") or ""
            try:
                if not await page.locator(sel).count() and _css:
                    sel = _css
                    print(f"[llm_driver]      (index stale -> using {sel})", flush=True)
            except Exception:
                if _css:
                    sel = _css
            page.set_default_timeout(8000)   # (also set at connect; harmless to repeat)
            try:
                _txt = (_lbl or "").lower()
                # The stepper at the top of the form looks like a set of buttons. Clicking one
                # NAVIGATES BACKWARDS - the model clicked "My Information" and threw the whole
                # run back to page 1, losing the page it was working on. Progress is made with
                # Next, never with the progress bar.
                if a == "click" and re.fullmatch(
                        r"\s*(my information|my experience|voluntary disclosures|review|"
                        r"sign up|sign in|back|zur[uü]ck)\s*", _txt or "", re.I):
                    print(f"[llm_driver]      -> BLOCKED: {_lbl!r} is the progress bar / back "
                          "navigation; it would undo this page", flush=True)
                    facts += (f"\n(BANNED: never click {_lbl!r} - it is the step navigation and it "
                              "sends the application BACKWARDS. Only 'Next'/'Save and Continue' "
                              "moves forward.)")
                    r["actions"].append("blocked_nav")
                    continue
                if a == "click" and miss and any(k in _txt for k in
                                                 ("next", "continue", "submit", "weiter")):
                    # The model claimed "all required fields are filled" 12 times while 2 were
                    # empty. Its claim is not evidence; the DOM is.
                    names = [m.get("label") for m in miss][:5]
                    print(f"[llm_driver]      -> BLOCKED: {len(miss)} required field(s) still "
                          f"empty {names}", flush=True)
                    facts += (f"\n(You tried to advance but these REQUIRED fields are still "
                              f"empty: {names}. Fill them first.)")
                    r["actions"].append("blocked_next")
                    continue
                if a == "pick" or (a == "fill" and (_el or {}).get("arr")):
                    # ONE routine for every suggestion-backed field: prefix ladder + click a row.
                    # A `fill` on an array-typed box is silently upgraded - the model asking for
                    # the wrong verb must not be able to poison the field again.
                    if a == "fill":
                        print("[llm_driver]      -> this field is a typeahead (array-typed); "
                              "filling it as text cannot satisfy it", flush=True)
                    want = str(act.get("value", ""))
                    res = await _typeahead(page, sel, want, _lbl)
                    if res.get("picked"):
                        r["_picked_at"][_lbl] = step
                        if str(res["picked"]).strip().lower() != want.strip().lower():
                            r.setdefault("warnings", []).append(
                                f"{_lbl}: wanted {want!r}, site offered {res['picked']!r}")
                        if _lbl in r["unresolved"]:
                            r["unresolved"].remove(_lbl)
                    elif _lbl and _lbl not in r["unresolved"]:
                        r["unresolved"].append(_lbl)
                elif a == "fill":
                    try:
                        await page.fill(sel, str(act.get("value", "")))
                    except Exception:
                        # stale index + no id/name -> set it in the DOM by label with the
                        # NATIVE setter so React registers the change.
                        res = await page.evaluate(
                            FILL_JS, {"label": _lbl, "value": str(act.get("value", "")),
                                      "placeholder": (_el or {}).get("placeholder", "")}) or {}
                        if not res.get("ok"):
                            print(f"[llm_driver]      -> DOM fill by label failed: "
                                  f"{res.get('why')} (inputs seen: {res.get('saw')})", flush=True)
                            raise
                        ph = res.get("placeholder") or ""
                        print("[llm_driver]      -> filled via DOM by label"
                              + (f" (format: {ph})" if ph else ""), flush=True)
                    # DATE FIELDS. Pressing Escape to close the calendar REVERTED the picker and
                    # left the box empty while the log said "ok" - the fill was never committed.
                    # So: read the value BACK, and if it did not stick, type it as real
                    # keystrokes (which is what the picker parses) and blur to commit. Then say
                    # out loud what the field actually holds.
                    if re.search(r"date|dd[-/]mm|mm[-/]dd|yyyy", (_lbl or "") +
                                 str((_el or {}).get("placeholder", "")), re.I):
                        val = str(act.get("value", ""))
                        got = await _commit_date(page, sel, val, _lbl)
                        if not got.strip():
                            print("[llm_driver]      -> the box refused text (read-only picker) "
                                  "- driving the calendar instead", flush=True)
                            got = await _pick_date_in_calendar(page, sel, val, _lbl)
                        print(f"[llm_driver]      -> date field now reads {got!r}"
                              + ("" if got else "  [STILL EMPTY]"), flush=True)
                        # Survives navigation, unlike a snapshot check: an empty date keeps
                        # 'done' impossible even if the model wanders to another step.
                        if got.strip():
                            if _lbl in r["unresolved"]:
                                r["unresolved"].remove(_lbl)
                        elif _lbl not in r["unresolved"]:
                            r["unresolved"].append(_lbl)
                elif a == "select":
                    # These selects have no id/name, so [data-jhw-idx] is the only handle and it
                    # dies on re-render -> 8s timeout, forever. Set the value in the DOM by LABEL
                    # and dispatch input+change so React notices. Falls back to the FIRST real
                    # option rather than leaving a required question empty.
                    want = str(act.get("value", ""))
                    res = await page.evaluate(SELECT_JS, {"label": _lbl, "want": want}) or {}
                    if res.get("ok"):
                        note = " (first option)" if res.get("fallback") else ""
                        if not res.get("verified"):
                            note += "  [WARNING: the element did not keep the value]"
                        print(f"[llm_driver]      -> selected {res['picked']!r}{note} "
                              f"of {len(res.get('options') or [])}", flush=True)
                        await page.wait_for_timeout(500)
                        continue
                    print(f"[llm_driver]      -> DOM select failed ({res.get('why')}), "
                          "trying Playwright", flush=True)
                    # A native <select> cannot be filled or clicked open reliably. Try the visible
                    # label first, then the value - Playwright raises if neither matches, which
                    # becomes feedback for the next turn instead of a silent no-op.
                    want = str(act.get("value", ""))
                    try:
                        await page.select_option(sel, label=want)
                    except Exception:
                        await page.select_option(sel, value=want)
                elif a == "click":
                    if str((_el or {}).get("type", "")).lower() in ("checkbox", "radio"):
                        res = await page.evaluate(CHECK_JS, {"sel": sel}) or {}
                        if res.get("ok"):
                            if res.get("already"):
                                print("[llm_driver]      -> already ticked; NOT clicking again "
                                      "(a second click would untick it)", flush=True)
                                facts += (f"\n({_lbl!r} is ALREADY ticked. Do not click it again - "
                                          "a second click unticks it. The error about it is stale.)")
                            else:
                                print(f"[llm_driver]      -> ticked (checked="
                                      f"{res.get('checked')})", flush=True)
                            await page.wait_for_timeout(600)
                            continue
                    await page.click(sel)
                else:
                    continue
                print(f"[llm_driver]      -> ok", flush=True)
            except Exception as e:
                print(f"[llm_driver]      -> FAILED: {str(e)[:110]}", flush=True)
                facts += f"\n(action {a} on {i} failed: {e})"
            await page.wait_for_timeout(600)
        r["note"] = r["note"] or f"stopped after {r['steps']} steps"
    except Exception as e:
        r["note"] = f"llm_driver error: {e}"
        print(f"[llm_driver] EXIT: unhandled error: {str(e)[:160]}", flush=True)
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        await pw.stop()
        # ALWAYS state why the run ended - a silent stop is how "why only 11 steps?" happens.
        print(f"[llm_driver] DONE after {r['steps']} step(s) | ok={r['ok']} | "
              f"actions={len(r['actions'])} | note={r['note'] or 'n/a'}", flush=True)
    return r


if __name__ == "__main__":
    print(asyncio.run(run("Name: Test Candidate; email test@example.com", max_steps=5)))
