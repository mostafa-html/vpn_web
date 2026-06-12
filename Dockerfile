# ─────────────────────────────────────────────────────────────────────────
# vShop – Django VPN Billing Platform
# Multi-stage build: keeps the final image lean.
# ─────────────────────────────────────────────────────────────────────────

# ═══ Stage 1: dependency builder ══════════════════════════════════════════════════════
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ═══ Stage 2: runtime image ═══════════════════════════════════════════════════════════════
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=vShop.settings

# curl added for the docker-compose healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libjpeg62-turbo \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

COPY --chown=appuser:appuser . .

RUN mkdir -p /app/staticfiles /app/protected_media /app/static && \
    chown -R appuser:appuser /app/staticfiles /app/protected_media /app/static

USER appuser

COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
