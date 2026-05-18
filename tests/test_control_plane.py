import threading

import pytest

from mac.models import (
    AgentStatus,
    AuthorizationError,
    HealthStatus,
    LeaseStatus,
    ReviewStatus,
    RolloutStatus,
    TaskState,
    TransitionError,
    ValidationError,
    utcnow,
)
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def register_agent(cp, name="agent", capabilities=None):
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=capabilities or [])


def test_hermes_identity_context_and_interaction_task_boundaries(cp):
    tenant = cp.register_tenant("acme")
    user = cp.register_user(tenant.id, "jordan", display_name="Jordan")
    persona = cp.register_persona(
        tenant.id,
        "Rocky",
        soul_ref="hermes://acme/rocky/SOUL.md",
        memory_scope="hermes://acme/rocky/memory",
    )
    hermes = cp.register_hermes_instance(
        tenant.id,
        "rocky",
        persona_id=persona.id,
        home_ref="hermes://acme/rocky",
    )
    binding = cp.register_platform_binding(
        tenant.id,
        hermes.id,
        "slack",
        "T123/C456",
        display_name="#ops",
        scopes={"channels": ["C456"]},
    )

    context = cp.hermes_context(hermes.id)
    assert context["memory_contract"]["personality_authority"] == "hermes"
    assert context["memory_contract"]["operational_provenance_authority"] == "mac"
    assert context["persona"]["soul_ref"] == "hermes://acme/rocky/SOUL.md"
    assert context["platform_bindings"][0]["id"] == binding.id

    task = cp.create_interaction_task(
        hermes.id,
        "Investigate incident",
        user_id=user.id,
        platform_binding_id=binding.id,
        conversation_ref="slack://T123/C456/1712345678.000100",
        required_capabilities=["ops"],
    )
    assert task.metadata["origin"]["type"] == "hermes_interaction"
    assert task.metadata["origin"]["tenant_id"] == tenant.id
    assert task.metadata["origin"]["persona_id"] == persona.id
    assert task.metadata["memory_boundary"]["hermes_is_authoritative_for_user_memory"] is True
    assert "SOUL.md" not in task.description


def finish_task(cp, task, worker, reviewer):
    if task.state == TaskState.OPEN.value:
        task, _lease = cp.claim_task(task.id, worker.id)
    if task.state == TaskState.CLAIMED.value:
        task = cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(task.id, "test", "artifact://tests", "tests passed", worker.id)
    task = cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=evidence.id)
    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)
    return cp.get_task(task.id)


def test_task_lifecycle_requires_evidence_review_and_publication(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])

    assignment = cp.dispatch_once()
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == worker.id

    cp.start_task(task.id, worker.id)
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)

    evidence = cp.add_evidence(task.id, "test", "artifact://pytest", "pytest passed", worker.id)
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    assert review.status == ReviewStatus.PENDING.value

    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=evidence.id)
    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    completed = cp.get_task(task.id)
    assert completed.state == TaskState.COMPLETED.value
    assert publication.status == "published"
    assert cp.get_agent(worker.id).status == AgentStatus.IDLE.value
    event_types = [event.event_type for event in cp.task_history(task.id)]
    assert "task.claimed" in event_types
    assert "task.review_completed" in event_types
    assert "task.published" in event_types


def test_dispatcher_matches_capabilities_and_expired_leases_recover(cp):
    python_agent = register_agent(cp, "python", ["python"])
    docs_agent = register_agent(cp, "docs", ["docs"])
    task = cp.create_task("Python work", required_capabilities=["python"], max_attempts=2)

    assignment = cp.dispatch_once(lease_seconds=-1)
    assert assignment["agent"]["id"] == python_agent.id
    assert assignment["agent"]["id"] != docs_agent.id

    recovered = cp.expire_leases(now=utcnow())
    assert [item.id for item in recovered] == [task.id]
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    assert cp.get_agent(python_agent.id).status == AgentStatus.IDLE.value


def test_dependencies_block_until_parent_completes(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    parent = cp.create_task("Parent", required_capabilities=["python"])
    child = cp.create_task("Child", required_capabilities=["python"], dependencies=[parent.id])

    assert child.state == TaskState.BLOCKED.value
    finish_task(cp, parent, worker, reviewer)
    tick = cp.tick()

    assert cp.get_task(child.id).state == TaskState.CLAIMED.value
    assert tick["assignments"][0]["task"]["id"] == child.id


def test_message_bus_accepts_structured_payloads_and_rejects_execution(cp):
    sender = register_agent(cp, "sender", ["python"])
    recipient = register_agent(cp, "recipient", ["review"])
    message = cp.send_message(
        sender.id,
        recipient.id,
        "help_request",
        {"question": "Can you inspect this evidence?", "evidence_id": "ev_123"},
    )

    delivered = cp.deliver_messages(recipient.id)
    assert delivered[0].id == message.id
    assert delivered[0].status == "delivered"

    with pytest.raises(ValidationError):
        cp.send_message(
            sender.id,
            recipient.id,
            "help_request",
            {"question": "Can you inspect this evidence?", "command": "rm -rf /"},
        )


def test_secrets_are_scoped_redacted_audited_and_not_stored_plaintext(cp):
    deployer = register_agent(cp, "deployer", ["deploy"])
    docs = register_agent(cp, "docs", ["docs"])
    secret = cp.create_secret(
        "github-token",
        "super-secret-token",
        {"capabilities": ["deploy"]},
        "human",
    )

    handle = cp.request_secret(secret.id, deployer.id, "publish release")
    assert handle.handle.startswith("secret://")
    assert "super-secret-token" not in handle.handle
    assert cp.reveal_secret(secret.id, handle.audit_id, deployer.id) == "super-secret-token"

    with pytest.raises(AuthorizationError):
        cp.request_secret(secret.id, docs.id, "read docs")

    redacted = cp.list_secrets()[0].to_dict()
    assert redacted["value"] == "***REDACTED***"
    stored = cp.store.query_one("SELECT ciphertext FROM secrets WHERE id = ?", (secret.id,))
    assert stored["ciphertext"] != "super-secret-token"
    audits = cp.list_secret_audits(secret.id)
    assert [audit.result for audit in audits] == ["granted", "denied"]


def test_runtime_boundary_pins_manifests_and_blocks_secret_values(cp):
    manifest = {
        "image": "python:3.12@sha256:abc123",
        "dependencies": ["fastapi==0.111.0"],
        "entrypoint": ["pytest"],
        "secret_refs": ["github-token"],
    }
    runtime = cp.create_runtime("pytest", manifest, "human")
    same = cp.create_runtime("pytest-copy", dict(reversed(list(manifest.items()))), "human")

    assert runtime.digest == same.digest
    with pytest.raises(ValidationError):
        cp.create_runtime("latest", {"image": "python:latest"}, "human")
    with pytest.raises(ValidationError):
        cp.create_runtime("leaky", {"image": "python:3.12@sha256:abc123", "env": {"TOKEN": "raw"}}, "human")


def test_project_bridge_memory_and_rollout_rescue(cp):
    item = cp.import_project_item(
        "github",
        "42",
        "Fix issue",
        {"url": "https://example.invalid/issues/42"},
        required_capabilities=["python"],
    )
    duplicate = cp.import_project_item("github", "42", "Fix issue", {"url": "ignored"})
    assert duplicate.id == item.id
    assert cp.get_task(item.task_id).metadata["external_id"] == "42"
    assert cp.search_memory(task_id=item.task_id)[0].record_type == "imported"

    rollout = cp.create_rollout("0.2.0", "canary", 10, "human")
    canary = cp.advance_rollout(rollout.id, "start_canary", "human")
    assert canary.status == RolloutStatus.CANARYING.value

    rescued, rescue_task = cp.rescue_rollout(rollout.id, "human", "canary failed health checks")
    assert rescued.status == RolloutStatus.RESCUING.value
    assert rescue_task.priority == 100
    assert rescue_task.metadata["rescue"] is True


def test_concurrent_claim_picks_exactly_one_winner(cp):
    worker_a = register_agent(cp, "worker-a", ["python"])
    worker_b = register_agent(cp, "worker-b", ["python"])
    task = cp.create_task("contested", required_capabilities=["python"])
    results = {}
    barrier = threading.Barrier(2)

    def claim(name, agent_id):
        barrier.wait()
        try:
            claimed, lease = cp.claim_task(task.id, agent_id)
            results[name] = ("ok", claimed.id, lease.id)
        except (TransitionError, ValidationError) as exc:
            results[name] = ("err", str(exc), None)

    threads = [
        threading.Thread(target=claim, args=("a", worker_a.id)),
        threading.Thread(target=claim, args=("b", worker_b.id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = [results[name][0] for name in ("a", "b")]
    assert outcomes.count("ok") == 1
    assert outcomes.count("err") == 1
    final_task = cp.get_task(task.id)
    assert final_task.state == TaskState.CLAIMED.value
    assert final_task.attempt_count == 1
    leases = cp.store.query_all("SELECT id FROM leases WHERE task_id = ?", (task.id,))
    assert len(leases) == 1


def test_reviewer_cannot_be_task_owner(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(task.id, "test", "artifact://t", "tests passed", worker.id)
    cp.submit_for_review(task.id, worker.id)

    with pytest.raises(AuthorizationError):
        cp.request_review(task.id, worker.id)


def test_review_approval_requires_evidence_id(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(task.id, "test", "artifact://t", "tests passed", worker.id)
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)

    with pytest.raises(ValidationError):
        cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id)


def test_completion_requires_evidence_linked_from_approved_review(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(task.id, "test", "artifact://t", "tests passed", worker.id)
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=evidence.id)
    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)
    assert publication.status == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_idle_heartbeat_requires_no_active_lease(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)
    assert cp.get_agent(worker.id).current_task_id == task.id

    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, status="not-a-real-state")
    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, health_status="hot")
    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, status=AgentStatus.IDLE.value)

    refreshed = cp.get_agent(worker.id)
    assert refreshed.status == AgentStatus.BUSY.value
    assert refreshed.current_task_id == claimed.id

    cp.release_lease(lease.id, worker.id)
    refreshed = cp.heartbeat_agent(worker.id, status=AgentStatus.IDLE.value)
    assert refreshed.status == AgentStatus.IDLE.value
    assert refreshed.current_task_id is None


def test_offline_heartbeat_expires_active_lease_and_requeues_work(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)

    refreshed = cp.heartbeat_agent(worker.id, status=AgentStatus.OFFLINE.value)

    assert refreshed.status == AgentStatus.OFFLINE.value
    assert refreshed.current_task_id is None
    assert cp.get_lease(lease.id).status == LeaseStatus.EXPIRED.value
    recovered = cp.get_task(claimed.id)
    assert recovered.state == TaskState.OPEN.value
    assert recovered.owner_agent_id is None
    assert cp.dispatch_once() is None


def test_register_tenant_preserves_metadata_on_reregister(cp):
    first = cp.register_tenant("acme", metadata={"region": "eu-west"})
    second = cp.register_tenant("acme")
    assert second.id == first.id
    assert second.metadata == {"region": "eu-west"}


def test_untrusted_machine_agent_cannot_request_secret(cp):
    untrusted_machine = cp.register_machine("untrusted-host", trusted=False)
    agent = cp.register_agent(untrusted_machine.id, "shady", capabilities=["deploy"])
    secret = cp.create_secret(
        "deploy-token", "value-xyz", {"capabilities": ["deploy"]}, "human"
    )
    with pytest.raises(AuthorizationError):
        cp.request_secret(secret.id, agent.id, "deploy")


def test_secret_handle_is_single_use_and_agent_bound(cp):
    deployer = register_agent(cp, "deployer", ["deploy"])
    other = register_agent(cp, "other", ["deploy"])
    secret = cp.create_secret(
        "deploy-token", "value-xyz", {"capabilities": ["deploy"]}, "human"
    )
    handle = cp.request_secret(secret.id, deployer.id, "deploy")
    # Wrong agent cannot redeem.
    with pytest.raises(AuthorizationError):
        cp.reveal_secret(secret.id, handle.audit_id, other.id)
    # Correct agent succeeds once.
    assert cp.reveal_secret(secret.id, handle.audit_id, deployer.id) == "value-xyz"
    # Same handle cannot be redeemed again.
    with pytest.raises(AuthorizationError):
        cp.reveal_secret(secret.id, handle.audit_id, deployer.id)


def test_secret_handle_expires(cp):
    deployer = register_agent(cp, "deployer", ["deploy"])
    secret = cp.create_secret(
        "deploy-token", "value-xyz", {"capabilities": ["deploy"]}, "human"
    )
    handle = cp.request_secret(secret.id, deployer.id, "deploy", ttl_seconds=1)
    cp.store.execute(
        "UPDATE secret_access_audit SET expires_at = '1970-01-01T00:00:00+00:00' WHERE id = ?",
        (handle.audit_id,),
    )
    with pytest.raises(AuthorizationError):
        cp.reveal_secret(secret.id, handle.audit_id, deployer.id)


def test_rotate_secret_writes_audit_row(cp):
    secret = cp.create_secret(
        "deploy-token", "v1", {"capabilities": ["deploy"]}, "human"
    )
    cp.rotate_secret(secret.id, "v2", "human-operator")
    audits = cp.list_secret_audits(secret.id)
    rotations = [a for a in audits if a.result == "rotated"]
    assert len(rotations) == 1
    assert rotations[0].accessor_agent_id == "human-operator"


def test_rollout_pause_then_resume_round_trips(cp):
    rollout = cp.create_rollout("1.0", "canary", 10, "human")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    paused = cp.advance_rollout(rollout.id, "pause", "human")
    assert paused.status == RolloutStatus.PAUSED.value
    resumed = cp.advance_rollout(rollout.id, "resume", "human")
    assert resumed.status == RolloutStatus.CANARYING.value


def test_rollout_promote_from_paused_is_allowed_pause_from_promoted_is_not(cp):
    rollout = cp.create_rollout("1.1", "canary", 10, "human")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.advance_rollout(rollout.id, "pause", "human")
    promoted = cp.advance_rollout(rollout.id, "promote", "human")
    assert promoted.status == RolloutStatus.PROMOTED.value
    assert promoted.target_percent == 100
    with pytest.raises(TransitionError):
        cp.advance_rollout(rollout.id, "pause", "human")


def test_runtime_manifest_rejects_nested_latest_and_substring_secret_fields(cp):
    with pytest.raises(ValidationError):
        cp.create_runtime(
            "nested-latest",
            {"containers": [{"image": "python:latest"}]},
            "human",
        )
    with pytest.raises(ValidationError):
        cp.create_runtime(
            "leaky-api-key",
            {"image": "python:3.12@sha256:abc", "env": {"api_key": "raw"}},
            "human",
        )
