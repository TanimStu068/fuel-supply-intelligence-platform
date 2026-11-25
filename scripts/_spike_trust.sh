#!/usr/bin/env bash
# Does a demand spike flip the trust verdict?
#
# The demo injects a spike and then expects the platform to still be using
# the learned model. This measures the verdict and its evidence across the
# spike, window by window.
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
echo "before the spike:"
curl -fsS "$BACKEND/api/model-status" | jq -c '{active: .active_model, h: .learned_model.horizon_total_mae_liters, b: .learned_model.baseline_horizon_total_mae_liters}'

echo "injecting the spike at tick 120..."
curl -fsS -X POST "$BACKEND/api/simulation/crisis" -H 'Content-Type: application/json' \
  -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}' | jq -c '.event.type'

for n in 2 4 8 12 16 24 32 48 64; do
    step_to "$((120 + n))"
    sleep 7
    curl -fsS -X POST "$BACKEND/api/model/train" > /dev/null 2>&1
    sleep 2
    S=$(curl -fsS "$BACKEND/api/model-status")
    MH=$(echo "$S" | jq -r '.learned_model.horizon_total_mae_liters // 0')
    BH=$(echo "$S" | jq -r '.learned_model.baseline_horizon_total_mae_liters // 0')
    printf 'tick=%-5s model_h=%-10s base_h=%-10s ratio=%-7s active=%s\n' \
        "$((120 + n))" "$MH" "$BH" \
        "$(awk -v m="$MH" -v b="$BH" 'BEGIN{ if (b>0) printf "%.3f", m/b; else print "-" }')" \
        "$(echo "$S" | jq -r '.active_model')"
done
