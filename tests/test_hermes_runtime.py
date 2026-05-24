from __future__ import annotations

import json

from mac.hermes_runtime import stable_id, write_runtime_context


def parse_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_write_runtime_context_materializes_mac_task_project_bridge(tmp_path):
    hermes_home = tmp_path / ".hermes"
    mac_home = tmp_path / ".mac"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    env_path = hermes_home / ".env"

    context = write_runtime_context(
        context_path=context_path,
        markdown_path=markdown_path,
        hermes_env_path=env_path,
        agent_name="Rocky Host",
        fleet_name="classic-fleet",
        mac_url="http://hub.example.internal:8789/path?token=hidden",
        hermes_home=hermes_home,
        mac_home=mac_home,
    )

    stored = json.loads(context_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    env = parse_env(env_path)
    assert context["schema"] == "mac.hermes.runtime_context.v1"
    assert stored["identity"]["tenant_id"] == "tenant_classic-fleet"
    assert stored["agent"]["agent_id"] == "agent_rocky_host"
    assert stored["identity"]["hermes_instance_id"] == "hermes_rocky_host"
    assert stored["authority"]["tasks"] == "mac"
    assert stored["authority"]["projects"] == "mac"
    assert stored["authority"]["agents"] == "mac"
    assert stored["authority"]["personality"] == "hermes"
    assert stored["endpoints"]["mac_api"] == "http://hub.example.internal:8789/path"
    assert "mac-hermes work-context hermes_rocky_host --active-only" in markdown
    assert "mac-hermes claim {task_id} agent_rocky_host" in markdown
    assert env["MAC_HERMES_RUNTIME_CONTEXT_FILE"] == str(context_path)
    assert env["MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"] == str(markdown_path)
    assert env["MAC_HERMES_RUNTIME_CONTEXT_REQUIRED"] == "1"
    assert env["MAC_HERMES_INSTANCE_ID"] == "hermes_rocky_host"
    assert env["MAC_WORKER_HERMES_INSTANCE_ID"] == "hermes_rocky_host"
    assert env["MAC_AGENT_ID"] == "agent_rocky_host"
    assert env["MAC_URL"] == "http://hub.example.internal:8789/path"
    assert "token=hidden" not in str(stored)
    assert "MAC_TOKEN" not in env


def test_stable_id_matches_deployed_worker_id_shape():
    assert stable_id("agent", "Rocky Host") == "agent_rocky_host"
    assert stable_id("hermes", "puck.local") == "hermes_puck.local"
