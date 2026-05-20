"""Tests for the SSH-transport-loss reconciliation step in the fleet deploy
script.

Background: during the hardened mac fleet redeploy, the remote host finished
the deploy (post manifest + ``deploy complete`` log line both written) but the
local ``ssh`` client exited 255 with ``Connection reset by peer``. The deploy
orchestrator must, on SSH transport loss, reconnect to the host and treat the
deploy as successful if the remote actually completed it.

These tests cover both the static shape of the bash script (so the wiring
cannot silently regress) and the runtime behaviour of the
``reconcile_remote_deploy`` function, exercised against a stubbed ``ssh``
binary.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"


def script_text() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


# --- Static shape checks --------------------------------------------------


def test_reconcile_function_is_defined():
    text = script_text()
    assert "reconcile_remote_deploy()" in text, (
        "deploy-mac-fleet.sh must define a reconcile_remote_deploy() helper"
    )


def test_deploy_host_captures_ssh_exit_and_invokes_reconcile():
    text = script_text()

    # The ssh invocation must run with errexit temporarily disabled so we can
    # capture its exit code instead of aborting the deploy script.
    deploy_host_idx = text.index("deploy_host() {")
    ssh_idx = text.index('ssh -o BatchMode=yes -o ConnectTimeout=10 "$target"', deploy_host_idx)
    set_disable_idx = text.rindex("set +e", deploy_host_idx, ssh_idx)
    assert deploy_host_idx < set_disable_idx < ssh_idx, (
        "deploy_host must disable errexit before the deploy ssh call"
    )

    # And we must capture $? + re-enable errexit + call reconcile on non-zero.
    remote_end_idx = text.index("\nREMOTE\n", ssh_idx)
    tail = text[remote_end_idx:remote_end_idx + 800]
    assert "deploy_rc=$?" in tail, "deploy_host must capture the ssh exit code"
    assert "set -e" in tail, "deploy_host must re-enable errexit after capturing the exit code"
    assert "reconcile_remote_deploy" in tail, (
        "deploy_host must call reconcile_remote_deploy on non-zero ssh exit"
    )


def test_reconcile_probe_checks_deploy_log_and_post_manifest():
    text = script_text()
    fn_idx = text.index("reconcile_remote_deploy()")
    fn_end_idx = text.index("\ndeploy_host()", fn_idx)
    fn = text[fn_idx:fn_end_idx]

    # The probe must connect with BatchMode (no password prompts) and a short
    # timeout, and it must use -n to detach stdin so the heredoc is the only
    # input source.
    assert "ssh -n -o BatchMode=yes" in fn
    assert "ConnectTimeout=10" in fn

    # Inspects the per-timestamp deploy log and post manifest.
    assert 'DEPLOY_LOG="$LOG_DIR/deploy-${TS}.log"' in fn
    assert 'MANIFEST_POST="$LOG_DIR/deploy-manifest-${TS}-post.json"' in fn
    assert "deploy complete" in fn, (
        "reconcile must look for the 'deploy complete' completion marker in the deploy log"
    )

    # Emits a structured status line the caller parses.
    assert "RECONCILE_STATUS" in fn
    assert "manifest_present=1" in fn
    assert "manifest_matches_ts=1" in fn
    assert "deploy_complete=1" in fn


def test_reconcile_retries_with_backoff_and_preserves_original_rc():
    text = script_text()
    fn_idx = text.index("reconcile_remote_deploy()")
    fn_end_idx = text.index("\ndeploy_host()", fn_idx)
    fn = text[fn_idx:fn_end_idx]

    assert "max_attempts=" in fn, "reconcile must bound retries on probe failures"
    assert "sleep " in fn, "reconcile must sleep between retries"
    assert "original_rc" in fn, "reconcile must surface the original ssh exit if it cannot confirm"


# --- Behavioural checks (run the bash function with a stub ssh) -----------


def _extract_reconcile_block() -> str:
    """Pull the reconcile_remote_deploy + shell_quote definitions out of the
    deploy script so we can source them directly in a test shell.

    We can't ``source`` the whole script because it runs ``main`` and depends
    on a checked-out git tree with archives. Lifting only the helpers keeps the
    test hermetic.
    """
    text = script_text()
    shell_quote_idx = text.index("shell_quote() {")
    shell_quote_end = text.index("\n}\n", shell_quote_idx) + len("\n}\n")
    fn_idx = text.index("reconcile_remote_deploy() {")
    fn_end_idx = text.index("\n}\n", fn_idx) + len("\n}\n")
    return text[shell_quote_idx:shell_quote_end] + "\n" + text[fn_idx:fn_end_idx]


def _write_stub_ssh(tmp_path: Path, payload: str, exit_code: int = 0) -> Path:
    """Write a fake ssh binary into ``tmp_path/bin`` that ignores its args,
    emits ``payload`` on stdout and exits ``exit_code``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "ssh"
    # Use printf to avoid shell-escaping surprises.
    escaped = payload.replace("\\", "\\\\").replace("'", "'\"'\"'")
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # Record the invocation for inspection.
            echo "ssh invoked with: $*" >> "$STUB_SSH_LOG"
            printf '%s' '{escaped}'
            exit {exit_code}
            """
        )
    )
    stub.chmod(0o755)
    return stub


def _run_reconcile(tmp_path: Path, stub_payload: str, stub_exit: int = 0,
                   *, original_rc: int = 255) -> subprocess.CompletedProcess:
    block = _extract_reconcile_block()
    _write_stub_ssh(tmp_path, stub_payload, stub_exit)
    stub_log = tmp_path / "ssh.log"
    stub_log.write_text("")

    runner = tmp_path / "runner.sh"
    runner.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -uo pipefail
            export PATH="{tmp_path / 'bin'}:$PATH"
            export STUB_SSH_LOG="{stub_log}"
            # Keep tests fast — collapse the backoff to zero seconds.
            export MAC_DEPLOY_RECONCILE_SLEEP_OVERRIDE=0
            {block}
            reconcile_remote_deploy "fake@host" "fakeagent" "20260520T210000Z" {original_rc}
            rc=$?
            echo "RECONCILE_EXIT=$rc"
            exit 0
            """
        )
    )
    runner.chmod(0o755)
    return subprocess.run(
        ["/usr/bin/env", "bash", str(runner)],
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.fixture
def isolated_tmp(tmp_path: Path) -> Path:
    return tmp_path


def test_reconcile_returns_zero_when_remote_reports_complete(isolated_tmp: Path):
    payload = (
        "RECONCILE_STATUS log_present=1 manifest_present=1 manifest_matches_ts=1 "
        "deploy_complete=1 manifest_post=/x/deploy-manifest-20260520T210000Z-post.json "
        "deploy_log=/x/deploy-20260520T210000Z.log manifest_latest_exists=1\n"
        "RECONCILE_LOG_TAIL_BEGIN\n"
        "[ts] [fakeagent] deploy complete\n"
        "RECONCILE_LOG_TAIL_END\n"
    )
    result = _run_reconcile(isolated_tmp, payload, stub_exit=0, original_rc=255)
    assert "RECONCILE_EXIT=0" in result.stdout, (
        f"expected reconcile to succeed; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "reconciliation confirms remote deploy completed" in result.stdout


def test_reconcile_preserves_original_rc_when_remote_incomplete(isolated_tmp: Path):
    payload = (
        "RECONCILE_STATUS log_present=1 manifest_present=0 manifest_matches_ts=0 "
        "deploy_complete=0 manifest_post=/x/post.json deploy_log=/x/log "
        "manifest_latest_exists=0\n"
    )
    result = _run_reconcile(isolated_tmp, payload, stub_exit=0, original_rc=42)
    assert "RECONCILE_EXIT=42" in result.stdout, result.stdout
    assert "remote deploy is incomplete" in result.stdout


def test_reconcile_preserves_original_rc_when_probe_keeps_failing(isolated_tmp: Path):
    # Stub ssh fails every time (mimicking a still-unreachable host).
    result = _run_reconcile(isolated_tmp, "", stub_exit=255, original_rc=255)
    # Bash returns the literal exit code we passed in as original_rc.
    assert "RECONCILE_EXIT=255" in result.stdout, result.stdout
    assert "reconciliation exhausted" in result.stdout
