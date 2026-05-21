from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, urlencode

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_CONTENT_TYPE,
    REPO_UPDATE_RESULT_SCHEMA,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_SCHEMA,
    REPO_UPDATE_TOPIC,
)
from mac.hermes_adapter import MacApiClient, MacApiError


JsonDict = Dict[str, Any]
Executor = Callable[[JsonDict, Path], "WorkerExecution"]
CommandAuditSink = Callable[[JsonDict], None]
SAFE_GIT_REF_RE = r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$"
VERIFICATION_SCHEMA = "mac.worker_evidence.v1"
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass
class WorkerExecution:
    returncode: int
    summary: str
    stdout: str = ""
    stderr: str = ""
    metadata: JsonDict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass
class WorkerRunResult:
    status: str
    task: Optional[JsonDict] = None
    lease: Optional[JsonDict] = None
    evidence: Optional[JsonDict] = None
    error: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


class SubprocessExecutor:
    def __init__(self, argv: List[str], timeout: Optional[float] = None) -> None:
        if not argv:
            raise MacApiError("executor command is required")
        self.argv = argv
        self.timeout = timeout
        self.audit_sink: Optional[CommandAuditSink] = None
        self.audit_context: JsonDict = {}

    def __call__(self, task: JsonDict, task_dir: Path) -> WorkerExecution:
        env = os.environ.copy()
        repository_context = _load_repository_context(task_dir)
        env.update(
            {
                "MAC_TASK_ID": task["id"],
                "MAC_TASK_FILE": str(task_dir / "task.json"),
                "MAC_TASK_WORKSPACE": str(task_dir),
            }
        )
        if repository_context:
            env.update(_repository_context_env(repository_context))
        command_id = _command_audit_id()
        started_at = _utcnow()
        started_monotonic = time.monotonic()
        base_record = {
            "command_id": command_id,
            "argv": _audit_safe_argv(self.argv),
            "cwd": str(task_dir),
            "task_id": self.audit_context.get("task_id") or task.get("id"),
            "lease_id": self.audit_context.get("lease_id"),
            "started_at": started_at,
            "metadata": {
                "argv_sha256": _sha256_text(json.dumps(self.argv, separators=(",", ":"))),
                **_repository_context_audit_metadata(repository_context),
                **ensure_json_object(self.audit_context.get("metadata")),
            },
        }
        self._emit_audit({**base_record, "phase": "started"})
        try:
            completed = subprocess.run(
                self.argv,
                cwd=str(task_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            completed_at = _utcnow()
            stdout = _coerce_process_output(exc.stdout)
            stderr = _coerce_process_output(exc.stderr)
            self._emit_audit(
                {
                    **base_record,
                    "phase": "timeout",
                    "completed_at": completed_at,
                    "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                    "stdout_sha256": _sha256_text(stdout),
                    "stderr_sha256": _sha256_text(stderr),
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr.encode("utf-8")),
                    "metadata": {
                        **base_record["metadata"],
                        "timeout_seconds": self.timeout,
                    },
                }
            )
            raise
        except OSError as exc:
            completed_at = _utcnow()
            self._emit_audit(
                {
                    **base_record,
                    "phase": "error",
                    "completed_at": completed_at,
                    "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                    "metadata": {**base_record["metadata"], "error": str(exc)},
                }
            )
            raise
        completed_at = _utcnow()
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        self._emit_audit(
            {
                **base_record,
                "phase": "completed" if completed.returncode == 0 else "failed",
                "completed_at": completed_at,
                "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                "returncode": completed.returncode,
                "stdout_sha256": _sha256_text(stdout),
                "stderr_sha256": _sha256_text(stderr),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
            }
        )
        return WorkerExecution(
            returncode=completed.returncode,
            summary=_summary_from_output(completed.returncode, stdout, stderr),
            stdout=stdout,
            stderr=stderr,
            metadata={
                "executor": _audit_safe_argv(self.argv),
                "executor_argv_sha256": base_record["metadata"]["argv_sha256"],
            },
        )

    def _emit_audit(self, record: JsonDict) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink(record)
        except Exception:
            pass


def register_worker(
    client: MacApiClient,
    hostname: Optional[str] = None,
    agent_name: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    resources: Optional[JsonDict] = None,
    machine_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> JsonDict:
    """Register or refresh the machine and agent rows for this worker process."""
    host = (hostname or socket.gethostname()).strip()
    if not host:
        raise MacApiError("hostname is required for worker registration")
    name = (agent_name or host).strip()
    if not name:
        raise MacApiError("agent_name is required for worker registration")
    resolved_machine_id = machine_id or _stable_id("machine", host)
    resolved_agent_id = agent_id or _stable_id("agent", name)
    machine = client.post(
        "/machines",
        {
            "hostname": host,
            "machine_id": resolved_machine_id,
            "labels": {"registered_by": "mac-agent"},
            "resources": resources or {},
            "trusted": True,
        },
    )
    return client.post(
        "/agents",
        {
            "machine_id": machine["id"],
            "name": name,
            "agent_id": resolved_agent_id,
            "capabilities": capabilities or [],
            "resources": resources or {},
        },
    )


class MacWorker:
    """Small worker harness for mac-owned tasks.

    This is intentionally narrower than ACC's deployed worker. It proves the
    claim/start/execute/evidence/review handoff without owning Hermes memory or
    pretending to be the final production daemon.
    """

    def __init__(
        self,
        client: MacApiClient,
        agent_id: str,
        workspace: Path,
        executor: Executor,
        lease_seconds: int = 900,
        running_digest: Optional[str] = None,
        poll_interval_seconds: float = 1.0,
        allowed_projects: Optional[List[str]] = None,
        required_metadata: Optional[JsonDict] = None,
        require_canary: bool = False,
        lease_renew_interval_seconds: Optional[float] = None,
        agentbus_control_enabled: bool = True,
        self_update_repo: Optional[Path] = None,
        agentbus_control_state_path: Optional[Path] = None,
        attestation_key: Optional[str] = None,
    ) -> None:
        if not agent_id:
            raise MacApiError("agent_id is required")
        self.client = client
        self.agent_id = agent_id
        self.workspace = workspace
        self.executor = executor
        if isinstance(self.executor, SubprocessExecutor):
            self.executor.audit_sink = self._record_command_audit
        self.lease_seconds = lease_seconds
        self.running_digest = running_digest
        # Attestation key for signing verification manifests
        # (mac-ng2). Falls back to MAC_ATTESTATION_KEY when not passed.
        # Without a key the worker still writes evidence — but the
        # default-review workflow will reject it as "manifest_not_signed"
        # and refuse to publish. The CLI surfaces this in deploy via
        # MAC_ATTESTATION_KEY.
        self.attestation_key = attestation_key or os.environ.get("MAC_ATTESTATION_KEY")
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.allowed_projects = list(allowed_projects or [])
        self.required_metadata = dict(required_metadata or {})
        self.require_canary = bool(require_canary)
        self.lease_renew_interval_seconds = lease_renew_interval_seconds
        self.agentbus_control_enabled = bool(agentbus_control_enabled)
        self.self_update_repo = (self_update_repo or _default_self_update_repo()).expanduser().resolve()
        self.agentbus_control_state_path = (
            agentbus_control_state_path
            if agentbus_control_state_path is not None
            else self.workspace / ".mac-agentbus-control.json"
        )
        self._stop = False
        self._declared_digest = False
        self._declared_policy = False
        self.active_lease: Optional[JsonDict] = None

    def stop(self) -> None:
        """Signal the run loop to exit after the current task."""
        self._stop = True

    def run_forever(self, max_iterations: Optional[int] = None) -> List[WorkerRunResult]:
        """Loop run_once() with sleep on empty. Bounded by max_iterations for tests.

        Reacts to SIGTERM/SIGINT for graceful shutdown when running as a daemon.
        On exit, marks the agent offline so the control plane can requeue any
        active lease held by this worker. The signal handlers installed for the
        duration of this call are restored before return — the process-wide
        SIGTERM/SIGINT state is not mutated past the worker's lifetime.
        """
        prior_handlers = self._install_signal_handlers()
        results: List[WorkerRunResult] = []
        iterations = 0
        try:
            while not self._stop and (max_iterations is None or iterations < max_iterations):
                iterations += 1
                outcome = self.run_once()
                if outcome.status == "no_task":
                    if max_iterations is None:
                        time.sleep(self.poll_interval_seconds)
                    continue
                results.append(outcome)
        finally:
            self._restore_signal_handlers(prior_handlers)
            self._shutdown()
        return results

    def _install_signal_handlers(self) -> Dict[int, Any]:
        """Install graceful-stop signal handlers; return prior handlers so we
        can restore them. Returns an empty dict if signals can't be installed
        (e.g. when called outside the main thread)."""
        prior: Dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                prior[signum] = signal.signal(signum, lambda *_: self.stop())
            except (ValueError, AttributeError, OSError):
                # signal.signal raises if not in main thread or on platforms
                # without the signal. Tests bound execution via max_iterations.
                pass
        return prior

    def _restore_signal_handlers(self, prior: Dict[int, Any]) -> None:
        for signum, handler in prior.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, AttributeError, OSError):
                pass

    def _shutdown(self) -> None:
        # Best-effort: mark offline so the control plane requeues any active
        # lease tied to this agent. Catch broadly: shutdown must not raise.
        if self.active_lease:
            try:
                self.client.post(
                    f"/leases/{self.active_lease['id']}/release",
                    {"agent_id": self.agent_id},
                )
            except Exception:
                pass
        try:
            self.client.post(
                "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
                {"status": "offline"},
            )
        except Exception:  # noqa: BLE001 — shutdown is a boundary
            pass

    def run_once(self) -> WorkerRunResult:
        self._heartbeat()
        control_result = self._process_agentbus_control()
        if control_result and control_result.get("restart_requested"):
            self.stop()
            return WorkerRunResult(
                status="self_update_restart",
                evidence=control_result,
                error=control_result.get("summary"),
            )
        review_result = self._process_review_nudges()
        if review_result is not None:
            return review_result
        self._observe_policy_once()
        assignment = self._claim_next_for_agent()
        if assignment is None:
            self._observe_log("worker.no_task", level="debug", detail={"agent_id": self.agent_id})
            return WorkerRunResult(status="no_task")

        task = assignment["task"]
        lease = assignment["lease"]
        self.active_lease = lease
        task_id = task["id"]
        self._observe_log(
            "worker.task_claimed",
            subject_type="task",
            subject_id=task_id,
            detail={"lease_id": lease["id"], "agent_id": self.agent_id},
        )
        try:
            self.client.post(
                "/tasks/%s/start?%s"
                % (quote(task_id, safe=""), urlencode({"agent_id": self.agent_id})),
                {},
            )
            task_dir = self._prepare_task_workspace(task, lease)
            started = time.monotonic()
            execution = self._execute_with_lease_renewal(task, lease, task_dir)
            duration_ms = (time.monotonic() - started) * 1000.0
            self._observe_metric(
                "worker.execution.duration_ms",
                duration_ms,
                unit="ms",
                subject_type="task",
                subject_id=task_id,
                detail={"returncode": execution.returncode},
            )
            self._observe_log(
                "worker.execution.completed",
                level="info" if execution.succeeded else "error",
                subject_type="task",
                subject_id=task_id,
                detail={"returncode": execution.returncode, "summary": execution.summary},
            )
            if not self._assignment_is_current(task_id, lease["id"]):
                return self._stale_result(
                    task_id,
                    lease,
                    "assignment no longer current after executor completed",
                    execution=execution,
                )
            evidence = self._record_execution(task_id, task_dir, execution)
            if execution.succeeded:
                submission_problems = self._execution_submission_problems(task_dir, evidence)
                if submission_problems:
                    self._observe_log(
                        "worker.execution.verification_failed",
                        level="error",
                        subject_type="task",
                        subject_id=task_id,
                        detail={
                            "evidence_id": evidence.get("id"),
                            "problems": submission_problems,
                        },
                    )
                    failed_task = self.client.post(
                        "/tasks/%s/transition" % quote(task_id, safe=""),
                        {
                            "target_state": "failed",
                            "actor": self.agent_id,
                            "detail": {
                                "reason": "verification_contract_failed",
                                "evidence_id": evidence.get("id"),
                                "problems": submission_problems,
                            },
                        },
                    )
                    return WorkerRunResult(
                        status="failed",
                        task=failed_task,
                        lease=lease,
                        evidence=evidence,
                        error="; ".join(submission_problems[:4]),
                    )
                reviewed_task = self.client.post(
                    "/tasks/%s/submit-for-review?%s"
                    % (
                        quote(task_id, safe=""),
                        urlencode(
                            {
                                "agent_id": self.agent_id,
                                "advance_default_workflow": "true",
                            }
                        ),
                    ),
                    {},
                )
                return WorkerRunResult(
                    status="submitted_for_review",
                    task=reviewed_task,
                    lease=lease,
                    evidence=evidence,
                )
            failed_task = self.client.post(
                "/tasks/%s/transition" % quote(task_id, safe=""),
                {
                    "target_state": "failed",
                    "actor": self.agent_id,
                    "detail": {
                        "reason": "executor_failed",
                        "returncode": execution.returncode,
                        "evidence_id": evidence["id"],
                    },
                },
            )
            return WorkerRunResult(
                status="failed",
                task=failed_task,
                lease=lease,
                evidence=evidence,
                error=execution.summary,
            )
        except Exception as exc:
            if not self._assignment_is_current(task_id, lease["id"]):
                return self._stale_result(task_id, lease, str(exc))
            self._observe_log(
                "worker.execution.exception",
                level="error",
                subject_type="task",
                subject_id=task_id,
                detail={"error": str(exc)},
            )
            try:
                self.client.post(
                    "/tasks/%s/transition" % quote(task_id, safe=""),
                    {
                        "target_state": "failed",
                        "actor": self.agent_id,
                        "detail": {"reason": "worker_exception", "error": str(exc)},
                    },
                )
            except Exception:
                pass
            raise
        finally:
            self.active_lease = None

    def _assignment_is_current(self, task_id: str, lease_id: str) -> bool:
        try:
            current = self.client.get("/tasks/%s" % quote(task_id, safe=""))
        except MacApiError:
            # Hub unreachable / transient API error: preserve the older
            # behavior and let the concrete operation surface the
            # failure. Narrowed from bare ``except Exception`` (mac-h3d)
            # so TypeError/KeyError/AttributeError from a malformed
            # response or a programming bug bubbles up instead of being
            # silently treated as "still current."
            return True
        current_task = current.get("task", current)
        return (
            current_task.get("owner_agent_id") == self.agent_id
            and current_task.get("lease_id") == lease_id
            and current_task.get("state") in {"claimed", "running"}
        )

    def _stale_result(
        self,
        task_id: str,
        lease: JsonDict,
        reason: str,
        execution: Optional[WorkerExecution] = None,
    ) -> WorkerRunResult:
        detail: JsonDict = {
            "agent_id": self.agent_id,
            "lease_id": lease["id"],
            "reason": reason,
        }
        if execution is not None:
            detail.update(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                }
            )
        self._observe_log(
            "worker.execution.stale_result",
            level="warning",
            subject_type="task",
            subject_id=task_id,
            detail=detail,
        )
        try:
            current = self.client.get("/tasks/%s" % quote(task_id, safe=""))
            current_task: Optional[JsonDict] = current.get("task", current)
        except Exception:
            current_task = None
        return WorkerRunResult(
            status="stale_result",
            task=current_task,
            lease=lease,
            error=reason,
        )

    def _execute_with_lease_renewal(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
    ) -> WorkerExecution:
        stop = threading.Event()
        thread: Optional[threading.Thread] = None
        interval = self.lease_renew_interval_seconds
        if interval is None:
            interval = max(1.0, min(60.0, float(self.lease_seconds) / 2.0))
        if self.lease_seconds > 0 and interval > 0:
            thread = threading.Thread(
                target=self._renew_lease_until_stopped,
                args=(lease["id"], task["id"], stop, interval),
                daemon=True,
            )
            thread.start()
        try:
            return self._call_executor(
                task,
                task_dir,
                {
                    "task_id": task["id"],
                    "lease_id": lease["id"],
                    "metadata": {"execution_kind": "task"},
                },
            )
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1.0)

    def _renew_lease_until_stopped(
        self,
        lease_id: str,
        task_id: str,
        stop: threading.Event,
        interval_seconds: float,
    ) -> None:
        while not stop.wait(interval_seconds):
            try:
                lease = self.client.post(
                    "/leases/%s/renew" % quote(lease_id, safe=""),
                    {"agent_id": self.agent_id, "lease_seconds": self.lease_seconds},
                )
                self._observe_log(
                    "worker.lease_renewed",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"lease_id": lease_id, "expires_at": lease["expires_at"]},
                )
            except Exception as exc:  # noqa: BLE001 - renewal is best-effort telemetry
                self._observe_log(
                    "worker.lease_renew_failed",
                    level="error",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"lease_id": lease_id, "error": str(exc)},
                )

    def _claim_next_for_agent(self) -> Optional[JsonDict]:
        return self.client.post(
            "/agents/%s/claim-next" % quote(self.agent_id, safe=""),
            self._claim_payload(dry_run=False),
        )

    def dry_run_claim(self) -> Optional[JsonDict]:
        self._heartbeat()
        self._observe_policy_once()
        assignment = self.client.post(
            "/agents/%s/claim-next" % quote(self.agent_id, safe=""),
            self._claim_payload(dry_run=True),
        )
        self._observe_log(
            "worker.routing.dry_run_result",
            level="info" if assignment is not None else "debug",
            subject_type="task" if assignment else None,
            subject_id=(assignment.get("task") or {}).get("id") if assignment else None,
            detail={
                "agent_id": self.agent_id,
                "matched": assignment is not None,
                "policy": self._policy_payload(),
            },
        )
        return assignment

    def _process_review_nudges(self) -> Optional[WorkerRunResult]:
        try:
            messages = self.client.post(
                "/agents/%s/messages/deliver?%s"
                % (quote(self.agent_id, safe=""), urlencode({"limit": 20})),
                {},
            )
        except Exception as exc:  # noqa: BLE001 - message polling must not break task polling.
            self._observe_log(
                "worker.review_nudge.poll_failed",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return None

        if not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("message_type") or "") != "nudge":
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("reason") or "") != "produce_review_verdict":
                continue
            return self._handle_review_verdict_nudge(message, payload)
        return None

    def _handle_review_verdict_nudge(self, message: JsonDict, payload: JsonDict) -> WorkerRunResult:
        task_id = str(payload.get("task_id") or "").strip()
        review_id = str(payload.get("review_id") or "").strip()
        executor_evidence_id = str(payload.get("executor_evidence_id") or "").strip()
        if not task_id or not review_id or not executor_evidence_id:
            error = "review verdict nudge missing task_id, review_id, or executor_evidence_id"
            self._observe_log(
                "worker.review_nudge.invalid",
                level="warning",
                detail={"message_id": message.get("id"), "error": error, "payload": payload},
            )
            return WorkerRunResult(status="review_nudge_invalid", error=error)

        try:
            task_detail = self.client.get("/tasks/%s" % quote(task_id, safe=""))
            task_dir = self._prepare_review_workspace(
                task_id,
                review_id,
                executor_evidence_id,
                task_detail if isinstance(task_detail, dict) else {},
                message,
            )
            started = time.monotonic()
            execution = self._call_executor(
                self._review_task_payload(task_dir),
                task_dir,
                {
                    "task_id": task_id,
                    "metadata": {
                        "execution_kind": "review",
                        "review_id": review_id,
                        "executor_evidence_id": executor_evidence_id,
                        "nudge_message_id": message.get("id"),
                    },
                },
            )
            duration_ms = (time.monotonic() - started) * 1000.0
            self._observe_metric(
                "worker.review.duration_ms",
                duration_ms,
                unit="ms",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "returncode": execution.returncode,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                },
            )
            evidence = self._record_review_execution(
                task_id,
                task_dir,
                execution,
                review_id=review_id,
                executor_evidence_id=executor_evidence_id,
                message_id=str(message.get("id") or ""),
            )
            if execution.succeeded:
                self._advance_review_workflow_after_verdict(task_id)
            status = "review_verdict_recorded" if execution.succeeded else "review_verdict_failed"
            self._observe_log(
                "worker.%s" % status,
                level="info" if execution.succeeded else "error",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "evidence_id": evidence.get("id"),
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                },
            )
            return WorkerRunResult(
                status=status,
                task=(task_detail.get("task") if isinstance(task_detail, dict) else None),
                evidence=evidence,
                error=None if execution.succeeded else execution.summary,
            )
        except Exception as exc:
            self._observe_log(
                "worker.review_nudge.exception",
                level="error",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "message_id": message.get("id"),
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "error": str(exc),
                },
            )
            return WorkerRunResult(status="review_verdict_failed", error=str(exc))

    def _advance_review_workflow_after_verdict(self, task_id: str) -> None:
        try:
            self.client.post(
                "/reviews/default/tick?%s"
                % urlencode({"limit": 10, "actor": self.agent_id}),
                {},
            )
        except Exception as exc:  # noqa: BLE001 - verdict evidence is already recorded.
            self._observe_log(
                "worker.review_workflow.advance_failed",
                level="warning",
                subject_type="task",
                subject_id=task_id,
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )

    def _process_agentbus_control(self) -> Optional[JsonDict]:
        if not self.agentbus_control_enabled:
            return None
        try:
            processed = self._load_agentbus_control_state()
            streams = self.client.get(
                "/agentbus/streams?%s"
                % urlencode({"agent_id": self.agent_id, "status": "closed", "limit": 50})
            )
        except Exception as exc:  # noqa: BLE001 - control bus must not break task polling.
            self._observe_log(
                "worker.agentbus.control_poll_failed",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return None

        if not isinstance(streams, list):
            return None
        for stream in reversed(streams):
            if not isinstance(stream, dict):
                continue
            stream_id = str(stream.get("id") or "")
            if not stream_id or stream_id in processed:
                continue
            if stream.get("recipient_agent_id") != self.agent_id:
                continue
            if stream.get("topic") != REPO_UPDATE_TOPIC:
                continue
            if str(stream.get("content_type") or "").split(";", 1)[0] != REPO_UPDATE_CONTENT_TYPE:
                continue

            result = self._handle_repo_update_stream(stream)
            processed.append(stream_id)
            self._save_agentbus_control_state(processed)
            self._publish_repo_update_result(stream, result)
            if result.get("restart_requested"):
                return result
        return None

    def _handle_repo_update_stream(self, stream: JsonDict) -> JsonDict:
        stream_id = str(stream.get("id") or "")
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(stream_id, safe=""),
                urlencode({"agent_id": self.agent_id, "after_sequence": 0, "limit": 10}),
            )
        )
        payload: Any = None
        if isinstance(chunks, list) and chunks:
            payload = chunks[-1].get("payload") if isinstance(chunks[-1], dict) else None
        try:
            result = self._execute_repo_update(payload, stream_id)
        except Exception as exc:  # noqa: BLE001 - malformed control messages should report failure.
            result = self._repo_update_result(
                stream_id,
                "error",
                "repo update handler failed: %s" % exc,
                {},
            )
        self._observe_log(
            "worker.agentbus.repo_update.%s" % result["status"],
            level="info" if result["status"] in {"updated", "no_update", "skipped"} else "error",
            detail=result,
        )
        return result

    def _execute_repo_update(self, payload: Any, stream_id: str) -> JsonDict:
        request: JsonDict = payload if isinstance(payload, dict) else {}
        if request.get("schema") not in {None, "", REPO_UPDATE_SCHEMA}:
            return self._repo_update_result(
                stream_id,
                "error",
                "unsupported repo update schema: %s" % request.get("schema"),
                request,
            )

        repo = self.self_update_repo.expanduser()
        requested_repo = str(request.get("repo_path") or "").strip()
        if requested_repo:
            try:
                if Path(requested_repo).expanduser().resolve() != repo.resolve():
                    return self._repo_update_result(
                        stream_id,
                        "error",
                        "repo_path does not match this listener's configured update repo",
                        request,
                        repo_path=str(repo),
                    )
            except OSError as exc:
                return self._repo_update_result(
                    stream_id,
                    "error",
                    "could not resolve repo_path: %s" % exc,
                    request,
                    repo_path=str(repo),
                )

        remote = str(request.get("remote") or "origin").strip()
        branch = str(request.get("branch") or "").strip()
        restart = bool(request.get("restart", True))
        if not _safe_git_ref(remote):
            return self._repo_update_result(
                stream_id,
                "error",
                "invalid git remote name",
                request,
                repo_path=str(repo),
            )
        if branch and not _safe_git_ref(branch):
            return self._repo_update_result(
                stream_id,
                "error",
                "invalid git branch/ref name",
                request,
                repo_path=str(repo),
            )
        if not repo.exists():
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo does not exist",
                request,
                repo_path=str(repo),
            )

        inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo is not a git worktree",
                request,
                repo_path=str(repo),
                stderr=inside.stderr,
            )

        dirty = _run_git(repo, ["status", "--porcelain"])
        if dirty.returncode != 0:
            return self._repo_update_result(
                stream_id,
                "error",
                "could not inspect git status",
                request,
                repo_path=str(repo),
                stderr=dirty.stderr,
            )
        if dirty.stdout.strip():
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo has local modifications",
                request,
                repo_path=str(repo),
            )

        before = _run_git(repo, ["rev-parse", "HEAD"])
        before_sha = before.stdout.strip() if before.returncode == 0 else ""
        pull_args = ["pull", "--ff-only"]
        if branch:
            pull_args.extend([remote, branch])
        pulled = _run_git(repo, pull_args)
        if pulled.returncode != 0:
            return self._repo_update_result(
                stream_id,
                "error",
                "git pull --ff-only failed",
                request,
                repo_path=str(repo),
                before_sha=before_sha,
                stdout=pulled.stdout,
                stderr=pulled.stderr,
            )

        after = _run_git(repo, ["rev-parse", "HEAD"])
        after_sha = after.stdout.strip() if after.returncode == 0 else ""
        updated = bool(before_sha and after_sha and before_sha != after_sha)
        return self._repo_update_result(
            stream_id,
            "updated" if updated else "no_update",
            "repo updated; restart requested" if updated and restart else "repo already current",
            request,
            repo_path=str(repo),
            before_sha=before_sha,
            after_sha=after_sha,
            stdout=pulled.stdout,
            stderr=pulled.stderr,
            restart_requested=updated and restart,
        )

    def _repo_update_result(
        self,
        stream_id: str,
        status: str,
        summary: str,
        request: JsonDict,
        **extra: Any,
    ) -> JsonDict:
        result: JsonDict = {
            "schema": REPO_UPDATE_RESULT_SCHEMA,
            "status": status,
            "summary": summary,
            "agent_id": self.agent_id,
            "stream_id": stream_id,
            "request_id": request.get("request_id"),
            "restart_requested": bool(extra.pop("restart_requested", False)),
        }
        for key, value in extra.items():
            if isinstance(value, str):
                result[key] = value[:4000]
            else:
                result[key] = value
        return result

    def _publish_repo_update_result(self, stream: JsonDict, result: JsonDict) -> None:
        sender = str(stream.get("sender_agent_id") or "")
        if not sender:
            return
        try:
            self.client.post(
                "/agentbus",
                {
                    "sender_agent_id": self.agent_id,
                    "recipient_agent_id": sender,
                    "content_type": REPO_UPDATE_RESULT_CONTENT_TYPE,
                    "topic": REPO_UPDATE_RESULT_TOPIC,
                    "payload": result,
                },
            )
        except Exception as exc:  # noqa: BLE001 - result publishing is best-effort.
            self._observe_log(
                "worker.agentbus.repo_update_result_failed",
                level="warning",
                detail={"stream_id": stream.get("id"), "error": str(exc)},
            )

    def _load_agentbus_control_state(self) -> List[str]:
        try:
            loaded = json.loads(self.agentbus_control_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []
        values = loaded.get("processed_stream_ids") if isinstance(loaded, dict) else []
        if not isinstance(values, list):
            return []
        return [str(value) for value in values if str(value)]

    def _save_agentbus_control_state(self, processed_stream_ids: List[str]) -> None:
        try:
            self.agentbus_control_state_path.parent.mkdir(parents=True, exist_ok=True)
            deduped = list(dict.fromkeys(processed_stream_ids))[-500:]
            self.agentbus_control_state_path.write_text(
                json.dumps({"processed_stream_ids": deduped}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - state loss should not break task polling.
            self._observe_log(
                "worker.agentbus.control_state_write_failed",
                level="warning",
                detail={"path": str(self.agentbus_control_state_path), "error": str(exc)},
            )

    def _claim_payload(self, dry_run: bool) -> JsonDict:
        return {
            "lease_seconds": self.lease_seconds,
            "allowed_projects": self.allowed_projects,
            "required_metadata": self.required_metadata,
            "require_canary": self.require_canary,
            "dry_run": dry_run,
        }

    def _policy_payload(self) -> JsonDict:
        return {
            "allowed_projects": self.allowed_projects,
            "required_metadata": self.required_metadata,
            "require_canary": self.require_canary,
        }

    def _observe_policy_once(self) -> None:
        if self._declared_policy:
            return
        self._declared_policy = True
        self._observe_log(
            "worker.routing.policy",
            detail={"agent_id": self.agent_id, "policy": self._policy_payload()},
        )

    def _prepare_task_workspace(self, task: JsonDict, lease: JsonDict) -> Path:
        task_dir = self.workspace / _safe_path_component(task["id"])
        task_dir.mkdir(parents=True, exist_ok=True)
        repository_context = self._prepare_repository_worktree(task, lease, task_dir)
        if repository_context is not None:
            metadata = task.setdefault("metadata", {})
            if isinstance(metadata, dict):
                runtime = metadata.setdefault("runtime", {})
                if isinstance(runtime, dict):
                    runtime.update(repository_context)
            (task_dir / "repository-worktree.json").write_text(
                json.dumps(repository_context, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        (task_dir / "task.json").write_text(
            json.dumps({"task": task, "lease": lease}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

    def _prepare_repository_worktree(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
    ) -> Optional[JsonDict]:
        origin = _repository_task_origin(task)
        if origin is None:
            return None
        source = self._resolve_repository_source_path(origin)
        if not source.exists():
            raise RuntimeError(
                "repository source path does not exist: %s; tried %s"
                % (
                    origin.get("repository_path"),
                    ", ".join(str(candidate) for candidate in _repository_source_candidates(origin, self.self_update_repo)),
                )
            )

        top_level = _run_git(source, ["rev-parse", "--show-toplevel"])
        if top_level.returncode != 0 or not top_level.stdout.strip():
            raise RuntimeError(
                "repository source path is not a git worktree: %s" % source
            )
        source_root = Path(top_level.stdout.strip()).resolve()
        inside = _run_git(source_root, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeError(
                "repository source path is not a git worktree: %s" % source_root
            )

        dirty = _run_git(source_root, ["status", "--porcelain"])
        if dirty.returncode != 0:
            raise RuntimeError(
                "could not inspect repository source status: %s"
                % ((dirty.stderr or dirty.stdout or "").strip() or source_root)
            )
        dirty_paths = [line.strip() for line in dirty.stdout.splitlines() if line.strip()]
        if dirty_paths:
            self._observe_log(
                "worker.repository.source_dirty",
                level="warning",
                subject_type="task",
                subject_id=str(task.get("id") or ""),
                detail={
                    "repository_path": str(source_root),
                    "dirty_paths": dirty_paths[:50],
                    "dirty_path_count": len(dirty_paths),
                },
            )
            raise RuntimeError(
                "repository source checkout is dirty; refusing to run task outside an isolated clean base: %s"
                % source_root
            )

        head = _run_git(source_root, ["rev-parse", "HEAD"])
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(
                "could not resolve repository source HEAD: %s"
                % ((head.stderr or head.stdout or "").strip() or source_root)
            )
        base_sha = head.stdout.strip()
        branch = _task_worktree_branch(self.agent_id, str(task.get("id") or ""), str(lease.get("id") or ""))
        worktree_dir = task_dir / ("repo-" + _safe_path_component(str(lease.get("id") or "lease")))
        if worktree_dir.exists():
            existing_head = _run_git(worktree_dir, ["rev-parse", "HEAD"])
            if existing_head.returncode == 0 and existing_head.stdout.strip():
                raise RuntimeError(
                    "repository task worktree already exists for this lease: %s" % worktree_dir
                )
            shutil.rmtree(worktree_dir)

        add = _run_git(
            source_root,
            ["worktree", "add", "-b", branch, str(worktree_dir), base_sha],
        )
        if add.returncode != 0:
            raise RuntimeError(
                "could not create repository task worktree: %s"
                % ((add.stderr or add.stdout or "").strip() or worktree_dir)
            )
        remote = _run_git(source_root, ["remote", "get-url", "origin"])
        context: JsonDict = {
            "schema": "mac.repository_task_worktree.v1",
            "checkout_policy": "task_owned_git_worktree",
            "repository_declared_path": str(origin.get("repository_path") or ""),
            "repository_source_path": str(source_root),
            "repository_worktree": str(worktree_dir),
            "repository_branch": branch,
            "repository_base_sha": base_sha,
            "repository_origin_remote": remote.stdout.strip() if remote.returncode == 0 else "",
        }
        self._observe_log(
            "worker.repository.worktree_prepared",
            subject_type="task",
            subject_id=str(task.get("id") or ""),
            detail=context,
        )
        return context

    def _resolve_repository_source_path(self, origin: JsonDict) -> Path:
        for candidate in _repository_source_candidates(origin, self.self_update_repo):
            if candidate.exists():
                return candidate
        return Path(str(origin.get("repository_path") or "")).expanduser()

    def _prepare_review_workspace(
        self,
        task_id: str,
        review_id: str,
        executor_evidence_id: str,
        task_detail: JsonDict,
        message: JsonDict,
    ) -> Path:
        task_dir = self.workspace / "_reviews" / _safe_path_component(review_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        task = {
            "id": "review_%s" % review_id,
            "title": "Review task %s" % task_id,
            "description": (
                "Review the executor evidence for task %s and write a signed "
                "review_verdict manifest." % task_id
            ),
            "required_capabilities": ["review"],
            "metadata": {
                "review_context": {
                    "task_id": task_id,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "nudge_message_id": message.get("id"),
                    "task_detail": task_detail,
                }
            },
        }
        (task_dir / "task.json").write_text(
            json.dumps({"task": task}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

    def _review_task_payload(self, task_dir: Path) -> JsonDict:
        loaded = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        task = loaded.get("task", loaded)
        return task if isinstance(task, dict) else loaded

    def _record_execution(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
    ) -> JsonDict:
        (task_dir / "stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (task_dir / "stderr.txt").write_text(execution.stderr, encoding="utf-8")
        result_path = task_dir / "worker-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                    "metadata": self._execution_metadata(task_dir, execution),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        metadata = self._execution_metadata(task_dir, execution)
        return self.client.post(
            "/tasks/%s/evidence" % quote(task_id, safe=""),
            {
                "kind": "log",
                "uri": result_path.resolve().as_uri(),
                "summary": execution.summary,
                "created_by": self.agent_id,
                "metadata": {
                    "returncode": execution.returncode,
                    "stdout": (task_dir / "stdout.txt").resolve().as_uri(),
                    "stderr": (task_dir / "stderr.txt").resolve().as_uri(),
                    **metadata,
                },
            },
        )

    def _execution_submission_problems(self, task_dir: Path, evidence: JsonDict) -> List[str]:
        problems: List[str] = []
        metadata = evidence.get("metadata") if isinstance(evidence, dict) else None
        manifest = metadata.get("verification") if isinstance(metadata, dict) else None
        if not isinstance(manifest, dict):
            return ["evidence metadata lacks verification manifest"]
        if str(manifest.get("schema") or "").strip() != VERIFICATION_SCHEMA:
            problems.append("verification.schema must be %s" % VERIFICATION_SCHEMA)
        if str(manifest.get("status") or "").strip().lower() != "complete":
            problems.append('verification.status must be "complete"')
        evidence_type = str(manifest.get("evidence_type") or "").strip().lower()
        if not evidence_type:
            problems.append("verification.evidence_type is required")
        if not str(manifest.get("signed_by") or "").strip() or not str(manifest.get("signature") or "").strip():
            problems.append("verification.signed_by and verification.signature are required")
        if evidence_type:
            problems.extend(_worker_verification_contract_problems(manifest, evidence_type))

        repository_context = _load_repository_context(task_dir)
        if repository_context:
            worktree = Path(str(repository_context.get("repository_worktree") or ""))
            if not worktree.exists():
                problems.append("repository worktree is missing: %s" % worktree)
            else:
                dirty = _run_git(worktree, ["status", "--porcelain"])
                if dirty.returncode != 0:
                    problems.append(
                        "could not inspect repository worktree status: %s"
                        % ((dirty.stderr or dirty.stdout or "").strip() or worktree)
                    )
                elif dirty.stdout.strip():
                    problems.append("repository worktree has uncommitted changes")
                head = _run_git(worktree, ["rev-parse", "HEAD"])
                repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
                manifest_head = str(repo.get("head_sha") or "").strip() if isinstance(repo, dict) else ""
                if head.returncode == 0 and manifest_head and head.stdout.strip() != manifest_head:
                    problems.append("verification.repo.head_sha does not match worktree HEAD")
        return problems

    def _record_review_execution(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        *,
        review_id: str,
        executor_evidence_id: str,
        message_id: str,
    ) -> JsonDict:
        (task_dir / "stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (task_dir / "stderr.txt").write_text(execution.stderr, encoding="utf-8")
        result_path = task_dir / "review-result.json"
        metadata = self._execution_metadata(task_dir, execution)
        result_path.write_text(
            json.dumps(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "metadata": metadata,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return self.client.post(
            "/tasks/%s/evidence" % quote(task_id, safe=""),
            {
                "kind": "review",
                "uri": result_path.resolve().as_uri(),
                "summary": execution.summary,
                "created_by": self.agent_id,
                "metadata": {
                    "returncode": execution.returncode,
                    "stdout": (task_dir / "stdout.txt").resolve().as_uri(),
                    "stderr": (task_dir / "stderr.txt").resolve().as_uri(),
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "nudge_message_id": message_id,
                    **metadata,
                },
            },
        )

    def _execution_metadata(self, task_dir: Path, execution: WorkerExecution) -> JsonDict:
        metadata = dict(execution.metadata)
        manifest = metadata.get("verification") or self._load_verification_manifest(task_dir)
        metadata["verification"] = self._sign_verification_manifest(manifest)
        metadata.setdefault(
            "workspace_outputs",
            {
                "stdout_sha256": _sha256_file(task_dir / "stdout.txt"),
                "stderr_sha256": _sha256_file(task_dir / "stderr.txt"),
            },
        )
        return metadata

    def _sign_verification_manifest(self, manifest: JsonDict) -> JsonDict:
        """Stamp ``signed_by`` + ``signature`` onto the manifest if an
        attestation key is configured (mac-ng2). Without a key the
        manifest is returned unmodified — the default-review workflow
        will then refuse the evidence as ``manifest_not_signed``,
        which is the correct outcome for an unkeyed worker."""
        if not self.attestation_key or not isinstance(manifest, dict):
            return manifest
        from mac.services import sign_verification_manifest

        signed = dict(manifest)
        signed["signed_by"] = self.agent_id
        signed["signature"] = sign_verification_manifest(self.attestation_key, signed)
        return signed

    def _load_verification_manifest(self, task_dir: Path) -> JsonDict:
        manifest_path = task_dir / "mac-evidence.json"
        if not manifest_path.exists():
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "missing",
                "problems": ["mac-evidence.json was not produced by the executor"],
            }
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - malformed evidence should be captured, not crash reporting
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "invalid",
                "problems": ["could not parse mac-evidence.json: %s" % exc],
                "uri": manifest_path.resolve().as_uri(),
            }
        if not isinstance(loaded, dict):
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "invalid",
                "problems": ["mac-evidence.json must contain a JSON object"],
                "uri": manifest_path.resolve().as_uri(),
            }
        loaded.setdefault("schema", "mac.worker_evidence.v1")
        loaded.setdefault("uri", manifest_path.resolve().as_uri())
        loaded.setdefault("sha256", _sha256_file(manifest_path))
        return loaded

    def _call_executor(
        self,
        task: JsonDict,
        task_dir: Path,
        audit_context: JsonDict,
    ) -> WorkerExecution:
        if isinstance(self.executor, SubprocessExecutor):
            prior_context = self.executor.audit_context
            self.executor.audit_context = audit_context
            try:
                return self.executor(task, task_dir)
            finally:
                self.executor.audit_context = prior_context
        return self.executor(task, task_dir)

    def _record_command_audit(self, record: JsonDict) -> None:
        payload = {
            "command_id": record.get("command_id"),
            "phase": record.get("phase"),
            "argv": record.get("argv") or [],
            "cwd": record.get("cwd") or "",
            "task_id": record.get("task_id"),
            "lease_id": record.get("lease_id"),
            "started_at": record.get("started_at"),
            "completed_at": record.get("completed_at"),
            "duration_ms": record.get("duration_ms"),
            "returncode": record.get("returncode"),
            "stdout_sha256": record.get("stdout_sha256"),
            "stderr_sha256": record.get("stderr_sha256"),
            "stdout_bytes": record.get("stdout_bytes"),
            "stderr_bytes": record.get("stderr_bytes"),
            "metadata": record.get("metadata") or {},
        }
        try:
            self.client.post(
                "/agents/%s/command-audit" % quote(self.agent_id, safe=""),
                payload,
            )
        except Exception:
            pass

    def _heartbeat(self) -> None:
        payload: JsonDict = {"status": "idle"}
        # Declare the build the agent is running. Send the digest at most once
        # per process; subsequent heartbeats are pure liveness pings.
        if self.running_digest and not self._declared_digest:
            payload["running_digest"] = self.running_digest
        self.client.post(
            "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
            payload,
        )
        if self.running_digest and not self._declared_digest:
            self._declared_digest = True

    def _observe_metric(
        self,
        name: str,
        value: float,
        unit: str = "",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        self._post_observation(
            "/observability/metrics",
            {
                "name": name,
                "value": value,
                "unit": unit,
                "layer": "worker",
                "source": self.agent_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "detail": detail or {},
            },
        )

    def _observe_log(
        self,
        name: str,
        level: str = "info",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        self._post_observation(
            "/observability/logs",
            {
                "name": name,
                "level": level,
                "layer": "worker",
                "source": self.agent_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "detail": detail or {},
            },
        )

    def _post_observation(self, path: str, payload: JsonDict) -> None:
        try:
            self.client.post(path, payload)
        except Exception:
            pass


def _summary_from_output(returncode: int, stdout: str, stderr: str) -> str:
    stream = stdout if stdout.strip() else stderr
    first_line = next((line.strip() for line in stream.splitlines() if line.strip()), "")
    if first_line:
        return first_line[:500]
    return "executor completed" if returncode == 0 else "executor failed with returncode %d" % returncode


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _command_audit_id() -> str:
    seed = "%s:%s:%s" % (time.time_ns(), os.getpid(), threading.get_ident())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _sha256_text(value: str) -> str:
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_json_object(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, dict) else {}


def _coerce_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _audit_safe_argv(argv: List[str]) -> List[str]:
    safe: List[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            safe.append(_redacted_arg(arg))
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--key", "--secret", "--password"}:
            safe.append(arg)
            redact_next = True
            continue
        if any(marker in lowered for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")):
            safe.append(_redacted_arg(arg))
            continue
        if len(arg) > 512:
            safe.append("<truncated:%s:chars=%d>" % (_sha256_text(arg), len(arg)))
            continue
        safe.append(arg)
    return safe


def _redacted_arg(value: str) -> str:
    return "<redacted:%s:chars=%d>" % (_sha256_text(value), len(value))


def _safe_path_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return ""
    return "sha256:%s" % digest.hexdigest()


def _default_self_update_repo() -> Path:
    configured = os.environ.get("MAC_SELF_UPDATE_REPO")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2]


def _repository_task_origin(task: JsonDict) -> Optional[JsonDict]:
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return None
    origin = metadata.get("origin")
    if not isinstance(origin, dict):
        return None
    repository_path = str(origin.get("repository_path") or "").strip()
    if not repository_path:
        return None

    # Dirty-source remediation tasks are the one explicit exception: their
    # purpose is to repair the registered checkout itself.
    remediation = metadata.get("remediation")
    if isinstance(remediation, dict) and remediation.get("type") == "beads_source_refresh":
        return None
    if origin.get("type") == "beads_source_remediation":
        return None

    contract = origin.get("repository_contract")
    execution_contract = metadata.get("execution_contract")
    if isinstance(execution_contract, dict) and execution_contract.get("type") == "repository":
        return dict(origin)
    if isinstance(contract, dict) and contract.get("schema"):
        return dict(origin)
    if str(origin.get("type") or "") in {"beads", "direct_task"}:
        return dict(origin)
    return None


def _repository_source_candidates(origin: JsonDict, self_update_repo: Path) -> List[Path]:
    candidates: List[Path] = []
    raw = str(origin.get("repository_path") or "").strip()
    if raw:
        declared = Path(raw).expanduser()
        candidates.append(declared)
        parts = declared.parts
        if ".mac" in parts:
            idx = parts.index(".mac")
            suffix = Path(*parts[idx + 1 :]) if idx + 1 < len(parts) else Path()
            candidates.append(Path.home() / ".mac" / suffix)

    repository_name = str(origin.get("repository_name") or "").strip()
    if repository_name:
        candidates.append(Path.home() / ".mac" / "src" / _safe_path_component(repository_name))

    source = str(origin.get("source") or "").strip()
    contract = origin.get("repository_contract")
    project = str(contract.get("project") or "").strip() if isinstance(contract, dict) else ""
    if repository_name == "mac" or source == "repo-beads-mac":
        candidates.insert(0, self_update_repo.expanduser())
    elif project == "repo-beads-mac":
        candidates.append(self_update_repo.expanduser())

    seen = set()
    unique: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _task_worktree_branch(agent_id: str, task_id: str, lease_id: str) -> str:
    agent = _safe_path_component(agent_id).strip("._-/") or "agent"
    task = _safe_path_component(task_id).strip("._-/") or "task"
    lease = _safe_path_component(lease_id).strip("._-/") or "lease"
    branch = "mac/%s/%s-%s" % (agent[:32], task[:48], lease[:24])
    return branch[:127].rstrip("./-") or "mac/agent/task"


def _load_repository_context(task_dir: Path) -> JsonDict:
    path = task_dir / "repository-worktree.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _repository_context_env(context: JsonDict) -> Dict[str, str]:
    mapping = {
        "MAC_TASK_REPO_WORKTREE": context.get("repository_worktree"),
        "MAC_TASK_REPO_SOURCE": context.get("repository_source_path"),
        "MAC_TASK_REPO_BRANCH": context.get("repository_branch"),
        "MAC_TASK_REPO_BASE_SHA": context.get("repository_base_sha"),
        "MAC_TASK_REPO_REMOTE": context.get("repository_origin_remote"),
    }
    return {key: str(value) for key, value in mapping.items() if value not in {None, ""}}


def _repository_context_audit_metadata(context: JsonDict) -> JsonDict:
    if not context:
        return {}
    return {
        "repository_checkout_policy": context.get("checkout_policy"),
        "repository_worktree": context.get("repository_worktree"),
        "repository_source_path": context.get("repository_source_path"),
        "repository_branch": context.get("repository_branch"),
        "repository_base_sha": context.get("repository_base_sha"),
    }


def _safe_git_ref(value: str) -> bool:
    return bool(value and not value.startswith("-") and re.match(SAFE_GIT_REF_RE, value))


def _manifest_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _worker_verification_contract_problems(manifest: JsonDict, evidence_type: str) -> List[str]:
    if evidence_type == "repo_change":
        return _worker_repo_verification_problems(manifest, require_tests=True)
    if evidence_type == "documentation":
        return _worker_repo_verification_problems(manifest, require_tests=False)
    if evidence_type == "deployment":
        problems = _worker_require_pushed_repo_anchor(manifest)
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("deployment evidence requires at least one passing check")
        if not (
            _manifest_list(manifest.get("targets"))
            or _manifest_list(manifest.get("services"))
            or _manifest_list(manifest.get("artifacts"))
        ):
            problems.append("deployment evidence requires targets, services, or artifacts")
        return problems
    if evidence_type in {"test", "artifact"}:
        problems = _worker_require_pushed_repo_anchor(manifest)
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("%s evidence requires at least one passing check or test" % evidence_type)
        if evidence_type == "artifact" and not _manifest_list(manifest.get("artifacts")):
            problems.append("artifact evidence requires artifacts")
        return problems
    if evidence_type == "no_change":
        problems = _worker_require_pushed_repo_anchor(manifest)
        if not str(manifest.get("reason") or manifest.get("no_change_reason") or "").strip():
            problems.append("no_change evidence requires a reason")
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("no_change evidence requires at least one passing check")
        return problems
    if evidence_type == "review_verdict":
        return []
    return ["unsupported verification.evidence_type: %s" % evidence_type]


def _worker_repo_verification_problems(manifest: JsonDict, require_tests: bool) -> List[str]:
    problems = _worker_require_pushed_repo_anchor(manifest)
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    files_changed = _manifest_list(repo.get("files_changed")) if isinstance(repo, dict) else []
    if not files_changed:
        problems.append("repo evidence requires changed files")
    if require_tests and _worker_passed_verification_check_count(manifest) < 1:
        problems.append("repo code evidence requires at least one passing test/check")
    return problems


def _worker_require_pushed_repo_anchor(manifest: JsonDict) -> List[str]:
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return ["repo evidence requires verification.repo object"]
    problems: List[str] = []
    head_sha = str(repo.get("head_sha") or "").strip()
    if not GIT_SHA_RE.match(head_sha):
        problems.append("repo.head_sha must be a git SHA")
    dirty = repo.get("dirty")
    if dirty not in {False, "false", "False", 0, "0"}:
        problems.append("repo evidence must declare dirty=false")
    pushed = repo.get("pushed") is True or str(repo.get("pushed") or "").lower() == "true"
    remote_ref = str(repo.get("remote_ref") or "").strip()
    pr_url = str(repo.get("pr_url") or "").strip()
    if not (pushed and remote_ref) and not pr_url:
        problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
    return problems


def _worker_passed_verification_check_count(manifest: JsonDict) -> int:
    count = 0
    for item in _manifest_list(manifest.get("tests")):
        if _worker_verification_item_passed(item):
            count += 1
    for item in _manifest_list(manifest.get("checks")):
        if _worker_verification_item_passed(item):
            count += 1
    return count


PASSING_VERIFICATION_WORDS = {"pass", "passed", "success", "successful", "succeeded", "ok"}


def _worker_int_value(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _worker_verification_item_passed(item: Any) -> bool:
    if isinstance(item, list):
        return any(_worker_verification_item_passed(nested) for nested in item)
    if not isinstance(item, dict):
        return False
    if "returncode" in item:
        return _worker_int_value(item["returncode"]) == 0
    failed = _worker_int_value(item.get("failed"))
    if failed is not None and failed > 0:
        return False
    for key in ("status", "result", "outcome"):
        if str(item.get(key) or "").strip().lower() in PASSING_VERIFICATION_WORDS:
            return True
    for key in ("passed", "success", "succeeded", "ok", "satisfied"):
        value = item.get(key)
        if value is True:
            return True
        number = _worker_int_value(value)
        if number is not None and number > 0 and failed == 0:
            return True
    bool_values = [value for value in item.values() if isinstance(value, bool)]
    if bool_values and len(bool_values) == len(item) and all(bool_values):
        return True
    return any(
        _worker_verification_item_passed(nested)
        for nested in item.values()
        if isinstance(nested, (dict, list))
    )


def _run_git(repo: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    try:
        timeout = float(os.environ.get("MAC_SELF_UPDATE_GIT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _stable_id(prefix: str, value: str) -> str:
    return "%s_%s" % (prefix, _safe_path_component(value.lower()).strip("_") or "default")


def _csv_arg(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_arg(value: Optional[str]) -> JsonDict:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise MacApiError("resources must be a JSON object")
    return loaded


def _read_env_value(path: Path, key: str) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key and value.strip():
                return value.strip()
    except FileNotFoundError:
        return None
    return None


def _write_env_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    replaced = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: List[str] = []
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            name, _old = line.split("=", 1)
            if name.strip() == key:
                updated.append("%s=%s" % (key, value))
                replaced = True
                continue
        updated.append(line)
    if not replaced:
        updated.append("%s=%s" % (key, value))
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mac worker harness")
    parser.add_argument("--url", default=os.environ.get("MAC_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("MAC_TOKEN"))
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID"))
    parser.add_argument(
        "--register",
        action="store_true",
        help="register or refresh this host's machine and agent rows before running",
    )
    parser.add_argument("--machine-id", default=os.environ.get("MAC_MACHINE_ID"))
    parser.add_argument("--hostname", default=os.environ.get("MAC_HOSTNAME"))
    parser.add_argument("--agent-name", default=os.environ.get("MAC_AGENT_NAME"))
    parser.add_argument(
        "--capabilities",
        default=os.environ.get("MAC_WORKER_CAPABILITIES", ""),
        help="comma-separated capabilities to advertise when --register is used",
    )
    parser.add_argument(
        "--resources",
        default=os.environ.get("MAC_WORKER_RESOURCES"),
        help="JSON resource/capacity object to advertise when --register is used",
    )
    parser.add_argument("--workspace", default=".mac-agent-workspaces")
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--timeout", type=float)
    parser.add_argument(
        "--allowed-projects",
        default=os.environ.get("MAC_WORKER_ALLOWED_PROJECTS", ""),
        help="comma-separated projects this worker may claim",
    )
    parser.add_argument(
        "--required-metadata",
        default=os.environ.get("MAC_WORKER_REQUIRED_METADATA"),
        help="JSON object of top-level task metadata key/value pairs required before claiming",
    )
    parser.add_argument(
        "--require-canary",
        action="store_true",
        default=_env_bool("MAC_WORKER_REQUIRE_CANARY", False),
        help="claim only tasks with metadata.canary, metadata.mac_canary, or metadata.worker_canary true",
    )
    parser.add_argument(
        "--running-digest",
        help="runtime_environments.digest the worker is running (declared at first heartbeat)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="run forever (poll for tasks). Default is run_once and exit.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        help="cap iterations in --loop mode (mostly for tests)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds to sleep between polls when no task is available",
    )
    parser.add_argument(
        "--self-update-repo",
        default=os.environ.get("MAC_SELF_UPDATE_REPO"),
        help="git worktree this worker may pull for AgentBus repo-update control messages",
    )
    parser.add_argument(
        "--disable-agentbus-control",
        action="store_true",
        help="disable AgentBus control-message polling before task claims",
    )
    parser.add_argument(
        "--attestation-key-env",
        default=os.environ.get("MAC_ATTESTATION_KEY_ENV"),
        help="env file where a first-registration attestation key should be persisted",
    )
    parser.add_argument(
        "--rotate-missing-attestation-key",
        action="store_true",
        default=_env_bool("MAC_ROTATE_MISSING_ATTESTATION_KEY", False),
        help="rotate and persist this agent's attestation key when no local key is configured",
    )
    parser.add_argument(
        "--heartbeat-only",
        action="store_true",
        help="register/heartbeat once and exit without claiming tasks",
    )
    parser.add_argument(
        "--dry-run-claim",
        action="store_true",
        help="register/heartbeat and ask the hub what this worker would claim without creating a lease",
    )
    parser.add_argument(
        "--executor",
        nargs=argparse.REMAINDER,
        default=None,
        help="executor argv; pass this flag last, followed by the command and arguments",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    client = MacApiClient(args.url, token=args.token)
    agent_id = args.agent_id
    try:
        registered: Optional[JsonDict] = None
        attestation_key = os.environ.get("MAC_ATTESTATION_KEY")
        attestation_env_path = Path(args.attestation_key_env).expanduser() if args.attestation_key_env else None
        if not attestation_key and attestation_env_path is not None:
            attestation_key = _read_env_value(attestation_env_path, "MAC_ATTESTATION_KEY")
        if args.register:
            registered = register_worker(
                client,
                hostname=args.hostname,
                agent_name=args.agent_name,
                capabilities=_csv_arg(args.capabilities),
                resources=_json_arg(args.resources),
                machine_id=args.machine_id,
                agent_id=args.agent_id,
            )
            agent_id = registered["id"]
            if registered.get("attestation_key"):
                attestation_key = str(registered["attestation_key"])
                os.environ["MAC_ATTESTATION_KEY"] = attestation_key
                if attestation_env_path is not None:
                    _write_env_value(attestation_env_path, "MAC_ATTESTATION_KEY", attestation_key)
        if not agent_id:
            raise MacApiError("--agent-id or --register is required")
        if not attestation_key and args.rotate_missing_attestation_key:
            rotated = client.post(
                "/agents/%s/attestation-key/rotate" % quote(agent_id, safe=""),
                {},
            )
            attestation_key = str(rotated["attestation_key"])
            os.environ["MAC_ATTESTATION_KEY"] = attestation_key
            if attestation_env_path is not None:
                _write_env_value(attestation_env_path, "MAC_ATTESTATION_KEY", attestation_key)
        if args.heartbeat_only:
            heartbeat = client.post(
                "/agents/%s/heartbeat" % quote(agent_id, safe=""),
                {"status": "idle", "running_digest": args.running_digest},
            )
            print(
                json.dumps(
                    {"status": "heartbeat", "agent": heartbeat, "registered": registered},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        required_metadata = _json_arg(args.required_metadata)
        allowed_projects = _csv_arg(args.allowed_projects)
        executor_argv = list(args.executor or [])
        if executor_argv and executor_argv[0] == "--":
            executor_argv = executor_argv[1:]
        if args.dry_run_claim:
            worker = MacWorker(
                client,
                agent_id,
                Path(args.workspace),
                SubprocessExecutor(["true"]),
                lease_seconds=args.lease_seconds,
                running_digest=args.running_digest,
                poll_interval_seconds=args.poll_interval,
                allowed_projects=allowed_projects,
                required_metadata=required_metadata,
                require_canary=args.require_canary,
                agentbus_control_enabled=not args.disable_agentbus_control,
                self_update_repo=Path(args.self_update_repo).expanduser()
                if args.self_update_repo
                else None,
                attestation_key=attestation_key,
            )
            print(json.dumps({"status": "dry_run", "assignment": worker.dry_run_claim()}, indent=2, sort_keys=True))
            return 0
        if not executor_argv:
            raise MacApiError("--executor is required unless --heartbeat-only is set")
        worker = MacWorker(
            client,
            agent_id,
            Path(args.workspace),
            SubprocessExecutor(executor_argv, timeout=args.timeout),
            lease_seconds=args.lease_seconds,
            running_digest=args.running_digest,
            poll_interval_seconds=args.poll_interval,
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=args.require_canary,
            agentbus_control_enabled=not args.disable_agentbus_control,
            self_update_repo=Path(args.self_update_repo).expanduser()
            if args.self_update_repo
            else None,
            attestation_key=attestation_key,
        )
        if args.loop:
            results = worker.run_forever(max_iterations=args.max_iterations)
            print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
            if any(result.status == "self_update_restart" for result in results):
                return 75
        else:
            result = worker.run_once()
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            if result.status == "self_update_restart":
                return 75
    except MacApiError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
