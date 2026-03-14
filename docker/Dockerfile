FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Keep compatibility with configs that expect ./ffmpeg.
RUN ln -sf /usr/bin/ffmpeg /app/ffmpeg

EXPOSE 8090

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
