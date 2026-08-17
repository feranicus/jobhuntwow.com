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


ASK_DIR = os.getenv("JHW_ASK_DIR", "/agent/out/asks")


def _asks():
    """Pending questions dropped by the apply engine (ask.py bridge mode), oldest first."""
    try:
        out = []
        for f in sorted(os.listdir(ASK_DIR)):
            # `_`-prefixed files are the bot's OWN state, not questions. _volunteered.json used to
            # be read as an ask, and because it holds a LIST the `.get()` below raised
            # AttributeError -- swallowed by the outer except into `return []`, which silently
            # stopped EVERY apply question from being relayed. One bad file must not blind the
            # bridge, so the loop is also per-file fault-tolerant now.
            if not f.endswith(".json") or f.startswith("_"):
                continue
            p = os.path.join(ASK_DIR, f)
            try:
                d = json.load(open(p, encoding="utf-8"))
                if not isinstance(d, dict):
                    continue
                if not d.get("answered"):
                    out.append((p, d))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _pump_asks() -> None:
    """Relay unsent apply-engine questions into Telegram.

    THE BOT IS THE ONLY PROCESS THAT MAY POLL getUpdates. Telegram allows a single consumer per
    token: when the agent polled getUpdates itself (JHW_ASK_CHANNEL=auto -> 'telegram') the two
    pollers stole each other's messages, which is why replies went missing. The agent now only
    writes a question file and waits; this relays it and writes the answer back.
    """
    for path, d in _asks():
        if not d.get("sent"):
            _send("🤖 APPLY ▸ " + str(d.get("q", ""))[:3500] +
                  "\n\nReply to this message and I'll pass it straight to the apply engine. "
                  "(/cancel to drop it)")
            print(f"[bot] relayed apply question {os.path.basename(path)} to Telegram", flush=True)
            d["sent"] = True
            try:
                json.dump(d, open(path, "w", encoding="utf-8"))
            except Exception:
                pass
            # He may have ALREADY sent this (an answer arriving in pieces). Use it rather than
            # making him type it twice -- the whole point of a 24/7 agent.
            vol = _vol_take(str(d.get("q") or ""))
            if vol:
                d["answer"], d["answered"] = vol["text"], True
                try:
                    json.dump(d, open(path, "w", encoding="utf-8"))
                except Exception:
                    pass
                shown = _redact(vol["text"]) if vol.get("secret") else vol["text"][:60]
                _send(f"↩ Used what you already sent: {shown}")
                print(f"[bot] auto-answered from the volunteered store ({'secret' if vol.get('secret') else 'plain'})",
                      flush=True)


# ---------------------------------------------------------------------------------------------
# VOLUNTEERED ANSWERS + THE CREDENTIAL GUARD
#
# MEASURED 2026-08-16, from the candidate's own Telegram transcript. The apply engine asked for the
# Workday password. He answered in THREE messages:
#     "Use google sso"                  -> consumed correctly, "Passed to the apply engine"
#     "Username : feranicus@s4biz.io"   -> fell through to the CHAT model
#     "Password : @Q4sD9i..."           -> fell through to the CHAT model
# The ask was already closed by the first reply, so messages 2 and 3 hit _handle() and the assistant
# replied with chit-chat about profile e-mails and "please don't paste passwords here" — while the
# apply engine sat at the gate and then gave up. Two defects in one exchange:
#   1. AN ANSWER ARRIVING IN PIECES IS STILL AN ANSWER. A follow-up window keeps the next lines for
#      the engine instead of handing them to a chatbot.
#   2. A CREDENTIAL MUST NEVER REACH THE CHAT MODEL. It is a third-party inference endpoint and the
#      reply is logged in the chat. It is now refused before _handle() is ever called.
_VOL = os.path.join(ASK_DIR, "_volunteered.json")
_FOLLOWUP_S = int(os.environ.get("JHW_FOLLOWUP_WINDOW", "600"))
_LAST_ANSWER = [0.0]

_SECRETISH = re.compile(
    r"\bpass\s*word\b|\bpasswort\b|\bpwd\b|\bsecret\b|\btoken\b|\bapi[\s_-]?key\b|"
    r"\bcredential", re.I)


def _looks_secret(t: str) -> bool:
    """Is this message carrying a credential? Conservative on BOTH sides: a false positive only
       costs a chat reply, a false negative ships a password to an inference endpoint."""
    if _SECRETISH.search(t or ""):
        return True
    # A bare high-entropy string with no spaces is almost certainly a generated password.
    # BUT A URL AND AN E-MAIL FIT THAT DESCRIPTION TOO, and he pastes job links constantly -- my own
    # test caught this classifying a myworkdayjobs.com URL as a credential, which would have refused
    # to chat about every job he ever sends. Exclude them by SHAPE before measuring entropy.
    x = (t or "").strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://", x, re.I) or x.lower().startswith("www."):
        return False                                        # a URL
    if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", x, re.I):
        return False                                        # an e-mail address
    if re.match(r"^[\w.-]+\.[a-z]{2,24}(/\S*)?$", x, re.I):
        return False                                        # a bare host / path
    return (12 <= len(x) <= 120 and " " not in x
            and bool(re.search(r"[A-Z]", x)) and bool(re.search(r"[a-z]", x))
            and bool(re.search(r"\d", x)) and bool(re.search(r"[^A-Za-z0-9]", x)))


def _redact(t: str) -> str:
    return re.sub(r"\S", "*", (t or ""))[:12] + " (redacted)"


def _vol_load() -> list:
    try:
        return json.load(open(_VOL, encoding="utf-8"))
    except Exception:
        return []


def _vol_add(text: str) -> None:
    """Park a line the candidate volunteered while the engine was mid-question."""
    v = [x for x in _vol_load() if time.time() - x.get("ts", 0) < 1800][-8:]
    v.append({"text": text, "ts": int(time.time()), "secret": _looks_secret(text), "used": False})
    try:
        os.makedirs(ASK_DIR, exist_ok=True)
        json.dump(v, open(_VOL, "w", encoding="utf-8"))
    except Exception:
        pass


def _vol_take(question: str):
    """A volunteered line that ANSWERS this question, or None.

    The match is deliberately crude and one-dimensional: does the question want a SECRET, and is
    this line one? Anything cleverer would silently type the wrong thing into a real employer's
    form, and a wrong guess here is worse than asking again."""
    wants_secret = bool(_SECRETISH.search(question or ""))
    for x in sorted(_vol_load(), key=lambda y: -y.get("ts", 0)):
        if x.get("used") or time.time() - x.get("ts", 0) > 1800:
            continue
        if bool(x.get("secret")) != wants_secret:
            continue
        v = _vol_load()
        for y in v:
            if y.get("ts") == x.get("ts") and y.get("text") == x.get("text"):
                y["used"] = True
        try:
            json.dump(v, open(_VOL, "w", encoding="utf-8"))
        except Exception:
            pass
        return x
    return None


def _answer_ask(text: str) -> bool:
    """True if this message was consumed as the answer to a pending apply question."""
    pend = [(p, d) for p, d in _asks() if d.get("sent")]
    if not pend:
        return False
    # NEWEST first. A question left unanswered by an earlier run stays pending forever, and
    # answering the OLDEST sent your reply to that ghost instead of the question on screen.
    pend.sort(key=lambda x: int(x[1].get("ts") or 0), reverse=True)
    path, d = pend[0]
    d["answer"], d["answered"] = text, True
    try:
        json.dump(d, open(path, "w", encoding="utf-8"))
    except Exception:
        return False
    _LAST_ANSWER[0] = time.time()
    _send("✅ Passed to the apply engine. Anything else you send in the next "
          f"{_FOLLOWUP_S // 60} min goes to the application too, not to chat.")
    return True


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
    # Announce "online" at most once per JHW_BOT_ONLINE_THROTTLE seconds (default 6h). The bot
    # is recreated on every `jhw.py local`/`apply` rebuild; without this throttle it spammed
    # the chat with "Candidate agent online" on each restart. The marker lives on the shared
    # OUT volume so it survives restarts.
    _mk = os.path.join(OUT, ".bot_online_ts")
    _throttle = int(os.environ.get("JHW_BOT_ONLINE_THROTTLE", "21600"))
    try:
        _last = os.path.getmtime(_mk)
    except Exception:
        _last = 0.0
    if time.time() - _last > _throttle:
        _send("👋 Candidate agent online. /help for what I do.")
        try:
            open(_mk, "w").close()
        except Exception:
            pass
    else:
        print("[bot] online banner throttled (last sent %ds ago)" % int(time.time() - _last),
              flush=True)
    while True:
        try:
            _pump_asks()                 # relay apply-engine questions before waiting for input
            # 25s of long-polling meant a question written just after a pump waited up to 25s
            # before reaching the phone, which reads as "the bot never asked me".
            with httpx.Client(timeout=25) as c:
                rr = c.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 10}).json()
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
                t = txt.strip()
                if t == "/cancel":
                    for p_, d_ in _asks():
                        d_["answered"], d_["answer"] = True, ""
                        json.dump(d_, open(p_, "w", encoding="utf-8"))
                    _send("Dropped any pending apply questions.")
                    continue
                # An open apply question owns the next reply — no magic word needed.
                if not t.startswith("/") and _answer_ask(t):
                    continue
                # THE FOLLOW-UP WINDOW. An answer that arrives in pieces is still an answer: it
                # belongs to the apply engine, never to the chat model.
                if not t.startswith("/") and time.time() - _LAST_ANSWER[0] < _FOLLOWUP_S:
                    _vol_add(t)
                    _send("✅ Noted for the application"
                          + (" (credential — kept out of chat, never sent to a model)."
                             if _looks_secret(t) else "."))
                    continue
                # A CREDENTIAL NEVER REACHES THE CHAT MODEL. That endpoint is a third party and its
                # reply is logged in this chat; the transcript already shows the assistant
                # discussing a pasted password back at him.
                if not t.startswith("/") and _looks_secret(t):
                    _vol_add(t)
                    print(f"[bot] refused to chat about a credential-shaped message "
                          f"({_redact(t)})", flush=True)
                    _send("🔒 That looks like a credential, so I have NOT sent it to any model.\n"
                          "I have kept it for the next thing the apply engine asks. To store it "
                          "permanently in the vault instead:\n"
                          "python jhw.py atspw <host> '<password>' <login-email>")
                    continue
                _handle(txt)
            except Exception:
                print("[bot] handler error:\n" + traceback.format_exc(), flush=True)
                _send("⚠ internal error — logged.")


def _selftest() -> int:
    """Contracts for the Telegram side, provable with no Telegram and no models.

    Every case below is a line from the candidate's real 2026-08-16 transcript."""
    import tempfile
    global ASK_DIR, _VOL
    ASK_DIR = tempfile.mkdtemp()
    _VOL = os.path.join(ASK_DIR, "_volunteered.json")
    fails = []

    def check(n, c, extra=""):
        print(("  OK   " if c else "  FAIL ") + n + (f"   {extra}" if extra and not c else ""))
        if not c:
            fails.append(n)

    print("\n[1] the credential guard (the message that was chatted about)")
    check("his real generated password is recognised",
          _looks_secret("@Q4sD9igY!mzW5u@TNtv7Ib@U0fZP6icN3"))
    check("a labelled password line is recognised",
          _looks_secret("Password : hunter2"))
    check("'Passwort' (German) is recognised", _looks_secret("Passwort: abc"))
    check("an api key is recognised", _looks_secret("api_key sk-abc123"))
    check("'Use google sso' is NOT a credential", not _looks_secret("Use google sso"))
    check("a plain username is NOT a credential", not _looks_secret("Username : feranicus@s4biz.io"))
    check("ordinary chat is NOT a credential",
          not _looks_secret("what should I apply for next?"))
    check("a job URL is NOT a credential",
          not _looks_secret("https://accenture.wd103.myworkdayjobs.com/AccentureCareers"))
    print("\n[2] redaction never leaks the value")
    r = _redact("@Q4sD9igY!mzW5u")
    check("no original character survives", not any(c in r for c in "Q4sD9igY"), r)
    check("and it says it is redacted", "redacted" in r, r)

    print("\n[3] a volunteered line is matched by KIND, not by guesswork")
    _vol_add("Username : feranicus@s4biz.io")
    _vol_add("@Q4sD9igY!mzW5u@TNtv7Ib")
    got = _vol_take("Reply with its PASSWORD (or reset via 'Forgot your password?')")
    check("a password question takes the SECRET line",
          got and got["secret"] is True, str(got))
    got2 = _vol_take("Which e-mail do you use for this employer?")
    check("a non-secret question takes the PLAIN line",
          got2 and got2["secret"] is False and "feranicus" in got2["text"], str(got2))
    check("a consumed line is not handed out twice", _vol_take("password?") is None)

    print("\n[4] a stale volunteered line is never used")
    json.dump([{"text": "old", "ts": int(time.time()) - 4000, "secret": False, "used": False}],
              open(_VOL, "w"))
    check("older than 30 min is ignored", _vol_take("what is your city?") is None)

    print("\n[5] an ask answered in pieces reaches the engine")
    os.makedirs(ASK_DIR, exist_ok=True)
    qp = os.path.join(ASK_DIR, "1.json")
    json.dump({"q": "Reply with its PASSWORD", "ts": int(time.time()), "sent": True,
               "answered": False}, open(qp, "w"))
    sent = []
    global _send
    _real_send, _send = _send, lambda t: sent.append(t)
    try:
        consumed = _answer_ask("Use google sso")
        d = json.load(open(qp))
        check("the first reply is passed to the engine",
              consumed and d.get("answered") and d["answer"] == "Use google sso", str(d))
        check("and he is told the follow-up window is open",
              any("goes to the application" in x for x in sent), str(sent))
        check("the window is actually open now", time.time() - _LAST_ANSWER[0] < 5)
    finally:
        _send = _real_send

    print("\n" + "=" * 78)
    if fails:
        print(f"[X] {len(fails)} BOT CONTRACT(S) BROKEN: {fails}")
        return 1
    print("ALL BOT CONTRACTS HOLD")
    return 0


if __name__ == "__main__":
    import sys
    if "--logic" in sys.argv:
        sys.exit(_selftest())
    main()
