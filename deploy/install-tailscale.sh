#!/usr/bin/env bash
# install-tailscale.sh — install Tailscale and join the fleet tailnet.
#
# Agents need a private mesh network so hubs and workers across different
# physical networks (Azure, on-prem, cloud VMs) can reach each other.
# This script is idempotent: if tailscale is already connected it exits 0.
set -euo pipefail

AGENT_NAME="${AGENT:-$(hostname)}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"
ENV_FILE="${ENV_FILE:-$MAC_HOME/mac.env}"
SUPERVISOR_KIND="${TAILSCALE_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"

TAILSCALE_AUTH_KEY="${MAC_DEPLOY_TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME_PREFIX="${TAILSCALE_HOSTNAME_PREFIX:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME_PREFIX}${AGENT_NAME}"

if [ -z "$TAILSCALE_AUTH_KEY" ]; then
  echo "[tailscale] ERROR: MAC_DEPLOY_TAILSCALE_AUTH_KEY is not set" >&2
  exit 1
fi

set_env_key() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    : > "$file"
    chmod 600 "$file"
  fi
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_KIND"
      return
      ;;
    auto|"")
      ;;
    *)
      echo "[tailscale] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
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
  echo "[tailscale] ERROR: could not detect systemd, launchd, or supervisord" >&2
  exit 1
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

run_supervisorctl() {
  if command -v sudo >/dev/null 2>&1; then
    sudo supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
  fi
}

tailscale_connected() {
  command -v tailscale >/dev/null 2>&1 || return 1
  tailscale status --json 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('BackendState')=='Running' else 1)" 2>/dev/null
}

wait_for_tailscale_ip() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    local ip
    ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return 0
    fi
    sleep 2
  done
  return 1
}

# -- Already connected? --
if tailscale_connected; then
  ts_ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  echo "[tailscale] Already connected (IP: ${ts_ip:-unknown})"
  if [ -n "$ts_ip" ]; then
    set_env_key "$ENV_FILE" MAC_TAILSCALE_IP "$ts_ip"
    set_env_key "$ENV_FILE" MAC_TAILSCALE_HOSTNAME "$TAILSCALE_HOSTNAME"
  fi
  exit 0
fi

# -- Install tailscale package if missing --
if ! command -v tailscale >/dev/null 2>&1; then
  echo "[tailscale] Installing Tailscale package"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    # Use official install script which handles keyring + apt repo
    curl -fsSL https://tailscale.com/install.sh | sudo sh
  elif command -v brew >/dev/null 2>&1; then
    brew install tailscale
  elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sudo sh
  else
    echo "[tailscale] ERROR: unsupported platform; install tailscale manually" >&2
    exit 1
  fi
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "[tailscale] ERROR: tailscale not found after install" >&2
  exit 1
fi

# -- Start tailscaled under the detected supervisor --
SUPERVISOR_KIND="$(detect_supervisor)"
echo "[tailscale] Starting tailscaled under ${SUPERVISOR_KIND}"

mkdir -p "$LOG_DIR"

case "$SUPERVISOR_KIND" in
  systemd)
    sudo systemctl enable tailscaled >/dev/null 2>&1 || true
    sudo systemctl start tailscaled
    ;;
  supervisord)
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/tailscaled.conf" >/dev/null <<EOF
[program:tailscaled]
command=/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock --port=41641
directory=/var/lib/tailscale
user=root
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=15
stdout_logfile=$LOG_DIR/tailscaled.log
stderr_logfile=$LOG_DIR/tailscaled.log
EOF
    sudo mkdir -p /var/lib/tailscale /run/tailscale
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart tailscaled >/dev/null 2>&1 \
      || run_supervisorctl start tailscaled >/dev/null
    ;;
  launchd)
    # Tailscale install.sh places a launchd plist on macOS
    sudo launchctl enable system/com.tailscale.tailscaled 2>/dev/null || true
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.tailscale.tailscaled.plist 2>/dev/null || true
    sudo launchctl kickstart -k system/com.tailscale.tailscaled 2>/dev/null || true
    ;;
esac

# Wait for tailscaled socket to be ready before running tailscale up
for i in 1 2 3 4 5 6 7 8 9 10; do
  if tailscale status >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# -- Join the tailnet --
echo "[tailscale] Joining tailnet as hostname='${TAILSCALE_HOSTNAME}'"
tailscale up \
  --auth-key="$TAILSCALE_AUTH_KEY" \
  --hostname="$TAILSCALE_HOSTNAME" \
  --accept-routes \
  --accept-dns=true

# -- Wait for Tailscale IP --
ts_ip="$(wait_for_tailscale_ip || true)"
if [ -z "$ts_ip" ]; then
  echo "[tailscale] ERROR: did not get a Tailscale IP after joining" >&2
  tailscale status >&2 || true
  exit 1
fi

echo "[tailscale] Connected — hostname=${TAILSCALE_HOSTNAME} IP=${ts_ip}"

# Persist to mac.env for qdrant bind-addr and hub URL derivation
set_env_key "$ENV_FILE" MAC_TAILSCALE_IP "$ts_ip"
set_env_key "$ENV_FILE" MAC_TAILSCALE_HOSTNAME "$TAILSCALE_HOSTNAME"
