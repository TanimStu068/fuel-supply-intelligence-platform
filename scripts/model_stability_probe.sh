#!/usr/bin/env bash
#
# How stable is the learned model's acceptance?
#
# The platform only uses the learned model when it has demonstrably beaten the
# rule-based baseline over the forecast horizon. On a *flat* world the rule is
# genuinely the better predictor, so the model is correctly rejected. The
# question this answers is: under what world does the model stay trusted?
#
# Scenario A — flat world, retrain every 40 ticks.
# Scenario B — one sustained level shift, retrain every 40 ticks.
#
# Usage: scripts/model_stability_probe.sh

set -uo pipefail

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BACKEND="${BACKEND:-http://localhost:${BACKEND_PORT:-8001}}"
SIM="${SIM:-http://localhost:${SIMULATOR_PORT:-8000}}"

tick() { curl -fsS "$SIM/v1/instance" | jq -r '.tick'; }

wait_tick() {
    local target="$1"
    while [ "$(tick)" -lt "$target" ]; do sleep 1; done
}

report() {
    local label="$1"
    local status
    status=$(curl -fsS "$BACKEND/api/model-status")
    echo "  $label  tick=$(tick)  active=$(echo "$status" | jq -r '.active_model')  \
beats=$(echo "$status" | jq -r '.learned_model.beats_baseline_at_horizon // "n/a"')  \
model=$(echo "$status" | jq -r '.learned_model.horizon_total_mae_liters // "n/a"')  \
base=$(echo "$status" | jq -r '.learned_model.baseline_horizon_total_mae_liters // "n/a"')"
}

reset_world() {
    curl -s -X POST "$SIM/admin/reset" > /dev/null
    curl -s -X POST "$SIM/admin/run" > /dev/null
    docker compose exec -T postgres psql -U "${POSTGRES_USER:-fuel}" -d "${POSTGRES_DB:-fuel}" -q -c "
        DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
        DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
        DELETE FROM prediction_evaluations; DELETE FROM demand_observations;" 2>/dev/null
    curl -s -X POST "$BACKEND/api/model/fault" \
        -H 'Content-Type: application/json' -d '{"fault":null}' > /dev/null
}

train() {
    curl -s -X POST "$BACKEND/api/model/train" | jq -c '{trained, training_rows}' 2>/dev/null
}

echo "=============================================================="
echo "Scenario A — a flat world (no events at all)"
echo "=============================================================="
reset_world
wait_tick 120
sleep 15
for round in 1 2 3 4 5; do
    echo "round $round:"
    train
    report "  after train"
    wait_tick $(( $(tick) + 40 ))
    sleep 15
done

echo ""
echo "=============================================================="
echo "Scenario B — one sustained level shift (a long demand spike)"
echo "=============================================================="
reset_world
wait_tick 60
echo "injecting a sustained spike through the simulator's admin API"
curl -s -X POST "$SIM/admin/events" \
    -H 'Content-Type: application/json' \
    -d "{\"type\":\"demand_spike\",\"start_tick\":$(tick),\"duration_ticks\":4000,\"parameters\":{\"multiplier\":1.6}}" \
    | jq -c '{type, start_tick, end_tick}'
wait_tick 140
sleep 15
for round in 1 2 3 4 5; do
    echo "round $round:"
    train
    report "  after train"
    wait_tick $(( $(tick) + 40 ))
    sleep 15
done
