import json
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


def create_runtime(cp, name="runtime"):
    return cp.create_runtime(
        name,
        {
            "image": "python:3.12@sha256:abc123",
            "dependencies": ["fastapi==0.111.0"],
            "entrypoint": ["pytest"],
        },
        "human",
    )


def create_verified_rollout(cp, version="1.0", strategy="canary", tenant_id=None, channel="fleet", health_policy=None):
    runtime = create_runtime(cp, "runtime-%s" % version)
    return cp.create_rollout(
        version,
        strategy,
        10,
        "human",
        tenant_id=tenant_id,
        channel=channel,
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/%s" % version,
        artifact_hash="sha256:abc123",
        health_policy=health_policy or {},
    )


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


def test_tenant_scoped_task_visibility_and_machine_pool_policy(cp):
    tenant_a = cp.register_tenant("tenant-a")
    tenant_b = cp.register_tenant("tenant-b")
    hermes_a = cp.register_hermes_instance(tenant_a.id, "rocky")
    hermes_b = cp.register_hermes_instance(tenant_b.id, "natasha")
    task_a = cp.create_interaction_task(hermes_a.id, "A work", required_capabilities=["python"])
    task_b = cp.create_interaction_task(
        hermes_b.id,
        "B work",
        priority=50,
        required_capabilities=["python"],
    )
    machine = cp.register_machine(
        "private-a",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_a.id]}},
    )
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])

    assert [task.id for task in cp.list_tasks(tenant_id=tenant_a.id)] == [task_a.id]
    assignment = cp.dispatch_once()

    assert assignment["task"]["id"] == task_a.id
    assert assignment["agent"]["id"] == agent.id
    assert cp.get_task(task_b.id).state == TaskState.OPEN.value


def test_tenant_scoped_secret_requires_machine_policy_and_capability(cp):
    tenant_a = cp.register_tenant("tenant-a")
    tenant_b = cp.register_tenant("tenant-b")
    machine = cp.register_machine(
        "private-a",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_a.id]}},
    )
    agent = cp.register_agent(machine.id, "deployer", capabilities=["deploy"])
    allowed = cp.create_secret(
        "tenant-a-token",
        "a-secret",
        {"tenant_id": tenant_a.id, "capabilities": ["deploy"]},
        "human",
    )
    denied = cp.create_secret(
        "tenant-b-token",
        "b-secret",
        {"tenant_id": tenant_b.id, "capabilities": ["deploy"]},
        "human",
    )

    assert cp.request_secret(allowed.id, agent.id, "deploy").granted is True
    with pytest.raises(AuthorizationError):
        cp.request_secret(denied.id, agent.id, "deploy")


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


def test_dispatcher_respects_capacity_resources_and_dead_letters(cp):
    small = register_agent(cp, "small", ["python"])
    large_machine = cp.register_machine("large-host", resources={"memory_gb": 32})
    large = cp.register_agent(
        large_machine.id,
        "large",
        capabilities=["python"],
        resources={"capacity": 2, "memory_gb": 16},
    )
    cp.create_task(
        "needs memory",
        required_capabilities=["python"],
        metadata={"resources": {"memory_gb": 12}},
    )
    cp.create_task("second slot", required_capabilities=["python"])

    first = cp.dispatch_once()
    second = cp.dispatch_once()

    assert first["agent"]["id"] == large.id
    assert second["agent"]["id"] == large.id
    assert cp.get_agent(small.id).status == AgentStatus.IDLE.value

    dead = cp.create_task("dead letter", required_capabilities=["docs"], max_attempts=1)
    docs = register_agent(cp, "docs-dead", ["docs"])
    cp.claim_task(dead.id, docs.id, lease_seconds=-1)
    cp.expire_leases(now=utcnow())

    assert [task.id for task in cp.list_dead_letters()] == [dead.id]


def test_tick_marks_stale_agents_offline_and_requeues_work(cp):
    worker = register_agent(cp, "stale", ["python"])
    task = cp.create_task("stale work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)
    cp.store.execute(
        "UPDATE agents SET last_seen_at = '1970-01-01T00:00:00+00:00' WHERE id = ?",
        (worker.id,),
    )

    tick = cp.tick(stale_after_seconds=60)

    assert tick["stale_agents"][0]["id"] == worker.id
    assert cp.get_agent(worker.id).status == AgentStatus.OFFLINE.value
    assert cp.get_lease(lease.id).status == LeaseStatus.EXPIRED.value
    assert cp.get_task(claimed.id).state == TaskState.OPEN.value
    assert tick["assignments"] == []


def test_dispatch_tick_round_robins_between_tenants(cp):
    tenant_a = cp.register_tenant("tenant-a")
    tenant_b = cp.register_tenant("tenant-b")
    hermes_a = cp.register_hermes_instance(tenant_a.id, "rocky")
    hermes_b = cp.register_hermes_instance(tenant_b.id, "bullwinkle")
    task_a1 = cp.create_interaction_task(hermes_a.id, "A1", priority=100, required_capabilities=["python"])
    cp.create_interaction_task(hermes_a.id, "A2", priority=90, required_capabilities=["python"])
    task_b = cp.create_interaction_task(hermes_b.id, "B1", priority=10, required_capabilities=["python"])
    for index in range(3):
        register_agent(cp, "fair-%d" % index, ["python"])

    tick = cp.tick(limit=2)

    assert [item["task"]["id"] for item in tick["assignments"]] == [task_a1.id, task_b.id]


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

    rollout = create_verified_rollout(cp, "0.2.0")
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


def test_publication_policy_requires_publication_evidence_with_hash(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "release",
        required_capabilities=["python"],
        metadata={"policy": {"require_publication_evidence": True}},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    test_evidence = cp.add_evidence(task.id, "test", "artifact://tests", "tests passed", worker.id)
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=test_evidence.id)

    with pytest.raises(ValidationError):
        cp.publish_task(task.id, "git://main", reviewer.id)
    with pytest.raises(ValidationError):
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=test_evidence.id)
    with pytest.raises(ValidationError):
        cp.add_evidence(task.id, "publication", "git://main", "published", reviewer.id)

    pub_evidence = cp.add_evidence(
        task.id,
        "publication",
        "git://main",
        "published",
        reviewer.id,
        checksum="sha256:abc123",
    )
    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=pub_evidence.id)

    assert publication.content_hash == "sha256:abc123"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_evidence_kind_is_explicit(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])

    with pytest.raises(ValidationError):
        cp.add_evidence(task.id, "misc", "artifact://x", "unclassified", worker.id)


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
    rollout = create_verified_rollout(cp, "1.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    paused = cp.advance_rollout(rollout.id, "pause", "human")
    assert paused.status == RolloutStatus.PAUSED.value
    resumed = cp.advance_rollout(rollout.id, "resume", "human")
    assert resumed.status == RolloutStatus.CANARYING.value


def test_rollout_promote_from_paused_is_allowed_pause_from_promoted_is_not(cp):
    rollout = create_verified_rollout(cp, "1.1")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "pause", "human")
    promoted = cp.advance_rollout(rollout.id, "promote", "human")
    assert promoted.status == RolloutStatus.PROMOTED.value
    assert promoted.target_percent == 100
    with pytest.raises(TransitionError):
        cp.advance_rollout(rollout.id, "pause", "human")


def test_rollout_install_requires_runtime_and_verified_artifact(cp):
    rollout = cp.create_rollout("2.0", "canary", 10, "human")
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "start_canary", "human")

    runtime = create_runtime(cp, "runtime-2.0")
    rollout = cp.create_rollout(
        "2.1",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
    )
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "start_canary", "human")
    with pytest.raises(ValidationError):
        cp.verify_rollout_artifact(rollout.id, "artifact://mac/2.1", "md5:not-ok", "human")

    verified = cp.verify_rollout_artifact(
        rollout.id,
        "artifact://mac/2.1",
        "sha256:abc123",
        "human",
    )
    assert verified.artifact_hash == "sha256:abc123"
    assert cp.advance_rollout(rollout.id, "start_canary", "human").status == RolloutStatus.CANARYING.value


def test_rollout_health_gate_blocks_promotion_and_failed_health_rescues(cp):
    rollout = create_verified_rollout(
        cp,
        "2.2",
        health_policy={"required_checks": ["runtime", "canary"]},
    )
    with pytest.raises(TransitionError):
        cp.advance_rollout(rollout.id, "promote", "human")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "promote", "human")

    result = cp.evaluate_rollout_health(
        rollout.id,
        {"runtime": "healthy", "canary": {"status": "failed"}},
        "monitor",
    )

    assert result["healthy"] is False
    assert result["failed_checks"] == ["canary"]
    assert result["rollout"]["status"] == RolloutStatus.RESCUING.value
    assert result["rollout"]["target_percent"] == 0
    assert result["rescue_task"]["metadata"]["failed_checks"] == ["canary"]

    healthy = create_verified_rollout(
        cp,
        "2.3",
        health_policy={"required_checks": ["runtime", "canary"]},
    )
    cp.advance_rollout(healthy.id, "start_canary", "human")
    cp.evaluate_rollout_health(healthy.id, {"runtime": True, "canary": "ok"}, "monitor")
    assert cp.advance_rollout(healthy.id, "promote", "human").status == RolloutStatus.PROMOTED.value


def test_rollout_channels_scope_tenant_and_fleet(cp):
    tenant = cp.register_tenant("rollout-tenant")
    fleet = create_verified_rollout(cp, "3.0", strategy="full", channel="fleet")
    tenant_rollout = create_verified_rollout(
        cp,
        "3.1",
        strategy="full",
        tenant_id=tenant.id,
        channel="tenant-stable",
    )

    assert [rollout.id for rollout in cp.list_rollouts(channel="fleet")] == [fleet.id]
    assert [rollout.id for rollout in cp.list_rollouts(tenant_id=tenant.id)] == [tenant_rollout.id]
    assert tenant_rollout.tenant_id == tenant.id
    assert tenant_rollout.channel == "tenant-stable"


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
    with pytest.raises(ValidationError):
        cp.create_runtime(
            "unpinned-image",
            {"image": "python:3.12"},
            "human",
        )


def test_eval_set_scoring_higher_is_better_pass_fail(cp):
    eval_set = cp.create_eval_set(
        "task-success-rate",
        scoring="higher_is_better",
        baseline_score=0.80,
        regression_threshold=0.02,
    )
    passing = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.81)
    assert passing.passed is True
    assert passing.delta == pytest.approx(0.01)

    inside_threshold = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.79)
    assert inside_threshold.passed is True  # 0.01 below baseline, within 0.02 threshold

    regression = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.70)
    assert regression.passed is False
    assert regression.delta == pytest.approx(-0.10)


def test_eval_set_scoring_lower_is_better_pass_fail(cp):
    eval_set = cp.create_eval_set(
        "p95-latency-ms",
        scoring="lower_is_better",
        baseline_score=200.0,
        regression_threshold=20.0,
    )
    improvement = cp.record_eval_run(eval_set.id, "runtime_environment", "rt1", 150.0)
    assert improvement.passed is True

    inside_threshold = cp.record_eval_run(eval_set.id, "runtime_environment", "rt1", 215.0)
    assert inside_threshold.passed is True

    regression = cp.record_eval_run(eval_set.id, "runtime_environment", "rt1", 260.0)
    assert regression.passed is False


def test_eval_run_without_baseline_passes_and_can_seed_baseline(cp):
    eval_set = cp.create_eval_set("first-run", scoring="higher_is_better")
    run = cp.record_eval_run(eval_set.id, "rollout_version", "v0", 0.55)
    assert run.passed is True
    assert run.delta is None

    updated = cp.update_eval_set_baseline(eval_set.id, 0.60)
    assert updated.baseline_score == pytest.approx(0.60)
    # subsequent runs are now compared against the seeded baseline
    follow_up = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.50)
    assert follow_up.passed is False


def test_rollout_promote_requires_passing_eval_run(cp):
    eval_set = cp.create_eval_set(
        "smoke-eval",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    rollout = create_verified_rollout(cp, "2.0")
    # attach the eval_set requirement after-the-fact via a fresh rollout
    runtime = create_runtime(cp, "runtime-2.1")
    gated = cp.create_rollout(
        "2.1",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/2.1",
        artifact_hash="sha256:abc123",
        required_eval_set_id=eval_set.id,
    )
    cp.advance_rollout(gated.id, "start_canary", "human")
    cp.evaluate_rollout_health(gated.id, {}, "human")  # default health gate passes

    # No eval run yet — promote refused.
    with pytest.raises(ValidationError):
        cp.advance_rollout(gated.id, "promote", "human")

    # A failing run is still refused.
    cp.record_eval_run(eval_set.id, "rollout_version", "2.1", 0.70)
    with pytest.raises(ValidationError):
        cp.advance_rollout(gated.id, "promote", "human")

    # A passing run unlocks promote.
    cp.record_eval_run(eval_set.id, "rollout_version", "2.1", 0.92)
    promoted = cp.advance_rollout(gated.id, "promote", "human")
    assert promoted.status == RolloutStatus.PROMOTED.value
    assert promoted.target_percent == 100

    # Sanity: an ungated rollout doesn't need an eval.
    assert rollout.required_eval_set_id is None


def test_eval_run_rejects_unknown_target_kind(cp):
    eval_set = cp.create_eval_set("any", scoring="higher_is_better")
    with pytest.raises(ValidationError):
        cp.record_eval_run(eval_set.id, "not-a-real-kind", "x", 1.0)


def test_evidence_kind_eval_is_accepted(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "eval", "artifact://scorecard.json", "eval scorecard", worker.id
    )
    assert evidence.kind == "eval"


def _gated_rollout(cp, version, eval_set_id):
    runtime = create_runtime(cp, "runtime-%s" % version)
    rollout = cp.create_rollout(
        version,
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/%s" % version,
        artifact_hash="sha256:abc123",
        required_eval_set_id=eval_set_id,
    )
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {}, "human")
    return rollout


def test_eval_gate_blocks_when_failing_run_supersedes_passing(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    rollout = _gated_rollout(cp, "3.0", eval_set.id)
    # An older passing run is no longer "latest" once a failing run lands.
    cp.record_eval_run(eval_set.id, "rollout_version", "3.0", 0.95)
    cp.record_eval_run(eval_set.id, "rollout_version", "3.0", 0.50)
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "promote", "human")


def test_eval_run_rejects_non_eval_evidence(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    test_evidence = cp.add_evidence(
        task.id, "test", "artifact://pytest", "pytest passed", worker.id
    )
    eval_set = cp.create_eval_set("any", scoring="higher_is_better")
    with pytest.raises(ValidationError):
        cp.record_eval_run(
            eval_set.id,
            "rollout_version",
            "v1",
            0.9,
            evidence_id=test_evidence.id,
        )


def test_eval_gate_errors_clearly_when_required_eval_set_is_deleted(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
    )
    rollout = _gated_rollout(cp, "4.0", eval_set.id)
    cp.record_eval_run(eval_set.id, "rollout_version", "4.0", 0.95)
    # Delete the eval_set directly to simulate retirement.
    cp.store.execute("DELETE FROM eval_sets WHERE id = ?", (eval_set.id,))
    with pytest.raises(ValidationError) as exc:
        cp.advance_rollout(rollout.id, "promote", "human")
    assert "no longer exists" in str(exc.value)


def test_eval_gate_records_eval_run_id_in_rollout_event(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    rollout = _gated_rollout(cp, "5.0", eval_set.id)
    run = cp.record_eval_run(eval_set.id, "rollout_version", "5.0", 0.95)
    cp.advance_rollout(rollout.id, "promote", "human")
    rows = cp.store.query_all(
        "SELECT event_type, detail FROM rollout_events WHERE rollout_id = ? ORDER BY created_at, id",
        (rollout.id,),
    )
    promote = [row for row in rows if row["event_type"] == "rollout.promote"]
    assert len(promote) == 1
    detail = json.loads(promote[0]["detail"])
    assert detail["eval_run_id"] == run.id
    assert detail["eval_score"] == pytest.approx(0.95)


def test_eval_set_baseline_change_writes_event(cp):
    eval_set = cp.create_eval_set(
        "drift",
        scoring="higher_is_better",
        baseline_score=0.80,
    )
    cp.update_eval_set_baseline(eval_set.id, 0.85, actor="release-manager")
    events = cp.list_eval_set_events(eval_set.id)
    types = [event["event_type"] for event in events]
    assert "eval_set.created" in types
    baseline_events = [event for event in events if event["event_type"] == "eval_set.baseline_changed"]
    assert len(baseline_events) == 1
    assert baseline_events[0]["actor"] == "release-manager"
    assert baseline_events[0]["detail"]["previous_baseline_score"] == pytest.approx(0.80)
    assert baseline_events[0]["detail"]["new_baseline_score"] == pytest.approx(0.85)


def test_eval_run_event_records_run_id_and_passed(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    run = cp.record_eval_run(eval_set.id, "rollout_version", "6.0", 0.95)
    events = cp.list_eval_set_events(eval_set.id)
    run_events = [event for event in events if event["event_type"] == "eval_set.run_recorded"]
    assert len(run_events) == 1
    assert run_events[0]["detail"]["run_id"] == run.id
    assert run_events[0]["detail"]["passed"] is True


def test_evaluate_rollout_health_failing_twice_does_not_duplicate_rescue(cp):
    rollout = create_verified_rollout(
        cp,
        "7.0",
        health_policy={"required_checks": ["runtime", "canary"]},
    )
    cp.advance_rollout(rollout.id, "start_canary", "human")
    first = cp.evaluate_rollout_health(
        rollout.id,
        {"runtime": "healthy", "canary": {"status": "failed"}},
        "monitor",
    )
    second = cp.evaluate_rollout_health(
        rollout.id,
        {"runtime": "healthy", "canary": {"status": "failed"}},
        "monitor",
    )
    rescue_tasks = [
        task for task in cp.list_tasks()
        if task.metadata.get("rollout_id") == rollout.id and task.metadata.get("rescue")
    ]
    assert len(rescue_tasks) == 1
    # The second call should return the same in-flight rescue task and record an
    # additional health-failure event without spawning a duplicate task.
    assert second["healthy"] is False
    assert second["rescue_task"]["id"] == first["rescue_task"]["id"]
    events = cp.store.query_all(
        "SELECT event_type FROM rollout_events WHERE rollout_id = ? ORDER BY created_at, id",
        (rollout.id,),
    )
    types = [row["event_type"] for row in events]
    assert types.count("rollout.health_failure_during_rescue") == 1


def test_tenant_only_secret_scope_grants_access_to_matching_machine(cp):
    tenant = cp.register_tenant("scoped-tenant")
    machine = cp.register_machine(
        "scoped-host",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant.id]}},
    )
    agent = cp.register_agent(machine.id, "scoped-agent", capabilities=["any"])
    secret = cp.create_secret(
        "tenant-only", "abc", {"tenant_id": tenant.id}, "human"
    )
    handle = cp.request_secret(secret.id, agent.id, "deploy")
    assert handle.granted is True
    revealed = cp.reveal_secret(secret.id, handle.audit_id, agent.id)
    assert revealed == "abc"


def test_runtime_run_status_is_enum_validated(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    runtime = create_runtime(cp, "rt-status")
    run = cp.create_runtime_run(task.id, worker.id, runtime.id)
    assert run.status == "running"
    evidence = cp.add_evidence(task.id, "test", "artifact://t", "tests", worker.id)
    with pytest.raises(ValidationError):
        cp.complete_runtime_run(run.id, evidence.id, status="bogus")
    with pytest.raises(ValidationError):
        cp.complete_runtime_run(run.id, evidence.id, status="running")
    completed = cp.complete_runtime_run(run.id, evidence.id, status="completed")
    assert completed.status == "completed"


def test_events_view_unifies_all_audit_surfaces(cp):
    # Generate one event of each kind.
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("audited", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    # rollout event
    rollout = create_verified_rollout(cp, "8.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    # eval_set event
    eval_set = cp.create_eval_set("audit-eval", scoring="higher_is_better")
    # secret event
    deployer = register_agent(cp, "deployer", ["deploy"])
    secret = cp.create_secret("audit-token", "x", {"capabilities": ["deploy"]}, "human")
    cp.request_secret(secret.id, deployer.id, "audit-test")

    events = cp.list_events(limit=500)
    subject_types = {event["subject_type"] for event in events}
    assert subject_types == {"task", "rollout", "eval_set", "secret"}
    # Each event includes the unified shape.
    for event in events:
        assert set(event.keys()) >= {
            "id",
            "subject_type",
            "subject_id",
            "event_type",
            "actor",
            "detail",
            "created_at",
        }
        assert isinstance(event["detail"], dict)


def test_events_filter_by_subject_returns_only_matching_stream(cp):
    rollout = create_verified_rollout(cp, "8.1")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    eval_set = cp.create_eval_set("audit-eval-2", scoring="higher_is_better")
    cp.update_eval_set_baseline(eval_set.id, 0.5)

    rollout_events = cp.list_events(subject_type="rollout", subject_id=rollout.id)
    assert rollout_events
    assert {event["subject_type"] for event in rollout_events} == {"rollout"}
    assert {event["subject_id"] for event in rollout_events} == {rollout.id}

    eval_events = cp.list_events(subject_type="eval_set", subject_id=eval_set.id)
    types = {event["event_type"] for event in eval_events}
    assert "eval_set.created" in types
    assert "eval_set.baseline_changed" in types


def test_events_filter_by_event_type_prefix(cp):
    rollout = create_verified_rollout(cp, "8.2")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.advance_rollout(rollout.id, "pause", "human")

    rollout_prefix = cp.list_events(event_type_prefix="rollout.")
    assert rollout_prefix
    assert all(event["event_type"].startswith("rollout.") for event in rollout_prefix)


def test_events_filter_by_actor_and_time_window(cp):
    rollout = create_verified_rollout(cp, "8.3")
    cp.advance_rollout(rollout.id, "start_canary", "alice")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "bob")

    alice_events = cp.list_events(actor="alice")
    assert alice_events
    assert all(event["actor"] == "alice" for event in alice_events)

    # since filter
    future = "2999-01-01T00:00:00+00:00"
    assert cp.list_events(since=future) == []


def test_events_rejects_unknown_subject_type(cp):
    with pytest.raises(ValidationError):
        cp.list_events(subject_type="not-a-real-subject")


def test_events_task_detail_includes_from_to_states(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("transitions", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    task_events = cp.list_events(subject_type="task", subject_id=task.id)
    transitions = [
        event for event in task_events if event["event_type"] == "task.transitioned"
    ]
    assert transitions
    # Most recent transition is to RUNNING.
    latest = transitions[0]
    assert latest["detail"].get("to_state") == "running"
    assert latest["detail"].get("from_state") == "claimed"
