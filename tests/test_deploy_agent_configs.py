from pathlib import Path

import yaml

from tests._deploy_helpers import deploy_script_text, REPO_ROOT

ROOT = REPO_ROOT


def parse_env(path: Path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_fleet_agent_configs_include_hermes_home_channel():
    expected = {
        "rocky": ("jkh@100.125.137.89", "linux"),
        "natasha": ("jkh@100.87.229.125", "linux"),
        "bullwinkle": ("jkh@100.72.16.110", "darwin"),
    }

    for agent, (target, os_kind) in expected.items():
        values = parse_env(ROOT / "deploy" / "agents" / agent / "config.env")
        assert values["MAC_DEPLOY_AGENT"] == agent
        assert values["MAC_DEPLOY_TARGET"] == target
        assert values["MAC_DEPLOY_OS"] == os_kind
        assert values["MAC_HERMES_SLACK_HOME_CHANNEL_NAME"] == "rockyandfriends"


def test_fleet_agent_configs_use_distinct_hermes_models():
    expected_models = {
        "rocky": "azure/openai/gpt-5.5",
        "natasha": "azure/anthropic/claude-opus-4-7",
        "bullwinkle": "gcp/google/gemini-2.5-pro",
    }
    models = []

    for agent, expected_model in expected_models.items():
        values = parse_env(ROOT / "deploy" / "agents" / agent / "config.env")
        model = values["MAC_HERMES_GATEWAY_MODEL"]
        models.append(model)
        assert model == expected_model
        assert values["MAC_HERMES_GATEWAY_PROVIDER"] == "custom"

    assert len(set(models)) == len(models)


def test_fleet_agent_configs_enable_review_capability_by_default():
    script = deploy_script_text()
    template = parse_env(ROOT / "deploy" / "agents" / "TEMPLATE" / "config.env")

    assert 'MAC_DEPLOY_WORKER_CAPABILITIES="ops,python,hermes,review"' in script
    assert 'WORKER_CAPABILITIES="${MAC_DEPLOY_WORKER_CAPABILITIES:-ops,python,hermes,review}"' in script
    assert 'configured_worker_capabilities = sys.argv[13].strip() or "ops,python,hermes,review"' in script
    assert 'capabilities="${MAC_WORKER_CAPABILITIES:-ops,python,hermes,review}"' in script
    assert template["MAC_DEPLOY_WORKER_CAPABILITIES"] == "ops,python,hermes,review"

    for agent in ("rocky", "natasha", "bullwinkle"):
        values = parse_env(ROOT / "deploy" / "agents" / agent / "config.env")
        assert values["MAC_DEPLOY_WORKER_CAPABILITIES"] == "ops,python,hermes,review"


def test_fleet_deploy_persists_or_recovers_worker_attestation_key():
    script = deploy_script_text()

    assert '--attestation-key-env "$HOME/.mac/mac.env"' in script
    assert "--rotate-missing-attestation-key" in script
    assert "evidence_type=review_verdict" in script


def test_fleet_deploy_drain_agent_lookup_does_not_pipe_json_into_python_stdin():
    script = deploy_script_text()
    agent_id_for_drain = script.split("agent_id_for_drain() {", 1)[1].split(
        "wait_for_agent_active_leases() {", 1
    )[0]

    assert 'response="$(mac_api_json GET "/agents")"' in agent_id_for_drain
    assert "json.loads(sys.argv[2])" in agent_id_for_drain
    assert 'mac_api_json GET "/agents" |' not in agent_id_for_drain


def test_fleet_deploy_bootstraps_beads_cli_for_bridge():
    script = deploy_script_text()

    assert "BEADS_REPO_URL=\"${MAC_DEPLOY_BEADS_REPO_URL:-https://github.com/steveyegge/beads.git}\"" in script
    assert "install_beads_cli()" in script
    assert '"$HOME/.local/bin/bd"' in script
    assert '"$HOME/bin/bd"' in script
    assert "bootstrap_beads_repositories()" in script
    assert "restore_beads_tracked_exports()" in script
    assert 'values.setdefault("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")' in script
    assert 'bootstrap --yes' in script
    source_install_block = script.split('mv "$SRC_DIR.new" "$SRC_DIR"', 1)[1].split(
        'log "creating/updating mac environment file"', 1
    )[0]
    assert "install_beads_cli" in source_install_block
    env_source_block = script.split('. "$ENV_FILE"', 1)[1].split(
        'log "installing mac Python package"', 1
    )[0]
    assert "bootstrap_beads_repositories" in env_source_block
    assert "restore_beads_tracked_exports" in env_source_block
    assert 'values["MAC_BEADS_CLI"] = str(mac_home / "bin" / "bd")' in script
    assert '[_beads_cli(), "ready", "--json"]' in (
        ROOT / "src" / "mac" / "services.py"
    ).read_text(encoding="utf-8")


def test_fleet_deploy_applies_hermes_patch_set():
    script = deploy_script_text()
    quench_patch = ROOT / "deploy" / "hermes" / "disable-shutdown-chat-notices.patch"

    assert "multi-slack-mvp.patch" in script
    assert "disable-shutdown-chat-notices.patch" in script
    assert "upstream plus mac-managed patches" in script
    assert "Shutdown chat notifications disabled by MAC deployment policy." in quench_patch.read_text(
        encoding="utf-8"
    )


def test_executor_prompt_includes_repository_runtime_contract():
    script = deploy_script_text()

    assert "def repository_contract_section(task: dict) -> str:" in script
    assert "Repository runtime contract:" in script
    assert "metadata.runtime.repository_worktree" in script
    assert "origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only" in script
    assert "bootstrap.command" in script
    assert "test.command" in script


def test_mac_repository_contract_test_command_uses_local_venv_path():
    contract = yaml.safe_load((ROOT / ".mac" / "project.yaml").read_text(encoding="utf-8"))

    assert contract["test"]["command"] == "PATH=.venv/bin:$PATH .venv/bin/python -m pytest"
