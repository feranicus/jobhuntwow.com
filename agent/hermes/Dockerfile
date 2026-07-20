# Hermes brain container — ISOLATED. Fully self-contained: ALL runtime deps baked in, zero manual pip.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ripgrep git gnupg ffmpeg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
# hermes-agent + the MESSAGING extra (Telegram adapter = python-telegram-bot). NOTE: the extra is
# 'messaging', NOT 'telegram' (the runtime warning's suggested name is wrong; verified on PyPI).
RUN pip install --no-cache-dir "hermes-agent[messaging,cli,mcp,web]==0.18.2"
# agent-browser = the CLI Hermes' browser tools use to drive Chromium over CDP (jhw-browser).
# Non-fatal so a browser-client hiccup can never block the Telegram brain from starting.
RUN npm install -g agent-browser || echo "[build] agent-browser install skipped; browser tools may need it"
ENV HERMES_HOME=/hermes
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
