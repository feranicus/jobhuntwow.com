# ATS Automation — Deep Research Findings (2026-07-14)

Verified against primary sources: official docs, Playwright's own test suite, peer-reviewed benchmarks,
and **real working code in 4 independent Workday repos**. Marketing claims explicitly rejected.

---

## THE VERDICT IN ONE LINE

**Deterministic Playwright + a per-tenant selector map is the ONLY approach with a working track record.
LLM browser agents are the worst-performing category on exactly our task (form filling). Stop chasing them.**

---

## 1. The evidence that settles the LLM-agent question

| Claim | Evidence | Source |
|---|---|---|
| LLM agents are WORST at form-filling ("write") tasks | Skyvern: **64.4%** on WebBench vs 85.8% WebVoyager — same agent, 21pt inflation. "**All agents performed surprisingly poorly on write-heavy tasks.**" Read is "largely a solved problem"; write is where "the real gap opens up" | [Skyvern WebBench](https://www.skyvern.com/blog/web-bench-a-new-way-to-compare-ai-browser-agents/) |
| browser-use's "89.1% SOTA" is invalid for our case — **by their own admission** | "We **removed 55 tasks**"… "we **manually reviewed and corrected** the evaluations"… "**the eval model is not good**"… "**it's not actually testing correct things**… complex sites with iFrames and Shadow elements are extremely tricky, **but not tested**" | [browser-use SOTA report](https://browser-use.com/posts/sota-technical-report) |
| Real-world agent success is ~61%, not ~90% | COLM 2025 "An Illusion of Progress?" — OpenAI **Operator tops at 61%** on 300 live tasks; results show "**over-optimism in previously reported results**" | [arXiv 2504.01382](https://arxiv.org/abs/2504.01382) |
| **OpenAI retreated from pure vision agents** | Operator discontinued ~8 months after launch, folded into Agent Mode with a *text/DOM* browser added. "**The shift reflected a hard-earned truth: computer-use models don't yet work reliably enough in production.**" | [InfoWorld](https://www.infoworld.com/article/4081396/when-will-browser-agents-do-real-work.html) |
| LLM form-filling is unusably slow | Skyvern issue: "**5 to 6 input fields… taking around 4–5 minutes end-to-end**" for job application forms. Cause: "**LLM-in-the-loop decision making for each small interaction instead of a compiled action plan**" vs "Playwright + heuristics, which complete similar tasks in **seconds**" | [Skyvern #4439](https://github.com/Skyvern-AI/skyvern/issues/4439) |
| browser-use's real failure mode = **infinite loops that bill you** | Tracked issues: #191 (endless loop cost), #784 (`max_failures` doesn't fire), #1587 (empty actions), #951, #1971, #3615 | [browser-use issues](https://github.com/browser-use/browser-use/issues/784) |
| The #1 write failure = **hallucinated success** | Agents "optimistically assume that clicking Submit completed the task when in reality a captcha appeared" → *user believes they applied and did not* | Skyvern WebBench |

### page-agent: architecturally wrong for third-party ATS — confirmed by its own docs
> "Sites with **strict Content-Security-Policy may refuse to load the CDN script or disallow inline eval**"
> "**Best fit: copilots and form-filling inside apps you own, not external or locked-down sites.**"

It is designed to put a copilot in **your own** app, where you control the CSP. It cannot drive Workday.
**Decision: drop page-agent for ATS. Not a fixable gap — a design boundary.**

### What teams that ship actually do
Stagehand's own pitch names our exact trap: *"Too brittle (selectors break) / **Too agentic** (AI agents are
unpredictable and impossible to debug)"* → sell the middle. InfoWorld's recommended architecture:
**explore once with the LLM, compile to a deterministic script, replay forever.** There are only ~6 ATS
vendors — **you pay the LLM once per template, not once per application.**

---

## 2. Workday hard facts

**There is NO apply API. Browser automation is mandatory.**
- Workday REST/SOAP (`Put_Candidate`) is **tenant-scoped** — OAuth creds issued by a Workday *customer* to
  its own integrations. Not obtainable by a candidate "at any price."
- `POST /wday/cxs/{tenant}/{site}/jobs` returns structured JSON — **READ-ONLY**. No `/apply` endpoint.
  → **Use CXS JSON for Job Scout (no browser). Use browser only for apply.** Clean architecture split.
- Workday Extend "doesn't support external accounts including candidates." Apply-with-LinkedIn is an
  employer-enabled prefill, not a programmatic submit channel.

**CSP is a NON-ISSUE for us — proven by Playwright's own test suite.**
`page.evaluate` runs via CDP `Runtime.evaluate`, *outside* CSP jurisdiction; only `addScriptTag` gets
blocked. So our Playwright driver is immune regardless of Workday's headers. **Do NOT set `bypass_csp`.**
([test suite](https://raw.githubusercontent.com/microsoft/playwright/main/tests/library/browsercontext-csp.spec.ts))

**`set_input_files` has ZERO actionability checks** — the only input action in Playwright's matrix with no
Visible/Stable/Enabled requirement. Hidden file inputs behind drop-zones are the *documented, sanctioned*
path. No drag-and-drop simulation needed. ([actionability](https://playwright.dev/docs/actionability))

**Workday does NOT use shadow DOM.** Canvas Kit is React + Emotion (CSS-in-JS). The churn people mistake
for shadow DOM is *hashed class names* (`css-1x2y3z`) — never write CSS structural chains.

**Anti-bot: not our threat model.** None of the working apply repos use proxies/stealth/UA-rotation.
Real Chrome + real residential IP + non-headless + human pace = what they all do. Our CDP-to-real-Chrome
setup is already the correct answer.

**⚠️ ToS:** Workday's terms prohibit "automated software, scripts, or other methods of accessing the
Website." This is a business/legal call, not an engineering one. Every working repo **stops before submit** —
which is already our standing rule.

---

## 3. Selector ground truth — and why OUR dump beats the repos

Cross-verified real code: [ubangura](https://github.com/ubangura/Workday-Application-Automator/blob/master/apply.js) (Puppeteer, 72★) ·
[raghuboosetty](https://github.com/raghuboosetty/workday/blob/master/workday.py) (Selenium) ·
[ahdibiaymen](https://github.com/ahdibiaymen/workday-application-automation/blob/main/app.py) (Selenium) ·
[jasonchen270](https://github.com/jasonchen270/workday-autofill) (Python+Playwright+CDP — same architecture as ours)

**CRITICAL: tenants run different Workday UI versions. Selectors MUST be fallback chains.**
Proof: two working repos use *different* sign-in submit selectors —
`button[data-automation-id="signInSubmitButton"]` vs `div[role=button][data-automation-id="click_filter"]`.

**Red Hat runs a NEWER UI than those repos.** Our own `out/wd_form.json` dump proves it:

| Element | Repos (older UI) | **Red Hat (our dump — authoritative)** |
|---|---|---|
| Next | `bottom-navigation-next-button` | **`pageFooterNextButton`** (text "Save and Continue") |
| Upload | `file-upload-input-ref` | **`file-upload-input-ref`** ✅ + `file-upload-drop-zone`, `select-files` |
| Add experience | `Add` / `Add Another` | **`add-button`** |
| Page marker | `myExperiencePage` | **`applyFlowMyExpPage`** |

→ Keep **both** in the chain. Our `S["next"]` already has both — so **Next was not the bug.**

**Confirmed-good (2–3 repos agree):** `file-upload-input-ref`, `legalNameSection_firstName/lastName`,
`addressSection_addressLine1/city/postalCode`, `phone-number`, `phone-device-type`, `applyManually`,
`email`/`password`/`verifyPassword`, `agreementCheckbox`, `delete-file`, `errorBanner`.

**Never copy:** ubangura hardcodes tenant WIDs (`input[id="64cbff5f364f10000ae7a421cf210000"]`) — instance-specific.

**Dropdowns:** `button[aria-haspopup="listbox"]` is real. `[role=option]` is **NOT** — options are
`div[data-automation-id="promptOption"][data-automation-label="LinkedIn"]` or matched by exact text.

---

## 4. 🔴 THE ACTUAL BUG (why we stalled on My Experience)

> "raghuboosetty sleeps **10s** after upload — **Workday parses the resume SERVER-SIDE and may overwrite
> fields you already filled.** Upload *before* filling, or re-verify after."
> ubangura clicks `delete-file` first or uploads **stack duplicates**.

Our driver uploads then clicks "Save and Continue" **immediately**. Workday is still parsing → the
required-file validation hasn't cleared → error persists → we never advance → 9 empty loops → `stage=paused`.

**Fix = 3 lines:** upload → wait for the parse to complete (file chip appears / `errorBanner` clears) → then Next.

---

## 5. TOP 5 — ranked by fewest iterations to working

| # | Approach | Technology | Why | Proven in |
|---|---|---|---|---|
| **1** | **Fix upload ordering + wait for server-side resume parse** | Playwright `set_input_files` + wait-for-condition (not `sleep`) | This is *the* current blocker. ~3 lines. | raghuboosetty (10s sleep), ubangura (`delete-file` first) |
| **2** | **Deep-link straight to `/apply/applyManually`** | Playwright `goto` | Skips `adventureButton` — which ubangura had to **click twice** (flaky). Workday bounces to auth then returns. | ahdibiaymen hardcodes this as entry point |
| **3** | **Selector map as DATA with fallback chains** (not inline strings) | YAML/JSON + Playwright locators; priority `data-automation-id` → `getByRole`/`getByLabel` → text | Tenants diverge (`signInSubmitButton` vs `click_filter`; `pageFooterNextButton` vs `bottom-navigation-next-button`). Single selectors silently fail. | All 4 repos; our own dump proves version drift |
| **4** | **Selector = runtime assertion → halt & report, never guess** | Playwright + our Telegram `ask_human` | Kills "hallucinated success," the #1 write failure mode. Matches our standing rule "LLM assists, does not decide side effects." | Every repo stops at Review |
| **5** | **LLM ONLY for free-text** (screening answers, cover letter) — never for driving | Our `llm_answer` via DO proxy (text-only, no vision) | Skyvern 64.4% on write; browser-use benchmark invalid by own admission; OpenAI killed Operator. Explore-once → compile-to-script. | Stagehand's entire product thesis; InfoWorld's recommended architecture |

**Explicitly REJECTED:** browser-use (infinite-loop billing, invalid benchmark, needs vision) · page-agent
for ATS (own docs: "apps you own, not external or locked-down sites") · Cypress (multi-tab is a
**permanent** trade-off per its own docs — disqualifies LinkedIn→Workday) · Selenium (you rebuild
auto-waiting/hit-testing by hand) · any "90% WebVoyager" claim as evidence of form capability.

**KEEP:** Playwright + CDP to real Chrome (exactly what jasonchen270 independently converged on),
Python, our DO proxy for text-only LLM, Telegram human-gate, stop-at-Review.

---

## 6. Architecture (confirmed correct)

```
Job Scout   ->  Workday CXS JSON  (POST /wday/cxs/{tenant}/{site}/jobs)   NO browser
Tailor      ->  LLM (text)        resume + cover letter
Apply       ->  Playwright + CDP + per-tenant selector map                browser mandatory
Free text   ->  LLM (text-only)   screening answers
Unknown     ->  Telegram ask_human -> halt, never guess
Submit      ->  HUMAN. Always.
```

**Open questions:** (a) Workday live CSP header (moot per §2); (b) is Akamai bot-management on the apply
flow real? — single vendor source with a conflict of interest; the 4 working repos use zero stealth, which
argues no.
