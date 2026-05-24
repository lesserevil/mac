import json

from fastapi.testclient import TestClient

from mac.cli import main as mac_cli_main
from mac.api import create_app
from mac.hermes_adapter import (
    ConversationTaskInput,
    HermesMacAdapter,
    MacApiClient,
    MacApiError,
    PlatformBindingSpec,
    main as mac_hermes_main,
)
from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane
from mac.store import SQLiteStore


def api_transport(client):
    def transport(method, path, payload):
        if method == "GET":
            response = client.get(path)
        elif method == "POST":
            response = client.post(path, json=payload)
        else:
            raise MacApiError("unsupported test method: %s" % method)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json()

    return transport


def register_agent(cp, name, capabilities):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name, capabilities=capabilities)


def finish_task(cp, task_id):
    from mac.services import sign_verification_manifest
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task, _lease = cp.claim_task(task_id, worker.id)
    assert task.state == TaskState.CLAIMED.value
    cp.start_task(task_id, worker.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "test",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/example",
            "dirty": False,
        },
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    manifest["signed_by"] = worker.id
    manifest["signature"] = sign_verification_manifest(cp._agent_attestation_key(worker.id), manifest)
    evidence = cp.add_evidence(
        task_id,
        "test",
        "artifact://pytest",
        "tests passed",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task_id, worker.id)
    review = cp.request_review(task_id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task_id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    cp.publish_task(task_id, "git://main", reviewer.id, evidence_id=evidence.id)


def test_hermes_adapter_registers_identity_and_creates_sanitized_task():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))

    registration = adapter.register_identity(
        "personal",
        "Rocky",
        "rocky",
        "hermes://personal/rocky/SOUL.md",
        "hermes://personal/rocky/memory",
        platform_bindings=[PlatformBindingSpec("slack", "T123/C456", "#ops")],
    )
    repeat = adapter.register_identity(
        "personal",
        "Rocky",
        "rocky",
        "hermes://personal/rocky/SOUL.md",
        "hermes://personal/rocky/memory",
    )
    assert repeat["tenant"]["id"] == registration["tenant"]["id"]
    assert repeat["persona"]["id"] == registration["persona"]["id"]
    assert repeat["hermes_instance"]["id"] == registration["hermes_instance"]["id"]

    task = adapter.create_task_from_conversation(
        registration["hermes_instance"]["id"],
        ConversationTaskInput(
            title="Investigate failed deploy",
            summary="Deploy failed after the package publish step.",
            platform_binding_id=registration["platform_bindings"][0]["id"],
            conversation_ref="slack://T123/C456/1712345678.000100",
            project="deploy",
            required_capabilities=["ops"],
            snippets=["User-visible error: publish returned 500"],
            metadata={
                "ticket": "INC-42",
                "private_memory": "do not copy",
                "api_token": "do not copy",
                "raw_messages": ["do not copy"],
            },
        ),
    )

    assert task["metadata"]["origin"]["type"] == "hermes_interaction"
    assert task["metadata"]["sanitized_conversation"]["summary"].startswith("Deploy failed")
    assert task["metadata"]["ticket"] == "INC-42"
    assert "private_memory" not in task["metadata"]
    assert "api_token" not in task["metadata"]
    assert "raw_messages" not in task["metadata"]
    assert "do not copy" not in task["description"]

    work_context = adapter.work_context(registration["hermes_instance"]["id"])
    assert work_context["schema"] == "mac.hermes_work_context.v1"
    assert work_context["projects"][0]["project"] == "deploy"
    assert work_context["tasks"][0]["id"] == task["id"]
    assert work_context["tasks"][0]["origin"]["hermes_instance_id"] == registration["hermes_instance"]["id"]
    assert any(
        operation["name"] == "create_task_from_conversation"
        for operation in work_context["operations"]["api"]
    )
    assert adapter.work_context_brief(registration["hermes_instance"]["id"]).startswith(
        "MAC work context:"
    )


def test_hermes_adapter_summarizes_result_and_prepares_memory_writeback():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))
    registration = adapter.register_identity(
        "team",
        "Natasha",
        "natasha",
        "hermes://team/natasha/SOUL.md",
        "hermes://team/natasha/memory",
    )
    task = adapter.create_task_from_conversation(
        registration["hermes_instance"]["id"],
        ConversationTaskInput(
            title="Fix build",
            summary="The build is failing in CI.",
            required_capabilities=["ops"],
        ),
    )
    finish_task(cp, task["id"])

    summary = adapter.task_summary(task["id"])
    assert summary["state"] == "completed"
    assert summary["approved_review_count"] == 1
    assert adapter.user_reply_for_task(task["id"]) == "Fix build is complete and published to git://main."

    writes = []
    writeback = adapter.write_completed_task_to_memory(
        registration["hermes_instance"]["id"],
        task["id"],
        sink=writes.append,
    )
    assert writes[0]["memory_scope"] == "hermes://team/natasha/memory"
    assert writes[0]["content"] == "Fix build is complete and published to git://main."
    assert writeback["record"]["subject_type"] == "hermes_memory"
    assert cp.search_memory(task_id=task["id"])[0].record_type == "task_result_writeback"


def test_mac_cli_prints_hermes_work_context(tmp_path, capsys, monkeypatch):
    db = tmp_path / "mac.db"
    monkeypatch.setenv("MAC_SECRET_KEY", "test-secret-key-for-cli-work-context")
    cp = ControlPlane(
        SQLiteStore(str(db)),
        secret_key="test-secret-key-for-cli-work-context",
    )
    tenant = cp.register_tenant("team")
    persona = cp.register_persona(
        tenant.id,
        "Rocky",
        "hermes://team/rocky/SOUL.md",
        "hermes://team/rocky/memory",
    )
    hermes = cp.register_hermes_instance(tenant.id, "rocky", persona_id=persona.id)
    cp.create_interaction_task(
        hermes.id,
        "Track project from Hermes",
        project="mac",
        description="Created through the Hermes boundary.",
    )

    rc = mac_cli_main(["--db", str(db), "hermes", "work-context", hermes.id])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mac.hermes_work_context.v1"
    assert payload["projects"][0]["project"] == "mac"
    assert payload["operations"]["mac_cli"][0].startswith("mac hermes work-context")


def test_mac_hermes_cli_fetches_work_context(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((self.base_url, method, path, payload))
        return {
            "schema": "mac.hermes_work_context.v1",
            "projects": [],
            "tasks": [],
            "agents": [],
            "relationships": {},
            "operations": {},
        }

    monkeypatch.setattr(MacApiClient, "request", request)

    rc = mac_hermes_main(
        [
            "--url",
            "http://hub:8789",
            "work-context",
            "hermes_1",
            "--active-only",
            "--task-limit",
            "7",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "mac.hermes_work_context.v1"
    assert calls == [
        (
            "http://hub:8789",
            "GET",
            "/hermes-instances/hermes_1/work-context?include_completed=false&task_limit=7",
            None,
        )
    ]
