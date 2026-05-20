"""Structural tests for the modular fleet deployment script.

These tests guard the refactor that split deploy/deploy-mac-fleet.sh into
focused modules. They verify:

  - the entry point is a thin orchestrator that sources its helpers
  - every expected module exists under deploy/lib/{orchestrator,remote}
  - the assembled remote payload is well-formed bash
  - the orchestrator concatenates remote modules in deterministic order
  - the remote payload still contains every top-level function the deploy
    historically defined (so we have not silently dropped a helper)
  - the entry point file stays small (the regression we are preventing)

Run with: pytest tests/test_deploy_modules.py
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests._deploy_helpers import (
    DEPLOY_DIR,
    DEPLOY_ENTRY,
    ORCHESTRATOR_LIB_DIR,
    REMOTE_LIB_DIR,
    REPO_ROOT,
    deploy_script_text,
    orchestrator_text,
    remote_payload_text,
)


# Modules we expect to exist. Their names and order encode the deploy
# architecture; adding or renaming a module is a deliberate refactor and
# this test should be updated to match.
EXPECTED_ORCHESTRATOR_MODULES = ("arguments.sh", "archive.sh", "dispatch.sh")

EXPECTED_REMOTE_MODULES = (
    "00-header.sh",
    "10-utils.sh",
    "20-manifest.sh",
    "30-rollback-and-backup.sh",
    "40-drain.sh",
    "50-beads.sh",
    "60-hermes-runtime.sh",
    "65-hermes-kanban.sh",
    "68-procedural-install.sh",
    "70-migration-report.sh",
    "75-procedural-migration.sh",
    "80-services-common.sh",
    "81-services-linux.sh",
    "82-services-darwin.sh",
    "90-verify.sh",
    "99-procedural-finalize.sh",
)

# Top-level helper functions that the historical deploy script defined inside
# the REMOTE heredoc. If any of these disappears, deployments silently break
# on the host side.
EXPECTED_REMOTE_FUNCTIONS = (
    "log",
    "python_bin",
    "dns_lookup",
    "ensure_dns_resolution",
    "ensure_venv_support",
    "write_deploy_manifest",
    "write_rollback_script",
    "backup_existing_artifacts",
    "stop_existing_services_for_deploy",
    "load_drain_api_env",
    "mac_api_json",
    "agent_id_for_drain",
    "wait_for_agent_active_leases",
    "drain_mac_agent_before_deploy",
    "clear_mac_agent_drain_after_deploy",
    "install_beads_cli",
    "bootstrap_beads_repositories",
    "restore_beads_tracked_exports",
    "normalize_hermes_redaction_env",
    "apply_hermes_gateway_runtime_shim",
    "install_hermes_messaging_deps",
    "sync_hermes_home_channels",
    "repair_hermes_kanban_schema",
    "summarize_report",
    "write_migration_status",
    "install_linux_service",
    "install_hermes_gateway_wrapper",
    "install_mac_agent_wrapper",
    "install_linux_hermes_service",
    "install_linux_agent_service",
    "install_darwin_service",
    "install_darwin_hermes_service",
    "install_darwin_agent_service",
    "classify_gateway_logs",
    "verify_hub_registration",
)


def test_orchestrator_entry_point_is_thin():
    """deploy-mac-fleet.sh must remain small. The whole point of the refactor
    is that the entry point stays a thin dispatcher; the bulk of the deploy
    logic lives in modules. Pick a generous cap so unrelated additions don't
    fail the test, but enough to catch a regression back to a multi-thousand
    line monolith."""

    entry_lines = DEPLOY_ENTRY.read_text(encoding="utf-8").splitlines()
    assert len(entry_lines) < 200, (
        f"deploy/deploy-mac-fleet.sh has grown to {len(entry_lines)} lines; "
        "deploy logic belongs in deploy/lib/remote/*.sh modules."
    )


def test_orchestrator_modules_present_and_sourced():
    for name in EXPECTED_ORCHESTRATOR_MODULES:
        path = ORCHESTRATOR_LIB_DIR / name
        assert path.exists(), f"missing orchestrator module: {path}"
        assert path.stat().st_size > 0, f"empty orchestrator module: {path}"

    entry = DEPLOY_ENTRY.read_text(encoding="utf-8")
    for name in EXPECTED_ORCHESTRATOR_MODULES:
        assert f'orchestrator/{name}' in entry, (
            f"deploy-mac-fleet.sh does not source orchestrator/{name}"
        )


def test_remote_modules_present_and_ordered():
    actual = tuple(p.name for p in sorted(REMOTE_LIB_DIR.glob("*.sh")))
    assert actual == EXPECTED_REMOTE_MODULES, (
        f"unexpected remote module set or order. expected\n  {EXPECTED_REMOTE_MODULES}\n"
        f"got\n  {actual}"
    )


def test_remote_modules_assemble_in_lexical_order():
    """The orchestrator uses `*.sh` glob expansion to concatenate modules. The
    naming convention (NN-name.sh) must keep that lexical order matching the
    semantic order documented in deploy/lib/remote/README.md."""

    lex_names = tuple(p.name for p in sorted(REMOTE_LIB_DIR.glob("*.sh")))
    numeric_prefixes = [int(name.split("-", 1)[0]) for name in lex_names]
    assert numeric_prefixes == sorted(numeric_prefixes), (
        "module numeric prefixes are not monotonically increasing: "
        f"{lex_names}"
    )


def test_remote_payload_defines_every_expected_function():
    payload = remote_payload_text()
    for fn in EXPECTED_REMOTE_FUNCTIONS:
        # Use regex to match `fn() {` at line start (function definition).
        pattern = re.compile(rf"^{re.escape(fn)}\(\) \{{$", re.MULTILINE)
        assert pattern.search(payload), (
            f"remote payload is missing top-level helper: {fn}"
        )


def test_remote_payload_is_syntactically_valid_bash():
    payload = remote_payload_text()
    result = subprocess.run(
        ["bash", "-n", "-"],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"assembled remote payload has bash syntax errors:\n{result.stderr}"
    )


def test_orchestrator_entry_point_is_syntactically_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY_ENTRY)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"deploy-mac-fleet.sh has bash syntax errors:\n{result.stderr}"
    )


def test_dispatch_module_assembles_payload_via_concat_not_heredoc():
    """The historical script used a 2,000+ line `<<'REMOTE'` heredoc. The new
    dispatch module must use module concatenation instead, so deploy logic
    stays editable as small files."""

    dispatch = (ORCHESTRATOR_LIB_DIR / "dispatch.sh").read_text(encoding="utf-8")
    assert "build_remote_payload" in dispatch
    assert "LIB_REMOTE_DIR" in dispatch
    assert "<<'REMOTE'" not in dispatch, (
        "dispatch.sh still uses an inline REMOTE heredoc; refactor incomplete"
    )

    entry = DEPLOY_ENTRY.read_text(encoding="utf-8")
    assert "<<'REMOTE'" not in entry, (
        "deploy-mac-fleet.sh still embeds the REMOTE heredoc"
    )


def test_remote_module_readme_documents_every_module():
    readme_path = REMOTE_LIB_DIR / "README.md"
    assert readme_path.exists(), "deploy/lib/remote/README.md is missing"
    readme = readme_path.read_text(encoding="utf-8")
    for name in EXPECTED_REMOTE_MODULES:
        stem = name.split(".", 1)[0]
        assert stem in readme, (
            f"deploy/lib/remote/README.md does not mention module: {stem}"
        )


def test_deploy_script_text_helper_concatenates_orchestrator_and_remote():
    full = deploy_script_text()
    assert full == orchestrator_text() + remote_payload_text()
    # Sanity: the assembled text must still be substantially the size of the
    # historical script (~80KB remote + a few KB orchestrator).
    assert len(full) > 70_000, (
        f"assembled deploy script text is only {len(full)} bytes; modules may "
        "have lost content"
    )
