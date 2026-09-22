# syntax=docker/dockerfile:1
# NopeList sandbox image.
#
# This container IS the Docker sandbox: untrusted Luma event HTML and the Playwright /
# Bright Data browser session are handled in here, isolated from the host, as a
# non-root user. Playwright only needs its Python client: the browser itself runs
# remotely on Bright Data's Browser API (connect_over_cdp), so no Chromium or
# system browser deps are installed. If you ever switch to a local browser, swap the
# base image for mcr.microsoft.com/playwright/python:v1.63.0-noble
# (https://playwright.dev/python/docs/docker) and run `playwright install chromium`.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is needed by a few cognee transitive deps at install time; curl for healthchecks/debug.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root sandbox user.
RUN groupadd --gid 10001 nopelist \
 && useradd --uid 10001 --gid nopelist --create-home --shell /usr/sbin/nologin nopelist

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=nopelist:nopelist src/ ./src/
COPY --chown=nopelist:nopelist data/ ./data/

# Cognee state (graph db, vector store, logs) and the fastembed model cache live under
# /app/data so they persist through the ./data bind mount in docker-compose.yml.
ENV DATA_ROOT_DIRECTORY=/app/data/.cognee/data \
    SYSTEM_ROOT_DIRECTORY=/app/data/.cognee/system \
    CACHE_ROOT_DIRECTORY=/app/data/.cognee/cache \
    COGNEE_LOGS_DIR=/app/data/.cognee/logs \
    FASTEMBED_CACHE_PATH=/app/data/.fastembed \
    HOME=/home/nopelist

RUN mkdir -p /app/data/cache /app/data/.cognee /app/data/.fastembed \
 && chown -R nopelist:nopelist /app

USER nopelist

# Optional: pre-warm the local embedding model (~90MB) into the image so the first
# demo run does not download it on stage wifi. Best-effort: build continues if it fails
# (e.g. no network at build time). Disable with: --build-arg PREWARM_EMBEDDINGS=0
ARG PREWARM_EMBEDDINGS=1
RUN if [ "$PREWARM_EMBEDDINGS" = "1" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')" \
      || echo 'WARN: fastembed pre-warm skipped (no network?) - model downloads on first run'; \
    fi

# Default: the full Strands agent loop. Override with `--no-llm` for the deterministic path.
ENTRYPOINT ["python", "-m", "src.agent"]
CMD []
