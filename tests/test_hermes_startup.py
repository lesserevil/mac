from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_runtime import build_runtime_context
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
        "MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM",
        "MAC_HERMES_GATEWAY_BASE_URL",
        "MAC_HERMES_GATEWAY_MODEL",
        "MAC_HERMES_GATEWAY_PROVIDER",
        "MAC_HERMES_LOG_SUMMARY",
        "MAC_HERMES_RUNTIME_CONTEXT_FILE",
        "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN",
        "MAC_HERMES_RUNTIME_CONTEXT_REQUIRED",
        "MAC_HERMES_INSTANCE_ID",
        "MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM",
        "MAC_HERMES_STARTUP_CHECK",
        "MAC_HERMES_SLACK_HOME_CHANNEL_NAME",
        "MAC_MEMORY_TOPOLOGY_FILE",
        "MAC_QDRANT_CHECK_TIMEOUT_SECONDS",
        "MAC_QDRANT_MEMORY",
        "MAC_QDRANT_MEMORY_ALLOW_DEGRADED",
        "MAC_QDRANT_MEMORY_ROLE",
        "MAC_REQUIRE_HERMES_STARTUP_READY",
        "MAC_REQUIRE_QDRANT_MEMORY",
        "MAC_SHARED_SERVICES_MANAGER_AGENT",
        "MAC_URL",
        "MAC_WORKER_HERMES_INSTANCE_ID",
        "ACC_HERMES_GATEWAY_BASE_URL",
        "ACC_HERMES_GATEWAY_MODEL",
        "ACC_HERMES_GATEWAY_PROVIDER",
        "ACC_QDRANT_MEMORY",
        "ACC_QDRANT_MEMORY_ALLOW_DEGRADED",
        "ACC_REQUIRE_QDRANT_MEMORY",
        "ACC_LLM_MODEL",
        "ACC_SLACK_HOME_CHANNEL_NAME",
        "CUSTOM_BASE_URL",
        "HERMES_INFERENCE_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "OPENAI_BASE_URL",
        "QDRANT_ADDRESS",
        "QDRANT_API_KEY",
        "QDRANT_FLEET_KEY",
        "QDRANT_FLEET_URL",
        "QDRANT_URL",
        "SLACK_BOT_TOKEN",
        "SLACK_HOME_CHANNEL_NAME",
        "TOKENHUB_API_KEY",
        "TOKENHUB_AGENT_KEY",
        "TOKENHUB_URL",
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


def test_startup_applies_gateway_runtime_model_shim(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    run_py = agent_dir / "gateway" / "run.py"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        run_py,
        '''import os


def _resolve_gateway_model(user_config):
    return "upstream-default"


def _resolve_runtime_agent_kwargs():
    return {}


class GatewayRunner:
    def _resolve_session_agent_runtime(self, user_config=None):
        model = _resolve_gateway_model(user_config)
        runtime_kwargs = _resolve_runtime_agent_kwargs()
        return model, runtime_kwargs
''',
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_GATEWAY_MODEL", "azure/openai/gpt-5.5")
    monkeypatch.setenv("MAC_HERMES_GATEWAY_PROVIDER", "custom")
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.invalid:8090")
    monkeypatch.setenv("TOKENHUB_API_KEY", "secret-tokenhub-key")

    report = build_hermes_startup_report()
    patched = run_py.read_text(encoding="utf-8")

    assert report["ready"] is True
    assert report["runtime"]["configured_model"] == "azure/openai/gpt-5.5"
    assert report["runtime"]["provider_override_configured"] is True
    assert report["runtime"]["base_url_override_configured"] is True
    assert report["runtime"]["gateway_runtime_shim_patch"]["applied"] is True
    assert report["runtime"]["gateway_runtime_shim_present"] is True
    assert report["checks"]["gateway_runtime_override_active"] is True
    assert "MAC_HERMES_GATEWAY_MODEL" in patched
    assert "TOKENHUB_URL" in patched
    assert "resolve_runtime_provider" in patched
    assert "secret-tokenhub-key" not in str(report)


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


def test_qdrant_shared_memory_disabled_without_endpoint(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["qdrant_level2"]["status"] == "disabled"
    assert report["checks"]["shared_qdrant_memory_ready"] is True


def test_required_qdrant_without_endpoint_blocks_readiness(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_QDRANT_MEMORY", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["qdrant_level2"]["status"] == "missing_endpoint"
    assert report["checks"]["shared_qdrant_memory_ready"] is False
    assert "required Qdrant shared memory endpoint is not configured" in " ".join(
        report["warnings"]
    )


def test_required_qdrant_endpoint_uses_redacted_topology(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    topology = hermes_home / "mac-memory-topology.json"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        topology,
        json.dumps(
            {
                "schema": "mac.hermes.memory_topology.v1",
                "agent": "hub",
                "hub": {"agent": "hub", "url": "http://secret@hub.example.internal:8789"},
                "shared_services": {
                    "qdrant": {
                        "url": "http://secret@hub.example.internal:6333?token=hidden",
                        "role": "shared_level2_memory",
                    }
                },
            }
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("QDRANT_URL", "http://secret@hub.example.internal:6333?token=hidden")
    monkeypatch.setenv("QDRANT_API_KEY", "secret-qdrant-key")

    def fake_fetch(endpoint, api_key, timeout_seconds):
        assert endpoint == "http://secret@hub.example.internal:6333?token=hidden"
        assert api_key == "secret-qdrant-key"
        assert timeout_seconds == 2
        return {"result": {"collections": [{"name": "hermes-memory"}]}}

    monkeypatch.setattr("mac.hermes_startup._fetch_qdrant_collections", fake_fetch)

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["qdrant_level2"]["status"] == "ready"
    assert report["qdrant_level2"]["collection_count"] == 1
    assert report["qdrant_level2"]["api_key_present"] is True
    assert report["qdrant_level2"]["endpoint"] == "http://redacted@hub.example.internal:6333"
    assert report["qdrant_level2"]["topology"]["hub_url"] == "http://redacted@hub.example.internal:8789"
    assert report["qdrant_level2"]["topology"]["qdrant_url"] == "http://redacted@hub.example.internal:6333"
    rendered = str(report)
    assert "secret-qdrant-key" not in rendered
    assert "token=hidden" not in rendered


def test_required_task_project_runtime_context_reports_mac_authority(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    context = build_runtime_context(
        agent_name="rocky",
        fleet_name="classic",
        mac_url="http://secret@hub.example.internal:8789?token=hidden",
        hermes_home=hermes_home,
        mac_home=tmp_path / ".mac",
        hermes_instance_id="hermes_rocky",
        agent_id="agent_rocky",
    )
    _write(context_path, json.dumps(context))
    _write(markdown_path, "private runtime command notes")
    _write(
        agent_dir / "agent" / "prompt_builder.py",
        "_load_mac_runtime_context\nMAC_HERMES_RUNTIME_CONTEXT_MARKDOWN\nmac-runtime-context.md\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["task_project_runtime"]["status"] == "ready"
    assert report["task_project_runtime"]["schema"] == "mac.hermes.runtime_context.v1"
    assert report["task_project_runtime"]["authority"]["tasks"] == "mac"
    assert report["task_project_runtime"]["authority"]["projects"] == "mac"
    assert report["task_project_runtime"]["hermes_instance_id"] == "hermes_rocky"
    assert report["task_project_runtime"]["agent_id"] == "agent_rocky"
    assert report["task_project_runtime"]["mac_url"] == "http://hub.example.internal:8789"
    assert report["checks"]["task_project_runtime_context_available"] is True
    assert report["checks"]["task_project_runtime_prompt_bridge_active"] is True
    assert report["checks"]["mac_task_project_authority_declared"] is True
    assert report["task_project_runtime"]["prompt_bridge"]["present"] is True
    rendered = str(report)
    assert "token=hidden" not in rendered
    assert "private runtime command notes" not in rendered


def test_required_task_project_runtime_context_blocks_readiness_when_missing(
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
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["task_project_runtime"]["status"] == "missing_context"
    assert report["checks"]["task_project_runtime_context_available"] is False
    assert "runtime context file is missing" in " ".join(report["warnings"])


def test_required_task_project_runtime_context_blocks_when_prompt_bridge_missing(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        context_path,
        json.dumps(
            build_runtime_context(
                agent_name="rocky",
                fleet_name="classic",
                mac_url="http://hub.example.internal:8789",
                hermes_home=hermes_home,
                mac_home=tmp_path / ".mac",
                hermes_instance_id="hermes_rocky",
                agent_id="agent_rocky",
            )
        ),
    )
    _write(markdown_path, "runtime")
    _write(agent_dir / "agent" / "prompt_builder.py", "def build_context_files_prompt(): pass\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["task_project_runtime"]["prompt_bridge"]["present"] is False
    assert report["checks"]["task_project_runtime_prompt_bridge_active"] is False
    assert "runtime prompt bridge is missing" in " ".join(report["warnings"])


def test_qdrant_degraded_override_allows_startup(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_QDRANT_MEMORY", "1")
    monkeypatch.setenv("MAC_QDRANT_MEMORY_ALLOW_DEGRADED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["qdrant_level2"]["status"] == "degraded_allowed"
    assert report["qdrant_level2"]["degradation_reason"]
    assert report["warnings"] == []


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
    assert "discord_missing_token_unconfigured" in report["logs"]["known_benign_classes"]
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
