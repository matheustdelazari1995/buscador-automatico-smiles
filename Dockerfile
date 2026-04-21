FROM python:3.11-slim

# System deps: Chrome, Xvfb (virtual display), x11vnc (VNC server),
# noVNC (web-based VNC client), supervisor (process manager)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 curl ca-certificates \
    xvfb x11vnc \
    novnc websockify \
    supervisor \
    fonts-liberation fonts-noto-color-emoji \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# noVNC index.html symlink para servir direto em /
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY search_engine.py server.py routes_store.py accounts_store.py system_state.py login_helper.py ./
COPY static/ ./static/

# Supervisor config (roda Xvfb + x11vnc + noVNC + app em paralelo)
COPY deploy/supervisord.conf /etc/supervisor/conf.d/app.conf
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Virtual display
ENV DISPLAY=:99

# Portas: 8001 = app, 6080 = noVNC web
EXPOSE 8001 6080

ENTRYPOINT ["/entrypoint.sh"]
