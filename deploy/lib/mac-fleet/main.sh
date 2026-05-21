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
