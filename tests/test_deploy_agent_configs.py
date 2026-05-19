from pathlib import Path


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
