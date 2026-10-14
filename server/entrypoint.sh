#!/bin/sh
set -eu
mkdir -p "$DATA_DIR/media/dishes" "$DATA_DIR/media/imports" "$DATA_DIR/media/scans"
cd /app
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'
