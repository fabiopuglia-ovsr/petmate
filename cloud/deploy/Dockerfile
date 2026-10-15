FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd -r -u 10001 petmate && mkdir -p /data/clips && chown -R petmate /data
USER petmate

ENV DATABASE_URL=sqlite:////data/petmate.db STORAGE_DIR=/data/clips
EXPOSE 8000
# Single worker: the signaling hub is in-memory (see app/hub.py for the Redis path).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws-ping-interval", "20", "--ws-ping-timeout", "20"]
