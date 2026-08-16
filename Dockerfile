FROM python:3.12-slim

WORKDIR /app

# Deno JS runtime for yt-dlp's YouTube challenge solving (EJS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && curl -fsSLo /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip" \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && apt-get purge -y curl unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && deno --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# API only — no static app files on the host.
COPY server.py .

ENV PORT=8765
ENV PYTHONUNBUFFERED=1
# No player_client override: default (web) client + cookies + Deno solves
# YouTube's JS challenges. Override per-host with YTDLP_EXTRA if needed.
ENV YTDLP_EXTRA=""
# Cap Deno's V8 heap so challenge solving stays under the 512 MB free tier.
ENV DENO_V8_FLAGS="--max-old-space-size=128"

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/api/health', timeout=4)" || exit 1

CMD ["python3", "server.py"]
