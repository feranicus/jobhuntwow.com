/**
 * JHW Stagehand sidecar — the AI layer of the apply stack.
 *
 * The Python Playwright adapters (built from real recordings) do the deterministic 80%.
 * This service is called ONLY when a selector breaks or the ATS is unknown: it attaches to the
 * SAME Chrome over CDP (so it drives the browser the adapters are already using), and exposes
 * Stagehand's primitives over plain HTTP so Python can call them.
 *
 * LLM = our DO proxy (OpenAI-compatible) -> only for the fuzzy bits.
 */
import http from "node:http";
import { Stagehand } from "@browserbasehq/stagehand";
import { chromium } from "playwright";

const CDP  = process.env.CDP_URL ?? "http://127.0.0.1:9222";
const MODEL = process.env.MODEL_NAME ?? "openai-gpt-oss-120b";
const BASE = process.env.OPENAI_BASE_URL ?? "http://host.docker.internal:8000/v1";
const KEY  = process.env.OPENAI_API_KEY ?? "";
const PORT = Number(process.env.PORT ?? 8081);

let sh: any = null;
async function stage() {
  if (sh) return sh;
  // A custom OpenAI-COMPATIBLE endpoint needs the PROVIDER-PREFIXED name ("openai/<model>"):
  // Stagehand maps the prefix to a provider and only then honours baseURL. A bare "jhw-fast"
  // cannot be mapped, which is what produced the opaque 500 on /goto.
  // Chrome's CDP WebSocket is NOT at the root: it is ws://host:9222/devtools/browser/<uuid>, and
  // the uuid changes on every Chrome start. Handing over "http://127.0.0.1:9222" makes the ws
  // client upgrade "/" -> Chrome answers 404 ("Unexpected server response: 404"). Ask
  // /json/version for the real endpoint first.
  let cdpTarget = CDP;
  try {
    const vr = await fetch(CDP.replace(/\/$/, "") + "/json/version");
    const vj: any = await vr.json();
    if (vj?.webSocketDebuggerUrl) {
      cdpTarget = vj.webSocketDebuggerUrl;
      console.log("[stagehand-svc] cdp ws resolved: %s (%s)", cdpTarget, vj.Browser || "?");
    } else {
      console.warn("[stagehand-svc] /json/version had no webSocketDebuggerUrl:", JSON.stringify(vj).slice(0, 200));
    }
  } catch (e: any) {
    console.error("[stagehand-svc] cannot reach CDP at %s : %s", CDP, e?.message || e);
    throw new Error(`CDP unreachable at ${CDP}: ${e?.message || e}`);
  }

  const qualified = MODEL.includes("/") ? MODEL : `openai/${MODEL}`;
  const cfg: any = {
    env: "LOCAL",
    localBrowserLaunchOptions: { cdpUrl: cdpTarget },   // resolved ws://.../devtools/browser/<uuid>
    // v3 shape:
    model: { modelName: qualified, apiKey: KEY, baseURL: BASE },
    // v2 shape (harmless on v3, required on v2):
    modelName: qualified,
    modelClientOptions: { baseURL: BASE, apiKey: KEY },
    verbose: 2,
  };
  console.log("[stagehand-svc] init model=%s base=%s cdp=%s", qualified, BASE, cdpTarget);
  sh = new Stagehand(cfg);
  try {
    await sh.init();
  } catch (e: any) {
    sh = null;                                    // never cache a half-built instance
    console.error("[stagehand-svc] INIT FAILED:", e?.message || e, "\n", e?.stack || "");
    throw e;
  }
  console.log("[stagehand-svc] init OK");
  return sh;
}

/** Playwright over the same CDP — used for file upload (LLMs cannot drive a native file picker). */
async function withPage(fn: (page: any) => Promise<any>) {
  const browser = await chromium.connectOverCDP(CDP);
  try {
    const ctx = browser.contexts()[0] ?? (await browser.newContext());
    const page = ctx.pages()[0] ?? (await ctx.newPage());
    return await fn(page);
  } finally {
    await browser.close();
  }
}

// Stagehand v3 does NOT expose `stagehand.page` (that is the v2 API). The docs show:
//     const page = stagehand.context.pages()[0]
// Using s.page on v3.7 gives "Cannot read properties of undefined (reading 'goto')".
// Try v3 first, then v2, then open a tab if the context has none.
async function pageOf(s: any) {
  const ctx = s?.context;
  if (ctx?.pages) {
    const ps = ctx.pages();
    if (ps?.length) return ps[0];
    if (ctx.newPage) return await ctx.newPage();
  }
  if (s?.page) return s.page;                      // v2 fallback
  throw new Error("no page: stagehand.context.pages() empty and stagehand.page undefined");
}

const routes: Record<string, (b: any) => Promise<any>> = {
  "/health": async () => ({ ok: true, cdp: CDP, model: MODEL }),

  "/goto": async (b) => {
    const s = await stage();
    const page = await pageOf(s);
    await page.goto(b.url, { waitUntil: "domcontentloaded" });
    return { ok: true, url: b.url };
  },

  // Find candidate actions without acting -> Python can cache the returned selector.
  "/observe": async (b) => {
    const s = await stage();
    const actions = await (await pageOf(s)).observe(b.instruction);
    return { ok: true, actions };
  },

  // Act from natural language OR replay a previously observed action object (no re-inference).
  "/act": async (b) => {
    const s = await stage();
    const r = await (await pageOf(s)).act(b.action ?? b.instruction);
    return { ok: true, result: r };
  },

  "/extract": async (b) => {
    const s = await stage();
    const r = b.schema
      ? await (await pageOf(s)).extract(b.instruction, b.schema)
      : await (await pageOf(s)).extract(b.instruction);
    return { ok: true, result: r };
  },

  // Autonomous multi-step: used for a whole unknown form, guided by SKILL.md + candidate_profile.md.
  "/agent": async (b) => {
    const s = await stage();
    const agent = s.agent({ instructions: b.system ?? undefined });
    const r = await agent.execute({ instruction: b.instruction, maxSteps: b.maxSteps ?? 25 });
    return { ok: true, result: r };
  },

  // Resume upload — the piece no LLM can do. Plain Playwright on the same browser.
  "/upload": async (b) => withPage(async (page) => {
    const sel = b.selector ?? 'input[type="file"]';
    await page.locator(sel).first().setInputFiles(b.path);
    return { ok: true, uploaded: b.path, selector: sel };
  }),
};

http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    const fn = routes[(req.url ?? "").split("?")[0]];
    res.setHeader("Content-Type", "application/json");
    if (!fn) { res.statusCode = 404; return res.end(JSON.stringify({ ok: false, error: "no route" })); }
    // OBSERVABILITY: one structured line per call (route, ms, ok) + the FULL stack on failure.
    // A bare `{"error": "..."}` 500 told the caller nothing about WHY, which cost a whole cycle.
    const t0 = Date.now();
    const path = (req.url ?? "").split("?")[0];
    try {
      const out = await fn(body ? JSON.parse(body) : {});
      console.log(JSON.stringify({ evt: "stagehand_call", route: path, ok: true,
                                   ms: Date.now() - t0, service: "jhw-stagehand" }));
      res.end(JSON.stringify(out));
    } catch (e: any) {
      const msg = String(e?.message ?? e);
      console.error(JSON.stringify({ evt: "stagehand_call", route: path, ok: false,
                                     ms: Date.now() - t0, err: msg.slice(0, 400),
                                     service: "jhw-stagehand" }));
      console.error("[stagehand-svc] STACK for " + path + ":\n" + (e?.stack || "(none)"));
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, route: path, error: msg,
                              stack: String(e?.stack || "").split("\n").slice(0, 6).join(" | ") }));
    }
  });
}).listen(PORT, "0.0.0.0", () => console.log(`[stagehand-svc] listening :${PORT} cdp=${CDP} model=${MODEL}`));
