"""
workday_upload.py — hands-off resume upload for Workday apply flows.

Workday's "Select files" button opens a NATIVE OS file dialog that the browser
agent CANNOT drive via JS. This module uploads the resume with NO human click,
using one of two strategies (auto-selected by what the host agent provides):

  A) Playwright (preferred): intercept the native 'filechooser' event and call
     chooser.set_files(path). Handles the OS dialog natively — most robust.

  B) Raw CDP fallback: find the hidden <input type=file> and call
     DOM.setFileInputFiles, then dispatch input/change events so React's
     onChange fires. Avoids the OS dialog entirely (even cleaner).

Neither strategy "fakes" the upload — the real file bytes reach Workday.

Integration:
  - Docker agent using Playwright:   upload_via_playwright(page, resume_path)
  - Docker agent using a raw CDP session (Playwright CDPSession, aiohttp
    websocket, etc.):                await upload_via_cdp(session, resume_path)
"""

import os


def _abs(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Resume PDF not found: {path}")
    return path


# --------------------------------------------------------------------------
# A) PLAYWRIGHT — intercept the native file-chooser dialog
# --------------------------------------------------------------------------
def upload_via_playwright(page, resume_path: str, timeout_ms: int = 15000) -> bool:
    """page: a Playwright Page. Clicks the upload button and feeds the file
    straight into the intercepted chooser — no OS dialog, no human."""
    resume_path = _abs(resume_path)
    with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
        # The button label varies ("Select files" / "Upload a file" / "Upload").
        btn = (
            page.locator('button:has-text("Select files")').first
            or page.locator('button:has-text("Upload")').first
        )
        btn.click()
    chooser = chooser_info.value
    chooser.set_files(resume_path)
    # Confirm Workday registered it: the "Delete <file>" control appears.
    page.wait_for_selector("text=/Delete .*\\.(pdf|docx?|rtf)/i", timeout=timeout_ms)
    return True


# --------------------------------------------------------------------------
# B) RAW CDP — set the hidden file input directly, then fire React events
# --------------------------------------------------------------------------
async def upload_via_cdp(session, resume_path: str, selector: str = "input[type=file]") -> bool:
    """session: an async CDP session with the DOM + Runtime domains available
    (e.g. Playwright's page.context.new_cdp_session(page), or an aiohttp
    websocket wrapper exposing `await session.send(method, params)`).

    Steps: enable DOM -> locate the file input -> setFileInputFiles (populates
    .files) -> dispatch input+change so React's onChange handler runs.
    """
    resume_path = _abs(resume_path)

    await session.send("DOM.enable")
    doc = await session.send("DOM.getDocument", {"depth": -1})
    root_id = doc["root"]["nodeId"]
    q = await session.send("DOM.querySelector", {"nodeId": root_id, "selector": selector})
    backend_node_id = q.get("backendNodeId") or q.get("nodeId")
    if not backend_node_id:
        raise RuntimeError("No <input type=file> present in the DOM")

    await session.send(
        "DOM.setFileInputFiles",
        {"backendNodeId": backend_node_id, "files": [resume_path]},
    )

    # React listens for the native 'input'/'change' events on file inputs.
    # Dispatching them (bubbling) makes Workday register the selection.
    await session.send(
        "Runtime.evaluate",
        {
            "expression": (
                "(() => {"
                "  const i = document.querySelector('input[type=file]');"
                "  if (!i) return 'NO_INPUT';"
                "  i.dispatchEvent(new Event('input', {bubbles: true}));"
                "  i.dispatchEvent(new Event('change', {bubbles: true}));"
                "  return 'DISPATCHED:' + (i.files && i.files.length);"
                "})();"
            ),
            "returnByValue": True,
        },
    )
    return True


# --------------------------------------------------------------------------
# Self-check (run: python workday_upload.py /path/to/resume.pdf)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python workday_upload.py <resume.pdf>")
        sys.exit(1)
    p = _abs(sys.argv[1])
    try:
        import playwright  # noqa: F401

        print(f"OK playwright available; resume={p}")
        print("Use: upload_via_playwright(page, r'%s')" % p)
    except ImportError:
        print(f"playwright NOT installed; resume={p}")
        print("Use raw CDP: await upload_via_cdp(session, r'%s')" % p)
