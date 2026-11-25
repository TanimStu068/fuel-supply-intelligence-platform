#!/usr/bin/env bash
#
# How much history does the learned model need before it is trusted?
#
# The demo needs the model to be in use, and it needs the world's tanks to
# still hold fuel when the story starts. Those two wants pull against each
# other: history comes from the world running, and the world running empties
# the tanks.
#
# This measures the first point at which the platform actually trusts the
# learned model, so the reset script can wait for a *condition* rather than
# guess at a duration.
#
# Usage: scripts/model_trust_threshold.sh

set -uo pipefail

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BACKEND="${BACKEND:-http://localhost:${BACKEND_PORT:-8001}}"
SIM="${SIM:-http://localhost:${SIMULATOR_PORT:-8000}}"

tick() { curl -fsS "$SIM/v1/instance" | jq -r '.tick'; }

echo "Resetting the world..."
curl -s -X POST "$SIM/admin/reset" > /dev/null
curl -s -X POST "$SIM/admin/run" > /dev/null
docker compose exec -T postgres psql -U "${POSTGRES_USER:-fuel}" -d "${POSTGRES_DB:-fuel}" -q -c "
    DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
    DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
    DELETE FROM prediction_evaluations; DELETE FROM demand_observations;" 2>/dev/null

printf '%8s %10s %14s %16s %12s\n' "tick" "train_rows" "model_mae" "baseline_mae" "verdict"
printf '%8s %10s %14s %16s %12s\n' "----" "----------" "------------" "----------------" "--------"

CHECKED=0
while [ "$CHECKED" -lt 14 ]; do
    # Let the world run a slice, then let the platform ingest it.
    sleep 3
    TICK_NOW=$(tick)
    sleep 12
    TRAIN=$(curl -s -X POST "$BACKEND/api/model/train")
    ROWS=$(echo "$TRAIN" | jq -r '.training_rows // 0')
    if [ "$ROWS" = "0" ] || [ "$ROWS" = "null" ]; then
        continue
    fi
    STATUS=$(curl -fsS "$BACKEND/api/model-status")
    MODEL_MAE=$(echo "$STATUS" | jq -r '.learned_model.validation_mae_liters // "n/a"')
    BASE_MAE=$(echo "$STATUS" | jq -r '.learned_model.baseline_validation_mae_liters // "n/a"')
    VERDICT=$(echo "$STATUS" | jq -r '.active_model')
    printf '%8s %10s %14s %16s %12s\n' "$TICK_NOW" "$ROWS" "$MODEL_MAE" "$BASE_MAE" "$VERDICT"
    CHECKED=$((CHECKED + 1))
    if [ "$VERDICT" = "learned" ]; then
        echo ""
        echo "The model is trusted from tick ${TICK_NOW}."
        break
    fi
done

echo ""
echo "The world's tanks at this point:"
curl -fsS "$SIM/v1/stations" | jq -r '.[] | "  \(.id): \(.inventory | to_entries | map("\(.key)=\(.value)") | join(" "))"'
