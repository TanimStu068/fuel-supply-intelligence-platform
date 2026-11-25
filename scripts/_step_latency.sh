#!/usr/bin/env bash
# Probe: how fast does the platform react to a single simulator step?
set -uo pipefail

cd "$(dirname "$0")/.."
SIM=http://localhost:8100
BACKEND=http://localhost:8001

curl -fsS -X POST "$SIM/admin/reset" > /dev/null
curl -fsS -X POST "$BACKEND/admin/reset" > /dev/null
docker compose exec -T postgres psql -U fuel -d fuel -q -c "
    DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
    DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
    DELETE FROM prediction_evaluations;" 2>/dev/null

for n in 4 8 12 16 20 24; do
    while [ "$(curl -fsS "$SIM/v1/instance" | jq -r '.tick')" -lt "$n" ]; do
        curl -fsS -X POST "$SIM/admin/step" > /dev/null
    done
    START=$(date +%s%N)
    for _ in $(seq 1 40); do
        CACHED=$(curl -fsS "$BACKEND/api/state" | jq -r '.instance.tick // 0')
        [ "$CACHED" -ge "$n" ] && break
        sleep 0.25
    done
    END=$(date +%s%N)
    printf 'step to %-4s -> platform caught up in %s ms (cached tick %s)\n' \
        "$n" "$(( (END - START) / 1000000 ))" "$CACHED"
done

echo "training..."
curl -fsS -X POST "$BACKEND/api/model/train" | jq -c '{training_rows}'
sleep 2
curl -fsS "$BACKEND/api/model-status" | jq -c '{active_model, horizon: .learned_model.horizon_total_mae_liters, base: .learned_model.baseline_horizon_total_mae_liters}'
echo "risk: $(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')"
echo "fuel: $(curl -fsS "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')"
echo "recs: $(curl -fsS "$BACKEND/api/recommendations" | jq -r '.recommendations | length')"
echo "svc:  $(curl -fsS "$BACKEND/api/state" | jq -c '.metrics | {service_level, unmet_demand_liters}')"
