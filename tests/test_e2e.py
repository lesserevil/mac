"""End-to-end tests crossing the full FastAPI + ControlPlane + on-disk
SQLite stack.

The rest of the suite tests one layer at a time:
``test_control_plane.py`` exercises ``ControlPlane`` directly with
``:memory:``; ``test_api.py`` tests the HTTP layer in isolation;
``test_worker.py`` stops at ``submitted_for_review`` without crossing
the review + publish path. None of them use a file-backed SQLite or
walk a task through the full lifecycle via HTTP.

These tests close that gap. They use ``create_app(db_path=...)``
against a real ``tmp_path`` SQLite file so WAL/busy_timeout/threading
behaves like production, and they drive the FastAPI app through
``TestClient`` so every request crosses Pydantic, the auth middleware,
and the full domain service composition.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.worker import MacWorker, WorkerExecution

_SECRET_KEY = "test-key-with-enough-entropy-32+chars"


def _disk_app(tmp_path: Path) -> TestClient:
    """Build a FastAPI app against a disk-backed SQLite file.

    Uses the same fixed secret_key as ControlPlane.in_memory so the
    secret encryption path works in tests without mutating the
    environment.
    """
    db_path = tmp_path / "mac.db"
    cp = ControlPlane(SQLiteStore(str(db_path)), secret_key=_SECRET_KEY)
    return TestClient(create_app(control_plane=cp))


def _api_transport(client: TestClient):
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


# ---------------------------------------------------------------------------
# Test 1: full task lifecycle through HTTP against a real on-disk DB
# ---------------------------------------------------------------------------


def test_e2e_full_task_lifecycle_via_http_and_disk(tmp_path: Path):
    client = _disk_app(tmp_path)

    machine = client.post("/machines", json={"hostname": "host-e2e"}).json()
    worker = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "rocky", "capabilities": ["python"]},
    ).json()
    reviewer = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "natasha", "capabilities": ["review"]},
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "E2E task", "required_capabilities": ["python"]},
    ).json()

    # MacWorker drives claim → start → run → evidence → submit_for_review
    # through the same API surface.
    api = MacApiClient("http://mac.test", transport=_api_transport(client))
    macworker = MacWorker(
        api,
        worker["id"],
        tmp_path / "workspaces",
        lambda _t, _d: WorkerExecution(0, "tests passed", stdout="ok\n"),
    )
    result = macworker.run_once()
    assert result.status == "submitted_for_review"
    assert result.task["id"] == task["id"]

    # Reviewer requests + approves with task evidence.
    evidence_list = client.get(
        "/dashboard/tasks/%s/timeline" % task["id"]
    ).json()["evidence"]
    assert evidence_list, "worker must have produced evidence before review"
    evidence_id = evidence_list[0]["id"]
    review = client.post(
        "/tasks/%s/reviews" % task["id"],
        json={"reviewer_agent_id": reviewer["id"], "actor": "human"},
    ).json()
    decision = client.post(
        "/reviews/%s/decision" % review["id"],
        json={
            "status": "approved",
            "reviewer_agent_id": reviewer["id"],
            "evidence_id": evidence_id,
        },
    ).json()
    assert decision["status"] == "approved"

    publication = client.post(
        "/publications",
        json={
            "task_id": task["id"],
            "target": "stdout",
            "created_by": "human",
            "evidence_id": evidence_id,
        },
    ).json()
    assert publication["status"] == "published"

    final = client.get("/tasks/%s" % task["id"]).json()
    assert final["task"]["state"] == TaskState.COMPLETED.value

    # History shows every transition.
    history_events = {h["event_type"] for h in final["history"]}
    assert {
        "task.transitioned",
        "task.evidence_added",
        "task.review_requested",
        "task.review_completed",
    }.issubset(history_events)

    # The on-disk file actually has bytes — proves we crossed the file path.
    assert (tmp_path / "mac.db").stat().st_size > 0


# ---------------------------------------------------------------------------
# Test 2: two workers race for one task; exactly one wins
# ---------------------------------------------------------------------------


def test_e2e_two_workers_race_for_one_task_serializes(tmp_path: Path):
    client = _disk_app(tmp_path)

    m1 = client.post("/machines", json={"hostname": "host-a"}).json()
    m2 = client.post("/machines", json={"hostname": "host-b"}).json()
    a1 = client.post(
        "/agents",
        json={"machine_id": m1["id"], "name": "rocky", "capabilities": ["python"]},
    ).json()
    a2 = client.post(
        "/agents",
        json={"machine_id": m2["id"], "name": "natasha", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks", json={"title": "race", "required_capabilities": ["python"]}
    ).json()

    api = MacApiClient("http://mac.test", transport=_api_transport(client))

    def make_worker(agent_id: str) -> MacWorker:
        return MacWorker(
            api,
            agent_id,
            tmp_path / ("ws-%s" % agent_id),
            lambda _t, _d: WorkerExecution(0, "ok", stdout="ok\n"),
        )

    results: Dict[str, Any] = {}

    def run_worker(name: str, worker: MacWorker) -> None:
        # Tiny stagger so both threads are actually contending. The
        # store's BEGIN IMMEDIATE + RLock is what serializes the race.
        time.sleep(0.01)
        results[name] = worker.run_once()

    t1 = threading.Thread(target=run_worker, args=("a1", make_worker(a1["id"])))
    t2 = threading.Thread(target=run_worker, args=("a2", make_worker(a2["id"])))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status for r in results.values())
    assert statuses == ["no_task", "submitted_for_review"], statuses

    final = client.get("/tasks/%s" % task["id"]).json()
    assert final["task"]["state"] == TaskState.NEEDS_REVIEW.value
    # NEEDS_REVIEW releases the lease + clears owner_agent_id on the task,
    # but exactly one worker must have led it there. Exactly one
    # transition row of `running -> needs_review` proves the race
    # serialized: the losing worker never claimed, so it never
    # transitioned the task.
    needs_review_transitions = [
        h
        for h in final["history"]
        if h["event_type"] == "task.transitioned"
        and h["to_state"] == TaskState.NEEDS_REVIEW.value
    ]
    assert len(needs_review_transitions) == 1
    assert needs_review_transitions[0]["from_state"] == TaskState.RUNNING.value
    assert needs_review_transitions[0]["actor"] in {a1["id"], a2["id"]}


# ---------------------------------------------------------------------------
# Test 3: rollout advance blocked until a passing eval run exists
# ---------------------------------------------------------------------------


def test_e2e_rollout_advance_blocks_on_eval_gate_via_http(tmp_path: Path):
    client = _disk_app(tmp_path)

    runtime = client.post(
        "/runtimes",
        json={
            "name": "py-runtime",
            "manifest": {
                "image": "python:3.12@sha256:abc123",
                "dependencies": ["fastapi==0.111.0"],
            },
            "created_by": "ops",
        },
    ).json()
    eval_set = client.post(
        "/eval-sets",
        json={
            "name": "smoke-suite",
            "scoring": "higher_is_better",
            "baseline_score": 0.9,
            "regression_threshold": 0.05,
            "created_by": "ops",
        },
    ).json()
    rollout = client.post(
        "/rollouts",
        json={
            "version": "v1.2.3",
            "strategy": "canary",
            "target_percent": 25,
            "created_by": "ops",
            "channel": "fleet",
            "runtime_environment_id": runtime["id"],
            "required_eval_set_id": eval_set["id"],
        },
    ).json()

    # Pin an artifact so install_ready is satisfied.
    pinned = client.post(
        "/rollouts/%s/artifact" % rollout["id"],
        json={
            "artifact_uri": "registry://team/mac@sha256:abc",
            "artifact_hash": "sha256:abcabcabc",
            "actor": "ops",
        },
    ).json()
    assert pinned["artifact_hash"].startswith("sha256:")

    started = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "start_canary", "actor": "ops", "detail": {}},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "canarying"

    # Pass the health gate so the eval gate is the next thing standing.
    health = client.post(
        "/rollouts/%s/health" % rollout["id"],
        json={"actor": "ops", "checks": {"latency_p95_ms": "ok", "error_rate": "ok"}},
    ).json()
    assert health["healthy"] is True

    # Promotion is now blocked specifically on the eval gate.
    blocked = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "promote", "actor": "ops", "detail": {}},
    )
    assert blocked.status_code == 400
    assert "eval" in blocked.json()["detail"].lower()

    # A failing eval run keeps the gate closed.
    client.post(
        "/eval-runs",
        json={
            "eval_set_id": eval_set["id"],
            "target_kind": "rollout_version",
            "target_id": rollout["version"],
            "score": 0.5,
            "created_by": "ops",
        },
    )
    still_blocked = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "promote", "actor": "ops", "detail": {}},
    )
    assert still_blocked.status_code == 400

    # A passing run opens the gate.
    client.post(
        "/eval-runs",
        json={
            "eval_set_id": eval_set["id"],
            "target_kind": "rollout_version",
            "target_id": rollout["version"],
            "score": 0.95,
            "created_by": "ops",
        },
    )
    promoted = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "promote", "actor": "ops", "detail": {}},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"


# ---------------------------------------------------------------------------
# Test 4: secret handle is single-use
# ---------------------------------------------------------------------------


def test_e2e_secret_handle_is_single_use_via_http(tmp_path: Path):
    client = _disk_app(tmp_path)
    machine = client.post("/machines", json={"hostname": "host-secret"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "ops", "capabilities": ["deploy"]},
    ).json()
    secret = client.post(
        "/secrets",
        json={
            "name": "deploy-token",
            "value": "plaintext-only-revealed-once",
            "scopes": {"capabilities": ["deploy"]},
            "created_by": "ops",
        },
    ).json()
    handle = client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "deploy"},
    ).json()
    assert handle["handle"].startswith("secret://")

    first = client.post(
        "/secrets/%s/reveal" % secret["id"],
        json={"audit_id": handle["audit_id"], "accessor_agent_id": agent["id"]},
    )
    assert first.status_code == 200
    assert first.json()["value"] == "plaintext-only-revealed-once"

    # Second reveal with the same handle is refused — single-use.
    second = client.post(
        "/secrets/%s/reveal" % secret["id"],
        json={"audit_id": handle["audit_id"], "accessor_agent_id": agent["id"]},
    )
    assert second.status_code == 403


# ---------------------------------------------------------------------------
# Test 5: workflow drives a task end-to-end
# ---------------------------------------------------------------------------


def test_e2e_workflow_runtime_drives_task_via_http(tmp_path: Path):
    client = _disk_app(tmp_path)

    # Roles + a minimal one-node workflow that ends after a single success.
    client.post(
        "/roles",
        json={
            "slug": "qa",
            "name": "QA",
            "description": "checks things",
            "system_prompt": "Run the tests.",
            "level": "ic",
            "default_capabilities": ["python", "qa"],
        },
    )
    workflow = client.post(
        "/workflows",
        json={
            "slug": "smoke",
            "name": "Smoke",
            "description": "single-node",
            "workflow_type": "smoke",
            "created_by": "ops",
            "definition": {
                "nodes": [
                    {
                        "node_key": "run",
                        "node_type": "task",
                        "role_required": "qa",
                        "max_attempts": 1,
                    }
                ],
                "edges": [
                    {"from_node_key": "", "to_node_key": "run", "condition": "success", "priority": 100},
                    {"from_node_key": "run", "to_node_key": "", "condition": "failure", "priority": 100},
                ],
            },
        },
    ).json()
    assert workflow["slug"] == "smoke"

    machine = client.post("/machines", json={"hostname": "host-wf"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "wf-runner", "capabilities": ["python"]},
    ).json()
    # Assign role so dispatcher accepts the workflow's required_role pin.
    client.post(
        "/agents/%s/role" % agent["id"], json={"role_id_or_slug": "qa"}
    )

    run = client.post(
        "/workflows/smoke/start", json={"started_by": "ops"}
    ).json()
    assert run["state"] == "running"
    assert run["current_node_key"] == "run"

    # Worker drives the task to failure (we only wired a failure→end edge
    # in this minimal workflow, so failure leads to a terminal state).
    api = MacApiClient("http://mac.test", transport=_api_transport(client))
    macworker = MacWorker(
        api,
        agent["id"],
        tmp_path / "ws-wf",
        lambda _t, _d: WorkerExecution(2, "boom", stderr="boom\n"),
    )
    macworker.run_once()

    fresh = client.get("/workflows/runs/%s" % run["id"]).json()
    assert fresh["state"] == "failed"
    assert fresh["completed_at"] is not None
