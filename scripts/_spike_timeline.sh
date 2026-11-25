#!/usr/bin/env bash
# Probe: reset, pause, step to 24, spike, step, watch the risk move.
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

wait_platform() {
    local target="$1"
    for _ in $(seq 1 40); do
        local cached
        cached=$(curl -fsS "$BACKEND/api/state" | jq -r '.instance.tick // 0')
        [ "$cached" -ge "$target" ] && { echo "  platform at $cached"; return 0; }
        sleep 1
    done
    echo "  platform did NOT catch up to $target"
}

curl -fsS -X POST "$SIM/admin/reset" > /dev/null
curl -fsS -X POST "$BACKEND/admin/reset" > /dev/null
docker compose exec -T postgres psql -U fuel -d fuel -q -c "
    DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
    DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
    DELETE FROM prediction_evaluations;" 2>/dev/null

step_to 24
wait_platform 24
sleep 4
curl -fsS -X POST "$BACKEND/api/model/train" | jq -c '{training_rows}'
sleep 3
echo "model: $(curl -fsS "$BACKEND/api/model-status" | jq -r '.active_model')"
echo "risk:  $(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')"
echo "fuel:  $(curl -fsS "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')"
echo "recs:  $(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')"
echo "svc:   $(curl -fsS "$BACKEND/api/state" | jq -c '.metrics | {service_level, unmet_demand_liters}')"

echo ""
echo "injecting the spike at tick 24..."
curl -fsS -X POST "$BACKEND/api/simulation/crisis" -H 'Content-Type: application/json' \
  -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}' | jq -c '.event.type'

for n in 2 4 6 8 10 12 16 20 24 30; do
    step_to "$((24 + n))"
    sleep 5
    printf 'tick=%-4s risk=%-42s anomalies=%-4s recs=%-4s fuel=%s\n' \
        "$((24 + n))" \
        "$(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')" \
        "$(curl -fsS "$BACKEND/api/anomalies" | jq -c '[.anomalies[]?] | length')" \
        "$(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')" \
        "$(curl -fsS "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')"
done
