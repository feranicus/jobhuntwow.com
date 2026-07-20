# JobHuntWOW — Local hybrid stack (Docker, isolated)

Everything that touches the browser or your data runs **locally in Docker** on your machine (your
residential IP). Only **inference/brains** run remotely (DigitalOcean). A compromised agent is boxed
into a container with no access to your PC.

```
LOCAL (this machine, Docker)                         DIGITALOCEAN (cloud)
┌───────────────────────────────────────┐          ┌──────────────────────────────┐
│ jhw-browser  Chromium + noVNC          │          │ LLM serverless (DeepSeek…)   │
│   • real residential IP                │  brains  │ bge-m3 resume↔JD matching    │
│   • watch at http://localhost:9090     │◀────────▶│ React portal (builds profile)│
│   • CDP internal only (not published)  │          │ per-user Telegram orchestr.  │
│ jhw-hermes   the agent (read-only,     │          └──────────────────────────────┘
│   cap-drop ALL, only /data:ro mounted) │
│   • drives jhw-browser over CDP        │
│   • reads /data/candidate_profile.md   │
└───────────────────────────────────────┘
```

## Security posture (why it's safe)
- `jhw-hermes`: `read_only` rootfs, `cap_drop: [ALL]`, `no-new-privileges`, **no host filesystem** except
  the single dedicated `./userdata` folder mounted **read-only**. No `docker.sock`. If the agent is
  popped, it can't read your Documents, your keys, or run host commands.
- `jhw-browser`: CDP (`:9222`) is **not published** — only the in-Docker agent reaches it. Only noVNC
  (`:9090`) is exposed, on `127.0.0.1` only. `mem_limit` caps RAM.
- Local IP is residential (not a DO datacenter IP), so ATS bot-detection sees a normal home browser.

## Run it (ONE command)
```bash
cd agent && python jhw.py local
```
`python jhw.py local` does EVERYTHING itself — frees any ports held by the old sandbox, starts the
DO-proxy backend (brains), builds + starts the two isolated containers, and waits until the browser is
ready. No `.env.local`, no separate backend step. Reuses your existing `agent/.env`.
Watch the browser at **http://localhost:9090**; talk to the agent on **Telegram** (`/start`).
Stop with `python jhw.py local-down`.

## Per-user data (`agent/userdata/`, mounted read-only)
- `candidate_profile.md` — the answer DB (seeded from hermes-context). The portal will generate this per user.
- `skills/` — per-ATS playbooks (e.g. `ats-workday/SKILL.md`). Drop the ones Hermes authored here so
  every container ships them; one fix rolls out to all users.

## Browser / RAM
Chromium (not full Chrome) + tuning flags (`--disable-dev-shm-usage --disable-gpu --disable-extensions
--disable-background-networking`) via `CHROME_EXTRA_FLAGS`. Same engine as Chrome but lighter idle and a
cleaner ATS fingerprint. If RAM per user is still too high at scale, swap to Camofox (Firefox anti-detect,
built-in VNC) — a later change, not now.

## NOT yet verified / open items (honest)
- **First boot is untested here** (no Docker in the authoring env). Expect to debug the first `up`:
  - `run-chrome.sh` must pass `$CHROME_EXTRA_FLAGS` to chromium for the RAM flags to take effect (one-line add).
  - `browser.cdp_url` may need the `ws://jhw-browser:9222/...` form instead of `http://` — confirm on boot.
  - `hermes gateway` in a slim container: confirm the Telegram bot comes up and it connects to the browser CDP.
- **Resume upload** still needs the CDP `setInputFiles` shim in the browser layer (the scale blocker Hermes flagged).
- **DO side** (portal, DB, bge matching, per-user orchestration) is the next build — this file is the local half only.
