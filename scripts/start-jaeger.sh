#!/usr/bin/env bash
# NexusOps — local tracing backend (Jaeger all-in-one).
#
# Idempotent: re-running stops/removes a stale container and starts fresh.
# Spans arrive over OTLP/HTTP on :4318 (set in .env via
# OTEL_EXPORTER_OTLP_ENDPOINT), query UI on :16686.
#
#   scripts/start-jaeger.sh            start (or restart) Jaeger
#   open http://localhost:16686        the trace explorer
set -euo pipefail

NAME="nexusops-jaeger"

if docker inspect "$NAME" >/dev/null 2>&1; then
  echo "[jaeger] container exists -> restarting"
  docker rm -f "$NAME" >/dev/null
fi

echo "[jaeger] starting all-in-one (OTLP HTTP :4318, UI :16686)"
docker run -d --name "$NAME" \
  -e COLLECTOR_OTLP_ENABLED=true \
  -p 16686:16686 -p 4318:4318 -p 4317:4317 \
  jaegertracing/all-in-one:latest

echo "[jaeger] up. Waiting for query API..."
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null http://localhost:16686/; then
    break
  fi
  sleep 1
done
curl -sf -o /dev/null http://localhost:16686/ && echo "[jaeger] READY — open http://localhost:16686" || echo "[jaeger] not responding yet"