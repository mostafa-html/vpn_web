#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────
set -e

echo "[entrypoint] Running database migrations..."
python manage.py migrate --noinput

echo "[entrypoint] Starting Gunicorn..."
exec gunicorn vShop.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 3 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
