#!/usr/bin/env python3
"""JobHuntWOW candidate agent — Cassandra-style (modelled on cybergod.ai's cassandra_bot.py).

NARROW BY DESIGN. It does exactly four things and nothing else:
  1. /intake   interview the candidate to fill the gaps in candidate.md (asks only what's missing)
  2. /resume   write a tailored resume for a given job description
  3. /cover    write a tailored cover letter for that job
  4. /profile  (re)build candidate.md from the resume + answers and send it back as an artifact

Security posture (why this replaced Hermes): NO browser, NO terminal, NO computer-use, no host
filesystem beyond its own out/ dir, and it serves exactly ONE Telegram chat. The apply engine is a
separate local container that only READS the artifacts this bot produces.

Stdlib + httpx only. Single Telegram poller.
"""
from __future__ import annotations
import os, re, json, time, glob, traceback
import httpx

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT    = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
PROXY   = os.getenv("JHW_PROXY_BASE", "http://host.docker.internal:8000/v1").rstrip("/")
PTOK    = os.getenv("AGENT_PROXY_TOKEN", "none")
OUT     = os.getenv("JHW_OUT_DIR", "/agent/out")
PROFILE = os.getenv("JHW_PROFILE", "/data/candidate.md")
MODELS  = [m.strip() for m in os.getenv("JHW_BOT_MODELS", "deepseek-v4-pro,deepseek-3.2,llama-4-maverick").split(",") if m.strip()]
API     = f"https://api.telegram.org/bot{TOKEN}"
MAXTOK  = int(os.getenv("JHW_BOT_MAX_TOKENS", "3000"))

CONVO: dict[str, list] = {}
STATE: dict = {"mode": None, "gap": None, "jd": ""}    # /intake + /resume flow state

SYSTEM = """You are the JobHuntWOW candidate agent. You help ONE job seeker with exactly three things:
writing an excellent resume, writing an excellent cover letter, and maintaining their candidate
profile (candidate.md) that an automated apply-engine uses to fill job application forms.

HARD RULES:
1. NEVER invent facts about the candidate — no fabricated employers, dates, degrees, titles, metrics
   or salaries. Use ONLY the profile and what the candidate tells you. If something is missing, ASK.
2. Write like a strong human career writer: concrete, specific, achievement-led, no fluff, no
   buzzword soup, no "results-driven professional". Every bullet carries a fact, number or outcome.
3. Mirror the job description's real requirements and language (ATS keyword alignment) WITHOUT lying.
4. Keep resumes ATS-safe: plain structure, standard section headings, no tables/columns/graphics.
5. Be brief in chat; put the substance in the artifact."""


# ----------------------------- plumbing -----------------------------
def _llm(messages: list, max_tokens: int = MAXTOK) -> str:
    last = "unknown error"
    for model in MODELS:
        for attempt in range(2):
            try:
                with httpx.Client(timeout=180) as c:
                    r = c.post(f"{PROXY}/chat/completions",
                               headers={"Authorization": f"Bearer {PTOK}", "Content-Type": "application/json"},
                               json={"model": model, "messages": messages,
                                     "temperature": 0.4, "max_tokens": max_tokens})
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1)); last = f"429 {model}"; continue
                r.raise_for_status()
                txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
                if txt:
                    return txt
                last = f"empty from {model}"
            except Exception as e:
                last = f"{type(e).__name__} {model}: {str(e)[:70]}"
                time.sleep(1.0 * (attempt + 1))
    return f"⚠ Model unavailable (tried {', '.join(MODELS)}). Last: {last}"


def _send(text: str) -> None:
    for i in range(0, max(len(text), 1), 3900):
        try:
            with httpx.Client(timeout=25) as c:
                c.get(f"{API}/sendMessage", params={"chat_id": CHAT, "text": text[i:i+3900] or " "})
        except Exception as e:
            print(f"[bot] send failed: {e}", flush=True)


def _send_doc(path: str, caption: str = "") -> None:
    """Deliver an artifact to the candidate as a real file."""
    try:
        with httpx.Client(timeout=60) as c:
            with open(path, "rb") as fh:
                c.post(f"{API}/sendDocument",
                       data={"chat_id": CHAT, "caption": caption[:900]},
                       files={"document": (os.path.basename(path), fh)})
    except Exception as e:
        print(f"[bot] sendDocument failed: {e}", flush=True)
        _send(f"(couldn't attach {os.path.basename(path)}: {e})")


def _profile() -> str:
    # live (updated) copy first, then the read-only seed shipped in userdata/
    for p in (os.path.join(OUT, "candidate.md"), PROFILE):
        try:
            return open(p, encoding="utf-8").read()
        except Exception:
            continue
    return ""


def _write(name: str, text: str) -> str:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    open(path, "w", encoding="utf-8").write(text)
    return path


def _gaps(profile: str) -> list:
    """Fields the profile explicitly marks unknown -> the only things we should ask about."""
    return [ln.strip("- ").split(":")[0].strip()
            for ln in profile.splitlines() if re.search(r":\s*ASK\s*$", ln)]


# ----------------------------- the four jobs -----------------------------
def do_profile() -> None:
    prof = _profile()
    if not prof:
        _send("I don't have a candidate.md yet. Send me your resume (PDF/text) or run /intake and I'll build one.")
        return
    path = _write("candidate.md", prof)
    missing = _gaps(prof)
    _send_doc(path, "Your candidate profile — the apply engine reads this for every application.")
    _send(("Still missing: " + ", ".join(missing) + "\nRun /intake and I'll fill them.") if missing
          else "Profile is complete — no gaps.")


def do_intake() -> None:
    prof = _profile()
    missing = _gaps(prof)
    if not missing:
        _send("No gaps — your profile is complete. Use /resume <job description> when you're ready.")
        STATE["mode"] = None
        return
    STATE["mode"], STATE["gap"] = "intake", missing[0]
    _send(f"Filling profile gaps ({len(missing)} left).\n\n**{missing[0]}** — what should I put here?")


def _apply_answer(field: str, answer: str) -> None:
    """Persist the candidate's answer into candidate.md so it is never asked again."""
    prof = _profile()
    out, done = [], False
    for ln in prof.splitlines():
        if not done and re.match(rf"^\s*-\s*{re.escape(field)}\s*:\s*ASK\s*$", ln):
            out.append(f"- {field}: {answer}"); done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"- {field}: {answer}")
    _write("candidate.md", "\n".join(out))


def do_resume(jd: str) -> None:
    prof = _profile()
    if not prof:
        _send("I need your profile first — run /profile or send me your resume."); return
    _send("Writing your tailored resume…")
    md = _llm([{"role": "system", "content": SYSTEM},
               {"role": "user", "content":
                f"Write a tailored, ATS-safe resume in Markdown for this job.\n\n"
                f"CANDIDATE PROFILE (the only facts you may use):\n{prof[:9000]}\n\n"
                f"JOB DESCRIPTION:\n{jd[:6000]}\n\n"
                "Rules: reorder and re-weight the candidate's REAL experience to match this JD; mirror its "
                "language where truthful; lead every bullet with an achievement and a concrete fact/number; "
                "one page unless the seniority justifies two. Output ONLY the resume Markdown."}])
    path = _write("resume_tailored.md", md)
    _send_doc(path, "Tailored resume — drop it in the apply engine's out/ folder.")


def do_cover(jd: str) -> None:
    prof = _profile()
    if not prof:
        _send("I need your profile first — run /profile."); return
    _send("Writing your cover letter…")
    md = _llm([{"role": "system", "content": SYSTEM},
               {"role": "user", "content":
                f"Write a tailored cover letter in Markdown.\n\nCANDIDATE PROFILE:\n{prof[:9000]}\n\n"
                f"JOB DESCRIPTION:\n{jd[:6000]}\n\n"
                "Rules: max ~320 words, specific to THIS role and company, opens with why them (not 'I am "
                "writing to apply'), 2-3 concrete proof points from the candidate's real history, closes with "
                "a clear ask. No invented facts. Output ONLY the letter."}], max_tokens=1400)
    path = _write("cover_letter_tailored.md", md)
    _send_doc(path, "Tailored cover letter.")


# ----------------------------- dispatch -----------------------------
def _handle(text: str) -> None:
    t = text.strip()

    if STATE.get("mode") == "intake" and not t.startswith("/"):
        _apply_answer(STATE["gap"], t)
        _send(f"✓ saved {STATE['gap']}")
        do_intake(); return

    if STATE.get("mode") in ("resume", "cover") and not t.startswith("/"):
        mode = STATE["mode"]; STATE["mode"] = None
        (do_resume if mode == "resume" else do_cover)(t); return

    if t in ("/start", "/help"):
        _send("JobHuntWOW candidate agent. I do four things:\n\n"
              "/intake — I ask only what's missing and fill your profile\n"
              "/resume — send the job description, I write a tailored resume\n"
              "/cover — same, for a cover letter\n"
              "/profile — send you your candidate.md\n\n"
              "The artifacts go into the local apply engine, which does the actual applying."); return
    if t == "/profile": do_profile(); return
    if t == "/intake":  do_intake();  return
    if t.startswith("/resume"):
        jd = t[len("/resume"):].strip()
        if jd: do_resume(jd)
        else: STATE["mode"] = "resume"; _send("Paste the job description.")
        return
    if t.startswith("/cover"):
        jd = t[len("/cover"):].strip()
        if jd: do_cover(jd)
        else: STATE["mode"] = "cover"; _send("Paste the job description.")
        return

    hist = CONVO.setdefault(CHAT, [])
    hist.append({"role": "user", "content": t}); CONVO[CHAT] = hist[-10:]
    reply = _llm([{"role": "system", "content": SYSTEM + "\n\nPROFILE:\n" + _profile()[:6000]}] + CONVO[CHAT],
                 max_tokens=900)
    CONVO[CHAT].append({"role": "assistant", "content": reply}); CONVO[CHAT] = CONVO[CHAT][-10:]
    _send(reply)


def main() -> None:
    if not TOKEN or not CHAT:
        print("[bot] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — idle.", flush=True); return
    os.makedirs(OUT, exist_ok=True)
    print(f"[bot] candidate agent up. chat={CHAT} models={MODELS}", flush=True)
    offset = 0
    try:
        with httpx.Client(timeout=15) as c:
            for u in c.get(f"{API}/getUpdates", params={"timeout": 0}).json().get("result", []):
                offset = max(offset, u["update_id"] + 1)
    except Exception:
        pass
    _send("👋 Candidate agent online. /help for what I do.")
    while True:
        try:
            with httpx.Client(timeout=40) as c:
                rr = c.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 25}).json()
        except Exception:
            time.sleep(2); continue
        for u in rr.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message") or {}
            if str(m.get("chat", {}).get("id")) != CHAT:
                continue
            txt = m.get("text")
            if not txt:
                continue
            try:
                _handle(txt)
            except Exception:
                print("[bot] handler error:\n" + traceback.format_exc(), flush=True)
                _send("⚠ internal error — logged.")


if __name__ == "__main__":
    main()
