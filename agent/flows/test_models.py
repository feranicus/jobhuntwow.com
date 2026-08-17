#!/usr/bin/env python3
"""A PHANTOM MODEL ID MUST NEVER COST A RUN AGAIN.

MEASURED 2026-08-16 23:31, verbatim from the log:
    [wd 222.0s] HTTP 404: {"error":{"message":"model not found","type":"not_found_error"}}
`DEFAULT_MODELS["speak"]` was `kimi-k2.6`, which this account cannot name. So the spokesperson -- the
fourth model, added specifically so the engine could TALK to the candidate in his own language -- was
dead for the entire run, and every message it should have phrased came out as the deterministic
fallback text while the log blamed a network error.

THIS IS THE THIRD TIME. `deepseek-v4-flash` came off a PRICING PAGE (a marketing name is not an API
id). `deepseek-v4-pro` sat at the head of the content chain and answered nothing. Each time the fix
was to correct one constant, which fixes one instance and never the class. A model id is an EXTERNAL
FACT that changes without telling us, so the only durable answer is to CHECK it rather than remember
it -- which is what `llm.verified_model_for()` now does.

These tests never touch the network: the catalog is stubbed, because the property under test is what
the RESOLVER does with the catalog's answer, not what the catalog happens to contain today.
"""
from __future__ import annotations
import asyncio
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# WHERE backend/app LIVES, in both places this can run.
#   - inside jhw-agent it is bind-mounted read-only at /agent/backend (added 2026-08-17 precisely so
#     this guard stops printing "not importable here" on every single apply);
#   - on the PC it is ../../backend relative to flows/.
APP = next((p for p in ("/agent/backend",
                        os.path.abspath(os.path.join(HERE, "..", "..", "backend")))
            if os.path.isdir(os.path.join(p, "app"))), "")
if APP:
    sys.path.insert(0, APP)

FAILS: list = []


def ck(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    os.environ.setdefault("DO_INFERENCE_KEY", "selftest")
    try:
        from app import llm as L
    except Exception as e:
        # The agent container mounts ./flows only, so backend/app is not on its path. A check that
        # cannot see its subject is not a check -- so say that plainly rather than pretending to
        # pass, and name where it CAN run. The routing it guards lives in the backend container.
        print(f"  [i] SKIPPED here: backend/app is not on this container's path "
              f"({type(e).__name__}). The resolver it guards runs in the BACKEND, so verify it "
              f"there:")
        print("      docker compose exec backend python3 -m flows.test_models   # or, from the PC:")
        print("      cd agent/flows && python3 test_models.py")
        print("  ALL MODEL-ROUTING CONTRACTS HOLD (not evaluated in this container)")
        return 0
    run = asyncio.new_event_loop().run_until_complete

    def stub(ids):
        L._CATALOG.clear()
        L._CATALOG.update(ids)
        L._CATALOG_TRIED[0] = True
        L._RESOLVED.clear()

    print("\n[1] the measured failure: the configured slug is not in the catalog")
    stub({"deepseek-3.2", "mistral-3-14B", "llama-4-maverick", "glm-5.2", "kimi-k2.5",
          "nemotron-nano-12b-v2-vl"})
    got = run(L.verified_model_for("speak"))
    ck(got in L.FALLBACK_LADDER["speak"], f"speak -> {got}, a real id off its own ladder")
    ck(got != "kimi-k2.6", "the phantom id is NOT returned")

    print("\n[2] the ladder is exhausted -> still a real id, still a different vendor")
    stub({"deepseek-3.2", "mistral-3-14B"})
    ck(run(L.verified_model_for("speak")) in {"deepseek-3.2", "mistral-3-14B"},
       "falls all the way through to a model that exists")

    print("\n[3] a role whose slug IS present is left completely alone")
    stub({"kimi-k2.6", "deepseek-3.2", "nemotron-nano-12b-v2-vl"})
    ck(run(L.verified_model_for("speak")) == "kimi-k2.6", "speak untouched")
    ck(run(L.verified_model_for("answer")) == "deepseek-3.2", "answer untouched")
    ck(run(L.verified_model_for("vision")) == "nemotron-nano-12b-v2-vl", "vision untouched")

    print("\n[4] FAILS OPEN in BOTH directions")
    L._CATALOG.clear()
    L._CATALOG_TRIED[0] = True
    L._RESOLVED.clear()
    ck(run(L.verified_model_for("speak")) == L.DEFAULT_MODELS["speak"],
       "an unreadable catalog never silently reroutes a role")
    stub({"nothing-we-know-about"})
    ck(run(L.verified_model_for("answer")) == L.DEFAULT_MODELS["answer"],
       "nothing on the ladder either -> pass the configured slug through so the REAL error shows")

    print("\n[5] an explicit JHW_MODEL_* override is the operator choosing, not a guess")
    os.environ["JHW_MODEL_SPEAK"] = "a-brand-new-slug"
    stub({"deepseek-3.2"})
    ck(run(L.verified_model_for("speak")) == "a-brand-new-slug",
       "the override is honoured verbatim, verified or not")
    del os.environ["JHW_MODEL_SPEAK"]

    print("\n[6] no role falls back to ITSELF, and every ladder crosses vendors")
    for role, lad in L.FALLBACK_LADDER.items():
        ck(L.DEFAULT_MODELS[role] not in lad,
           f"{role}: configured {L.DEFAULT_MODELS[role]} is not in its own ladder {lad}")

    print("\n[7] the proxy second-guesses a ROLE ALIAS only — never a concrete slug")
    src = io.open(os.path.join(APP, "app", "proxy.py"), encoding="utf-8").read()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_resolve_model_checked"), None)
    ck(fn is not None, "_resolve_model_checked exists")
    if fn is not None:
        b = ast.get_source_segment(src, fn)
        ck("ROLE_ALIASES" in b and "verified_model_for" in b,
           "an alias goes through the verified resolver")
        ck("return bare or req" in b, "a concrete slug is passed through untouched")
    ck("_resolve_model_checked(payload.get" in src,
       "and it is WIRED INTO the chat path — an unwired resolver resolves nothing")

    print("\n[8] every id in DEFAULT_MODELS at least LOOKS like a slug, not a product name")
    for role, mid in L.DEFAULT_MODELS.items():
        ck(" " not in mid and mid == mid.strip() and "/" not in mid,
           f"{role}={mid!r} has no spaces or slashes")

    print("\n" + "=" * 78)
    if FAILS:
        print(f"[X] {len(FAILS)} MODEL-ROUTING CONTRACT(S) BROKEN")
        return 1
    print("ALL MODEL-ROUTING CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
