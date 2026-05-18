from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
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
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
    assert cp.get_task(skipped.id).state == TaskState.OPEN.value
    evidence = cp.list_evidence(task.id)
    assert evidence[0].summary == "tests passed"
    assert evidence[0].metadata["returncode"] == 0


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


def test_mac_worker_run_forever_drains_queue_then_reports_offline(tmp_path: Path):
    cp = ControlPlane.in_memory()
    # Worker with capacity 3 so it can hold multiple leases-in-review at once
    # without waiting for the reviewer to publish (which is a separate role).
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
