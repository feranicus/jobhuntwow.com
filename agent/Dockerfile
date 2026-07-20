# jhw-agent — built on the cybergodai secure-browser sandbox (Ubuntu 24.04 + real Google Chrome
# in an Xvfb virtual display, viewable via noVNC). Chrome runs HEADFUL with CDP on 9222;
# browser-use connects to it. Python 3.12 (Noble) satisfies browser-use.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:0 \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

RUN apt-get update && apt-get install -y \
    ca-certificates curl wget gnupg unzip supervisor \
    xvfb x11vnc novnc websockify xauth dbus x11-utils \
    fonts-liberation fonts-noto-color-emoji \
    libayatana-appindicator3-1 libasound2t64 libatk-bridge2.0-0 \
    libnss3 libxss1 libx11-xcb1 libgbm1 \
    libxcomposite1 libxdamage1 libxrandr2 libgtk-3-0 \
    libxshmfence1 libdrm2 libxkbcommon0 libu2f-udev xdg-utils libvulkan1 \
    python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
  | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
  > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

# Isolated venv so pip doesn't fight apt-managed Python packages (typing_extensions, requests, ...).
COPY requirements.txt /agent/requirements.txt
RUN apt-get update && apt-get install -y python3-venv && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r /agent/requirements.txt

RUN useradd -m -s /bin/bash chrome-user \
 && mkdir -p /home/chrome-user/chrome-data /tmp/runtime-chrome-user /agent/out \
 && chown -R chrome-user:chrome-user /home/chrome-user /tmp/runtime-chrome-user /agent

COPY run-chrome.sh /usr/local/bin/run-chrome.sh
RUN chmod +x /usr/local/bin/run-chrome.sh
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY agent.py /agent/agent.py

EXPOSE 5900 9090 9222
CMD ["/usr/bin/supervisord","-c","/etc/supervisor/conf.d/supervisord.conf"]
