#!/usr/bin/env bash
# Why does the verdict flip right after a spike?
#
# Prints the one-step ratio (the gate) alongside the horizon ratio, at
# every tick across a spike.
set -uo pipefail

cd "$(dirname "$0")/.."
SIM=http://localhost:8100
BACKEND=http://localhost:8001

step_to() {
    local target="$1" now
    now=$(curl -fsS "$SIM/v1/instance" | jq -r '.tick')
    while [ "$now" -lt "$target" ]; do
        curl -fsS -X POST "$SIM/admin/step" > /dev/null
        now=$(curl -fsS "$SIM/v1/instance" | jq -r '.tick')
    done
}

curl -fsS -X POST "$SIM/admin/reset" > /dev/null
curl -fsS -X POST "$BACKEND/admin/reset" > /dev/null
docker compose exec -T postgres psql -U fuel -d fuel -q -c "
    DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
    DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
    DELETE FROM prediction_evaluations;" 2>/dev/null

step_to 120
sleep 8
curl -fsS -X POST "$BACKEND/api/model/train" > /dev/null
sleep 2
echo "before the spike: $(curl -fsS "$BACKEND/api/model-status" | jq -c '{active: .active_model, ratio: .learned_model.validation_error_ratio}')"

echo "injecting the spike at tick 120..."
curl -fsS -X POST "$BACKEND/api/simulation/crisis" -H 'Content-Type: application/json' \
  -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}' | jq -c '.event.type'

for n in 2 4 6 8 10 12 16 20 24 32; do
    step_to "$((120 + n))"
    sleep 7
    curl -fsS -X POST "$BACKEND/api/model/train" > /dev/null 2>&1
    sleep 2
    S=$(curl -fsS "$BACKEND/api/model-status")
    printf 'tick=%-5s one_step_ratio=%-8s model_mae=%-9s base_mae=%-9s horizon=%-10s base_h=%-10s active=%s\n' \
        "$((120 + n))" \
        "$(echo "$S" | jq -r '.learned_model.validation_error_ratio // "n/a"')" \
        "$(echo "$S" | jq -r '.learned_model.validation_mae_liters // "n/a"')" \
        "$(echo "$S" | jq -r '.learned_model.baseline_validation_mae // "n/a"')" \
        "$(echo "$S" | jq -r '.learned_model.horizon_total_mae_liters // "n/a"')" \
        "$(echo "$S" | jq -r '.learned_model.baseline_horizon_total_mae_liters // "n/a"')" \
        "$(echo "$S" | jq -r '.active_model')"
done
