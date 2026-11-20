#!/usr/bin/env bash
# The Day 2 definition-of-done drill: stop the simulator container mid-run,
# confirm the backend stays up in degraded mode, then restart it and confirm
# the platform recovers on its own.
#
# Usage: scripts/degraded_mode_drill.sh [backend_base_url]
#
# Exits non-zero if the backend does not degrade, or does not recover.

set -uo pipefail

BACKEND_URL="${1:-http://localhost:8001}"

PASS=0
FAIL=0

check() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf '  ok    %-52s %s\n' "$label" "$actual"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-52s %s (expected %s)\n' "$label" "$actual" "$expected"
    FAIL=$((FAIL + 1))
  fi
}

field() {
  # field <url> <python-expression on the parsed body>
  curl -s --max-time 10 "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print($2)" 2>/dev/null
}

echo "=== 1. Baseline ==="
check "simulator component status" "$(field "$BACKEND_URL/health" "d['components']['simulator']['status']")" "ok"
check "sync degraded flag" "$(field "$BACKEND_URL/health" "d['sync']['degraded']")" "False"

echo
echo "=== 2. Stopping the simulator container ==="
docker compose stop simulator
echo "    stopped; waiting for the poller to notice..."

DEGRADED=""
for i in $(seq 1 24); do
  sleep 5
  DEGRADED="$(field "$BACKEND_URL/health" "d['sync']['degraded']")"
  if [[ "$DEGRADED" == "True" ]]; then
    echo "    degraded after $((i * 5))s"
    break
  fi
done

check "sync degraded flag" "$DEGRADED" "True"
check "api still answers" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BACKEND_URL/api/stations")" "200"
check "served payload flagged degraded" "$(field "$BACKEND_URL/api/stations" "d['degraded']")" "True"
printf '  info  last-known state age: %ss\n' \
  "$(field "$BACKEND_URL/api/stations" "d['age_seconds']")"
printf '  info  stations still served: %s\n' \
  "$(field "$BACKEND_URL/api/stations" "len(d['stations'])")"

echo
echo "=== 3. Restarting the simulator container ==="
docker compose start simulator
echo "    started; waiting for automatic recovery..."

RECOVERED=""
for i in $(seq 1 24); do
  sleep 5
  STATUS="$(field "$BACKEND_URL/health" "d['status']")"
  DEGRADED="$(field "$BACKEND_URL/health" "d['sync']['degraded']")"
  if [[ "$STATUS" == "ok" && "$DEGRADED" == "False" ]]; then
    RECOVERED="$((i * 5))"
    echo "    recovered after ${RECOVERED}s"
    break
  fi
done

if [[ -n "$RECOVERED" ]]; then
  check "recovered automatically" "yes (${RECOVERED}s)" "yes (${RECOVERED}s)"
else
  check "recovered automatically" "no" "yes"
fi

check "sync degraded flag" "$(field "$BACKEND_URL/health" "d['sync']['degraded']")" "False"
check "health status" "$(field "$BACKEND_URL/health" "d['status']")" "ok"

echo
echo "=== Result ==="
printf 'passed: %s   failed: %s\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
