from __future__ import annotations

import base64
import os
import re
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mac.models import (
    Agent,
    AgentBusChunk,
    AgentBusStream,
    AgentBusStreamStatus,
    AgentMessage,
    AgentStatus,
    Artifact,
    AuthorizationError,
    MoodMode,
    MoodOverlay,
    NapRun,
    NapSchedule,
    NapStatus,
    ConversationThread,
    Deployment,
    DeploymentStatus,
    Environment,
    EVIDENCE_KINDS,
    EvalRun,
    EvalSet,
    EvalTargetKind,
    Evidence,
    HealthStatus,
    HistoryEvent,
    HermesInstance,
    JsonDict,
    Lease,
    LeaseStatus,
    MACError,
    Machine,
    MemoryRecord,
    MessageType,
    NotFoundError,
    ObservabilityEvent,
    Persona,
    PlatformBinding,
    ProjectItem,
    Publication,
    PublicationStatus,
    Review,
    ReviewStatus,
    Rollout,
    ROLLOUT_ACTIONS,
    RolloutStatus,
    RolloutStrategy,
    RuntimeEnvironment,
    RuntimeRun,
    RuntimeRunStatus,
    SecretAccess,
    SecretAuditResult,
    SecretHandle,
    SecretRecord,
    Task,
    TaskState,
    Tenant,
    TERMINAL_TASK_STATES,
    TransitionError,
    User,
    ValidationError,
    VectorRef,
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
    validate_transition,
)
from mac.agent_state_service import AgentStateService
from mac.agentbus_service import AgentBusService
from mac.deploy_service import DeployService
from mac.eval_service import EvalService
from mac.identity_service import IdentityService
from mac.memory_service import MemoryService
from mac.messaging_service import MessagingService
from mac.observability_service import ObservabilityService
from mac.review_service import ReviewService
from mac.secrets_service import SecretsService
from mac.store import SQLiteStore


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


class ControlPlane:
    """Application service layer for the multi-agent control plane."""

    def __init__(
        self,
        store: Optional[SQLiteStore] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        self.store = store or SQLiteStore()
        raw_key = secret_key if secret_key is not None else os.environ.get("MAC_SECRET_KEY")
        if not raw_key:
            raise ValidationError(
                "MAC_SECRET_KEY is required (32+ chars). Set it in the environment or pass secret_key explicitly."
            )
        if len(raw_key) < 32:
            raise ValidationError("MAC_SECRET_KEY must be at least 32 characters")
        # Refuse common placeholder substrings so the example env file in
        # deploy/systemd/mac.env.example cannot be deployed verbatim. The
        # placeholder is long enough to satisfy the length check, but lands
        # every secret under a globally-known Fernet key. Better to fail loud
        # at startup than encrypt with a known constant.
        placeholder_substrings = (
            "REPLACE-ME",
            "REPLACE_ME",
            "CHANGE-ME",
            "CHANGE_ME",
            "your-key-here",
            "xxxxxxxx",
        )
        for marker in placeholder_substrings:
            if marker.lower() in raw_key.lower():
                raise ValidationError(
                    "MAC_SECRET_KEY appears to be a placeholder (%r). "
                    "Generate one with: openssl rand -base64 48" % marker
                )
        fernet_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"mac.control_plane.secrets.v1",
            info=b"fernet-key",
        ).derive(raw_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(fernet_key))
        # Domain sub-services. New domains should land here as their own
        # service classes rather than as more methods on ControlPlane.
        self.identity = IdentityService(self.store)
        self.observability = ObservabilityService(self.store)
        self.agentbus = AgentBusService(self.store, self.observability)
        self.secrets = SecretsService(
            self.store,
            self.observability,
            self._fernet,
            get_agent=self.get_agent,
            get_machine=self.get_machine,
            machine_allows_tenant=self._machine_allows_tenant,
        )
        self.memory = MemoryService(
            self.store,
            get_task=self.get_task,
            get_evidence=self.get_evidence,
            get_platform_binding=self.get_platform_binding,
            record_history=self._record_history,
        )
        self.messaging = MessagingService(
            self.store,
            get_agent=self.get_agent,
            get_task=self.get_task,
        )
        self.evaluations = EvalService(
            self.store,
            self.observability,
            get_evidence=self.get_evidence,
        )
        self.reviews = ReviewService(
            self.store,
            self.observability,
            self.messaging,
            get_task=self.get_task,
            get_agent=self.get_agent,
            get_evidence=self.get_evidence,
            transition_task=self.transition_task,
            record_history=self._record_history,
        )
        self.agent_state = AgentStateService(
            self.store,
            self.observability,
            get_agent=self.get_agent,
            get_evidence=self.get_evidence,
            agent_has_active_lease=self._agent_has_active_lease,
        )
        self.deploy = DeployService(
            self.store,
            self.observability,
            get_tenant=self.get_tenant,
            get_task=self.get_task,
            get_agent=self.get_agent,
            get_evidence=self.get_evidence,
        )

    @classmethod
    def in_memory(cls) -> "ControlPlane":
        return cls(SQLiteStore(":memory:"), secret_key="test-key-with-enough-entropy-32+chars")

    def _resolved_json_column(
        self,
        table: str,
        column: str,
        row_id: str,
        value: Optional[Dict[str, Any]],
    ) -> str:
        """Resolve a JSON column for register-style upserts.

        If the caller explicitly passed a value, use it. Otherwise preserve the
        existing row's value (so re-registering with no metadata does not wipe
        previously-stored metadata). Defaults to {} for new rows.
        """
        if value is not None:
            return json_dumps(ensure_json_object(value))
        row = self.store.query_one(
            "SELECT %s AS value FROM %s WHERE id = ?" % (column, table),
            (row_id,),
        )
        if row is None or row["value"] is None:
            return json_dumps({})
        return row["value"]

    # Human-facing identity + Hermes boundary: thin facade over
    # ``self.identity``. New code should call ``cp.identity.<method>``.

    def register_tenant(self, *args: Any, **kwargs: Any) -> Tenant:
        return self.identity.register_tenant(*args, **kwargs)

    def get_tenant(self, tenant_id_or_name: str) -> Tenant:
        return self.identity.get_tenant(tenant_id_or_name)

    def list_tenants(self) -> List[Tenant]:
        return self.identity.list_tenants()

    def register_user(self, *args: Any, **kwargs: Any) -> User:
        return self.identity.register_user(*args, **kwargs)

    def get_user(self, user_id: str) -> User:
        return self.identity.get_user(user_id)

    def list_users(self, *args: Any, **kwargs: Any) -> List[User]:
        return self.identity.list_users(*args, **kwargs)

    def register_persona(self, *args: Any, **kwargs: Any) -> Persona:
        return self.identity.register_persona(*args, **kwargs)

    def get_persona(self, persona_id: str) -> Persona:
        return self.identity.get_persona(persona_id)

    def list_personas(self, *args: Any, **kwargs: Any) -> List[Persona]:
        return self.identity.list_personas(*args, **kwargs)

    def register_hermes_instance(self, *args: Any, **kwargs: Any) -> HermesInstance:
        return self.identity.register_hermes_instance(*args, **kwargs)

    def get_hermes_instance(self, instance_id: str) -> HermesInstance:
        return self.identity.get_hermes_instance(instance_id)

    def list_hermes_instances(self, *args: Any, **kwargs: Any) -> List[HermesInstance]:
        return self.identity.list_hermes_instances(*args, **kwargs)

    def register_platform_binding(self, *args: Any, **kwargs: Any) -> PlatformBinding:
        return self.identity.register_platform_binding(*args, **kwargs)

    def get_platform_binding(self, binding_id: str) -> PlatformBinding:
        return self.identity.get_platform_binding(binding_id)

    def list_platform_bindings(self, *args: Any, **kwargs: Any) -> List[PlatformBinding]:
        return self.identity.list_platform_bindings(*args, **kwargs)

    def hermes_context(self, hermes_instance_id: str) -> JsonDict:
        return self.identity.hermes_context(hermes_instance_id)

    def create_interaction_task(
        self,
        hermes_instance_id: str,
        title: str,
        user_id: Optional[str] = None,
        platform_binding_id: Optional[str] = None,
        conversation_ref: Optional[str] = None,
        description: str = "",
        project: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
        actor: str = "hermes",
    ) -> Task:
        instance = self.get_hermes_instance(hermes_instance_id)
        if user_id:
            user = self.get_user(user_id)
            if user.tenant_id != instance.tenant_id:
                raise ValidationError("interaction user must belong to hermes instance tenant")
        if platform_binding_id:
            binding = self.get_platform_binding(platform_binding_id)
            if binding.tenant_id != instance.tenant_id or binding.hermes_instance_id != instance.id:
                raise ValidationError("platform binding must belong to hermes instance")
        task_metadata = ensure_json_object(metadata)
        task_metadata.setdefault(
            "origin",
            {
                "type": "hermes_interaction",
                "tenant_id": instance.tenant_id,
                "user_id": user_id,
                "hermes_instance_id": instance.id,
                "persona_id": instance.persona_id,
                "platform_binding_id": platform_binding_id,
                "conversation_ref": conversation_ref,
            },
        )
        task_metadata.setdefault(
            "memory_boundary",
            {
                "hermes_is_authoritative_for_personality": True,
                "hermes_is_authoritative_for_user_memory": True,
                "mac_records_operational_provenance_only": True,
            },
        )
        return self.create_task(
            title,
            description=description,
            project=project,
            priority=priority,
            required_capabilities=required_capabilities,
            dependencies=dependencies,
            metadata=task_metadata,
            max_attempts=max_attempts,
            actor=actor,
        )

    # Task ledger

    def create_task(
        self,
        title: str,
        description: str = "",
        project: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
        actor: str = "human",
    ) -> Task:
        title = title.strip()
        if not title:
            raise ValidationError("task title is required")
        dep_ids = coerce_list(dependencies)
        for dep_id in dep_ids:
            self.get_task(dep_id)
        now = utcnow()
        task_id = new_id("task")
        state = TaskState.BLOCKED.value if dep_ids else TaskState.OPEN.value
        self.store.execute(
            """
            INSERT INTO tasks (
                id, title, description, project, priority, state,
                required_capabilities, dependencies, metadata,
                owner_agent_id, lease_id, leased_until, attempt_count,
                max_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, ?)
            """,
            (
                task_id,
                title,
                description,
                project,
                int(priority),
                state,
                json_dumps(coerce_list(required_capabilities)),
                json_dumps(dep_ids),
                json_dumps(ensure_json_object(metadata)),
                int(max_attempts),
                now,
                now,
            ),
        )
        self._record_history(
            task_id,
            "task.created",
            actor,
            None,
            state,
            {
                "title": title,
                "required_capabilities": coerce_list(required_capabilities),
                "dependencies": dep_ids,
            },
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        row = self.store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFoundError("task not found: %s" % task_id)
        return self._task_from_row(row)

    def list_tasks(self, state: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Task]:
        if state:
            rows = self.store.query_all(
                "SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC, created_at",
                (_state_value(state),),
            )
        else:
            rows = self.store.query_all("SELECT * FROM tasks ORDER BY priority DESC, created_at")
        tasks = [self._task_from_row(row) for row in rows]
        if tenant_id is not None:
            tasks = [task for task in tasks if self._task_tenant_id(task) == tenant_id]
        return tasks

    def task_detail(self, task_id: str) -> JsonDict:
        task = self.get_task(task_id)
        return {
            "task": task.to_dict(),
            "history": [event.to_dict() for event in self.task_history(task_id)],
            "evidence": [item.to_dict() for item in self.list_evidence(task_id)],
            "reviews": [item.to_dict() for item in self.list_reviews(task_id)],
        }

    def task_summary(self, task_id: str) -> JsonDict:
        detail = self.task_detail(task_id)
        task = detail["task"]
        evidence = detail["evidence"]
        reviews = detail["reviews"]
        approved_reviews = [review for review in reviews if review["status"] == ReviewStatus.APPROVED.value]
        publications = [pub.to_dict() for pub in self.reviews.list_publications(task_id)]
        parts = ["%s is %s" % (task["title"], task["state"])]
        if task["owner_agent_id"]:
            parts.append("owner=%s" % task["owner_agent_id"])
        if evidence:
            parts.append("%d evidence item(s)" % len(evidence))
        if approved_reviews:
            parts.append("%d approved review(s)" % len(approved_reviews))
        if publications:
            parts.append("published to %s" % publications[-1]["target"])
        return {
            "task_id": task_id,
            "title": task["title"],
            "state": task["state"],
            "owner_agent_id": task["owner_agent_id"],
            "evidence_count": len(evidence),
            "review_count": len(reviews),
            "approved_review_count": len(approved_reviews),
            "publications": publications,
            "origin": task["metadata"].get("origin"),
            "memory_boundary": task["metadata"].get("memory_boundary"),
            "summary": "; ".join(parts),
        }

    def task_history(self, task_id: str) -> List[HistoryEvent]:
        self.get_task(task_id)
        rows = self.store.query_all(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        )
        return [self._history_from_row(row) for row in rows]

    # Unified audit / event stream

    EVENT_SUBJECT_TYPES = (
        "task",
        "rollout",
        "eval_set",
        "secret",
        "environment",
        "conversation_thread",
        "vector_ref",
        "agent",
    )

    def list_events(
        self,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        actor: Optional[str] = None,
        event_type: Optional[str] = None,
        event_type_prefix: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> List[JsonDict]:
        """Query the unified audit stream across task/rollout/eval_set/secret events.

        Filters compose with AND. Results are newest-first; cap is 1000 to keep
        a single page bounded. Operators asking "what happened" should reach for
        this method instead of joining the four per-resource audit tables.
        """
        if subject_type is not None and subject_type not in self.EVENT_SUBJECT_TYPES:
            raise ValidationError(
                "unsupported event subject_type: %s (allowed: %s)"
                % (subject_type, ", ".join(self.EVENT_SUBJECT_TYPES))
            )
        clauses: List[str] = []
        params: List[Any] = []
        if subject_type is not None:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if event_type_prefix is not None:
            clauses.append("event_type LIKE ?")
            params.append(event_type_prefix + "%")
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        sql = "SELECT id, subject_type, subject_id, event_type, actor, detail, created_at FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        rows = self.store.query_all(sql, tuple(params))
        return [
            {
                "id": row["id"],
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # Observability: thin facade over ``self.observability`` so existing
    # callers keep working. New code should call ``cp.observability.<method>``
    # directly.

    def record_observation(self, *args: Any, **kwargs: Any) -> ObservabilityEvent:
        return self.observability.record_observation(*args, **kwargs)

    def record_metric(self, *args: Any, **kwargs: Any) -> ObservabilityEvent:
        return self.observability.record_metric(*args, **kwargs)

    def record_log(self, *args: Any, **kwargs: Any) -> ObservabilityEvent:
        return self.observability.record_log(*args, **kwargs)

    def list_observability(self, *args: Any, **kwargs: Any) -> List[ObservabilityEvent]:
        return self.observability.list_observability(*args, **kwargs)

    def prune_observability(self, *args: Any, **kwargs: Any) -> int:
        return self.observability.prune(*args, **kwargs)

    def observability_summary(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.observability.summary(*args, **kwargs)

    def list_dead_letters(self, tenant_id: Optional[str] = None) -> List[Task]:
        rows = self.store.query_all(
            """
            SELECT * FROM tasks
            WHERE state = ? AND attempt_count >= max_attempts
            ORDER BY updated_at, id
            """,
            (TaskState.FAILED.value,),
        )
        tasks = [self._task_from_row(row) for row in rows]
        if tenant_id is not None:
            tasks = [task for task in tasks if self._task_tenant_id(task) == tenant_id]
        return tasks

    def transition_task(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Task:
        target = _state_value(target_state)
        task = self.get_task(task_id)
        if task.state == target:
            return task
        validate_transition(task.state, target)
        if target == TaskState.NEEDS_REVIEW.value and not self.list_evidence(task_id):
            raise ValidationError("task needs evidence before review")
        if target == TaskState.COMPLETED.value and not self.reviews.completion_authorized(task_id):
            raise ValidationError("task completion requires approved review and evidence")
        now = utcnow()
        owner_agent_id = task.owner_agent_id
        lease_id = task.lease_id
        leased_until = task.leased_until
        if target in {TaskState.OPEN.value, TaskState.FAILED.value, TaskState.CANCELLED.value}:
            owner_agent_id = None
            lease_id = None
            leased_until = None
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (target, owner_agent_id, lease_id, leased_until, now, task_id),
            )
            if task.owner_agent_id and target in TERMINAL_TASK_STATES.union({TaskState.OPEN.value}):
                self._set_agent_idle(task.owner_agent_id, conn=conn)
            self._record_history(
                task_id, "task.transitioned", actor, task.state, target, detail or {}, conn=conn
            )
        return self.get_task(task_id)

    def claim_task(self, task_id: str, agent_id: str, lease_seconds: int = 900) -> Tuple[Task, Lease]:
        task = self.get_task(task_id)
        agent = self.get_agent(agent_id)
        if task.state == TaskState.BLOCKED.value and self._dependencies_satisfied(task):
            task = self.transition_task(task_id, TaskState.OPEN.value, "dispatcher", {"reason": "dependencies satisfied"})
        if task.state != TaskState.OPEN.value:
            raise TransitionError("only open tasks can be claimed")
        if not self._agent_available_for(agent, task):
            raise ValidationError("agent %s cannot claim task %s" % (agent_id, task_id))
        if task.attempt_count >= task.max_attempts:
            self.transition_task(task_id, TaskState.FAILED.value, "dispatcher", {"reason": "max attempts"})
            raise TransitionError("task %s exhausted max_attempts" % task_id)
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="microseconds")
        lease_id = new_id("lease")
        with self.store.transaction() as conn:
            # Atomic claim: the UPDATE only succeeds if the task is still OPEN and
            # unleased. rowcount==0 means another dispatcher already took it.
            cursor = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ? AND state = ? AND lease_id IS NULL
                """,
                (
                    TaskState.CLAIMED.value,
                    agent_id,
                    lease_id,
                    expires_at,
                    now,
                    task_id,
                    TaskState.OPEN.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TransitionError("task %s was claimed by another agent" % task_id)
            conn.execute(
                """
                INSERT INTO leases (id, task_id, agent_id, expires_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (lease_id, task_id, agent_id, expires_at, LeaseStatus.ACTIVE.value, now, now),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = ?, updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (AgentStatus.BUSY.value, task_id, now, now, agent_id),
            )
        self._record_history(
            task_id,
            "task.claimed",
            agent_id,
            task.state,
            TaskState.CLAIMED.value,
            {"lease_id": lease_id, "expires_at": expires_at},
        )
        return self.get_task(task_id), self.get_lease(lease_id)

    def start_task(self, task_id: str, agent_id: str) -> Task:
        task = self.get_task(task_id)
        if task.owner_agent_id != agent_id:
            raise AuthorizationError("agent does not own task lease")
        return self.transition_task(task_id, TaskState.RUNNING.value, agent_id, {})

    def submit_for_review(self, task_id: str, agent_id: str) -> Task:
        task = self.get_task(task_id)
        if task.owner_agent_id != agent_id:
            raise AuthorizationError("agent does not own task lease")
        return self.transition_task(task_id, TaskState.NEEDS_REVIEW.value, agent_id, {})

    def add_evidence(
        self,
        task_id: str,
        kind: str,
        uri: str,
        summary: str,
        created_by: str,
        checksum: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Evidence:
        self.get_task(task_id)
        if not kind or not uri or not summary:
            raise ValidationError("evidence requires kind, uri, and summary")
        if kind not in EVIDENCE_KINDS:
            raise ValidationError("unsupported evidence kind: %s" % kind)
        if kind == "publication" and not checksum:
            raise ValidationError("publication evidence requires a checksum")
        now = utcnow()
        evidence_id = new_id("ev")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence (id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    task_id,
                    kind,
                    uri,
                    summary,
                    checksum,
                    json_dumps(ensure_json_object(metadata)),
                    created_by,
                    now,
                ),
            )
            self._record_history(
                task_id,
                "task.evidence_added",
                created_by,
                None,
                None,
                {"evidence_id": evidence_id, "kind": kind, "uri": uri},
                conn=conn,
            )
        return self.get_evidence(evidence_id)

    def get_evidence(self, evidence_id: str) -> Evidence:
        row = self.store.query_one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        if row is None:
            raise NotFoundError("evidence not found: %s" % evidence_id)
        return self._evidence_from_row(row)

    def list_evidence(self, task_id: str) -> List[Evidence]:
        rows = self.store.query_all(
            "SELECT * FROM evidence WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        )
        return [self._evidence_from_row(row) for row in rows]

    def renew_lease(self, lease_id: str, agent_id: str, lease_seconds: int = 900) -> Lease:
        lease = self.get_lease(lease_id)
        if lease.agent_id != agent_id:
            raise AuthorizationError("agent does not own lease")
        if lease.status != LeaseStatus.ACTIVE.value:
            raise ValidationError("only active leases can be renewed")
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="microseconds")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE leases SET expires_at = ?, status = ?, updated_at = ? WHERE id = ?",
                (expires_at, LeaseStatus.ACTIVE.value, now, lease_id),
            )
            conn.execute(
                "UPDATE tasks SET leased_until = ?, updated_at = ? WHERE lease_id = ?",
                (expires_at, now, lease_id),
            )
        self._record_history(lease.task_id, "task.lease_renewed", agent_id, None, None, {"lease_id": lease_id})
        return self.get_lease(lease_id)

    def get_lease(self, lease_id: str) -> Lease:
        row = self.store.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
        if row is None:
            raise NotFoundError("lease not found: %s" % lease_id)
        return self._lease_from_row(row)

    def expire_leases(self, now: Optional[str] = None) -> List[Task]:
        cutoff = now or utcnow()
        rows = self.store.query_all(
            "SELECT * FROM leases WHERE status = ? AND expires_at <= ? ORDER BY expires_at",
            (LeaseStatus.ACTIVE.value, cutoff),
        )
        recovered: List[Task] = []
        for row in rows:
            lease = self._lease_from_row(row)
            task = self.get_task(lease.task_id)
            next_state = TaskState.FAILED.value if task.attempt_count >= task.max_attempts else TaskState.OPEN.value
            timestamp = utcnow()
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ?",
                    (LeaseStatus.EXPIRED.value, timestamp, lease.id),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_state, timestamp, task.id),
                )
                conn.execute(
                    """
                    UPDATE agents
                    SET status = ?, current_task_id = NULL, updated_at = ?
                    WHERE id = ? AND current_task_id = ?
                    """,
                    (AgentStatus.IDLE.value, timestamp, lease.agent_id, task.id),
                )
            self._record_history(
                task.id,
                "task.lease_expired",
                "dispatcher",
                task.state,
                next_state,
                {"lease_id": lease.id, "agent_id": lease.agent_id},
            )
            recovered.append(self.get_task(task.id))
        return recovered

    def release_lease(self, lease_id: str, agent_id: str) -> Task:
        lease = self.get_lease(lease_id)
        if lease.agent_id != agent_id:
            raise AuthorizationError("agent does not own lease")
        task = self.get_task(lease.task_id)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE leases SET status = ?, updated_at = ? WHERE id = ?",
                (LeaseStatus.RELEASED.value, now, lease_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (TaskState.OPEN.value, now, task.id),
            )
            conn.execute(
                "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
                (AgentStatus.IDLE.value, now, agent_id),
            )
        self._record_history(task.id, "task.lease_released", agent_id, task.state, TaskState.OPEN.value, {"lease_id": lease_id})
        return self.get_task(task.id)

    # Fleet registry

    def register_machine(
        self,
        hostname: str,
        labels: Optional[Dict[str, Any]] = None,
        resources: Optional[Dict[str, Any]] = None,
        trusted: bool = True,
        machine_id: Optional[str] = None,
    ) -> Machine:
        if not hostname:
            raise ValidationError("hostname is required")
        now = utcnow()
        mid = machine_id or new_id("machine")
        labels_json = self._resolved_json_column("machines", "labels", mid, labels)
        resources_json = self._resolved_json_column("machines", "resources", mid, resources)
        self.store.execute(
            """
            INSERT INTO machines (id, hostname, labels, resources, trusted, created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                hostname = excluded.hostname,
                labels = excluded.labels,
                resources = excluded.resources,
                trusted = excluded.trusted,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                mid,
                hostname,
                labels_json,
                resources_json,
                1 if trusted else 0,
                now,
                now,
                now,
            ),
        )
        return self.get_machine(mid)

    def get_machine(self, machine_id: str) -> Machine:
        row = self.store.query_one("SELECT * FROM machines WHERE id = ?", (machine_id,))
        if row is None:
            raise NotFoundError("machine not found: %s" % machine_id)
        return self._machine_from_row(row)

    def list_machines(self) -> List[Machine]:
        return [self._machine_from_row(row) for row in self.store.query_all("SELECT * FROM machines ORDER BY hostname")]

    def register_agent(
        self,
        machine_id: str,
        name: str,
        capabilities: Optional[Iterable[str]] = None,
        resources: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> Agent:
        self.get_machine(machine_id)
        if not name:
            raise ValidationError("agent name is required")
        now = utcnow()
        aid = agent_id or new_id("agent")
        if capabilities is None:
            existing_caps = self.store.query_one(
                "SELECT capabilities FROM agents WHERE id = ?", (aid,)
            )
            capabilities_json = (
                existing_caps["capabilities"] if existing_caps is not None else json_dumps([])
            )
        else:
            capabilities_json = json_dumps(coerce_list(capabilities))
        resources_json = self._resolved_json_column("agents", "resources", aid, resources)
        self.store.execute(
            """
            INSERT INTO agents (
                id, machine_id, name, capabilities, resources, status, health_status,
                current_task_id, created_at, updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                machine_id = excluded.machine_id,
                name = excluded.name,
                capabilities = excluded.capabilities,
                resources = excluded.resources,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                aid,
                machine_id,
                name,
                capabilities_json,
                resources_json,
                AgentStatus.IDLE.value,
                HealthStatus.HEALTHY.value,
                now,
                now,
                now,
            ),
        )
        return self.get_agent(aid)

    def get_agent(self, agent_id: str) -> Agent:
        row = self.store.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise NotFoundError("agent not found: %s" % agent_id)
        return self._agent_from_row(row)

    def list_agents(self) -> List[Agent]:
        rows = self.store.query_all("SELECT * FROM agents ORDER BY name, id")
        return [self._agent_from_row(row) for row in rows]

    def heartbeat_agent(
        self,
        agent_id: str,
        status: Optional[str] = None,
        health_status: Optional[str] = None,
        resources: Optional[Dict[str, Any]] = None,
        running_digest: Optional[str] = None,
    ) -> Agent:
        self.get_agent(agent_id)
        now = utcnow()
        updates = ["last_seen_at = ?", "updated_at = ?"]
        params: List[Any] = [now, now]
        status_value: Optional[str] = None
        if status is not None:
            status_value = _state_value(status)
            try:
                AgentStatus(status_value)
            except ValueError:
                raise ValidationError("unsupported agent status: %s" % status_value)
            updates.append("status = ?")
            params.append(status_value)
        if health_status is not None:
            health_value = _state_value(health_status)
            try:
                HealthStatus(health_value)
            except ValueError:
                raise ValidationError("unsupported agent health_status: %s" % health_value)
            updates.append("health_status = ?")
            params.append(health_value)
        if resources is not None:
            updates.append("resources = ?")
            params.append(json_dumps(resources))
        if running_digest is not None:
            digest = running_digest.strip()
            if digest:
                # Anchor fleet rollout state to a known runtime build. If you
                # roll out a new agent build, register the runtime first; the
                # heartbeat that declares the new digest then becomes the truth
                # source for "how many agents are on which build."
                exists = self.store.query_one(
                    "SELECT 1 FROM runtime_environments WHERE digest = ? LIMIT 1",
                    (digest,),
                )
                if exists is None:
                    raise ValidationError(
                        "running_digest %s is not registered as a runtime_environments.digest"
                        % digest
                    )
                updates.append("running_digest = ?")
                params.append(digest)
            else:
                updates.append("running_digest = NULL")
        if status_value == AgentStatus.IDLE.value and self._agent_has_active_lease(agent_id):
            raise ValidationError("agent cannot report idle while holding an active lease")
        if status_value == AgentStatus.OFFLINE.value:
            self._expire_agent_active_leases(agent_id, now, "heartbeat_offline")
        if status_value in {AgentStatus.IDLE.value, AgentStatus.OFFLINE.value}:
            updates.append("current_task_id = NULL")
        params.append(agent_id)
        self.store.execute("UPDATE agents SET %s WHERE id = ?" % ", ".join(updates), tuple(params))
        return self.get_agent(agent_id)

    def fleet_build_distribution(self) -> JsonDict:
        """Aggregate agents by their declared running_digest.

        Useful for "what percent of the fleet is on v0.8 vs v0.9" without joining
        rollouts. Agents with no declared digest are bucketed as 'unknown'.
        """
        rows = self.store.query_all(
            """
            SELECT COALESCE(running_digest, '') AS digest, COUNT(*) AS count
            FROM agents
            WHERE status != ?
            GROUP BY running_digest
            ORDER BY count DESC
            """,
            (AgentStatus.OFFLINE.value,),
        )
        buckets = [
            {"digest": row["digest"] or None, "count": int(row["count"])}
            for row in rows
        ]
        total = sum(bucket["count"] for bucket in buckets) or 1
        for bucket in buckets:
            bucket["percent"] = round(bucket["count"] * 100.0 / total, 2)
        return {"total_live_agents": total if total > 0 else 0, "buckets": buckets}

    # Mood overlays (agent-self-reported emotional state)
    #
    # The contract: agents pick their own mood based on local signals (recent
    # outcomes, retry counts, review rejections — already in the events
    # stream). mac records and audits transitions; it does NOT derive mood on
    # the agent's behalf. Operators can read, but the authoritative caller is
    # the agent itself.

    # Moods: thin facade over ``self.agent_state``.

    def set_mood(self, *args: Any, **kwargs: Any) -> MoodOverlay:
        return self.agent_state.set_mood(*args, **kwargs)

    def get_current_mood(self, agent_id: str) -> Optional[MoodOverlay]:
        return self.agent_state.get_current_mood(agent_id)

    def clear_mood(self, *args: Any, **kwargs: Any) -> Optional[MoodOverlay]:
        return self.agent_state.clear_mood(*args, **kwargs)

    def get_mood_overlay(self, overlay_id: str) -> MoodOverlay:
        return self.agent_state.get_mood_overlay(overlay_id)

    def list_mood_history(self, *args: Any, **kwargs: Any) -> List[MoodOverlay]:
        return self.agent_state.list_mood_history(*args, **kwargs)

    # Nap schedule + lifecycle
    #
    # Each agent has a single nap_schedule row (offset_minutes, window_minutes).
    # The offset defaults to a stable hash of the agent's name to spread the
    # fleet across the early-UTC window (matches ACC's spec, MD5 % 360). Nap
    # *execution* is off-process — the agent (or a sidecar) decides what to
    # summarize and where to store it. mac records begin/complete events and
    # links to the produced summary evidence + vector refs.

    # Nap schedule + lifecycle: thin facade over ``self.agent_state``.

    def configure_nap(self, *args: Any, **kwargs: Any) -> NapSchedule:
        return self.agent_state.configure_nap(*args, **kwargs)

    def get_nap_schedule(self, agent_id: str) -> Optional[NapSchedule]:
        return self.agent_state.get_nap_schedule(agent_id)

    def list_nap_schedules(self) -> List[NapSchedule]:
        return self.agent_state.list_nap_schedules()

    def next_nap_window(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, str]]:
        return self.agent_state.next_nap_window(*args, **kwargs)

    def begin_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.begin_nap(*args, **kwargs)

    def complete_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.complete_nap(*args, **kwargs)

    def fail_nap(self, *args: Any, **kwargs: Any) -> NapRun:
        return self.agent_state.fail_nap(*args, **kwargs)

    def get_nap_run(self, run_id: str) -> NapRun:
        return self.agent_state.get_nap_run(run_id)

    def list_nap_runs(self, *args: Any, **kwargs: Any) -> List[NapRun]:
        return self.agent_state.list_nap_runs(*args, **kwargs)

    def mark_stale_agents_offline(self, stale_after_seconds: int) -> List[Agent]:
        cutoff = (
            parse_time(utcnow()) - timedelta(seconds=max(1, int(stale_after_seconds)))
        ).isoformat(timespec="microseconds")
        rows = self.store.query_all(
            """
            SELECT * FROM agents
            WHERE status != ? AND last_seen_at <= ?
            ORDER BY last_seen_at, id
            """,
            (AgentStatus.OFFLINE.value, cutoff),
        )
        marked = []
        for row in rows:
            agent = self._agent_from_row(row)
            marked.append(self.heartbeat_agent(agent.id, status=AgentStatus.OFFLINE.value))
        return marked

    # Dispatcher

    def dispatch_once(
        self,
        lease_seconds: int = 900,
        skip_tenants: Optional[Iterable[str]] = None,
    ) -> Optional[JsonDict]:
        self.expire_leases()
        self._unblock_ready_tasks()
        skipped = set(skip_tenants or [])
        tasks = [
            task
            for task in self._dispatch_ordered_tasks()
            if (self._task_tenant_id(task) or "") not in skipped
        ]
        agents = self._available_agents()
        for task in tasks:
            for agent in agents:
                if not self._agent_available_for(agent, task):
                    continue
                try:
                    claimed, lease = self.claim_task(task.id, agent.id, lease_seconds=lease_seconds)
                except (TransitionError, ValidationError):
                    # task was already claimed, exhausted attempts, or otherwise
                    # ineligible — try the next (task, agent) pair.
                    continue
                self.send_message(
                    "dispatcher",
                    agent.id,
                    MessageType.NUDGE.value,
                    {"task_id": claimed.id, "lease_id": lease.id, "reason": "assigned"},
                    task_id=claimed.id,
                )
                return {"task": claimed.to_dict(), "agent": agent.to_dict(), "lease": lease.to_dict()}
        return None

    def claim_next_for_agent(
        self,
        agent_id: str,
        lease_seconds: int = 900,
        allowed_projects: Optional[Iterable[str]] = None,
        required_metadata: Optional[Dict[str, Any]] = None,
        require_canary: bool = False,
        dry_run: bool = False,
    ) -> Optional[JsonDict]:
        """Claim the next dispatch-eligible task for one worker.

        This is the worker-side counterpart to dispatch_once(). It preserves
        the same capability, capacity, tenant, trust, and health checks while
        allowing a worker daemon to pull only work assigned to its own durable
        identity. Worker policy filters provide a quarantine lane for canaries:
        dry runs can inspect the next eligible task without leasing it, and
        loop-mode workers can refuse non-canary or out-of-project work before
        touching production tasks.
        """
        self.expire_leases()
        self._unblock_ready_tasks()
        agent = self.get_agent(agent_id)
        policy = self._worker_claim_policy(
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=require_canary,
            dry_run=dry_run,
        )
        rejected_policy: Dict[str, int] = {}
        rejected_dispatch = 0
        considered = 0
        for task in self._dispatch_ordered_tasks():
            considered += 1
            allowed, reason = self._task_matches_worker_claim_policy(task, policy)
            if not allowed:
                rejected_policy[reason] = rejected_policy.get(reason, 0) + 1
                continue
            if not self._agent_available_for(agent, task):
                rejected_dispatch += 1
                continue
            detail = {
                "agent_id": agent.id,
                "task_id": task.id,
                "dry_run": dry_run,
                "policy": policy,
                "considered": considered,
                "rejected_policy": rejected_policy,
                "rejected_dispatch": rejected_dispatch,
            }
            if dry_run:
                self.record_log(
                    "worker.routing.dry_run_candidate",
                    layer="control_plane",
                    source=agent.id,
                    subject_type="task",
                    subject_id=task.id,
                    detail=detail,
                )
                return {
                    "task": task.to_dict(),
                    "agent": agent.to_dict(),
                    "lease": None,
                    "dry_run": True,
                    "policy": policy,
                }
            try:
                claimed, lease = self.claim_task(task.id, agent.id, lease_seconds=lease_seconds)
            except (TransitionError, ValidationError):
                continue
            self.record_log(
                "worker.routing.claimed",
                layer="control_plane",
                source=agent.id,
                subject_type="task",
                subject_id=claimed.id,
                detail={**detail, "lease_id": lease.id},
            )
            self.send_message(
                "dispatcher",
                agent.id,
                MessageType.NUDGE.value,
                {"task_id": claimed.id, "lease_id": lease.id, "reason": "worker_claimed"},
                task_id=claimed.id,
            )
            return {"task": claimed.to_dict(), "agent": agent.to_dict(), "lease": lease.to_dict()}
        self.record_log(
            "worker.routing.no_candidate",
            level="debug",
            layer="control_plane",
            source=agent.id,
            detail={
                "agent_id": agent.id,
                "dry_run": dry_run,
                "policy": policy,
                "considered": considered,
                "rejected_policy": rejected_policy,
                "rejected_dispatch": rejected_dispatch,
            },
        )
        return None

    def tick(
        self,
        lease_seconds: int = 900,
        limit: int = 100,
        stale_after_seconds: Optional[int] = None,
    ) -> JsonDict:
        stale_agents = []
        if stale_after_seconds is not None:
            stale_agents = [
                agent.to_dict()
                for agent in self.mark_stale_agents_offline(stale_after_seconds)
            ]
        expired = [task.to_dict() for task in self.expire_leases()]
        self._unblock_ready_tasks()
        assignments = []
        served_tenants = set()
        for _ in range(limit):
            assignment = self.dispatch_once(
                lease_seconds=lease_seconds,
                skip_tenants=served_tenants,
            )
            if assignment is None and served_tenants:
                served_tenants.clear()
                assignment = self.dispatch_once(lease_seconds=lease_seconds)
            if assignment is None:
                break
            assignments.append(assignment)
            task_dict = assignment["task"]
            origin = task_dict.get("metadata", {}).get("origin", {})
            served_tenants.add(str(origin.get("tenant_id") or task_dict.get("metadata", {}).get("tenant_id") or ""))
        return {
            "stale_agents": stale_agents,
            "expired": expired,
            "assignments": assignments,
            "dead_letters": [task.to_dict() for task in self.list_dead_letters()],
        }

    # Communication bus

    # Agent control messages: thin facade over ``self.messaging``.

    def send_message(self, *args: Any, **kwargs: Any) -> AgentMessage:
        return self.messaging.send_message(*args, **kwargs)

    def get_message(self, message_id: str) -> AgentMessage:
        return self.messaging.get_message(message_id)

    def deliver_messages(self, *args: Any, **kwargs: Any) -> List[AgentMessage]:
        return self.messaging.deliver_messages(*args, **kwargs)

    def list_messages(self, *args: Any, **kwargs: Any) -> List[AgentMessage]:
        return self.messaging.list_messages(*args, **kwargs)

    # AgentBus typed content streams: thin facade over ``self.agentbus``.
    # New code should call ``cp.agentbus.<method>`` directly.

    def open_agentbus_stream(self, *args: Any, **kwargs: Any) -> AgentBusStream:
        return self.agentbus.open_stream(*args, **kwargs)

    def append_agentbus_chunk(self, *args: Any, **kwargs: Any) -> AgentBusChunk:
        return self.agentbus.append_chunk(*args, **kwargs)

    def close_agentbus_stream(self, *args: Any, **kwargs: Any) -> AgentBusStream:
        return self.agentbus.close_stream(*args, **kwargs)

    def get_agentbus_stream(self, stream_id: str) -> AgentBusStream:
        return self.agentbus.get_stream(stream_id)

    def get_agentbus_chunk(self, chunk_id: str) -> AgentBusChunk:
        return self.agentbus.get_chunk(chunk_id)

    def list_agentbus_streams(self, *args: Any, **kwargs: Any) -> List[AgentBusStream]:
        return self.agentbus.list_streams(*args, **kwargs)

    def assert_agentbus_authorized(self, agent_id: str, stream_id: str) -> AgentBusStream:
        return self.agentbus.assert_authorized(agent_id, stream_id)

    def read_agentbus_chunks(self, *args: Any, **kwargs: Any) -> List[AgentBusChunk]:
        return self.agentbus.read_chunks(*args, **kwargs)

    def publish_agentbus_content(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.agentbus.publish(*args, **kwargs)

    # Reviews + publication: thin facade over ``self.reviews``.

    def request_review(self, *args: Any, **kwargs: Any) -> Review:
        return self.reviews.request_review(*args, **kwargs)

    def submit_review(self, *args: Any, **kwargs: Any) -> Review:
        return self.reviews.submit_review(*args, **kwargs)

    def get_review(self, review_id: str) -> Review:
        return self.reviews.get_review(review_id)

    def list_reviews(self, task_id: str) -> List[Review]:
        return self.reviews.list_reviews(task_id)

    def publish_task(self, *args: Any, **kwargs: Any) -> Publication:
        return self.reviews.publish_task(*args, **kwargs)

    def get_publication(self, publication_id: str) -> Publication:
        return self.reviews.get_publication(publication_id)

    def list_publications(self, *args: Any, **kwargs: Any) -> List[Publication]:
        return self.reviews.list_publications(*args, **kwargs)

    # Secrets boundary: thin facade over ``self.secrets``. New code should
    # call ``cp.secrets.<method>`` directly.

    def create_secret(self, *args: Any, **kwargs: Any) -> SecretRecord:
        return self.secrets.create_secret(*args, **kwargs)

    def get_secret(self, secret_id_or_name: str) -> SecretRecord:
        return self.secrets.get_secret(secret_id_or_name)

    def list_secrets(self) -> List[SecretRecord]:
        return self.secrets.list_secrets()

    def request_secret(self, *args: Any, **kwargs: Any) -> SecretHandle:
        return self.secrets.request_secret(*args, **kwargs)

    def rotate_secret(self, *args: Any, **kwargs: Any) -> SecretRecord:
        return self.secrets.rotate_secret(*args, **kwargs)

    def list_secret_audits(self, *args: Any, **kwargs: Any) -> List[SecretAccess]:
        return self.secrets.list_audits(*args, **kwargs)

    def reveal_secret(self, *args: Any, **kwargs: Any) -> str:
        return self.secrets.reveal_secret(*args, **kwargs)

    # Artifact registry

    # Artifacts + environments + deployments + runtimes: thin facade over
    # ``self.deploy``. New code should call ``cp.deploy.<method>`` directly.

    def register_artifact(self, *args: Any, **kwargs: Any) -> Artifact:
        return self.deploy.register_artifact(*args, **kwargs)

    def get_artifact(self, artifact_id_or_digest: str) -> Artifact:
        return self.deploy.get_artifact(artifact_id_or_digest)

    def list_artifacts(self, *args: Any, **kwargs: Any) -> List[Artifact]:
        return self.deploy.list_artifacts(*args, **kwargs)

    def register_environment(self, *args: Any, **kwargs: Any) -> Environment:
        return self.deploy.register_environment(*args, **kwargs)

    def get_environment(self, env_id_or_name: str) -> Environment:
        return self.deploy.get_environment(env_id_or_name)

    def list_environments(self, *args: Any, **kwargs: Any) -> List[Environment]:
        return self.deploy.list_environments(*args, **kwargs)

    def deploy_artifact(self, *args: Any, **kwargs: Any) -> Deployment:
        return self.deploy.deploy_artifact(*args, **kwargs)

    def get_deployment(self, deployment_id: str) -> Deployment:
        return self.deploy.get_deployment(deployment_id)

    def current_deployment(self, environment_id: str) -> Optional[Deployment]:
        return self.deploy.current_deployment(environment_id)

    def list_deployments(self, environment_id: str) -> List[Deployment]:
        return self.deploy.list_deployments(environment_id)

    def create_runtime(self, *args: Any, **kwargs: Any) -> RuntimeEnvironment:
        return self.deploy.create_runtime(*args, **kwargs)

    def get_runtime(self, runtime_id_or_name: str) -> RuntimeEnvironment:
        return self.deploy.get_runtime(runtime_id_or_name)

    def list_runtimes(self) -> List[RuntimeEnvironment]:
        return self.deploy.list_runtimes()

    def create_runtime_run(self, *args: Any, **kwargs: Any) -> RuntimeRun:
        return self.deploy.create_runtime_run(*args, **kwargs)

    def complete_runtime_run(self, *args: Any, **kwargs: Any) -> RuntimeRun:
        return self.deploy.complete_runtime_run(*args, **kwargs)

    def get_runtime_run(self, run_id: str) -> RuntimeRun:
        return self.deploy.get_runtime_run(run_id)

    def list_runtime_runs(self) -> List[RuntimeRun]:
        return self.deploy.list_runtime_runs()

    # Project bridge

    def import_project_item(
        self,
        source: str,
        external_id: str,
        title: str,
        payload: Dict[str, Any],
        required_capabilities: Optional[Iterable[str]] = None,
        actor: str = "bridge",
    ) -> ProjectItem:
        existing = self.store.query_one(
            "SELECT * FROM project_items WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        if existing is not None:
            return self._project_item_from_row(existing)
        task = self.create_task(
            title,
            description=json_dumps(payload),
            project=source,
            required_capabilities=required_capabilities,
            metadata={"source": source, "external_id": external_id},
            actor=actor,
        )
        now = utcnow()
        item_id = new_id("item")
        self.store.execute(
            """
            INSERT INTO project_items (id, source, external_id, title, payload, task_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, source, external_id, title, json_dumps(payload), task.id, "imported", now, now),
        )
        self.add_memory(
            task.id,
            "project_item",
            item_id,
            "imported",
            "Imported %s:%s as durable task %s" % (source, external_id, task.id),
            None,
            actor,
        )
        return self.get_project_item(item_id)

    def get_project_item(self, item_id: str) -> ProjectItem:
        row = self.store.query_one("SELECT * FROM project_items WHERE id = ?", (item_id,))
        if row is None:
            raise NotFoundError("project item not found: %s" % item_id)
        return self._project_item_from_row(row)

    def list_project_items(self) -> List[ProjectItem]:
        rows = self.store.query_all("SELECT * FROM project_items ORDER BY created_at, id")
        return [self._project_item_from_row(row) for row in rows]

    # Memory + conversation threads + vector refs: thin facade over
    # ``self.memory``. New code should call ``cp.memory.<method>`` directly.

    def add_memory(self, *args: Any, **kwargs: Any) -> MemoryRecord:
        return self.memory.add_memory(*args, **kwargs)

    def get_memory(self, memory_id: str) -> MemoryRecord:
        return self.memory.get_memory(memory_id)

    def search_memory(self, *args: Any, **kwargs: Any) -> List[MemoryRecord]:
        return self.memory.search_memory(*args, **kwargs)

    def track_conversation(self, *args: Any, **kwargs: Any) -> ConversationThread:
        return self.memory.track_conversation(*args, **kwargs)

    def get_conversation_thread(self, thread_id: str) -> ConversationThread:
        return self.memory.get_conversation_thread(thread_id)

    def list_conversation_threads(self, *args: Any, **kwargs: Any) -> List[ConversationThread]:
        return self.memory.list_conversation_threads(*args, **kwargs)

    def record_vector_ref(self, *args: Any, **kwargs: Any) -> VectorRef:
        return self.memory.record_vector_ref(*args, **kwargs)

    def get_vector_ref(self, ref_id: str) -> VectorRef:
        return self.memory.get_vector_ref(ref_id)

    def list_vector_refs(self, *args: Any, **kwargs: Any) -> List[VectorRef]:
        return self.memory.list_vector_refs(*args, **kwargs)

    # Evaluation: thin facade over ``self.evaluations``.

    def create_eval_set(self, *args: Any, **kwargs: Any) -> EvalSet:
        return self.evaluations.create_eval_set(*args, **kwargs)

    def get_eval_set(self, eval_set_id_or_name: str) -> EvalSet:
        return self.evaluations.get_eval_set(eval_set_id_or_name)

    def list_eval_sets(self) -> List[EvalSet]:
        return self.evaluations.list_eval_sets()

    def update_eval_set_baseline(self, *args: Any, **kwargs: Any) -> EvalSet:
        return self.evaluations.update_eval_set_baseline(*args, **kwargs)

    def list_eval_set_events(self, eval_set_id_or_name: str) -> List[JsonDict]:
        return self.evaluations.list_eval_set_events(eval_set_id_or_name)

    def record_eval_run(self, *args: Any, **kwargs: Any) -> EvalRun:
        return self.evaluations.record_eval_run(*args, **kwargs)

    def get_eval_run(self, run_id: str) -> EvalRun:
        return self.evaluations.get_eval_run(run_id)

    def latest_eval_run(self, *args: Any, **kwargs: Any) -> Optional[EvalRun]:
        return self.evaluations.latest_eval_run(*args, **kwargs)

    def list_eval_runs(self, *args: Any, **kwargs: Any) -> List[EvalRun]:
        return self.evaluations.list_eval_runs(*args, **kwargs)

    # Rollout and rescue

    def create_rollout(
        self,
        version: str,
        strategy: str,
        target_percent: int,
        created_by: str,
        tenant_id: Optional[str] = None,
        channel: str = "fleet",
        runtime_environment_id: Optional[str] = None,
        artifact_uri: Optional[str] = None,
        artifact_hash: Optional[str] = None,
        health_policy: Optional[Dict[str, Any]] = None,
        required_eval_set_id: Optional[str] = None,
    ) -> Rollout:
        if not version:
            raise ValidationError("rollout version is required")
        if tenant_id is not None:
            self.get_tenant(tenant_id)
        channel = (channel or "fleet").strip()
        if not channel:
            raise ValidationError("rollout channel is required")
        strategy_value = _state_value(strategy)
        try:
            RolloutStrategy(strategy_value)
        except ValueError:
            raise ValidationError("unsupported rollout strategy: %s" % strategy_value)
        if int(target_percent) < 0 or int(target_percent) > 100:
            raise ValidationError("rollout target percent must be between 0 and 100")
        if runtime_environment_id is not None:
            self.get_runtime(runtime_environment_id)
        if bool(artifact_uri) != bool(artifact_hash):
            raise ValidationError("artifact_uri and artifact_hash must be provided together")
        if artifact_hash is not None:
            self._validate_artifact_hash(artifact_hash)
        if required_eval_set_id is not None:
            self.get_eval_set(required_eval_set_id)
        policy = ensure_json_object(health_policy)
        now = utcnow()
        rollout_id = new_id("rollout")
        self.store.execute(
            """
            INSERT INTO rollouts (
                id, version, strategy, status, target_percent, tenant_id, channel,
                runtime_environment_id, artifact_uri, artifact_hash, health_policy,
                required_eval_set_id, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rollout_id,
                version,
                strategy_value,
                RolloutStatus.PLANNED.value,
                int(target_percent),
                tenant_id,
                channel,
                runtime_environment_id,
                artifact_uri,
                artifact_hash,
                json_dumps(policy),
                required_eval_set_id,
                created_by,
                now,
                now,
            ),
        )
        self._record_rollout_event(
            rollout_id,
            "rollout.created",
            created_by,
            {
                "target_percent": int(target_percent),
                "tenant_id": tenant_id,
                "channel": channel,
                "runtime_environment_id": runtime_environment_id,
                "artifact_uri": artifact_uri,
                "artifact_hash": artifact_hash,
            },
        )
        if artifact_uri and artifact_hash:
            self._record_rollout_event(
                rollout_id,
                "rollout.artifact_verified",
                created_by,
                {"artifact_uri": artifact_uri, "artifact_hash": artifact_hash},
            )
        return self.get_rollout(rollout_id)

    def get_rollout(self, rollout_id: str) -> Rollout:
        row = self.store.query_one("SELECT * FROM rollouts WHERE id = ?", (rollout_id,))
        if row is None:
            raise NotFoundError("rollout not found: %s" % rollout_id)
        return self._rollout_from_row(row)

    def list_rollout_events(self, rollout_id: str) -> List[JsonDict]:
        self.get_rollout(rollout_id)
        rows = self.store.query_all(
            "SELECT * FROM rollout_events WHERE rollout_id = ? ORDER BY created_at, id",
            (rollout_id,),
        )
        return [
            {
                "id": row["id"],
                "rollout_id": row["rollout_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_rollouts(
        self,
        tenant_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[Rollout]:
        clauses = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        sql = "SELECT * FROM rollouts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [self._rollout_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def verify_rollout_artifact(
        self,
        rollout_id: str,
        artifact_uri: str,
        artifact_hash: str,
        actor: str,
    ) -> Rollout:
        rollout = self.get_rollout(rollout_id)
        if rollout.status not in {RolloutStatus.PLANNED.value, RolloutStatus.PAUSED.value}:
            raise TransitionError("artifact can only be verified before install or while paused")
        if not artifact_uri:
            raise ValidationError("artifact_uri is required")
        self._validate_artifact_hash(artifact_hash)
        now = utcnow()
        self.store.execute(
            """
            UPDATE rollouts
            SET artifact_uri = ?, artifact_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (artifact_uri, artifact_hash, now, rollout_id),
        )
        self._record_rollout_event(
            rollout_id,
            "rollout.artifact_verified",
            actor,
            {"artifact_uri": artifact_uri, "artifact_hash": artifact_hash},
        )
        return self.get_rollout(rollout_id)

    def advance_rollout(
        self,
        rollout_id: str,
        action: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Rollout:
        rollout = self.get_rollout(rollout_id)
        detail = detail or {}
        rule = ROLLOUT_ACTIONS.get(action)
        if rule is None:
            raise ValidationError("unsupported rollout action: %s" % action)
        if rollout.status not in rule["from"]:
            raise TransitionError(
                "rollout action %s not allowed from status %s" % (action, rollout.status)
            )
        if action in {"start_canary", "promote"}:
            self._rollout_install_ready(rollout)
        if (
            action == "promote"
            and rollout.strategy == RolloutStrategy.CANARY.value
            and rollout.status == RolloutStatus.PLANNED.value
        ):
            raise TransitionError("canary rollout must start canary before promotion")
        if (
            action == "promote"
            and rollout.strategy == RolloutStrategy.CANARY.value
            and rollout.status in {RolloutStatus.CANARYING.value, RolloutStatus.PAUSED.value}
            and not self._latest_rollout_health_passed(rollout.id)
        ):
            raise ValidationError("canary promotion requires a passing health gate")
        status = rule["to"]
        if "target_percent" in rule:
            detail.setdefault("target_percent", rule["target_percent"])
        target_percent = int(detail.get("target_percent", rollout.target_percent))
        now = utcnow()

        # The eval gate is read inside the transaction that commits the rollout
        # status change. BEGIN IMMEDIATE blocks concurrent writers (including
        # record_eval_run), so a failing run cannot land between gate-read and
        # commit. The conditional UPDATE on status ensures no other writer
        # advanced the rollout out from under us.
        with self.store.transaction() as conn:
            if action == "promote" and rollout.required_eval_set_id is not None:
                eval_set_row = conn.execute(
                    "SELECT id FROM eval_sets WHERE id = ?",
                    (rollout.required_eval_set_id,),
                ).fetchone()
                if eval_set_row is None:
                    raise ValidationError(
                        "rollout promote blocked: required eval_set %s no longer exists"
                        % rollout.required_eval_set_id
                    )
                run_row = conn.execute(
                    """
                    SELECT id, score, delta, threshold, passed
                    FROM eval_runs
                    WHERE eval_set_id = ? AND target_kind = ? AND target_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        rollout.required_eval_set_id,
                        EvalTargetKind.ROLLOUT_VERSION.value,
                        rollout.version,
                    ),
                ).fetchone()
                if run_row is None:
                    raise ValidationError(
                        "rollout promote requires an eval_run against %s for version %s"
                        % (rollout.required_eval_set_id, rollout.version)
                    )
                if not bool(run_row["passed"]):
                    raise ValidationError(
                        "rollout promote blocked: latest eval_run %s did not pass (score=%s delta=%s threshold=%s)"
                        % (run_row["id"], run_row["score"], run_row["delta"], run_row["threshold"])
                    )
                detail.setdefault("eval_run_id", run_row["id"])
                detail.setdefault("eval_score", run_row["score"])
            cursor = conn.execute(
                """
                UPDATE rollouts
                SET status = ?, target_percent = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (status, target_percent, now, rollout_id, rollout.status),
            )
            if cursor.rowcount != 1:
                raise TransitionError(
                    "rollout %s status changed during advance; retry" % rollout_id
                )
            conn.execute(
                """
                INSERT INTO rollout_events (id, rollout_id, event_type, actor, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("revt"), rollout_id, "rollout.%s" % action, actor, json_dumps(detail), now),
            )
            self.observability.insert_observation(
                conn,
                "log",
                "rollout.%s" % action,
                "control_plane",
                "rollout",
                "info",
                None,
                "",
                "rollout",
                rollout_id,
                {"actor": actor, **detail},
                now,
            )
        return self.get_rollout(rollout_id)

    def evaluate_rollout_health(
        self,
        rollout_id: str,
        checks: Dict[str, Any],
        actor: str,
    ) -> JsonDict:
        rollout = self.get_rollout(rollout_id)
        checks_obj = ensure_json_object(checks)
        required = self._required_rollout_checks(rollout, checks_obj)
        failed = [
            check
            for check in required
            if not self._health_check_passed(checks_obj.get(check))
        ]
        detail = {
            "checks": checks_obj,
            "required_checks": required,
            "failed_checks": failed,
            "status": "failed" if failed else "healthy",
        }
        self._record_rollout_event(rollout_id, "rollout.health_checked", actor, detail)
        if failed:
            # Idempotency: if the rollout is already RESCUING, don't open another
            # rescue task. Record that the additional failure happened and return
            # the in-flight rescue task so callers can act on a stable handle.
            if rollout.status == RolloutStatus.RESCUING.value:
                self._record_rollout_event(
                    rollout_id,
                    "rollout.health_failure_during_rescue",
                    actor,
                    {"failed_checks": failed, "checks": checks_obj},
                )
                in_flight = self._in_flight_rescue_task(rollout_id)
                return {
                    "healthy": False,
                    "failed_checks": failed,
                    "rollout": rollout.to_dict(),
                    "rescue_task": in_flight.to_dict() if in_flight is not None else None,
                }
            rescued, task = self.rescue_rollout(
                rollout_id,
                actor,
                "health gate failed: %s" % ", ".join(failed),
                detail={"failed_checks": failed, "checks": checks_obj},
            )
            return {
                "healthy": False,
                "failed_checks": failed,
                "rollout": rescued.to_dict(),
                "rescue_task": task.to_dict(),
            }
        return {
            "healthy": True,
            "failed_checks": [],
            "rollout": self.get_rollout(rollout_id).to_dict(),
            "rescue_task": None,
        }

    def _in_flight_rescue_task(self, rollout_id: str) -> Optional[Task]:
        """Return the most recent non-terminal rescue task for a rollout, if any."""
        row = self.store.query_one(
            """
            SELECT * FROM tasks
            WHERE project = 'rollout'
              AND state NOT IN (?, ?, ?)
              AND json_extract(metadata, '$.rollout_id') = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                TaskState.COMPLETED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
                rollout_id,
            ),
        )
        return self._task_from_row(row) if row is not None else None

    def rescue_rollout(
        self,
        rollout_id: str,
        actor: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Rollout, Task]:
        rollout = self.get_rollout(rollout_id)
        now = utcnow()
        self.store.execute(
            "UPDATE rollouts SET status = ?, target_percent = ?, updated_at = ? WHERE id = ?",
            (RolloutStatus.RESCUING.value, 0, now, rollout_id),
        )
        rescue_detail = {"reason": reason}
        rescue_detail.update(ensure_json_object(detail))
        self._record_rollout_event(rollout_id, "rollout.rescue_started", actor, rescue_detail)
        task = self.create_task(
            "Rescue rollout %s" % rollout.version,
            description=reason,
            project="rollout",
            priority=100,
            required_capabilities=["ops"],
            metadata={
                "rollout_id": rollout_id,
                "rescue": True,
                "tenant_id": rollout.tenant_id,
                "channel": rollout.channel,
                "failed_checks": rescue_detail.get("failed_checks", []),
            },
            actor=actor,
        )
        self.add_memory(
            task.id,
            "rollout",
            rollout_id,
            "rescue",
            "Rescue path opened for rollout %s: %s" % (rollout.version, reason),
            None,
            actor,
        )
        return self.get_rollout(rollout_id), task

    # Row mapping

    def _task_from_row(self, row: Any) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            project=row["project"],
            priority=row["priority"],
            state=row["state"],
            required_capabilities=json_loads(row["required_capabilities"], []),
            dependencies=json_loads(row["dependencies"], []),
            metadata=json_loads(row["metadata"], {}),
            owner_agent_id=row["owner_agent_id"],
            lease_id=row["lease_id"],
            leased_until=row["leased_until"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _history_from_row(self, row: Any) -> HistoryEvent:
        return HistoryEvent(
            row["id"],
            row["task_id"],
            row["event_type"],
            row["actor"],
            row["from_state"],
            row["to_state"],
            json_loads(row["detail"], {}),
            row["created_at"],
        )

    def _evidence_from_row(self, row: Any) -> Evidence:
        return Evidence(
            row["id"],
            row["task_id"],
            row["kind"],
            row["uri"],
            row["summary"],
            row["checksum"],
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
        )

    def _lease_from_row(self, row: Any) -> Lease:
        return Lease(row["id"], row["task_id"], row["agent_id"], row["expires_at"], row["status"], row["created_at"], row["updated_at"])

    def _machine_from_row(self, row: Any) -> Machine:
        return Machine(
            row["id"],
            row["hostname"],
            json_loads(row["labels"], {}),
            json_loads(row["resources"], {}),
            bool(row["trusted"]),
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
        )

    def _agent_from_row(self, row: Any) -> Agent:
        keys = row.keys() if hasattr(row, "keys") else []
        running_digest = row["running_digest"] if "running_digest" in keys else None
        return Agent(
            row["id"],
            row["machine_id"],
            row["name"],
            json_loads(row["capabilities"], []),
            json_loads(row["resources"], {}),
            row["status"],
            row["health_status"],
            row["current_task_id"],
            running_digest,
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
        )

    def _project_item_from_row(self, row: Any) -> ProjectItem:
        return ProjectItem(
            row["id"],
            row["source"],
            row["external_id"],
            row["title"],
            json_loads(row["payload"], {}),
            row["task_id"],
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    def _rollout_from_row(self, row: Any) -> Rollout:
        keys = row.keys() if hasattr(row, "keys") else []
        required_eval_set_id = row["required_eval_set_id"] if "required_eval_set_id" in keys else None
        return Rollout(
            row["id"],
            row["version"],
            row["strategy"],
            row["status"],
            row["target_percent"],
            row["tenant_id"],
            row["channel"],
            row["runtime_environment_id"],
            row["artifact_uri"],
            row["artifact_hash"],
            json_loads(row["health_policy"], {}),
            required_eval_set_id,
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )

    # Internal helpers

    def _record_history(
        self,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Dict[str, Any],
        conn: Any = None,
    ) -> None:
        when = utcnow()
        writer = conn if conn is not None else self.store
        writer.execute(
            """
            INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("hist"), task_id, event_type, actor, from_state, to_state, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            writer,
            "log",
            event_type,
            "control_plane",
            "task",
            "info",
            None,
            "",
            "task",
            task_id,
            {"actor": actor, "from_state": from_state, "to_state": to_state, **detail},
            when,
        )

    def _record_rollout_event(self, rollout_id: str, event_type: str, actor: str, detail: Dict[str, Any]) -> None:
        when = utcnow()
        self.store.execute(
            """
            INSERT INTO rollout_events (id, rollout_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("revt"), rollout_id, event_type, actor, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            self.store,
            "log",
            event_type,
            "control_plane",
            "rollout",
            "info",
            None,
            "",
            "rollout",
            rollout_id,
            {"actor": actor, **detail},
            when,
        )

    def _rollout_install_ready(self, rollout: Rollout) -> None:
        if not rollout.runtime_environment_id:
            raise ValidationError("rollout requires a runtime environment before install")
        self.get_runtime(rollout.runtime_environment_id)
        if not rollout.artifact_uri or not rollout.artifact_hash:
            raise ValidationError("rollout artifact must be verified before install")
        self._validate_artifact_hash(rollout.artifact_hash)

    def _latest_rollout_health_passed(self, rollout_id: str) -> bool:
        row = self.store.query_one(
            """
            SELECT detail FROM rollout_events
            WHERE rollout_id = ? AND event_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (rollout_id, "rollout.health_checked"),
        )
        if row is None:
            return False
        detail = json_loads(row["detail"], {})
        return detail.get("status") == "healthy"

    def _required_rollout_checks(self, rollout: Rollout, checks: JsonDict) -> List[str]:
        required = rollout.health_policy.get("required_checks")
        if required:
            return [str(check) for check in required]
        return sorted(str(check) for check in checks)

    def _health_check_passed(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"ok", "pass", "passed", "healthy", "success"}
        if isinstance(value, dict):
            status = value.get("status")
            return self._health_check_passed(status)
        return False

    def _validate_artifact_hash(self, artifact_hash: str) -> None:
        if not artifact_hash or not artifact_hash.startswith("sha256:"):
            raise ValidationError("artifact_hash must be a sha256:<digest> value")
        digest = artifact_hash.removeprefix("sha256:")
        if len(digest) < 6:
            raise ValidationError("artifact_hash digest is too short")

    def _dependencies_satisfied(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self.get_task(dep_id)
            if dep.state != TaskState.COMPLETED.value:
                return False
        return True

    def _unblock_ready_tasks(self) -> None:
        for task in self.list_tasks(TaskState.BLOCKED.value):
            if self._dependencies_satisfied(task):
                self.transition_task(task.id, TaskState.OPEN.value, "dispatcher", {"reason": "dependencies satisfied"})

    def _dispatch_ordered_tasks(self) -> List[Task]:
        groups: Dict[str, List[Task]] = {}
        for task in self.list_tasks(TaskState.OPEN.value):
            tenant_key = self._task_tenant_id(task) or ""
            groups.setdefault(tenant_key, []).append(task)
        for tenant_tasks in groups.values():
            tenant_tasks.sort(key=lambda item: (-item.priority, item.created_at, item.id))
        tenant_order = sorted(
            groups,
            key=lambda tenant_id: (-groups[tenant_id][0].priority, tenant_id),
        )
        ordered: List[Task] = []
        while any(groups.values()):
            for tenant_id in tenant_order:
                if groups[tenant_id]:
                    ordered.append(groups[tenant_id].pop(0))
        return ordered

    def _worker_claim_policy(
        self,
        allowed_projects: Optional[Iterable[str]],
        required_metadata: Optional[Dict[str, Any]],
        require_canary: bool,
        dry_run: bool,
    ) -> JsonDict:
        return {
            "allowed_projects": sorted(
                {
                    str(project).strip()
                    for project in (allowed_projects or [])
                    if str(project).strip()
                }
            ),
            "required_metadata": ensure_json_object(required_metadata or {}),
            "require_canary": bool(require_canary),
            "dry_run": bool(dry_run),
        }

    def _task_matches_worker_claim_policy(self, task: Task, policy: JsonDict) -> Tuple[bool, str]:
        allowed_projects = set(policy.get("allowed_projects") or [])
        if allowed_projects and (task.project or "") not in allowed_projects:
            return False, "project_not_allowed"
        metadata = ensure_json_object(task.metadata)
        if policy.get("require_canary") and not (
            metadata.get("canary") is True
            or metadata.get("mac_canary") is True
            or metadata.get("worker_canary") is True
        ):
            return False, "not_canary"
        for key, expected in (policy.get("required_metadata") or {}).items():
            if metadata.get(key) != expected:
                return False, "metadata_mismatch"
        return True, "matched"

    def _available_agents(self) -> List[Agent]:
        rows = self.store.query_all(
            """
            SELECT a.* FROM agents a
            JOIN machines m ON m.id = a.machine_id
            WHERE a.status IN (?, ?) AND a.health_status = ? AND m.trusted = 1
            ORDER BY a.last_seen_at DESC, a.id
            """,
            (AgentStatus.IDLE.value, AgentStatus.BUSY.value, HealthStatus.HEALTHY.value),
        )
        return [self._agent_from_row(row) for row in rows]

    def _agent_available_for(self, agent: Agent, task: Task) -> bool:
        if agent.status not in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}:
            return False
        if agent.health_status != HealthStatus.HEALTHY.value:
            return False
        machine = self.get_machine(agent.machine_id)
        if not machine.trusted:
            return False
        if self._agent_active_lease_count(agent.id) >= self._agent_capacity(agent):
            return False
        if not self._machine_allows_tenant(machine, self._task_tenant_id(task)):
            return False
        if not self._agent_resources_satisfy(agent, machine, task):
            return False
        capabilities = set(agent.capabilities)
        required = set(task.required_capabilities)
        return required.issubset(capabilities)

    def _set_agent_idle(self, agent_id: str, conn: Any = None) -> None:
        now = utcnow()
        writer = conn if conn is not None else self.store
        writer.execute(
            "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
            (AgentStatus.IDLE.value, now, agent_id),
        )

    def _agent_has_active_lease(self, agent_id: str) -> bool:
        row = self.store.query_one(
            """
            SELECT 1 FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
            LIMIT 1
            """,
            (agent_id, LeaseStatus.ACTIVE.value, agent_id),
        )
        return row is not None

    def _agent_active_lease_count(self, agent_id: str) -> int:
        row = self.store.query_one(
            """
            SELECT COUNT(*) AS count FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
            """,
            (agent_id, LeaseStatus.ACTIVE.value, agent_id),
        )
        return int(row["count"] if row is not None else 0)

    def _agent_capacity(self, agent: Agent) -> int:
        for key in ("capacity", "max_concurrent_tasks"):
            value = agent.resources.get(key)
            if value is not None:
                return max(1, int(value))
        return 1

    def _task_tenant_id(self, task: Task) -> Optional[str]:
        origin = task.metadata.get("origin")
        if isinstance(origin, dict) and origin.get("tenant_id"):
            return str(origin["tenant_id"])
        tenant_id = task.metadata.get("tenant_id")
        return str(tenant_id) if tenant_id else None

    def _machine_allows_tenant(self, machine: Machine, tenant_id: Optional[str]) -> bool:
        policy = machine.labels.get("tenant_policy") or {}
        if not isinstance(policy, dict):
            return True
        mode = str(policy.get("mode", "shared"))
        allowed = set(policy.get("tenant_ids") or policy.get("allow_tenants") or [])
        denied = set(policy.get("deny_tenants") or [])
        if mode == "denied":
            return False
        if tenant_id is None:
            return mode != "private"
        if tenant_id in denied:
            return False
        if mode == "private":
            return tenant_id in allowed
        if allowed:
            return tenant_id in allowed
        return True

    def _agent_resources_satisfy(self, agent: Agent, machine: Machine, task: Task) -> bool:
        required = task.metadata.get("resources") or task.metadata.get("required_resources") or {}
        if not isinstance(required, dict):
            return True
        available = dict(machine.resources)
        available.update(agent.resources)
        for key, needed in required.items():
            current = available.get(key)
            if isinstance(needed, (int, float)):
                if current is None or float(current) < float(needed):
                    return False
            elif isinstance(needed, list):
                if not set(needed).issubset(set(current or [])):
                    return False
            elif needed is not None and current != needed:
                return False
        return True

    def _expire_agent_active_leases(self, agent_id: str, timestamp: str, reason: str) -> None:
        rows = self.store.query_all(
            """
            SELECT
                l.id AS lease_id,
                l.task_id AS task_id,
                t.state AS task_state,
                t.attempt_count AS attempt_count,
                t.max_attempts AS max_attempts
            FROM leases l
            JOIN tasks t ON t.lease_id = l.id
            WHERE l.agent_id = ?
              AND l.status = ?
              AND t.owner_agent_id = ?
            ORDER BY l.created_at, l.id
            """,
            (agent_id, LeaseStatus.ACTIVE.value, agent_id),
        )
        if not rows:
            return
        with self.store.transaction() as conn:
            for row in rows:
                next_state = (
                    TaskState.FAILED.value
                    if row["attempt_count"] >= row["max_attempts"]
                    else TaskState.OPEN.value
                )
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ?",
                    (LeaseStatus.EXPIRED.value, timestamp, row["lease_id"]),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL, updated_at = ?
                    WHERE id = ? AND lease_id = ?
                    """,
                    (next_state, timestamp, row["task_id"], row["lease_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("hist"),
                        row["task_id"],
                        "task.lease_expired",
                        "dispatcher",
                        row["task_state"],
                        next_state,
                        json_dumps(
                            {
                                "lease_id": row["lease_id"],
                                "agent_id": agent_id,
                                "reason": reason,
                            }
                        ),
                        timestamp,
                    ),
                )
                self.observability.insert_observation(
                    conn,
                    "log",
                    "task.lease_expired",
                    "control_plane",
                    "task",
                    "warning",
                    None,
                    "",
                    "task",
                    row["task_id"],
                    {
                        "actor": "dispatcher",
                        "from_state": row["task_state"],
                        "to_state": next_state,
                        "lease_id": row["lease_id"],
                        "agent_id": agent_id,
                        "reason": reason,
                    },
                    timestamp,
                )

