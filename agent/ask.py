"""Human-in-the-loop channel: the agent asks the candidate for info it doesn't have.

V1 = Telegram (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID). V2 seam = 'web' (React cabinet via the
backend). Channel chosen by JHW_ASK_CHANNEL (default 'auto' -> telegram if configured, else none).
"""
from __future__ import annotations
import asyncio, os, time, json, glob, random
import httpx

CHANNEL     = os.getenv("JHW_ASK_CHANNEL", "auto")
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT     = os.getenv("TELEGRAM_CHAT_ID", "")
# HOW LONG TO WAIT FOR A HUMAN, and it is a TWO-STAGE answer.
# MEASURED 2026-08-16 23:31: the run asked at t=222s and produced its next line at t=18521s -- more
# than five hours -- and then died with `ok=False stage=account`. The deadline itself is 600s and is
# overridden nowhere in this repo, so the wall clock advanced while the host was asleep. That is not
# the interesting part. THE INTERESTING PART IS THAT IT SAT THERE AT ALL, silently, having produced
# nothing, on a system whose whole point is "run at 3am without me".
#   * ASK_WINDOW  the FIRST wait. Short, because a question nobody has answered in two minutes is a
#                 question nobody is awake for, and the engine has better things to do than block.
#   * ASK_TIMEOUT the total. Kept, because a reply five minutes later must still be honoured.
# A HEARTBEAT goes out while waiting, so the phone shows a live agent rather than silence -- and
# `waited()` tells the CALLER how long it blocked, so a caller can decide to act autonomously
# instead of treating "no answer" as "stop".
ASK_TIMEOUT = int(os.getenv("JHW_ASK_TIMEOUT", "600"))         # total seconds to wait for a reply
ASK_WINDOW = int(os.getenv("JHW_ASK_WINDOW", "120"))           # first window before we act alone
ASK_BEAT = int(os.getenv("JHW_ASK_BEAT", "45"))                # seconds between heartbeats
WEB_BASE    = os.getenv("JHW_ASK_WEB_BASE", "")                # V2: backend endpoint base
ASK_DIR     = os.getenv("JHW_ASK_DIR", "/agent/out/asks")      # bridge to jhw_bot.py (single Telegram poller)


def channel() -> str:
    """'auto' prefers the BRIDGE, never direct Telegram polling.

    Telegram permits ONE getUpdates consumer per token. With 'auto' -> 'telegram' both this agent
    and jhw_bot polled the same token and stole each other's updates, so answers silently went to
    the wrong process. The bridge keeps jhw_bot as the single poller.
    """
    if CHANNEL != "auto":
        return CHANNEL
    if TG_TOKEN and TG_CHAT:
        return "bridge"
    return "none"


async def _telegram(question: str, timeout: int) -> str:
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    async with httpx.AsyncClient(timeout=40) as c:
        last = 0
        try:
            r = (await c.get(f"{base}/getUpdates", params={"timeout": 0})).json()
            for u in r.get("result", []):
                last = max(last, u["update_id"])
        except Exception:
            pass
        _r = await c.get(f"{base}/sendMessage", params={
            "chat_id": TG_CHAT,
            "text": f"🤖 JobHuntWOW needs your input:\n\n{question}\n\nReply here with the value."})
        if _r.status_code != 200:
            print(f"[ask_human] TELEGRAM API {_r.status_code}: {_r.text[:200]} — check "
                  f"TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (verify: python jhw.py telegram)", flush=True)
            return ""
        offset, deadline = last + 1, time.time() + timeout
        while time.time() < deadline:
            try:
                rr = (await c.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 30})).json()
            except Exception:
                await asyncio.sleep(2); continue
            for u in rr.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message", {})
                if str(m.get("chat", {}).get("id")) == str(TG_CHAT) and m.get("text"):
                    ans = m["text"].strip()
                    await c.get(f"{base}/sendMessage", params={"chat_id": TG_CHAT, "text": "✅ Got it — thanks."})
                    return ans
    return ""


async def _web(question: str, timeout: int) -> str:
    """V2 stub: post the question to the backend and poll for the candidate's answer in the cabinet."""
    if not WEB_BASE:
        return ""
    async with httpx.AsyncClient(timeout=40) as c:
        try:
            r = await c.post(f"{WEB_BASE}/api/ask", json={"question": question})
            qid = r.json().get("id")
        except Exception:
            return ""
        deadline = time.time() + timeout
        while qid and time.time() < deadline:
            try:
                a = (await c.get(f"{WEB_BASE}/api/ask/{qid}")).json()
                if a.get("answer"):
                    return a["answer"].strip()
            except Exception:
                pass
            await asyncio.sleep(3)
    return ""


async def _bridge(question: str, timeout: int) -> str:
    """Hand the question to jhw_bot.py via the shared out/asks/ dir and wait for the answer.
    The BOT is the only process that polls Telegram getUpdates (two pollers would 409-conflict),
    so the agent never talks to getUpdates directly — it drops a question file and waits."""
    try:
        os.makedirs(ASK_DIR, exist_ok=True)
        # Drop questions left over from previous runs: they are answered by nobody, they clutter
        # Telegram, and the reply can be routed to them instead of the live question.
        for f in os.listdir(ASK_DIR):
            fp = os.path.join(ASK_DIR, f)
            try:
                if f.endswith(".json") and (time.time() - os.path.getmtime(fp)) > 900:
                    os.remove(fp)
            except Exception:
                pass
        qid = f"{int(time.time())}_{random.randint(1000, 9999)}"
        path = os.path.join(ASK_DIR, qid + ".json")
        json.dump({"q": question, "ts": int(time.time()), "sent": False, "answered": False}, open(path, "w"))
    except Exception as e:
        print(f"[ask_human] bridge write failed: {e}", flush=True)
        return ""
    deadline = time.time() + timeout
    warned = False
    beat = time.time()
    while time.time() < deadline:
        await asyncio.sleep(2)
        # A HEARTBEAT, because silence and a crash look identical from a phone. It also carries the
        # remaining time, so the human knows whether it is still worth answering.
        if ASK_BEAT and time.time() - beat > ASK_BEAT:
            beat = time.time()
            left = int(deadline - time.time())
            try:
                d0 = json.load(open(path))
            except Exception:
                d0 = {}
            if not d0.get("answered"):
                print(f"[ask_human] still waiting ({left}s left) — the engine is holding this page "
                      f"and will act on its own if you do not reply", flush=True)
                if TG_TOKEN and TG_CHAT and left > 0:
                    try:
                        async with httpx.AsyncClient(timeout=15) as c:
                            await c.get(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                        params={"chat_id": TG_CHAT,
                                                "text": f"⏳ still waiting on your reply "
                                                        f"({left}s left, then I decide myself)"})
                    except Exception:
                        pass
        try:
            d = json.load(open(path))
        except Exception:
            continue
        if d.get("answered"):
            return (d.get("answer") or "").strip()
        # jhw_bot marks the file `sent` once it has relayed it. If that has not happened within
        # 10s the bot is not pumping - the human would sit staring at a silent phone while the
        # run waits 10 minutes. Say so, and deliver the question directly so it is not lost.
        if not warned and not d.get("sent") and time.time() - d.get("ts", 0) > 10:
            warned = True
            print("[ask_human] jhw-bot has NOT relayed this question (it is not running or it is "
                  "stuck). Sending it to Telegram directly; your reply is still routed by the "
                  "bot, so check it with: python jhw.py local-logs jhw-bot", flush=True)
            if TG_TOKEN and TG_CHAT:
                try:
                    async with httpx.AsyncClient(timeout=20) as c:
                        rsp = await c.get(
                            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                            params={"chat_id": TG_CHAT,
                                    "text": "🤖 APPLY (direct - the bot did not relay this) ▸\n\n"
                                            + question[:3500]})
                    if rsp.status_code != 200:
                        print(f"[ask_human] direct send failed {rsp.status_code}: "
                              f"{rsp.text[:160]}", flush=True)
                except Exception as e:
                    print(f"[ask_human] direct send failed: {e}", flush=True)
    print("[ask_human] no answer within timeout (is jhw_bot running? -> python jhw.py bot)", flush=True)
    return ""


async def notify(message: str) -> None:
    """Fire-and-forget alert to the candidate (no waiting for a reply). Used whenever the driver
       stops, pauses, or gets stuck — so the human ALWAYS hears about it."""
    ch = channel()
    print(f"[notify/{ch}] {message}", flush=True)
    # BUG: this used to `return` unless ch == "telegram" - but channel() returns "bridge"
    # whenever a token+chat are configured, so EVERY notification (paused, stuck, finished) was
    # printed to the console and never sent. Sending is safe on any channel: only getUpdates may
    # have a single consumer, sendMessage has no such limit.
    if ch not in ("telegram", "bridge") or not (TG_TOKEN and TG_CHAT):
        return
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(f"{base}/sendMessage",
                               params={"chat_id": TG_CHAT, "text": f"🤖 JobHuntWOW: {message}"})
        # NEVER fail silently: a bad token (401) / bad chat id (400) must be visible.
        if resp.status_code != 200:
            print(f"[notify] TELEGRAM API {resp.status_code}: {resp.text[:200]} — check "
                  f"TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in agent/.env "
                  f"(verify with: python jhw.py telegram)", flush=True)
    except Exception as e:
        print(f"[notify] send failed: {e}", flush=True)


_LAST = {"asked": 0.0, "answered": False, "waited": 0.0}


def waited() -> dict:
    """What happened to the LAST question: how long we blocked and whether anyone replied.

    A caller that treats "no answer" as "stop" turns an absent human into a failed application. With
    this it can tell the difference between "the human said no" and "the human is asleep" and choose
    to act on its own -- which is the entire point of the 3-LLM panel sitting one rung below."""
    return dict(_LAST)


async def ask_human(question: str, timeout: int | None = None) -> str:
    timeout = timeout or ASK_TIMEOUT
    ch = channel()
    print(f"[ask_human/{ch}] {question}", flush=True)
    # "bridge" = write a question file; jhw_bot (the ONLY Telegram poller) relays it and writes
    # the answer back. "telegram" is mapped here too: two getUpdates consumers on one token steal
    # each other's messages. Without this branch channel()=="bridge" fell through to return "" —
    # no file was ever written, so no question ever reached Telegram.
    if ch in ("bridge", "telegram"):
        t0 = time.time()
        _LAST.update(asked=t0, answered=False, waited=0.0)
        r = await _bridge(question, timeout)
        _LAST.update(waited=time.time() - t0, answered=bool(r))
        return r
    if ch == "web":
        return await _web(question, timeout)
    print(f"[ask_human] NO CHANNEL configured (ch={ch!r}) — question unanswered", flush=True)
    return ""
