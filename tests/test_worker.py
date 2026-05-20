from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import subprocess
import time
from typing import Any, Dict, Optional

import pytest

from fastapi.testclient import TestClient

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_SCHEMA,
    REPO_UPDATE_TOPIC,
)
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane, sign_verification_manifest
from mac.worker import MacWorker, WorkerExecution, register_worker


def api_transport(client: TestClient):
    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        request = getattr(client, method.lower())
        kwargs: Dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        response = request(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    return transport


def register_worker_fixture(cp: ControlPlane):
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])
    return agent


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_fixture(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "mac-tests@example.invalid")
    _git(seed, "config", "user.name", "mac tests")
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(work)],
        check=True,
        capture_output=True,
    )
    return seed, work


def _commit_fixture_update(seed: Path, text: str) -> str:
    (seed / "README.md").write_text(text, encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "update")
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def test_mac_worker_claims_for_specific_agent_and_submits_for_review(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    skipped = cp.create_task("Docs task", required_capabilities=["docs"])
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        assert task_payload["id"] == task.id
        assert (task_dir / "task.json").exists()
        return WorkerExecution(0, "tests passed", stdout="tests passed\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert result.task["id"] == task.id
    reviewed = cp.get_task(task.id)
    assert reviewed.state == TaskState.NEEDS_REVIEW.value
    assert reviewed.owner_agent_id is None
    assert reviewed.lease_id is None
    assert cp.get_task(skipped.id).state == TaskState.OPEN.value
    evidence = cp.list_evidence(task.id)
    assert evidence[0].summary == "tests passed"
    assert evidence[0].metadata["returncode"] == 0
    observations = cp.list_observability(layer="worker", limit=20)
    names = {item.name for item in observations}
    assert "worker.task_claimed" in names
    assert "worker.execution.duration_ms" in names
    assert any(item.subject_id == task.id for item in observations)


def test_mac_worker_processes_review_nudge_and_records_signed_verdict(tmp_path: Path):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("review-host")
    executor_agent = cp.register_agent(machine.id, "executor", capabilities=["python"])
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    task = cp.create_task(
        "Reviewable repo task",
        required_capabilities=["python"],
        metadata={"publication_target": "git://main"},
    )
    cp.claim_task(task.id, executor_agent.id)
    cp.start_task(task.id, executor_agent.id)
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abc123abc123abc123abc123abc123abc123abcd",
            "remote_ref": "origin/main",
            "pushed": True,
            "dirty": False,
            "files_changed": ["src/example.py"],
        },
        "checks": [{"name": "pytest", "status": "passed", "returncode": 0}],
        "signed_by": executor_agent.id,
    }
    executor_manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor_agent.id), executor_manifest
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/executor-result.json",
        "executor completed",
        executor_agent.id,
        metadata={"returncode": 0, "verification": executor_manifest},
    )
    cp.submit_for_review(task.id, executor_agent.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    assert first["reviewer_agent_id"] == reviewer.id
    client = TestClient(create_app(control_plane=cp))

    def review_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        context = task_payload["metadata"]["review_context"]
        assert context["task_id"] == task.id
        assert context["review_id"] == first["review_id"]
        assert context["executor_evidence_id"] == evidence.id
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "review_id": context["review_id"],
            "reviewed_evidence_id": context["executor_evidence_id"],
            "findings": ["executor evidence is signed and tests passed"],
        }
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return WorkerExecution(0, "review approved", stdout="approved\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        reviewer.id,
        tmp_path,
        review_executor,
        attestation_key=cp._agent_attestation_key(reviewer.id),
    )

    result = worker.run_once()

    assert result.status == "review_verdict_recorded"
    verdict_evidence = cp.list_evidence(task.id)[-1]
    manifest = verdict_evidence.metadata["verification"]
    assert verdict_evidence.kind == "review"
    assert manifest["evidence_type"] == "review_verdict"
    assert manifest["signed_by"] == reviewer.id
    assert manifest["reviewed_evidence_id"] == evidence.id
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_mac_worker_records_failed_execution_and_fails_task(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        return WorkerExecution(2, "pytest failed", stderr="pytest failed\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "failed"
    assert result.error == "pytest failed"
    assert cp.get_task(task.id).state == TaskState.FAILED.value
    evidence = cp.list_evidence(task.id)
    assert evidence[0].metadata["returncode"] == 2


def test_mac_worker_renews_lease_while_executor_runs(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        time.sleep(0.05)
        return WorkerExecution(0, "tests passed", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        lease_seconds=60,
        lease_renew_interval_seconds=0.01,
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert any(event.event_type == "task.lease_renewed" for event in cp.task_history(task.id))


def test_assignment_is_current_propagates_programming_errors_not_silently_true(tmp_path: Path):
    """mac-h3d: _assignment_is_current's exception net was bare
    ``except Exception`` and silently returned True. Narrowed to
    MacApiError so a TypeError from a malformed response (or any
    programming bug) bubbles instead of being treated as "still
    current" — that path lets a worker complete a task it doesn't
    own."""
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))

    original_get = api.get

    def crashing_get(path: str) -> Any:
        if path.startswith("/tasks/"):
            # Simulate a malformed response that would crash the
            # downstream .get("task", ...) call. Pre-fix this was caught
            # by the bare except and returned True; post-fix it should
            # bubble out of _assignment_is_current.
            raise TypeError("simulated malformed response")
        return original_get(path)

    api.get = crashing_get  # type: ignore[assignment]
    worker = MacWorker(api, agent.id, tmp_path, lambda _t, _d: WorkerExecution(0, "ok"))
    # Direct call — no task in flight, but the helper should now raise
    # the TypeError instead of swallowing it.
    with pytest.raises(TypeError):
        worker._assignment_is_current("task_doesnt_matter", "lease_doesnt_matter")


def test_mac_worker_does_not_mutate_task_after_losing_lease(tmp_path: Path):
    cp = ControlPlane.in_memory()
    first = register_worker_fixture(cp)
    machine = cp.register_machine("second-worker-host")
    second = cp.register_agent(machine.id, "second-worker", capabilities=["python"])
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
            timespec="microseconds"
        )
        cp.expire_leases(now=future)
        cp.claim_task(task.id, second.id)
        cp.start_task(task.id, second.id)
        return WorkerExecution(0, "late success", stdout="late success\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        first.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "stale_result"
    current = cp.get_task(task.id)
    assert current.state == TaskState.RUNNING.value
    assert current.owner_agent_id == second.id
    assert cp.list_evidence(task.id) == []
    observations = cp.list_observability(layer="worker", limit=20)
    assert any(item.name == "worker.execution.stale_result" for item in observations)


def test_mac_worker_run_forever_drains_queue_then_reports_offline(tmp_path: Path):
    cp = ControlPlane.in_memory()
    # Capacity is above one so the loop can drain several assignments in a
    # single bounded run; submitted tasks release their executor lease at review.
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(
        machine.id, "worker", capabilities=["python"], resources={"capacity": 3}
    )
    task_ids = [
        cp.create_task("work-%d" % i, required_capabilities=["python"]).id
        for i in range(3)
    ]
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        return WorkerExecution(0, "ok", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        poll_interval_seconds=0.0,
    )
    # max_iterations bounds the loop so the test doesn't hang.
    results = worker.run_forever(max_iterations=5)

    submitted = [r for r in results if r.status == "submitted_for_review"]
    assert {r.task["id"] for r in submitted} == set(task_ids)
    # After the loop the worker marks itself offline (best-effort heartbeat).
    refreshed = cp.get_agent(agent.id)
    assert refreshed.status == "offline"


def test_mac_worker_restores_prior_signal_handlers_after_run_forever(tmp_path: Path):
    import signal

    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))

    def sentinel_handler(*_args):
        return None

    prior_term = signal.signal(signal.SIGTERM, sentinel_handler)
    prior_int = signal.signal(signal.SIGINT, sentinel_handler)
    try:
        worker = MacWorker(
            MacApiClient("http://mac.test", transport=api_transport(client)),
            agent.id,
            tmp_path,
            lambda _t, _d: WorkerExecution(0, "ok"),
            poll_interval_seconds=0.0,
        )
        worker.run_forever(max_iterations=2)

        # The worker must have restored the handlers it found, not left its
        # own stop-callback installed for the rest of the process.
        assert signal.getsignal(signal.SIGTERM) is sentinel_handler
        assert signal.getsignal(signal.SIGINT) is sentinel_handler
    finally:
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_int)


def test_mac_worker_run_forever_tolerates_failing_offline_heartbeat(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    cp.create_task("one", required_capabilities=["python"])

    underlying = TestClient(create_app(control_plane=cp))

    class _FlakyTransport:
        def __init__(self) -> None:
            self.heartbeat_call = 0

        def __call__(self, method: str, path: str, payload):
            # Fail the offline heartbeat that fires from _shutdown.
            if (
                method == "POST"
                and "/agents/" in path
                and path.endswith("/heartbeat")
                and isinstance(payload, dict)
                and payload.get("status") == "offline"
            ):
                raise MacApiError("simulated network failure on shutdown")
            return api_transport(underlying)(method, path, payload)

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_FlakyTransport()),
        agent.id,
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "ok"),
        poll_interval_seconds=0.0,
    )
    # Should not raise — _shutdown swallows transport errors.
    results = worker.run_forever(max_iterations=2)
    assert any(r.status == "submitted_for_review" for r in results)


def test_mac_worker_processes_agentbus_repo_update_and_requests_restart(tmp_path: Path):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    seed, work = _git_fixture(tmp_path)
    expected = _commit_fixture_update(seed, "two\n")
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=REPO_UPDATE_CONTENT_TYPE,
        topic=REPO_UPDATE_TOPIC,
        payload={
            "schema": REPO_UPDATE_SCHEMA,
            "remote": "origin",
            "branch": "main",
            "restart": True,
            "request_id": "req-1",
        },
    )
    client = TestClient(create_app(control_plane=cp))

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
        self_update_repo=work,
    )
    result = worker.run_once()

    assert result.status == "self_update_restart"
    assert _git(work, "rev-parse", "HEAD") == expected
    result_streams = [
        stream
        for stream in cp.list_agentbus_streams(agent_id=sender.id, status="closed")
        if stream.topic == REPO_UPDATE_RESULT_TOPIC
    ]
    assert result_streams
    chunks = cp.read_agentbus_chunks(sender.id, result_streams[0].id)
    assert chunks[0].payload["status"] == "updated"
    assert chunks[0].payload["restart_requested"] is True
    assert chunks[0].payload["request_id"] == "req-1"


def test_mac_worker_repo_update_noops_without_restart_when_current(tmp_path: Path):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    _seed, work = _git_fixture(tmp_path)
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=REPO_UPDATE_CONTENT_TYPE,
        topic=REPO_UPDATE_TOPIC,
        payload={"schema": REPO_UPDATE_SCHEMA, "remote": "origin", "branch": "main"},
    )
    client = TestClient(create_app(control_plane=cp))

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
        self_update_repo=work,
    )
    result = worker.run_once()

    assert result.status == "no_task"
    result_streams = [
        stream
        for stream in cp.list_agentbus_streams(agent_id=sender.id, status="closed")
        if stream.topic == REPO_UPDATE_RESULT_TOPIC
    ]
    assert result_streams
    chunks = cp.read_agentbus_chunks(sender.id, result_streams[0].id)
    assert chunks[0].payload["status"] == "no_update"
    assert chunks[0].payload["restart_requested"] is False


def test_mac_worker_declares_running_digest_on_first_heartbeat(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    runtime = cp.create_runtime(
        "worker-runtime",
        {"image": "python:3.12@sha256:abc123", "dependencies": ["fastapi==0.111.0"]},
        "human",
    )
    cp.create_task("declared", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_t: Dict[str, Any], _d: Path) -> WorkerExecution:
        return WorkerExecution(0, "ok")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        running_digest=runtime.digest,
    )
    worker.run_once()

    refreshed = cp.get_agent(agent.id)
    assert refreshed.running_digest == runtime.digest
    distribution = cp.fleet_build_distribution()
    by_digest = {b["digest"]: b for b in distribution["buckets"]}
    assert by_digest[runtime.digest]["count"] == 1


def test_register_worker_creates_identity_then_worker_claims_tasks(tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))

    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
        resources={"capacity": 2},
    )
    task = cp.create_task("registered worker task", required_capabilities=["python"])

    worker = MacWorker(
        api,
        registered["id"],
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "ok", stdout="ok\n"),
    )
    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert result.task["id"] == task.id
    assert cp.get_agent(registered["id"]).name == "rocky"
    assert cp.get_agent(registered["id"]).capabilities == ["python"]
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value


def test_mac_worker_dry_run_claim_uses_canary_policy_without_leasing(tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
    )
    normal = cp.create_task(
        "normal",
        project="mac-canary",
        priority=100,
        required_capabilities=["python"],
    )
    canary = cp.create_task(
        "canary",
        project="mac-canary",
        priority=10,
        required_capabilities=["python"],
        metadata={"canary": True},
    )
    worker = MacWorker(
        api,
        registered["id"],
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "unused"),
        allowed_projects=["mac-canary"],
        require_canary=True,
    )

    assignment = worker.dry_run_claim()

    assert assignment is not None
    assert assignment["task"]["id"] == canary.id
    assert assignment["lease"] is None
    assert cp.get_task(normal.id).state == TaskState.OPEN.value
    assert cp.get_task(canary.id).state == TaskState.OPEN.value
    names = {item.name for item in cp.list_observability(layer="worker", limit=20)}
    assert "worker.routing.policy" in names
    assert "worker.routing.dry_run_result" in names


def test_mac_worker_completes_task_even_if_observability_writes_fail(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    api = MacApiClient("http://mac.test", transport=api_transport(client))
    original_post = api.post

    def broken_post(path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if path.startswith("/observability/"):
            raise MacApiError("observability sink is down")
        return original_post(path, payload)

    api.post = broken_post  # type: ignore[assignment]

    worker = MacWorker(
        api,
        agent.id,
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "ok", stdout="ok\n"),
    )
    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert result.task["id"] == task.id
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
