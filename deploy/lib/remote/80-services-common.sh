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

