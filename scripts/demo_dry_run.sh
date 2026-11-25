#!/usr/bin/env bash
#
# Dry-run the demo end to end, and time it.
#
# Every command in docs/demo-script.md is executed here, in order, against
# the running stack. The point is not that the commands work — it is that
# they work *in the order the demo runs them*, and that the whole thing
# fits in the time the demo claims.
#
#   scripts/demo_dry_run.sh
#
# It prints each step with its elapsed time, and it fails loudly if a
# step does not produce what the demo script claims it produces.

set -uo pipefail

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BACKEND="${BACKEND:-http://localhost:${BACKEND_PORT:-8001}}"
SIM="${SIM:-http://localhost:${SIMULATOR_PORT:-8000}}"

START=$(date +%s)
STEP_NO=0
FAILURES=0

say() {
    STEP_NO=$((STEP_NO + 1))
    NOW=$(date +%s)
    printf '\n\033[1m[%2ds] Step %2d — %s\033[0m\n' "$((NOW - START))" "$STEP_NO" "$1"
}

fail() {
    printf '   \033[31mFAIL\033[0m %s\n' "$1"
    FAILURES=$((FAILURES + 1))
}

ok() {
    printf '   \033[32mok\033[0m   %s\n' "$1"
}

# --- 0. reset --------------------------------------------------------------

say "Reset the world"
if scripts/reset_demo.sh --wait > /tmp/dry-reset.log 2>&1; then
    ok "the world reset and started"
else
    fail "the reset failed — see /tmp/dry-reset.log"
    tail -5 /tmp/dry-reset.log
fi

# --- 1. normal operations --------------------------------------------------

say "Normal operations — read the state"
STATE=$(curl -fsS --max-time 10 "$BACKEND/api/state")
if [ -n "$STATE" ]; then
    ok "$(echo "$STATE" | jq -r '"tick \(.instance.tick), status \(.instance.status)"')"
    ok "service level $(echo "$STATE" | jq -r '.metrics.service_level // "n/a"')"
else
    fail "the state did not come back"
fi

# The backend's sync interval is 15 seconds, so right after a reset the
# cached state is still the previous world's for a moment. The demo waits
# for the world to catch up before it starts talking about numbers.
say "Waiting for the backend's sync to catch up"
for _ in $(seq 1 20); do
    LIVE=$(curl -fsS --max-time 10 "$SIM/v1/instance" | jq -r '.tick' 2>/dev/null || echo 0)
    CACHED=$(echo "$STATE" | jq -r '.instance.tick' 2>/dev/null || echo 0)
    [ "${LIVE:-0}" -le $((CACHED + 2)) ] && break
    sleep 1
    STATE=$(curl -fsS --max-time 10 "$BACKEND/api/state")
done
ok "the backend is at tick $(echo "$STATE" | jq -r '.instance.tick'), the simulator at $LIVE"

# --- 2. the dashboard ------------------------------------------------------

say "The dashboard is served"
DASH_PORT="${FRONTEND_PORT:-5173}"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://localhost:${DASH_PORT}/")
if [ "$CODE" = "200" ]; then
    ok "the dashboard answers 200"
else
    fail "the dashboard answered $CODE"
fi

# --- 3. demand is rising ---------------------------------------------------

say "Demand is rising — inject a 3.0 spike"
SPIKE=$(curl -fsS --max-time 10 -X POST "$BACKEND/api/simulation/crisis" \
    -H 'Content-Type: application/json' \
    -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}')
if [ -n "$SPIKE" ]; then
    ok "$(echo "$SPIKE" | jq -r '.event.type') injected"
else
    fail "the spike was not injected"
fi

# --- 4. risk is detected ---------------------------------------------------

say "Risk is detected — wait for the anomaly"
DETECTED=0
for _ in $(seq 1 20); do
    ANOMALIES=$(curl -fsS --max-time 10 "$BACKEND/api/anomalies" 2>/dev/null || echo '[]')
    COUNT=$(echo "$ANOMALIES" | jq -r '[.anomalies[]?] | length' 2>/dev/null || echo 0)
    if [ "${COUNT:-0}" -gt 0 ]; then
        DETECTED=1
        ok "$COUNT anomal$( [ "$COUNT" -eq 1 ] && echo y || echo ies) detected"
        break
    fi
    sleep 1
done
[ "$DETECTED" -eq 1 ] || fail "no anomaly was detected within 20 seconds"

# --- 5. a shortage is predicted --------------------------------------------

say "A shortage is predicted"
CRITICAL=$(curl -fsS --max-time 10 "$BACKEND/api/risk" 2>/dev/null \
    | jq -r '[.risks[]? | select(.risk_level=="CRITICAL")] | length' 2>/dev/null || echo 0)
if [ "${CRITICAL:-0}" -gt 0 ]; then
    ok "$CRITICAL critical risk assessment(s)"
else
    fail "no critical risk assessment"
fi

FORECAST=$(curl -fsS --max-time 10 \
    "$BACKEND/api/forecast?station_id=station-mirpur&fuel_type=DIESEL" 2>/dev/null || echo '{}')
MODEL=$(echo "$FORECAST" | jq -r '.forecasts[0].model // "n/a"' 2>/dev/null || echo "n/a")
ok "the forecast model is $MODEL"
CONFIDENCE=$(echo "$FORECAST" | jq -r '.forecasts[0].confidence // "n/a"' 2>/dev/null || echo "n/a")
ok "the forecast confidence is $CONFIDENCE"

# --- 6. a recommendation is made -------------------------------------------

say "A recommendation is made"
RECS=$(curl -fsS --max-time 10 "$BACKEND/api/recommendations")
COUNT=$(echo "$RECS" | jq -r '.recommendations | length' 2>/dev/null || echo 0)
if [ "${COUNT:-0}" -gt 0 ]; then
    ok "$COUNT recommendation(s) on the table"
else
    fail "no recommendation was made"
fi

REC_ID=$(echo "$RECS" | jq -r '.recommendations[0].id // empty')
if [ -z "$REC_ID" ]; then
    fail "no recommendation id to work with"
    REC_ID="none"
else
    ok "the first one is $REC_ID"
fi

# --- 7. the operator inspects it -------------------------------------------

say "The operator inspects it — simulate writes nothing"
BEFORE=$(curl -fsS --max-time 10 "$BACKEND/api/recommendations/$REC_ID" \
    | jq -r '.status' 2>/dev/null || echo "?")
SIM_RESULT=$(curl -fsS --max-time 10 -X POST "$BACKEND/api/recommendations/$REC_ID/simulate")
AFTER=$(curl -fsS --max-time 10 "$BACKEND/api/recommendations/$REC_ID" \
    | jq -r '.status' 2>/dev/null || echo "?")
if [ "$BEFORE" = "$AFTER" ]; then
    ok "the status did not change ($BEFORE) — the what-if wrote nothing"
else
    fail "the status changed from $BEFORE to $AFTER"
fi

if [ -n "$SIM_RESULT" ]; then
    ok "the projection came back"
fi

# --- 8. the operator approves ----------------------------------------------

say "The operator approves"
APPROVED=$(curl -fsS --max-time 15 -X POST "$BACKEND/api/recommendations/$REC_ID/approve" \
    -H 'Content-Type: application/json' -d '{"decided_by":"dry-run"}')
if [ -n "$APPROVED" ]; then
    ok "the recommendation is $(echo "$APPROVED" | jq -r '.status // "approved"')"
    ERR=$(echo "$APPROVED" | jq -r '.error_message // empty')
    [ -n "$ERR" ] && fail "the approval reported: $ERR"
else
    fail "the approval failed"
fi

# The allocation reaches the simulator immediately — the approve call
# posts it. It appears in the *read* endpoint one sync cycle later, so
# the check waits for the sync rather than failing on a race.
ALLOCS=$(curl -fsS --max-time 10 "$BACKEND/api/allocations" 2>/dev/null || echo '{"allocations":[]}')
ACOUNT=$(echo "$ALLOCS" | jq -r '.allocations | length' 2>/dev/null || echo 0)
for _ in $(seq 1 10); do
    [ "${ACOUNT:-0}" -gt 0 ] && break
    sleep 2
    ALLOCS=$(curl -fsS --max-time 10 "$BACKEND/api/allocations" 2>/dev/null || echo '{"allocations":[]}')
    ACOUNT=$(echo "$ALLOCS" | jq -r '.allocations | length' 2>/dev/null || echo 0)
done
if [ "${ACOUNT:-0}" -gt 0 ]; then
    ok "$ACOUNT allocation(s) in the simulator"
else
    fail "no allocation reached the simulator"
fi

# --- 9. the crisis ---------------------------------------------------------

say "The crisis — cut the fast route"
DISRUPT=$(curl -fsS --max-time 10 -X POST "$BACKEND/api/simulation/crisis" \
    -H 'Content-Type: application/json' \
    -d '{"type":"route_disruption","duration_ticks":16,
         "parameters":{"route_ids":["route-gazipur-mirpur"]}}')
if [ -n "$DISRUPT" ]; then
    ok "$(echo "$DISRUPT" | jq -r '.event.type') injected"
else
    fail "the disruption was not injected"
fi

# --- 10. the platform adapts -----------------------------------------------

say "The platform adapts — re-propose"
curl -fsS --max-time 20 -X POST "$BACKEND/api/recommendations/propose" > /tmp/dry-repropose.json 2>&1
RECOUNT=$(jq -r '.recommendations | length' /tmp/dry-repropose.json 2>/dev/null || echo 0)
if [ "${RECOUNT:-0}" -gt 0 ]; then
    ok "$RECOUNT recommendation(s) after the disruption"
else
    fail "the platform proposed nothing after the disruption"
fi

# --- 11. a failure is injected ---------------------------------------------

say "A failure is injected — break the prediction model"
FAULT=$(curl -fsS --max-time 10 -X POST "$BACKEND/api/model/fault" \
    -H 'Content-Type: application/json' -d '{"fault":"predict_error"}')
if echo "$FAULT" | jq -e '.prediction_fault' > /dev/null 2>&1; then
    ok "the prediction fault is set to $(echo "$FAULT" | jq -r '.prediction_fault')"
else
    fail "the fault was not set"
fi

# --- 12. monitoring catches it ---------------------------------------------

say "Monitoring catches it"
# The signal that the model is broken is on /api/risk: the active model
# flips to "baseline" and the fallback activations name the reason.
CAUGHT=0
for _ in $(seq 1 30); do
    RISK=$(curl -fsS --max-time 10 "$BACKEND/api/risk" 2>/dev/null || echo '{}')
    ACTIVE=$(echo "$RISK" | jq -r '.active_model // "unknown"' 2>/dev/null || echo unknown)
    REASONS=$(echo "$RISK" | jq -c '.fallback_activations // {}' 2>/dev/null || echo '{}')
    if [ "$ACTIVE" = "baseline" ]; then
        CAUGHT=1
        ok "the active model is baseline, and the fallback activations are $REASONS"
        break
    fi
    sleep 1
done
[ "$CAUGHT" -eq 1 ] || fail "the platform did not notice the broken model within 30 seconds"

HEALTH=$(curl -fsS --max-time 10 "$BACKEND/api/service-health" 2>/dev/null || echo '{}')
COMPONENTS=$(echo "$HEALTH" | jq -r '.components | keys | join(", ")' 2>/dev/null || echo "n/a")
ok "the service health panel reports: $COMPONENTS"
PRED=$(echo "$HEALTH" | jq -r '.components.prediction.status // .components.forecasting.status // "n/a"' 2>/dev/null || echo "n/a")
ok "the prediction component is $PRED"

# --- 13. the fallback takes over -------------------------------------------

say "The fallback takes over"

# Breaking the *model* makes the forecast layer fall back. Breaking the
# whole *evaluation* is what makes the decision layer fall back to the
# rule-based policy — which is the fallback the demo is about.
curl -fsS --max-time 10 -X POST "$BACKEND/api/model/fault" \
    -H 'Content-Type: application/json' -d '{"fault":"evaluate_error"}' > /dev/null 2>&1

POLICY=0
for _ in $(seq 1 30); do
    HEALTH=$(curl -fsS --max-time 10 "$BACKEND/api/service-health" 2>/dev/null || echo '{}')
    POLICY=$(echo "$HEALTH" | jq -r '.components.decision_engine.fallback_active // false' 2>/dev/null || echo false)
    [ "$POLICY" = "true" ] && break
    sleep 1
done
if [ "$POLICY" = "true" ]; then
    ok "the decision engine is on the rule-based policy"
else
    fail "the decision layer did not fall back within 30 seconds"
fi

sleep 5
RECS=$(curl -fsS --max-time 10 "$BACKEND/api/recommendations")
FALLBACK_RECS=$(echo "$RECS" | jq -r '[.recommendations[]? | select(.used_fallback_policy == true)] | length' 2>/dev/null || echo 0)
if [ "${FALLBACK_RECS:-0}" -gt 0 ]; then
    ok "$FALLBACK_RECS fallback recommendation(s)"
else
    fail "no recommendation came from the fallback policy"
fi

REVIEW=$(echo "$RECS" | jq -r '[.recommendations[]? | select(.needs_human_review == true)] | length' 2>/dev/null || echo 0)
if [ "${REVIEW:-0}" -gt 0 ]; then
    ok "$REVIEW recommendation(s) flagged for human review"
else
    fail "no fallback recommendation asked for a human"
fi

# --- 14. operations continue ------------------------------------------------

say "Operations continue — repair the model"
curl -fsS --max-time 10 -X POST "$BACKEND/api/model/fault" \
    -H 'Content-Type: application/json' -d '{"fault":null}' > /dev/null 2>&1
RECOVERED=0
for _ in $(seq 1 40); do
    RISK=$(curl -fsS --max-time 10 "$BACKEND/api/risk" 2>/dev/null || echo '{}')
    ACTIVE=$(echo "$RISK" | jq -r '.active_model // "unknown"' 2>/dev/null || echo unknown)
    if [ "$ACTIVE" = "learned" ]; then
        RECOVERED=1
        ok "the model is back in service ($ACTIVE)"
        break
    fi
    sleep 1
done
[ "$RECOVERED" -eq 1 ] || fail "the model did not come back within 40 seconds"

DECISIONS=$(curl -fsS --max-time 10 "$BACKEND/api/decision-history" 2>/dev/null \
    | jq -r '.decisions | length' 2>/dev/null || echo 0)
ok "$DECISIONS decision(s) in the history"

# --- the summary ------------------------------------------------------------

END=$(date +%s)
printf '\n\033[1m==> The dry run took %d seconds\033[0m\n' "$((END - START))"
if [ "$FAILURES" -eq 0 ]; then
    printf '\033[32mEvery step produced what the demo script claims.\033[0m\n'
    exit 0
fi
printf '\033[31m%d step(s) did not produce what the demo script claims.\033[0m\n' "$FAILURES"
exit 1
