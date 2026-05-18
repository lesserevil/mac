#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR_LOCAL="${TMPDIR:-/tmp}/mac-fleet-deploy-${TS}.$$"
ARCHIVE="${TMPDIR_LOCAL}/mac.tar.gz"

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
  - the minimal Hermes multi-Slack patch
  - a local mac service on 127.0.0.1:8789
  - one-time ACC SQLite dry-run and import reports under ~/.mac/logs

Arguments may be agent names: rocky, natasha, bullwinkle.
USAGE
}

selected_hosts() {
  if [ "$#" -eq 0 ]; then
    printf '%s\n' "${DEFAULT_HOSTS[@]}"
    return
  fi
  local requested spec name found
  for requested in "$@"; do
    found=""
    for spec in "${DEFAULT_HOSTS[@]}"; do
      name="${spec%%|*}"
      if [ "$name" = "$requested" ]; then
        printf '%s\n' "$spec"
        found=1
        break
      fi
    done
    if [ -z "$found" ]; then
      echo "unknown agent: $requested" >&2
      usage >&2
      return 2
    fi
  done
}

make_archive() {
  mkdir -p "$TMPDIR_LOCAL"
  git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" HEAD
}

deploy_host() {
  local spec="$1" agent target os remote_archive
  IFS='|' read -r agent target os <<<"$spec"
  remote_archive="/tmp/mac-${agent}-${TS}.tar.gz"

  echo "==> ${agent}: copying mac release archive"
  scp -q -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "${target}:${remote_archive}"

  echo "==> ${agent}: running one-time deploy"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$target" \
    "MAC_DEPLOY_AGENT='$agent' MAC_DEPLOY_OS='$os' MAC_DEPLOY_ARCHIVE='$remote_archive' bash -s" <<'REMOTE'
set -euo pipefail

AGENT="${MAC_DEPLOY_AGENT:?}"
OS_KIND="${MAC_DEPLOY_OS:?}"
ARCHIVE="${MAC_DEPLOY_ARCHIVE:?}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_PORT="${MAC_PORT:-8789}"
SRC_DIR="$MAC_HOME/src/mac"
VENV="$MAC_HOME/venv"
HERMES_DIR="$MAC_HOME/hermes-agent"
ENV_FILE="$MAC_HOME/mac.env"
LOG_DIR="$MAC_HOME/logs"
DEPLOY_LOG="$LOG_DIR/deploy-$(date -u +%Y%m%dT%H%M%SZ).log"

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

log "deploy log: $DEPLOY_LOG"
ensure_dns_resolution
ensure_venv_support
log "installing mac source"
rm -rf "$SRC_DIR.new"
mkdir -p "$SRC_DIR.new"
tar -xzf "$ARCHIVE" -C "$SRC_DIR.new"
if [ -d "$SRC_DIR" ]; then
  mv "$SRC_DIR" "$MAC_HOME/backups/mac-src.${AGENT}.$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$SRC_DIR.new" "$SRC_DIR"
rm -f "$ARCHIVE"

log "creating/updating mac environment file"
"$PY" - "$ENV_FILE" "$MAC_HOME" "$HOME" "$MAC_PORT" <<'PY'
from pathlib import Path
import secrets
import sys

env_path = Path(sys.argv[1])
mac_home = Path(sys.argv[2])
home = Path(sys.argv[3])
port = sys.argv[4]
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
values["HERMES_HOME"] = str(home / ".hermes")
values["ACC_DIR"] = str(home / ".acc")
values["MAC_HERMES_AGENT_DIR"] = str(mac_home / "hermes-agent")
values["MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM"] = "1"
values["MAC_HERMES_STARTUP_CHECK"] = "1"
values.setdefault("MAC_REQUIRE_HERMES_STARTUP_READY", "0")

lines = [
    "# Generated by mac deploy/deploy-mac-fleet.sh.",
    "# Contains bearer tokens; keep mode 0600.",
]
for key in sorted(values):
    lines.append(f"{key}={values[key]}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
env_path.chmod(0o600)
PY

set -a
. "$ENV_FILE"
set +a

log "installing mac Python package"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$VENV/bin/python" -m pip install -e "$SRC_DIR" >/dev/null

log "redeploying upstream Hermes agent"
if [ -d "$HERMES_DIR" ]; then
  mv "$HERMES_DIR" "$MAC_HOME/backups/hermes-agent.${AGENT}.$(date -u +%Y%m%dT%H%M%SZ)"
fi
git clone --quiet https://github.com/NousResearch/hermes-agent.git "$HERMES_DIR"
git -C "$HERMES_DIR" rev-parse HEAD > "$LOG_DIR/hermes-upstream-rev.txt"
if git -C "$HERMES_DIR" apply --check "$SRC_DIR/deploy/hermes/multi-slack-mvp.patch"; then
  git -C "$HERMES_DIR" apply "$SRC_DIR/deploy/hermes/multi-slack-mvp.patch"
  log "applied Hermes multi-Slack patch"
else
  log "ERROR: Hermes multi-Slack patch does not apply to upstream checkout"
  git -C "$HERMES_DIR" status --short
  exit 1
fi
"$PY" -m venv "$HERMES_DIR/.venv"
"$HERMES_DIR/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$HERMES_DIR/.venv/bin/python" -m pip install -e "$HERMES_DIR" >/dev/null
log "installed Hermes agent from upstream plus mac-managed patch"

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

if [ -n "$ACC_DB" ]; then
  if [ -f "$LOG_DIR/acc-migration-import.json" ] && [ "${MAC_FORCE_ACC_MIGRATION:-0}" != "1" ]; then
    log "existing ACC migration import report found; skipping one-time import"
    summarize_report "migration import existing" "$LOG_DIR/acc-migration-import.json"
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
  fi
else
  log "WARNING: no ACC SQLite database found under ~/.acc/data"
fi

install_linux_service() {
  local unit="/etc/systemd/system/mac.service"
  log "installing systemd service $unit"
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
ExecStart=$VENV/bin/uvicorn mac.api:create_app --factory --host 127.0.0.1 --port $MAC_PORT --workers 1 --log-level info
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now mac.service
  sleep 3
  sudo systemctl --no-pager -l status mac.service || true
  sudo journalctl -u mac.service -n 200 --no-pager > "$LOG_DIR/mac-service-journal.txt" || true
}

install_darwin_service() {
  local uid plist wrapper
  uid="$(id -u)"
  plist="$HOME/Library/LaunchAgents/com.mac.control-plane.plist"
  wrapper="$MAC_HOME/bin/mac-service"
  mkdir -p "$MAC_HOME/bin" "$HOME/Library/LaunchAgents"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
exec "$HOME/.mac/venv/bin/uvicorn" mac.api:create_app --factory --host 127.0.0.1 --port "${MAC_PORT:-8789}" --workers 1 --log-level info
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
  launchctl bootout "gui/$uid/com.mac.control-plane" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$plist"
  launchctl enable "gui/$uid/com.mac.control-plane"
  sleep 3
  launchctl list com.mac.control-plane || true
}

case "$OS_KIND" in
  linux) install_linux_service ;;
  darwin) install_darwin_service ;;
  *) log "ERROR: unsupported OS kind $OS_KIND"; exit 1 ;;
esac

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
    "slack_activation=%s shim_present=%s patch_attempted=%s patch_applied=%s patch_error=%s"
    % (
        data.get("ready"),
        len(data.get("warnings") or []),
        existing,
        slack.get("activation_source"),
        slack.get("account_file_activation_shim_present"),
        patch.get("attempted"),
        patch.get("applied"),
        bool(patch.get("error")),
    )
)
if data.get("warnings"):
    for warning in data["warnings"]:
        print("startup warning: %s" % warning)
PY

log "deploy complete"
REMOTE
}

main() {
  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
  fi
  make_archive
  local spec
  while IFS= read -r spec; do
    deploy_host "$spec"
  done < <(selected_hosts "$@")
  rm -rf "$TMPDIR_LOCAL"
}

main "$@"
