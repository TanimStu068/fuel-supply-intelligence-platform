#!/usr/bin/env bash
# Probe the fully stepped demo opening: pause at a tick, spike, step, watch.
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

echo "stepping to 64 (paused)..."
step_to 64
wait_platform 64
sleep 4
curl -fsS -X POST "$BACKEND/api/model/train" | jq -c '{training_rows}'
sleep 3
echo "model: $(curl -fsS "$BACKEND/api/model-status" | jq -r '.active_model')"
echo "risk:  $(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')"
echo "fuel:  $(curl -fsS "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')"
echo "recs:  $(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')"

echo ""
echo "injecting the spike..."
curl -fsS -X POST "$BACKEND/api/simulation/crisis" -H 'Content-Type: application/json' \
  -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}' | jq -c '.event.type'

echo "stepping 4 ticks..."
for _ in 1 2 3 4; do curl -fsS -X POST "$SIM/admin/step" > /dev/null; done
sleep 6
echo "  risk:     $(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')"
echo "  anomalies:$(curl -fsS "$BACKEND/api/anomalies" | jq -c '[.anomalies[]?] | length')"
echo "  recs:     $(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')"
echo "  demand now: $(curl -fsS "$SIM/v1/demand-history?limit=3" | jq -c '.[0:3] | map({station: .station_id, fuel: .fuel_type, tick, d: (.demand_liters|floor)})')"
echo "  fuel:     $(curl -fsS "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')"
