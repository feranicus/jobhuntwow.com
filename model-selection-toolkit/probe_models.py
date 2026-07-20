#!/usr/bin/env python3
"""
probe_models.py — see every model your API key can reach, then pick a REAL main/backup/3rd chain.

Standalone + portable: talks straight to any OpenAI-compatible endpoint (DigitalOcean serverless
inference, OpenAI, OpenRouter, Together, Groq, Fireworks, a local vLLM/Ollama, ...). No SSH, no
Docker, no project imports. Point it at a base URL + key and run.

    # 1. set where to look (env vars OR --base/--key flags)
    export LLM_BASE_URL="https://inference.do-ai.run/v1"     # any OpenAI-compatible /v1
    export LLM_API_KEY="sk-..."

    python probe_models.py --list                 # JUST the catalog. One call, ~1s.
    python probe_models.py                         # catalog + live probe of a shortlist
    python probe_models.py --lang de               # also score whether the output is German
    python probe_models.py --models a,b,c          # probe exactly these ids
    python probe_models.py --task path/to/task.txt # probe with YOUR prompt instead of the demo one
    python probe_models.py --json                  # machine-readable results to stdout

WHY a chain and not "the best model":
  * A 429 / outage is usually PROVIDER-wide, so your backup MUST be a DIFFERENT VENDOR.
    model-A -> another model-A is the same failure twice.
  * Visibility != entitlement. A model can appear in /models and still return 403 on your tier.
    The only truth is a live call. That is what this does.
  * Reasoning / "thinking" / distill models break strict-JSON contracts (they emit chain-of-thought
    then truncate). If you need clean JSON, probe INSTRUCT models and read the note column.

The script scores each model on: reachable · valid-JSON contract · (optional) target language ·
latency · price, then prints a recommended chain = the best passing model per vendor.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

# ------------------------------------------------------------------ config
BASE = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1").rstrip("/")
KEY  = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""

# Per-1M-token blended price hints (input+output averaged) so the table can show ~cost.
# Override with a prices.json next to this file: {"model-id": 0.42, ...}. Unknown -> 0.
PRICE_HINTS = {
    "deepseek": 0.30, "gpt-oss-120b": 0.30, "gpt-oss-20b": 0.10, "llama": 0.25,
    "gemma": 0.20, "glm": 0.30, "kimi": 0.30, "qwen": 0.25, "mistral": 0.25,
    "gpt-4o-mini": 0.30, "gpt-4o": 5.0, "gpt-5": 5.0, "o1": 15.0, "o3": 8.0,
    "claude-haiku": 1.0, "claude-sonnet": 6.0, "claude-opus": 18.0, "nemotron": 0.40,
    "minimax": 0.30,
}

VENDORS = ("anthropic", "openai", "gpt-oss", "deepseek", "qwen", "llama", "mistral",
           "google", "gemma", "glm", "zhipu", "kimi", "moonshot", "minimax",
           "nvidia", "nemotron", "cohere", "mixtral", "phi", "gemini")

# Never probe these — not text generators, or known to break strict JSON.
SKIP = ("embed", "rerank", "whisper", "tts", "image", "guard", "moderation", "mini-lm",
        "bge-", "all-mini", "nomic", "e5-large", "gte-", "multi-qa", "stable-diffusion",
        "wan2", "-codex", "coder", "thinking", "-distill", "reasoner", "openai-o1",
        "openai-o3", "router:", "dall-e", "sora")

# Sensible shortlist of open-weight instruct families to try first if the user gives no --models.
PREFERRED = [
    "deepseek-3.2", "deepseek-chat", "deepseek-v3",
    "openai-gpt-oss-120b", "gpt-oss-120b", "openai-gpt-oss-20b",
    "llama-4-maverick", "meta-llama-3.1-70b-instruct",
    "gemma-4-31B-it", "google/gemma-2-27b-it",
    "glm-5.2", "glm-4-plus", "kimi-k2.6", "minimax-m2.5",
    "mistral-3-14B", "mistral-large", "qwen2.5-72b-instruct",
    "nvidia-nemotron-3-super-120b",
]


def out(s=""):
    print(s, flush=True)


def vendor(m):
    ml = m.lower()
    fam = {"gpt-oss": "openai(oss)", "gemma": "google", "gemini": "google", "glm": "zhipu",
           "zhipu": "zhipu", "kimi": "moonshot", "moonshot": "moonshot",
           "nemotron": "nvidia", "mixtral": "mistral", "phi": "microsoft"}
    for v in VENDORS:
        if v in ml:
            return fam.get(v, v)
    return "other"


def price(m):
    ml = m.lower()
    try:
        local = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.json")))
        for k, v in local.items():
            if k.lower() in ml:
                return float(v)
    except Exception:
        pass
    for k, v in PRICE_HINTS.items():
        if k in ml:
            return v
    return 0.0


def api(path, body=None, timeout=25):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    rq = urllib.request.Request(url, data=data, headers=hdr, method="POST" if body else "GET")
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read())


# ------------------------------------------------------------------ default task
DEMO_TASK = (
    'Return ONLY strict JSON, no prose, no markdown fence: '
    '{"summary":"<2 sentences>","items":[{"id":"1","note":"..."}],"tags":["..."]} '
    "Topic: an internet-exposed database on a company network edge — risk in two sentences."
)
DE_SUFFIX = " Schreibe ALLE Fliesstexte auf Hochdeutsch (Sie-Form). JSON-Schluessel bleiben englisch."


def main():
    ap = argparse.ArgumentParser(description="Probe an OpenAI-compatible endpoint and pick a model chain.")
    ap.add_argument("--list", action="store_true", help="only print the catalog (1 API call)")
    ap.add_argument("--lang", default="en", help="'de' also scores whether output is German; any tag works as a label")
    ap.add_argument("--models", default="", help="comma-separated ids to probe (default: auto shortlist)")
    ap.add_argument("--task", default="", help="path to a prompt file to probe with (default: built-in demo)")
    ap.add_argument("--timeout", type=int, default=90, help="per-call seconds (slow models need 60-90)")
    ap.add_argument("--base", default="", help="override base URL")
    ap.add_argument("--key", default="", help="override API key")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON results")
    ap.add_argument("--top", type=int, default=3, help="chain length to recommend")
    a = ap.parse_args()

    global BASE, KEY
    if a.base:
        BASE = a.base.rstrip("/")
    if a.key:
        KEY = a.key
    if not KEY:
        sys.exit("[X] no API key. Set LLM_API_KEY (or OPENAI_API_KEY), or pass --key.")

    # ---------- 1. catalog ----------
    t = time.time()
    try:
        catalog = sorted(m.get("id") for m in api("/models").get("data", []))
    except Exception as e:
        sys.exit("[X] GET %s/models failed: %r" % (BASE, e))

    out("=" * 80)
    out("  CATALOG — %d models visible to this key   (%dms)   %s"
        % (len(catalog), int((time.time() - t) * 1000), BASE))
    out("=" * 80)
    byv = {}
    for m in catalog:
        byv.setdefault(vendor(m), []).append(m)
    for v in sorted(byv):
        out("\n  %s (%d)" % (v.upper(), len(byv[v])))
        for m in byv[v]:
            out("     %s" % m)
    if a.list:
        out("\n  (--list: catalog only)")
        return

    # ---------- 2. shortlist ----------
    if a.models:
        cands = [m.strip() for m in a.models.split(",") if m.strip()]
    else:
        cands = [m for m in PREFERRED if m in catalog]
        have = {vendor(m) for m in cands}
        for m in catalog:                       # ensure no vendor family is missed
            ml = m.lower()
            if any(s in ml for s in SKIP):
                continue
            if vendor(m) not in have:
                cands.append(m)
                have.add(vendor(m))
        cands = cands[:10]
    if not cands:
        sys.exit("[X] nothing to probe — pass --models with ids from the catalog above.")

    task = open(a.task, encoding="utf-8").read() if a.task else DEMO_TASK
    if a.lang.lower().startswith("de") and not a.task:
        task += DE_SUFFIX

    out("\n" + "=" * 80)
    out("  PROBING %d candidates (lang=%s, %ds timeout each)" % (len(cands), a.lang, a.timeout))
    out("=" * 80)
    out("\n  %-32s %-13s %8s %7s %8s  %s" % ("model", "vendor", "status", "ms", "$/1M", "note"))
    out("  " + "-" * 84)

    def ask(m, strict=True):
        body = {"model": m, "messages": [{"role": "user", "content": task}],
                "temperature": 0.3, "max_tokens": 500}
        if strict:
            body["response_format"] = {"type": "json_object"}
        return api("/chat/completions", body, timeout=a.timeout)

    res = []
    for m in cands:
        rec = {"model": m, "vendor": vendor(m), "price": price(m)}
        t = time.time()
        try:
            try:
                d = ask(m, True)
            except urllib.error.HTTPError as e:
                if e.code in (400, 422):
                    rec["note_extra"] = "no response_format"
                    d = ask(m, False)             # some models reject response_format
                elif e.code == 429:
                    time.sleep(4)
                    d = ask(m, True)               # transient quota — one retry
                else:
                    raise
            rec["ms"] = int((time.time() - t) * 1000)
            msg = d["choices"][0]["message"]
            txt = msg.get("content") or msg.get("reasoning_content") or ""
            u = d.get("usage", {})
            rec["tokens"] = int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))
            try:
                s = txt[txt.find("{"):]
                j, _ = json.JSONDecoder().raw_decode(s)
                sample = str(j.get("summary") or j.get("exec_summary") or next(iter(j.values()), ""))
                rec["contract_ok"] = isinstance(j, dict) and len(j) > 0
                rec["sample"] = sample[:60]
                if a.lang.lower().startswith("de"):
                    de = sum(w in sample for w in ("Sie", "der", "die", "das", "und", "ist", "wird", "Risiko", "Datenbank"))
                    en = sum(w in sample for w in (" the ", " is ", " and ", " of ", "database", "exposed"))
                    rec["lang_ok"] = de > en
                rec["status"] = "ok" if rec["contract_ok"] else "bad-json"
                if a.lang.lower().startswith("de"):
                    rec["note"] = "German OK" if rec.get("lang_ok") else "NOT German"
                else:
                    rec["note"] = "contract OK" if rec["contract_ok"] else "no JSON object"
                if rec.get("note_extra"):
                    rec["note"] += " (%s)" % rec["note_extra"]
            except Exception:
                rec.update(status="bad-json", contract_ok=False, note="not JSON", sample=txt[:60])
        except urllib.error.HTTPError as e:
            rec.update(status="http-%d" % e.code, ms=int((time.time() - t) * 1000), contract_ok=False,
                       note={401: "no access on this key", 403: "forbidden on this tier",
                             404: "not on this endpoint", 429: "account quota/balance"}.get(e.code, e.reason))
        except Exception as e:
            rec.update(status="timeout" if "timed out" in repr(e) else "error",
                       ms=int((time.time() - t) * 1000), contract_ok=False, note=repr(e)[:26])
        res.append(rec)
        out("  %-32s %-13s %8s %7s %8s  %s"
            % (m[:32], rec["vendor"], rec["status"], rec.get("ms", "-"),
               ("%.2f" % rec["price"]) if rec["price"] else "-", rec.get("note", "")[:26]))

    # ---------- 3. recommend a vendor-diverse chain ----------
    ok = sorted([r for r in res if r.get("contract_ok")
                 and (not a.lang.lower().startswith("de") or r.get("lang_ok"))],
                key=lambda x: x["ms"])
    out("\n" + "=" * 80)
    if not ok:
        out("  NOTHING passed. If everything is http-429 that is an account quota / empty balance —")
        out("  a different model cannot fix it. If http-403, your tier cannot call those models.")
    else:
        best, seen = [], set()
        for r in ok:
            if r["vendor"] not in seen:
                seen.add(r["vendor"])
                best.append(r)
        out("  RECOMMENDED CHAIN — fastest passing model per VENDOR")
        out("  (a 429/outage is provider-wide, so each backup is a DIFFERENT vendor)")
        out("=" * 80)
        roles = ["MAIN", "BACKUP", "3RD"] + ["ALT"] * 9
        for i, r in enumerate(best[:a.top]):
            out("   %-7s %-32s %-13s %5dms  %s"
                % (roles[i], r["model"], r["vendor"], r["ms"], r.get("sample", "")[:34]))
        out("\n  Chain string (env-var style):")
        out('    ENRICH_MODELS="%s"' % ",".join(r["model"] for r in best[:a.top]))

    if a.json:
        out("\n--- JSON ---")
        out(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
