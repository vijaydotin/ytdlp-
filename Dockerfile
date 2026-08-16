FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# API only — no static app files on the host.
COPY server.py .

ENV PORT=8765
ENV PYTHONUNBUFFERED=1
# Android player client avoids YouTube bot-checks on datacenter IPs.
# Override per-host with YTDLP_EXTRA if needed.
ENV YTDLP_EXTRA="--extractor-args youtube:player_client=android"

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)" || exit 1

CMD ["python3", "server.py"]
