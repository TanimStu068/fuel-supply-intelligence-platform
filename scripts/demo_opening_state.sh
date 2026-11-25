#!/usr/bin/env bash
#
# Measure the demo's opening state.
#
# The demo has to start somewhere an audience can read: a world where the
# platform is working, the model is in use, and there is something for the
# operator to do. This measures the risk distribution and the tank levels at
# a range of ticks, and whether an approved allocation actually arrives and
# improves the risk.
#
# Usage: scripts/demo_opening_state.sh

set -uo pipefail

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BACKEND="${BACKEND:-http://localhost:${BACKEND_PORT:-8001}}"
SIM="${SIM:-http://localhost:${SIMULATOR_PORT:-8000}}"

tick() { curl -fsS "$SIM/v1/instance" | jq -r '.tick'; }

reset_world() {
    curl -s -X POST "$SIM/admin/reset" > /dev/null
    curl -s -X POST "$SIM/admin/run" > /dev/null
    docker compose exec -T postgres psql -U "${POSTGRES_USER:-fuel}" -d "${POSTGRES_DB:-fuel}" -q -c "
        DELETE FROM decision_records; DELETE FROM recommendations; DELETE FROM alerts;
        DELETE FROM allocations; DELETE FROM risk_assessments; DELETE FROM demand_forecasts;
        DELETE FROM prediction_evaluations; DELETE FROM demand_observations;" 2>/dev/null
}

report() {
    local label="$1"
    local risk model rows tanks
    risk=$(curl -fsS "$BACKEND/api/risk" | jq -c '.summary')
    model=$(curl -fsS "$BACKEND/api/model-status" | jq -r '.active_model')
    rows=$(curl -fsS "$BACKEND/api/model-status" | jq -r '.learned_model.training_rows // 0')
    tanks=$(curl -fsS "$SIM/v1/stations" | jq -r '[.[] | [.inventory.DIESEL, .inventory.PETROL, .inventory.OCTANE] | add] | add')
    printf '%14s  model=%-9s rows=%-6s total_fuel=%-10s risk=%s\n' \
        "$label" "$model" "$rows" "${tanks%.*}" "$risk"
}

echo "=== the risk distribution as the world runs from a reset ==="
reset_world
for slice in 1 2 3 4 5 6 7 8; do
    sleep 4
    sleep 12
    curl -s -X POST "$BACKEND/api/model/train" > /dev/null
    report "tick $(tick)"
done

echo ""
echo "=== does an approved allocation arrive and improve the risk? ==="
report "before approve"
RECS=$(curl -fsS "$BACKEND/api/recommendations")
COUNT=$(echo "$RECS" | jq -r '.recommendations | length')
echo "  proposals: $COUNT"
echo "$RECS" | jq -r '.recommendations[:3][] | "    \(.id)  \(.station_id)/\(.fuel_type)  \(.quantity) L  hours_to_stockout=\(.hours_to_stockout)"'

# Approve the single best proposal — the one with the lowest cover.
BEST=$(echo "$RECS" | jq -r '[.recommendations[] | select(.hours_to_stockout != null)] | sort_by(.hours_to_stockout) | .[0].id // empty')
if [ -n "$BEST" ]; then
    RESULT=$(curl -s -X POST "$BACKEND/api/recommendations/$BEST/approve" \
        -H 'Content-Type: application/json' -d '{"decided_by":"probe"}')
    echo "  approved $BEST -> $(echo "$RESULT" | jq -r '.status // .error_message')"
else
    echo "  no proposal with a stockout horizon — nothing to approve"
fi

for wait in 5 10 15 20; do
    sleep 5
    report "  +${wait}s"
done

echo ""
echo "  the allocation ledger:"
curl -fsS "$BACKEND/api/allocations" | jq -r '.allocations[:3][] | "    id=\(.id) \(.destination_station_id)/\(.fuel_type) \(.quantity) L status=\(.status)"'
