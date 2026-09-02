# The web server only. The capture stack (vosk, pyaudio, numpy) is Windows-only
# and has no business on a server that just serves records, which is why this
# installs requirements-web.txt rather than requirements.txt.
FROM python:3.13-slim

# Nothing is compiled and nothing is fetched at runtime, so the image needs no
# build tools. curl is here for the healthcheck and nothing else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Its own user. The database is a bind mount from the host, and a container
# writing to it as root leaves files the host user cannot read.
RUN useradd --system --uid 10001 --create-home --home-dir /home/modsuite modsuite

WORKDIR /app

# Dependencies first, so editing the app does not reinstall them.
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY . .

# Where state lives, all of it outside the image: the database, the screenshots
# and the config are mounted in. Nothing in /app is written to at runtime, so
# an upgrade is "pull a new image" and never "migrate the container".
ENV MODTOOL_DB=/data/modtool.db \
    MODTOOL_SHOTS=/data/incident_shots \
    MODTOOL_WEB_CONFIG=/config/web_config.json \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data /config && chown -R modsuite:modsuite /data /config /app

USER modsuite
EXPOSE 8787

# The same endpoint the watchdog and the status page use, so "healthy" means
# the same thing everywhere.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/healthz || exit 1

# --no-reload because the reloader spawns a child, and then the signal that
# stops the container reaches the wrong process: the run log fills with starts
# and no stops, and shutdown takes the full grace period every time.
CMD ["python", "run_web.py", "--no-reload", "--host", "0.0.0.0", "--port", "8787"]
