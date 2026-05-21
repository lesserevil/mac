#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="$ROOT/deploy/lib/mac-fleet"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR_LOCAL="${TMPDIR:-/tmp}/mac-fleet-deploy-${TS}.$$"
ARCHIVE="${TMPDIR_LOCAL}/mac.tar.gz"
GIT_REV="$(git -C "$ROOT" rev-parse HEAD)"
GIT_URL="$(git -C "$ROOT" config --get remote.origin.url || true)"
case "$GIT_URL" in
  git@github.com:*) GIT_URL="https://github.com/${GIT_URL#git@github.com:}" ;;
  github.com:*) GIT_URL="https://github.com/${GIT_URL#github.com:}" ;;
esac
GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"
AGENT_CONFIG_DIR="${MAC_DEPLOY_AGENT_CONFIG_DIR:-$ROOT/deploy/agents}"
MAC_DEPLOY_HUB_AGENT="${MAC_DEPLOY_HUB_AGENT:-rocky}"
MAC_DEPLOY_HUB_URL="${MAC_DEPLOY_HUB_URL:-http://100.125.137.89:8789}"

source "$LIB_DIR/hosts.sh"
source "$LIB_DIR/remote-payload.sh"
source "$LIB_DIR/local.sh"
source "$LIB_DIR/main.sh"

main "$@"
