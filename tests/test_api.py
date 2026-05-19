import json
from pathlib import Path

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def test_fastapi_exposes_core_workflow_and_redacts_secrets():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    assert client.get("/health").json() == {"status": "ok"}
    machine_response = client.post("/machines", json={"hostname": "host-1"})
    assert machine_response.status_code == 200
    machine = machine_response.json()
    agent_response = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python", "deploy"]},
    )
    assert agent_response.status_code == 200
    agent = agent_response.json()
    task_response = client.post(
        "/tasks",
        json={"title": "API task", "required_capabilities": ["python"]},
    )
    assert task_response.status_code == 200
    task = task_response.json()

    assignment_response = client.post("/dispatch/assign", json={"lease_seconds": 900})
    assert assignment_response.status_code == 200
    assignment = assignment_response.json()
    assert assignment["task"]["id"] == task["id"]
    assert assignment["agent"]["id"] == agent["id"]

    secret_response = client.post(
        "/secrets",
        json={
            "name": "deploy-token",
            "value": "never-return-this",
            "scopes": {"capabilities": ["deploy"]},
            "created_by": "human",
        },
    )
    assert secret_response.status_code == 200
    secret = secret_response.json()
    listed = client.get("/secrets").json()
    assert listed[0]["value"] == "***REDACTED***"
    handle = client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "deploy"},
    ).json()
    assert handle["handle"].startswith("secret://")
    assert "never-return-this" not in str(handle)
    revealed = client.post(
        "/secrets/%s/reveal" % secret["id"],
        json={"audit_id": handle["audit_id"], "accessor_agent_id": agent["id"]},
    ).json()
    assert revealed["value"] == "never-return-this"


def test_fastapi_exposes_hermes_identity_boundary():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    tenant = client.post("/tenants", json={"name": "team"}).json()
    persona = client.post(
        "/personas",
        json={
            "tenant_id": tenant["id"],
            "name": "Rocky",
            "soul_ref": "hermes://team/rocky/SOUL.md",
            "memory_scope": "hermes://team/rocky/memory",
        },
    ).json()
    hermes = client.post(
        "/hermes-instances",
        json={
            "tenant_id": tenant["id"],
            "name": "rocky",
            "persona_id": persona["id"],
            "home_ref": "hermes://team/rocky",
        },
    ).json()
    binding = client.post(
        "/platform-bindings",
        json={
            "tenant_id": tenant["id"],
            "hermes_instance_id": hermes["id"],
            "platform": "telegram",
            "external_id": "chat-42",
        },
    ).json()

    context = client.get("/hermes-instances/%s/context" % hermes["id"]).json()
    assert context["memory_contract"]["user_memory_authority"] == "hermes"
    assert context["platform_bindings"][0]["id"] == binding["id"]

    task = client.post(
        "/hermes-instances/%s/tasks" % hermes["id"],
        json={
            "title": "Follow up from chat",
            "platform_binding_id": binding["id"],
            "conversation_ref": "telegram://chat-42/99",
        },
    ).json()
    assert task["metadata"]["origin"]["hermes_instance_id"] == hermes["id"]
    assert task["metadata"]["memory_boundary"]["mac_records_operational_provenance_only"] is True


def test_fastapi_can_require_scoped_bearer_tokens():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={
                "writer": ["write"],
                "reader": ["read"],
            },
        )
    )

    assert client.get("/health").status_code == 200
    assert client.post("/machines", json={"hostname": "host-1"}).status_code == 403
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer reader"},
        json={"hostname": "host-1"},
    ).status_code == 403
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer writer"},
        json={"hostname": "host-1"},
    ).status_code == 200
    assert client.get("/machines", headers={"Authorization": "Bearer reader"}).status_code == 200


def test_fastapi_serves_dashboard_shell_without_api_token():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={"reader": ["read"]},
        )
    )

    ui_response = client.get("/ui")
    assert ui_response.status_code == 200
    assert "MAC Control Plane" in ui_response.text
    assert "/ui/assets/app.js" in ui_response.text

    script_response = client.get("/ui/assets/app.js")
    assert script_response.status_code == 200
    assert "requestJSON" in script_response.text
    assert "data-action=\"dispatchTick\"" in script_response.text

    assert client.get("/agents").status_code == 403
    assert client.get("/agents", headers={"Authorization": "Bearer reader"}).status_code == 200


def test_fastapi_exposes_dashboard_read_models_and_redacts_secret_values():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "worker",
            "capabilities": ["python", "deploy"],
            "resources": {"capacity": 2},
        },
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "Dashboard task", "required_capabilities": ["python"]},
    ).json()
    secret = client.post(
        "/secrets",
        json={
            "name": "dashboard-token",
            "value": "never-render-this",
            "scopes": {"capabilities": ["deploy"]},
            "created_by": "human",
        },
    ).json()
    handle = client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "dashboard"},
    ).json()

    state = client.get("/dashboard/state").json()
    assert state["overview"]["counts"]["agents"] == 1
    assert state["dispatch"]["open_task_count"] == 1
    assert state["dispatch"]["tasks"][0]["eligible_agent_count"] == 1
    assert state["tasks"][0]["task"]["id"] == task["id"]
    assert state["hermes_startup"]["operator_health"]["status"] in {"healthy", "degraded"}
    assert state["secrets"][0]["value"] == "***REDACTED***"
    assert "never-render-this" not in str(state)
    assert state["secret_audits"][0]["id"] == handle["audit_id"]

    timeline = client.get("/dashboard/tasks/%s/timeline" % task["id"]).json()
    assert timeline["task"]["title"] == "Dashboard task"
    assert timeline["summary"]["state"] == "open"

    agent_detail = client.get("/dashboard/agents/%s" % agent["id"]).json()
    assert agent_detail["availability"]["eligible"] is True


def test_events_endpoint_returns_unified_stream():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    tenant = client.post("/tenants", json={"name": "ops"}).json()
    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "audited", "required_capabilities": ["python"]},
    ).json()
    client.post(
        "/tasks/%s/claim" % task["id"],
        params={"agent_id": agent["id"]},
    )
    secret = client.post(
        "/secrets",
        json={
            "name": "audit-token",
            "value": "v",
            "scopes": {"capabilities": ["python"]},
            "created_by": "human",
        },
    ).json()
    client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "test"},
    )

    events = client.get("/events", params={"limit": 200}).json()
    types = {event["subject_type"] for event in events}
    assert {"task", "secret"} <= types

    task_only = client.get(
        "/events",
        params={"subject_type": "task", "subject_id": task["id"]},
    ).json()
    assert task_only
    assert all(event["subject_id"] == task["id"] for event in task_only)


def test_create_app_refuses_to_start_with_placeholder_secret_key():
    """The deployment env example ships a placeholder long enough to pass the
    32-char length check. Refusing it at startup prevents copy-and-deploy
    deployments from encrypting with a known constant."""
    import pytest
    from mac.models import ValidationError
    from mac.services import ControlPlane
    from mac.store import SQLiteStore

    with pytest.raises(ValidationError):
        ControlPlane(
            SQLiteStore(":memory:"),
            secret_key="REPLACE-ME-WITH-A-32-PLUS-CHAR-RANDOM-STRING",
        )


def test_create_app_via_env_only_works_with_real_secret_key(monkeypatch, tmp_path):
    """Simulate the Docker / systemd path: env-only configuration, fresh empty
    DB directory, no MAC_API_TOKEN. The factory should succeed and /health
    should answer 200."""
    import importlib
    import mac.api as api_module

    db_path = tmp_path / "deploy.db"
    monkeypatch.setenv(
        "MAC_SECRET_KEY",
        "deploy-smoke-key-with-32-plus-characters-of-entropy-abc",
    )
    monkeypatch.setenv("MAC_DB", str(db_path))
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKENS", raising=False)
    # Reload so the conditional `if MAC_SECRET_KEY: app = create_app()` re-runs.
    importlib.reload(api_module)

    client = TestClient(api_module.app)
    assert client.get("/health").json() == {"status": "ok"}
    # The DB file was created on first connect.
    assert db_path.exists()


def test_dashboard_has_typescript_source_without_node_toolchain_files():
    root = Path(__file__).resolve().parents[1]

    assert (root / "src/mac/ui/app.ts").exists()
    assert (root / "src/mac/ui/app.js").exists()
    assert not (root / "package.json").exists()
    assert not (root / "package-lock.json").exists()


def test_fastapi_exposes_typed_agentbus_streams_and_ndjson_events():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "bus-host"}).json()
    sender = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "sender"},
    ).json()
    recipient = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "recipient"},
    ).json()

    stream = client.post(
        "/agentbus/streams",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content_type": "application/vnd.mac.delta+json",
            "topic": "delta",
        },
    ).json()
    first = client.post(
        "/agentbus/streams/%s/chunks" % stream["id"],
        json={
            "sender_agent_id": sender["id"],
            "payload": {"seq": 1, "text": "hello"},
        },
    ).json()
    second = client.post(
        "/agentbus/streams/%s/chunks" % stream["id"],
        json={
            "sender_agent_id": sender["id"],
            "payload": {"seq": 2, "done": True},
            "final": True,
        },
    ).json()

    assert [first["sequence"], second["sequence"]] == [1, 2]
    chunks = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": recipient["id"]},
    ).json()
    assert [chunk["payload"]["seq"] for chunk in chunks] == [1, 2]

    events = client.get(
        "/agentbus/streams/%s/events" % stream["id"],
        params={"agent_id": recipient["id"], "timeout_seconds": 0.01},
    )
    lines = [line for line in events.text.splitlines() if line]
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]

    published = client.post(
        "/agentbus",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content_type": "text/plain",
            "payload_encoding": "text",
            "payload": "one-shot",
        },
    ).json()
    assert published["stream"]["status"] == "closed"
    assert published["chunk"]["payload"] == "one-shot"


def test_agentbus_rejects_broadcast_oversized_and_unauthorized_readers():
    import time as _time

    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "bus-host"}).json()
    sender = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "sender"}
    ).json()
    recipient = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "recipient"}
    ).json()
    outsider = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "outsider"}
    ).json()

    no_recipient = client.post(
        "/agentbus/streams", json={"sender_agent_id": sender["id"]}
    )
    assert no_recipient.status_code == 400

    stream = client.post(
        "/agentbus/streams",
        json={"sender_agent_id": sender["id"], "recipient_agent_id": recipient["id"]},
    ).json()

    huge = client.post(
        "/agentbus/streams/%s/chunks" % stream["id"],
        json={"sender_agent_id": sender["id"], "payload": {"blob": "x" * (256 * 1024 + 1)}},
    )
    assert huge.status_code == 400

    listed = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": outsider["id"]},
    )
    assert listed.status_code == 403

    events = client.get(
        "/agentbus/streams/%s/events" % stream["id"],
        params={"agent_id": outsider["id"], "timeout_seconds": 0.01},
    )
    assert events.status_code == 403

    close = client.post(
        "/agentbus/streams/%s/close" % stream["id"],
        params={"sender_agent_id": sender["id"]},
    )
    assert close.status_code == 200

    started = _time.monotonic()
    capped = client.get(
        "/agentbus/streams/%s/events" % stream["id"],
        params={
            "agent_id": recipient["id"],
            "timeout_seconds": 600,
            "poll_interval_seconds": 0.001,
        },
    )
    elapsed = _time.monotonic() - started
    assert capped.status_code == 200
    # Closed-stream short-circuit must return promptly even though caller
    # asked for a 10-minute timeout — proves the server isn't honoring the
    # client-controlled value verbatim.
    assert elapsed < 5
