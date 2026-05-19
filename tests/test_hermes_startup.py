from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_startup import build_hermes_startup_report
from mac.models import ValidationError
from mac.services import ControlPlane


def _write(path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _clear_startup_env(monkeypatch) -> None:
    for name in (
        "ACC_DIR",
        "HERMES_AGENT_DIR",
        "HERMES_REDACT_SECRETS",
        "HERMES_HOME",
        "MAC_HERMES_AGENT_DIR",
        "MAC_HERMES_LOG_SUMMARY",
        "MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM",
        "MAC_HERMES_STARTUP_CHECK",
        "MAC_HERMES_SLACK_HOME_CHANNEL_NAME",
        "MAC_REQUIRE_HERMES_STARTUP_READY",
        "ACC_SLACK_HOME_CHANNEL_NAME",
        "SLACK_BOT_TOKEN",
        "SLACK_HOME_CHANNEL_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


def test_startup_report_inventories_hermes_state_without_contents(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    acc_dir = tmp_path / ".acc"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "secret soul text")
    _write(hermes_home / "MEMORY.md", "private memory text")
    _write(hermes_home / "state.db", "state bytes")
    _write(hermes_home / "slack_accounts.json", '{"token":"secret-slack-token"}')
    _write(acc_dir / "data" / "fleet.db", "fleet")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ACC_DIR", str(acc_dir))

    report = build_hermes_startup_report()

    assert report["enabled"] is True
    assert report["checks"]["soul_present"] is True
    assert report["checks"]["conversation_state_present"] is True
    assert report["slack"]["needs_account_file_activation_shim"] is True
    assert report["slack"]["activation_source"] == "missing_account_file_activation"
    roles = {ref["role"] for ref in report["state_refs"] if ref["exists"]}
    assert {"soul", "long_term_memory", "conversation_state", "slack_accounts"} <= roles
    rendered = str(report)
    assert "secret soul text" not in rendered
    assert "private memory text" not in rendered
    assert "secret-slack-token" not in rendered


def test_slack_accounts_file_shim_satisfies_account_file_only_startup(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / "slack_accounts.json", '{"workspace":"T123"}')
    _write(
        agent_dir / "gateway" / "config.py",
        "def _slack_accounts_file_configured():\n    return 'slack_accounts.json'\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["slack"]["activation_source"] == "slack_accounts_file_shim"
    assert report["slack"]["account_file_activation_shim_present"] is True
    assert report["slack"]["needs_account_file_activation_shim"] is False


def test_startup_applies_home_channel_shim_for_slack_home_channels(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    run_py = agent_dir / "gateway" / "run.py"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        hermes_home / "slack_home_channels.json",
        '[{"team_id":"T123","channel_id":"C456","channel_name":"#ops"}]',
    )
    _write(
        run_py,
        '''import json
import os
from typing import Any

_hermes_home = None


def _home_target_env_var(platform_name: str) -> str:
    return f"{platform_name.upper()}_HOME_CHANNEL"


def _home_thread_env_var(platform_name: str) -> str:
    return f"{_home_target_env_var(platform_name)}_THREAD_ID"


def needs_home(source, platform_name):
    env_key = _home_target_env_var(platform_name)
    if not os.getenv(env_key):
        return True
    return False
''',
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_SLACK_HOME_CHANNEL_NAME", "ops")

    report = build_hermes_startup_report()
    patched = run_py.read_text(encoding="utf-8")

    assert report["slack"]["home_channel_file_present"] is True
    assert report["slack"]["configured_home_channel_name"] == "ops"
    assert report["slack"]["home_channel_shim_patch"]["applied"] is True
    assert report["slack"]["home_channel_shim_present"] is True
    assert "_source_has_home_target" in patched
    assert "slack_home_channels.json" in patched
    assert "if not _source_has_home_target(source, platform_name, env_key):" in patched


def test_startup_applies_slack_accounts_shim_for_explicit_hermes_checkout(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    config_py = agent_dir / "gateway" / "config.py"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / "slack_accounts.json", '{"workspace":"T123"}')
    _write(
        config_py,
        '''from typing import Callable
import os


class Platform:
    SLACK = "slack"


class PlatformConfig:
    pass


def get_hermes_home():
    return None


# -----------------------------------------------------------------------------
# Built-in platform connection checkers
_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {
}


def apply_env_overrides(config):
    # Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    if slack_token:
        if Platform.SLACK not in config.platforms:
            # No yaml config for Slack — env-only setup, enable it
            config.platforms[Platform.SLACK] = PlatformConfig()
            config.platforms[Platform.SLACK].enabled = True
        else:
            slack_config = config.platforms[Platform.SLACK]
            enabled_was_explicit = bool(slack_config.extra.pop("_enabled_explicit", False))
            if not slack_config.enabled and not enabled_was_explicit:
                # Top-level Slack settings such as channel prompts should not
                # turn an env-token setup into a disabled platform. Only an
                # explicit slack.enabled/platforms.slack.enabled false should.
                slack_config.enabled = True
        # If yaml config exists, respect its enabled flag (don't override
        # explicit enabled: false). Token is still stored so skills that
        # send Slack messages can use it without activating the gateway adapter.
        config.platforms[Platform.SLACK].token = slack_token
''',
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))

    report = build_hermes_startup_report()
    patched = config_py.read_text(encoding="utf-8")

    assert report["ready"] is True
    assert report["slack"]["activation_source"] == "slack_accounts_file_shim"
    assert report["slack"]["account_file_activation_shim_patch"]["applied"] is True
    assert "_slack_accounts_file_configured" in patched
    assert "slack_accounts_configured = _slack_accounts_file_configured()" in patched


def test_startup_applies_slack_accounts_shim_even_when_slack_has_other_activation(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    config_py = agent_dir / "gateway" / "config.py"
    _write(hermes_home / "config.yaml", "slack:\n  enabled: true\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / "slack_accounts.json", '{"workspace":"T123"}')
    _write(
        config_py,
        '''from typing import Callable
import os


class Platform:
    SLACK = "slack"


class PlatformConfig:
    pass


def get_hermes_home():
    return None


# -----------------------------------------------------------------------------
# Built-in platform connection checkers
_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {
}


def apply_env_overrides(config):
    # Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    if slack_token:
        if Platform.SLACK not in config.platforms:
            # No yaml config for Slack — env-only setup, enable it
            config.platforms[Platform.SLACK] = PlatformConfig()
            config.platforms[Platform.SLACK].enabled = True
        else:
            slack_config = config.platforms[Platform.SLACK]
            enabled_was_explicit = bool(slack_config.extra.pop("_enabled_explicit", False))
            if not slack_config.enabled and not enabled_was_explicit:
                # Top-level Slack settings such as channel prompts should not
                # turn an env-token setup into a disabled platform. Only an
                # explicit slack.enabled/platforms.slack.enabled false should.
                slack_config.enabled = True
        # If yaml config exists, respect its enabled flag (don't override
        # explicit enabled: false). Token is still stored so skills that
        # send Slack messages can use it without activating the gateway adapter.
        config.platforms[Platform.SLACK].token = slack_token
''',
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["slack"]["activation_source"] == "explicit_config"
    assert report["slack"]["account_file_activation_shim_present"] is True
    assert report["slack"]["account_file_activation_shim_patch"]["applied"] is True


def test_slack_bot_token_satisfies_upstream_slack_activation(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / "slack_accounts.json", '{"workspace":"T123"}')
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-returned")

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["slack"]["activation_source"] == "slack_bot_token"
    assert "xoxb-not-returned" not in str(report)


def test_startup_fails_readiness_when_secret_redaction_is_disabled(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["security"]["secret_redaction"]["effective"] is False
    assert report["checks"]["secret_redaction_enabled"] is False
    assert "secret redaction is disabled" in " ".join(report["warnings"])


def test_startup_detects_inherited_env_file_redaction_drift(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    acc_dir = tmp_path / ".acc"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / ".env", "HERMES_REDACT_SECRETS=false\nSECRET=not-returned")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ACC_DIR", str(acc_dir))
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "true")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["security"]["secret_redaction"]["effective"] is True
    assert report["security"]["secret_redaction"]["drift_detected"] is True
    assert report["security"]["secret_redaction"]["env_files"][0]["redact_secrets"] == "false"
    assert "not-returned" not in str(report)


def test_startup_includes_gateway_log_classification(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    log_summary = tmp_path / "hermes-log-summary.json"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        log_summary,
        '{"classes":[{"name":"secret_redaction_disabled","severity":"critical","count":1}],"actionable_count":1,"benign_count":0}',
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_LOG_SUMMARY", str(log_summary))

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["logs"]["actionable_count"] == 1
    assert report["operator_health"]["status"] == "degraded"


def test_api_exposes_hermes_startup_report_and_can_fail_closed(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    missing_home = tmp_path / "missing-hermes"
    monkeypatch.setenv("HERMES_HOME", str(missing_home))
    monkeypatch.setenv("ACC_DIR", str(tmp_path / ".acc"))

    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    response = client.get("/startup/hermes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["ready"] is False
    assert "Hermes home does not exist" in " ".join(payload["warnings"])

    monkeypatch.setenv("MAC_REQUIRE_HERMES_STARTUP_READY", "1")
    with pytest.raises(ValidationError):
        create_app(control_plane=ControlPlane.in_memory())
