#!/usr/bin/env bash
# Test the proposed opening state: reset, step to a fixed tick, spike, look.
set -uo pipefail

cd "$(dirname "$0")/.."
SIM=http://localhost:8100
BACKEND=http://localhost:8001

step_to() {
    local target="$1"
    local now
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

echo "stepping to 64..."
step_to 64
sleep 8
curl -fsS -X POST "$BACKEND/api/model/train" | jq -c '{training_rows}'
sleep 2
echo "model: $(curl -fsS "$BACKEND/api/model-status" | jq -r '.active_model')"
echo "risk before spike: $(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')"
echo "fuel: $(curl -fsS "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')"
echo "recs: $(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')"

echo "injecting the spike..."
curl -fsS -X POST "$BACKEND/api/simulation/crisis" -H 'Content-Type: application/json' \
  -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}' | jq -c '.event.type'

for i in $(seq 1 12); do
    sleep 3
    echo "  risk: $(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')  anomalies: $(curl -fsS "$BACKEND/api/anomalies" | jq -r '[.anomalies[]?] | length')  recs: $(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')"
done
