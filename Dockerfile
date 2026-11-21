# Lyftr backend — FastAPI + LiteLLM + SQLite.
# Base image is a build argument so it can be pinned to a digest in CI:
#   docker build --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest> backend
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- build stage: resolve pinned dependencies into a self-contained venv --------------
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} +

# ---- runtime stage: only the venv, the app package and a non-root user -----------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    DATABASE_URL=sqlite:////data/app.db \
    APP_ENV=production \
    PORT=8000

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app \
 && mkdir -p /data /app \
 && chown app:app /data

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app docker/healthcheck.py ./docker/healthcheck.py

USER 10001:10001
# SQLite (app.db + WAL files) lives here; mount a named volume to persist it.
VOLUME ["/data"]
EXPOSE 8000

# Liveness: the process answers /health. 503 (e.g. keys not configured) still counts as alive;
# readiness is reported by /health itself.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "/app/docker/healthcheck.py"]

# Exactly one worker: the LiteLLM cache, key health and per-session locks are in-memory.
# PORT defaults to 8000 (hosting platforms may inject their own).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1 --no-server-header --proxy-headers --forwarded-allow-ips '*'"]
