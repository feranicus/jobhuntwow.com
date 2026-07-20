#!/usr/bin/env bash
# Reused from cybergodai/cybergodai (secure browser sandbox). Waits for X, launches HEADFUL
# Google Chrome in the Xvfb display with CDP open on 9222 so browser-use can drive it.
set -euo pipefail

export DISPLAY="${DISPLAY:-:0}"
export HOME="/home/chrome-user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-chrome-user}"
mkdir -p "$XDG_RUNTIME_DIR"
chown chrome-user:chrome-user "$XDG_RUNTIME_DIR" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR"

# PERMANENT FIX: a container recreate/kill leaves Chrome's profile singleton lock behind, and Chrome
# then refuses to start (exit 21 -> supervisord FATAL -> CDP never opens). Clear the stale lock on every
# start. This does NOT touch cookies/session — the LinkedIn login in chrome-data is preserved.
rm -f "$HOME/chrome-data/SingletonLock" "$HOME/chrome-data/SingletonSocket" \
      "$HOME/chrome-data/SingletonCookie" 2>/dev/null || true
echo "[chrome] cleared any stale profile singleton lock"

echo "[wait-for-x] Checking X server on $DISPLAY ..."
for i in {1..40}; do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "[wait-for-x] X is ready after $i s."
    break
  fi
  echo "[wait-for-x] ($i) not ready yet; sleeping 1s..."
  sleep 1
done
sleep 3

exec /usr/bin/google-chrome-stable \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --disable-software-rasterizer \
  --disable-features=VizDisplayCompositor \
  --no-first-run \
  --start-maximized \
  --user-data-dir="$HOME/chrome-data" \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  about:blank
