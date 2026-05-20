# shellcheck shell=bash
#
# Orchestrator helpers that ship the assembled REMOTE payload to a fleet host.
#
# `deploy_host` is the per-agent dispatch entry point. It scp's the release
# archive, then opens an ssh shell on the remote and pipes the concatenated
# bodies of deploy/lib/remote/*.sh into `bash -s`. Each module is a focused
# unit; keeping them as separate files lets reviewers reason about deploy
# concerns one at a time, while the orchestrator handles transmission.
#
# `hub_target` and `read_hub_token` are read-side helpers used to learn the
# hub URL/token before deploying spoke agents.

# Assemble the remote bash script by concatenating modules under deploy/lib/remote
# in lexical order. Each module is a self-contained slice of what used to be
# the single inline REMOTE heredoc. Stdout is the byte-identical payload that
# previously appeared between the opening and closing REMOTE sentinels.
build_remote_payload() {
  local module
  for module in "$LIB_REMOTE_DIR"/*.sh; do
    cat "$module"
  done
}

deploy_host() {
  local spec="$1" hub_token="${2:-}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary remote_archive
  IFS='|' read -r agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary <<<"$spec"
  remote_archive="/tmp/mac-${agent}-${TS}.tar.gz"

  echo "==> ${agent}: copying mac release archive"
  scp -q -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "${target}:${remote_archive}"

  echo "==> ${agent}: running one-time deploy"
  build_remote_payload | ssh -o BatchMode=yes -o ConnectTimeout=10 "$target" \
    "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_OS=$(shell_quote "$os") MAC_DEPLOY_ARCHIVE=$(shell_quote "$remote_archive") MAC_DEPLOY_TS=$(shell_quote "$TS") MAC_DEPLOY_GIT_REV=$(shell_quote "$GIT_REV") MAC_DEPLOY_GIT_URL=$(shell_quote "$GIT_URL") MAC_DEPLOY_GIT_BRANCH=$(shell_quote "$GIT_BRANCH") MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME=$(shell_quote "$home_channel") MAC_DEPLOY_HERMES_GATEWAY_MODEL=$(shell_quote "$gateway_model") MAC_DEPLOY_HERMES_GATEWAY_PROVIDER=$(shell_quote "$gateway_provider") MAC_DEPLOY_HERMES_GATEWAY_BASE_URL=$(shell_quote "$gateway_base_url") MAC_DEPLOY_HUB_URL=$(shell_quote "$hub_url") MAC_DEPLOY_HUB_TOKEN=$(shell_quote "$hub_token") MAC_DEPLOY_CONTROL_BIND_HOST=$(shell_quote "$bind_host") MAC_DEPLOY_WORKER_MODE=$(shell_quote "$worker_mode") MAC_DEPLOY_WORKER_CAPABILITIES=$(shell_quote "$worker_capabilities") MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=$(shell_quote "$worker_allowed_projects") MAC_DEPLOY_WORKER_REQUIRED_METADATA=$(shell_quote "$worker_required_metadata") MAC_DEPLOY_WORKER_REQUIRE_CANARY=$(shell_quote "$worker_require_canary") bash -s"
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
