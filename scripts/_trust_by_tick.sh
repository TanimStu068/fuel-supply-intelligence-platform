#!/usr/bin/env bash
# Which tick does the learned model become trusted at?
#
# The demo needs an opening state where the model is in use. This measures
# it by stepping the world deterministically and training at each stop.
set -uo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BACKEND="${BACKEND:-http://localhost:${BACKEND_PORT:-8001}}"
SIM="${SIM:-http://localhost:${SIMULATOR_PORT:-8000}}"

step_to() {
    local target="$1"
    local now
    now=$(curl -fsS "$SIM/v1/instance" | jq -r '.tick')
    while [ "$now" -lt "$target" ]; do
        curl -fsS -X POST "$SIM/admin/step" > /dev/null
        now=$(curl -fsS "$SIM/v1/instance" | jq -r '.tick')
    done
}

echo "resetting..."
curl -fsS -X POST "$SIM/admin/reset" > /dev/null
curl -fsS -X POST "$BACKEND/admin/reset" > /dev/null
docker compose exec -T postgres psql -U "${POSTGRES_USER:-fuel}" -d "${POSTGRES_DB:-fuel}" -q -c "
    DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
    DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
    DELETE FROM prediction_evaluations;" 2>/dev/null

printf '%8s %10s %12s %14s %14s %12s\n' "tick" "rows" "model_mae" "model_horiz" "base_horiz" "active"
for target in 8 16 24 40 56 72 96 128 160 200 240 300 400; do
    step_to "$target"
    # Let the platform ingest the new history.
    sleep 6
    curl -fsS -X POST "$BACKEND/api/model/train" > /dev/null 2>&1
    sleep 2
    STATUS=$(curl -fsS "$BACKEND/api/model-status")
    printf '%8s %10s %12s %14s %14s %12s\n' \
        "$target" \
        "$(echo "$STATUS" | jq -r '.learned_model.training_rows // 0')" \
        "$(echo "$STATUS" | jq -r '.learned_model.validation_mae_liters // "n/a"')" \
        "$(echo "$STATUS" | jq -r '.learned_model.horizon_total_mae_liters // "n/a"')" \
        "$(echo "$STATUS" | jq -r '.learned_model.baseline_horizon_total_mae_liters // "n/a"')" \
        "$(echo "$STATUS" | jq -r '.active_model')"
done
