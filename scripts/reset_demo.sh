#!/usr/bin/env bash
#
# Reset the demo to a clean, known state.
#
# The demo has to start from the same world every time — otherwise the
# second run behaves differently from the first, and the timings in
# docs/demo-script.md mean nothing. This script puts the simulation back
# to tick zero, clears the platform's decisions and alerts, and leaves
# the simulator running so the story can start immediately.
#
#   scripts/reset_demo.sh              # reset and start the clock
#   scripts/reset_demo.sh --no-run     # reset and leave it paused
#   scripts/reset_demo.sh --wait       # reset, then wait for the platform
#                                       # to be healthy and the world to
#                                       # have a few ticks of history
#
# Every step is idempotent, so running it twice is harmless.

set -euo pipefail

# --- configuration ---------------------------------------------------------

# The ports live in .env, and they are remapped on a busy machine. Read
# them from there rather than assuming, so the script works on whatever
# port the stack is actually on.
ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BACKEND_PORT="${BACKEND_PORT:-8001}"
SIMULATOR_PORT="${SIMULATOR_PORT:-8000}"

BACKEND_URL="${BACKEND_URL:-http://localhost:${BACKEND_PORT}}"
SIMULATOR_URL="${SIMULATOR_URL:-http://localhost:${SIMULATOR_PORT}}"

RUN_AFTER=1
WAIT_FOR_HISTORY=0
HISTORY_TICKS=12

while [ $# -gt 0 ]; do
    case "$1" in
        --no-run)
            RUN_AFTER=0
            shift
            ;;
        --wait)
            WAIT_FOR_HISTORY=1
            shift
            ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            exit 2
            ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# --- is the stack up? ------------------------------------------------------

say "Checking the stack"

if ! curl -fsS --max-time 5 "${BACKEND_URL}/health" > /dev/null 2>&1; then
    echo "The backend is not answering at ${BACKEND_URL}." >&2
    echo "" >&2
    echo "Start it first:" >&2
    echo "    docker compose up -d --wait" >&2
    exit 1
fi

curl -fsS --max-time 5 "${SIMULATOR_URL}/v1/health" > /dev/null 2>&1 || {
    echo "The simulator is not answering at ${SIMULATOR_URL}." >&2
    exit 1
}

echo "the backend and the simulator are both up"

# --- 1. reset the platform's own state -------------------------------------

say "Resetting the platform's own state"

# This clears the decision engine's in-memory working set, the counters,
# any injected prediction fault and the kill-switch.
#
# The in-memory set is the important one. A reset that only cleared
# Postgres would leave the previous run's proposals in memory, and every
# one of them would fail to approve — they were planned for a world that
# no longer exists, and the simulator refuses them with *"the depot's
# dispatch allowance for this tick is used up"*.
#
# An injected fault survives a simulator reset, because it lives in the
# backend, not in the world. Without this step the next demo starts with
# the prediction layer already broken, and the "the model recovers on its
# own" moment at the end of the story never happens.
curl -fsS -X POST "${BACKEND_URL}/admin/reset" \
    | jq -c '{recommendations_cleared, kill_switch, prediction_fault}' 2>/dev/null \
    || curl -fsS -X POST "${BACKEND_URL}/admin/reset" > /dev/null

echo "the platform's proposals, counters and fault state are clear"

# --- 2. reset the simulation ----------------------------------------------

say "Resetting the simulation"

# The reset reloads the scenario from tick zero. It leaves the
# simulation paused, which is why the run step below matters.
curl -fsS -X POST "${SIMULATOR_URL}/admin/reset" | head -c 200
echo

# --- 3. clear the platform's own records ----------------------------------

say "Clearing the platform's decisions and alerts"

# The platform keeps its decisions in Postgres. The demo wants an empty
# decision history, so the tables are truncated. The simulator's own
# allocations were already cleared by the reset above.
if docker compose ps --status running --services 2>/dev/null | grep -q '^postgres$'; then
    # DELETE rather than TRUNCATE. TRUNCATE needs an ACCESS EXCLUSIVE
    # lock, and the backend writes to these tables on every sync cycle,
    # so a TRUNCATE waits for a gap that may never come. DELETE takes a
    # weaker lock and gets through.
    #
    # The lock timeout makes the failure mode explicit: if the tables are
    # busy, the step is skipped in a second rather than hanging the demo
    # before it starts.
    docker compose exec -T postgres \
        psql -U "${POSTGRES_USER:-fuel}" -d "${POSTGRES_DB:-fuel}" -q -c "
            SET lock_timeout = '2s';
            DELETE FROM decision_records;
            DELETE FROM recommendations;
            DELETE FROM alerts;
            DELETE FROM allocations;
            DELETE FROM risk_assessments;
            DELETE FROM demand_forecasts;
            DELETE FROM prediction_evaluations;
        " 2>/dev/null && echo "the platform's decisions and alerts are empty" || {
        echo "could not clear the platform's tables — continuing anyway" >&2
    }
else
    echo "the database is not running — skipping the truncate"
fi

# --- 4. start the clock ----------------------------------------------------

if [ "$RUN_AFTER" -eq 1 ]; then
    say "Starting the simulation"
    curl -fsS -X POST "${SIMULATOR_URL}/admin/run" | head -c 200
    echo
else
    say "Leaving the simulation paused"
fi

# --- 5. wait for the platform to catch up ----------------------------------

if [ "$WAIT_FOR_HISTORY" -eq 1 ]; then
    say "Waiting for ${HISTORY_TICKS} ticks of history"

    # The platform needs a little history before its forecast is worth
    # anything. A demo that starts with an empty model shows the
    # rule-based fallback rather than the learned predictor, which is a
    # different story.
    START=$(curl -fsS "${SIMULATOR_URL}/v1/instance" | sed -n 's/.*"tick": *\([0-9]*\).*/\1/p')
    TARGET=$((START + HISTORY_TICKS))

    while :; do
        NOW=$(curl -fsS "${SIMULATOR_URL}/v1/instance" | sed -n 's/.*"tick": *\([0-9]*\).*/\1/p')
        if [ -n "$NOW" ] && [ "$NOW" -ge "$TARGET" ]; then
            break
        fi
        sleep 0.5
    done

    echo "the world is at tick ${NOW}"

    # The platform's own cache has to catch up with the world before the
    # demo starts talking about numbers, or the dashboard shows the
    # previous run's state for a few seconds.
    #
    # After a reset the platform **drops** the cached snapshot of the world
    # that just ended — it will not serve a run's numbers as if they were
    # current — so `/api/state` answers 503 until the next sync cycle
    # fetches the new world. The wait therefore has to tolerate the 503
    # and look for the *new* world's tick.
    #
    # The wait is on the *simulator's* tick, not on the platform's cached
    # one: a wait keyed on the cache would return immediately and then
    # assert against stale data.
    CACHED=""
    for _ in $(seq 1 30); do
        # ``curl -f`` makes a 503 an error, so the status code is checked
        # instead and the body is parsed only when there is one.
        BODY=$(curl -s --max-time 5 "${BACKEND_URL}/api/state" 2>/dev/null || true)
        CACHED=$(printf '%s' "${BODY:-}" \
            | jq -r '.instance.tick // empty' 2>/dev/null || true)
        if [ -n "$CACHED" ] && [ "$CACHED" -ge "$NOW" ]; then
            break
        fi
        sleep 1
    done
    echo "the platform is at tick ${CACHED:-unknown}"

    # Train the model on what the world has just produced, so the demo
    # starts with the learned model in use rather than with the
    # rule-based fallback.
    say "Training the model on the history so far"
    curl -fsS --max-time 30 -X POST "${BACKEND_URL}/api/model/train" \
        | jq -c '{trained, training_rows, validation_mae}' 2>/dev/null || true
fi

say "The demo is ready"
echo ""
echo "  Dashboard    http://localhost:${FRONTEND_PORT:-5173}"
echo "  Backend API  ${BACKEND_URL}"
echo "  Simulator    ${SIMULATOR_URL}"
echo ""
echo "Next: docs/demo-script.md"
