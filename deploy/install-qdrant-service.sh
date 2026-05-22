#!/usr/bin/env bash
# install-qdrant-service.sh - install/start Qdrant for the mac hub memory layer.
#
# Qdrant is a hub-managed shared service. Worker agents use the hub endpoint as
# shared level-2 memory for Hermes recall and semantic history. Bind to the
# hub's Tailscale IPv4 when available; never bind to all interfaces.
set -euo pipefail

MAC_HOME="${MAC_HOME:-$HOME/.mac}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
UNIT_TEMPLATE="${WORKSPACE}/deploy/systemd/mac-qdrant.service"
UNIT_DEST="/etc/systemd/system/mac-qdrant.service"
ENV_DEST="/etc/mac/qdrant.env"
SUPERVISOR_KIND="${QDRANT_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"

QDRANT_IMAGE="${QDRANT_IMAGE:-docker.io/qdrant/qdrant:latest}"

detect_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -1
  fi
}

default_qdrant_bind_addr() {
  local ts_ip
  ts_ip="$(detect_tailscale_ip || true)"
  if [ -n "$ts_ip" ]; then
    printf '%s\n' "$ts_ip"
  else
    printf '%s\n' "127.0.0.1"
  fi
}

QDRANT_BIND_ADDR="${QDRANT_BIND_ADDR:-$(default_qdrant_bind_addr)}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
QDRANT_DATA_DIR="${QDRANT_DATA_DIR:-/var/lib/mac/qdrant}"
QDRANT_MEMORY_LIMIT="${QDRANT_MEMORY_LIMIT:-2g}"
QDRANT_CONTAINER_NAME="${QDRANT_CONTAINER_NAME:-mac-qdrant}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_KIND"
      return
      ;;
    auto|"")
      ;;
    *)
      echo "[qdrant] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
      exit 1
      ;;
  esac
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf '%s\n' "systemd"
    return
  fi
  if command -v launchctl >/dev/null 2>&1; then
    printf '%s\n' "launchd"
    return
  fi
  if command -v supervisorctl >/dev/null 2>&1; then
    printf '%s\n' "supervisord"
    return
  fi
  echo "[qdrant] ERROR: could not detect systemd, launchd, or supervisord" >&2
  exit 1
}

run_supervisorctl() {
  if command -v sudo >/dev/null 2>&1; then
    sudo supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
  fi
}

supervisord_conf_dir() {
  if [ -n "${MAC_DEPLOY_SUPERVISOR_CONF_DIR:-}" ]; then
    printf '%s\n' "$MAC_DEPLOY_SUPERVISOR_CONF_DIR"
  elif [ -d /etc/supervisor/conf.d ]; then
    printf '%s\n' "/etc/supervisor/conf.d"
  elif [ -d /etc/supervisord.d ]; then
    printf '%s\n' "/etc/supervisord.d"
  else
    printf '%s\n' "/etc/supervisor/conf.d"
  fi
}

if [ -z "$WORKSPACE" ] || [ ! -f "$UNIT_TEMPLATE" ]; then
  echo "[qdrant] ERROR: cannot locate $UNIT_TEMPLATE" >&2
  exit 1
fi

case "$QDRANT_BIND_ADDR" in
  0.0.0.0|::|\[::\])
    echo "[qdrant] ERROR: refusing unsafe all-interface bind address: $QDRANT_BIND_ADDR" >&2
    exit 1
    ;;
esac

if [ "${1:-}" = "--print-bind-addr" ]; then
  printf '%s\n' "$QDRANT_BIND_ADDR"
  exit 0
fi

if ! command -v podman >/dev/null 2>&1; then
  echo "[qdrant] ERROR: podman is required for mac-qdrant.service" >&2
  exit 1
fi

SUPERVISOR_KIND="$(detect_supervisor)"

set_env_key() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    : > "$file"
    chmod 600 "$file"
  fi
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

service_url="http://${QDRANT_BIND_ADDR}:${QDRANT_PORT}"

echo "[qdrant] Installing Qdrant under ${SUPERVISOR_KIND}"
echo "[qdrant] Binding Qdrant to ${QDRANT_BIND_ADDR}:${QDRANT_PORT}"
sudo install -d -m 0755 /etc/mac
sudo install -d -m 0750 "$QDRANT_DATA_DIR"
sudo chown "$USER" "$QDRANT_DATA_DIR" || true
mkdir -p "$MAC_HOME/bin" "$LOG_DIR"

tmp_env="$(mktemp)"
cat > "$tmp_env" <<EOF
QDRANT_IMAGE=${QDRANT_IMAGE}
QDRANT_CONTAINER_NAME=${QDRANT_CONTAINER_NAME}
QDRANT_BIND_ADDR=${QDRANT_BIND_ADDR}
QDRANT_PORT=${QDRANT_PORT}
QDRANT_DATA_DIR=${QDRANT_DATA_DIR}
QDRANT_MEMORY_LIMIT=${QDRANT_MEMORY_LIMIT}
EOF
sudo install -m 0644 "$tmp_env" "$ENV_DEST"
rm -f "$tmp_env"

set_env_key "${MAC_HOME}/mac.env" QDRANT_URL "$service_url"
set_env_key "${MAC_HOME}/mac.env" QDRANT_ADDRESS "$service_url"
set_env_key "${MAC_HOME}/mac.env" QDRANT_FLEET_URL "$service_url"
set_env_key "${MAC_HOME}/mac.env" MAC_REQUIRE_QDRANT_MEMORY "1"
set_env_key "${MAC_HOME}/mac.env" MAC_QDRANT_MEMORY_ROLE "shared_level2"

write_qdrant_wrapper() {
  local wrapper="$MAC_HOME/bin/mac-qdrant-run"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
[ -f /etc/mac/qdrant.env ] && . /etc/mac/qdrant.env
[ -f "$HOME/.mac/qdrant.env" ] && . "$HOME/.mac/qdrant.env"
set +a
: "${QDRANT_IMAGE:=docker.io/qdrant/qdrant:latest}"
: "${QDRANT_CONTAINER_NAME:=mac-qdrant}"
: "${QDRANT_BIND_ADDR:=127.0.0.1}"
: "${QDRANT_PORT:=6333}"
: "${QDRANT_DATA_DIR:=/var/lib/mac/qdrant}"
: "${QDRANT_MEMORY_LIMIT:=2g}"
exec podman run --rm --name "$QDRANT_CONTAINER_NAME" --pull=missing \
  --security-opt=no-new-privileges --pids-limit=512 \
  --memory="$QDRANT_MEMORY_LIMIT" \
  -p "$QDRANT_BIND_ADDR:$QDRANT_PORT:6333" \
  -v "$QDRANT_DATA_DIR:/qdrant/storage" "$QDRANT_IMAGE"
EOF
  chmod 700 "$wrapper"
}

case "$SUPERVISOR_KIND" in
  systemd)
    echo "[qdrant] Installing systemd unit"
    sudo install -m 0644 "$UNIT_TEMPLATE" "$UNIT_DEST"
    sudo systemctl daemon-reload
    sudo systemctl enable mac-qdrant.service >/dev/null
    echo "[qdrant] Starting mac-qdrant.service"
    sudo systemctl restart mac-qdrant.service
    ;;
  supervisord)
    echo "[qdrant] Installing supervisord program"
    write_qdrant_wrapper
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/mac-qdrant.conf" >/dev/null <<EOF
[program:mac-qdrant]
command=$MAC_HOME/bin/mac-qdrant-run
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=30
stdout_logfile=$LOG_DIR/mac-qdrant.log
stderr_logfile=$LOG_DIR/mac-qdrant.log
environment=HOME="$HOME"
EOF
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart mac-qdrant >/dev/null 2>&1 || run_supervisorctl start mac-qdrant >/dev/null
    ;;
  launchd)
    echo "[qdrant] Installing launchd agent"
    write_qdrant_wrapper
    plist="$HOME/Library/LaunchAgents/com.mac.qdrant.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mac.qdrant</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/mac-qdrant-run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-qdrant.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-qdrant.log</string>
</dict>
</plist>
EOF
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$plist"
    fi
    uid="$(id -u)"
    launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
    launchctl bootout "gui/$uid/com.mac.qdrant" >/dev/null 2>&1 || true
    launchctl enable "gui/$uid/com.mac.qdrant"
    if ! launchctl bootstrap "gui/$uid" "$plist"; then
      launchctl kickstart -k "gui/$uid/com.mac.qdrant"
    fi
    ;;
esac

health_url="${service_url}/collections"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS --connect-timeout 2 --max-time 5 "$health_url" >/dev/null 2>&1; then
    echo "[qdrant] Qdrant ready at $health_url"
    exit 0
  fi
  sleep 2
done

echo "[qdrant] ERROR: Qdrant did not become ready at $health_url" >&2
case "$SUPERVISOR_KIND" in
  systemd) systemctl status mac-qdrant.service --no-pager -n 40 >&2 || true ;;
  supervisord) supervisorctl status mac-qdrant >&2 || true ;;
  launchd) launchctl list com.mac.qdrant >&2 || true ;;
esac
exit 1
