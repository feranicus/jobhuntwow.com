#!/usr/bin/env python3
"""probe_models.py — which DO model can actually drive a browser, and how fast?

Run it with:  python jhw.py probe          (never on its own - one orchestrator)

WHY THIS EXISTS
Stagehand's act()/observe()/extract() are STRUCTURED-OUTPUT calls: the model must return strict
JSON matching a schema, once per browser action. So the selection criteria are, in order:
  1. does it return VALID, SCHEMA-CONFORMING JSON every time (a creative model is a liability),
  2. latency per call (an apply flow is 20-40 sequential calls - 500ms vs 3s decides the run),
  3. does it pick the RIGHT element from a real DOM (not just well-formed JSON).
Browserbase's own testing says the same thing in different words: Gemini 2.0 Flash beat GPT-4o and
Claude 3.5 Sonnet on accuracy at a fraction of the cost, and the "more creative" models (GPT-4.5,
Claude 3.7) were WORSE at doing exactly what they were told. We cannot call Gemini/OpenAI/Anthropic
on this DO account (403 - visibility is not entitlement), so we need the open-weight model that
behaves most like an instruction-follower.

This probe therefore sends the REAL contract - a genuine ATS form snippet plus the same JSON schema
llm_driver/Stagehand demand - and scores json_ok, schema_ok, correct_choice and latency.
It never guesses: every number printed was measured against the live endpoint.
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
import urllib.request

HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, HERE)

# Models that CANNOT work here, with the reason - so nobody re-adds them:
#   *-embed / *-rerank      : not chat models
#   *thinking* / *-r1-*     : emit reasoning before JSON -> breaks strict structured output
#   *-distill*              : same failure mode, measured in the sibling project's bake-off
SKIP = re.compile(r"embed|rerank|image|whisper|tts|guard|thinking|-r1-|distill"
                 r"|mini-lm|bge-|e5-|gte-|mpnet|stable-diffusion|wan2|-t2v-", re.I)

# A real fragment of an ATS application form, indexed the way llm_driver presents the DOM.
DOM = """[0] <input id="firstName" placeholder="First name" type="text">
[1] <input id="lastName" placeholder="Last name" type="text">
[2] <input id="email" placeholder="Email address" type="email">
[3] <input id="phone" placeholder="Phone" type="tel">
[4] <input id="resume" type="file" aria-label="Upload resume">
[5] <select id="workAuth"><option>Select</option><option>Yes</option><option>No</option></select>
[6] <button id="next">Save and Continue</button>
[7] <a href="/jobs">Back to search</a>"""

SYSTEM = ("You drive a web form. Reply with ONE JSON object and nothing else: "
          '{"action":"fill|click|ask|done","i":<element index>,"value":"<text>","why":"<short>"}. '
          "No prose, no markdown fence.")
USER = ("Candidate: Evgeny Vainshtein, feranicus@s4biz.io, +49 157 855 1545.\n"
        "The email field is still empty. Fill it.\n\nDOM:\n" + DOM)


def _post(base, key, model, timeout=45):
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": USER}],
        "max_tokens": 200,
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return (time.time() - t0) * 1000, raw


def _judge(raw):
    """json_ok / schema_ok / correct - graded exactly as the driver would consume it."""
    try:
        txt = json.loads(raw)["choices"][0]["message"]["content"]
        if not txt:
            return False, False, False, "null content (tool-call shaped reply)"
    except Exception:
        return False, False, False, "no content"
    t = txt.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n?|```$", "", t, flags=re.S).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return False, False, False, t[:60]
    try:
        j = json.loads(m.group(0))
    except Exception:
        return False, False, False, t[:60]
    schema_ok = isinstance(j, dict) and j.get("action") in ("fill", "click", "ask", "done")
    # the ONLY right answer: fill element 2 (the email input) with the candidate's address
    correct = (schema_ok and j.get("action") == "fill" and str(j.get("i")) == "2"
               and "@" in str(j.get("value", "")))
    fenced = t is not txt.strip()
    return True, schema_ok, correct, ("fenced json" if fenced else json.dumps(j)[:60])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("DO_INFERENCE_BASE_URL",
                                                     "https://inference.do-ai.run/v1"))
    ap.add_argument("--key", default="")
    ap.add_argument("--models", default="", help="comma list; default = the whole catalog")
    ap.add_argument("--runs", type=int, default=2, help="calls per model (latency varies wildly)")
    a = ap.parse_args()

    key = a.key or os.environ.get("DO_INFERENCE_KEY", "")
    if not key:
        try:
            import sync_secrets
            key = sync_secrets.collect()[0].get("DO_INFERENCE_KEY", "")
        except Exception:
            pass
    if not key:
        sys.exit("[X] no DO_INFERENCE_KEY (put it in .env or pass --key)")

    if a.models:
        ids = [m.strip() for m in a.models.split(",") if m.strip()]
    else:
        req = urllib.request.Request(a.base.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as r:
            ids = [m["id"] for m in json.loads(r.read())["data"] if m.get("id")]
        ids = [i for i in ids if not SKIP.search(i)]

    print(f"probing {len(ids)} models against the REAL driver contract "
          f"({a.runs} run(s) each)\n")
    print(f"{'model':38} {'ms':>7}  json schema correct  note")
    print("-" * 96)
    rows = []
    for mid in ids:
        times, ok, sok, cor, note = [], False, False, False, ""
        for _ in range(a.runs):
            try:
                ms, raw = _post(a.base, key, mid)
                ok, sok, cor, note = _judge(raw)
                times.append(ms)
            except Exception as e:
                note = ("%s" % e)[:48]
                break
        ms = min(times) if times else 0
        rows.append((cor, sok, ms or 9e9, mid, ok, note))
        print(f"{mid[:38]:38} {ms:7.0f}  {str(ok)[:5]:5} {str(sok)[:6]:6} {str(cor)[:7]:7} {note[:30]}")

    good = sorted([r for r in rows if r[0]], key=lambda r: r[2])
    ok_only = sorted([r for r in rows if r[1] and not r[0]], key=lambda r: r[2])
    print("\n" + "=" * 96)
    if good:
        print("CORRECT + FASTEST (use these):")
        for cor, sok, ms, mid, ok, note in good[:5]:
            print(f"   {mid:38} {ms:7.0f} ms")
        best = good[0][3]
        second = good[1][3] if len(good) > 1 else (ok_only[0][3] if ok_only else best)
        print("\nSet these in backend/app/llm.py (roles 'fast' / 'fast2'), then `python jhw.py deploy`:")
        print(f'    "fast":  "{best}",')
        print(f'    "fast2": "{second}",')
        print("\nStagehand + llm_driver ask the proxy for the alias jhw-fast, so changing llm.py")
        print("retargets BOTH without touching any container.")
    else:
        print("[!] NO model returned a correct action. Valid-JSON-but-wrong-choice models:")
        for cor, sok, ms, mid, ok, note in ok_only[:8]:
            print(f"   {mid:38} {ms:7.0f} ms  {note}")
    print("\nNote: a model that is fast but picks the wrong element is worse than a slow correct")
    print("one - a wrong click on an ATS form can submit an incomplete application.")


if __name__ == "__main__":
    main()
