"""Atomicity tests for the task-lifecycle domain-event outbox.

The control plane used to update the core ``tasks`` row + history row
inside a transaction, then fire workflow-runtime and Beads side-effects
*after* COMMIT. A crash (or any post-commit exception) between COMMIT
and the side-effect silently dropped the side-effect: Beads never
learned about the transition, workflow runs never advanced.

These tests pin down the new behaviour:

  1. Every transition path that has a relevant side-effect writes an
     intent row into ``domain_events`` *inside the same transaction* as
     the state change. A crash before COMMIT rolls everything back; a
     crash after COMMIT leaves the row ``pending`` for replay.
  2. ``drain_domain_events`` dispatches pending rows and marks them
     ``delivered`` exactly once per successful dispatch.
  3. Handler failures keep the row ``pending`` (with last_error
     recorded) so the next drain retries.
"""

from __future__ import annotations

import pytest

from mac.models import TaskState, TransitionError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp: ControlPlane, name: str = "alpha", caps=None):
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=caps or ["python"])


def _pending_events(cp: ControlPlane, task_id: str):
    return cp.store.query_all(
        "SELECT event_type, status, attempts, last_error, payload "
        "FROM domain_events WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    )


# ---------------------------------------------------------------------------
# Enqueue happens inside the transition transaction.
# ---------------------------------------------------------------------------


def test_transition_to_failed_enqueues_beads_reopen_in_same_txn(cp):
    """A FAILED transition writes a `beads.reopen` outbox row atomically.

    We disable beads sync at the binding level (no binding => handler
    noops) so the test focuses on the *enqueue*, not the dispatch.
    """
    agent = _register_agent(cp)
    task = cp.create_task("outbox-fail", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)

    # Pre-condition: claim itself enqueued a beads.claim event. It
    # already drained (handler noops because there's no Beads binding),
    # so it sits as 'failed' (no binding -> handler noops -> delivered)
    # or 'delivered'. Either way it is not still 'pending'.
    rows_before = _pending_events(cp, task.id)
    assert any(r["event_type"] == "beads.claim" for r in rows_before)
    assert all(r["status"] != "pending" for r in rows_before)

    cp.transition_task(task.id, TaskState.FAILED.value, agent.id, {"reason": "boom"})

    rows_after = _pending_events(cp, task.id)
    reopen_rows = [r for r in rows_after if r["event_type"] == "beads.reopen"]
    assert len(reopen_rows) == 1, rows_after
    # After drain (which runs synchronously at end of transition_task),
    # the row is delivered. The handler noops in-memory because there's
    # no Beads binding for this task.
    assert reopen_rows[0]["status"] == "delivered"


def test_transition_history_failure_rolls_back_outbox_intent(cp):
    """If the in-txn history write raises, the outbox row must roll back too.

    This is the regression test for the original bug: state + history +
    outbox must commit together or not at all.
    """
    agent = _register_agent(cp)
    task = cp.create_task("outbox-rollback", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)

    # Force _record_history to explode mid-transition.
    original = cp._record_history

    def boom(*args, **kwargs):
        raise RuntimeError("simulated history failure")

    cp._record_history = boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            cp.transition_task(task.id, TaskState.FAILED.value, agent.id)
    finally:
        cp._record_history = original  # type: ignore[assignment]

    # Task should still be RUNNING and no `beads.reopen` event should
    # have been recorded.
    same = cp.get_task(task.id)
    assert same.state == TaskState.RUNNING.value
    rows = _pending_events(cp, task.id)
    assert not any(r["event_type"] == "beads.reopen" for r in rows)


def test_drain_replays_pending_event_when_handler_recovers(cp):
    """A failing handler keeps the row pending; the next drain retries."""
    agent = _register_agent(cp)
    task = cp.create_task("outbox-retry", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)

    # Replace the workflow.advance handler with one that fails the
    # first time, succeeds the second time. We attach to a synthetic
    # workflow_run_id directly on the task row so the enqueue path
    # actually fires.
    cp.store.execute(
        "UPDATE tasks SET workflow_run_id = ? WHERE id = ?",
        ("wfrun_synthetic", task.id),
    )
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        # On retry: noop (don't actually advance — we don't have a real run).
        return None

    cp.register_domain_event_handler("workflow.advance", flaky)

    # FAILED is terminal, so transition_task enqueues a workflow.advance.
    cp.transition_task(task.id, TaskState.FAILED.value, agent.id, {"reason": "x"})

    # First drain happened inside transition_task and failed.
    pending = [
        r for r in _pending_events(cp, task.id)
        if r["event_type"] == "workflow.advance"
    ]
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["attempts"] == 1
    assert "transient" in (pending[0]["last_error"] or "")

    # Replaying drains the same row successfully.
    delivered = cp.drain_domain_events()
    assert delivered == 1
    final = [
        r for r in _pending_events(cp, task.id)
        if r["event_type"] == "workflow.advance"
    ]
    assert final[0]["status"] == "delivered"
    assert final[0]["attempts"] == 2
    assert calls["n"] == 2


def test_drain_is_idempotent_when_no_pending(cp):
    """drain_domain_events returns 0 and does nothing when nothing is pending."""
    assert cp.drain_domain_events() == 0


def test_claim_enqueues_beads_claim_intent(cp):
    """claim_task enqueues a beads.claim outbox row inside its transaction."""
    agent = _register_agent(cp)
    task = cp.create_task("outbox-claim", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)

    rows = _pending_events(cp, task.id)
    claim_rows = [r for r in rows if r["event_type"] == "beads.claim"]
    assert len(claim_rows) == 1
    # No Beads binding for the in-memory test fixture, so handler noops
    # and the row settles as delivered.
    assert claim_rows[0]["status"] == "delivered"


def test_drain_marks_unknown_event_type_as_failed_not_pending(cp):
    """Outbox rows with no registered handler must not loop forever."""
    # Insert a synthetic row directly (no handler for this event_type).
    with cp.store.transaction() as conn:
        cp._enqueue_domain_event(conn, "no.such.handler", {"foo": "bar"})

    delivered = cp.drain_domain_events()
    assert delivered == 0  # nothing was delivered
    row = cp.store.query_one(
        "SELECT status, last_error FROM domain_events WHERE event_type = ?",
        ("no.such.handler",),
    )
    assert row is not None
    assert row["status"] == "failed"
    assert "no handler registered" in (row["last_error"] or "")
    # Next drain is a no-op (row is no longer pending).
    assert cp.drain_domain_events() == 0
