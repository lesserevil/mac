import io
import json

from mac.hgmac import run


def test_hgmac_agent_crud_commands_emit_json_and_use_api_paths(monkeypatch):
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body, token))
        return {"method": method, "url": url, "body": body, "token": token}

    out = io.StringIO()
    rc = run(
        [
            "--url",
            "http://hub:8789",
            "--token",
            "secret",
            "agents",
            "create",
            "--machine-id",
            "machine_1",
            "--name",
            "worker",
            "--capabilities",
            "python,ops",
            "--resources-json",
            '{"cpu": 4}',
        ],
        transport=transport,
        stdout=out,
    )

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["method"] == "POST"
    assert payload["url"] == "http://hub:8789/agents"
    assert payload["token"] == "secret"
    assert payload["body"]["capabilities"] == ["ops", "python"]
    assert payload["body"]["resources"] == {"cpu": 4}

    out = io.StringIO()
    rc = run(
        [
            "--url",
            "http://hub:8789",
            "agents",
            "update",
            "agent_1",
            "--status",
            "offline",
            "--health-status",
            "degraded",
        ],
        transport=transport,
        stdout=out,
    )
    assert rc == 0
    assert calls[-1][0] == "PUT"
    assert calls[-1][1] == "http://hub:8789/agents/agent_1"
    assert calls[-1][2]["status"] == "offline"

    run(["--url", "http://hub:8789", "agents", "disable", "agent_1"], transport=transport, stdout=io.StringIO())
    run(["--url", "http://hub:8789", "agents", "delete", "agent_1"], transport=transport, stdout=io.StringIO())
    assert calls[-2][0:2] == ("POST", "http://hub:8789/agents/agent_1/disable")
    assert calls[-1][0:2] == ("DELETE", "http://hub:8789/agents/agent_1")


def test_hgmac_covers_related_agent_operations(monkeypatch):
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body))
        return {"ok": True}

    commands = [
        ["agents", "heartbeat", "agent_1", "--status", "idle"],
        ["agents", "claim-next", "agent_1", "--dry-run", "--allowed-project", "nanolang"],
        ["agents", "identity", "agent_1"],
        ["agents", "role", "assign", "agent_1", "reviewer"],
        ["agents", "role", "unassign", "agent_1"],
        ["agents", "mood", "set", "agent_1", "--mode", "focused"],
        ["agents", "mood", "show", "agent_1"],
        ["agents", "mood", "clear", "agent_1"],
        ["agents", "mood", "history", "agent_1", "--limit", "5"],
        ["agents", "nap", "configure", "agent_1", "--offset-minutes", "60"],
        ["agents", "nap", "show", "agent_1"],
        ["agents", "nap", "next", "agent_1"],
        ["agents", "nap", "begin", "agent_1"],
        ["agents", "nap", "runs", "--agent-id", "agent_1"],
        ["agents", "nap", "complete", "nap_1"],
        ["agents", "nap", "fail", "nap_1", "--reason", "interrupted"],
        ["agents", "command-audit", "list", "--agent-id", "agent_1"],
        [
            "agents",
            "command-audit",
            "record",
            "agent_1",
            "--phase",
            "completed",
            "--argv-json",
            '["pytest"]',
            "--cwd",
            "/repo",
        ],
    ]

    for command in commands:
        rc = run(["--url", "http://hub:8789", *command], transport=transport, stdout=io.StringIO())
        assert rc == 0

    paths = [url.removeprefix("http://hub:8789") for _method, url, _body in calls]
    assert "/agents/agent_1/heartbeat" in paths
    assert "/agents/agent_1/claim-next" in paths
    assert "/agents/agent_1/identity" in paths
    assert "/agents/agent_1/role" in paths
    assert "/agents/agent_1/mood" in paths
    assert "/agents/agent_1/nap-schedule" in paths
    assert "/agents/agent_1/nap-runs" in paths
    assert "/nap-runs/nap_1/complete" in paths
    assert "/nap-runs/nap_1/fail" in paths
    assert any(path.startswith("/command-audit?") for path in paths)
    assert "/agents/agent_1/command-audit" in paths
