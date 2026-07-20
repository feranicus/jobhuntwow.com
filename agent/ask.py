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
ASK_TIMEOUT = int(os.getenv("JHW_ASK_TIMEOUT", "600"))         # seconds to wait for a human reply
WEB_BASE    = os.getenv("JHW_ASK_WEB_BASE", "")                # V2: backend endpoint base
ASK_DIR     = os.getenv("JHW_ASK_DIR", "/agent/out/asks")      # bridge to jhw_bot.py (single Telegram poller)


def channel() -> str:
    if CHANNEL != "auto":
        return CHANNEL
    if TG_TOKEN and TG_CHAT:
        return "telegram"
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
        qid = f"{int(time.time())}_{random.randint(1000, 9999)}"
        path = os.path.join(ASK_DIR, qid + ".json")
        json.dump({"q": question, "ts": int(time.time()), "sent": False, "answered": False}, open(path, "w"))
    except Exception as e:
        print(f"[ask_human] bridge write failed: {e}", flush=True)
        return ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            d = json.load(open(path))
        except Exception:
            continue
        if d.get("answered"):
            return (d.get("answer") or "").strip()
    print("[ask_human] no answer within timeout (is jhw_bot running? -> python jhw.py bot)", flush=True)
    return ""


async def notify(message: str) -> None:
    """Fire-and-forget alert to the candidate (no waiting for a reply). Used whenever the driver
       stops, pauses, or gets stuck — so the human ALWAYS hears about it."""
    ch = channel()
    print(f"[notify/{ch}] {message}", flush=True)
    if ch != "telegram":
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


async def ask_human(question: str, timeout: int | None = None) -> str:
    timeout = timeout or ASK_TIMEOUT
    ch = channel()
    print(f"[ask_human/{ch}] {question}", flush=True)
    if ch == "telegram":
        return await _bridge(question, timeout)
    if ch == "web":
        return await _web(question, timeout)
    return ""   # no channel configured -> agent leaves blank + notes it
