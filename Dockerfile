# vShop – Django VPN Billing Platform

# ═══ Stage 1: builder ═════════════════════════════════════════════════════════════════
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ═══ Stage 2: runtime ═══════════════════════════════════════════════════════════════
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=vShop.settings

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libjpeg62-turbo curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app
COPY --chown=appuser:appuser . .

RUN mkdir -p /app/staticfiles /app/protected_media && \
    chown -R appuser:appuser /app/staticfiles /app/protected_media

# Write the entrypoint directly so line endings are always LF,
# regardless of the host OS (Windows CRLF would break /bin/sh).
RUN printf '#!/bin/sh\nset -e\necho "[entrypoint] Running migrations..."\npython manage.py migrate --noinput\necho "[entrypoint] Collecting static files..."\npython manage.py collectstatic --noinput --clear\necho "[entrypoint] Starting Gunicorn..."\nexec gunicorn vShop.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120 --access-logfile - --error-logfile -\n' > /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

USER appuser
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
