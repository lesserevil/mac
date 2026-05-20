load_drain_api_env() {
  DRAIN_API_URL="${MAC_HUB_URL:-${HUB_URL:-http://127.0.0.1:$MAC_PORT}}"
  DRAIN_API_TOKEN="${MAC_WORKER_TOKEN:-${MAC_API_TOKEN:-}}"
  if [ -f "$ENV_FILE" ]; then
    set -a
    set +u
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set -u
    set +a
    DRAIN_API_URL="${MAC_HUB_URL:-${HUB_URL:-$DRAIN_API_URL}}"
    DRAIN_API_TOKEN="${MAC_WORKER_TOKEN:-${MAC_API_TOKEN:-$DRAIN_API_TOKEN}}"
  fi
  DRAIN_API_URL="${DRAIN_API_URL%/}"
}

mac_api_json() {
  local method="$1" path="$2" body="${3:-}"
  [ -n "${DRAIN_API_TOKEN:-}" ] || return 1
  "$PY" - "$method" "$DRAIN_API_URL$path" "$DRAIN_API_TOKEN" "$body" <<'PY'
import json
import sys
import urllib.error
import urllib.request

method, url, token, body = sys.argv[1:5]
data = body.encode("utf-8") if body else None
request = urllib.request.Request(url, data=data, method=method)
request.add_header("Authorization", "Bearer " + token)
if data is not None:
    request.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    sys.stderr.write(exc.read().decode("utf-8", errors="replace"))
    raise SystemExit(1)
PY
}

agent_id_for_drain() {
  local response
  response="$(mac_api_json GET "/agents")" || return 1
  "$PY" - "$AGENT" "$response" <<'PY'
import json
import sys

expected = sys.argv[1]
agents = json.loads(sys.argv[2])
for agent in agents:
    if agent.get("name") == expected or agent.get("id") == expected:
        print(agent.get("id"))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

wait_for_agent_active_leases() {
  local agent_id="$1" deadline now count summary_path="$LOG_DIR/mac-agent-drain.json"
  deadline=$(( $(date +%s) + ${DRAIN_TIMEOUT_SECONDS:-1800} ))
  while :; do
    if mac_api_json GET "/tasks" > "$summary_path.tasks"; then
      count="$($PY - "$summary_path.tasks" "$agent_id" "$summary_path" <<'PY'
import json
import sys
import time
from pathlib import Path

tasks_path = Path(sys.argv[1])
agent_id = sys.argv[2]
summary_path = Path(sys.argv[3])
tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
active = [
    task
    for task in tasks
    if task.get("owner_agent_id") == agent_id
    and task.get("lease_id")
    and task.get("state") in {"claimed", "running"}
]
summary = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "agent_id": agent_id,
    "active_lease_count": len(active),
    "active_tasks": [
        {
            "id": task.get("id"),
            "state": task.get("state"),
            "lease_id": task.get("lease_id"),
            "leased_until": task.get("leased_until"),
            "title": task.get("title"),
        }
        for task in active
    ],
}
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(len(active))
PY
)"
      if [ "$count" = "0" ]; then
        log "mac-agent drain complete: no active leases for $agent_id"
        return 0
      fi
      log "mac-agent drain waiting: $count active lease(s) for $agent_id"
    else
      log "WARNING: could not query active leases during drain"
    fi
    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      log "ERROR: drain timed out with active leases for $agent_id"
      return 1
    fi
    sleep "${DRAIN_POLL_SECONDS:-10}"
  done
}

drain_mac_agent_before_deploy() {
  case "${DRAIN_MODE:-wait}" in
    skip|off|disabled)
      log "skipping mac-agent drain because MAC_DEPLOY_DRAIN_MODE=$DRAIN_MODE"
      return 0
      ;;
    wait|fail-fast)
      ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_DRAIN_MODE=$DRAIN_MODE"
      return 1
      ;;
  esac
  load_drain_api_env
  if ! mac_api_json GET "/health" >/dev/null 2>&1; then
    log "existing mac API is not reachable; skipping drain"
    return 0
  fi
  local agent_id
  if ! agent_id="$(agent_id_for_drain)" || [ -z "$agent_id" ]; then
    log "existing mac-agent registration for $AGENT not found; skipping drain"
    return 0
  fi
  log "pausing new claims for $agent_id before artifact replacement"
  mac_api_json POST "/agents/$agent_id/heartbeat" '{"status":"draining","health_status":"degraded"}' >/dev/null
  if [ "${DRAIN_MODE:-wait}" = "fail-fast" ]; then
    DRAIN_TIMEOUT_SECONDS=0 wait_for_agent_active_leases "$agent_id"
  else
    wait_for_agent_active_leases "$agent_id"
  fi
}

clear_mac_agent_drain_after_deploy() {
  load_drain_api_env
  if ! mac_api_json GET "/health" >/dev/null 2>&1; then
    log "WARNING: mac API is not reachable after deploy; cannot clear drain state"
    return 0
  fi
  local agent_id
  if ! agent_id="$(agent_id_for_drain)" || [ -z "$agent_id" ]; then
    return 0
  fi
  log "clearing drain state for $agent_id"
  mac_api_json POST "/agents/$agent_id/heartbeat" '{"status":"idle","health_status":"healthy"}' >/dev/null || true
}

