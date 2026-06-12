# ─────────────────────────────────────────────────────────────────────────
# vShop – Django VPN Billing Platform
# Multi-stage build: keeps the final image lean by separating
# build-time tools (pip wheel compilation) from runtime.
# ─────────────────────────────────────────────────────────────────────────

# ═══ Stage 1: dependency builder ══════════════════════════════════════════════════════
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# System deps needed to compile certain wheels (Pillow, psycopg2, etc.)
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

# Runtime-only system libs (no gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

# Install wheels built in stage 1
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

# Create non-root app user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy project source
COPY --chown=appuser:appuser . .

# Create directories that need to exist at runtime
RUN mkdir -p /app/staticfiles /app/protected_media /app/static && \
    chown -R appuser:appuser /app/staticfiles /app/protected_media /app/static

# NOTE: collectstatic is intentionally NOT run here.
# It requires the full Django app registry (including Celery + Redis)
# which is unavailable at image build time.
# It is instead run in docker-entrypoint.sh on every container start.

USER appuser

COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
