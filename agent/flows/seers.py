#!/usr/bin/env python3
"""FIVE MORE WAYS TO SEE THE PAGE — because one reader cannot be trusted.

MEASURED, from the 21:01 run at the Accenture gate:
    [vision-box] resolved 0 control(s); dropped nohit=1
    [vision] 'Evgeny Vainshtein' did NOT resolve to a real element — discarded
    [vision] JSON malformed (Expecting ',' delimiter) ... x2, and once 'no JSON: None'
    action click raised TimeoutError: waiting for locator("[data-jhw-esc=...")
Four distinct failure modes, and my two existing readers share the fragile parts: a
hand-written CSS visibility predicate, an accessible-name lookup, and an LLM that returns
malformed JSON about one run in three. Adding a sixth variation of the same idea is what the
operator correctly called madness.

SO: readers that share NOTHING with those, and a UNION instead of first-wins.

  1. AX TREE    `page.accessibility.snapshot()` — the browser's OWN computed accessibility
                tree. Role + name for everything a screen reader would announce. No CSS
                predicate, no LLM. This is what would have found 'Evgeny Vainshtein', which
                Google renders as a div[role=link] that get_by_role("link") missed.
  2. HIT GRID   `document.elementFromPoint` over a grid of the viewport. Pure geometry: it
                finds whatever is actually PAINTED at a pixel, by touching it. Immune to
                aria-hidden, to clip-hiding, to my predicates, to everything.
  3. TAB WALK   press Tab, record `document.activeElement`, repeat. The BROWSER's own focus
                order, which is also how a keyboard user works. A modal TRAPS focus, so this
                reader physically cannot see the page behind an open dialog -- the exact
                problem that produced 'Search Careers' instead of the sign-in buttons.
  4. DEEP DOM   recursive walk through `shadowRoot` and `iframe.contentDocument`.
                `querySelectorAll` does NOT cross a shadow boundary, so a web component's
                controls are invisible to every reader I had.
  5. SELF-HEAL  clicking. `data-jhw-esc` is written at index time and React re-renders it
                away, which is the TimeoutError above. On a miss, re-index and re-match by
                label instead of failing.

EVERY reader returns the same shape as workday.py's index, so the executor is unchanged.
NONE of them raises: a reader that dies must cost nothing.
"""
from __future__ import annotations
import os
import re

MAX_CONTROLS = int(os.getenv("JHW_SEERS_MAX", "60"))

# ---------------------------------------------------------------- 1. THE ACCESSIBILITY TREE
_AX_ROLES = {
    "button": "button", "link": "link", "textbox": "input", "searchbox": "input",
    "combobox": "select", "listbox": "select", "checkbox": "checkbox", "radio": "radio",
    "menuitem": "button", "tab": "button", "switch": "checkbox", "spinbutton": "input",
}


def _from_cdp(nodes: list) -> dict:
    """CDP returns a FLAT list with childIds; `walk()` wants a nested {role,name,children} tree.
    Rebuild it once here so the reader itself is unchanged."""
    by_id, root_id = {}, None
    for n in nodes:
        nid = str(n.get("nodeId"))
        role = ((n.get("role") or {}).get("value") or "").lower()
        name = ((n.get("name") or {}).get("value") or "")
        props = {str(p.get("name")): (p.get("value") or {}).get("value")
                 for p in (n.get("properties") or [])}
        by_id[nid] = {"role": role, "name": name,
                      "value": ((n.get("value") or {}).get("value") or ""),
                      "required": bool(props.get("required")),
                      "checked": props.get("checked"),
                      "ignored": bool(n.get("ignored")),
                      "_kids": [str(c) for c in (n.get("childIds") or [])],
                      "children": []}
        if role in ("rootwebarea", "webarea") and root_id is None:
            root_id = nid
    for nid, node in by_id.items():
        for cid in node["_kids"]:
            ch = by_id.get(cid)
            if ch is not None and not ch["ignored"]:
                node["children"].append(ch)
    if root_id and root_id in by_id:
        return by_id[root_id]
    # no explicit root: synthesise one so nothing is lost
    return {"role": "webarea", "name": "", "children":
            [n for n in by_id.values() if not n["ignored"]][:200]}


async def ax_tree(page, log=print, junk=None) -> dict:
    """The browser's own computed a11y tree. No CSS predicate, no LLM, no selector.

    This is the reader with the best claim to 'what a human is offered', because it is the
    same tree assistive technology consumes. It also names things my role queries missed:
    Google renders each account as a div[role=link], which `get_by_role("link", name=...)`
    did not find but which appears here as role=link name='Evgeny Vainshtein'."""
    # MEASURED on the operator's machine: `AttributeError: 'Page' object has no attribute
    # 'accessibility'` -- the Page.accessibility API was REMOVED from Playwright Python. The tree is
    # still available over CDP (`Accessibility.getFullAXTree`), which is what Playwright used
    # internally anyway. Try the modern path first, keep the old one for older versions, and say
    # which one answered. A reader that cannot run is not a reader.
    snap, how = None, ""
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send("Accessibility.enable")
            full = await cdp.send("Accessibility.getFullAXTree")
            snap = _from_cdp(full.get("nodes") or [])
            how = "cdp"
        finally:
            try:
                await cdp.detach()
            except Exception:
                pass
    except Exception as e_cdp:
        try:
            snap = await page.accessibility.snapshot(interesting_only=True)
            how = "legacy"
        except Exception as e_old:
            log(f"    [ax] unavailable (cdp: {type(e_cdp).__name__}; "
                f"legacy: {type(e_old).__name__})")
            return {"controls": [], "via": "ax-error", "dialog": {"present": False}}
    log(f"    [ax] tree read via {how}")
    if not snap:
        return {"controls": [], "via": "ax-empty", "dialog": {"present": False}}

    out, seen = [], set()
    dialog = {"present": False}

    def walk(node, in_dialog=False):
        if len(out) >= MAX_CONTROLS or not isinstance(node, dict):
            return
        role = str(node.get("role") or "").lower()
        name = re.sub(r"\s+", " ", str(node.get("name") or "")).strip()
        if role in ("dialog", "alertdialog"):
            in_dialog = True
            if not dialog["present"]:
                dialog.update({"present": True, "title": name[:80], "text": "",
                               "scrollable": False})
        kind = _AX_ROLES.get(role)
        if kind and name and not (junk and junk(name)):
            key = (role, name.lower())
            if key not in seen:
                seen.add(key)
                out.append({"kind": kind, "label": name[:70], "role": role,
                            "value": str(node.get("value") or ""),
                            "required": bool(node.get("required")),
                            "checked": node.get("checked"),
                            "secret": role == "textbox" and "password" in name.lower(),
                            "options": [], "honeypot": False,
                            "in_dialog": in_dialog, "seen_by": "ax"})
        for ch in (node.get("children") or []):
            walk(ch, in_dialog)

    walk(snap)
    # A DIALOG OWNS THE PAGE. If one is open, keep only what is inside it -- the same rule the
    # DOM reader applies, arrived at from a completely different source.
    if dialog["present"]:
        inside = [c for c in out if c.get("in_dialog")]
        if len(inside) >= 1:
            out = inside
    for i, c in enumerate(out):
        c["i"] = i
    log(f"    [ax] the accessibility tree offers {len(out)} control(s)"
        + (f" INSIDE dialog {dialog.get('title','')!r}" if dialog["present"] else ""))
    return {"controls": out, "via": "ax", "dialog": dialog, "errors": [],
            "url": page.url or "", "step": ""}


# ---------------------------------------------------------------- 2. THE HIT GRID
_GRID_JS = r"""([cols, rows]) => {
  // TOUCH THE PAGE. For every point on a grid, ask the browser what is painted there and walk up
  // to the nearest interactive ancestor. This cannot be fooled by aria-hidden, by clip-hiding, by
  // a wrong visibility predicate or by a shadow boundary -- if a human could click a pixel, this
  // finds what they would hit. It is the most honest reader available and it costs one evaluate().
  const W = innerWidth, H = innerHeight;
  const INTER = 'button,[role=button],a[href],input,select,textarea,[role=link],[role=combobox],'
              + '[role=checkbox],[role=radio],[role=menuitem],[role=tab],[role=switch]';
  const seen = new Map();
  const lab = (t) => {
    let s = '';
    if (t.id) { const l = document.querySelector('label[for="' + CSS.escape(t.id) + '"]'); if (l) s = l.innerText; }
    if (!s) s = t.getAttribute('aria-label') || '';
    if (!s) s = t.placeholder || '';
    if (!s) s = (t.innerText || t.textContent || t.value || '');
    if (!s) { const w = t.closest('label,[data-automation-id]'); if (w) s = (w.innerText || ''); }
    return (s || t.name || t.type || '').replace(/\s+/g, ' ').trim().slice(0, 70);
  };
  for (let gy = 0; gy < rows; gy++) {
    for (let gx = 0; gx < cols; gx++) {
      const x = Math.round((gx + 0.5) * W / cols), y = Math.round((gy + 0.5) * H / rows);
      const el = document.elementFromPoint(x, y);
      if (!el) continue;
      const t = el.closest(INTER) || (el.matches && el.matches(INTER) ? el : null);
      if (!t) continue;
      if (seen.has(t)) continue;
      const r = t.getBoundingClientRect(), s = getComputedStyle(t);
      if (r.width < 2 || r.height < 2) continue;            // honeypot geometry
      const cp = (s.clipPath || '').replace(/\s+/g, ' ');
      if (/^polygon\(/.test(cp) && !/[1-9]/.test(cp)) continue;
      const L = lab(t);
      if (!L) continue;
      seen.set(t, {label: L, x: x, y: y,
                   tag: t.tagName.toLowerCase(), ty: (t.type || '').toLowerCase(),
                   value: String(t.value || ''),
                   required: !!(t.required || t.getAttribute('aria-required') === 'true'),
                   checked: (t.type === 'checkbox' || t.type === 'radio') ? !!t.checked : null,
                   secret: (t.type || '') === 'password',
                   options: t.tagName === 'SELECT'
                     ? [...t.options].map(o => o.text.trim()).filter(Boolean).slice(0, 25) : []});
    }
  }
  const out = [];
  let i = 0;
  for (const [el, d] of seen) {
    el.setAttribute('data-jhw-grid', String(i));
    out.push(Object.assign({i: i++}, d));
    if (i >= 60) break;
  }
  return {controls: out, cols: cols, rows: rows, points: cols * rows};
}"""


async def hit_grid(page, log=print, junk=None, cols: int = 40, rows: int = 25) -> dict:
    """Whatever is PAINTED and clickable, found by touching the pixels.

    1000 points by default, one evaluate(), no LLM. This is the reader that cannot be blinded:
    the aria-hidden line that cost four runs, the clip-hidden honeypot, a wrong offsetParent
    rule -- none of them affect what `elementFromPoint` returns."""
    try:
        d = await page.evaluate(_GRID_JS, [cols, rows])
    except Exception as e:
        log(f"    [grid] unavailable ({type(e).__name__}: {str(e)[:60]})")
        return {"controls": [], "via": "grid-error", "dialog": {"present": False}}
    ctrls = []
    for c in (d.get("controls") or []):
        label = str(c.get("label") or "")
        if not label or (junk and junk(label)):
            continue
        tag, ty = c.get("tag"), c.get("ty")
        kind = ("link" if tag == "a" else "select" if tag == "select"
                else ty if ty in ("checkbox", "radio", "file")
                else "textarea" if tag == "textarea"
                else "button" if tag == "button" or ty in ("submit", "button") else "input")
        ctrls.append({"i": len(ctrls), "kind": kind, "label": label[:70],
                      "css": f"[data-jhw-idx='{c.get('i')}']",
                      "value": c.get("value") or "", "required": c.get("required"),
                      "checked": c.get("checked"), "secret": c.get("secret"),
                      "options": c.get("options") or [], "honeypot": False,
                      "point": (c.get("x"), c.get("y")), "seen_by": "grid"})
    log(f"    [grid] touched {d.get('points')} point(s) -> {len(ctrls)} control(s)")
    return {"controls": ctrls, "via": "grid", "dialog": {"present": False}, "errors": [],
            "url": page.url or "", "step": ""}


# ---------------------------------------------------------------- 3. THE TAB WALK
_FOCUS_JS = r"""() => {
  const e = document.activeElement;
  if (!e || e === document.body) return null;
  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  if (s.visibility === 'hidden' || s.display === 'none') return {skip: 1};
  if (r.width < 2 || r.height < 2) return {skip: 1};
  let lab = '';
  if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) lab = l.innerText; }
  if (!lab) lab = e.getAttribute('aria-label') || '';
  if (!lab) lab = e.placeholder || '';
  if (!lab) lab = (e.innerText || e.textContent || e.value || '');
  if (!lab) { const w = e.closest('label,[data-automation-id]'); if (w) lab = (w.innerText || ''); }
  const tag = e.tagName.toLowerCase(), ty = (e.type || '').toLowerCase();
  return {tag: tag, ty: ty, label: (lab || e.name || ty).replace(/\s+/g, ' ').trim().slice(0, 70),
          value: String(e.value || ''),
          required: !!(e.required || e.getAttribute('aria-required') === 'true'),
          checked: (ty === 'checkbox' || ty === 'radio') ? !!e.checked : null,
          secret: ty === 'password',
          options: tag === 'select'
            ? [...e.options].map(o => o.text.trim()).filter(Boolean).slice(0, 25) : []};
}"""

_MARK_FOCUS_JS = r"""(i) => {
  const e = document.activeElement;
  if (!e || e === document.body) return false;
  e.setAttribute('data-jhw-tab', String(i));
  return true;}"""


async def tab_walk(page, log=print, junk=None, steps: int = 30) -> dict:
    """Press Tab and write down what gets focus. The browser's own order, no predicate at all.

    THE PROPERTY THAT MATTERS HERE: a correctly built modal TRAPS focus. So when the Workday
    sign-in dialog is open, this reader physically cannot reach 'Search Careers' or the footer
    -- the page behind is unreachable by Tab. It answers "what can I actually interact with"
    the way a keyboard user would, which is the definition we want."""
    out, seen = [], set()
    try:
        # DO NOT press Escape here. I wrote that line and it is exactly wrong: Escape CLOSES the
        # sign-in modal we are trying to read, and on a date field it reverts the value (already
        # recorded in CLAUDE.md). Clear the stale index and start tabbing from wherever focus is.
        # CLEAR ONLY OUR OWN NAMESPACE. This line used to wipe every `data-jhw-esc` on the
        # page, which destroyed the tags the readers BEFORE us had written -- so the union handed
        # out indices that addressed a different element, or nothing at all. That is the
        # `TimeoutError: waiting for [data-jhw-esc=...]` in this file's own docstring, and the
        # reason `_fresh()` had to exist. One reader, one namespace; `stamp()` owns the canonical one.
        await page.evaluate("() => document.querySelectorAll('[data-jhw-tab]')"
                            ".forEach(e => e.removeAttribute('data-jhw-tab'))")
    except Exception:
        pass
    try:
        for _ in range(steps):
            await page.keyboard.press("Tab")
            d = await page.evaluate(_FOCUS_JS)
            if not d or d.get("skip"):
                continue
            label = str(d.get("label") or "").strip()
            if not label or (junk and junk(label)):
                continue
            key = (label.lower(), d.get("tag"), d.get("ty"))
            if key in seen:
                continue                          # focus has cycled round
            seen.add(key)
            i = len(out)
            await page.evaluate(_MARK_FOCUS_JS, i)
            tag, ty = d.get("tag"), d.get("ty")
            kind = ("link" if tag == "a" else "select" if tag == "select"
                    else ty if ty in ("checkbox", "radio", "file")
                    else "textarea" if tag == "textarea"
                    else "button" if tag == "button" or ty in ("submit", "button") else "input")
            out.append({"i": i, "kind": kind, "label": label[:70],
                        "css": f"[data-jhw-idx='{i}']",
                        "value": d.get("value") or "", "required": d.get("required"),
                        "checked": d.get("checked"), "secret": d.get("secret"),
                        "options": d.get("options") or [], "honeypot": False,
                        "seen_by": "tab"})
            if len(out) >= MAX_CONTROLS:
                break
    except Exception as e:
        log(f"    [tab] walk stopped ({type(e).__name__}: {str(e)[:50]})")
    log(f"    [tab] focus order offers {len(out)} control(s)")
    return {"controls": out, "via": "tab", "dialog": {"present": False}, "errors": [],
            "url": page.url or "", "step": ""}


# ---------------------------------------------------------------- 4. THE DEEP DOM
_DEEP_JS = r"""() => {
  // querySelectorAll DOES NOT CROSS A SHADOW BOUNDARY, so a web component's controls are
  // invisible to every other reader here. Walk shadowRoots and same-origin iframes explicitly.
  const INTER = 'button,[role=button],a[href],input,select,textarea,[role=link],[role=combobox],'
              + '[role=checkbox],[role=radio],[role=menuitem],[role=tab]';
  const found = [];
  const vis = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    return r.width >= 2 && r.height >= 2;
  };
  const lab = (t) => {
    let s = t.getAttribute('aria-label') || t.placeholder || '';
    if (!s) s = (t.innerText || t.textContent || t.value || '');
    return (s || t.name || t.type || '').replace(/\s+/g, ' ').trim().slice(0, 70);
  };
  const walk = (root, depth) => {
    if (depth > 6 || found.length >= 60) return;
    let els = [];
    try { els = [...root.querySelectorAll(INTER)]; } catch (e) { return; }
    for (const el of els) {
      if (found.length >= 60) break;
      if (!vis(el)) continue;
      const L = lab(el);
      if (L) found.push({el: el, label: L, shadow: depth > 0});
    }
    let all = [];
    try { all = [...root.querySelectorAll('*')]; } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      if (el.tagName === 'IFRAME') {
        try { if (el.contentDocument) walk(el.contentDocument, depth + 1); } catch (e) {}
      }
    }
  };
  walk(document, 0);
  const out = [];
  let i = 0;
  const seen = new Set();
  for (const f of found) {
    const k = f.label.toLowerCase() + '|' + f.el.tagName;
    if (seen.has(k)) continue;
    seen.add(k);
    f.el.setAttribute('data-jhw-deep', String(i));
    const t = f.el, ty = (t.type || '').toLowerCase();
    out.push({i: i++, label: f.label, shadow: f.shadow,
              tag: t.tagName.toLowerCase(), ty: ty, value: String(t.value || ''),
              required: !!(t.required || t.getAttribute('aria-required') === 'true'),
              checked: (ty === 'checkbox' || ty === 'radio') ? !!t.checked : null,
              secret: ty === 'password',
              options: t.tagName === 'SELECT'
                ? [...t.options].map(o => o.text.trim()).filter(Boolean).slice(0, 25) : []});
  }
  return {controls: out, shadowHits: out.filter(c => c.shadow).length};
}"""


async def deep_dom(page, log=print, junk=None) -> dict:
    """Pierce shadow roots and same-origin iframes. No other reader here can see inside them."""
    try:
        d = await page.evaluate(_DEEP_JS)
    except Exception as e:
        log(f"    [deep] unavailable ({type(e).__name__}: {str(e)[:60]})")
        return {"controls": [], "via": "deep-error", "dialog": {"present": False}}
    out = []
    for c in (d.get("controls") or []):
        label = str(c.get("label") or "")
        if not label or (junk and junk(label)):
            continue
        tag, ty = c.get("tag"), c.get("ty")
        kind = ("link" if tag == "a" else "select" if tag == "select"
                else ty if ty in ("checkbox", "radio", "file")
                else "textarea" if tag == "textarea"
                else "button" if tag == "button" or ty in ("submit", "button") else "input")
        out.append({"i": len(out), "kind": kind, "label": label[:70],
                    "css": f"[data-jhw-idx='{c.get('i')}']",
                    "value": c.get("value") or "", "required": c.get("required"),
                    "checked": c.get("checked"), "secret": c.get("secret"),
                    "options": c.get("options") or [], "honeypot": False,
                    "shadow": bool(c.get("shadow")), "seen_by": "deep"})
    log(f"    [deep] {len(out)} control(s), {d.get('shadowHits', 0)} of them behind a shadow root")
    return {"controls": out, "via": "deep", "dialog": {"present": False}, "errors": [],
            "url": page.url or "", "step": ""}


# ---------------------------------------------------------------- the UNION
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# WHICH ATTRIBUTE HOLDS EACH READER'S TAG. Every reader used to write `data-jhw-esc` with its own
# 0-based numbering and `tab_walk` cleared the lot before starting, so after `see()` the DOM carried
# only the LAST reader's numbering while `merge()` handed out UNION indices. The panel was therefore
# given indices that addressed a different element -- silently, and on every union read. One reader,
# one namespace; the union owns the canonical `data-jhw-esc` and writes it once, at the end.
_NS = {"hit_grid": "grid", "tab_walk": "tab", "deep_dom": "deep"}

_STAMP_JS = r"""
(items) => {
  document.querySelectorAll('[data-jhw-esc]').forEach(e => e.removeAttribute('data-jhw-esc'));
  document.querySelectorAll('[data-jhw-idx]').forEach(e => e.removeAttribute('data-jhw-idx'));
  const done = [], lost = [];
  for (const it of items) {
    if (!it.ns) { lost.push(it.i); continue; }          // no tag to copy from (e.g. the a11y tree)
    const el = document.querySelector('[data-jhw-' + it.ns + '="' + it.esc0 + '"]');
    if (!el) { lost.push(it.i); continue; }             // re-rendered away since its reader ran
    el.setAttribute('data-jhw-esc', String(it.i));
    el.setAttribute('data-jhw-idx', String(it.i));
    done.push(it.i);
  }
  return {stamped: done, lost: lost};
}
"""


async def stamp(page, index: dict, log=print) -> dict:
    """Write the canonical `data-jhw-esc` from the UNION's numbering, once, authoritatively.

    Until this existed the index the panel saw and the attribute the executor addressed were two
    different numbering schemes that happened to agree only when a single reader had run. A control
    whose element can no longer be found is marked `stale` rather than quietly renumbered: offering
    an index that points at nothing is how a correct decision becomes a wrong click."""
    ctrls = index.get("controls") or []
    if not ctrls:
        return index
    try:
        r = await page.evaluate(_STAMP_JS, [{"i": c["i"], "ns": c.get("ns") or "",
                                             "esc0": c.get("esc0")} for c in ctrls])
    except Exception as e:
        log(f"    [stamp] could not write the canonical index ({type(e).__name__}) — the executor "
            f"will have to self-heal by label")
        return index
    lost = set(r.get("lost") or [])
    for c in ctrls:
        c["stale"] = c["i"] in lost
    if lost:
        # An a11y-tree control has no attribute to copy from and is addressed by role+name instead,
        # so it is expected here and is NOT a defect. Say which is which.
        noattr = sum(1 for c in ctrls if c["i"] in lost and not c.get("ns"))
        gone = len(lost) - noattr
        log(f"    [stamp] {len(r.get('stamped') or [])} control(s) addressable by attribute"
            + (f"; {noattr} by role+name only" if noattr else "")
            + (f"; {gone} re-rendered away and marked stale" if gone else ""))
    index["stamped"] = len(r.get("stamped") or [])
    return index


def merge(reads: list, log=print) -> dict:
    """UNION every reader, keeping the FIRST sighting of each control and recording who saw it.

    First-wins was the mistake: for a whole evening the DOM reader answered first and answered
    WRONGLY while a reader that could see sat unused. A union cannot be blinded by one bad
    predicate, and `seen_by` makes it obvious which reader is carrying the run."""
    out, seen, by = [], {}, {}
    for r in reads:
        via = (r or {}).get("via", "?")
        ns = _NS.get(via, "")
        n = 0
        for c in ((r or {}).get("controls") or []):
            key = (_norm(c.get("label")), c.get("kind"))
            if not key[0]:
                continue
            if key in seen:
                seen[key].setdefault("also", []).append(via)
                continue
            c = dict(c)
            c["ns"] = c.get("ns") or ns          # which attribute still holds this element's tag
            c["esc0"] = c.get("esc", c.get("i"))  # the number THAT reader wrote, pre-union
            c["i"] = len(out)
            seen[key] = c
            out.append(c)
            n += 1
        by[via] = n
    dlg = next(((r or {}).get("dialog") for r in reads
                if (r or {}).get("dialog", {}).get("present")), {"present": False})
    if out:
        log("    [union] " + ", ".join(f"{k}+{v}" for k, v in by.items() if v)
            + f"  ->  {len(out)} distinct control(s)")
    return {"controls": out[:MAX_CONTROLS], "via": "union(" + ",".join(by) + ")",
            "dialog": dlg, "errors": [], "url": "", "step": "", "by_reader": by}


async def see(page, log=print, junk=None, want: int = 2) -> dict:
    """Run the cheap deterministic readers and UNION them. No LLM anywhere in here.

    Order is by cost, but every reader runs unless we already have plenty, because the whole
    lesson of this project is that the first answer is not reliably the right one."""
    reads = []
    for fn in (ax_tree, hit_grid, deep_dom, tab_walk):
        try:
            r = await fn(page, log=log, junk=junk)
        except Exception as e:
            log(f"    [{fn.__name__}] raised {type(e).__name__} — ignored")
            r = {"controls": [], "via": fn.__name__ + "-error"}
        reads.append(r)
        got = len({_norm(c.get("label")) for rr in reads for c in (rr.get("controls") or [])})
        if got >= max(want, 4) and fn is not ax_tree:
            break                                  # enough distinct controls; stop paying
    u = merge(reads, log=log)
    return await stamp(page, u, log=log)


# ---------------------------------------------------------------- selftest
def _selftest() -> int:
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    print("\n[1] the union keeps every distinct control and records who saw it")
    a = {"via": "ax", "controls": [{"kind": "button", "label": "Sign in with Google"},
                                   {"kind": "button", "label": "Sign in with email"}]}
    g = {"via": "grid", "controls": [{"kind": "button", "label": "Sign in with email"},
                                     {"kind": "link", "label": "Evgeny Vainshtein"}]}
    m = merge([a, g], log=lambda s: None)
    labels = [c["label"] for c in m["controls"]]
    check("both readers' controls survive", len(labels) == 3, str(labels))
    check("the duplicate is merged, not repeated",
          labels.count("Sign in with email") == 1, str(labels))
    check("indices are renumbered contiguously",
          [c["i"] for c in m["controls"]] == [0, 1, 2])
    check("it reports what each reader contributed", m["by_reader"] == {"ax": 2, "grid": 1},
          str(m["by_reader"]))
    check("the control ONLY the grid saw is present", "Evgeny Vainshtein" in labels)

    print("\n[2] a dead reader costs nothing")
    m = merge([{"via": "ax-error", "controls": []}, g], log=lambda s: None)
    check("an empty reader does not lose the others", len(m["controls"]) == 2)
    check("merge of nothing is empty, not an exception", merge([], log=lambda s: None)["controls"] == [])
    check("merge tolerates None entries", merge([None, g], log=lambda s: None)["controls"])

    print("\n[3] label normalisation dedupes real-world variants")
    check("case and punctuation are ignored",
          _norm("Sign in with e-mail") == _norm("SIGN IN WITH EMAIL"))
    check("but different controls stay different",
          _norm("Sign in with Google") != _norm("Sign in with email"))

    print("\n[4] a dialog reported by ANY reader wins")
    d = merge([{"via": "grid", "controls": [], "dialog": {"present": False}},
               {"via": "ax", "controls": [], "dialog": {"present": True, "title": "Sign In"}}],
              log=lambda s: None)
    check("the dialog is carried through", d["dialog"].get("title") == "Sign In", str(d["dialog"]))

    print("\n[5] every JS blob is syntactically valid and tags BOTH namespaces")
    for name, blob in (("_GRID_JS", _GRID_JS), ("_FOCUS_JS", _FOCUS_JS),
                       ("_DEEP_JS", _DEEP_JS), ("_MARK_FOCUS_JS", _MARK_FOCUS_JS)):
        check(f"{name} is present", len(blob) > 100)
    for name, blob in (("_GRID_JS", _GRID_JS), ("_DEEP_JS", _DEEP_JS),
                       ("_MARK_FOCUS_JS", _MARK_FOCUS_JS)):
        check(f"{name} tags its OWN namespace, never the shared data-jhw-esc",
              re.search(r"data-jhw-(grid|tab|deep)", blob) is not None
              and "data-jhw-esc" not in blob)
    check("the grid rejects honeypot geometry", "r.width < 2" in _GRID_JS)
    check("the grid walks up to the nearest interactive ancestor", "closest(INTER)" in _GRID_JS)
    check("the deep walker crosses shadow roots", "shadowRoot" in _DEEP_JS)
    check("and same-origin iframes", "contentDocument" in _DEEP_JS)
    check("the ax reader consumes the browser's own tree",
          "accessibility.snapshot" in open(__file__, encoding="utf-8").read())

    print("\n[5b] ONE canonical index: the union stamps it, and a lost element is marked STALE")
    # FUNCTIONAL, against a fake page, because the property is about what stamp() DOES with the
    # browser's answer -- a static grep for an attribute name cannot see whether `stale` gets set.
    import asyncio as _aio

    class _FakePage:
        def __init__(self, stamped, lost):
            self.r = {"stamped": stamped, "lost": lost}
            self.sent = None

        async def evaluate(self, js, arg=None):
            self.sent = arg
            return self.r

    idx = {"controls": [{"i": 0, "ns": "grid", "esc0": 7, "label": "Email"},
                        {"i": 1, "ns": "grid", "esc0": 8, "label": "Password"},
                        {"i": 2, "ns": "", "esc0": None, "label": "Sign In"},
                        {"i": 3, "ns": "deep", "esc0": 2, "label": "Gone"}]}
    pg = _FakePage([0, 1], [2, 3])
    out = _aio.new_event_loop().run_until_complete(stamp(pg, idx, log=lambda *a: None))
    check("stamp() sends the UNION index, carrying each reader's own number to copy from",
          [x["i"] for x in pg.sent] == [0, 1, 2, 3]
          and [x["esc0"] for x in pg.sent] == [7, 8, None, 2], str(pg.sent))
    st = [c["stale"] for c in out["controls"]]
    check("a control the browser could not tag is STALE; the rest are not",
          st == [False, False, True, True], str(st))
    check("the number of attribute-addressable controls is reported", out.get("stamped") == 2,
          str(out.get("stamped")))

    class _Boom:
        async def evaluate(self, *a, **k):
            raise RuntimeError("execution context destroyed")

    keep = {"controls": [{"i": 0, "ns": "grid", "esc0": 1, "label": "X"}]}
    out2 = _aio.new_event_loop().run_until_complete(stamp(_Boom(), keep, log=lambda *a: None))
    check("a destroyed execution context leaves the index UNCHANGED, never half-stamped",
          out2["controls"][0].get("stale") is None and "stamped" not in out2)
    _src = open(__file__, encoding="utf-8").read()
    check("see() ends by stamping, so the panel's index and the executor agree by construction",
          "return await stamp(" in _src)
    check("tab_walk clears ONLY its own namespace — it used to wipe every reader's tag",
          "removeAttribute('data-jhw-tab')" in _src
          and "forEach(e => e.removeAttribute('data-jhw-esc'))\")" not in _src)

    print("\n[6] NO LLM anywhere in these readers")
    ship = open(__file__, encoding="utf-8").read()
    ship = ship[:ship.index("def _selftest")]
    code = "\n".join(ln for ln in ship.splitlines()
                     if not ln.strip().startswith("#") and not ln.strip().startswith("//"))
    for banned in ("chat/completions", "httpx", "openai", "PROXY"):
        check(f"no {banned!r} in the shipping code", banned not in code)

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} SEER CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL SEER CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
