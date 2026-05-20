# shellcheck shell=bash
usage() {
  cat <<'USAGE'
Usage: deploy/deploy-mac-fleet.sh [agent ...]

Deploy mac as the local ACC replacement on rocky, natasha, and bullwinkle by
default. Each host gets:
  - ~/.mac/src/mac from this repository
  - ~/.mac/venv with mac installed
  - upstream NousResearch/hermes-agent in ~/.mac/hermes-agent
  - the minimal Hermes multi-Slack patch set
  - preinstalled configured Hermes messaging dependencies
  - enforced Hermes secret redaction
  - a host-local mac service, with Rocky exposed as the hub
  - a mac-agent service that registers against the Rocky hub
  - rollback script and structured deploy manifests under ~/.mac/logs
  - one-time ACC SQLite dry-run and import reports under ~/.mac/logs

Arguments may be agent names: rocky, natasha, bullwinkle.
Per-agent defaults are read from deploy/agents/<agent>/config.env when present.
Rocky is the default hub at http://100.125.137.89:8789. Spokes keep their
local control plane for host-local state and register their mac-agent service
against the hub.
USAGE
}

legacy_host_spec() {
  local requested="$1" spec name
  for spec in "${DEFAULT_HOSTS[@]}"; do
    name="${spec%%|*}"
    if [ "$name" = "$requested" ]; then
      printf '%s\n' "$spec"
      return 0
    fi
  done
  return 1
}

agent_spec() {
  local requested="$1" legacy config
  legacy="$(legacy_host_spec "$requested")" || return 1
  config="$AGENT_CONFIG_DIR/$requested/config.env"
  (
    IFS='|' read -r MAC_DEPLOY_AGENT MAC_DEPLOY_TARGET MAC_DEPLOY_OS <<EOF
$legacy
EOF
    MAC_HERMES_SLACK_HOME_CHANNEL_NAME=""
    MAC_HERMES_GATEWAY_MODEL=""
    MAC_HERMES_GATEWAY_PROVIDER="custom"
    MAC_HERMES_GATEWAY_BASE_URL=""
    MAC_DEPLOY_HUB_URL="${MAC_DEPLOY_HUB_URL:-http://100.125.137.89:8789}"
    MAC_DEPLOY_CONTROL_BIND_HOST=""
    MAC_DEPLOY_WORKER_MODE="heartbeat"
    MAC_DEPLOY_WORKER_CAPABILITIES="ops,python,hermes,review"
    MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=""
    MAC_DEPLOY_WORKER_REQUIRED_METADATA=""
    MAC_DEPLOY_WORKER_REQUIRE_CANARY="1"
    if [ -f "$config" ]; then
      # shellcheck source=/dev/null
      . "$config"
    fi
    : "${MAC_DEPLOY_AGENT:=$requested}"
    : "${MAC_DEPLOY_TARGET:?agent config must set MAC_DEPLOY_TARGET}"
    : "${MAC_DEPLOY_OS:?agent config must set MAC_DEPLOY_OS}"
    if [ -z "$MAC_DEPLOY_CONTROL_BIND_HOST" ]; then
      if [ "$MAC_DEPLOY_AGENT" = "${MAC_DEPLOY_HUB_AGENT:-rocky}" ]; then
        MAC_DEPLOY_CONTROL_BIND_HOST="0.0.0.0"
      else
        MAC_DEPLOY_CONTROL_BIND_HOST="127.0.0.1"
      fi
    fi
    printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
      "$MAC_DEPLOY_AGENT" \
      "$MAC_DEPLOY_TARGET" \
      "$MAC_DEPLOY_OS" \
      "${MAC_HERMES_SLACK_HOME_CHANNEL_NAME:-}" \
      "${MAC_HERMES_GATEWAY_MODEL:-}" \
      "${MAC_HERMES_GATEWAY_PROVIDER:-}" \
      "${MAC_HERMES_GATEWAY_BASE_URL:-}" \
      "$MAC_DEPLOY_HUB_URL" \
      "$MAC_DEPLOY_CONTROL_BIND_HOST" \
      "$MAC_DEPLOY_WORKER_MODE" \
      "$MAC_DEPLOY_WORKER_CAPABILITIES" \
      "$MAC_DEPLOY_WORKER_ALLOWED_PROJECTS" \
      "$MAC_DEPLOY_WORKER_REQUIRED_METADATA" \
      "$MAC_DEPLOY_WORKER_REQUIRE_CANARY"
  )
}

selected_hosts() {
  if [ "$#" -eq 0 ]; then
    local spec name
    for spec in "${DEFAULT_HOSTS[@]}"; do
      name="${spec%%|*}"
      agent_spec "$name"
    done
    return
  fi
  local requested
  for requested in "$@"; do
    if ! agent_spec "$requested"; then
      echo "unknown agent: $requested" >&2
      usage >&2
      return 2
    fi
  done
}

