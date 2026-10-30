FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CIHIVE_DATA_DIR=/data

WORKDIR /app

RUN groupadd --system cihive \
    && useradd --system --gid cihive --home-dir /app cihive

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && mkdir -p /data \
    && chown -R cihive:cihive /app /data

USER cihive

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["cihive", "serve", "--host", "0.0.0.0", "--port", "8000"]
