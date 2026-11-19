#!/usr/bin/env bash
# Bring the stack up, wait for it to become healthy, and run the smoke test.
#
# Usage: scripts/dev_up.sh [--build]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "==> .env missing; creating it from .env.example"
  cp .env.example .env
  echo "    Edit .env and change the passwords before exposing this stack."
fi

echo "==> Starting the stack"
if [[ "${1:-}" == "--build" ]]; then
  docker compose up -d --build
else
  docker compose up -d
fi

echo "==> Waiting for health"
SIM_URL="http://localhost:${SIMULATOR_PORT:-8000}"
BACKEND_URL="http://localhost:${BACKEND_PORT:-8001}"

for i in $(seq 1 60); do
  if curl -fsS "$BACKEND_URL/health" > /dev/null 2>&1; then
    echo "    backend healthy after ${i}s"
    break
  fi
  if [[ "$i" -eq 60 ]]; then
    echo "    backend did NOT become healthy; last logs:"
    docker compose logs --tail 40 backend
    exit 1
  fi
  sleep 1
done

echo
curl -s "$BACKEND_URL/health"
echo
echo "==> Smoke test"
./scripts/smoke_test.sh "$SIM_URL" "$BACKEND_URL"
