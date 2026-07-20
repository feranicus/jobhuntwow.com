#!/usr/bin/env bash
# Seed ~/.hermes from env + migrated assets, then run the Telegram gateway. Brains = our DO proxy
# with the cybergod 3-model failover chain (deepseek-3.2 -> llama-4-maverick -> openai-gpt-oss-120b).
set -euo pipefail
H="${HERMES_HOME:-/hermes}"; mkdir -p "$H" "$H/logs"

# 1) migrate durable assets (memory + skills)
if [ -d /data/hermes-home ]; then
  echo "[hermes] migrating durable assets from /data/hermes-home ..."
  for item in memories skills SOUL.md MEMORY.md USER.md; do
    [ -e "/data/hermes-home/$item" ] && cp -rn "/data/hermes-home/$item" "$H/" 2>/dev/null || true
  done
fi
[ -d /data/skills ] && { mkdir -p "$H/skills"; cp -rf /data/skills/. "$H/skills/" 2>/dev/null || true; }  # skills are authoritative -> overwrite
[ -f /data/candidate_profile.md ] && cp -f /data/candidate_profile.md "$H/candidate_profile.md" || true

# 2) brains = our DO proxy
PROV="${MODEL_PROVIDER:-custom}"; MODEL="${MODEL_NAME:-openai-gpt-oss-120b}"   # best tool-caller on DO (deepseek/llama narrate)
export OPENAI_API_KEY="${OPENAI_API_KEY:-${AGENT_PROXY_TOKEN:-}}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-${JHW_PROXY_BASE:-http://host.docker.internal:8000/v1}}"
ALLOWED="${TELEGRAM_ALLOWED_USERS:-${TELEGRAM_CHAT_ID:-}}"

# 3) secrets -> .env
{
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
  [ -n "$ALLOWED" ] && echo "TELEGRAM_ALLOWED_USERS=${ALLOWED}"
  [ -n "$ALLOWED" ] && echo "GATEWAY_ALLOWED_USERS=${ALLOWED}"
  echo "GATEWAY_ALLOW_ALL_USERS=true"
  [ -n "$OPENAI_API_KEY" ]  && echo "OPENAI_API_KEY=${OPENAI_API_KEY}"
  [ -n "$OPENAI_BASE_URL" ] && echo "OPENAI_BASE_URL=${OPENAI_BASE_URL}"
} > "$H/.env"
chmod 600 "$H/.env"

# 4) config.yaml — api_key written EXPLICITLY on the model AND every fallback (main-model block does
#    not auto-inherit OPENAI_API_KEY, which is why the primary 401'd while fallbacks worked).
python3 - "$H" "$PROV" "$MODEL" "${BROWSER_CDP_URL:-http://jhw-browser:9222}" <<'PYEOF'
import sys, os
H, prov, model, cdp = sys.argv[1:5]
key  = os.environ.get("OPENAI_API_KEY", "")
base = os.environ.get("OPENAI_BASE_URL", "")
try: import yaml
except Exception: print("[hermes] pyyaml missing"); sys.exit(0)
cfg = {"model": {"provider": prov, "default": model},
       "agent": {"tool_use_enforcement": True},
       "browser": {"cdp_url": cdp},
       "display": {"tool_progress": "verbose", "tool_progress_command": True}}
if prov == "custom":
    cfg["model"]["base_url"] = base
    cfg["model"]["api_key"]  = key
    chain = [model, "openai-gpt-oss-120b", "llama-4-maverick", "deepseek-3.2"]
    seen=set(); chain=[m for m in chain if not (m in seen or seen.add(m))]
    cfg["fallback_providers"] = [
        {"provider": "custom", "model": m, "base_url": base, "api_key": key} for m in chain[1:]
    ]
yaml.safe_dump(cfg, open(os.path.join(H, "config.yaml"), "w"), sort_keys=False)
print("[hermes] config: provider=%s chain=%s api_key=%s cdp=%s"
      % (prov, [model,"llama-4-maverick","openai-gpt-oss-120b"], "set" if key else "MISSING", cdp))
PYEOF

echo "[hermes] HOME=$H provider=$PROV model=$MODEL allow=$ALLOWED"
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "$ALLOWED" ]; then
  curl -s --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
       -d chat_id="${ALLOWED}" \
       -d text="Hi Master 👋 I'm up on our DO models (deepseek-3.2 head). Which Workday position today?" >/dev/null 2>&1 || true
fi
echo "[hermes] starting gateway ..."
exec hermes gateway
