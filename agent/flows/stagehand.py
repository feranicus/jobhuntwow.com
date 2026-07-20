"""AI fallback layer of the apply stack — thin client for the Stagehand sidecar.

The deterministic Playwright adapters (built from real recordings) handle known ATSes with ZERO LLM.
This module is called only when a recorded selector fails or the ATS is unknown. The sidecar drives
the SAME Chrome over CDP, so control can pass back and forth mid-application.

Pattern that keeps it fast: call observe() to get a concrete selector, USE it, and persist it into the
ATS selector map -> next application on that ATS is deterministic again (no LLM).
"""
from __future__ import annotations
import os
import httpx

BASE = os.getenv("JHW_STAGEHAND_URL", "http://127.0.0.1:8081")
TIMEOUT = float(os.getenv("JHW_STAGEHAND_TIMEOUT", "300"))


async def _post(path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}{path}", json=payload or {})
        r.raise_for_status()
        return r.json()


async def health() -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        return (await c.get(f"{BASE}/health")).json()


async def goto(url: str) -> dict:
    return await _post("/goto", {"url": url})


async def observe(instruction: str) -> list:
    """Return candidate actions (each carries a concrete selector) WITHOUT acting.
       Persist the winning selector into the ATS map to make the next run deterministic."""
    return (await _post("/observe", {"instruction": instruction})).get("actions", [])


async def act(instruction: str | None = None, action: dict | None = None) -> dict:
    """Act from natural language, or REPLAY a previously observed action object (no inference)."""
    return await _post("/act", {"instruction": instruction, "action": action})


async def extract(instruction: str, schema: dict | None = None) -> dict:
    return (await _post("/extract", {"instruction": instruction, "schema": schema})).get("result")


async def agent(instruction: str, system: str = "", max_steps: int = 25) -> dict:
    """Autonomous multi-step — for a whole unknown form. Feed it SKILL.md + candidate_profile.md."""
    return (await _post("/agent", {"instruction": instruction, "system": system,
                                   "maxSteps": max_steps})).get("result")


async def upload(path: str, selector: str = 'input[type="file"]') -> dict:
    """Resume upload via Playwright setInputFiles on the same browser (no LLM, no file picker)."""
    return await _post("/upload", {"path": path, "selector": selector})
