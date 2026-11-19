#!/usr/bin/env bash
# Smoke-test every simulator endpoint against a running stack.
#
# Usage:
#   scripts/smoke_test.sh [simulator_base_url] [backend_base_url]
#
# Defaults to the docker-compose ports. Exits non-zero if any check fails.

set -uo pipefail

SIM_URL="${1:-http://localhost:8000}"
BACKEND_URL="${2:-http://localhost:8001}"

PASS=0
FAIL=0

check() {
  local label="$1" url="$2" expected="${3:-200}"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "$url")"
  if [[ "$code" == "$expected" ]]; then
    printf '  ok    %-46s HTTP %s\n' "$label" "$code"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s HTTP %s (expected %s)\n' "$label" "$code" "$expected"
    FAIL=$((FAIL + 1))
  fi
}

post() {
  local label="$1" url="$2" body="${3:-{\}}" expected="${4:-200}"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$body" "$url")"
  if [[ "$code" == "$expected" ]]; then
    printf '  ok    %-46s HTTP %s\n' "$label" "$code"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s HTTP %s (expected %s)\n' "$label" "$code" "$expected"
    FAIL=$((FAIL + 1))
  fi
}

echo "Simulator reads  -> $SIM_URL"
for ep in health instance regions depots stations routes supply-arrivals \
          events allocations metrics demand-history; do
  check "GET /v1/$ep" "$SIM_URL/v1/$ep"
done

echo
echo "Simulator admin reads"
for ep in audit faults events stats; do
  check "GET /admin/$ep" "$SIM_URL/admin/$ep"
done

echo
echo "Simulator writes"
post "POST /v1/allocations" "$SIM_URL/v1/allocations" \
  '{"idempotency_key":"smoke-001","source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","route_id":"route-gazipur-mirpur","fuel_type":"DIESEL","quantity":1000}' \
  201
post "POST /v1/allocations (replay)" "$SIM_URL/v1/allocations" \
  '{"idempotency_key":"smoke-001","source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","route_id":"route-gazipur-mirpur","fuel_type":"DIESEL","quantity":1000}' \
  201
post "POST /v1/allocations (bad route)" "$SIM_URL/v1/allocations" \
  '{"idempotency_key":"smoke-002","source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","route_id":"route-gazipur-tongi","fuel_type":"DIESEL","quantity":1000}' \
  409
post "POST /admin/step" "$SIM_URL/admin/step" '{}' 200
post "POST /admin/pause" "$SIM_URL/admin/pause" '{}' 200
post "POST /admin/faults/clear" "$SIM_URL/admin/faults/clear" '{}' 200

echo
echo "Backend"
check "GET /health" "$BACKEND_URL/health"
check "GET /api/state" "$BACKEND_URL/api/state"
check "GET /metrics" "$BACKEND_URL/metrics"

echo
echo "passed: $PASS   failed: $FAIL"
[[ "$FAIL" -eq 0 ]]
