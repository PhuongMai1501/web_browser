#!/bin/bash
# start_redis.sh — Start Redis server (if not already running via systemd)
# Skip this if Redis is already installed as a system service.

REDIS_PORT=6379
REDIS_DATADIR="/var/lib/redis"

if redis-cli ping 2>/dev/null | grep -q PONG; then
  echo "[Redis] Already running on port $REDIS_PORT"
  exit 0
fi

echo "[Redis] Starting redis-server on port $REDIS_PORT..."
mkdir -p "$REDIS_DATADIR"
exec redis-server \
  --port "$REDIS_PORT" \
  --daemonize no \
  --loglevel notice \
  --dir "$REDIS_DATADIR"
