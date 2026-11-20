#!/usr/bin/env bash
# Run the fault-injection scenarios against the live simulator and record what
# the platform actually did.
#
# Usage: scripts/fault_test.sh [simulator_base_url] [backend_base_url]
#
# Writes the transcript to stdout. The result is checked in as
# docs/fault-tests.md.

set -uo pipefail

SIM_URL="${1:-http://localhost:8000}"
BACKEND_URL="${2:-http://localhost:8001}"

PASS=0
FAIL=0

check() {
  local label="$1" url="$2" expected="${3:-200}"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")"
  if [[ "$code" == "$expected" ]]; then
    printf '  ok    %-52s HTTP %s\n' "$label" "$code"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-52s HTTP %s (expected %s)\n' "$label" "$code" "$expected"
    FAIL=$((FAIL + 1))
  fi
}

json_field() {
  # json_field <url> <python-expression on the parsed body>
  curl -s "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print($2)"
}

banner() {
  printf '\n=== %s ===\n' "$1"
}

clear_faults() {
  curl -s -X POST "$SIM_URL/admin/faults/clear" -H 'Content-Type: application/json' -d '{}' > /dev/null
}

banner "1. Baseline (no faults)"
check "GET /v1/stations" "$SIM_URL/v1/stations"
check "GET /v1/stream" "$SIM_URL/v1/stream"
check "GET /api/stations (backend)" "$BACKEND_URL/api/stations"

banner "2. stale_data fault -> X-Simulator-Stale header"
curl -s -X POST "$SIM_URL/admin/faults" -H 'Content-Type: application/json' \
  -d '{"type":"stale_data","duration_seconds":20,"parameters":{}}' > /dev/null
sleep 1
STALE_HEADER="$(curl -s -D - -o /dev/null "$SIM_URL/v1/stations" | grep -i '^x-simulator-stale' | tr -d '\r')"
printf '  response header: %s\n' "${STALE_HEADER:-<none>}"
if [[ "$STALE_HEADER" == *"true"* ]]; then
  printf '  ok    stale_data fault sets X-Simulator-Stale: true\n'
  PASS=$((PASS + 1))
else
  printf '  FAIL  stale_data fault did not set the header\n'
  FAIL=$((FAIL + 1))
fi
sleep 7
printf '  backend stale flag: %s\n' \
  "$(json_field "$BACKEND_URL/api/stations" "d.get('stale')")"
clear_faults

banner "3. stream_disconnect fault -> GET /v1/stream answers 503"
curl -s -X POST "$SIM_URL/admin/faults" -H 'Content-Type: application/json' \
  -d '{"type":"stream_disconnect","duration_seconds":15,"parameters":{}}' > /dev/null
sleep 1
check "GET /v1/stream with stream_disconnect" "$SIM_URL/v1/stream" 503
clear_faults

banner "4. unavailable fault -> /v1/* answers 503"
curl -s -X POST "$SIM_URL/admin/faults" -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":15,"parameters":{}}' > /dev/null
sleep 1
check "GET /v1/stations with unavailable" "$SIM_URL/v1/stations" 503
check "GET /v1/health (bypasses faults)" "$SIM_URL/v1/health" 200
check "GET /admin/faults (bypasses faults)" "$SIM_URL/admin/faults" 200
clear_faults

banner "5. latency fault -> responses are slow but succeed"
curl -s -X POST "$SIM_URL/admin/faults" -H 'Content-Type: application/json' \
  -d '{"type":"latency","duration_seconds":10,"parameters":{"delay_ms":600}}' > /dev/null
sleep 1
START=$(date +%s%N)
check "GET /v1/stations with latency" "$SIM_URL/v1/stations" 200
END=$(date +%s%N)
ELAPSED_MS=$(( (END - START) / 1000000 ))
printf '  elapsed: %sms\n' "$ELAPSED_MS"
clear_faults

banner "6. Backend still serves the API while the simulator is faulted"
curl -s -X POST "$SIM_URL/admin/faults" -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":12,"parameters":{}}' > /dev/null
sleep 1
check "GET /api/stations (backend, simulator faulted)" "$BACKEND_URL/api/stations" 200
printf '  backend degraded flag: %s\n' \
  "$(json_field "$BACKEND_URL/api/stations" "d.get('degraded')")"
clear_faults

banner "Result"
printf 'passed: %s   failed: %s\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
