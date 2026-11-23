FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app app

# The SQLite database lives in /data (mount a volume there). The app runs as a
# non-root user; on Unraid run it as 99:100 (see README) so the bind mount is writable.
RUN useradd --system --uid 1000 --no-create-home jobhub && mkdir /data && chown jobhub /data
USER jobhub
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# One worker on purpose: the background alert checker runs inside each worker. Threads handle
# concurrency. No access log: request URLs contain coordinates, which must never be logged.
# Gunicorn's access log is written only once a reply has been fully sent: "sent" + method, PATH ONLY (%(U)s, never the
# query string, which can hold coordinates), status, bytes, microseconds, and the client address (LAN devices).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "60", \
     "--access-logfile", "-", "--access-logformat", "sent %(h)s %(m)s %(U)s %(s)s %(B)sB %(D)sus", "app:create_app()"]
