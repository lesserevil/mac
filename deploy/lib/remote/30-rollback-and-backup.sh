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

