#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR_LOCAL="${TMPDIR:-/tmp}/mac-fleet-deploy-${TS}.$$"
ARCHIVE="${TMPDIR_LOCAL}/mac.tar.gz"
GIT_REV="$(git -C "$ROOT" rev-parse HEAD)"
GIT_URL="$(git -C "$ROOT" config --get remote.origin.url || true)"
case "$GIT_URL" in
  git@github.com:*)
    GIT_URL="https://github.com/${GIT_URL#git@github.com:}"
    ;;
  github.com:*)
    GIT_URL="https://github.com/${GIT_URL#github.com:}"
    ;;
esac
GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"
AGENT_CONFIG_DIR="${MAC_DEPLOY_AGENT_CONFIG_DIR:-$ROOT/deploy/agents}"
MAC_DEPLOY_HUB_AGENT="${MAC_DEPLOY_HUB_AGENT:-rocky}"
MAC_DEPLOY_HUB_URL="${MAC_DEPLOY_HUB_URL:-http://100.125.137.89:8789}"

DEFAULT_HOSTS=(
  "rocky|jkh@100.125.137.89|linux"
  "natasha|jkh@100.87.229.125|linux"
  "bullwinkle|jkh@100.72.16.110|darwin"
)

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

shell_quote() {
  local value="$1"
  printf "'%s'" "$(printf '%s' "$value" | sed "s/'/'\\\\''/g")"
}

make_archive() {
  mkdir -p "$TMPDIR_LOCAL"
  git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" HEAD
}

deploy_host() {
  local spec="$1" hub_token="${2:-}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary remote_archive
  IFS='|' read -r agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary <<<"$spec"
  remote_archive="/tmp/mac-${agent}-${TS}.tar.gz"

  echo "==> ${agent}: copying mac release archive"
  scp -q -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "${target}:${remote_archive}"

  echo "==> ${agent}: running one-time deploy"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$target" \
    "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_OS=$(shell_quote "$os") MAC_DEPLOY_ARCHIVE=$(shell_quote "$remote_archive") MAC_DEPLOY_TS=$(shell_quote "$TS") MAC_DEPLOY_GIT_REV=$(shell_quote "$GIT_REV") MAC_DEPLOY_GIT_URL=$(shell_quote "$GIT_URL") MAC_DEPLOY_GIT_BRANCH=$(shell_quote "$GIT_BRANCH") MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME=$(shell_quote "$home_channel") MAC_DEPLOY_HERMES_GATEWAY_MODEL=$(shell_quote "$gateway_model") MAC_DEPLOY_HERMES_GATEWAY_PROVIDER=$(shell_quote "$gateway_provider") MAC_DEPLOY_HERMES_GATEWAY_BASE_URL=$(shell_quote "$gateway_base_url") MAC_DEPLOY_HUB_URL=$(shell_quote "$hub_url") MAC_DEPLOY_HUB_TOKEN=$(shell_quote "$hub_token") MAC_DEPLOY_CONTROL_BIND_HOST=$(shell_quote "$bind_host") MAC_DEPLOY_WORKER_MODE=$(shell_quote "$worker_mode") MAC_DEPLOY_WORKER_CAPABILITIES=$(shell_quote "$worker_capabilities") MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=$(shell_quote "$worker_allowed_projects") MAC_DEPLOY_WORKER_REQUIRED_METADATA=$(shell_quote "$worker_required_metadata") MAC_DEPLOY_WORKER_REQUIRE_CANARY=$(shell_quote "$worker_require_canary") bash -s" <<'REMOTE'
set -euo pipefail

AGENT="${MAC_DEPLOY_AGENT:?}"
OS_KIND="${MAC_DEPLOY_OS:?}"
ARCHIVE="${MAC_DEPLOY_ARCHIVE:?}"
DEPLOY_TS="${MAC_DEPLOY_TS:?}"
DEPLOY_REV="${MAC_DEPLOY_GIT_REV:?}"
DEPLOY_GIT_URL="${MAC_DEPLOY_GIT_URL:-}"
DEPLOY_GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"
HERMES_SLACK_HOME_CHANNEL_NAME="${MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME:-}"
HERMES_GATEWAY_MODEL="${MAC_DEPLOY_HERMES_GATEWAY_MODEL:-}"
HERMES_GATEWAY_PROVIDER="${MAC_DEPLOY_HERMES_GATEWAY_PROVIDER:-custom}"
HERMES_GATEWAY_BASE_URL="${MAC_DEPLOY_HERMES_GATEWAY_BASE_URL:-}"
HUB_URL="${MAC_DEPLOY_HUB_URL:-http://100.125.137.89:8789}"
HUB_TOKEN="${MAC_DEPLOY_HUB_TOKEN:-}"
CONTROL_BIND_HOST="${MAC_DEPLOY_CONTROL_BIND_HOST:-127.0.0.1}"
WORKER_MODE="${MAC_DEPLOY_WORKER_MODE:-heartbeat}"
WORKER_CAPABILITIES="${MAC_DEPLOY_WORKER_CAPABILITIES:-ops,python,hermes,review}"
WORKER_ALLOWED_PROJECTS="${MAC_DEPLOY_WORKER_ALLOWED_PROJECTS:-}"
WORKER_REQUIRED_METADATA="${MAC_DEPLOY_WORKER_REQUIRED_METADATA:-}"
WORKER_REQUIRE_CANARY="${MAC_DEPLOY_WORKER_REQUIRE_CANARY:-1}"
DRAIN_MODE="${MAC_DEPLOY_DRAIN_MODE:-wait}"
DRAIN_TIMEOUT_SECONDS="${MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS:-1800}"
DRAIN_POLL_SECONDS="${MAC_DEPLOY_DRAIN_POLL_SECONDS:-10}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_PORT="${MAC_PORT:-8789}"
SRC_DIR="$MAC_HOME/src/mac"
VENV="$MAC_HOME/venv"
HERMES_DIR="$MAC_HOME/hermes-agent"
BEADS_DIR="$MAC_HOME/vendor/beads"
ENV_FILE="$MAC_HOME/mac.env"
LOG_DIR="$MAC_HOME/logs"
DEPLOY_LOG="$LOG_DIR/deploy-${DEPLOY_TS}.log"
DEPLOY_STARTED_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ROLLBACK_SCRIPT="$LOG_DIR/rollback-${DEPLOY_TS}.sh"
ROLLBACK_LATEST="$LOG_DIR/rollback-latest.sh"
MANIFEST_PRE="$LOG_DIR/deploy-manifest-${DEPLOY_TS}-pre.json"
MANIFEST_POST="$LOG_DIR/deploy-manifest-${DEPLOY_TS}-post.json"
MAC_SERVICE_NAME="mac.service"
HERMES_SERVICE_NAME="mac-hermes-gateway.service"
MAC_AGENT_SERVICE_NAME="mac-agent.service"
MAC_LAUNCHD_LABEL="com.mac.control-plane"
HERMES_LAUNCHD_LABEL="com.mac.hermes-gateway"
MAC_AGENT_LAUNCHD_LABEL="com.mac.agent"
SRC_BACKUP=""
VENV_BACKUP=""
HERMES_BACKUP=""
MAC_UNIT_BACKUP=""
HERMES_UNIT_BACKUP=""
MAC_AGENT_UNIT_BACKUP=""
MAC_PLIST_BACKUP=""
HERMES_PLIST_BACKUP=""
MAC_AGENT_PLIST_BACKUP=""
BEADS_REPO_URL="${MAC_DEPLOY_BEADS_REPO_URL:-https://github.com/steveyegge/beads.git}"
BEADS_REF="${MAC_DEPLOY_BEADS_REF:-main}"

mkdir -p "$LOG_DIR" "$MAC_HOME/backups"
exec > >(tee -a "$DEPLOY_LOG") 2>&1

log() {
  printf '[%s] [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$AGENT" "$*"
}

python_bin() {
  local candidate
  for candidate in "${MAC_PYTHON:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
    [ -n "$candidate" ] || continue
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    candidate="$(command -v "$candidate")"
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      printf '%s\n' "$candidate"
      return
    fi
  done
  log "ERROR: no Python >= 3.11 found"
  exit 1
}

PY="$(python_bin)"
export AGENT OS_KIND DEPLOY_TS DEPLOY_REV DEPLOY_GIT_URL DEPLOY_GIT_BRANCH DEPLOY_STARTED_ISO HERMES_SLACK_HOME_CHANNEL_NAME HERMES_GATEWAY_MODEL HERMES_GATEWAY_PROVIDER HERMES_GATEWAY_BASE_URL HUB_URL CONTROL_BIND_HOST WORKER_MODE WORKER_CAPABILITIES WORKER_ALLOWED_PROJECTS WORKER_REQUIRED_METADATA WORKER_REQUIRE_CANARY DRAIN_MODE DRAIN_TIMEOUT_SECONDS DRAIN_POLL_SECONDS MAC_HOME MAC_PORT SRC_DIR VENV HERMES_DIR BEADS_DIR BEADS_REPO_URL BEADS_REF ENV_FILE LOG_DIR DEPLOY_LOG PY

dns_lookup() {
  if command -v getent >/dev/null 2>&1; then
    getent hosts pypi.org >/dev/null 2>&1
    return
  fi
  "$PY" - <<'PY' >/dev/null 2>&1
import socket
socket.getaddrinfo("pypi.org", 443)
PY
}

ensure_dns_resolution() {
  if dns_lookup; then
    return
  fi
  if [ "$OS_KIND" = "linux" ] && [ -f /run/systemd/resolve/resolv.conf ]; then
    log "repairing DNS resolver path for package installation"
    sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
  fi
  if ! dns_lookup; then
    log "ERROR: DNS resolution still fails after resolver repair"
    exit 1
  fi
}

ensure_venv_support() {
  local probe="$MAC_HOME/.venv-probe"
  rm -rf "$probe"
  if "$PY" -m venv "$probe" >/dev/null 2>&1; then
    rm -rf "$probe"
    return
  fi
  rm -rf "$probe"
  if [ "$OS_KIND" = "linux" ] && command -v apt-get >/dev/null 2>&1; then
    log "installing python3-venv prerequisite"
    sudo apt-get update >/dev/null
    sudo apt-get install -y python3-venv >/dev/null
    "$PY" -m venv "$probe" >/dev/null
    rm -rf "$probe"
    return
  fi
  log "ERROR: python venv support is unavailable and could not be installed automatically"
  exit 1
}

write_deploy_manifest() {
  local stage="$1" path="$2"
  SRC_BACKUP="$SRC_BACKUP" VENV_BACKUP="$VENV_BACKUP" HERMES_BACKUP="$HERMES_BACKUP" \
  MAC_UNIT_BACKUP="$MAC_UNIT_BACKUP" HERMES_UNIT_BACKUP="$HERMES_UNIT_BACKUP" \
  MAC_AGENT_UNIT_BACKUP="$MAC_AGENT_UNIT_BACKUP" \
  MAC_PLIST_BACKUP="$MAC_PLIST_BACKUP" HERMES_PLIST_BACKUP="$HERMES_PLIST_BACKUP" \
  MAC_AGENT_PLIST_BACKUP="$MAC_AGENT_PLIST_BACKUP" \
  "$PY" - "$stage" "$path" <<'PY'
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run(cmd):
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except Exception as exc:
        return {"ok": False, "output": str(exc)}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def py_version(path):
    candidate = Path(path)
    if not candidate.exists():
        return None
    result = run([str(candidate), "--version"])
    text = result.get("stdout") or result.get("stderr")
    return text or None


def file_ref(path):
    candidate = Path(path)
    try:
        exists = candidate.exists()
    except OSError:
        exists = False
    ref = {"path": str(candidate), "exists": exists}
    if exists:
        try:
            stat = candidate.stat()
            ref.update(
                {
                    "kind": "dir" if candidate.is_dir() else "file",
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        except OSError:
            ref["exists"] = False
    return ref


def service_summary():
    if os.environ["OS_KIND"] == "linux":
        result = run(
            [
                "systemctl",
                "show",
                "mac.service",
                "mac-hermes-gateway.service",
                "mac-agent.service",
                "-p",
                "Id",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "ExecMainStatus",
                "-p",
                "NRestarts",
                "-p",
                "TimeoutStopUSec",
            ]
        )
        return {"manager": "systemd", "raw": result}
    return {
        "manager": "launchd",
        "control_plane": run(["launchctl", "list", "com.mac.control-plane"]),
        "hermes_gateway": run(["launchctl", "list", "com.mac.hermes-gateway"]),
        "mac_agent": run(["launchctl", "list", "com.mac.agent"]),
    }


stage, output_path = sys.argv[1], Path(sys.argv[2])
mac_home = Path(os.environ["MAC_HOME"])
hermes_dir = Path(os.environ["HERMES_DIR"])
acc_candidates = [
    Path.home() / ".acc" / "data" / "fleet.db",
    Path.home() / ".acc" / "data" / "acc.db",
]
hermes_config = hermes_dir / "gateway" / "config.py"
hermes_config_text = ""
try:
    hermes_config_text = hermes_config.read_text(encoding="utf-8", errors="ignore")
except OSError:
    pass
hermes_run = hermes_dir / "gateway" / "run.py"
hermes_run_text = ""
try:
    hermes_run_text = hermes_run.read_text(encoding="utf-8", errors="ignore")
except OSError:
    pass

manifest = {
    "schema_version": 1,
    "stage": stage,
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "agent": os.environ["AGENT"],
    "os_kind": os.environ["OS_KIND"],
    "deploy": {
        "timestamp": os.environ["DEPLOY_TS"],
        "mac_git_rev": os.environ["DEPLOY_REV"],
        "mac_git_url": os.environ.get("DEPLOY_GIT_URL") or None,
        "mac_git_branch": os.environ.get("DEPLOY_GIT_BRANCH") or None,
        "log": os.environ["DEPLOY_LOG"],
        "hermes_slack_home_channel_name": os.environ.get("HERMES_SLACK_HOME_CHANNEL_NAME") or None,
        "hermes_gateway_model": os.environ.get("HERMES_GATEWAY_MODEL") or None,
        "hermes_gateway_provider": os.environ.get("HERMES_GATEWAY_PROVIDER") or None,
        "hermes_gateway_base_url_configured": bool(os.environ.get("HERMES_GATEWAY_BASE_URL")),
        "hub_url": os.environ.get("HUB_URL") or None,
        "control_bind_host": os.environ.get("CONTROL_BIND_HOST") or None,
        "worker_mode": os.environ.get("WORKER_MODE") or None,
        "worker_capabilities": [
            item.strip()
            for item in (os.environ.get("WORKER_CAPABILITIES") or "").split(",")
            if item.strip()
        ],
        "worker_allowed_projects": [
            item.strip()
            for item in (os.environ.get("WORKER_ALLOWED_PROJECTS") or "").split(",")
            if item.strip()
        ],
        "worker_required_metadata_configured": bool(os.environ.get("WORKER_REQUIRED_METADATA")),
        "worker_require_canary": os.environ.get("WORKER_REQUIRE_CANARY") or None,
        "drain": {
            "mode": os.environ.get("DRAIN_MODE") or None,
            "timeout_seconds": int(os.environ.get("DRAIN_TIMEOUT_SECONDS") or 0),
            "poll_seconds": int(os.environ.get("DRAIN_POLL_SECONDS") or 0),
        },
        "beads_repo_url": os.environ.get("BEADS_REPO_URL") or None,
        "beads_ref": os.environ.get("BEADS_REF") or None,
    },
    "paths": {
        "mac_home": str(mac_home),
        "source": str(Path(os.environ["SRC_DIR"])),
        "mac_venv": str(Path(os.environ["VENV"])),
        "hermes_agent": str(hermes_dir),
        "beads_source": str(Path(os.environ["BEADS_DIR"])),
        "beads_cli": str(mac_home / "bin" / "bd"),
        "env_file": str(Path(os.environ["ENV_FILE"])),
    },
    "python": {
        "selected": os.environ["PY"],
        "selected_version": py_version(os.environ["PY"]),
        "mac_venv_version": py_version(Path(os.environ["VENV"]) / "bin" / "python"),
        "hermes_venv_version": py_version(hermes_dir / ".venv" / "bin" / "python"),
    },
    "artifacts": {
        "mac_source": file_ref(os.environ["SRC_DIR"]),
        "mac_database": file_ref(mac_home / "mac.db"),
        "hermes_agent": file_ref(hermes_dir),
        "beads_cli": file_ref(mac_home / "bin" / "bd"),
        "hermes_state": file_ref(Path.home() / ".hermes"),
        "acc_state": file_ref(Path.home() / ".acc"),
    },
    "acc": {
        "candidate_databases": [file_ref(path) for path in acc_candidates],
        "selected_database": next((str(path) for path in acc_candidates if path.exists()), None),
        "migration_status_report": file_ref(Path(os.environ["LOG_DIR"]) / "acc-migration-status.json"),
        "migration_import_report": file_ref(Path(os.environ["LOG_DIR"]) / "acc-migration-import.json"),
    },
    "hermes": {
        "origin": run(["git", "-C", str(hermes_dir), "remote", "get-url", "origin"]),
        "rev": run(["git", "-C", str(hermes_dir), "rev-parse", "HEAD"]),
        "slack_account_file_shim_present": (
            "_slack_accounts_file_configured" in hermes_config_text
            and "slack_accounts.json" in hermes_config_text
        ),
        "gateway_runtime_shim_present": (
            "MAC_HERMES_GATEWAY_MODEL" in hermes_run_text
            and "MAC_HERMES_GATEWAY_PROVIDER" in hermes_run_text
            and "resolve_runtime_provider" in hermes_run_text
        ),
        "messaging_deps_report": file_ref(Path(os.environ["LOG_DIR"]) / "hermes-messaging-deps.json"),
        "log_summary": file_ref(Path(os.environ["LOG_DIR"]) / "hermes-log-summary.json"),
    },
    "services": service_summary(),
    "backups": {
        "source": os.environ.get("SRC_BACKUP") or None,
        "mac_venv": os.environ.get("VENV_BACKUP") or None,
        "hermes_agent": os.environ.get("HERMES_BACKUP") or None,
        "mac_unit": os.environ.get("MAC_UNIT_BACKUP") or None,
        "hermes_unit": os.environ.get("HERMES_UNIT_BACKUP") or None,
        "mac_agent_unit": os.environ.get("MAC_AGENT_UNIT_BACKUP") or None,
        "mac_plist": os.environ.get("MAC_PLIST_BACKUP") or None,
        "hermes_plist": os.environ.get("HERMES_PLIST_BACKUP") or None,
        "mac_agent_plist": os.environ.get("MAC_AGENT_PLIST_BACKUP") or None,
    },
    "rollback": str(Path(os.environ["LOG_DIR"]) / "rollback-latest.sh"),
}
output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

write_rollback_script() {
  cat > "$ROLLBACK_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

MAC_HOME='$MAC_HOME'
SRC_DIR='$SRC_DIR'
VENV='$VENV'
HERMES_DIR='$HERMES_DIR'
OS_KIND='$OS_KIND'
SRC_BACKUP='$SRC_BACKUP'
VENV_BACKUP='$VENV_BACKUP'
HERMES_BACKUP='$HERMES_BACKUP'
MAC_UNIT_BACKUP='$MAC_UNIT_BACKUP'
HERMES_UNIT_BACKUP='$HERMES_UNIT_BACKUP'
MAC_AGENT_UNIT_BACKUP='$MAC_AGENT_UNIT_BACKUP'
MAC_PLIST_BACKUP='$MAC_PLIST_BACKUP'
HERMES_PLIST_BACKUP='$HERMES_PLIST_BACKUP'
MAC_AGENT_PLIST_BACKUP='$MAC_AGENT_PLIST_BACKUP'
ROLLBACK_TS="\$(date -u +%Y%m%dT%H%M%SZ)"

restore_dir() {
  local backup="\$1" dest="\$2" current_backup
  [ -n "\$backup" ] || return 0
  [ -d "\$backup" ] || return 0
  current_backup="\$MAC_HOME/backups/rollback-current.\$(basename "\$dest").\$ROLLBACK_TS"
  if [ -e "\$dest" ]; then
    mv -f "\$dest" "\$current_backup"
  fi
  command cp -a "\$backup" "\$dest"
}

case "\$OS_KIND" in
  linux)
    sudo systemctl stop mac-agent.service mac-hermes-gateway.service mac.service >/dev/null 2>&1 || true
    ;;
  darwin)
    uid="\$(id -u)"
    launchctl bootout "gui/\$uid/com.mac.agent" >/dev/null 2>&1 || true
    launchctl bootout "gui/\$uid/com.mac.hermes-gateway" >/dev/null 2>&1 || true
    launchctl bootout "gui/\$uid/com.mac.control-plane" >/dev/null 2>&1 || true
    ;;
esac

restore_dir "\$SRC_BACKUP" "\$SRC_DIR"
restore_dir "\$VENV_BACKUP" "\$VENV"
restore_dir "\$HERMES_BACKUP" "\$HERMES_DIR"

case "\$OS_KIND" in
  linux)
    [ -n "\$MAC_UNIT_BACKUP" ] && [ -f "\$MAC_UNIT_BACKUP" ] && sudo cp -f "\$MAC_UNIT_BACKUP" /etc/systemd/system/mac.service
    [ -n "\$HERMES_UNIT_BACKUP" ] && [ -f "\$HERMES_UNIT_BACKUP" ] && sudo cp -f "\$HERMES_UNIT_BACKUP" /etc/systemd/system/mac-hermes-gateway.service
    [ -n "\$MAC_AGENT_UNIT_BACKUP" ] && [ -f "\$MAC_AGENT_UNIT_BACKUP" ] && sudo cp -f "\$MAC_AGENT_UNIT_BACKUP" /etc/systemd/system/mac-agent.service
    sudo systemctl daemon-reload
    sudo systemctl restart mac.service mac-hermes-gateway.service mac-agent.service
    ;;
  darwin)
    mkdir -p "\$HOME/Library/LaunchAgents"
    [ -n "\$MAC_PLIST_BACKUP" ] && [ -f "\$MAC_PLIST_BACKUP" ] && cp -f "\$MAC_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/com.mac.control-plane.plist"
    [ -n "\$HERMES_PLIST_BACKUP" ] && [ -f "\$HERMES_PLIST_BACKUP" ] && cp -f "\$HERMES_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/com.mac.hermes-gateway.plist"
    [ -n "\$MAC_AGENT_PLIST_BACKUP" ] && [ -f "\$MAC_AGENT_PLIST_BACKUP" ] && cp -f "\$MAC_AGENT_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/com.mac.agent.plist"
    uid="\$(id -u)"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/com.mac.control-plane.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/com.mac.control-plane"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/com.mac.hermes-gateway.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/com.mac.hermes-gateway"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/com.mac.agent.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/com.mac.agent"
    ;;
esac

echo "rollback complete from $DEPLOY_TS"
EOF
  chmod 700 "$ROLLBACK_SCRIPT"
  cp -f "$ROLLBACK_SCRIPT" "$ROLLBACK_LATEST"
}

backup_existing_artifacts() {
  if [ -d "$SRC_DIR" ]; then
    SRC_BACKUP="$MAC_HOME/backups/mac-src.${AGENT}.${DEPLOY_TS}"
    log "backing up existing mac source to $SRC_BACKUP"
    mv -f "$SRC_DIR" "$SRC_BACKUP"
  fi
  if [ -d "$VENV" ]; then
    VENV_BACKUP="$MAC_HOME/backups/venv.${AGENT}.${DEPLOY_TS}"
    log "backing up existing mac venv to $VENV_BACKUP"
    mv -f "$VENV" "$VENV_BACKUP"
  fi
  if [ -d "$HERMES_DIR" ]; then
    HERMES_BACKUP="$MAC_HOME/backups/hermes-agent.${AGENT}.${DEPLOY_TS}"
    log "backing up existing Hermes checkout to $HERMES_BACKUP"
    mv -f "$HERMES_DIR" "$HERMES_BACKUP"
  fi
  write_rollback_script
}

stop_existing_services_for_deploy() {
  log "stopping existing mac services for artifact replacement"
  case "$OS_KIND" in
    linux)
      sudo systemctl stop mac-agent.service mac-hermes-gateway.service mac.service >/dev/null 2>&1 || true
      ;;
    darwin)
      local uid
      uid="$(id -u)"
      launchctl bootout "gui/$uid/com.mac.agent" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/com.mac.hermes-gateway" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/com.mac.control-plane" >/dev/null 2>&1 || true
      ;;
  esac
}

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

install_beads_cli() {
  local target="$MAC_HOME/bin/bd" existing
  mkdir -p "$MAC_HOME/bin" "$(dirname "$BEADS_DIR")"
  if [ -x "$target" ]; then
    log "bd CLI already installed at $target"
    "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
    return 0
  fi
  existing="$(command -v bd 2>/dev/null || true)"
  if [ -z "$existing" ]; then
    for candidate in "$HOME/.local/bin/bd" "$HOME/bin/bd" /opt/homebrew/bin/bd /usr/local/bin/bd; do
      if [ -x "$candidate" ]; then
        existing="$candidate"
        break
      fi
    done
  fi
  if [ -n "$existing" ] && [ -x "$existing" ]; then
    log "copying existing bd CLI from $existing to managed mac bin"
    if [ "$existing" != "$target" ]; then
      cp "$existing" "$target"
      chmod 0755 "$target"
    fi
    "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
    return 0
  fi
  for required in git make go; do
    if ! command -v "$required" >/dev/null 2>&1; then
      log "ERROR: bd CLI is required for Beads lifecycle sync, but $required is unavailable"
      exit 1
    fi
  done
  log "building bd CLI from $BEADS_REPO_URL@$BEADS_REF"
  if [ -d "$BEADS_DIR/.git" ]; then
    git -C "$BEADS_DIR" fetch --quiet origin "$BEADS_REF"
  else
    git clone --quiet "$BEADS_REPO_URL" "$BEADS_DIR"
    git -C "$BEADS_DIR" fetch --quiet origin "$BEADS_REF"
  fi
  git -C "$BEADS_DIR" checkout --quiet FETCH_HEAD
  make -C "$BEADS_DIR" build
  install -m 0755 "$BEADS_DIR/bd" "$target"
  "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
}

bootstrap_beads_repositories() {
  local raw="${MAC_BEADS_REPOSITORIES:-}" entry rest repo_path index log_path
  [ -n "$raw" ] || return 0
  index=0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    if [ "$entry" = "${entry#*=}" ]; then
      log "WARNING: skipping malformed MAC_BEADS_REPOSITORIES entry: $entry"
      continue
    fi
    rest="${entry#*=}"
    repo_path="${rest%%|*}"
    repo_path="${repo_path%%:*}"
    [ -n "$repo_path" ] || continue
    if [ ! -d "$repo_path/.beads" ]; then
      log "WARNING: skipping Beads bootstrap for $repo_path because .beads is absent"
      continue
    fi
    index=$((index + 1))
    log_path="$LOG_DIR/beads-bootstrap-${index}.log"
    log "bootstrapping Beads repository at $repo_path"
    if ! (cd "$repo_path" && "$MAC_BEADS_CLI" bootstrap --yes) > "$log_path" 2>&1; then
      log "ERROR: Beads bootstrap failed for $repo_path; see $log_path"
      cat "$log_path"
      exit 1
    fi
  done <<EOF
${raw//;/$'\n'}
EOF
}

restore_beads_tracked_exports() {
  local raw="${MAC_BEADS_REPOSITORIES:-}" entry rest repo_path index status_path
  case "${MAC_BEADS_RESTORE_TRACKED_EXPORTS:-}" in
    1|true|TRUE|yes|YES|on|ON)
      ;;
    *)
      return 0
      ;;
  esac
  [ -n "$raw" ] || return 0
  index=0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    [ "$entry" != "${entry#*=}" ] || continue
    rest="${entry#*=}"
    repo_path="${rest%%|*}"
    repo_path="${repo_path%%:*}"
    [ -n "$repo_path" ] || continue
    if ! git -C "$repo_path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      continue
    fi
    if [ -z "$(git -C "$repo_path" status --porcelain -- .beads/config.yaml .beads/issues.jsonl)" ]; then
      continue
    fi
    index=$((index + 1))
    status_path="$LOG_DIR/beads-tracked-export-restore-${index}.txt"
    git -C "$repo_path" status --porcelain -- .beads/config.yaml .beads/issues.jsonl > "$status_path" || true
    git -C "$repo_path" restore --staged --worktree -- .beads/config.yaml .beads/issues.jsonl
    log "restored tracked Beads export noise in $repo_path; status saved to $status_path"
  done <<EOF
${raw//;/$'\n'}
EOF
}

normalize_hermes_redaction_env() {
  "$PY" - "$LOG_DIR/hermes-redaction-normalization.json" "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env" "$HOME/.acc/.env" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

report_path = Path(sys.argv[1])
config_path = Path(sys.argv[2])
targets = [Path(item) for item in sys.argv[3:]]
report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "policy": "Hermes secret redaction must not be false in env or config",
    "config": {"path": str(config_path), "exists": config_path.exists(), "changed": False, "had_false": False},
    "files": [],
}
if config_path.exists() and config_path.is_file():
    try:
        config_lines = config_path.read_text(encoding="utf-8").splitlines()
        output = []
        changed = False
        for line in config_lines:
            if re.match(r"^(\s*redact_secrets\s*:\s*)(false|no|off|0)\s*$", line, flags=re.IGNORECASE):
                prefix = re.match(r"^(\s*redact_secrets\s*:\s*)", line, flags=re.IGNORECASE).group(1)
                output.append(prefix + "true")
                changed = True
                report["config"]["had_false"] = True
            else:
                output.append(line)
        if changed:
            backup = config_path.with_name(config_path.name + ".mac-redaction-backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            backup.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
            backup.chmod(0o600)
            config_path.write_text("\n".join(output) + "\n", encoding="utf-8")
            report["config"]["changed"] = True
            report["config"]["backup"] = str(backup)
    except OSError as exc:
        report["config"]["error"] = str(exc)
for path in targets:
    entry = {"path": str(path), "exists": path.exists(), "changed": False, "had_false": False}
    if not path.exists() or not path.is_file():
        report["files"].append(entry)
        continue
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        entry["error"] = str(exc)
        report["files"].append(entry)
        continue
    changed = False
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("HERMES_REDACT_SECRETS="):
            value = stripped.split("=", 1)[1].strip().strip("\"'").lower()
            if value in {"0", "false", "no", "off"}:
                entry["had_false"] = True
                output.append("HERMES_REDACT_SECRETS=true")
                changed = True
                continue
        output.append(line)
    if changed:
        backup = path.with_name(path.name + ".mac-redaction-backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        backup.write_text("\n".join(lines) + "\n", encoding="utf-8")
        backup.chmod(0o600)
        path.write_text("\n".join(output) + "\n", encoding="utf-8")
        entry["changed"] = True
        entry["backup"] = str(backup)
    report["files"].append(entry)
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if report["config"].get("changed") or any(item.get("changed") for item in report["files"]):
    print("redaction: corrected inherited secret-redaction=false drift")
else:
    print("redaction: no inherited secret-redaction=false drift found")
PY
}

apply_hermes_gateway_runtime_shim() {
  log "applying Hermes gateway runtime/model shim"
  MAC_HERMES_AGENT_DIR="$HERMES_DIR" "$VENV/bin/python" - <<'PY'
from mac.hermes_startup import apply_hermes_gateway_runtime_shim_report

report = apply_hermes_gateway_runtime_shim_report()
patch = report.get("gateway_runtime_shim_patch") or {}
configured = bool(
    report.get("configured_model")
    or report.get("provider_override_configured")
    or report.get("base_url_override_configured")
)
print(
    "gateway runtime shim: present=%s applied=%s model=%s provider_override=%s base_url_override=%s error=%s"
    % (
        report.get("gateway_runtime_shim_present"),
        patch.get("applied"),
        report.get("configured_model") or "",
        report.get("provider_override_configured"),
        report.get("base_url_override_configured"),
        patch.get("error") or "",
    )
)
if configured and (patch.get("error") or not report.get("gateway_runtime_shim_present")):
    raise SystemExit(1)
PY
}

install_hermes_messaging_deps() {
  log "preinstalling configured Hermes messaging dependencies"
  "$HERMES_DIR/.venv/bin/python" - "$HERMES_DIR" "$HOME/.hermes" "$LOG_DIR/hermes-messaging-deps.json" <<'PY'
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

repo = Path(sys.argv[1])
hermes_home = Path(sys.argv[2])
report_path = Path(sys.argv[3])
sys.path.insert(0, str(repo))

from tools.lazy_deps import LAZY_DEPS  # type: ignore


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


config = read(hermes_home / "config.yaml")
env_text = read(hermes_home / ".env")
features = set()
if (
    (hermes_home / "slack_accounts.json").exists()
    or os.environ.get("SLACK_BOT_TOKEN")
    or re.search(r"(?mi)^\s*SLACK_BOT_TOKEN\s*=", env_text)
    or re.search(r"(?mi)^\s*slack\s*:", config)
):
    features.add("platform.slack")
if (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or re.search(r"(?mi)^\s*TELEGRAM_BOT_TOKEN\s*=", env_text)
    or re.search(r"(?mi)^\s*telegram\s*:", config)
):
    features.add("platform.telegram")
if (
    os.environ.get("DISCORD_TOKEN")
    or re.search(r"(?mi)^\s*DISCORD_TOKEN\s*=", env_text)
    or re.search(r"(?mi)^\s*discord\s*:", config)
):
    features.add("platform.discord")

report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "features": [],
}
failed = False
for feature in sorted(features):
    specs = list(LAZY_DEPS.get(feature, ()))
    entry = {"feature": feature, "specs": specs, "installed": False, "error": ""}
    if not specs:
        entry["error"] = "feature is not in Hermes LAZY_DEPS"
        failed = True
        report["features"].append(entry)
        continue
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *specs],
        text=True,
        capture_output=True,
    )
    entry["installed"] = result.returncode == 0
    if result.returncode != 0:
        entry["error"] = (result.stderr or result.stdout)[-4000:]
        failed = True
    report["features"].append(entry)

imports = {
    "platform.slack": ["slack_bolt", "slack_sdk", "aiohttp"],
    "platform.telegram": ["telegram"],
    "platform.discord": ["discord", "aiohttp", "brotlicffi"],
}
for entry in report["features"]:
    modules = imports.get(entry["feature"], [])
    entry["imports_ok"] = all(importlib.util.find_spec(module) is not None for module in modules)
    if not entry["imports_ok"]:
        failed = True

report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("messaging deps: %d configured feature(s), failures=%d" % (len(report["features"]), int(failed)))
raise SystemExit(1 if failed else 0)
PY
}

sync_hermes_home_channels() {
  log "syncing Hermes Slack home-channel data"
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  "$PY" "$SRC_DIR/deploy/sync-hermes-home-channels.py" \
    "${HERMES_SLACK_ACCOUNTS_FILE:-${HERMES_HOME:-$HOME/.hermes}/slack_accounts.json}" \
    "${HERMES_SLACK_HOME_CHANNELS_FILE:-${HERMES_HOME:-$HOME/.hermes}/slack_home_channels.json}" \
    "${HERMES_SLACK_CHANNEL_TEAMS_FILE:-${HERMES_HOME:-$HOME/.hermes}/slack_channel_teams.json}" \
    "$LOG_DIR/hermes-home-channel-sync.json" || \
    log "WARNING: Hermes Slack home-channel sync failed; preserving existing home-channel data"
}

repair_hermes_kanban_schema() {
  local report="$LOG_DIR/hermes-kanban-schema-repair.json"
  log "checking Hermes kanban SQLite schema compatibility"
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  "$PY" - "$report" "$LOG_DIR" "$DEPLOY_TS" <<'PY'
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

report_path = Path(sys.argv[1])
log_dir = Path(sys.argv[2])
deploy_ts = sys.argv[3]
hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def add_column(
    conn: sqlite3.Connection,
    table: str,
    columns: set[str],
    column: str,
    ddl: str,
) -> bool:
    if column in columns:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
    columns.add(column)
    return True


def maybe_copy_column(
    conn: sqlite3.Connection,
    table: str,
    columns: set[str],
    dest: str,
    source: str,
    expression: str,
) -> None:
    if dest in columns and source in columns:
        conn.execute(f"UPDATE {table} SET {dest} = {expression}")


def candidate_dbs() -> list[Path]:
    paths: list[Path] = []
    legacy = hermes_home / "kanban.db"
    if legacy.exists():
        paths.append(legacy)
    boards = hermes_home / "kanban" / "boards"
    if boards.exists():
        paths.extend(sorted(boards.glob("*/kanban.db")))
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(path)
    return deduped


def repair_db(path: Path) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": str(path),
        "exists": path.exists(),
        "changed": False,
        "backup": None,
        "added_columns": [],
        "created_indexes": [],
        "error": None,
    }
    if not path.exists():
        return entry
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        if not table_exists(conn, "tasks"):
            return entry

        task_cols = table_columns(conn, "tasks")
        planned = []
        optional_task_columns = [
            ("tenant", "tenant TEXT"),
            ("result", "result TEXT"),
            ("branch_name", "branch_name TEXT"),
            ("idempotency_key", "idempotency_key TEXT"),
            ("consecutive_failures", "consecutive_failures INTEGER NOT NULL DEFAULT 0"),
            ("worker_pid", "worker_pid INTEGER"),
            ("last_failure_error", "last_failure_error TEXT"),
            ("max_runtime_seconds", "max_runtime_seconds INTEGER"),
            ("last_heartbeat_at", "last_heartbeat_at INTEGER"),
            ("current_run_id", "current_run_id INTEGER"),
            ("workflow_template_id", "workflow_template_id TEXT"),
            ("current_step_key", "current_step_key TEXT"),
            ("skills", "skills TEXT"),
            ("model_override", "model_override TEXT"),
            ("max_retries", "max_retries INTEGER"),
            ("session_id", "session_id TEXT"),
        ]
        for column, ddl in optional_task_columns:
            if column not in task_cols:
                planned.append(("tasks", column, ddl))

        event_cols = table_columns(conn, "task_events") if table_exists(conn, "task_events") else set()
        if event_cols and "run_id" not in event_cols:
            planned.append(("task_events", "run_id", "run_id INTEGER"))

        notify_cols = (
            table_columns(conn, "kanban_notify_subs")
            if table_exists(conn, "kanban_notify_subs")
            else set()
        )
        if notify_cols and "notifier_profile" not in notify_cols:
            planned.append(
                ("kanban_notify_subs", "notifier_profile", "notifier_profile TEXT")
            )

        if planned:
            backup = log_dir / f"{path.name}.{deploy_ts}.bak"
            shutil.copy2(path, backup)
            entry["backup"] = str(backup)

        for table, column, ddl in planned:
            cols = table_columns(conn, table)
            if add_column(conn, table, cols, column, ddl):
                entry["added_columns"].append({"table": table, "column": column})
                entry["changed"] = True
                if table == "tasks" and column == "consecutive_failures":
                    maybe_copy_column(
                        conn,
                        "tasks",
                        table_columns(conn, "tasks"),
                        "consecutive_failures",
                        "spawn_failures",
                        "COALESCE(spawn_failures, 0)",
                    )
                if table == "tasks" and column == "last_failure_error":
                    maybe_copy_column(
                        conn,
                        "tasks",
                        table_columns(conn, "tasks"),
                        "last_failure_error",
                        "last_spawn_error",
                        "last_spawn_error",
                    )

        index_specs = [
            (
                "tasks",
                "session_id",
                "idx_tasks_session_id",
                "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)",
            ),
            (
                "tasks",
                "idempotency_key",
                "idx_tasks_idempotency",
                "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key)",
            ),
            (
                "task_events",
                "run_id",
                "idx_events_run",
                "CREATE INDEX IF NOT EXISTS idx_events_run ON task_events(run_id, id)",
            ),
        ]
        for table, column, name, sql in index_specs:
            if table_exists(conn, table) and column in table_columns(conn, table):
                conn.execute(sql)
                entry["created_indexes"].append(name)
        return entry
    except Exception as exc:  # pragma: no cover - remote deploy diagnostic.
        entry["error"] = str(exc)
        return entry
    finally:
        conn.close()


report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "hermes_home": str(hermes_home),
    "databases": [repair_db(path) for path in candidate_dbs()],
}
report["changed_count"] = sum(1 for db in report["databases"] if db.get("changed"))
report["error_count"] = sum(1 for db in report["databases"] if db.get("error"))
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(
    "kanban schema repair: dbs=%d changed=%d errors=%d"
    % (len(report["databases"]), report["changed_count"], report["error_count"])
)
raise SystemExit(1 if report["error_count"] else 0)
PY
}

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

summarize_report() {
  local label="$1" path="$2"
  "$PY" - "$label" "$path" <<'PY'
import json
import sys
label, path = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
counts = data.get("counts", {})
imp = data.get("import") or {}
print(
    f"{label}: tasks={counts.get('tasks', 0)} planned={counts.get('tasks_planned_for_import', 0)} "
    f"active_blockers={counts.get('active_tasks_blocking', 0)} terminal_skipped={counts.get('terminal_tasks_skipped', 0)} "
    f"private_tables={len(data.get('skipped_private_tables') or [])} "
    f"errors={len(imp.get('errors') or []) if imp else 0}"
)
warnings = data.get("warnings") or []
if warnings:
    print(f"{label}: warnings={len(warnings)}")
PY
}

write_migration_status() {
  local status="$1" db_path="${2:-}"
  "$PY" - "$LOG_DIR/acc-migration-status.json" "$status" "$db_path" <<'PY'
import json
import sys
import time
from pathlib import Path

report_path = Path(sys.argv[1])
status = sys.argv[2]
db_path = sys.argv[3] or None
hermes_home = Path.home() / ".hermes"
state_refs = {
    "hermes_home": hermes_home.exists(),
    "hermes_state_db": (hermes_home / "state.db").exists(),
    "hermes_soul": (hermes_home / "SOUL.md").exists(),
    "hermes_memory": (hermes_home / "MEMORY.md").exists() or (hermes_home / "memories" / "MEMORY.md").exists(),
}
host_class = "acc_migrated" if status in {"imported", "already_imported", "dry_run"} else "missing_migration_source"
if status == "no_acc_sqlite_db" and (state_refs["hermes_state_db"] or state_refs["hermes_soul"] or state_refs["hermes_memory"]):
    host_class = "hermes_state_only"
report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "status": status,
    "host_class": host_class,
    "database": db_path,
    "hermes_state_refs": state_refs,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("migration status: status=%s host_class=%s" % (status, host_class))
PY
}

if [ -n "$ACC_DB" ]; then
  if [ -f "$LOG_DIR/acc-migration-import.json" ] && [ "${MAC_FORCE_ACC_MIGRATION:-0}" != "1" ]; then
    log "existing ACC migration import report found; skipping one-time import"
    summarize_report "migration import existing" "$LOG_DIR/acc-migration-import.json"
    write_migration_status "already_imported" "$ACC_DB"
  else
    log "running ACC migration dry-run from $ACC_DB"
    "$VENV/bin/mac" --db "$MAC_DB" migrate acc "$ACC_DB" \
      --mode dry-run \
      --agent-home "$HOME" \
      --report "$LOG_DIR/acc-migration-dry-run.json" \
      > "$LOG_DIR/acc-migration-dry-run.stdout.json"
    summarize_report "migration dry-run" "$LOG_DIR/acc-migration-dry-run.json"

    log "running ACC migration import with active tasks requeued"
    "$VENV/bin/mac" --db "$MAC_DB" migrate acc "$ACC_DB" \
      --mode import \
      --allow-active \
      --agent-home "$HOME" \
      --report "$LOG_DIR/acc-migration-import.json" \
      > "$LOG_DIR/acc-migration-import.stdout.json"
    summarize_report "migration import" "$LOG_DIR/acc-migration-import.json"
    write_migration_status "imported" "$ACC_DB"
  fi
else
  log "no ACC SQLite database found under ~/.acc/data; classifying host"
  write_migration_status "no_acc_sqlite_db" ""
fi

install_linux_service() {
  local unit="/etc/systemd/system/mac.service" restart_since
  log "installing systemd service $unit"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  if sudo test -f "$unit"; then
    MAC_UNIT_BACKUP="$MAC_HOME/backups/mac.service.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$MAC_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=mac control plane replacement for ACC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn mac.api:create_app --factory --host $MAC_BIND_HOST --port $MAC_PORT --workers 1 --log-level info
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable mac.service
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo systemctl restart mac.service
  sleep 3
  sudo systemctl --no-pager -l status mac.service || true
  sudo journalctl -u mac.service --since "$restart_since" --no-pager > "$LOG_DIR/mac-service-journal.txt" || true
  install_linux_hermes_service
}

install_hermes_gateway_wrapper() {
  local wrapper="$MAC_HOME/bin/hermes-gateway"
  mkdir -p "$MAC_HOME/bin"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
set +u
[ -f "$HOME/.acc/.env" ] && . "$HOME/.acc/.env"
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
[ -f "$HOME/.mac/mac.env" ] && . "$HOME/.mac/mac.env"
set -u
set +a
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_DISABLE_LAZY_INSTALLS=1
export HERMES_REDACT_SECRETS=true
if [ -z "${CUSTOM_BASE_URL:-}" ] && [ -n "${TOKENHUB_URL:-}" ]; then
  export CUSTOM_BASE_URL="${TOKENHUB_URL%/}/v1"
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
fi
exec "$HOME/.mac/hermes-agent/.venv/bin/python" "$HOME/.mac/hermes-agent/hermes" gateway run --replace
EOF
  chmod 700 "$wrapper"
}

install_mac_agent_wrapper() {
  local wrapper="$MAC_HOME/bin/mac-agent-service"
  local executor="$MAC_HOME/bin/mac-hermes-task-executor"
  local executor_py="$MAC_HOME/bin/mac-hermes-task-executor.py"
  mkdir -p "$MAC_HOME/bin"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a

: "${MAC_HUB_URL:?MAC_HUB_URL is required}"
: "${MAC_WORKER_TOKEN:?MAC_WORKER_TOKEN is required}"

agent_name="${MAC_WORKER_AGENT_NAME:-$(hostname -s 2>/dev/null || hostname)}"
host_name="${MAC_WORKER_HOSTNAME:-$agent_name}"
workspace="${MAC_WORKER_WORKSPACE:-$HOME/.mac/agent-workspaces}"
mode="${MAC_WORKER_MODE:-heartbeat}"
capabilities="${MAC_WORKER_CAPABILITIES:-ops,python,hermes,review}"
mkdir -p "$workspace"

common=(
  "$HOME/.mac/venv/bin/mac-agent"
  --url "$MAC_HUB_URL"
  --token "$MAC_WORKER_TOKEN"
  --register
  --agent-name "$agent_name"
  --hostname "$host_name"
  --capabilities "$capabilities"
  --workspace "$workspace"
  --lease-seconds "${MAC_WORKER_LEASE_SECONDS:-900}"
  --poll-interval "${MAC_WORKER_POLL_INTERVAL:-2}"
  --attestation-key-env "$HOME/.mac/mac.env"
  --rotate-missing-attestation-key
)
if [ -n "${MAC_WORKER_RESOURCES:-}" ]; then
  common+=(--resources "$MAC_WORKER_RESOURCES")
fi
if [ -n "${MAC_WORKER_ALLOWED_PROJECTS:-}" ]; then
  common+=(--allowed-projects "$MAC_WORKER_ALLOWED_PROJECTS")
fi
if [ -n "${MAC_WORKER_REQUIRED_METADATA:-}" ]; then
  common+=(--required-metadata "$MAC_WORKER_REQUIRED_METADATA")
fi
case "${MAC_WORKER_REQUIRE_CANARY:-}" in
  1|true|TRUE|yes|YES|on|ON)
    common+=(--require-canary)
    ;;
esac

case "$mode" in
  heartbeat)
    interval="${MAC_WORKER_HEARTBEAT_INTERVAL:-30}"
    while :; do
      "${common[@]}" --heartbeat-only
      sleep "$interval"
    done
    ;;
  dry-run)
    interval="${MAC_WORKER_HEARTBEAT_INTERVAL:-30}"
    while :; do
      "${common[@]}" --dry-run-claim
      sleep "$interval"
    done
    ;;
  loop)
    executor="${MAC_WORKER_EXECUTOR:-$HOME/.mac/bin/mac-hermes-task-executor}"
    if [ "$executor" = "$HOME/.mac/bin/mac-hermes-task-executor" ]; then
      test -x "$HOME/.mac/hermes-agent/.venv/bin/python"
      test -f "$HOME/.mac/hermes-agent/hermes"
    fi
    exec "${common[@]}" --loop --executor "$executor"
    ;;
  *)
    echo "unsupported MAC_WORKER_MODE=$mode" >&2
    exit 2
    ;;
esac
EOF
  chmod 700 "$wrapper"

  cat > "$executor" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
set +u
[ -f "$HOME/.acc/.env" ] && . "$HOME/.acc/.env"
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
. "$HOME/.mac/mac.env"
set -u
set +a
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_DISABLE_LAZY_INSTALLS=1
export HERMES_REDACT_SECRETS=true
if [ -z "${CUSTOM_BASE_URL:-}" ] && [ -n "${TOKENHUB_URL:-}" ]; then
  export CUSTOM_BASE_URL="${TOKENHUB_URL%/}/v1"
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
fi
exec "$HOME/.mac/venv/bin/python" "$HOME/.mac/bin/mac-hermes-task-executor.py"
EOF
  chmod 700 "$executor"

  cat > "$executor_py" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_text(value: str) -> str:
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def command_audit_id() -> str:
    seed = "%s:%s" % (time.time_ns(), os.getpid())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def audit_safe_argv(argv: list[str]) -> list[str]:
    safe: list[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            safe.append(redacted_arg(arg))
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--key", "--secret", "--password"}:
            safe.append(arg)
            redact_next = True
            continue
        if any(marker in lowered for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")):
            safe.append(redacted_arg(arg))
            continue
        if len(arg) > 512:
            safe.append("<truncated:%s:chars=%d>" % (sha256_text(arg), len(arg)))
            continue
        safe.append(arg)
    return safe


def redacted_arg(value: str) -> str:
    return "<redacted:%s:chars=%d>" % (sha256_text(value), len(value))


def safe_path_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def local_agent_id() -> str:
    configured = os.environ.get("MAC_AGENT_ID") or os.environ.get("MAC_WORKER_AGENT_ID")
    if configured:
        return configured
    name = os.environ.get("MAC_WORKER_AGENT_NAME") or os.uname().nodename.split(".")[0]
    return "agent_%s" % (safe_path_component(name.lower()).strip("_") or "default")


def post_command_audit(agent_id: str, payload: dict) -> None:
    base_url = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or "").rstrip("/")
    token = os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_TOKEN") or os.environ.get("MAC_API_TOKEN")
    if not base_url or not token or not agent_id:
        return
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        "%s/agents/%s/command-audit" % (base_url, agent_id),
        data=data,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception:
        pass


def run_audited_command(argv: list[str], cwd: Path, task_id, metadata: dict) -> subprocess.CompletedProcess[str]:
    command_id = command_audit_id()
    agent_id = local_agent_id()
    started_at = utcnow()
    started = time.monotonic()
    argv_hash = sha256_text(json.dumps(argv, separators=(",", ":")))
    base = {
        "command_id": command_id,
        "argv": audit_safe_argv(argv),
        "cwd": str(cwd),
        "task_id": task_id,
        "started_at": started_at,
        "metadata": {"component": "mac-hermes-task-executor", "argv_sha256": argv_hash, **metadata},
    }
    post_command_audit(agent_id, {**base, "phase": "started"})
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        post_command_audit(
            agent_id,
            {
                **base,
                "phase": "error",
                "completed_at": utcnow(),
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "metadata": {**base["metadata"], "error": str(exc)},
            },
        )
        raise
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    post_command_audit(
        agent_id,
        {
            **base,
            "phase": "completed" if result.returncode == 0 else "failed",
            "completed_at": utcnow(),
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "returncode": result.returncode,
            "stdout_sha256": sha256_text(stdout),
            "stderr_sha256": sha256_text(stderr),
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        },
    )
    return result


def repository_contract_section(task: dict) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else {}
    contract = origin.get("repository_contract") if isinstance(origin, dict) else None
    if not isinstance(contract, dict):
        return (
            "No repository runtime contract is attached. Do not guess bootstrap or "
            "test commands; report this as a task contract failure."
        )
    summary = {
        "schema": contract.get("schema"),
        "project": contract.get("project"),
        "contract_path": contract.get("contract_path"),
        "platforms": contract.get("platforms"),
        "toolchain": contract.get("toolchain"),
        "bootstrap": contract.get("bootstrap"),
        "test": contract.get("test"),
        "evidence": contract.get("evidence"),
    }
    return "\n".join(
        [
            json.dumps(summary, indent=2, sort_keys=True),
            "For normal repository tasks, MAC prepares a task-owned git worktree before the executor starts.",
            "Use $MAC_TASK_REPO_WORKTREE, or metadata.runtime.repository_worktree in task.json, as the only writable checkout.",
            "Treat origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only registered source state; do not edit it for feature or bug work.",
            "The registered checkout must remain clean. Commit, test, and publish from the task worktree branch, then report the pushed ref in evidence.",
            "Only explicit source-remediation tasks may repair origin.repository_path directly.",
            "Before build or test work, run bootstrap.command from the repository root when the declared tools or bootstrap.creates outputs are missing.",
            "Use test.command as the canonical verification command unless the task explicitly narrows the check.",
        ]
    )


def main() -> int:
    task_file = Path(os.environ["MAC_TASK_FILE"])
    task_workspace = Path(os.environ["MAC_TASK_WORKSPACE"])
    task_payload = json.loads(task_file.read_text(encoding="utf-8"))
    task = task_payload.get("task", task_payload)
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    review_context = metadata.get("review_context") if isinstance(metadata, dict) else None
    if isinstance(review_context, dict):
        prompt = "\n\n".join(
            [
                "You are running as a MAC fleet reviewer. Review the executor's work independently.",
                "Use the task JSON and review_context as the source of truth. Preserve secrets and do not print bearer tokens.",
                "Decide whether the executor evidence actually proves the task was completed and verified.",
                "Approve only when the evidence is coherent, pushed/published when required, and the checks are passing. Reject unverifiable, local-only, failing, or mismatched work.",
                "When you finish, report concise findings and write a review verdict manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
                "Use schema mac.worker_evidence.v1 with status=complete, evidence_type=review_verdict, verdict=approved or rejected, reviewed_evidence_id=%s, and review_id=%s."
                % (
                    review_context.get("executor_evidence_id", ""),
                    review_context.get("review_id", ""),
                ),
                "Task JSON:\n%s" % json.dumps(task, indent=2, sort_keys=True),
            ]
        )
    else:
        prompt = "\n\n".join(
            [
                "You are running as a MAC fleet worker. Complete the assigned task from first principles.",
                "Use the task JSON as the source of truth. Preserve secrets and do not print bearer tokens.",
                "When you finish, report the exact outcome, files changed, tests run, and any blockers.",
                "Also write a verifiable evidence manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
                "Use schema mac.worker_evidence.v1 with status=complete and evidence_type set to one of repo_change, documentation, investigation, deployment, test, artifact, or no_change.",
                "For repo/code work include repo.head_sha, repo.remote_ref or repo.pr_url, repo.pushed=true, repo.dirty=false, repo.files_changed, and passing tests/checks. For deployments include targets/services plus passing checks. If you cannot produce this manifest, say why; MAC will not auto-publish unverifiable work.",
                "Repository runtime contract:\n%s" % repository_contract_section(task),
                "Task JSON:\n%s" % json.dumps(task, indent=2, sort_keys=True),
            ]
        )
    hermes_py = Path.home() / ".mac" / "hermes-agent" / ".venv" / "bin" / "python"
    hermes = Path.home() / ".mac" / "hermes-agent" / "hermes"
    audit_task_id = review_context.get("task_id") if isinstance(review_context, dict) else task.get("id")
    result = run_audited_command(
        [str(hermes_py), str(hermes), "--accept-hooks", "--oneshot", prompt],
        task_workspace,
        str(audit_task_id) if audit_task_id else None,
        {"execution_kind": "review" if isinstance(review_context, dict) else "task"},
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
PY
  chmod 600 "$executor_py"
}

install_linux_hermes_service() {
  local unit="/etc/systemd/system/mac-hermes-gateway.service" restart_since
  log "installing systemd service $unit"
  if sudo test -f "$unit"; then
    HERMES_UNIT_BACKUP="$MAC_HOME/backups/mac-hermes-gateway.service.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$HERMES_UNIT_BACKUP"
    sudo chown "$USER" "$HERMES_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=mac-managed Hermes gateway
After=network-online.target mac.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$HERMES_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/hermes-gateway
Restart=always
RestartSec=5
RestartForceExitStatus=75
SuccessExitStatus=75
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 \$MAINPID
TimeoutStopSec=120
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable mac-hermes-gateway.service
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo systemctl restart mac-hermes-gateway.service
  sleep 5
  sudo systemctl --no-pager -l status mac-hermes-gateway.service || true
  sudo journalctl -u mac-hermes-gateway.service --since "$restart_since" --no-pager > "$LOG_DIR/hermes-gateway-journal.txt" || true
  install_linux_agent_service
}

install_linux_agent_service() {
  local unit="/etc/systemd/system/mac-agent.service" restart_since
  log "installing systemd service $unit"
  if sudo test -f "$unit"; then
    MAC_AGENT_UNIT_BACKUP="$MAC_HOME/backups/mac-agent.service.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$MAC_AGENT_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_AGENT_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=mac worker agent registration loop
After=network-online.target mac.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/mac-agent-service
Restart=always
RestartSec=5
TimeoutStopSec=30
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable mac-agent.service
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo systemctl restart mac-agent.service
  sleep 3
  sudo systemctl --no-pager -l status mac-agent.service || true
  sudo journalctl -u mac-agent.service --since "$restart_since" --no-pager > "$LOG_DIR/mac-agent-journal.txt" || true
}

install_darwin_service() {
  local uid plist wrapper
  uid="$(id -u)"
  plist="$HOME/Library/LaunchAgents/com.mac.control-plane.plist"
  wrapper="$MAC_HOME/bin/mac-service"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  mkdir -p "$MAC_HOME/bin" "$HOME/Library/LaunchAgents"
  if [ -f "$plist" ]; then
    MAC_PLIST_BACKUP="$MAC_HOME/backups/com.mac.control-plane.${AGENT}.${DEPLOY_TS}.plist"
    cp -f "$plist" "$MAC_PLIST_BACKUP"
    write_rollback_script
  fi
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export HERMES_REDACT_SECRETS=true
exec "$HOME/.mac/venv/bin/uvicorn" mac.api:create_app --factory --host "${MAC_BIND_HOST:-127.0.0.1}" --port "${MAC_PORT:-8789}" --workers 1 --log-level info
EOF
  chmod 700 "$wrapper"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mac.control-plane</string>
  <key>ProgramArguments</key>
  <array><string>$wrapper</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-service.log</string>
</dict>
</plist>
EOF
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist"
  fi
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/com.mac.control-plane" >/dev/null 2>&1 || true
  : > "$LOG_DIR/mac-service.log"
  launchctl enable "gui/$uid/com.mac.control-plane"
  if ! launchctl bootstrap "gui/$uid" "$plist"; then
    launchctl kickstart -k "gui/$uid/com.mac.control-plane"
  fi
  sleep 3
  launchctl list com.mac.control-plane || true
  install_darwin_hermes_service "$uid"
  install_darwin_agent_service "$uid"
}

install_darwin_hermes_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/com.mac.hermes-gateway.plist"
  if [ -f "$plist" ]; then
    HERMES_PLIST_BACKUP="$MAC_HOME/backups/com.mac.hermes-gateway.${AGENT}.${DEPLOY_TS}.plist"
    cp -f "$plist" "$HERMES_PLIST_BACKUP"
    write_rollback_script
  fi
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mac.hermes-gateway</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/hermes-gateway</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$HERMES_DIR</string>
  <key>StandardOutPath</key><string>$LOG_DIR/hermes-gateway.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/hermes-gateway.log</string>
</dict>
</plist>
EOF
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist"
  fi
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/com.mac.hermes-gateway" >/dev/null 2>&1 || true
  : > "$LOG_DIR/hermes-gateway.log"
  launchctl enable "gui/$uid/com.mac.hermes-gateway"
  if ! launchctl bootstrap "gui/$uid" "$plist"; then
    launchctl kickstart -k "gui/$uid/com.mac.hermes-gateway"
  fi
  sleep 5
  launchctl list com.mac.hermes-gateway || true
}

install_darwin_agent_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/com.mac.agent.plist"
  log "installing launchd agent $plist"
  if [ -f "$plist" ]; then
    MAC_AGENT_PLIST_BACKUP="$MAC_HOME/backups/com.mac.agent.${AGENT}.${DEPLOY_TS}.plist"
    cp -f "$plist" "$MAC_AGENT_PLIST_BACKUP"
    write_rollback_script
  fi
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mac.agent</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/mac-agent-service</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-agent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-agent.log</string>
</dict>
</plist>
EOF
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist"
  fi
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/com.mac.agent" >/dev/null 2>&1 || true
  : > "$LOG_DIR/mac-agent.log"
  launchctl enable "gui/$uid/com.mac.agent"
  if ! launchctl bootstrap "gui/$uid" "$plist"; then
    launchctl kickstart -k "gui/$uid/com.mac.agent"
  fi
  sleep 3
  launchctl list com.mac.agent || true
}

classify_gateway_logs() {
  local input="$1"
  "$PY" - "$input" "$LOG_DIR/hermes-log-summary.json" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
try:
    text = input_path.read_text(encoding="utf-8", errors="ignore")
except OSError:
    text = ""

patterns = {
    "controlled_restart": {
        "severity": "info",
        "regex": r"Shutdown context: signal=SIGTERM|Failed with result 'exit-code'",
    },
    "slack_file_public_unhandled": {
        "severity": "info",
        "regex": r"Unhandled request .*'file_public'",
    },
    "secret_redaction_disabled": {
        "severity": "critical",
        "regex": r"Secret redaction: DISABLED|HERMES_REDACT_SECRETS=false",
    },
    "traceback": {
        "severity": "error",
        "regex": r"Traceback \(most recent call last\)|\bERROR\b|Exception",
    },
}
classes = []
for name, spec in patterns.items():
    matches = re.findall(spec["regex"], text, flags=re.IGNORECASE)
    if matches:
        classes.append({"name": name, "severity": spec["severity"], "count": len(matches)})

summary = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "source": str(input_path),
    "classes": classes,
    "actionable_count": sum(1 for item in classes if item["severity"] in {"critical", "error"}),
    "benign_count": sum(1 for item in classes if item["severity"] == "info"),
}
output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(
    "gateway log summary: actionable=%d benign=%d classes=%s"
    % (
        summary["actionable_count"],
        summary["benign_count"],
        ",".join(item["name"] for item in classes) or "none",
    )
)
if summary["actionable_count"]:
    raise SystemExit(1)
PY
}

verify_hub_registration() {
  log "verifying mac-agent registration with hub ${MAC_HUB_URL:-$HUB_URL}"
  local attempt
  for attempt in $(seq 1 10); do
    if curl -fsS -H "Authorization: Bearer $MAC_WORKER_TOKEN" \
      "${MAC_HUB_URL:-$HUB_URL}/agents" > "$LOG_DIR/hub-agents.json"; then
      if "$PY" - "$LOG_DIR/hub-agents.json" "${MAC_WORKER_AGENT_NAME:-$AGENT}" <<'PY'; then
import json
import sys

agents_path, expected_name = sys.argv[1], sys.argv[2]
with open(agents_path, "r", encoding="utf-8") as handle:
    agents = json.load(handle)
for agent in agents:
    if agent.get("name") == expected_name:
        print(
            "hub registration: agent=%s id=%s status=%s health=%s last_seen=%s"
            % (
                agent.get("name"),
                agent.get("id"),
                agent.get("status"),
                agent.get("health_status"),
                agent.get("last_seen_at"),
            )
        )
        raise SystemExit(0)
print("hub registration: agent %s not present yet among %d agents" % (expected_name, len(agents)))
raise SystemExit(1)
PY
        return 0
      fi
    fi
    sleep 2
  done
  log "ERROR: mac-agent did not register with hub ${MAC_HUB_URL:-$HUB_URL}"
  return 1
}

case "$OS_KIND" in
  linux) install_linux_service ;;
  darwin) install_darwin_service ;;
  *) log "ERROR: unsupported OS kind $OS_KIND"; exit 1 ;;
esac

if [ "$OS_KIND" = "linux" ]; then
  classify_gateway_logs "$LOG_DIR/hermes-gateway-journal.txt"
else
  classify_gateway_logs "$LOG_DIR/hermes-gateway.log"
fi

log "verifying mac health and Hermes startup report"
curl -fsS "http://127.0.0.1:$MAC_PORT/health" > "$LOG_DIR/health.json"
curl -fsS -H "Authorization: Bearer $MAC_API_TOKEN" \
  "http://127.0.0.1:$MAC_PORT/startup/hermes" \
  > "$LOG_DIR/startup-hermes.json"
"$PY" - "$LOG_DIR/startup-hermes.json" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
slack = data.get("slack") or {}
refs = data.get("state_refs") or []
existing = sum(1 for ref in refs if ref.get("exists"))
patch = slack.get("account_file_activation_shim_patch") or {}
print(
    "startup: ready=%s warnings=%d state_refs_existing=%d "
    "slack_activation=%s shim_present=%s redaction=%s operator_status=%s "
    "patch_attempted=%s patch_applied=%s patch_error=%s"
    % (
        data.get("ready"),
        len(data.get("warnings") or []),
        existing,
        slack.get("activation_source"),
        slack.get("account_file_activation_shim_present"),
        (data.get("security") or {}).get("secret_redaction", {}).get("effective"),
        (data.get("operator_health") or {}).get("status"),
        patch.get("attempted"),
        patch.get("applied"),
        bool(patch.get("error")),
    )
)
if data.get("warnings"):
    for warning in data["warnings"]:
        print("startup warning: %s" % warning)
PY

verify_hub_registration
clear_mac_agent_drain_after_deploy

write_deploy_manifest "post" "$MANIFEST_POST"
cp -f "$MANIFEST_POST" "$LOG_DIR/deploy-manifest-latest.json"
log "deploy complete"
REMOTE
}

hub_target() {
  local spec agent target
  spec="$(agent_spec "$MAC_DEPLOY_HUB_AGENT")"
  IFS='|' read -r agent target _ <<<"$spec"
  printf '%s\n' "$target"
}

read_hub_token() {
  local target
  target="$(hub_target)"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$target" \
    'set -euo pipefail; set -a; . "$HOME/.mac/mac.env"; set +a; printf "%s" "${MAC_API_TOKEN:?}"'
}

main() {
  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
  fi
  make_archive
  local spec agent hub_token
  hub_token="${MAC_DEPLOY_HUB_TOKEN:-}"
  while IFS= read -r spec; do
    IFS='|' read -r agent _ <<<"$spec"
    if [ "$agent" != "$MAC_DEPLOY_HUB_AGENT" ] && [ -z "$hub_token" ]; then
      hub_token="$(read_hub_token)"
    fi
    deploy_host "$spec" "$hub_token"
    if [ "$agent" = "$MAC_DEPLOY_HUB_AGENT" ] && [ -z "$hub_token" ]; then
      hub_token="$(read_hub_token)"
    fi
  done < <(selected_hosts "$@")
  rm -rf "$TMPDIR_LOCAL"
}

main "$@"
