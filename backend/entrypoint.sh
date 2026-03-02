#!/usr/bin/env sh
set -eu

if [ -n "${PORT:-}" ]; then
  export SERVER_PORT="${SERVER_PORT:-$PORT}"
fi

: "${SERVER_HOST:=0.0.0.0}"
: "${SERVER_PORT:=8000}"
: "${UVICORN_WORKERS:=1}"
: "${UVICORN_LOG_LEVEL:=info}"

exec uvicorn app.main:app \
  --host "$SERVER_HOST" \
  --port "$SERVER_PORT" \
  --workers "$UVICORN_WORKERS" \
  --log-level "$UVICORN_LOG_LEVEL" \
  --proxy-headers \
  --forwarded-allow-ips="*"
