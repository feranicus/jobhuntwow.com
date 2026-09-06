#!/usr/bin/env python3
"""Workday Application Questions — smart fill for ANY tenant.

Binds each control to its question label, classifies, runs answer_ladder,
acts, verifies. Does NOT apply recorded Yes to generic "Select One".
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Local imports work both as package and flat /agent/flows
try:
    from question_class import (
        CONDITIONAL_DETAIL,
        UNKNOWN,
        classify,
        is_yes_no_options,
    )
    from answer_ladder import resolve, save_learned, normalize_question
except ImportError:
    from flows.question_class import (  # type: ignore
        CONDITIONAL_DETAIL,
        UNKNOWN,
        classify,
        is_yes_no_options,
    )
    from flows.answer_ladder import resolve, save_learned, normalize_question  # type: ignore

ACTION_TIMEOUT = 5000


def _log(msg: str, log: Optional[Callable] = None) -> None:
    line = f"[wd-smart] {msg}"
    if log:
        try:
            log(line)
        except TypeError:
            log(line)
    else:
        print(line, flush=True)


async def _question_controls(page) -> list[dict]:
    """Return [{question, kind, options_hint}] from the live DOM."""
    try:
        rows = await page.evaluate(
            r"""() => {
              const out = [];
              const seen = new Set();
              const blocks = [...document.querySelectorAll(
                'fieldset, [data-automation-id*="formField"], [data-automation-id*="FormField"], li, section, div'
              )];
              for (const root of blocks) {
                const text = (root.innerText || '').replace(/\s+/g, ' ').trim();
                if (text.length < 12 || text.length > 600) continue;
                // must look like a question or required field
                if (!/[?]/.test(text) && !/\*/.test(text) && !/required/i.test(text)) continue;
                // first line-ish as question
                let q = text.split(/\n/).map(s => s.trim()).filter(Boolean)[0] || text.slice(0, 180);
                q = q.replace(/\s+/g, ' ').trim().slice(0, 200);
                if (seen.has(q.toLowerCase())) continue;
                // skip nav chrome
                if (/careers home|search for jobs|candidate home|job alerts|save and continue|back to job/i.test(q)
                    && q.length < 40) continue;
                const hasSelect = !!root.querySelector('button, [role=button], select');
                const hasTextarea = !!root.querySelector('textarea');
                const hasInput = !!root.querySelector('input[type=text], input:not([type]), input[type=email]');
                if (!hasSelect && !hasTextarea && !hasInput) continue;
                seen.add(q.toLowerCase());
                out.push({
                  question: q,
                  kind: hasTextarea ? 'textarea' : (hasSelect ? 'select' : 'text'),
                  snippet: text.slice(0, 280),
                });
                if (out.length >= 25) break;
              }
              return out;
            }"""
        )
        return rows or []
    except Exception:
        return []


async def _open_and_read_options(page, question: str) -> list[str]:
    """Click the Select One near question and list option labels."""
    try:
        await page.evaluate(
            """(q) => {
              const re = new RegExp(q.slice(0, 40).replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'i');
              const nodes = [...document.querySelectorAll('label,div,span,legend,p')];
              const qel = nodes.find(el => re.test((el.innerText||'').trim()) && (el.innerText||'').length < 250);
              if (!qel) return false;
              let root = qel;
              for (let i = 0; i < 8 && root; i++) {
                const btn = [...root.querySelectorAll('button,[role=button]')].find(b => {
                  const n = ((b.getAttribute('aria-label')||'') + ' ' + (b.innerText||'')).trim();
                  return /Select One|Yes|No|Required/i.test(n) || n.length < 30;
                });
                if (btn) { btn.click(); return true; }
                root = root.parentElement;
              }
              return false;
            }""",
            question[:80],
        )
        await page.wait_for_timeout(450)
        opts = await page.evaluate(
            """() => {
              const opts = [...document.querySelectorAll(
                '[role=option], [data-automation-id*=promptOption], li[role=option]'
              )].map(e => (e.innerText||'').trim()).filter(t => t && t.length < 80);
              return [...new Set(opts)].slice(0, 20);
            }"""
        )
        return list(opts or [])
    except Exception:
        return []


async def _pick_option(page, want: str) -> bool:
    for loc in (
        page.get_by_role("listbox").get_by_role("option", name=re.compile(rf"^{re.escape(want)}$", re.I)),
        page.get_by_role("option", name=re.compile(rf"^{re.escape(want)}$", re.I)),
        page.locator("[data-automation-id*='promptOption']").filter(
            has_text=re.compile(rf"^{re.escape(want)}$", re.I)
        ),
        page.get_by_text(re.compile(rf"^{re.escape(want)}$", re.I)),
    ):
        try:
            if await loc.count() == 0:
                continue
            row = loc.first
            if await row.is_visible():
                await row.click(timeout=ACTION_TIMEOUT)
                await page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def _fill_textarea_near(page, question: str, value: str) -> bool:
    try:
        ok = await page.evaluate(
            """([q, value]) => {
              const re = new RegExp(q.slice(0, 36).replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'i');
              const areas = [...document.querySelectorAll('textarea')];
              for (const a of areas) {
                const root = a.closest('div,li,fieldset,section') || a.parentElement;
                const t = (root && root.innerText || '').slice(0, 300);
                if (re.test(t) || /if you answered yes|further detail|further information/i.test(t)) {
                  a.focus();
                  a.value = value;
                  a.dispatchEvent(new Event('input', {bubbles:true}));
                  a.dispatchEvent(new Event('change', {bubbles:true}));
                  return true;
                }
              }
              return false;
            }""",
            [question[:80], value],
        )
        return bool(ok)
    except Exception:
        return False


async def fill_application_questions(
    page,
    data: dict | None = None,
    *,
    ask_human: Any = None,
    log: Optional[Callable] = None,
) -> int:
    """Fill Application Questions step. Returns number of fields written."""
    done = 0
    rows = await _question_controls(page)
    _log(f"indexed {len(rows)} question block(s)", log)

    for row in rows:
        q = row.get("question") or ""
        kind = row.get("kind") or "select"
        cls = classify(q)
        if cls == UNKNOWN and kind == "select" and len(q) < 20:
            continue

        options: list[str] = []
        if kind == "select":
            options = await _open_and_read_options(page, q)
            if not options and is_yes_no_options(["Yes", "No"]):
                options = ["Yes", "No"]

        result = await resolve(
            q,
            options=options,
            data=data,
            required=True,
            ask_human=ask_human,
            log=log,
        )
        ans = (result or {}).get("answer") or ""
        src = (result or {}).get("source") or ""
        if not ans:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            continue

        if kind == "textarea" or cls == CONDITIONAL_DETAIL:
            if await _fill_textarea_near(page, q, ans):
                done += 1
                _log(f"textarea <- {ans[:40]!r} ({src}) | {q[:50]!r}", log)
            continue

        # select / yes-no
        if not options:
            # reopen
            options = await _open_and_read_options(page, q)
        if await _pick_option(page, ans):
            done += 1
            _log(f"select <- {ans!r} ({src}) | {q[:50]!r}", log)
            if src in ("panel", "human", "rule", "profile"):
                try:
                    save_learned(normalize_question(q), ans, {"class": cls, "source": src})
                except Exception:
                    pass
        else:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    # Safety: any empty conditional textarea → N/A
    try:
        n = min(await page.locator("textarea").count(), 12)
        for i in range(n):
            a = page.locator("textarea").nth(i)
            if not await a.is_visible():
                continue
            lab = await a.evaluate(
                """el => ((el.closest('div,li,fieldset,section')||el.parentElement||{}).innerText||'').slice(0,220)"""
            ) or ""
            if not re.search(r"if you answered yes|further detail|further information|discuss with your recruiter", lab, re.I):
                continue
            cur = ""
            try:
                cur = await a.input_value()
            except Exception:
                pass
            if cur and cur.strip():
                continue
            await a.fill("N/A — answered No to the question above.", timeout=ACTION_TIMEOUT)
            done += 1
            _log("conditional textarea <- N/A", log)
    except Exception as e:
        _log(f"textarea sweep: {type(e).__name__}", log)

    return done


async def repair_validation_errors(
    page,
    error_text: str,
    data: dict | None = None,
    *,
    ask_human: Any = None,
    log: Optional[Callable] = None,
) -> int:
    """If validation mentions conditional detail or named questions, re-run smart fill."""
    if not error_text:
        return 0
    if re.search(
        r"if you answered yes|further detail|further information|"
        r"previously been considered|family member|non-compete|related by blood|"
        r"legally eligible|sponsorship",
        error_text,
        re.I,
    ):
        return await fill_application_questions(page, data, ask_human=ask_human, log=log)
    return 0
