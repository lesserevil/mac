import pytest

from mac.models import NotFoundError, TaskState, ValidationError
from mac.services import ControlPlane
from tests.conftest import bind_soul


@pytest.fixture()
def cp():
    cp = ControlPlane.in_memory()
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["python", "qa"],
    )
    cp.roles.create_role(
        slug="dev",
        name="Dev",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["python"],
    )
    return cp


def _two_node_workflow(cp, *, slug="bug-default"):
    return cp.workflows.create_workflow(
        slug=slug,
        name="Bug",
        description="walk a bug to fix",
        workflow_type="bug",
        definition={
            "nodes": [
                {
                    "node_key": "investigate",
                    "node_type": "task",
                    "role_required": "qa",
                    "max_attempts": 1,
                },
                {
                    "node_key": "fix",
                    "node_type": "task",
                    "role_required": "dev",
                    "max_attempts": 1,
                },
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "fix", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "", "condition": "failure", "priority": 100},
                {"from_node_key": "fix", "to_node_key": "", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )


def test_start_run_spawns_first_node_task_with_role_metadata(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    assert run.state == "running"
    assert run.current_node_key == "investigate"
    task = cp.get_task(run.current_task_id)
    assert task.metadata["workflow_run_id"] == run.id
    assert task.metadata["workflow_node_key"] == "investigate"
    assert task.metadata["required_role"] == "qa"
    # role.default_capabilities stack onto the task.
    assert "qa" in task.required_capabilities
    assert "python" in task.required_capabilities


def test_run_advances_on_task_completed_through_to_terminal(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    first_task = cp.get_task(run.current_task_id)

    # Register a worker that satisfies the qa role, claim and run the
    # first task to NEEDS_REVIEW then add evidence and approve.
    machine = cp.register_machine("h1")
    qa_soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    qa_agent = cp.register_agent(
        machine.id,
        "rocky",
        capabilities=["python", "qa", "review"],
        hermes_instance_id=qa_soul,
    )
    cp.roles.assign_role(qa_agent.id, "qa")
    cp.claim_task(first_task.id, qa_agent.id)
    cp.start_task(first_task.id, qa_agent.id)
    cp.add_evidence(first_task.id, "test", "file://t", "tests passed", "rocky")
    cp.submit_for_review(first_task.id, qa_agent.id)

    # Reviewer registered separately so they can request + approve.
    # No role assignment so no soul is required.
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    review = cp.request_review(first_task.id, reviewer.id, "ops")
    evidence = cp.list_evidence(first_task.id)[0]
    cp.submit_review(
        review.id,
        "approved",
        reviewer.id,
        evidence_id=evidence.id,
    )
    cp.publish_task(first_task.id, "stdout", "ops")

    # The runtime should have spawned the next node when the first task
    # hit COMPLETED via publish_task.
    run = cp.workflow_runtime.get_run(run.id)
    assert run.current_node_key == "fix"
    fix_task = cp.get_task(run.current_task_id)
    assert fix_task.metadata["required_role"] == "dev"


def test_failed_task_picks_failure_edge_and_finishes(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    first_task = cp.get_task(run.current_task_id)

    machine = cp.register_machine("h1")
    soul = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    qa_agent = cp.register_agent(
        machine.id, "rocky", capabilities=["python", "qa"], hermes_instance_id=soul
    )
    cp.roles.assign_role(qa_agent.id, "qa")
    cp.claim_task(first_task.id, qa_agent.id)
    cp.start_task(first_task.id, qa_agent.id)
    # Fail the task — there's a failure edge to '' (terminal).
    cp.transition_task(first_task.id, TaskState.FAILED.value, "rocky")

    run = cp.workflow_runtime.get_run(run.id)
    assert run.state == "failed"
    assert run.completed_at is not None


def test_cancel_run_cancels_current_task_and_marks_run_cancelled(cp):
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")
    first_task_id = run.current_task_id

    cancelled = cp.workflow_runtime.cancel_run(
        run.id, reason="operator abort", actor="ops"
    )
    assert cancelled.state == "cancelled"
    # The current task got cancelled too.
    assert cp.get_task(first_task_id).state == TaskState.CANCELLED.value


def test_forged_workflow_run_id_metadata_is_ignored_by_runtime(cp):
    """A caller cannot smuggle a free-floating task into the workflow
    state machine by setting metadata.workflow_run_id — the runtime
    only acts on tasks where the *column* tasks.workflow_run_id is set,
    which only the runtime does itself."""
    _two_node_workflow(cp)
    run = cp.workflow_runtime.start_run("bug-default", started_by="ops")

    # Create a separate task that pretends to belong to the run.
    forged = cp.create_task(
        "forged",
        required_capabilities=[],
        metadata={"workflow_run_id": run.id, "workflow_node_key": "fix"},
    )
    # Bring it to terminal state.
    machine = cp.register_machine("h2")
    agent = cp.register_agent(machine.id, "outsider")
    cp.claim_task(forged.id, agent.id)
    cp.start_task(forged.id, agent.id)
    cp.transition_task(forged.id, TaskState.FAILED.value, "outsider")

    # Run hasn't moved off its first node.
    run_after = cp.workflow_runtime.get_run(run.id)
    assert run_after.current_node_key == "investigate"
    assert run_after.state == "running"


def test_disabled_workflow_cannot_be_started(cp):
    wf = _two_node_workflow(cp, slug="bug-disabled")
    cp.workflows.disable_workflow(wf.id)
    with pytest.raises(ValidationError):
        cp.workflow_runtime.start_run("bug-disabled", started_by="ops")


def test_tick_times_out_stuck_node_and_advances_via_failure_edge(cp):
    # Workflow whose first node has a 1-minute timeout and a failure
    # edge to the terminal sink.
    cp.workflows.create_workflow(
        slug="bug-timeout",
        name="b",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {
                    "node_key": "investigate",
                    "node_type": "task",
                    "role_required": "qa",
                    "max_attempts": 1,
                    "timeout_minutes": 1,
                }
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "", "condition": "cancelled", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("bug-timeout", started_by="ops")
    task_id = run.current_task_id

    # Backdate the task so it's been "running" past the timeout.
    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", task_id),
    )
    advanced = cp.workflow_runtime.tick()
    assert any(r.id == run.id for r in advanced)
    assert cp.workflow_runtime.get_run(run.id).state in {"failed", "cancelled"}
