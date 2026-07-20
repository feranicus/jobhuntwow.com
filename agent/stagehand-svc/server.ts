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
  sh = new Stagehand({
    env: "LOCAL",
    localBrowserLaunchOptions: { cdpUrl: CDP },   // attach to the ALREADY-RUNNING Chrome
    modelName: MODEL,
    modelClientOptions: { baseURL: BASE, apiKey: KEY },
    verbose: 1,
  } as any);
  await sh.init();
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

const routes: Record<string, (b: any) => Promise<any>> = {
  "/health": async () => ({ ok: true, cdp: CDP, model: MODEL }),

  "/goto": async (b) => {
    const s = await stage();
    await s.page.goto(b.url);
    return { ok: true, url: b.url };
  },

  // Find candidate actions without acting -> Python can cache the returned selector.
  "/observe": async (b) => {
    const s = await stage();
    const actions = await s.page.observe(b.instruction);
    return { ok: true, actions };
  },

  // Act from natural language OR replay a previously observed action object (no re-inference).
  "/act": async (b) => {
    const s = await stage();
    const r = await s.page.act(b.action ?? b.instruction);
    return { ok: true, result: r };
  },

  "/extract": async (b) => {
    const s = await stage();
    const r = b.schema
      ? await s.page.extract(b.instruction, b.schema)
      : await s.page.extract(b.instruction);
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
    try {
      const out = await fn(body ? JSON.parse(body) : {});
      res.end(JSON.stringify(out));
    } catch (e: any) {
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: String(e?.message ?? e) }));
    }
  });
}).listen(PORT, "0.0.0.0", () => console.log(`[stagehand-svc] listening :${PORT} cdp=${CDP} model=${MODEL}`));
