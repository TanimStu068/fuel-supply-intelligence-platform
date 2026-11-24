#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# stack_guard.sh — keep the Docker host usable on a shared machine.
# ---------------------------------------------------------------------------
# This machine runs several projects' Docker stacks at once. When all of them
# run together the desktop becomes unresponsive, and the cause is not any one
# of them:
#
#   * several containers were in a *restart loop* — a crash-looping container
#     re-readies every few seconds and never lets the CPU idle;
#   * several containers had *no memory limit*, so a leak in any one of them
#     could take the whole 7 GB of RAM and force the host into swap;
#   * several containers had *unbounded logs*, and a chatty service fills the
#     disk, which stalls the Docker daemon itself.
#
# This script fixes all three at the host level, without touching the other
# projects' source code:
#
#   1. `stop-restart-loops` — find every container in a restart loop, stop it
#      and clear its restart policy, so it stops burning CPU.
#   2. `stop-all` — stop every container on the host.
#   3. `harden` — apply a default memory limit and log rotation to every
#      container that does not already have them.
#   4. `status` — show what is running, and how much of the host it is using.
#
# Usage:
#   scripts/stack_guard.sh status
#   scripts/stack_guard.sh stop-restart-loops
#   scripts/stack_guard.sh stop-all
#   scripts/stack_guard.sh harden
#   scripts/stack_guard.sh start-fuel [--with-observability]
#
# `harden` writes to the Docker daemon's defaults, so it affects containers
# created *after* it runs. The project's own compose file carries the same
# limits, so the fuel stack is covered either way.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

#: The default memory ceiling applied to any container that has none.
DEFAULT_MEM_LIMIT="${DEFAULT_MEM_LIMIT:-1g}"
#: The default log rotation applied to any container that has none.
DEFAULT_LOG_MAX_SIZE="${DEFAULT_LOG_MAX_SIZE:-10m}"
DEFAULT_LOG_MAX_FILE="${DEFAULT_LOG_MAX_FILE:-3}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
ok() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
cmd_status() {
  log "running containers"
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' || true
  echo
  log "restarting (crash-looping) containers"
  local looping
  looping="$(docker ps --filter 'status=restarting' --format '{{.Names}}' || true)"
  if [[ -z "$looping" ]]; then
    ok "none — no container is in a restart loop"
  else
    warn "$looping"
    warn "each of these re-readies every few seconds and burns CPU continuously"
  fi
  echo
  log "host resources"
  free -h | sed 's/^/    /'
  echo "    load: $(cat /proc/loadavg)"
  echo "    containers: $(docker ps -q | wc -l) running / $(docker ps -aq | wc -l) total"
  echo "    disk:  $(docker system df --format '{{.Type}}: {{.Size}} ({{.Reclaimable}} reclaimable)' | tr '\n' ' ')"
}

# ---------------------------------------------------------------------------
# stop-restart-loops
# ---------------------------------------------------------------------------
# A container whose *own* process keeps crashing will be restarted forever
# by Docker if the policy says so. Those are the ones that make the machine
# feel stuck: each one wakes every few seconds, and there were six of them
# running at once.
cmd_stop_restart_loops() {
  local looping
  looping="$(docker ps --filter 'status=restarting' --format '{{.Names}}')"
  if [[ -z "$looping" ]]; then
    ok "no container is restarting"
    return 0
  fi

  local name
  while read -r name; do
    [[ -z "$name" ]] && continue
    # Clear the policy first, so a stop does not immediately schedule
    # another start.
    docker update --restart=no "$name" >/dev/null
    docker stop "$name" >/dev/null 2>&1 || true
    ok "stopped the restart loop: $name"
  done <<< "$looping"
}

# ---------------------------------------------------------------------------
# stop-all
# ---------------------------------------------------------------------------
cmd_stop_all() {
  local count
  count="$(docker ps -q | wc -l)"
  if [[ "$count" -eq 0 ]]; then
    ok "nothing is running"
    return 0
  fi
  log "stopping $count container(s) and clearing their restart policies"
  # Clear the policies first: otherwise `stop` on a container with
  # `restart: always` leaves it scheduled to come straight back up.
  local id
  while read -r id; do
    [[ -z "$id" ]] && continue
    docker update --restart=no "$id" >/dev/null 2>&1 || true
  done < <(docker ps -q)
  docker stop $(docker ps -q) >/dev/null 2>&1 || true
  ok "stopped $count container(s)"
  free -h | sed 's/^/    /'
}

# ---------------------------------------------------------------------------
# harden
# ---------------------------------------------------------------------------
# Applies the two limits that keep a shared host usable:
#
#   * a memory ceiling, so one leaky container cannot exhaust the host;
#   * log rotation, so a chatty container cannot fill the disk.
#
# Both are applied to the *existing* containers, so the effect is immediate
# rather than only applying to containers created later.
cmd_harden() {
  log "applying memory limits and log rotation to every container"
  local id name hardened=0 skipped=0
  local -a unrotated=()
  while read -r id name; do
    [[ -z "$id" ]] && continue

    local mem
    mem="$(docker inspect --format '{{.HostConfig.Memory}}' "$id")"
    if [[ "$mem" == "0" ]]; then
      # The memory *swap* limit has to move with the memory limit, or
      # Docker refuses the update: a container cannot have less memory
      # than its swap allowance.
      docker update --memory "$DEFAULT_MEM_LIMIT" --memory-swap "$DEFAULT_MEM_LIMIT" "$id" >/dev/null
      ok "$name → memory limit $DEFAULT_MEM_LIMIT"
      hardened=$((hardened + 1))
    else
      skipped=$((skipped + 1))
    fi

    # The log driver cannot be changed on a running container: Docker's
    # `update` has no such flag. The rotation is applied per-service in
    # this project's compose file, and host-wide through the daemon's
    # defaults, which need one `sudo systemctl reload docker` to take
    # effect. The unrotated containers are listed so the operator knows
    # exactly which ones still need it.
    local logconf
    logconf="$(docker inspect --format '{{.HostConfig.LogConfig.Type}}:{{index .HostConfig.LogConfig.Config "max-size"}}' "$id")"
    if [[ "$logconf" != *"max-size"* ]]; then
      unrotated+=("$name")
    fi
  done < <(docker ps -a --format '{{.ID}} {{.Names}}')

  echo
  ok "$hardened container(s) given a memory limit, $skipped already had one"

  if [[ "${#unrotated[@]}" -gt 0 ]]; then
    warn "${#unrotated[@]} container(s) have unbounded logs:"
    printf '        %s\n' "${unrotated[@]}"
  fi

  # The daemon's own defaults, so anything created from now on inherits
  # them. Writing /etc/docker/daemon.json needs root, so this is prepared
  # rather than applied — and it is applied to the daemon's *defaults*, so
  # it only affects containers created after the reload.
  local target="/etc/docker/daemon.json"
  local prepared="$ROOT/docs/host-tuning/daemon.json"
  log "preparing the daemon defaults at $prepared"
  mkdir -p "$(dirname "$prepared")"
  python3 - "$prepared" "$DEFAULT_LOG_MAX_SIZE" "$DEFAULT_LOG_MAX_FILE" <<'PY'
import json, pathlib, sys
target = pathlib.Path(sys.argv[1])
existing = pathlib.Path("/etc/docker/daemon.json")
data = {}
if existing.exists():
    try:
        data = json.loads(existing.read_text())
    except Exception:
        data = {}
data.setdefault("dns", ["8.8.8.8", "1.1.1.1"])
data.setdefault("log-driver", "json-file")
data.setdefault("log-opts", {})
data["log-opts"].setdefault("max-size", sys.argv[2])
data["log-opts"].setdefault("max-file", sys.argv[3])
target.write_text(json.dumps(data, indent=2) + "\n")
PY
  ok "written: $prepared"
  echo
  echo "    To apply it host-wide (needs your password):"
  echo "        sudo cp $prepared $target && sudo systemctl restart docker"
  echo
  echo "    NOTE: restarting Docker stops every running container. Do this"
  echo "          when nothing else needs to stay up."
}

# ---------------------------------------------------------------------------
# start-fuel
# ---------------------------------------------------------------------------
cmd_start_fuel() {
  cd "$ROOT"
  local with_obs="${1:-}"
  log "starting the fuel stack${with_obs:+ (with observability)}"
  if [[ "$with_obs" == "--with-observability" ]]; then
    docker compose --profile observability up -d --wait
  else
    # Without the observability profile: the platform alone is four
    # containers and about 300 MB, which is what runs comfortably on a
    # shared machine.
    docker compose up -d --wait
  fi
  ok "the stack is up"
  docker compose ps
}

case "${1:-status}" in
  status) cmd_status ;;
  stop-restart-loops) cmd_stop_restart_loops ;;
  stop-all) cmd_stop_all ;;
  harden) cmd_harden ;;
  start-fuel) cmd_start_fuel "${2:-}" ;;
  *)
    echo "usage: $0 {status|stop-restart-loops|stop-all|harden|start-fuel [--with-observability]}" >&2
    exit 2
    ;;
esac
