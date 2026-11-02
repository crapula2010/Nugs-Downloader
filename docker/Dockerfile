FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The desktop window (pywebview/pyobjc) is only used by launcher.py, not by the web server.
COPY requirements.txt ./
RUN grep -viE '^(pywebview|pyobjc)' requirements.txt > requirements-server.txt \
    && pip install --no-cache-dir -r requirements-server.txt

COPY . .

# Keep compatibility with configs that expect ./ffmpeg.
RUN ln -sf /usr/bin/ffmpeg /app/ffmpeg

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/config', timeout=4)"

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
