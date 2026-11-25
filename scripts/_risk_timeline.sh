#!/usr/bin/env bash
# When does the risk become critical after a reset?
set -uo pipefail
cd /home/tanim-mahmud/Desktop/poridhi/fuel-supply-intelligence-platform
SIM=http://localhost:8100
BACKEND=http://localhost:8001

curl -s -X POST "$SIM/admin/reset" > /dev/null
curl -s -X POST "$SIM/admin/run" > /dev/null

for i in $(seq 1 40); do
    sleep 2
    T=$(curl -s "$SIM/v1/instance" | jq -r '.tick')
    R=$(curl -s "$BACKEND/api/risk" | jq -c '.summary')
    S=$(curl -s "$SIM/v1/stations" | jq -c '[.[] | [.inventory.DIESEL,.inventory.PETROL,.inventory.OCTANE] | add] | add')
    printf 'tick=%-6s risk=%-40s fuel=%s\n' "$T" "$R" "$S"
done
