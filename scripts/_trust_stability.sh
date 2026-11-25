#!/usr/bin/env bash
# Measure the model's trust verdict at every tick, with the ratios.
#
# The demo needs a *stable* verdict: the model should be in use at the
# opening and come back at the end, not flip on a coin toss between
# retrains. This measures the verdict and the evidence behind it.
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
    local target="$1" now
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

printf '%7s %8s %10s %10s %8s %10s %10s %8s %10s\n' \
    "tick" "rows" "model_mae" "base_mae" "ratio" "model_h" "base_h" "h_ratio" "active"
for target in 8 16 24 32 40 56 72 88 104 128 160 200 260 320; do
    step_to "$target"
    sleep 7
    curl -fsS -X POST "$BACKEND/api/model/train" > /dev/null 2>&1
    sleep 2
    S=$(curl -fsS "$BACKEND/api/model-status")
    M=$(echo "$S" | jq -r '.learned_model.validation_mae_liters // 0')
    B=$(echo "$S" | jq -r '.learned_model.baseline_validation_mae // 0')
    MH=$(echo "$S" | jq -r '.learned_model.horizon_total_mae_liters // 0')
    BH=$(echo "$S" | jq -r '.learned_model.baseline_horizon_total_mae_liters // 0')
    printf '%7s %8s %10s %10s %8s %10s %10s %8s %10s\n' \
        "$target" \
        "$(echo "$S" | jq -r '.learned_model.training_rows // 0')" \
        "$M" "$B" \
        "$(awk -v m="$M" -v b="$B" 'BEGIN{ if (b>0) printf "%.3f", m/b; else print "-" }')" \
        "$MH" "$BH" \
        "$(awk -v m="$MH" -v b="$BH" 'BEGIN{ if (b>0) printf "%.3f", m/b; else print "-" }')" \
        "$(echo "$S" | jq -r '.active_model')"
done
