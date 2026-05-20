#!/usr/bin/env bash
#
# Fleet deployment entry point.
#
# Historically this file held ~2,400 lines of mixed concerns: argument parsing,
# host orchestration, the entire remote bash script body (with embedded Python
# heredocs for manifest writers, drain helpers, Hermes patches, kanban schema
# repair, migration reports, and service installers), plus the local main loop.
# Small deployment changes required reasoning over the whole script.
#
# This file is now a thin orchestrator. The real work lives in:
#
#   deploy/lib/orchestrator/arguments.sh   - usage, host/agent spec parsing
#   deploy/lib/orchestrator/archive.sh     - shell_quote, release tarball builder
#   deploy/lib/orchestrator/dispatch.sh    - deploy_host, hub helpers
#   deploy/lib/remote/*.sh                 - the bash modules assembled and shipped to each host
#
# The orchestrator pipes the concatenated remote modules into `bash -s` on
# the target, preserving the original execution semantics byte-for-byte.
# See deploy/lib/remote/README.md for the module catalog.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="$ROOT/deploy/lib"
LIB_REMOTE_DIR="$LIB_DIR/remote"

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

# shellcheck source=deploy/lib/orchestrator/arguments.sh
. "$LIB_DIR/orchestrator/arguments.sh"
# shellcheck source=deploy/lib/orchestrator/archive.sh
. "$LIB_DIR/orchestrator/archive.sh"
# shellcheck source=deploy/lib/orchestrator/dispatch.sh
. "$LIB_DIR/orchestrator/dispatch.sh"

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
