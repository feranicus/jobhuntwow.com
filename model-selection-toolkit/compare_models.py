#!/usr/bin/env python3
"""
compare_models.py — settle "which model is actually better" on YOUR real task, not on benchmarks.

MMLU does not tell you whose output your users will trust. This runs the SAME prompt through each
model and prints the answers side by side with latency, token count and ~cost, so you decide from
the artifact.

Standalone + portable: any OpenAI-compatible endpoint, no SSH/Docker/project imports.

    export LLM_BASE_URL="https://inference.do-ai.run/v1"
    export LLM_API_KEY="sk-..."

    # simplest: one prompt file, the models you care about
    python compare_models.py --task prompt.txt --models deepseek-3.2,llama-4-maverick,gemma-4-31B-it

    # score valid JSON + require a field to be present (great for structured-output tasks)
    python compare_models.py --task prompt.txt --models a,b,c --json-contract --require summary,items

    # save every raw answer for inspection
    python compare_models.py --task prompt.txt --models a,b --out ./bakeoff_out

The scoreboard shows: latency · tokens · cost · valid-JSON? · required-fields-present · length.
The full answers are printed so you can read them the way your end user would. Cheapest+fastest is
NOT the win condition — a credible answer is.
"""
import argparse, json, os, sys, time, textwrap, urllib.request, urllib.error

BASE = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1").rstrip("/")
KEY  = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""

PRICE_HINTS = {
    "deepseek": 0.30, "gpt-oss-120b": 0.30, "gpt-oss-20b": 0.10, "llama": 0.25, "gemma": 0.20,
    "glm": 0.30, "kimi": 0.30, "qwen": 0.25, "mistral": 0.25, "gpt-4o-mini": 0.30, "gpt-4o": 5.0,
    "gpt-5": 5.0, "o1": 15.0, "o3": 8.0, "claude-haiku": 1.0, "claude-sonnet": 6.0,
    "claude-opus": 18.0, "nemotron": 0.40, "minimax": 0.30,
}


def out(s=""):
    print(s, flush=True)


def price(m):
    try:
        local = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.json")))
        for k, v in local.items():
            if k.lower() in m.lower():
                return float(v)
    except Exception:
        pass
    for k, v in PRICE_HINTS.items():
        if k in m.lower():
            return v
    return 0.0


def call(model, prompt, timeout, json_mode):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 4000}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    hdr = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    rq = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(), headers=hdr)
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if json_mode and e.code in (400, 422):        # model rejects response_format — retry plain
            return call(model, prompt, timeout, False)
        raise
    msg = d["choices"][0]["message"]
    txt = msg.get("content") or msg.get("reasoning_content") or ""
    return txt, d.get("usage", {})


def parse_json(txt):
    try:
        s = txt[txt.find("{"): txt.rfind("}") + 1]
        return json.loads(s)
    except Exception:
        try:
            s = txt[txt.find("["): txt.rfind("]") + 1]
            return json.loads(s)
        except Exception:
            return None


def main():
    ap = argparse.ArgumentParser(description="Run the same prompt through several models and compare.")
    ap.add_argument("--task", required=True, help="path to the prompt file (your REAL prompt)")
    ap.add_argument("--models", required=True, help="comma-separated model ids to compare")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--json-contract", action="store_true", help="request + validate JSON output")
    ap.add_argument("--require", default="", help="comma-separated JSON keys that MUST be present")
    ap.add_argument("--out", default="", help="dir to save each raw answer")
    ap.add_argument("--base", default="")
    ap.add_argument("--key", default="")
    a = ap.parse_args()

    global BASE, KEY
    if a.base:
        BASE = a.base.rstrip("/")
    if a.key:
        KEY = a.key
    if not KEY:
        sys.exit("[X] no API key. Set LLM_API_KEY (or OPENAI_API_KEY), or pass --key.")

    prompt = open(a.task, encoding="utf-8").read()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    need = [k.strip() for k in a.require.split(",") if k.strip()]
    if a.out:
        os.makedirs(a.out, exist_ok=True)

    out("=" * 96)
    out("  MODEL BAKE-OFF — same prompt, same params.   %s" % BASE)
    out("  prompt: %s  (%d chars)" % (a.task, len(prompt)))
    out("=" * 96)

    res = []
    for m in models:
        out("\n" + "-" * 96)
        out("  " + m)
        out("-" * 96)
        t = time.time()
        try:
            txt, u = call(m, prompt, a.timeout, a.json_contract)
            ms = int((time.time() - t) * 1000)
            ti, to = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
            cost = round((ti + to) / 1e6 * price(m), 6)
            j = parse_json(txt) if a.json_contract else None
            missing = [k for k in need if not (isinstance(j, dict) and j.get(k))] if need else []
            rec = {"model": m, "ms": ms, "tokens": ti + to, "cost": cost,
                   "json_ok": bool(j) if a.json_contract else None,
                   "missing": missing, "chars": len(txt)}
            res.append(rec)
            flag = ""
            if a.json_contract:
                flag = " · JSON=%s" % ("ok" if j else "FAIL")
                if need:
                    flag += " · missing=%s" % (",".join(missing) if missing else "none")
            out("  %dms · %d tok · ~$%.4f · %d chars%s" % (ms, rec["tokens"], cost, len(txt), flag))
            out("")
            preview = txt if not j else json.dumps(j, ensure_ascii=False, indent=2)
            for line in preview.splitlines()[:40]:
                for wl in textwrap.wrap(line, 92) or [""]:
                    out("    " + wl)
            if a.out:
                open(os.path.join(a.out, m.replace("/", "_") + ".txt"), "w", encoding="utf-8").write(txt)
        except Exception as e:
            out("  [X] FAILED after %dms: %r" % (int((time.time() - t) * 1000), e))
            res.append({"model": m, "ms": int((time.time() - t) * 1000), "error": repr(e)[:40]})

    ok = [r for r in res if "error" not in r]
    if len(ok) > 1:
        out("\n" + "=" * 96)
        out("  SCOREBOARD  (sorted by latency)")
        out("=" * 96)
        hdr = "  %-26s %9s %8s %10s %8s" % ("model", "ms", "tokens", "cost", "chars")
        if a.json_contract:
            hdr += " %7s" % "JSON"
        if need:
            hdr += " %10s" % "missing"
        out(hdr)
        for r in sorted(ok, key=lambda x: x["ms"]):
            row = "  %-26s %9d %8d %10.4f %8d" % (r["model"][:26], r["ms"], r["tokens"], r["cost"], r["chars"])
            if a.json_contract:
                row += " %7s" % ("ok" if r.get("json_ok") else "FAIL")
            if need:
                row += " %10s" % (",".join(r["missing"]) if r["missing"] else "-")
            out(row)
        out("\n  How to read this: latency, cost and tokens are facts. JSON/missing tell you if the")
        out("  structured contract held. The FULL answers above are the real test — read them as your")
        out("  end user would. A fast, cheap model that fills half the fields is not the winner.")


if __name__ == "__main__":
    main()
