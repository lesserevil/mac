log "deploy log: $DEPLOY_LOG"
ensure_dns_resolution
ensure_venv_support
write_deploy_manifest "pre" "$MANIFEST_PRE"
drain_mac_agent_before_deploy
stop_existing_services_for_deploy
backup_existing_artifacts
log "installing mac source"
rm -rf "$SRC_DIR.new"
if [ -n "$DEPLOY_GIT_URL" ] && git clone --quiet --branch "$DEPLOY_GIT_BRANCH" "$DEPLOY_GIT_URL" "$SRC_DIR.new"; then
  actual_rev="$(git -C "$SRC_DIR.new" rev-parse HEAD)"
  if [ "$actual_rev" != "$DEPLOY_REV" ]; then
    git -C "$SRC_DIR.new" fetch --quiet origin "$DEPLOY_REV"
    git -C "$SRC_DIR.new" merge --ff-only "$DEPLOY_REV"
  fi
else
  log "WARNING: git clone failed or was not configured; installing archive without self-update worktree"
  mkdir -p "$SRC_DIR.new"
  tar -xzf "$ARCHIVE" -C "$SRC_DIR.new"
fi
mv "$SRC_DIR.new" "$SRC_DIR"
rm -f "$ARCHIVE"

install_beads_cli

log "creating/updating mac environment file"
"$PY" - "$ENV_FILE" "$MAC_HOME" "$HOME" "$MAC_PORT" "$HERMES_SLACK_HOME_CHANNEL_NAME" "$HERMES_GATEWAY_MODEL" "$HERMES_GATEWAY_PROVIDER" "$HERMES_GATEWAY_BASE_URL" "$HUB_URL" "$HUB_TOKEN" "$CONTROL_BIND_HOST" "$WORKER_MODE" "$WORKER_CAPABILITIES" "$WORKER_ALLOWED_PROJECTS" "$WORKER_REQUIRED_METADATA" "$WORKER_REQUIRE_CANARY" "$AGENT" <<'PY'
from pathlib import Path
import secrets
import sys

env_path = Path(sys.argv[1])
mac_home = Path(sys.argv[2])
home = Path(sys.argv[3])
port = sys.argv[4]
configured_home_channel = sys.argv[5].strip().lstrip("#")
configured_gateway_model = sys.argv[6].strip()
configured_gateway_provider = sys.argv[7].strip()
configured_gateway_base_url = sys.argv[8].strip()
configured_hub_url = sys.argv[9].strip()
configured_hub_token = sys.argv[10].strip()
configured_bind_host = sys.argv[11].strip() or "127.0.0.1"
configured_worker_mode = sys.argv[12].strip() or "heartbeat"
configured_worker_capabilities = sys.argv[13].strip() or "ops,python,hermes,review"
configured_worker_allowed_projects = sys.argv[14].strip()
configured_worker_required_metadata = sys.argv[15].strip()
configured_worker_require_canary = sys.argv[16].strip() or "1"
agent_name = sys.argv[17].strip()
values = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

values.setdefault("MAC_SECRET_KEY", secrets.token_urlsafe(48))
values.setdefault("MAC_API_TOKEN", secrets.token_urlsafe(32))
values["MAC_DB"] = str(mac_home / "mac.db")
values["MAC_PORT"] = port
values["MAC_BIND_HOST"] = configured_bind_host
values["MAC_HUB_URL"] = configured_hub_url or values.get("MAC_HUB_URL", "http://127.0.0.1:8789")
values["HERMES_HOME"] = str(home / ".hermes")
values["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
values["HERMES_REDACT_SECRETS"] = "true"
values["ACC_DIR"] = str(home / ".acc")
values["MAC_HERMES_AGENT_DIR"] = str(mac_home / "hermes-agent")
values["MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM"] = "1"
values["MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM"] = "1"
values["MAC_HERMES_STARTUP_CHECK"] = "1"
values.setdefault("MAC_REQUIRE_HERMES_STARTUP_READY", "0")
values["MAC_SELF_UPDATE_REPO"] = str(mac_home / "src" / "mac")
values["MAC_BEADS_CLI"] = str(mac_home / "bin" / "bd")
if configured_gateway_model:
    values["MAC_HERMES_GATEWAY_MODEL"] = configured_gateway_model
    values["ACC_HERMES_GATEWAY_MODEL"] = configured_gateway_model
    values["HERMES_INFERENCE_MODEL"] = configured_gateway_model
    values["ACC_LLM_MODEL"] = configured_gateway_model
if configured_gateway_provider:
    values["MAC_HERMES_GATEWAY_PROVIDER"] = configured_gateway_provider
    values["ACC_HERMES_GATEWAY_PROVIDER"] = configured_gateway_provider
    values["HERMES_INFERENCE_PROVIDER"] = configured_gateway_provider
if configured_gateway_base_url:
    values["MAC_HERMES_GATEWAY_BASE_URL"] = configured_gateway_base_url
    values["ACC_HERMES_GATEWAY_BASE_URL"] = configured_gateway_base_url
    values["CUSTOM_BASE_URL"] = configured_gateway_base_url
    values["OPENAI_BASE_URL"] = configured_gateway_base_url
if configured_hub_token:
    values["MAC_WORKER_TOKEN"] = configured_hub_token
else:
    values.setdefault("MAC_WORKER_TOKEN", values["MAC_API_TOKEN"])
values["MAC_WORKER_AGENT_NAME"] = agent_name
values["MAC_WORKER_HOSTNAME"] = agent_name
values["MAC_WORKER_MODE"] = configured_worker_mode
values["MAC_WORKER_CAPABILITIES"] = configured_worker_capabilities
values["MAC_WORKER_REQUIRE_CANARY"] = configured_worker_require_canary
values["MAC_WORKER_ALLOWED_PROJECTS"] = configured_worker_allowed_projects
values["MAC_WORKER_REQUIRED_METADATA"] = configured_worker_required_metadata
values.setdefault("MAC_WORKER_WORKSPACE", str(mac_home / "agent-workspaces"))
values.setdefault("MAC_WORKER_HEARTBEAT_INTERVAL", "30")
values.setdefault("MAC_WORKER_POLL_INTERVAL", "2")
values.setdefault("MAC_WORKER_LEASE_SECONDS", "900")
values.setdefault("MAC_WORKER_EXECUTOR", str(mac_home / "bin" / "mac-hermes-task-executor"))
values.setdefault("MAC_BEADS_BRIDGE_HUB_AGENT", "rocky")
values.setdefault("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")
if agent_name == values.get("MAC_BEADS_BRIDGE_HUB_AGENT", "rocky"):
    values.setdefault("MAC_BEADS_BRIDGE_ON_HEARTBEAT", "1")
    values.setdefault(
        "MAC_BEADS_REPOSITORIES",
        "mac=%s:repo-beads-mac:repo-beads-mac::30" % (mac_home / "src" / "mac"),
    )
else:
    values.setdefault("MAC_BEADS_BRIDGE_ON_HEARTBEAT", "0")
if "MAC_BEADS_REPOSITORIES" in values:
    # Older generated env files used "|" as an internal separator. That is
    # readable by Python but invalid in a shell-sourced env file because it is
    # parsed as a pipeline. Normalize before the file is sourced below.
    values["MAC_BEADS_REPOSITORIES"] = values["MAC_BEADS_REPOSITORIES"].replace("|", ":")
home_channel = (
    configured_home_channel
    or values.get("MAC_HERMES_SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
    or values.get("ACC_SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
    or values.get("SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
    or "rockyandfriends"
)
values["MAC_HERMES_SLACK_HOME_CHANNEL_NAME"] = home_channel
values["ACC_SLACK_HOME_CHANNEL_NAME"] = home_channel
values["SLACK_HOME_CHANNEL_NAME"] = home_channel
values.setdefault("MAC_HERMES_SYNC_SLACK_HOME_CHANNELS", "1")

lines = [
    "# Generated by mac deploy/deploy-mac-fleet.sh.",
    "# Contains bearer tokens; keep mode 0600.",
]
for key in sorted(values):
    lines.append(f"{key}={values[key]}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
env_path.chmod(0o600)
PY

normalize_hermes_redaction_env

set -a
. "$ENV_FILE"
set +a
bootstrap_beads_repositories
restore_beads_tracked_exports
sync_hermes_home_channels

log "installing mac Python package"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$VENV/bin/python" -m pip install -e "$SRC_DIR" >/dev/null

log "redeploying upstream Hermes agent"
git clone --quiet https://github.com/NousResearch/hermes-agent.git "$HERMES_DIR"
git -C "$HERMES_DIR" rev-parse HEAD > "$LOG_DIR/hermes-upstream-rev.txt"
for patch_path in \
  "$SRC_DIR/deploy/hermes/multi-slack-mvp.patch" \
  "$SRC_DIR/deploy/hermes/disable-shutdown-chat-notices.patch"
do
  if git -C "$HERMES_DIR" apply --check "$patch_path"; then
    git -C "$HERMES_DIR" apply "$patch_path"
    log "applied Hermes patch $(basename "$patch_path")"
  else
    log "ERROR: Hermes patch $(basename "$patch_path") does not apply to upstream checkout"
    git -C "$HERMES_DIR" status --short
    exit 1
  fi
done
"$PY" -m venv "$HERMES_DIR/.venv"
"$HERMES_DIR/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$HERMES_DIR/.venv/bin/python" -m pip install -e "$HERMES_DIR" >/dev/null
apply_hermes_gateway_runtime_shim
install_hermes_messaging_deps
repair_hermes_kanban_schema
log "installed Hermes agent from upstream plus mac-managed patches"

log "initializing mac database"
"$VENV/bin/mac" --db "$MAC_DB" init >/dev/null

ACC_DB=""
for candidate in "$HOME/.acc/data/fleet.db" "$HOME/.acc/data/acc.db"; do
  if [ -f "$candidate" ]; then
    ACC_DB="$candidate"
    break
  fi
done

