from __future__ import annotations

import base64
import hashlib
import os
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mac.models import (
    Agent,
    AgentMessage,
    AgentStatus,
    AuthorizationError,
    EVIDENCE_KINDS,
    EvalRun,
    EvalScoringDirection,
    EvalSet,
    EvalTargetKind,
    Evidence,
    HealthStatus,
    HistoryEvent,
    HermesInstance,
    HermesInstanceStatus,
    JsonDict,
    Lease,
    LeaseStatus,
    MACError,
    Machine,
    MemoryRecord,
    MessageStatus,
    MessageType,
    NotFoundError,
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
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
    validate_transition,
)
from mac.store import SQLiteStore


FORBIDDEN_MESSAGE_KEYS = {
    "argv",
    "cmd",
    "code",
    "command",
    "exec",
    "executable",
    "powershell",
    "script",
    "shell",
}

MESSAGE_TYPE_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    MessageType.HELP_REQUEST.value: ("question",),
    MessageType.EVIDENCE_REQUEST.value: ("task_id",),
    MessageType.STATUS_UPDATE.value: ("status",),
    MessageType.REVIEW_REQUEST.value: ("task_id", "review_id"),
    MessageType.REVIEW_RESULT.value: ("task_id", "status"),
    MessageType.NUDGE.value: ("task_id",),
    MessageType.DECISION_RECORD.value: ("summary",),
}

SECRET_FIELD_HINTS = ("secret", "token", "password", "private_key", "credential", "api_key", "auth")

SECRET_HANDLE_DEFAULT_TTL_SECONDS = 300


def _hash_manifest(manifest: JsonDict) -> str:
    return hashlib.sha256(json_dumps(manifest).encode("utf-8")).hexdigest()


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
        fernet_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"mac.control_plane.secrets.v1",
            info=b"fernet-key",
        ).derive(raw_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(fernet_key))

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

    # Human-facing identity and Hermes boundary

    def register_tenant(
        self,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Tenant:
        name = name.strip()
        if not name:
            raise ValidationError("tenant name is required")
        existing = self.store.query_one("SELECT id FROM tenants WHERE name = ?", (name,))
        if existing is not None and tenant_id is None:
            tenant_id = existing["id"]
        now = utcnow()
        tid = tenant_id or new_id("tenant")
        metadata_json = self._resolved_json_column("tenants", "metadata", tid, metadata)
        self.store.execute(
            """
            INSERT INTO tenants (id, name, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (tid, name, metadata_json, now, now),
        )
        return self.get_tenant(tid)

    def get_tenant(self, tenant_id_or_name: str) -> Tenant:
        row = self.store.query_one(
            "SELECT * FROM tenants WHERE id = ? OR name = ?",
            (tenant_id_or_name, tenant_id_or_name),
        )
        if row is None:
            raise NotFoundError("tenant not found: %s" % tenant_id_or_name)
        return self._tenant_from_row(row)

    def list_tenants(self) -> List[Tenant]:
        rows = self.store.query_all("SELECT * FROM tenants ORDER BY name")
        return [self._tenant_from_row(row) for row in rows]

    def register_user(
        self,
        tenant_id: str,
        handle: str,
        display_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> User:
        self.get_tenant(tenant_id)
        handle = handle.strip()
        if not handle:
            raise ValidationError("user handle is required")
        existing = self.store.query_one(
            "SELECT id FROM users WHERE tenant_id = ? AND handle = ?",
            (tenant_id, handle),
        )
        if existing is not None and user_id is None:
            user_id = existing["id"]
        now = utcnow()
        uid = user_id or new_id("user")
        metadata_json = self._resolved_json_column("users", "metadata", uid, metadata)
        self.store.execute(
            """
            INSERT INTO users (id, tenant_id, handle, display_name, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                handle = excluded.handle,
                display_name = excluded.display_name,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                uid,
                tenant_id,
                handle,
                display_name or handle,
                metadata_json,
                now,
                now,
            ),
        )
        return self.get_user(uid)

    def get_user(self, user_id: str) -> User:
        row = self.store.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError("user not found: %s" % user_id)
        return self._user_from_row(row)

    def list_users(self, tenant_id: Optional[str] = None) -> List[User]:
        if tenant_id:
            rows = self.store.query_all(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY handle",
                (tenant_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM users ORDER BY tenant_id, handle")
        return [self._user_from_row(row) for row in rows]

    def register_persona(
        self,
        tenant_id: str,
        name: str,
        soul_ref: str,
        memory_scope: str,
        metadata: Optional[Dict[str, Any]] = None,
        persona_id: Optional[str] = None,
    ) -> Persona:
        self.get_tenant(tenant_id)
        if not name.strip():
            raise ValidationError("persona name is required")
        if not soul_ref.strip():
            raise ValidationError("persona soul_ref is required")
        if not memory_scope.strip():
            raise ValidationError("persona memory_scope is required")
        name = name.strip()
        existing = self.store.query_one(
            "SELECT id FROM personas WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        if existing is not None and persona_id is None:
            persona_id = existing["id"]
        now = utcnow()
        pid = persona_id or new_id("persona")
        metadata_json = self._resolved_json_column("personas", "metadata", pid, metadata)
        self.store.execute(
            """
            INSERT INTO personas (
                id, tenant_id, name, soul_ref, memory_scope, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                name = excluded.name,
                soul_ref = excluded.soul_ref,
                memory_scope = excluded.memory_scope,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                pid,
                tenant_id,
                name,
                soul_ref.strip(),
                memory_scope.strip(),
                metadata_json,
                now,
                now,
            ),
        )
        return self.get_persona(pid)

    def get_persona(self, persona_id: str) -> Persona:
        row = self.store.query_one("SELECT * FROM personas WHERE id = ?", (persona_id,))
        if row is None:
            raise NotFoundError("persona not found: %s" % persona_id)
        return self._persona_from_row(row)

    def list_personas(self, tenant_id: Optional[str] = None) -> List[Persona]:
        if tenant_id:
            rows = self.store.query_all(
                "SELECT * FROM personas WHERE tenant_id = ? ORDER BY name",
                (tenant_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM personas ORDER BY tenant_id, name")
        return [self._persona_from_row(row) for row in rows]

    def register_hermes_instance(
        self,
        tenant_id: str,
        name: str,
        persona_id: Optional[str] = None,
        home_ref: str = "",
        status: str = HermesInstanceStatus.ACTIVE.value,
        metadata: Optional[Dict[str, Any]] = None,
        instance_id: Optional[str] = None,
    ) -> HermesInstance:
        self.get_tenant(tenant_id)
        if persona_id:
            persona = self.get_persona(persona_id)
            if persona.tenant_id != tenant_id:
                raise ValidationError("persona must belong to hermes instance tenant")
        name = name.strip()
        if not name:
            raise ValidationError("hermes instance name is required")
        existing = self.store.query_one(
            "SELECT id FROM hermes_instances WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        if existing is not None and instance_id is None:
            instance_id = existing["id"]
        status_value = _state_value(status)
        try:
            HermesInstanceStatus(status_value)
        except ValueError:
            raise ValidationError("unsupported hermes instance status: %s" % status_value)
        now = utcnow()
        hid = instance_id or new_id("hermes")
        metadata_json = self._resolved_json_column("hermes_instances", "metadata", hid, metadata)
        self.store.execute(
            """
            INSERT INTO hermes_instances (
                id, tenant_id, name, persona_id, home_ref, status,
                metadata, created_at, updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                name = excluded.name,
                persona_id = excluded.persona_id,
                home_ref = excluded.home_ref,
                status = excluded.status,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                hid,
                tenant_id,
                name,
                persona_id,
                home_ref,
                status_value,
                metadata_json,
                now,
                now,
                now,
            ),
        )
        return self.get_hermes_instance(hid)

    def get_hermes_instance(self, instance_id: str) -> HermesInstance:
        row = self.store.query_one("SELECT * FROM hermes_instances WHERE id = ?", (instance_id,))
        if row is None:
            raise NotFoundError("hermes instance not found: %s" % instance_id)
        return self._hermes_instance_from_row(row)

    def list_hermes_instances(self, tenant_id: Optional[str] = None) -> List[HermesInstance]:
        if tenant_id:
            rows = self.store.query_all(
                "SELECT * FROM hermes_instances WHERE tenant_id = ? ORDER BY name",
                (tenant_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM hermes_instances ORDER BY tenant_id, name")
        return [self._hermes_instance_from_row(row) for row in rows]

    def register_platform_binding(
        self,
        tenant_id: str,
        hermes_instance_id: str,
        platform: str,
        external_id: str,
        display_name: str = "",
        scopes: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        binding_id: Optional[str] = None,
    ) -> PlatformBinding:
        self.get_tenant(tenant_id)
        instance = self.get_hermes_instance(hermes_instance_id)
        if instance.tenant_id != tenant_id:
            raise ValidationError("platform binding must belong to hermes instance tenant")
        if not platform.strip() or not external_id.strip():
            raise ValidationError("platform and external_id are required")
        platform = platform.strip()
        external_id = external_id.strip()
        existing = self.store.query_one(
            "SELECT id FROM platform_bindings WHERE tenant_id = ? AND platform = ? AND external_id = ?",
            (tenant_id, platform, external_id),
        )
        if existing is not None and binding_id is None:
            binding_id = existing["id"]
        now = utcnow()
        bid = binding_id or new_id("binding")
        scopes_json = self._resolved_json_column("platform_bindings", "scopes", bid, scopes)
        metadata_json = self._resolved_json_column("platform_bindings", "metadata", bid, metadata)
        self.store.execute(
            """
            INSERT INTO platform_bindings (
                id, tenant_id, hermes_instance_id, platform, external_id,
                display_name, scopes, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                hermes_instance_id = excluded.hermes_instance_id,
                platform = excluded.platform,
                external_id = excluded.external_id,
                display_name = excluded.display_name,
                scopes = excluded.scopes,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                bid,
                tenant_id,
                hermes_instance_id,
                platform,
                external_id,
                display_name or external_id,
                scopes_json,
                metadata_json,
                now,
                now,
            ),
        )
        return self.get_platform_binding(bid)

    def get_platform_binding(self, binding_id: str) -> PlatformBinding:
        row = self.store.query_one("SELECT * FROM platform_bindings WHERE id = ?", (binding_id,))
        if row is None:
            raise NotFoundError("platform binding not found: %s" % binding_id)
        return self._platform_binding_from_row(row)

    def list_platform_bindings(
        self,
        tenant_id: Optional[str] = None,
        hermes_instance_id: Optional[str] = None,
    ) -> List[PlatformBinding]:
        clauses = []
        params: List[Any] = []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if hermes_instance_id:
            clauses.append("hermes_instance_id = ?")
            params.append(hermes_instance_id)
        sql = "SELECT * FROM platform_bindings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY platform, external_id"
        rows = self.store.query_all(sql, tuple(params))
        return [self._platform_binding_from_row(row) for row in rows]

    def hermes_context(self, hermes_instance_id: str) -> JsonDict:
        instance = self.get_hermes_instance(hermes_instance_id)
        persona = self.get_persona(instance.persona_id) if instance.persona_id else None
        return {
            "tenant": self.get_tenant(instance.tenant_id).to_dict(),
            "hermes_instance": instance.to_dict(),
            "persona": persona.to_dict() if persona else None,
            "platform_bindings": [
                binding.to_dict()
                for binding in self.list_platform_bindings(
                    tenant_id=instance.tenant_id,
                    hermes_instance_id=instance.id,
                )
            ],
            "memory_contract": {
                "personality_authority": "hermes",
                "user_memory_authority": "hermes",
                "operational_provenance_authority": "mac",
                "soul_ref": persona.soul_ref if persona else None,
                "memory_scope": persona.memory_scope if persona else None,
            },
        }

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
        publications = [
            self._publication_from_row(row).to_dict()
            for row in self.store.query_all(
                "SELECT * FROM publications WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            )
        ]
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
        if target == TaskState.COMPLETED.value and not self._completion_authorized(task_id):
            raise ValidationError("task completion requires approved review and evidence")
        now = utcnow()
        owner_agent_id = task.owner_agent_id
        lease_id = task.lease_id
        leased_until = task.leased_until
        if target in {TaskState.OPEN.value, TaskState.FAILED.value, TaskState.CANCELLED.value}:
            owner_agent_id = None
            lease_id = None
            leased_until = None
        self.store.execute(
            """
            UPDATE tasks
            SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?, updated_at = ?
            WHERE id = ?
            """,
            (target, owner_agent_id, lease_id, leased_until, now, task_id),
        )
        if task.owner_agent_id and target in TERMINAL_TASK_STATES.union({TaskState.OPEN.value}):
            self._set_agent_idle(task.owner_agent_id)
        self._record_history(task_id, "task.transitioned", actor, task.state, target, detail or {})
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
        self.store.execute(
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
        if status_value == AgentStatus.IDLE.value and self._agent_has_active_lease(agent_id):
            raise ValidationError("agent cannot report idle while holding an active lease")
        if status_value == AgentStatus.OFFLINE.value:
            self._expire_agent_active_leases(agent_id, now, "heartbeat_offline")
        if status_value in {AgentStatus.IDLE.value, AgentStatus.OFFLINE.value}:
            updates.append("current_task_id = NULL")
        params.append(agent_id)
        self.store.execute("UPDATE agents SET %s WHERE id = ?" % ", ".join(updates), tuple(params))
        return self.get_agent(agent_id)

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

    def send_message(
        self,
        sender_agent_id: str,
        recipient_agent_id: Optional[str],
        message_type: str,
        payload: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> AgentMessage:
        if sender_agent_id != "dispatcher":
            self.get_agent(sender_agent_id)
        if recipient_agent_id is not None:
            self.get_agent(recipient_agent_id)
        if task_id is not None:
            self.get_task(task_id)
        message_type_value = _state_value(message_type)
        try:
            MessageType(message_type_value)
        except ValueError:
            raise ValidationError("unsupported message type: %s" % message_type)
        self._validate_message_payload(message_type_value, payload)
        now = utcnow()
        message_id = new_id("msg")
        self.store.execute(
            """
            INSERT INTO messages (
                id, sender_agent_id, recipient_agent_id, task_id, message_type,
                payload, status, created_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                message_id,
                sender_agent_id,
                recipient_agent_id,
                task_id,
                _state_value(message_type),
                json_dumps(payload),
                MessageStatus.QUEUED.value,
                now,
            ),
        )
        return self.get_message(message_id)

    def get_message(self, message_id: str) -> AgentMessage:
        row = self.store.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if row is None:
            raise NotFoundError("message not found: %s" % message_id)
        return self._message_from_row(row)

    def deliver_messages(self, agent_id: str, limit: int = 50) -> List[AgentMessage]:
        self.get_agent(agent_id)
        rows = self.store.query_all(
            """
            SELECT * FROM messages
            WHERE status = ? AND (recipient_agent_id = ? OR recipient_agent_id IS NULL)
            ORDER BY created_at, id
            LIMIT ?
            """,
            (MessageStatus.QUEUED.value, agent_id, int(limit)),
        )
        now = utcnow()
        messages = []
        for row in rows:
            message = self._message_from_row(row)
            self.store.execute(
                "UPDATE messages SET status = ?, delivered_at = ? WHERE id = ?",
                (MessageStatus.DELIVERED.value, now, message.id),
            )
            messages.append(self.get_message(message.id))
        return messages

    def list_messages(self, agent_id: Optional[str] = None) -> List[AgentMessage]:
        if agent_id:
            rows = self.store.query_all(
                "SELECT * FROM messages WHERE recipient_agent_id = ? ORDER BY created_at, id",
                (agent_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM messages ORDER BY created_at, id")
        return [self._message_from_row(row) for row in rows]

    # Reviews and publication

    def request_review(self, task_id: str, reviewer_agent_id: str, actor: str = "dispatcher") -> Review:
        task = self.get_task(task_id)
        self.get_agent(reviewer_agent_id)
        if self._agent_has_owned_task(task_id, reviewer_agent_id):
            raise AuthorizationError(
                "reviewer cannot be a prior or current owner of the reviewed task"
            )
        if task.state == TaskState.NEEDS_REVIEW.value:
            self.transition_task(task_id, TaskState.REVIEWING.value, actor, {"reviewer_agent_id": reviewer_agent_id})
        elif task.state != TaskState.REVIEWING.value:
            raise TransitionError("task must need review before requesting review")
        now = utcnow()
        review_id = new_id("review")
        self.store.execute(
            """
            INSERT INTO reviews (id, task_id, reviewer_agent_id, status, reason, evidence_id, created_at, completed_at)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
            """,
            (review_id, task_id, reviewer_agent_id, ReviewStatus.PENDING.value, now),
        )
        self.send_message(
            "dispatcher",
            reviewer_agent_id,
            MessageType.REVIEW_REQUEST.value,
            {"task_id": task_id, "review_id": review_id},
            task_id=task_id,
        )
        self._record_history(task_id, "task.review_requested", actor, None, None, {"review_id": review_id})
        return self.get_review(review_id)

    def submit_review(
        self,
        review_id: str,
        status: str,
        reviewer_agent_id: str,
        reason: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> Review:
        review = self.get_review(review_id)
        if review.reviewer_agent_id != reviewer_agent_id:
            raise AuthorizationError("reviewer does not own review")
        if review.status != ReviewStatus.PENDING.value:
            raise ValidationError("review is already completed")
        status_value = _state_value(status)
        if status_value not in {
            ReviewStatus.APPROVED.value,
            ReviewStatus.CHANGES_REQUESTED.value,
            ReviewStatus.REJECTED.value,
        }:
            raise ValidationError("unsupported review decision: %s" % status_value)
        if status_value == ReviewStatus.APPROVED.value and evidence_id is None:
            raise ValidationError("approving a review requires an evidence_id")
        if evidence_id is not None:
            evidence = self.get_evidence(evidence_id)
            if evidence.task_id != review.task_id:
                raise ValidationError("review evidence must belong to reviewed task")
        now = utcnow()
        self.store.execute(
            """
            UPDATE reviews
            SET status = ?, reason = ?, evidence_id = ?, completed_at = ?
            WHERE id = ?
            """,
            (status_value, reason, evidence_id, now, review_id),
        )
        self._record_history(
            review.task_id,
            "task.review_completed",
            reviewer_agent_id,
            None,
            None,
            {"review_id": review_id, "status": status_value, "reason": reason},
        )
        if status_value in {ReviewStatus.CHANGES_REQUESTED.value, ReviewStatus.REJECTED.value}:
            self.transition_task(review.task_id, TaskState.RUNNING.value, reviewer_agent_id, {"review_id": review_id})
        return self.get_review(review_id)

    def get_review(self, review_id: str) -> Review:
        row = self.store.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if row is None:
            raise NotFoundError("review not found: %s" % review_id)
        return self._review_from_row(row)

    def list_reviews(self, task_id: str) -> List[Review]:
        rows = self.store.query_all(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        )
        return [self._review_from_row(row) for row in rows]

    def publish_task(
        self,
        task_id: str,
        target: str,
        created_by: str,
        evidence_id: Optional[str] = None,
    ) -> Publication:
        task = self.get_task(task_id)
        if task.state != TaskState.REVIEWING.value:
            raise TransitionError("task must be in review before publication")
        if not self._completion_authorized(task_id):
            raise ValidationError("publication requires approved review and evidence")
        content_hash = None
        if self._task_requires_publication_evidence(task) and evidence_id is None:
            raise ValidationError("publication policy requires publication evidence")
        if evidence_id is not None:
            evidence = self.get_evidence(evidence_id)
            if evidence.task_id != task_id:
                raise ValidationError("publication evidence must belong to task")
            if self._task_requires_publication_evidence(task):
                if evidence.kind != "publication":
                    raise ValidationError("publication policy requires publication evidence")
                if not evidence.checksum:
                    raise ValidationError("publication evidence requires a checksum")
            content_hash = evidence.checksum
        owner_agent_id = task.owner_agent_id
        now = utcnow()
        publication_id = new_id("pub")
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (TaskState.COMPLETED.value, now, task_id, TaskState.REVIEWING.value),
            )
            if cursor.rowcount != 1:
                raise TransitionError("task state changed during publish; retry")
            conn.execute(
                """
                INSERT INTO publications (id, task_id, target, status, evidence_id, content_hash, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    task_id,
                    target,
                    PublicationStatus.PUBLISHED.value,
                    evidence_id,
                    content_hash,
                    created_by,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("hist"),
                    task_id,
                    "task.published",
                    created_by,
                    None,
                    None,
                    json_dumps({"publication_id": publication_id, "target": target}),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("hist"),
                    task_id,
                    "task.transitioned",
                    created_by,
                    TaskState.REVIEWING.value,
                    TaskState.COMPLETED.value,
                    json_dumps({"publication_id": publication_id}),
                    now,
                ),
            )
            if owner_agent_id:
                conn.execute(
                    "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
                    (AgentStatus.IDLE.value, now, owner_agent_id),
                )
        return self.get_publication(publication_id)

    def get_publication(self, publication_id: str) -> Publication:
        row = self.store.query_one("SELECT * FROM publications WHERE id = ?", (publication_id,))
        if row is None:
            raise NotFoundError("publication not found: %s" % publication_id)
        return self._publication_from_row(row)

    # Secrets boundary

    def create_secret(
        self,
        name: str,
        value: str,
        scopes: Dict[str, Any],
        created_by: str,
    ) -> SecretRecord:
        if not name or not value:
            raise ValidationError("secret name and value are required")
        if not scopes:
            raise ValidationError("secret scopes are required")
        now = utcnow()
        secret_id = new_id("secret")
        self.store.execute(
            """
            INSERT INTO secrets (id, name, scopes, ciphertext, created_by, created_at, updated_at, rotated_at, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (secret_id, name, json_dumps(scopes), self._encrypt(value), created_by, now, now),
        )
        return self.get_secret(secret_id)

    def get_secret(self, secret_id_or_name: str) -> SecretRecord:
        row = self.store.query_one(
            "SELECT * FROM secrets WHERE id = ? OR name = ?",
            (secret_id_or_name, secret_id_or_name),
        )
        if row is None:
            raise NotFoundError("secret not found: %s" % secret_id_or_name)
        return self._secret_from_row(row)

    def list_secrets(self) -> List[SecretRecord]:
        rows = self.store.query_all("SELECT * FROM secrets ORDER BY name")
        return [self._secret_from_row(row) for row in rows]

    def request_secret(
        self,
        secret_id_or_name: str,
        accessor_agent_id: str,
        purpose: str,
        ttl_seconds: int = SECRET_HANDLE_DEFAULT_TTL_SECONDS,
    ) -> SecretHandle:
        secret = self.get_secret(secret_id_or_name)
        agent = self.get_agent(accessor_agent_id)
        machine = self.get_machine(agent.machine_id)
        granted = bool(
            secret.enabled
            and machine.trusted
            and self._secret_scope_allows(secret.scopes, agent)
        )
        expires_at = None
        if granted:
            ttl = max(1, int(ttl_seconds))
            expires_at = (parse_time(utcnow()) + timedelta(seconds=ttl)).isoformat(timespec="microseconds")
        audit = self._record_secret_access(
            secret.id,
            accessor_agent_id,
            purpose,
            SecretAuditResult.GRANTED.value if granted else SecretAuditResult.DENIED.value,
            expires_at=expires_at,
        )
        if not granted:
            raise AuthorizationError("secret access denied")
        return SecretHandle(secret.id, audit.id, "secret://%s#%s" % (secret.id, audit.id), True)

    def rotate_secret(self, secret_id_or_name: str, value: str, actor: str) -> SecretRecord:
        if not value:
            raise ValidationError("rotation requires a new secret value")
        secret = self.get_secret(secret_id_or_name)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE secrets SET ciphertext = ?, updated_at = ?, rotated_at = ? WHERE id = ?",
                (self._encrypt(value), now, now, secret.id),
            )
            conn.execute(
                """
                INSERT INTO secret_access_audit (
                    id, secret_id, accessor_agent_id, purpose, result, expires_at, revealed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    new_id("audit"),
                    secret.id,
                    actor or "unspecified",
                    "rotate",
                    SecretAuditResult.ROTATED.value,
                    now,
                ),
            )
        return self.get_secret(secret.id)

    def list_secret_audits(self, secret_id: Optional[str] = None) -> List[SecretAccess]:
        if secret_id:
            rows = self.store.query_all(
                "SELECT * FROM secret_access_audit WHERE secret_id = ? ORDER BY created_at, id",
                (secret_id,),
            )
        else:
            rows = self.store.query_all("SELECT * FROM secret_access_audit ORDER BY created_at, id")
        return [self._secret_access_from_row(row) for row in rows]

    def reveal_secret(self, secret_id: str, audit_id: str, accessor_agent_id: str) -> str:
        """Single-use, time-limited secret reveal.

        The grant audit row must (1) name the same agent that is asking, (2) still
        be within its TTL, and (3) not already have been revealed. On success the
        audit row is marked revealed so the same handle cannot be redeemed twice.
        """
        now = utcnow()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE secret_access_audit
                SET revealed_at = ?
                WHERE id = ?
                  AND secret_id = ?
                  AND accessor_agent_id = ?
                  AND result = ?
                  AND revealed_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    now,
                    audit_id,
                    secret_id,
                    accessor_agent_id,
                    SecretAuditResult.GRANTED.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise AuthorizationError("secret handle is expired, already used, or not granted to this agent")
            row = conn.execute(
                "SELECT ciphertext FROM secrets WHERE id = ? AND enabled = 1",
                (secret_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("secret not found or disabled: %s" % secret_id)
        return self._decrypt(row["ciphertext"])

    # Runtime boundary

    def create_runtime(self, name: str, manifest: Dict[str, Any], created_by: str) -> RuntimeEnvironment:
        if not name:
            raise ValidationError("runtime name is required")
        manifest_dict = ensure_json_object(manifest)
        self._validate_runtime_manifest(manifest_dict)
        now = utcnow()
        runtime_id = new_id("runtime")
        digest = _hash_manifest(manifest_dict)
        self.store.execute(
            """
            INSERT INTO runtime_environments (id, name, manifest, digest, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (runtime_id, name, json_dumps(manifest_dict), digest, created_by, now),
        )
        return self.get_runtime(runtime_id)

    def get_runtime(self, runtime_id_or_name: str) -> RuntimeEnvironment:
        row = self.store.query_one(
            "SELECT * FROM runtime_environments WHERE id = ? OR name = ?",
            (runtime_id_or_name, runtime_id_or_name),
        )
        if row is None:
            raise NotFoundError("runtime not found: %s" % runtime_id_or_name)
        return self._runtime_from_row(row)

    def list_runtimes(self) -> List[RuntimeEnvironment]:
        rows = self.store.query_all("SELECT * FROM runtime_environments ORDER BY name")
        return [self._runtime_from_row(row) for row in rows]

    def create_runtime_run(self, task_id: str, agent_id: str, environment_id: str) -> RuntimeRun:
        self.get_task(task_id)
        self.get_agent(agent_id)
        runtime = self.get_runtime(environment_id)
        now = utcnow()
        run_id = new_id("run")
        self.store.execute(
            """
            INSERT INTO runtime_runs (id, task_id, agent_id, environment_id, status, evidence_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (run_id, task_id, agent_id, runtime.id, "running", now, now),
        )
        return self.get_runtime_run(run_id)

    def complete_runtime_run(self, run_id: str, evidence_id: str, status: str = "completed") -> RuntimeRun:
        run = self.get_runtime_run(run_id)
        evidence = self.get_evidence(evidence_id)
        if evidence.task_id != run.task_id:
            raise ValidationError("runtime evidence must belong to run task")
        now = utcnow()
        self.store.execute(
            "UPDATE runtime_runs SET status = ?, evidence_id = ?, updated_at = ? WHERE id = ?",
            (status, evidence_id, now, run_id),
        )
        return self.get_runtime_run(run_id)

    def get_runtime_run(self, run_id: str) -> RuntimeRun:
        row = self.store.query_one("SELECT * FROM runtime_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError("runtime run not found: %s" % run_id)
        return self._runtime_run_from_row(row)

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

    # Memory and provenance

    def add_memory(
        self,
        task_id: Optional[str],
        subject_type: str,
        subject_id: Optional[str],
        record_type: str,
        content: str,
        evidence_id: Optional[str],
        created_by: str,
    ) -> MemoryRecord:
        if task_id is not None:
            self.get_task(task_id)
        if evidence_id is not None:
            self.get_evidence(evidence_id)
        if not subject_type or not record_type or not content:
            raise ValidationError("memory requires subject_type, record_type, and content")
        now = utcnow()
        memory_id = new_id("mem")
        self.store.execute(
            """
            INSERT INTO memory_records (
                id, task_id, subject_type, subject_id, record_type, content, evidence_id, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, task_id, subject_type, subject_id, record_type, content, evidence_id, created_by, now),
        )
        if task_id:
            self._record_history(
                task_id,
                "task.memory_recorded",
                created_by,
                None,
                None,
                {"memory_id": memory_id, "record_type": record_type},
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> MemoryRecord:
        row = self.store.query_one("SELECT * FROM memory_records WHERE id = ?", (memory_id,))
        if row is None:
            raise NotFoundError("memory record not found: %s" % memory_id)
        return self._memory_from_row(row)

    def search_memory(
        self,
        task_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
    ) -> List[MemoryRecord]:
        clauses = []
        params: List[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if subject_type:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        sql = "SELECT * FROM memory_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        rows = self.store.query_all(sql, tuple(params))
        return [self._memory_from_row(row) for row in rows]

    # Evaluation

    def create_eval_set(
        self,
        name: str,
        scoring: str = EvalScoringDirection.HIGHER_IS_BETTER.value,
        description: str = "",
        baseline_score: Optional[float] = None,
        regression_threshold: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        created_by: str = "human",
    ) -> EvalSet:
        name = (name or "").strip()
        if not name:
            raise ValidationError("eval_set name is required")
        scoring_value = _state_value(scoring)
        try:
            EvalScoringDirection(scoring_value)
        except ValueError:
            raise ValidationError("unsupported eval scoring direction: %s" % scoring_value)
        if regression_threshold < 0:
            raise ValidationError("regression_threshold must be >= 0")
        now = utcnow()
        eval_id = new_id("evalset")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO eval_sets (
                    id, name, description, scoring, baseline_score, regression_threshold,
                    metadata, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eval_id,
                    name,
                    description,
                    scoring_value,
                    None if baseline_score is None else float(baseline_score),
                    float(regression_threshold),
                    json_dumps(ensure_json_object(metadata)),
                    created_by,
                    now,
                    now,
                ),
            )
            self._insert_eval_set_event(
                conn,
                eval_id,
                "eval_set.created",
                created_by,
                {
                    "scoring": scoring_value,
                    "baseline_score": baseline_score,
                    "regression_threshold": float(regression_threshold),
                },
                now,
            )
        return self.get_eval_set(eval_id)

    def get_eval_set(self, eval_set_id_or_name: str) -> EvalSet:
        row = self.store.query_one(
            "SELECT * FROM eval_sets WHERE id = ? OR name = ?",
            (eval_set_id_or_name, eval_set_id_or_name),
        )
        if row is None:
            raise NotFoundError("eval_set not found: %s" % eval_set_id_or_name)
        return self._eval_set_from_row(row)

    def list_eval_sets(self) -> List[EvalSet]:
        rows = self.store.query_all("SELECT * FROM eval_sets ORDER BY name")
        return [self._eval_set_from_row(row) for row in rows]

    def update_eval_set_baseline(
        self,
        eval_set_id_or_name: str,
        baseline_score: float,
        actor: str = "human",
    ) -> EvalSet:
        eval_set = self.get_eval_set(eval_set_id_or_name)
        new_baseline = float(baseline_score)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE eval_sets SET baseline_score = ?, updated_at = ? WHERE id = ?",
                (new_baseline, now, eval_set.id),
            )
            # Frozen `passed` on prior runs does NOT auto-recompute. Operators
            # weakening or tightening the gate need this trail to explain why
            # historical runs read differently from a re-evaluation.
            self._insert_eval_set_event(
                conn,
                eval_set.id,
                "eval_set.baseline_changed",
                actor,
                {
                    "previous_baseline_score": eval_set.baseline_score,
                    "new_baseline_score": new_baseline,
                },
                now,
            )
        return self.get_eval_set(eval_set.id)

    def list_eval_set_events(self, eval_set_id_or_name: str) -> List[JsonDict]:
        eval_set = self.get_eval_set(eval_set_id_or_name)
        rows = self.store.query_all(
            "SELECT * FROM eval_set_events WHERE eval_set_id = ? ORDER BY created_at, id",
            (eval_set.id,),
        )
        return [
            {
                "id": row["id"],
                "eval_set_id": row["eval_set_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_eval_run(
        self,
        eval_set_id_or_name: str,
        target_kind: str,
        target_id: str,
        score: float,
        detail: Optional[Dict[str, Any]] = None,
        evidence_id: Optional[str] = None,
        created_by: str = "human",
    ) -> EvalRun:
        eval_set = self.get_eval_set(eval_set_id_or_name)
        target_kind_value = _state_value(target_kind)
        try:
            EvalTargetKind(target_kind_value)
        except ValueError:
            raise ValidationError("unsupported eval target_kind: %s" % target_kind_value)
        if not target_id:
            raise ValidationError("eval run target_id is required")
        if evidence_id is not None:
            evidence = self.get_evidence(evidence_id)
            if evidence.kind != "eval":
                raise ValidationError(
                    "eval run evidence must have kind='eval' (got '%s')" % evidence.kind
                )
        score_f = float(score)
        baseline = eval_set.baseline_score
        threshold = eval_set.regression_threshold
        if baseline is None:
            delta = None
            passed = True
        else:
            delta = score_f - baseline
            if eval_set.scoring == EvalScoringDirection.HIGHER_IS_BETTER.value:
                # passing means the score did not regress past the threshold
                passed = delta >= -threshold
            else:
                # lower is better — score should not exceed baseline by more than threshold
                passed = delta <= threshold
        now = utcnow()
        run_id = new_id("evalrun")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO eval_runs (
                    id, eval_set_id, target_kind, target_id, score, baseline_score,
                    delta, threshold, passed, detail, evidence_id, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    eval_set.id,
                    target_kind_value,
                    target_id,
                    score_f,
                    baseline,
                    delta,
                    threshold,
                    1 if passed else 0,
                    json_dumps(ensure_json_object(detail)),
                    evidence_id,
                    created_by,
                    now,
                ),
            )
            self._insert_eval_set_event(
                conn,
                eval_set.id,
                "eval_set.run_recorded",
                created_by,
                {
                    "run_id": run_id,
                    "target_kind": target_kind_value,
                    "target_id": target_id,
                    "score": score_f,
                    "passed": bool(passed),
                    "evidence_id": evidence_id,
                },
                now,
            )
        return self.get_eval_run(run_id)

    def get_eval_run(self, run_id: str) -> EvalRun:
        row = self.store.query_one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError("eval_run not found: %s" % run_id)
        return self._eval_run_from_row(row)

    def latest_eval_run(
        self,
        eval_set_id: str,
        target_kind: str,
        target_id: str,
    ) -> Optional[EvalRun]:
        row = self.store.query_one(
            """
            SELECT * FROM eval_runs
            WHERE eval_set_id = ? AND target_kind = ? AND target_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (eval_set_id, _state_value(target_kind), target_id),
        )
        return self._eval_run_from_row(row) if row is not None else None

    def list_eval_runs(
        self,
        eval_set_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[EvalRun]:
        clauses: List[str] = []
        params: List[Any] = []
        if eval_set_id is not None:
            clauses.append("eval_set_id = ?")
            params.append(eval_set_id)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        sql = "SELECT * FROM eval_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [self._eval_run_from_row(row) for row in self.store.query_all(sql, tuple(params))]

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

    def _tenant_from_row(self, row: Any) -> Tenant:
        return Tenant(
            row["id"],
            row["name"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    def _user_from_row(self, row: Any) -> User:
        return User(
            row["id"],
            row["tenant_id"],
            row["handle"],
            row["display_name"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    def _persona_from_row(self, row: Any) -> Persona:
        return Persona(
            row["id"],
            row["tenant_id"],
            row["name"],
            row["soul_ref"],
            row["memory_scope"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

    def _hermes_instance_from_row(self, row: Any) -> HermesInstance:
        return HermesInstance(
            row["id"],
            row["tenant_id"],
            row["name"],
            row["persona_id"],
            row["home_ref"],
            row["status"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
        )

    def _platform_binding_from_row(self, row: Any) -> PlatformBinding:
        return PlatformBinding(
            row["id"],
            row["tenant_id"],
            row["hermes_instance_id"],
            row["platform"],
            row["external_id"],
            row["display_name"],
            json_loads(row["scopes"], {}),
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )

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
        return Agent(
            row["id"],
            row["machine_id"],
            row["name"],
            json_loads(row["capabilities"], []),
            json_loads(row["resources"], {}),
            row["status"],
            row["health_status"],
            row["current_task_id"],
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
        )

    def _message_from_row(self, row: Any) -> AgentMessage:
        return AgentMessage(
            row["id"],
            row["sender_agent_id"],
            row["recipient_agent_id"],
            row["task_id"],
            row["message_type"],
            json_loads(row["payload"], {}),
            row["status"],
            row["created_at"],
            row["delivered_at"],
        )

    def _review_from_row(self, row: Any) -> Review:
        return Review(
            row["id"],
            row["task_id"],
            row["reviewer_agent_id"],
            row["status"],
            row["reason"],
            row["evidence_id"],
            row["created_at"],
            row["completed_at"],
        )

    def _publication_from_row(self, row: Any) -> Publication:
        return Publication(
            row["id"],
            row["task_id"],
            row["target"],
            row["status"],
            row["evidence_id"],
            row["content_hash"],
            row["created_by"],
            row["created_at"],
        )

    def _secret_from_row(self, row: Any) -> SecretRecord:
        return SecretRecord(
            row["id"],
            row["name"],
            json_loads(row["scopes"], {}),
            row["created_by"],
            row["created_at"],
            row["updated_at"],
            row["rotated_at"],
            bool(row["enabled"]),
        )

    def _secret_access_from_row(self, row: Any) -> SecretAccess:
        return SecretAccess(
            row["id"],
            row["secret_id"],
            row["accessor_agent_id"],
            row["purpose"],
            row["result"],
            row["expires_at"],
            row["revealed_at"],
            row["created_at"],
        )

    def _runtime_from_row(self, row: Any) -> RuntimeEnvironment:
        return RuntimeEnvironment(
            row["id"],
            row["name"],
            json_loads(row["manifest"], {}),
            row["digest"],
            row["created_by"],
            row["created_at"],
        )

    def _runtime_run_from_row(self, row: Any) -> RuntimeRun:
        return RuntimeRun(
            row["id"],
            row["task_id"],
            row["agent_id"],
            row["environment_id"],
            row["status"],
            row["evidence_id"],
            row["created_at"],
            row["updated_at"],
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

    def _memory_from_row(self, row: Any) -> MemoryRecord:
        return MemoryRecord(
            row["id"],
            row["task_id"],
            row["subject_type"],
            row["subject_id"],
            row["record_type"],
            row["content"],
            row["evidence_id"],
            row["created_by"],
            row["created_at"],
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

    def _eval_set_from_row(self, row: Any) -> "EvalSet":
        return EvalSet(
            row["id"],
            row["name"],
            row["description"],
            row["scoring"],
            row["baseline_score"],
            row["regression_threshold"],
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )

    def _eval_run_from_row(self, row: Any) -> "EvalRun":
        return EvalRun(
            row["id"],
            row["eval_set_id"],
            row["target_kind"],
            row["target_id"],
            row["score"],
            row["baseline_score"],
            row["delta"],
            row["threshold"],
            bool(row["passed"]),
            json_loads(row["detail"], {}),
            row["evidence_id"],
            row["created_by"],
            row["created_at"],
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
    ) -> None:
        self.store.execute(
            """
            INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("hist"), task_id, event_type, actor, from_state, to_state, json_dumps(detail), utcnow()),
        )

    def _record_rollout_event(self, rollout_id: str, event_type: str, actor: str, detail: Dict[str, Any]) -> None:
        self.store.execute(
            """
            INSERT INTO rollout_events (id, rollout_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("revt"), rollout_id, event_type, actor, json_dumps(detail), utcnow()),
        )

    def _insert_eval_set_event(
        self,
        conn: Any,
        eval_set_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        # Uses the caller's connection so the event lands in the same transaction
        # as the originating write (create / baseline change / run record). Audit
        # trail and durable state must commit together.
        conn.execute(
            """
            INSERT INTO eval_set_events (id, eval_set_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("eevt"), eval_set_id, event_type, actor, json_dumps(detail), when),
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

    def _completion_authorized(self, task_id: str) -> bool:
        # An approved review must reference evidence that belongs to the same
        # task. This is the contract the README promises: completion requires
        # not just *some* evidence and *some* approval, but a documented link.
        approved = self.store.query_one(
            """
            SELECT r.id FROM reviews r
            JOIN evidence e ON e.id = r.evidence_id AND e.task_id = r.task_id
            WHERE r.task_id = ? AND r.status = ?
            LIMIT 1
            """,
            (task_id, ReviewStatus.APPROVED.value),
        )
        return approved is not None

    def _agent_has_owned_task(self, task_id: str, agent_id: str) -> bool:
        task = self.get_task(task_id)
        if task.owner_agent_id == agent_id:
            return True
        prior = self.store.query_one(
            "SELECT 1 FROM leases WHERE task_id = ? AND agent_id = ? LIMIT 1",
            (task_id, agent_id),
        )
        return prior is not None

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

    def _set_agent_idle(self, agent_id: str) -> None:
        now = utcnow()
        self.store.execute(
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

    def _task_requires_publication_evidence(self, task: Task) -> bool:
        policy = task.metadata.get("policy") or {}
        if not isinstance(policy, dict):
            return False
        return bool(
            policy.get("require_publication_evidence")
            or policy.get("publication_evidence_required")
        )

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

    def _validate_message_payload(self, message_type: str, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValidationError("message payload must be a JSON object")
        required = MESSAGE_TYPE_REQUIRED_FIELDS.get(message_type, ())
        missing = [field for field in required if payload.get(field) in (None, "")]
        if missing:
            raise ValidationError(
                "message %s payload missing required field(s): %s"
                % (message_type, ",".join(missing))
            )
        self._check_payload_is_json_safe(payload, ())

    def _check_payload_is_json_safe(self, value: Any, path: Sequence[str]) -> None:
        """Reject non-JSON-serializable payloads early.

        Workers consume messages as structured data and look up durable tasks
        from the ledger. Message payloads are not an execution channel.
        """
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValidationError(
                        "message payload keys must be strings at %s" % ".".join(path)
                    )
                key_path = path + (key,)
                if key.lower() in FORBIDDEN_MESSAGE_KEYS:
                    raise ValidationError(
                        "message payload cannot contain execution key: %s"
                        % ".".join(key_path)
                    )
                self._check_payload_is_json_safe(nested, key_path)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                self._check_payload_is_json_safe(nested, path + (str(index),))
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValidationError(
                "message payload contains non-JSON value at %s" % ".".join(path)
            )

    def _record_secret_access(
        self,
        secret_id: str,
        accessor_agent_id: str,
        purpose: str,
        result: str,
        expires_at: Optional[str] = None,
    ) -> SecretAccess:
        audit_id = new_id("audit")
        self.store.execute(
            """
            INSERT INTO secret_access_audit (
                id, secret_id, accessor_agent_id, purpose, result, expires_at, revealed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                audit_id,
                secret_id,
                accessor_agent_id,
                purpose or "unspecified",
                result,
                expires_at,
                utcnow(),
            ),
        )
        row = self.store.query_one("SELECT * FROM secret_access_audit WHERE id = ?", (audit_id,))
        if row is None:
            raise NotFoundError("secret audit not found: %s" % audit_id)
        return self._secret_access_from_row(row)

    def _secret_scope_allows(self, scopes: JsonDict, agent: Agent) -> bool:
        agents = set(scopes.get("agents") or [])
        capabilities = set(scopes.get("capabilities") or [])
        tenant_scope = set(scopes.get("tenant_ids") or [])
        if scopes.get("tenant_id"):
            tenant_scope.add(str(scopes["tenant_id"]))
        if tenant_scope:
            machine = self.get_machine(agent.machine_id)
            if not any(self._machine_allows_tenant(machine, tenant_id) for tenant_id in tenant_scope):
                return False
        if agent.id in agents:
            return True
        if capabilities and capabilities.intersection(set(agent.capabilities)):
            return True
        return False

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise MACError("secret ciphertext failed authentication") from exc

    def _validate_runtime_manifest(self, manifest: JsonDict) -> None:
        self._scan_runtime_manifest(manifest, ())

    def _scan_runtime_manifest(self, value: Any, path: Sequence[str]) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_str = str(key)
                key_lower = key_str.lower()
                if any(hint in key_lower for hint in SECRET_FIELD_HINTS) and key_lower not in {
                    "secret_refs",
                    "secret_ref",
                }:
                    raise ValidationError(
                        "runtime manifest cannot include raw secret field: %s"
                        % ".".join(path + (key_str,))
                    )
                self._scan_runtime_manifest(nested, path + (key_str,))
            return
        if isinstance(value, list):
            # Top-level "dependencies" list must be pinned. Nested lists (e.g.
            # inside an entrypoint) are not version specs.
            in_dependencies = path and path[-1].lower() == "dependencies"
            for index, nested in enumerate(value):
                if in_dependencies and isinstance(nested, str) and nested.strip().endswith("*"):
                    raise ValidationError("runtime dependencies must be pinned")
                self._scan_runtime_manifest(nested, path + (str(index),))
            return
        if isinstance(value, str):
            # Image references appear at any depth — e.g. multi-stage manifests,
            # init containers, sidecars. Reject :latest anywhere a string ends
            # with it; false positives are an acceptable cost for the guarantee.
            stripped = value.strip()
            if stripped.endswith(":latest"):
                raise ValidationError(
                    "runtime manifest field at %s pins :latest; pin a digest"
                    % (".".join(path) or "(root)")
                )
            if path and path[-1].lower() in {"image", "container_image"} and "@sha256:" not in stripped:
                raise ValidationError(
                    "runtime manifest image at %s must include a sha256 digest"
                    % ".".join(path)
                )
