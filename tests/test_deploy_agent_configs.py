from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]


def parse_env(path: Path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_sample_fleet_config():
    return yaml.safe_load((ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8"))


def test_sample_fleet_config_is_generic_and_externalized():
    cfg = load_sample_fleet_config()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    rendered = "\n".join(
        [
            (ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "systemd" / "mac.env.example").read_text(encoding="utf-8"),
            (ROOT / "scripts" / "setup-fleet.py").read_text(encoding="utf-8"),
        ]
    )

    assert cfg["sample"] is True
    assert cfg["hub_agent"] == "hub"
    assert cfg["shared_services_manager_agent"] == "hub"
    assert not (ROOT / "deploy" / "fleet" / "config-site.yaml").exists()
    assert "config-site" not in gitignore
    assert "config-site" not in rendered
    assert "~/.mac/fleets.yaml" in rendered
    assert "--hub <hub-node>" in rendered
    assert "deploy/agents/" not in rendered
    assert "rocky" not in rendered.lower()
    assert "natasha" not in rendered.lower()
    assert "bullwinkle" not in rendered.lower()
    assert "100.125.137.89" not in rendered


def test_sample_fleet_config_supports_home_channel_and_model_diversity():
    cfg = load_sample_fleet_config()
    assert cfg["defaults"]["hermes"]["slack_home_channel_name"] == ""
    assert cfg["defaults"]["hermes"]["gateway_provider"] == "custom"

    models = [
        agent.get("hermes", {}).get("gateway_model")
        for agent in cfg["agents"]
        if agent.get("hermes", {}).get("gateway_model")
    ]
    assert len(models) >= 3
    assert len(set(models)) == len(models)


def test_fleet_agent_configs_enable_review_capability_by_default():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    cfg = load_sample_fleet_config()

    assert 'text_field(worker.get("capabilities") or "ops,python,hermes,review")' in script
    assert 'WORKER_CAPABILITIES="${MAC_DEPLOY_WORKER_CAPABILITIES:-ops,python,hermes,review}"' in script
    assert 'configured_worker_capabilities = sys.argv[13].strip() or "ops,python,hermes,review"' in script
    assert 'capabilities="${MAC_WORKER_CAPABILITIES:-ops,python,hermes,review}"' in script
    assert cfg["defaults"]["worker"]["capabilities"] == ["ops", "python", "hermes", "review"]


def test_fleet_deploy_persists_or_recovers_worker_attestation_key():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert '--attestation-key-env "$HOME/.mac/mac.env"' in script
    assert "--rotate-missing-attestation-key" in script
    assert "evidence_type=review_verdict" in script


def test_fleet_deploy_drain_agent_lookup_does_not_pipe_json_into_python_stdin():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    agent_id_for_drain = script.split("agent_id_for_drain() {", 1)[1].split(
        "wait_for_agent_active_leases() {", 1
    )[0]

    assert 'response="$(mac_api_json GET "/agents")"' in agent_id_for_drain
    assert "json.loads(sys.argv[2])" in agent_id_for_drain
    assert 'mac_api_json GET "/agents" |' not in agent_id_for_drain


def test_fleet_deploy_bootstraps_beads_cli_for_bridge():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "BEADS_REPO_URL=\"${MAC_DEPLOY_BEADS_REPO_URL:-https://github.com/steveyegge/beads.git}\"" in script
    assert "install_beads_cli()" in script
    assert '"$HOME/.local/bin/bd"' in script
    assert '"$HOME/bin/bd"' in script
    assert "bootstrap_beads_repositories()" in script
    assert "restore_beads_tracked_exports()" in script
    assert 'values.setdefault("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")' in script
    assert 'values.setdefault("MAC_BEADS_BRIDGE_ROOT", str(mac_home / "beads-checkouts"))' in script
    assert 'bootstrap --yes' in script
    assert 'dolt pull' in script
    assert 'chmod 700 "$repo_path/.beads"' in script
    assert 'git -C "$repo_path" config beads.role maintainer' in script
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


def test_fleet_deploy_installs_github_cli_for_workers():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "install_github_cli()" in script
    assert 'install_github_cli' in script.split('mv "$SRC_DIR.new" "$SRC_DIR"', 1)[1].split(
        'log "creating/updating mac environment file"', 1
    )[0]
    assert 'brew install gh' in script
    assert 'sudo apt-get install -y gh' in script
    assert 'https://cli.github.com/packages' in script
    assert 'export PATH="$HOME/.mac/bin:$PATH"' in script


def test_fleet_deploy_does_not_print_worker_token_in_systemd_status():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    agent_service = script.split("install_linux_agent_service() {", 1)[1].split(
        "install_darwin_service() {", 1
    )[0]

    assert "systemctl show mac-agent.service" in agent_service
    assert "systemctl --no-pager -l status mac-agent.service" not in agent_service
    assert "-p ActiveState" in agent_service
    assert "-p MainPID" in agent_service


def test_fleet_deploy_applies_hermes_patch_set():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    quench_patch = ROOT / "deploy" / "hermes" / "disable-shutdown-chat-notices.patch"

    assert "multi-slack-mvp.patch" in script
    assert "disable-shutdown-chat-notices.patch" in script
    assert "upstream plus mac-managed patches" in script
    assert "Shutdown chat notifications disabled by MAC deployment policy." in quench_patch.read_text(
        encoding="utf-8"
    )


def test_fleet_deploy_declares_shared_memory_and_supervision_contract():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    qdrant_installer = (ROOT / "deploy" / "install-qdrant-service.sh").read_text(
        encoding="utf-8"
    )
    env_example = parse_env(ROOT / "deploy" / "systemd" / "mac.env.example")
    cfg = load_sample_fleet_config()

    assert 'SUPERVISOR_REQUESTED="${MAC_DEPLOY_SUPERVISOR:-auto}"' in script
    assert "detect_supervisor()" in script
    assert "systemd|launchd|supervisord" in script
    assert "install_supervisord_service()" in script
    assert "write_hermes_memory_topology()" in script
    assert "install_or_validate_shared_services" in script
    assert "mac.hermes.memory_topology.v1" in script
    assert "QDRANT_FLEET_URL" in script
    assert 'if ! truthy "$required"; then' in script
    assert 'values.pop(key, None)' in script
    assert 'updates["QDRANT_URL"] = None' in script
    assert 'values.setdefault("MAC_REVIEW_TICK_HUB_AGENT", shared_services_manager)' in script
    assert "mac-qdrant.service" in qdrant_installer
    assert "com.mac.qdrant" in qdrant_installer
    assert "[program:mac-qdrant]" in qdrant_installer
    assert cfg["defaults"]["supervisor"] == "auto"
    assert cfg["shared_services_manager_agent"] == "hub"
    assert cfg["defaults"]["qdrant"]["install"] == "auto"
    assert cfg["defaults"]["qdrant"]["required"] is True
    assert env_example["MAC_REQUIRE_QDRANT_MEMORY"] == "1"
    assert env_example["MAC_QDRANT_MEMORY_ROLE"] == "shared_level2"


def test_fleet_deploy_uses_home_scoped_registry_not_legacy_site_config():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "$HOME/.mac/fleets.yaml" in script
    assert "MAC_DEPLOY_FLEETS_CONFIG" in script
    assert "--fleets-config" in script
    assert "--hub <hub-node>" in script
    assert "multiple fleets are configured" in script
    assert "--site-config" not in script
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG" not in script
    assert "FLEET_SITE_CONFIG" not in script


def test_setup_fleet_wizard_writes_fleet_registry_and_env(tmp_path):
    fleets_config = tmp_path / ".mac" / "fleets.yaml"
    env_file = tmp_path / ".mac" / ".env"
    answers = "\n".join(
        [
            "test-fleet",
            "hub",
            "operator@hub.example.internal",
            "",
            "",
            "",
            "ops",
            "provider/family/hub-model",
            "",
            "n",
            "y",
            "",
            "",
            "",
            "n",
            "n",
            "n",
            "",
        ]
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--force",
            "--fleets-config",
            str(fleets_config),
            "--env-file",
            str(env_file),
        ],
        input=answers + "\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    registry = yaml.safe_load(fleets_config.read_text(encoding="utf-8"))
    cfg = registry["fleets"]["hub"]
    env = env_file.read_text(encoding="utf-8")
    assert registry["version"] == 1
    assert cfg["sample"] is False
    assert cfg["fleet_name"] == "test-fleet"
    assert cfg["hub_agent"] == "hub"
    assert cfg["agents"][0]["target"] == "operator@hub.example.internal"
    assert cfg["defaults"]["hermes"]["slack_home_channel_name"] == "ops"
    assert cfg["defaults"]["qdrant"]["required"] is True
    assert cfg["defaults"]["qdrant"]["url"] == "http://hub.example.internal:6333"
    assert "MAC_DEPLOY_FLEETS_CONFIG=" in env
    assert "MAC_DEPLOY_HUB_AGENT=hub" in env
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG=" not in env
    assert "MAC_DEPLOY_HUB_URL=" not in env
    assert "MAC_SECRET_KEY" not in env
    assert "MAC_API_TOKEN" not in env


def test_executor_prompt_includes_repository_runtime_contract():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "def repository_contract_section(task: dict) -> str:" in script
    assert "Repository runtime contract:" in script
    assert "metadata.runtime.repository_worktree" in script
    assert "origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only" in script
    assert "bootstrap.command" in script
    assert "test.command" in script
    assert "returncode=0, status=pass, result=passed" in script


def test_reviewer_prompt_includes_verdict_contract():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "repo copied from the executor verification repo object" in script
    assert "worktree_digest as sha256" in script
    assert "reviewed_evidence_id=%s" in script


def test_mac_repository_contract_test_command_uses_local_venv_path():
    contract = yaml.safe_load((ROOT / ".mac" / "project.yaml").read_text(encoding="utf-8"))

    assert contract["test"]["command"] == "PATH=.venv/bin:$PATH .venv/bin/python -m pytest"
    assert "gh" in contract["toolchain"]["required_commands"]
