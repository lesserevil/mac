from __future__ import annotations

import base64
import hashlib
import os
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from mac.models import (
    Agent,
    AgentMessage,
    AgentStatus,
    AuthorizationError,
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

SECRET_FIELD_HINTS = ("secret", "token", "password", "private_key", "credential")


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
        self._secret_key = (secret_key or os.environ.get("MAC_SECRET_KEY") or "local-dev-key").encode(
            "utf-8"
        )

    @classmethod
    def in_memory(cls) -> "ControlPlane":
        return cls(SQLiteStore(":memory:"), secret_key="test-key")

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
        self.store.execute(
            """
            INSERT INTO tenants (id, name, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (tid, name, json_dumps(ensure_json_object(metadata)), now, now),
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
                json_dumps(ensure_json_object(metadata)),
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
                json_dumps(ensure_json_object(metadata)),
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
                json_dumps(ensure_json_object(metadata)),
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
                json_dumps(ensure_json_object(scopes)),
                json_dumps(ensure_json_object(metadata)),
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

    def list_tasks(self, state: Optional[str] = None) -> List[Task]:
        if state:
            rows = self.store.query_all(
                "SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC, created_at",
                (_state_value(state),),
            )
        else:
            rows = self.store.query_all("SELECT * FROM tasks ORDER BY priority DESC, created_at")
        return [self._task_from_row(row) for row in rows]

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
            return self.transition_task(task_id, TaskState.FAILED.value, "dispatcher", {"reason": "max attempts"}), self._empty_lease(task_id, agent_id)
        now = utcnow()
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="seconds")
        lease_id = new_id("lease")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO leases (id, task_id, agent_id, expires_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (lease_id, task_id, agent_id, expires_at, LeaseStatus.ACTIVE.value, now, now),
            )
            conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (TaskState.CLAIMED.value, agent_id, lease_id, expires_at, now, task_id),
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
        expires_at = (parse_time(now) + timedelta(seconds=int(lease_seconds))).isoformat(timespec="seconds")
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
                json_dumps(ensure_json_object(labels)),
                json_dumps(ensure_json_object(resources)),
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
                health_status = excluded.health_status,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                aid,
                machine_id,
                name,
                json_dumps(coerce_list(capabilities)),
                json_dumps(ensure_json_object(resources)),
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
        if status is not None:
            updates.append("status = ?")
            params.append(_state_value(status))
        if health_status is not None:
            updates.append("health_status = ?")
            params.append(_state_value(health_status))
        if resources is not None:
            updates.append("resources = ?")
            params.append(json_dumps(resources))
        params.append(agent_id)
        self.store.execute("UPDATE agents SET %s WHERE id = ?" % ", ".join(updates), tuple(params))
        return self.get_agent(agent_id)

    # Dispatcher

    def dispatch_once(self, lease_seconds: int = 900) -> Optional[JsonDict]:
        self.expire_leases()
        self._unblock_ready_tasks()
        tasks = self.list_tasks(TaskState.OPEN.value)
        agents = self._available_agents()
        for task in tasks:
            for agent in agents:
                if self._agent_available_for(agent, task):
                    claimed, lease = self.claim_task(task.id, agent.id, lease_seconds=lease_seconds)
                    self.send_message(
                        "dispatcher",
                        agent.id,
                        MessageType.NUDGE.value,
                        {"task_id": claimed.id, "lease_id": lease.id, "reason": "assigned"},
                        task_id=claimed.id,
                    )
                    return {"task": claimed.to_dict(), "agent": agent.to_dict(), "lease": lease.to_dict()}
        return None

    def tick(self, lease_seconds: int = 900, limit: int = 100) -> JsonDict:
        expired = [task.to_dict() for task in self.expire_leases()]
        self._unblock_ready_tasks()
        assignments = []
        for _ in range(limit):
            assignment = self.dispatch_once(lease_seconds=lease_seconds)
            if assignment is None:
                break
            assignments.append(assignment)
        return {"expired": expired, "assignments": assignments}

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
        try:
            MessageType(_state_value(message_type))
        except ValueError:
            raise ValidationError("unsupported message type: %s" % message_type)
        self._validate_message_payload(payload)
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
        if evidence_id is not None:
            evidence = self.get_evidence(evidence_id)
            if evidence.task_id != task_id:
                raise ValidationError("publication evidence must belong to task")
        now = utcnow()
        publication_id = new_id("pub")
        self.store.execute(
            """
            INSERT INTO publications (id, task_id, target, status, evidence_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication_id,
                task_id,
                target,
                PublicationStatus.PUBLISHED.value,
                evidence_id,
                created_by,
                now,
            ),
        )
        self._record_history(
            task_id,
            "task.published",
            created_by,
            None,
            None,
            {"publication_id": publication_id, "target": target},
        )
        self.transition_task(task_id, TaskState.COMPLETED.value, created_by, {"publication_id": publication_id})
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

    def request_secret(self, secret_id_or_name: str, accessor_agent_id: str, purpose: str) -> SecretHandle:
        secret = self.get_secret(secret_id_or_name)
        agent = self.get_agent(accessor_agent_id)
        granted = bool(secret.enabled and self._secret_scope_allows(secret.scopes, agent))
        audit = self._record_secret_access(
            secret.id,
            accessor_agent_id,
            purpose,
            SecretAuditResult.GRANTED.value if granted else SecretAuditResult.DENIED.value,
        )
        if not granted:
            raise AuthorizationError("secret access denied")
        return SecretHandle(secret.id, audit.id, "secret://%s#%s" % (secret.id, audit.id), True)

    def rotate_secret(self, secret_id_or_name: str, value: str, actor: str) -> SecretRecord:
        secret = self.get_secret(secret_id_or_name)
        now = utcnow()
        self.store.execute(
            "UPDATE secrets SET ciphertext = ?, updated_at = ?, rotated_at = ? WHERE id = ?",
            (self._encrypt(value), now, now, secret.id),
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

    def _reveal_secret_for_runtime(self, secret_id: str, audit_id: str) -> str:
        audit = self.store.query_one(
            "SELECT * FROM secret_access_audit WHERE id = ? AND secret_id = ? AND result = ?",
            (audit_id, secret_id, SecretAuditResult.GRANTED.value),
        )
        if audit is None:
            raise AuthorizationError("no granted audit for runtime secret reveal")
        row = self.store.query_one("SELECT ciphertext FROM secrets WHERE id = ? AND enabled = 1", (secret_id,))
        if row is None:
            raise NotFoundError("secret not found: %s" % secret_id)
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

    # Rollout and rescue

    def create_rollout(
        self,
        version: str,
        strategy: str,
        target_percent: int,
        created_by: str,
    ) -> Rollout:
        if not version:
            raise ValidationError("rollout version is required")
        strategy_value = _state_value(strategy)
        try:
            RolloutStrategy(strategy_value)
        except ValueError:
            raise ValidationError("unsupported rollout strategy: %s" % strategy_value)
        if int(target_percent) < 0 or int(target_percent) > 100:
            raise ValidationError("rollout target percent must be between 0 and 100")
        now = utcnow()
        rollout_id = new_id("rollout")
        self.store.execute(
            """
            INSERT INTO rollouts (id, version, strategy, status, target_percent, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rollout_id,
                version,
                strategy_value,
                RolloutStatus.PLANNED.value,
                int(target_percent),
                created_by,
                now,
                now,
            ),
        )
        self._record_rollout_event(rollout_id, "rollout.created", created_by, {"target_percent": int(target_percent)})
        return self.get_rollout(rollout_id)

    def get_rollout(self, rollout_id: str) -> Rollout:
        row = self.store.query_one("SELECT * FROM rollouts WHERE id = ?", (rollout_id,))
        if row is None:
            raise NotFoundError("rollout not found: %s" % rollout_id)
        return self._rollout_from_row(row)

    def advance_rollout(
        self,
        rollout_id: str,
        action: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Rollout:
        rollout = self.get_rollout(rollout_id)
        detail = detail or {}
        if action == "start_canary":
            if rollout.status != RolloutStatus.PLANNED.value:
                raise TransitionError("canary can only start from planned")
            status = RolloutStatus.CANARYING.value
        elif action == "promote":
            if rollout.status not in {RolloutStatus.PLANNED.value, RolloutStatus.CANARYING.value}:
                raise TransitionError("rollout can only be promoted from planned or canarying")
            status = RolloutStatus.PROMOTED.value
            detail.setdefault("target_percent", 100)
        elif action == "pause":
            if rollout.status in {RolloutStatus.ROLLED_BACK.value, RolloutStatus.FAILED.value}:
                raise TransitionError("terminal rollout cannot pause")
            status = RolloutStatus.PAUSED.value
        elif action == "rollback":
            status = RolloutStatus.ROLLED_BACK.value
            detail.setdefault("target_percent", 0)
        else:
            raise ValidationError("unsupported rollout action: %s" % action)
        target_percent = int(detail.get("target_percent", rollout.target_percent))
        now = utcnow()
        self.store.execute(
            "UPDATE rollouts SET status = ?, target_percent = ?, updated_at = ? WHERE id = ?",
            (status, target_percent, now, rollout_id),
        )
        self._record_rollout_event(rollout_id, "rollout.%s" % action, actor, detail)
        return self.get_rollout(rollout_id)

    def rescue_rollout(self, rollout_id: str, actor: str, reason: str) -> Tuple[Rollout, Task]:
        rollout = self.get_rollout(rollout_id)
        now = utcnow()
        self.store.execute(
            "UPDATE rollouts SET status = ?, target_percent = ?, updated_at = ? WHERE id = ?",
            (RolloutStatus.RESCUING.value, 0, now, rollout_id),
        )
        self._record_rollout_event(rollout_id, "rollout.rescue_started", actor, {"reason": reason})
        task = self.create_task(
            "Rescue rollout %s" % rollout.version,
            description=reason,
            project="rollout",
            priority=100,
            required_capabilities=["ops"],
            metadata={"rollout_id": rollout_id, "rescue": True},
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

    def _empty_lease(self, task_id: str, agent_id: str) -> Lease:
        now = utcnow()
        return Lease("", task_id, agent_id, now, LeaseStatus.EXPIRED.value, now, now)

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
        return Rollout(
            row["id"],
            row["version"],
            row["strategy"],
            row["status"],
            row["target_percent"],
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

    def _completion_authorized(self, task_id: str) -> bool:
        has_evidence = bool(self.list_evidence(task_id))
        approved = self.store.query_one(
            "SELECT id FROM reviews WHERE task_id = ? AND status = ? LIMIT 1",
            (task_id, ReviewStatus.APPROVED.value),
        )
        return bool(has_evidence and approved)

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

    def _available_agents(self) -> List[Agent]:
        rows = self.store.query_all(
            """
            SELECT a.* FROM agents a
            JOIN machines m ON m.id = a.machine_id
            WHERE a.status = ? AND a.health_status = ? AND m.trusted = 1
            ORDER BY a.last_seen_at DESC, a.id
            """,
            (AgentStatus.IDLE.value, HealthStatus.HEALTHY.value),
        )
        return [self._agent_from_row(row) for row in rows]

    def _agent_available_for(self, agent: Agent, task: Task) -> bool:
        if agent.status != AgentStatus.IDLE.value:
            return False
        if agent.health_status != HealthStatus.HEALTHY.value:
            return False
        machine = self.get_machine(agent.machine_id)
        if not machine.trusted:
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

    def _validate_message_payload(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValidationError("message payload must be a JSON object")
        self._scan_for_forbidden_message_keys(payload, ())

    def _scan_for_forbidden_message_keys(self, value: Any, path: Sequence[str]) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_lower = str(key).lower()
                if key_lower in FORBIDDEN_MESSAGE_KEYS:
                    raise ValidationError("message payload cannot contain execution key: %s" % ".".join(path + (str(key),)))
                self._scan_for_forbidden_message_keys(nested, path + (str(key),))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                self._scan_for_forbidden_message_keys(nested, path + (str(index),))

    def _record_secret_access(self, secret_id: str, accessor_agent_id: str, purpose: str, result: str) -> SecretAccess:
        audit_id = new_id("audit")
        self.store.execute(
            """
            INSERT INTO secret_access_audit (id, secret_id, accessor_agent_id, purpose, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (audit_id, secret_id, accessor_agent_id, purpose or "unspecified", result, utcnow()),
        )
        row = self.store.query_one("SELECT * FROM secret_access_audit WHERE id = ?", (audit_id,))
        if row is None:
            raise NotFoundError("secret audit not found: %s" % audit_id)
        return self._secret_access_from_row(row)

    def _secret_scope_allows(self, scopes: JsonDict, agent: Agent) -> bool:
        agents = set(scopes.get("agents") or [])
        capabilities = set(scopes.get("capabilities") or [])
        if "*" in agents or agent.id in agents:
            return True
        if capabilities.intersection(set(agent.capabilities)):
            return True
        return False

    def _encrypt(self, value: str) -> str:
        value_bytes = value.encode("utf-8")
        encrypted = bytes(byte ^ self._secret_key[index % len(self._secret_key)] for index, byte in enumerate(value_bytes))
        return base64.urlsafe_b64encode(encrypted).decode("ascii")

    def _decrypt(self, value: str) -> str:
        encrypted = base64.urlsafe_b64decode(value.encode("ascii"))
        plain = bytes(byte ^ self._secret_key[index % len(self._secret_key)] for index, byte in enumerate(encrypted))
        return plain.decode("utf-8")

    def _validate_runtime_manifest(self, manifest: JsonDict) -> None:
        self._scan_runtime_manifest_for_secrets(manifest, ())
        image = manifest.get("image")
        if isinstance(image, str) and image.endswith(":latest"):
            raise ValidationError("runtime image must be pinned, not :latest")
        dependencies = manifest.get("dependencies", [])
        if isinstance(dependencies, list):
            for dep in dependencies:
                if isinstance(dep, str) and dep.strip().endswith("*"):
                    raise ValidationError("runtime dependencies must be pinned")

    def _scan_runtime_manifest_for_secrets(self, value: Any, path: Sequence[str]) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_lower = str(key).lower()
                if any(hint in key_lower for hint in SECRET_FIELD_HINTS) and key_lower not in {"secret_refs", "secret_ref"}:
                    raise ValidationError("runtime manifest cannot include raw secret field: %s" % ".".join(path + (str(key),)))
                self._scan_runtime_manifest_for_secrets(nested, path + (str(key),))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                self._scan_runtime_manifest_for_secrets(nested, path + (str(index),))
