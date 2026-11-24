# Multi-stage Dockerfile for production claim platform (Decision D3)
FROM alpine:3.23.8@sha256:5c216c9767869f6ec7c4718977601d52f087c5c7bf2fdfb198e403f6b48ec40d AS builder

WORKDIR /build

# Alpine v3.23 provides the supported Python 3.12 runtime and patched setuptools.
RUN apk upgrade --no-cache && \
    apk add --no-cache python3 python3-dev py3-pip build-base

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" && \
    python -m pip install --no-cache-dir . && \
    python -m pip uninstall -y setuptools pip

# Final runtime image contains only Python runtime libraries and the application venv.
FROM alpine:3.23.8@sha256:5c216c9767869f6ec7c4718977601d52f087c5c7bf2fdfb198e403f6b48ec40d AS runner

WORKDIR /app

RUN apk upgrade --no-cache && \
    apk add --no-cache python3 ca-certificates tzdata && \
    addgroup -S appgroup && \
    adduser -S -D -H -G appgroup -s /sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY templates/ ./templates/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY deploy/ ./deploy/

RUN mkdir -p storage/documents && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=5)" || exit 1

CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--timeout", "60", "--limit-request-line", "8190", "--limit-request-field_size", "8190", "--access-logfile", "-", "--error-logfile", "-"]
