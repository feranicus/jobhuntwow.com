#!/usr/bin/env python3
"""SET-OF-MARK — draw numbered boxes on the screenshot and let the model answer with a NUMBER.

WHY THIS EXISTS, and it is a MEASURED failure of the reader it replaces. `vision.py` asks the model
for the visible TEXT of each control and then resolves that text through Playwright's accessible-name
lookup. On the Accenture sign-in gate the model answered, correctly and usefully:

    ['evgeny@s4biz.io', '************', 'Sign In', 'Create Account', 'Forgot your password?']

and the first THREE were discarded as unresolvable. That is not a tuning problem, it is structural:

  * `'evgeny@s4biz.io'` is the e-mail box's VALUE. Its accessible NAME is "Email Address". No
    locator in Playwright matches an input by its value, so `get_by_role("textbox", name=...)`,
    `get_by_label`, `get_by_placeholder` and `get_by_text` all return zero.
  * `'************'` is the BROWSER'S OWN PASSWORD MASK. That string exists in no attribute, in no
    text node, nowhere in the DOM. It is unresolvable by construction.
  * `'Sign In'` was the MODAL TITLE, not a button.

So the two controls that mattered -- the only two fields on a sign-in form -- were deleted from the
index, and the panel was handed a page with no inputs on it. Four vendors then reasoned perfectly
about a page that had been emptied before they saw it.

SET-OF-MARK INVERTS THE DIRECTION OF TRUST, and it is the documented state of the art (Yang et al.,
"Set-of-Mark Prompting", and WebVoyager, which reports 59.1% task success over 643 tasks on 15 real
sites using exactly this): we do NOT ask the model to name things we then hunt for. We take the
candidates we ALREADY HOLD REAL HANDLES TO, paint a numbered box over each one, and ask which NUMBER
to act on. The answer is an index into a list we built ourselves, so:

  * RESOLUTION IS BY CONSTRUCTION. Mark 4 is the element we tagged as 4. Nothing is looked up, so
    nothing can fail to look up. A filled input, a masked password box and an icon-only button are
    all equally addressable, because the mark does not depend on the element exposing a name.
  * A HALLUCINATION IS STRUCTURALLY UNCLICKABLE. A number outside 1..N is refused by `parse()`, a
    pure function. The model cannot invent a mark that exists.
  * NO PIXEL CLICKING, EVER. The model returns integers; deterministic code clicks the handle those
    integers name. A mis-aimed coordinate click on a real employer's form is destructive and
    unattributable, which is why this project forbade it, and Set-of-Mark does not need it.

WHAT IT ADDS OVER THE DOM READERS: not the LIST -- they built the list -- but the JUDGEMENT. The
readers can tell us eight controls exist; they cannot tell us which is the e-mail box when the label
is an image, or that the thing at mark 6 is a cookie banner sitting over the form. That is what a
human eye supplies, and it is the only thing we are asking the model for.

COST: one call (~722ms measured, `nemotron-nano-12b-v2-vl`). STALL PATH ONLY --
FOUR_MODEL_CONSENSUS.md §7.6 rule 2 forbids a model in a request path, and the fill loop is one.
"""
from __future__ import annotations
import base64
import io as _io
import json
import os
import re

import httpx

PROXY = os.getenv("JHW_PROXY_BASE", "http://backend:8000/v1")
TOKEN = os.getenv("AGENT_PROXY_TOKEN", "none")
MODEL = os.getenv("JHW_VISION_MODEL", "jhw-vision")
TIMEOUT = float(os.getenv("JHW_SOM_TIMEOUT", "45"))
ENABLED = os.getenv("JHW_SOM", "1").lower() not in ("0", "false", "no")
OUT = os.getenv("JHW_OUT", "/agent/out")

MAX_MARKS = int(os.getenv("JHW_SOM_MAX", "40"))

# Geometry of every element the deterministic readers have already TAGGED. We never invent a
# candidate here -- `data-jhw-esc` is written by the index that ran before us, so a mark can only
# ever point at an element some reader already vouched for and already holds an addressable handle
# to. That is the whole safety argument, and it is why this file has no visibility predicate of its
# own to get wrong.
BOX_JS = r"""
() => {
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  document.querySelectorAll('[data-jhw-esc]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    // must be at least partly INSIDE the viewport: a mark drawn off-screen is a mark the model
    // cannot see, and offering it would invite a pick the human eye never had a chance to check.
    if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) return;
    const ty = (el.tagName || '').toLowerCase();
    const it = (el.getAttribute('type') || '').toLowerCase();
    out.push({
      esc: el.getAttribute('data-jhw-esc'),
      x: Math.max(0, r.left), y: Math.max(0, r.top),
      w: Math.min(vw, r.right) - Math.max(0, r.left),
      h: Math.min(vh, r.bottom) - Math.max(0, r.top),
      tag: ty, itype: it,
      secret: ty === 'input' && it === 'password',
      filled: ty === 'input' || ty === 'textarea' ? !!(el.value || '').length : false
    });
  });
  return {vw: vw, vh: vh, boxes: out};
}
"""

_SYS = (
    "You are looking at a screenshot of a web page. Every control that can be acted on has a "
    "NUMBERED BOX drawn over it. Answer ONLY about numbers you can actually see in the picture.\n"
    "Return STRICT JSON, no prose, no markdown:\n"
    '{"marks":[{"n":1,"what":"email field"},{"n":3,"what":"submit button"}],'
    '"blocking":null,"note":"one short sentence"}\n'
    "RULES:\n"
    "1. `n` MUST be a number printed in the image. Never invent one.\n"
    "2. `what` is the control's PURPOSE in a few words (email field, password field, submit button, "
    "create-account link, cookie-accept, close-dialog, language-picker, navigation, unknown).\n"
    "3. `blocking` is the number of an overlay that COVERS the form (cookie banner, modal backdrop, "
    "chat widget) and must be dismissed first, or null if nothing is in the way.\n"
    "4. List every control you can see, in reading order. Do not skip fields that already contain "
    "text or dots -- a filled box and a masked password box are still controls.\n"
    "5. If the picture shows a sign-in form, say which number is the e-mail box and which is the "
    "password box. Getting those two right matters more than anything else."
)


def _fnt(size: int):
    """A readable numeral or the run is pointless -- PIL's builtin bitmap font is ~6px tall and a
    model cannot reliably read it against page content."""
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)          # Pillow >= 10.1
    except Exception:
        return ImageFont.load_default()


# Ten hues that stay distinguishable next to ordinary page chrome. Marks are drawn in a CYCLE so two
# adjacent controls never share a colour -- a numeral is read far more reliably when its badge does
# not blend into the badge beside it.
_HUES = [(255, 59, 87), (0, 168, 255), (46, 204, 113), (255, 165, 0), (155, 89, 182),
         (26, 188, 156), (231, 76, 60), (52, 152, 219), (241, 196, 15), (192, 57, 43)]


def place(boxes: list, vw: int, vh: int) -> list:
    """WHERE each numbered badge goes. PURE -- no browser, no image -- so the one part with real
    arithmetic in it is testable on its own.

    The badge sits at the box's top-left, nudged INSIDE when that would fall off the viewport, and
    pushed BELOW the box when the slot above is already taken by another badge. Overlapping badges
    are the single easiest way to make a Set-of-Mark screenshot unreadable, and an unreadable mark
    is worse than a missing one: the model answers about the wrong control with full confidence."""
    taken, out = [], []
    for i, b in enumerate(boxes):
        bw, bh = 26 + 8 * (len(str(i + 1)) - 1), 20
        cands = [(b["x"], b["y"]),                                  # top-left, on the border
                 (b["x"] + b["w"] - bw, b["y"]),                    # top-right
                 (b["x"], b["y"] + b["h"] - bh),                    # bottom-left
                 (b["x"] + b["w"] - bw, b["y"] + b["h"] - bh)]
        best = None
        for cx, cy in cands:
            cx = max(0, min(cx, vw - bw))
            cy = max(0, min(cy, vh - bh))
            if not any(abs(cx - tx) < bw and abs(cy - ty) < bh for tx, ty in taken):
                best = (cx, cy)
                break
        if best is None:                                            # every slot contested: stack down
            cx = max(0, min(b["x"], vw - bw))
            cy = max(0, min(b["y"], vh - bh))
            while any(abs(cx - tx) < bw and abs(cy - ty) < bh for tx, ty in taken) and cy + bh < vh:
                cy += bh + 2
            best = (cx, min(cy, vh - bh))
        taken.append(best)
        out.append({"n": i + 1, "bx": int(best[0]), "by": int(best[1]),
                    "bw": int(bw), "bh": int(bh), "colour": _HUES[i % len(_HUES)]})
    return out


def draw(png: bytes, boxes: list, vw: int, vh: int) -> tuple:
    """Paint the numbered boxes. Returns (png_bytes, badge_layout)."""
    from PIL import Image, ImageDraw
    im = Image.open(_io.BytesIO(png)).convert("RGB")
    # A device-pixel-ratio > 1 makes the screenshot bigger than the CSS viewport the rects came from.
    # Scaling the rects instead of the image keeps the marks on the controls at any DPR -- getting
    # this wrong shifts every box and the model then describes its neighbour.
    sx = im.width / float(vw or im.width)
    sy = im.height / float(vh or im.height)
    layout = place(boxes, vw, vh)
    d = ImageDraw.Draw(im)
    f = _fnt(15)
    for b, m in zip(boxes, layout):
        col = m["colour"]
        d.rectangle([b["x"] * sx, b["y"] * sy, (b["x"] + b["w"]) * sx, (b["y"] + b["h"]) * sy],
                    outline=col, width=3)
        x0, y0 = m["bx"] * sx, m["by"] * sy
        x1, y1 = x0 + m["bw"] * sx, y0 + m["bh"] * sy
        d.rectangle([x0, y0, x1, y1], fill=col)
        d.text((x0 + 5 * sx, y0 + 2 * sy), str(m["n"]), fill=(255, 255, 255), font=f)
    buf = _io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), layout


def parse(text: str, n_max: int) -> dict:
    """Model answer -> {marks:[{n,what}], blocking, note}. PURE, and it REFUSES anything it cannot
    verify: a non-integer, a number outside 1..n_max, a duplicate. That range check is the entire
    hallucination guard -- the model can only ever name a mark we drew, and we only drew marks over
    elements a deterministic reader had already tagged."""
    out = {"marks": [], "blocking": None, "note": "", "refused": []}
    if not text:
        return out
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    d = None
    try:
        d = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
            except Exception:
                d = None
    if not isinstance(d, dict):
        # SALVAGE, because a truncated answer is still evidence. `{"n":3,"what":"submit"}` objects are
        # individually well-formed even when the array around them never closed.
        for mm in re.finditer(r'\{\s*"n"\s*:\s*(\d+)\s*,\s*"what"\s*:\s*"([^"]{0,60})"', t):
            d = d or {"marks": []}
            d["marks"].append({"n": int(mm.group(1)), "what": mm.group(2)})
    if not isinstance(d, dict):
        return out
    seen = set()
    for it in (d.get("marks") or []):
        if not isinstance(it, dict):
            continue
        try:
            n = int(it.get("n"))
        except Exception:
            out["refused"].append(f"not a number: {it.get('n')!r}")
            continue
        if not (1 <= n <= n_max):
            out["refused"].append(f"mark {n} does not exist (1..{n_max})")
            continue
        if n in seen:
            continue
        seen.add(n)
        out["marks"].append({"n": n, "what": str(it.get("what") or "")[:60]})
    b = d.get("blocking")
    if b is not None:
        try:
            b = int(b)
            out["blocking"] = b if 1 <= b <= n_max else None
            if out["blocking"] is None:
                out["refused"].append(f"blocking mark {b} does not exist")
        except Exception:
            out["blocking"] = None
    out["note"] = str(d.get("note") or "")[:200]
    return out


async def _ask(png: bytes, n_max: int, task: str) -> str:
    b64 = base64.b64encode(png).decode()
    body = {"model": MODEL, "temperature": 0.0, "max_tokens": 1400,
            "messages": [{"role": "system", "content": _SYS},
                         {"role": "user", "content": [
                             {"type": "text", "text":
                                 f"{task}\nThe image has marks numbered 1 to {n_max}."},
                             {"type": "image_url",
                              "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{PROXY}/chat/completions",
                         headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "")


async def refine(page, index: dict, task: str = "", log=print) -> dict:
    """Annotate an EXISTING control index with what the eye can see.

    It never adds or removes a control -- the deterministic readers own the list, and one of them
    dropping a field is a bug to fix there, not to paper over here. What it does add is `purpose`
    (which of these eight things is the e-mail box) and `blocking` (something is sitting over the
    form), plus a `som` marker so the log can say the eye was consulted. On any failure the index is
    returned UNCHANGED: a reader that cannot see must not degrade one that could."""
    ctrls = list(index.get("controls") or [])
    if not (ENABLED and ctrls):
        return index
    try:
        geo = await page.evaluate(BOX_JS)
    except Exception as e:
        log(f"    [som] could not measure the page ({type(e).__name__}) — index unchanged")
        return index
    by_esc = {str(c.get("esc")): c for c in ctrls if c.get("esc") is not None}
    boxes = [b for b in (geo.get("boxes") or []) if str(b.get("esc")) in by_esc][:MAX_MARKS]
    if len(boxes) < 2:
        log(f"    [som] only {len(boxes)} of {len(ctrls)} control(s) have on-screen geometry — "
            f"skipping the overlay")
        return index
    try:
        shot = await page.screenshot(type="png")
        marked, layout = draw(shot, boxes, geo.get("vw") or 0, geo.get("vh") or 0)
    except Exception as e:
        log(f"    [som] overlay failed ({type(e).__name__}: {str(e)[:60]}) — index unchanged")
        return index
    try:
        raw = await _ask(marked, len(boxes), task or "Which marks are the controls on this page?")
    except Exception as e:
        log(f"    [som] the model did not answer ({type(e).__name__}: {str(e)[:70]}) — index unchanged")
        return index
    got = parse(raw, len(boxes))
    for bad in got["refused"][:3]:
        log(f"    [som] REFUSED {bad}")
    if not got["marks"]:
        log("    [som] the model named no usable mark — index unchanged")
        return index
    hit = 0
    for m in got["marks"]:
        b = boxes[m["n"] - 1]
        c = by_esc.get(str(b.get("esc")))
        if c is None:
            continue
        c["purpose"] = m["what"]
        c["som"] = m["n"]
        hit += 1
    block = None
    if got["blocking"] is not None:
        bb = boxes[got["blocking"] - 1]
        block = by_esc.get(str(bb.get("esc")))
        if block is not None:
            block["blocking"] = True
    log(f"    [som] {len(boxes)} mark(s) drawn, the eye labelled {hit}"
        + (f"; OVERLAY IN THE WAY: {(block or {}).get('label') or '?'}" if block else ""))
    if got["note"]:
        log(f"    [som] \"{got['note'][:110]}\"")
    try:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "som_last.png"), "wb") as fh:
            fh.write(marked)
    except Exception:
        pass
    index["som"] = {"marks": len(boxes), "labelled": hit, "note": got["note"],
                    "blocking": (block or {}).get("label") if block else None}
    return index


def _selftest() -> int:
    fails = []

    def ck(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("[1] parse() refuses what it cannot verify")
    r = parse('{"marks":[{"n":2,"what":"email field"},{"n":99,"what":"ghost"},'
              '{"n":"x","what":"junk"},{"n":2,"what":"dupe"}],"blocking":null,"note":"hi"}', 8)
    ck([m["n"] for m in r["marks"]] == [2], f"only the in-range mark survives: {r['marks']}")
    ck(any("99" in x for x in r["refused"]), "an out-of-range mark is refused BY NUMBER")
    ck(any("not a number" in x for x in r["refused"]), "a non-numeric mark is refused")
    ck(r["note"] == "hi", "the note is carried")

    print("[2] the hallucination guard is the RANGE, and it is absolute")
    r = parse('{"marks":[{"n":0,"what":"a"},{"n":-3,"what":"b"},{"n":41,"what":"c"}]}', 40)
    ck(r["marks"] == [], f"0, negative and n_max+1 all refused: {r['marks']}")

    print("[3] a blocking overlay outside the range is dropped, not acted on")
    ck(parse('{"marks":[{"n":1,"what":"a"}],"blocking":77}', 3)["blocking"] is None,
       "blocking=77 with 3 marks -> None")
    ck(parse('{"marks":[{"n":1,"what":"a"}],"blocking":3}', 3)["blocking"] == 3,
       "a real blocking mark is kept")

    print("[4] a TRUNCATED answer is salvaged, not thrown away")
    r = parse('{"marks":[{"n":1,"what":"email field"},{"n":4,"what":"submit but', 6)
    ck([m["n"] for m in r["marks"]] == [1], f"the complete object survives: {r['marks']}")
    r = parse('```json\n{"marks":[{"n":3,"what":"password field"}]}\n```', 5)
    ck([m["n"] for m in r["marks"]] == [3], "fenced JSON is accepted")

    print("[5] place() never overlaps two badges — an unreadable numeral is worse than none")
    bx = [{"x": 100, "y": 100, "w": 200, "h": 30} for _ in range(6)]      # six identical stacked
    lay = place(bx, 1280, 800)
    ck(len(lay) == 6, "one badge per box")
    coll = 0
    for i in range(len(lay)):
        for j in range(i + 1, len(lay)):
            a, b = lay[i], lay[j]
            if abs(a["bx"] - b["bx"]) < a["bw"] and abs(a["by"] - b["by"]) < a["bh"]:
                coll += 1
    ck(coll == 0, f"zero badge collisions on six coincident boxes ({coll})")
    ck(all(0 <= m["bx"] and m["bx"] + m["bw"] <= 1280 for m in lay), "badges stay inside the width")
    ck(all(0 <= m["by"] and m["by"] + m["bh"] <= 800 for m in lay), "badges stay inside the height")

    print("[6] a box at the very edge is nudged INSIDE the viewport")
    lay = place([{"x": 1275, "y": 795, "w": 40, "h": 40}], 1280, 800)
    ck(lay[0]["bx"] + lay[0]["bw"] <= 1280 and lay[0]["by"] + lay[0]["bh"] <= 800,
       f"edge badge clamped to ({lay[0]['bx']},{lay[0]['by']})")

    print("[7] adjacent marks never share a colour")
    lay = place([{"x": i * 60, "y": 0, "w": 50, "h": 20} for i in range(4)], 1280, 800)
    ck(all(lay[i]["colour"] != lay[i + 1]["colour"] for i in range(3)), "colours cycle")

    print("[8] the overlay actually RENDERS, and the marks land on the boxes")
    try:
        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (1280, 800), (250, 250, 250)).save(buf, "PNG")
        boxes = [{"x": 400, "y": 300, "w": 300, "h": 34, "esc": "0"},
                 {"x": 400, "y": 360, "w": 300, "h": 34, "esc": "1"},
                 {"x": 400, "y": 420, "w": 120, "h": 38, "esc": "2"}]
        png, lay = draw(buf.getvalue(), boxes, 1280, 800)
        im = Image.open(_io.BytesIO(png))
        ck(im.size == (1280, 800), f"image size preserved {im.size}")
        ck(len(png) > 1500, f"a real PNG came back ({len(png)} bytes)")
        px = im.load()
        # the badge pixel must carry mark 1's colour, i.e. something was actually painted there
        ck(px[lay[0]["bx"] + 2, lay[0]["by"] + 2] != (250, 250, 250),
           "mark 1's badge is painted on the canvas")
        ck(px[402, 300] != (250, 250, 250), "box 1's outline is painted on its top edge")
        ck(px[10, 10] == (250, 250, 250), "nothing is painted where there is no control")
    except ImportError:
        print("  [i] Pillow missing here — the render check runs inside jhw-agent")

    print("[9] DPR>1: the rects are scaled, not the image")
    try:
        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (2560, 1600), (250, 250, 250)).save(buf, "PNG")   # 2x screenshot
        png, lay = draw(buf.getvalue(), [{"x": 100, "y": 100, "w": 200, "h": 40, "esc": "0"}],
                        1280, 800)
        im = Image.open(_io.BytesIO(png))
        px = im.load()
        ck(px[204, 200] != (250, 250, 250), "at DPR 2 the outline lands at 2x the CSS rect")
        ck(px[204, 100] == (250, 250, 250), "and NOT at the unscaled position")
    except ImportError:
        pass

    print("[10] BOX_JS only ever measures elements a reader already TAGGED")
    ck("[data-jhw-esc]" in BOX_JS, "the query is scoped to data-jhw-esc")
    ck("querySelectorAll('[data-jhw-esc]')" in BOX_JS.replace('"', "'"),
       "no other selector is used, so no candidate can be invented here")
    ck("password" in BOX_JS, "a masked field is still measured and marked")

    print("[11] refine() never invents or deletes a control")
    # ASSERT ON THE SYNTAX TREE, NOT ON SUBSTRINGS. My first version of this check searched the
    # function text for "del " and matched inside the word "mo|del| did not answer" -- failing a
    # correct file. That is the same wrong-subject defect this project has now paid for repeatedly:
    # a grep is aimed at characters, and the property is about CODE.
    import ast as _ast
    src = _io.open(__file__, encoding="utf-8").read()
    fn = next(n for n in _ast.walk(_ast.parse(src))
              if isinstance(n, _ast.AsyncFunctionDef) and n.name == "refine")
    muts = [n for n in _ast.walk(fn)
            if isinstance(n, _ast.Delete)
            or (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
                and n.func.attr in ("append", "pop", "insert", "remove", "extend", "clear"))]
    ck(not muts, f"refine() performs no list mutation and no del ({len(muts)} found)")
    ck(sum(1 for n in _ast.walk(fn) if isinstance(n, _ast.Return)) >= 5,
       "every failure path has its own `return index`")
    # AND MEASURE ONLY THE SHIPPING CODE. The naive version searched the WHOLE file for the banned
    # strings and matched THIS VERY ASSERTION -- the self-referential false positive that has now
    # cost this project ten review cycles. The selftest is not shipped, so it is not the subject.
    ship = src[:src.index("def _selftest(")]
    ck(len(ship) > 4000, f"the measured slice is the real module ({len(ship)} chars)")
    for banned in ("mouse.click", "mouse.move", "position="):
        ck(banned not in ship, f"no {banned} in the shipping code — the model never clicks a pixel")

    print("-" * 74)
    print(f"{'ALL SET-OF-MARK CONTRACTS HOLD' if not fails else str(len(fails)) + ' FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
