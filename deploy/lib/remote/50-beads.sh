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

