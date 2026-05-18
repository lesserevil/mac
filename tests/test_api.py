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
    assert "fetchJSON" in script_response.text

    assert client.get("/agents").status_code == 403
    assert client.get("/agents", headers={"Authorization": "Bearer reader"}).status_code == 200


def test_dashboard_has_typescript_source_without_node_toolchain_files():
    root = Path(__file__).resolve().parents[1]

    assert (root / "src/mac/ui/app.ts").exists()
    assert (root / "src/mac/ui/app.js").exists()
    assert not (root / "package.json").exists()
    assert not (root / "package-lock.json").exists()
