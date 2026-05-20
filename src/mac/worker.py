from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
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
SAFE_GIT_REF_RE = r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$"


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

    def __call__(self, task: JsonDict, task_dir: Path) -> WorkerExecution:
        env = os.environ.copy()
        env.update(
            {
                "MAC_TASK_ID": task["id"],
                "MAC_TASK_FILE": str(task_dir / "task.json"),
                "MAC_TASK_WORKSPACE": str(task_dir),
            }
        )
        completed = subprocess.run(
            self.argv,
            cwd=str(task_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        return WorkerExecution(
            returncode=completed.returncode,
            summary=_summary_from_output(completed.returncode, completed.stdout, completed.stderr),
            stdout=completed.stdout,
            stderr=completed.stderr,
            metadata={"executor": self.argv},
        )


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
    ) -> None:
        if not agent_id:
            raise MacApiError("agent_id is required")
        self.client = client
        self.agent_id = agent_id
        self.workspace = workspace
        self.executor = executor
        self.lease_seconds = lease_seconds
        self.running_digest = running_digest
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.allowed_projects = list(allowed_projects or [])
        self.required_metadata = dict(required_metadata or {})
        self.require_canary = bool(require_canary)
        self.lease_renew_interval_seconds = lease_renew_interval_seconds
        self.agentbus_control_enabled = bool(agentbus_control_enabled)
        self.self_update_repo = self_update_repo or _default_self_update_repo()
        self.agentbus_control_state_path = (
            agentbus_control_state_path
            if agentbus_control_state_path is not None
            else self.workspace / ".mac-agentbus-control.json"
        )
        self._stop = False
        self._declared_digest = False
        self._declared_policy = False

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
        self._observe_policy_once()
        assignment = self._claim_next_for_agent()
        if assignment is None:
            self._observe_log("worker.no_task", level="debug", detail={"agent_id": self.agent_id})
            return WorkerRunResult(status="no_task")

        task = assignment["task"]
        lease = assignment["lease"]
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
            return self.executor(task, task_dir)
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
        (task_dir / "task.json").write_text(
            json.dumps({"task": task, "lease": lease}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

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

    def _execution_metadata(self, task_dir: Path, execution: WorkerExecution) -> JsonDict:
        metadata = dict(execution.metadata)
        metadata.setdefault("verification", self._load_verification_manifest(task_dir))
        metadata.setdefault(
            "workspace_outputs",
            {
                "stdout_sha256": _sha256_file(task_dir / "stdout.txt"),
                "stderr_sha256": _sha256_file(task_dir / "stderr.txt"),
            },
        )
        return metadata

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


def _safe_git_ref(value: str) -> bool:
    return bool(value and not value.startswith("-") and re.match(SAFE_GIT_REF_RE, value))


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
        if not agent_id:
            raise MacApiError("--agent-id or --register is required")
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
