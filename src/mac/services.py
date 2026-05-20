from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mac.models import (
    Agent,
    AgentProvisioningRequest,
    AgentRole,
    Workflow,
    WorkflowRun,
    AgentBusChunk,
    AgentBusStream,
    AgentBusStreamStatus,
    AgentMessage,
    AgentStatus,
    Artifact,
    AuthorizationError,
    BeadsRepository,
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
from mac.provisioning_service import ProvisioningService
from mac.review_service import ReviewService
from mac.roles_service import RolesService
from mac.rollout_service import RolloutService
from mac.secrets_service import SecretsService
from mac.store import SQLiteStore
from mac.workflow_runtime import WorkflowRuntime
from mac.workflow_service import WorkflowService


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def _manifest_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _truthy_env(name: str, default: str = "") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _beads_cli() -> str:
    configured = os.environ.get("MAC_BEADS_CLI", "").strip()
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("bd")
    if found:
        return found
    for candidate in (
        Path.home() / ".mac" / "bin" / "bd",
        Path.home() / ".local" / "bin" / "bd",
        Path("/usr/local/bin/bd"),
        Path("/opt/homebrew/bin/bd"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "bd"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-._").lower()
    return slug or "repo"


VERIFICATION_SCHEMA = "mac.worker_evidence.v1"
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Bytes of cleartext attestation key per agent. 256 bits of HMAC key is
# overkill for the threat model but fits in one stretch of base64 and
# keeps the door closed if HMAC-SHA256 ever becomes the bottleneck.
ATTESTATION_KEY_BYTES = 32


def _generate_attestation_key() -> str:
    """Mint a fresh per-agent HMAC key. Returned base64url so it fits
    in a single env var or JSON string without escaping."""
    import secrets as _secrets

    return base64.urlsafe_b64encode(_secrets.token_bytes(ATTESTATION_KEY_BYTES)).decode("ascii").rstrip("=")


def _canonicalize_for_signature(manifest: Dict[str, Any]) -> bytes:
    """Deterministic JSON encoding of the verification manifest for
    HMAC signing. The ``signature`` field is excluded from the
    canonical form so a manifest can be signed once and embedded
    without recursive hashing.
    """
    excluded = {"signature", "signed_by"}
    filtered = {k: v for k, v in manifest.items() if k not in excluded}
    return json_dumps(filtered).encode("utf-8")


def sign_verification_manifest(key: str, manifest: Dict[str, Any]) -> str:
    """Sign ``manifest`` with the agent's attestation key. Returns the
    base64url HMAC tag. Exposed for the worker (writes signatures) and
    for tests (constructs signed evidence fixtures)."""
    import hmac as _hmac
    import hashlib as _hashlib

    digest = _hmac.new(
        key.encode("ascii"), _canonicalize_for_signature(manifest), _hashlib.sha256
    ).digest()
    return "v1:" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_verification_manifest_signature(
    key: str, manifest: Dict[str, Any], signature: str
) -> bool:
    """Constant-time HMAC verification. Returns True iff ``signature``
    matches the expected tag for ``manifest`` under ``key``."""
    import hmac as _hmac

    if not signature or not signature.startswith("v1:"):
        return False
    expected = sign_verification_manifest(key, manifest)
    return _hmac.compare_digest(expected, signature)


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
        self.provisioning = ProvisioningService(self.store, self.observability)
        self.roles = RolesService(
            self.store,
            self.observability,
            get_tenant=self.get_tenant,
            get_agent=self.get_agent,
            get_machine=self.get_machine,
            get_hermes_instance=self.identity.get_hermes_instance,
            get_persona=self.identity.get_persona,
        )
        self.workflows = WorkflowService(
            self.store,
            self.observability,
            get_role=self.roles.get_role,
            get_tenant=self.get_tenant,
        )
        self.workflow_runtime = WorkflowRuntime(
            self.store,
            self.observability,
            self.workflows,
            self.roles,
            create_task=self.create_task,
            transition_task=self.transition_task,
            get_task=self.get_task,
            record_history=self._record_history,
        )
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
        self.rollouts = RolloutService(
            self.store,
            self.observability,
            get_tenant=self.get_tenant,
            get_runtime=self.get_runtime,
            get_eval_set=self.get_eval_set,
            create_task=self.create_task,
            add_memory=self.add_memory,
            task_from_row=self._task_from_row,
        )
        self._seed_beads_repositories_from_env()

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

    # Agent roles: thin facade over ``self.roles``.

    def create_role(self, *args: Any, **kwargs: Any) -> AgentRole:
        return self.roles.create_role(*args, **kwargs)

    def get_role(self, *args: Any, **kwargs: Any) -> AgentRole:
        return self.roles.get_role(*args, **kwargs)

    def list_roles(self, *args: Any, **kwargs: Any) -> List[AgentRole]:
        return self.roles.list_roles(*args, **kwargs)

    def update_role(self, *args: Any, **kwargs: Any) -> AgentRole:
        return self.roles.update_role(*args, **kwargs)

    def delete_role(self, *args: Any, **kwargs: Any) -> None:
        return self.roles.delete_role(*args, **kwargs)

    def assign_role(self, agent_id: str, role_id_or_slug: str) -> Agent:
        return self.roles.assign_role(agent_id, role_id_or_slug)

    def unassign_role(self, agent_id: str) -> Agent:
        return self.roles.unassign_role(agent_id)

    def seed_default_roles(self, *args: Any, **kwargs: Any) -> List[AgentRole]:
        return self.roles.seed_defaults(*args, **kwargs)

    # Provisioning hook: emitted when the swarm needs an agent it doesn't
    # have. Today the provisioner is unimplemented; rows + observability
    # are the signal an external poller acts on.

    def request_agent_provisioning(
        self, *args: Any, **kwargs: Any
    ) -> AgentProvisioningRequest:
        return self.provisioning.request_agent(*args, **kwargs)

    def list_provisioning_requests(
        self, *args: Any, **kwargs: Any
    ) -> List[AgentProvisioningRequest]:
        return self.provisioning.list_requests(*args, **kwargs)

    def get_provisioning_request(self, request_id: str) -> AgentProvisioningRequest:
        return self.provisioning.get_request(request_id)

    def fulfill_provisioning_request(
        self, request_id: str, agent_id: str
    ) -> AgentProvisioningRequest:
        return self.provisioning.fulfill_request(request_id, agent_id)

    def cancel_provisioning_request(
        self, request_id: str, *, reason: str = "operator-cancelled"
    ) -> AgentProvisioningRequest:
        return self.provisioning.cancel_request(request_id, reason=reason)

    def agent_identity(self, agent_id: str) -> JsonDict:
        """Layered identity for an agent: soul → role → mood → hardware.

        The layers are returned separately rather than fused into a
        single prompt string — callers (worker, Hermes) own the
        composition. Soul is authoritative for personality; role is the
        operational hat; mood is the agent's transient self-report;
        hardware is the machine the agent runs on.
        """
        agent = self.get_agent(agent_id)
        machine = self.get_machine(agent.machine_id)
        soul: Optional[JsonDict] = None
        role_slugs: Optional[List[str]] = self.roles._allowed_role_slugs_for(agent)
        if agent.hermes_instance_id:
            try:
                instance = self.identity.get_hermes_instance(agent.hermes_instance_id)
                persona = (
                    self.identity.get_persona(instance.persona_id)
                    if instance.persona_id
                    else None
                )
                soul = {
                    "hermes_instance": instance.to_dict(),
                    "persona": persona.to_dict() if persona else None,
                }
            except NotFoundError:
                soul = None
        role: Optional[JsonDict] = None
        if agent.role_id:
            try:
                role = self.roles.get_role(agent.role_id).to_dict()
            except NotFoundError:
                role = None
        mood_overlay = self.agent_state.get_current_mood(agent.id)
        return {
            "agent": agent.to_dict(),
            "soul": soul,
            "allowed_role_slugs": role_slugs,
            "role": role,
            "mood": mood_overlay.to_dict() if mood_overlay is not None else None,
            "machine_hardware": machine.hardware,
        }

    # Workflows: thin facade over ``self.workflows``.

    def create_workflow(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.create_workflow(*args, **kwargs)

    def get_workflow(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.get_workflow(*args, **kwargs)

    def list_workflows(self, *args: Any, **kwargs: Any) -> List[Workflow]:
        return self.workflows.list_workflows(*args, **kwargs)

    def update_workflow(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.update_workflow(*args, **kwargs)

    def delete_workflow(self, workflow_id: str) -> None:
        return self.workflows.delete_workflow(workflow_id)

    def import_workflow_yaml(self, *args: Any, **kwargs: Any) -> Workflow:
        return self.workflows.import_yaml(*args, **kwargs)

    def seed_default_workflows(self, *args: Any, **kwargs: Any) -> List[Workflow]:
        return self.workflows.seed_defaults(*args, **kwargs)

    def start_workflow(self, *args: Any, **kwargs: Any) -> WorkflowRun:
        return self.workflow_runtime.start_run(*args, **kwargs)

    def get_workflow_run(self, run_id: str) -> WorkflowRun:
        return self.workflow_runtime.get_run(run_id)

    def list_workflow_runs(self, *args: Any, **kwargs: Any) -> List[WorkflowRun]:
        return self.workflow_runtime.list_runs(*args, **kwargs)

    def cancel_workflow_run(self, *args: Any, **kwargs: Any) -> WorkflowRun:
        return self.workflow_runtime.cancel_run(*args, **kwargs)

    def tick_workflow_runs(self, *args: Any, **kwargs: Any) -> List[WorkflowRun]:
        return self.workflow_runtime.tick(*args, **kwargs)

    def workflow_runs_summary(self) -> JsonDict:
        """Counts grouped by state plus the 20 most recent runs for the
        dashboard. Designed to be inlined into /dashboard/state without
        adding a separate read query path."""
        rows = self.store.query_all(
            "SELECT state, COUNT(*) AS count FROM workflow_runs GROUP BY state"
        )
        by_state = {row["state"]: int(row["count"]) for row in rows}
        latest = [
            run.to_dict()
            for run in self.workflow_runtime.list_runs(limit=20)
        ]
        return {
            "counts": by_state,
            "total": sum(by_state.values()),
            "latest": latest,
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
            "publications": [item.to_dict() for item in self.list_publications(task_id)],
        }

    def task_summary(self, task_id: str) -> JsonDict:
        detail = self.task_detail(task_id)
        task = detail["task"]
        evidence = detail["evidence"]
        reviews = detail["reviews"]
        approved_reviews = [review for review in reviews if review["status"] == ReviewStatus.APPROVED.value]
        publications = [
            pub.to_dict()
            for pub in self.reviews.list_publications(task_id)
            if pub.status == PublicationStatus.PUBLISHED.value
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
        release_lease_id = None
        if target in {
            TaskState.OPEN.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            release_lease_id = lease_id
            owner_agent_id = None
            lease_id = None
            leased_until = None
        with self.store.transaction() as conn:
            if release_lease_id:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.RELEASED.value, now, release_lease_id, LeaseStatus.ACTIVE.value),
                )
            conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (target, owner_agent_id, lease_id, leased_until, now, task_id),
            )
            if task.owner_agent_id and target in TERMINAL_TASK_STATES.union(
                {TaskState.OPEN.value, TaskState.NEEDS_REVIEW.value}
            ):
                self._set_agent_idle(task.owner_agent_id, conn=conn)
            self._record_history(
                task_id, "task.transitioned", actor, task.state, target, detail or {}, conn=conn
            )
        # Workflow-runtime hook. The link is the `tasks.workflow_run_id`
        # *column* (never the caller-supplied metadata), so a forged
        # `metadata.workflow_run_id` cannot push a free-floating task
        # into the workflow state machine. Runs in terminal states are
        # short-circuited inside `on_task_completed`.
        if target in TERMINAL_TASK_STATES:
            row = self.store.query_one(
                "SELECT workflow_run_id FROM tasks WHERE id = ?", (task_id,)
            )
            if row is not None and row["workflow_run_id"]:
                try:
                    self.workflow_runtime.on_task_completed(task_id, target)
                except Exception:  # noqa: BLE001 - runtime side-effects must not abort the transition
                    import logging

                    logging.getLogger(__name__).exception(
                        "workflow runtime failed to advance on task %s", task_id
                    )
        transitioned = self.get_task(task_id)
        if target in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
            self._sync_beads_reopen(transitioned, actor, target, detail or {})
        return transitioned

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
        claimed_task = self.get_task(task_id)
        self._sync_beads_claim(claimed_task, agent_id)
        return claimed_task, self.get_lease(lease_id)

    def start_task(self, task_id: str, agent_id: str) -> Task:
        task = self.get_task(task_id)
        if task.owner_agent_id != agent_id:
            raise AuthorizationError("agent does not own task lease")
        return self.transition_task(task_id, TaskState.RUNNING.value, agent_id, {})

    def submit_for_review(self, task_id: str, agent_id: str) -> Task:
        task = self.get_task(task_id)
        if task.owner_agent_id != agent_id:
            raise AuthorizationError("agent does not own task lease")
        reviewed = self.transition_task(task_id, TaskState.NEEDS_REVIEW.value, agent_id, {})
        return reviewed

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
            lease_cursor = conn.execute(
                """
                UPDATE leases
                SET expires_at = ?, updated_at = ?
                WHERE id = ? AND agent_id = ? AND status = ?
                """,
                (expires_at, now, lease_id, agent_id, LeaseStatus.ACTIVE.value),
            )
            if lease_cursor.rowcount != 1:
                raise ValidationError("only active leases can be renewed")
            task_cursor = conn.execute(
                """
                UPDATE tasks
                SET leased_until = ?, updated_at = ?
                WHERE id = ?
                  AND lease_id = ?
                  AND owner_agent_id = ?
                  AND state IN (?, ?)
                """,
                (
                    expires_at,
                    now,
                    lease.task_id,
                    lease_id,
                    agent_id,
                    TaskState.CLAIMED.value,
                    TaskState.RUNNING.value,
                ),
            )
            if task_cursor.rowcount != 1:
                raise ValidationError("lease is no longer attached to an active task")
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = ?, updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (AgentStatus.BUSY.value, lease.task_id, now, now, agent_id),
            )
        self._record_history(lease.task_id, "task.lease_renewed", agent_id, None, None, {"lease_id": lease_id})
        self._maybe_poll_beads_bridge_on_heartbeat(self.get_agent(agent_id))
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
        hardware: Optional[Dict[str, Any]] = None,
    ) -> Machine:
        if not hostname:
            raise ValidationError("hostname is required")
        now = utcnow()
        mid = machine_id or new_id("machine")
        labels_json = self._resolved_json_column("machines", "labels", mid, labels)
        resources_json = self._resolved_json_column("machines", "resources", mid, resources)
        hardware_json = self._resolved_json_column("machines", "hardware", mid, hardware)
        self.store.execute(
            """
            INSERT INTO machines (id, hostname, labels, resources, trusted, created_at, updated_at, last_seen_at, hardware)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                hostname = excluded.hostname,
                labels = excluded.labels,
                resources = excluded.resources,
                trusted = excluded.trusted,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at,
                hardware = excluded.hardware
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
                hardware_json,
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
        hermes_instance_id: Optional[str] = None,
    ) -> Agent:
        self.get_machine(machine_id)
        if not name:
            raise ValidationError("agent name is required")
        if hermes_instance_id is not None:
            # Confirms the soul exists before binding. The identity layer
            # is what gates role assignment downstream.
            self.identity.get_hermes_instance(hermes_instance_id)
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
        # Preserve hermes_instance_id across re-registrations when the caller
        # didn't pass one, so an ops re-register doesn't accidentally orphan
        # the agent from its soul.
        if hermes_instance_id is None:
            existing_soul = self.store.query_one(
                "SELECT hermes_instance_id FROM agents WHERE id = ?", (aid,)
            )
            hermes_instance_id = (
                existing_soul["hermes_instance_id"] if existing_soul is not None else None
            )
        # Attestation key. mac-ng2: every agent gets an HMAC-SHA256 key
        # at first registration. The cleartext key is returned ONCE in
        # the registration response so the operator can deploy it to
        # the worker; the ciphertext is stored under the same Fernet
        # used for secrets. Re-registrations preserve the existing key
        # — rotating it would invalidate all in-flight signed evidence.
        attestation_key_plaintext: Optional[str] = None
        existing_key_row = self.store.query_one(
            "SELECT attestation_key_ciphertext FROM agents WHERE id = ?", (aid,)
        )
        if existing_key_row is not None and existing_key_row["attestation_key_ciphertext"]:
            attestation_ciphertext = existing_key_row["attestation_key_ciphertext"]
        else:
            attestation_key_plaintext = _generate_attestation_key()
            attestation_ciphertext = self.secrets._encrypt(attestation_key_plaintext)
        self.store.execute(
            """
            INSERT INTO agents (
                id, machine_id, name, capabilities, resources, status, health_status,
                current_task_id, created_at, updated_at, last_seen_at,
                hermes_instance_id, attestation_key_ciphertext
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                machine_id = excluded.machine_id,
                name = excluded.name,
                capabilities = excluded.capabilities,
                resources = excluded.resources,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at,
                hermes_instance_id = excluded.hermes_instance_id,
                attestation_key_ciphertext = excluded.attestation_key_ciphertext
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
                hermes_instance_id,
                attestation_ciphertext,
            ),
        )
        agent = self.get_agent(aid)
        # Stash the cleartext key on the returned agent so the API layer
        # can surface it to the caller on first registration. The Agent
        # dataclass itself never persists this — it's an attribute set
        # only on the in-memory object returned from this call.
        if attestation_key_plaintext is not None:
            agent.attestation_key = attestation_key_plaintext  # type: ignore[attr-defined]
        return agent

    def _agent_attestation_key(self, agent_id: str) -> Optional[str]:
        """Decrypted HMAC key for an agent, or None if the row predates
        the attestation-key column."""
        row = self.store.query_one(
            "SELECT attestation_key_ciphertext FROM agents WHERE id = ?", (agent_id,)
        )
        if row is None or not row["attestation_key_ciphertext"]:
            return None
        try:
            return self.secrets._decrypt(row["attestation_key_ciphertext"])
        except Exception:  # noqa: BLE001 - corrupt or rotated key shouldn't crash review
            return None

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
        agent_before = self.get_agent(agent_id)
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
        if status_value == AgentStatus.DRAINING.value and self._agent_has_active_lease(agent_id):
            updates.append("current_task_id = NULL")
        if status_value == AgentStatus.OFFLINE.value:
            self._expire_agent_active_leases(agent_id, now, "heartbeat_offline")
        if status_value in {AgentStatus.IDLE.value, AgentStatus.OFFLINE.value}:
            updates.append("current_task_id = NULL")
        params.append(agent_id)
        self.store.execute("UPDATE agents SET %s WHERE id = ?" % ", ".join(updates), tuple(params))
        agent = self.get_agent(agent_id)
        self._maybe_poll_beads_bridge_on_heartbeat(agent_before)
        return agent

    def _maybe_poll_beads_bridge_on_heartbeat(self, agent: Agent) -> None:
        if not _truthy_env("MAC_BEADS_BRIDGE_ON_HEARTBEAT"):
            return
        hub_agent = os.environ.get("MAC_BEADS_BRIDGE_HUB_AGENT", "rocky").strip()
        if hub_agent and agent.name != hub_agent and agent.id != hub_agent:
            return
        try:
            self.poll_beads_repositories(actor=agent.id)
        except Exception as exc:  # noqa: BLE001 - heartbeat liveness must survive bridge failures.
            try:
                self.record_log(
                    "bridge.beads.heartbeat_poll_failed",
                    layer="control_plane",
                    source=agent.id,
                    level="warning",
                    detail={"error": str(exc)},
                )
            except Exception:
                pass

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
        unmatched: List[Task] = []
        for task in tasks:
            matched = False
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
            if not matched:
                unmatched.append(task)
        # No agent could claim any pending task. Emit a provisioning
        # signal so a future provisioner (k8s operator, nomad job, local
        # spawner) can spin up the kind of agent that's missing. Today
        # the row + observability log are the signal; no auto-spawn.
        for task in unmatched:
            self._emit_dispatch_provisioning_signal(task)
        return None

    def _emit_dispatch_provisioning_signal(self, task: Task) -> None:
        required_role = None
        hardware: JsonDict = {}
        if isinstance(task.metadata, dict):
            md_role = task.metadata.get("required_role")
            if isinstance(md_role, str) and md_role.strip():
                required_role = md_role.strip()
            md_hw = task.metadata.get("hardware")
            if isinstance(md_hw, dict):
                hardware = md_hw
        self.provisioning.request_agent(
            reason="dispatch.no_eligible_agent",
            role_slug=required_role,
            capabilities=list(task.required_capabilities or []),
            hardware=hardware,
            task_id=task.id,
            tenant_id=self._task_tenant_id(task),
            detail={
                "task_state": task.state,
                "task_title": task.title,
            },
        )

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
        review_workflows = self.advance_default_review_workflows(limit=limit)
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
            "review_workflows": review_workflows,
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
        publication = self.reviews.publish_task(*args, **kwargs)
        self._sync_beads_close(self.get_task(publication.task_id), publication.created_by)
        # publish_task transitions the underlying task to COMPLETED inside
        # its own transaction (bypassing transition_task), so we run the
        # workflow runtime hook here so workflow runs advance on publish.
        row = self.store.query_one(
            "SELECT workflow_run_id FROM tasks WHERE id = ?", (publication.task_id,)
        )
        if row is not None and row["workflow_run_id"]:
            try:
                self.workflow_runtime.on_task_completed(
                    publication.task_id, TaskState.COMPLETED.value
                )
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).exception(
                    "workflow runtime failed to advance after publish_task"
                )
        return publication

    def get_publication(self, publication_id: str) -> Publication:
        return self.reviews.get_publication(publication_id)

    def list_publications(self, *args: Any, **kwargs: Any) -> List[Publication]:
        return self.reviews.list_publications(*args, **kwargs)

    def advance_default_review_workflows(
        self,
        limit: int = 100,
        actor: str = "default-review-workflow",
        tenant_id: Optional[str] = None,
    ) -> JsonDict:
        """Sweep reviewable tasks. When ``tenant_id`` is set, only tasks
        owned by that tenant are processed — operator tools call this
        scoped, and the autonomous-tick endpoint passes their tenant
        through. Without a filter, sweeps everything (admin / single-
        tenant deployments)."""
        results = []
        for task in self.list_tasks():
            if task.state not in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}:
                continue
            if tenant_id is not None and self._task_tenant_id(task) != tenant_id:
                continue
            results.append(self.advance_default_review_workflow(task.id, actor=actor))
            if len(results) >= max(1, int(limit)):
                break
        return {
            "processed": len(results),
            "results": results,
        }

    def advance_default_review_workflow(
        self,
        task_id: str,
        actor: str = "default-review-workflow",
    ) -> JsonDict:
        task = self.get_task(task_id)
        if task.state == TaskState.COMPLETED.value:
            return {"task_id": task_id, "status": "already_completed"}
        if task.state not in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}:
            return {"task_id": task_id, "status": "not_reviewable", "state": task.state}
        existing_publications = [
            publication
            for publication in self.list_publications(task_id)
            if publication.status == PublicationStatus.PUBLISHED.value
        ]
        if existing_publications:
            return {
                "task_id": task_id,
                "status": "already_published",
                "publication_id": existing_publications[-1].id,
            }
        if self._default_review_disabled(task):
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.skipped",
                "info",
                {"reason": "disabled_by_task_policy"},
                actor,
            )
            return {"task_id": task_id, "status": "disabled_by_task_policy"}

        evidence, evidence_assessment = self._default_review_evidence(task)
        if evidence is None:
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.waiting",
                "warning",
                evidence_assessment,
                actor,
            )
            return {
                "task_id": task_id,
                "status": "waiting_for_verifiable_evidence",
                **evidence_assessment,
            }

        # If the task has more than one pending review, refuse to act —
        # the ambiguous state has no clear winner and the autonomous
        # swarm shouldn't silently pick one (mac-d9c).
        pending_reviews = [
            r for r in self.list_reviews(task_id)
            if r.status == ReviewStatus.PENDING.value
        ]
        if len(pending_reviews) > 1:
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.ambiguous",
                "warning",
                {
                    "reason": "multiple_pending_reviews",
                    "pending_review_ids": [r.id for r in pending_reviews],
                },
                actor,
            )
            return {
                "task_id": task_id,
                "status": "ambiguous_pending_reviews",
                "pending_review_ids": [r.id for r in pending_reviews],
            }

        review = self._default_review_for_task(task_id)
        if review is None:
            reviewer = self._select_default_reviewer(task)
            if reviewer is None:
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.waiting",
                    "warning",
                    {"reason": "no_eligible_reviewer"},
                    actor,
                )
                # Signal that the swarm needs a reviewer-capable agent it
                # doesn't have. The default-review workflow will pick the
                # request up on a future tick once the provisioner has
                # registered a matching agent.
                self.provisioning.request_agent(
                    reason="review.no_eligible_reviewer",
                    capabilities=["review"],
                    task_id=task_id,
                    tenant_id=self._task_tenant_id(task),
                    detail={
                        "evidence_type": evidence_assessment.get("evidence_type"),
                    },
                )
                return {"task_id": task_id, "status": "waiting_for_reviewer"}
            review = self.request_review(task_id, reviewer.id, actor=actor)
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.assigned",
                "info",
                {"review_id": review.id, "reviewer_agent_id": reviewer.id},
                actor,
            )

        if review.status == ReviewStatus.PENDING.value:
            # mac-jqb: the workflow no longer self-approves. It requires
            # the reviewer agent to have produced a *review verdict*
            # evidence row — a separate, signed manifest authored by
            # the reviewer (not the executor) declaring approve/reject.
            # Until that exists, the review stays pending. This makes
            # the second-eyes role actually do work; today the workflow
            # waits for the verdict, and a follow-up review-executor
            # worker will produce it automatically.
            verdict_evidence, verdict_problems = self._find_review_verdict_evidence(
                task_id, review.reviewer_agent_id, executor_evidence_id=evidence.id
            )
            if verdict_evidence is None:
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.waiting_for_verdict",
                    "warning",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "evidence_id": evidence.id,
                        "problems": verdict_problems,
                    },
                    actor,
                )
                # Nudge the reviewer so an autonomous review-executor
                # has something to react to.
                self.send_message(
                    "dispatcher",
                    review.reviewer_agent_id,
                    MessageType.NUDGE.value,
                    {
                        "task_id": task_id,
                        "review_id": review.id,
                        "executor_evidence_id": evidence.id,
                        "reason": "produce_review_verdict",
                    },
                    task_id=task_id,
                )
                return {
                    "task_id": task_id,
                    "status": "waiting_for_reviewer_verdict",
                    "review_id": review.id,
                    "reviewer_agent_id": review.reviewer_agent_id,
                    "executor_evidence_id": evidence.id,
                    "problems": verdict_problems,
                }
            verdict_value = self._verdict_value(verdict_evidence)
            if verdict_value == "rejected":
                review = self.submit_review(
                    review.id,
                    ReviewStatus.REJECTED.value,
                    review.reviewer_agent_id,
                    reason="reviewer rejected via signed verdict evidence",
                    evidence_id=verdict_evidence.id,
                )
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.rejected",
                    "warning",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "verdict_evidence_id": verdict_evidence.id,
                    },
                    actor,
                )
            else:
                review = self.submit_review(
                    review.id,
                    ReviewStatus.APPROVED.value,
                    review.reviewer_agent_id,
                    reason="reviewer approved via signed verdict evidence",
                    evidence_id=verdict_evidence.id,
                )
                self._record_default_review_observation(
                    task_id,
                    "workflow.default_review.approved",
                    "info",
                    {
                        "review_id": review.id,
                        "reviewer_agent_id": review.reviewer_agent_id,
                        "verdict_evidence_id": verdict_evidence.id,
                        "executor_evidence_id": evidence.id,
                        "evidence_type": evidence_assessment.get("evidence_type"),
                    },
                    actor,
                )
            # The publication evidence below stays as the executor's
            # signed work — that's the artifact being published. The
            # reviewer's verdict was just consumed onto the review row
            # via submit_review(evidence_id=verdict_evidence.id) above.

        if review.status != ReviewStatus.APPROVED.value:
            return {
                "task_id": task_id,
                "status": "review_not_approved",
                "review_id": review.id,
                "review_status": review.status,
            }

        task = self.get_task(task_id)
        if task.state != TaskState.REVIEWING.value:
            return {
                "task_id": task_id,
                "status": "approved_not_publishable",
                "state": task.state,
                "review_id": review.id,
            }
        if self.reviews.task_requires_publication_evidence(task):
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.waiting",
                "warning",
                {
                    "reason": "publication_evidence_required",
                    "review_id": review.id,
                    "evidence_id": evidence.id,
                },
                actor,
            )
            return {
                "task_id": task_id,
                "status": "waiting_for_publication_evidence",
                "review_id": review.id,
            }

        target = self._default_publication_target(task)
        if target is None:
            # No operator-configured publication destination; refuse to
            # invent one. The review is approved, but the task stays in
            # REVIEWING until an operator sets metadata.publication_target
            # (mac-w29).
            self._record_default_review_observation(
                task_id,
                "workflow.default_review.no_publication_target",
                "warning",
                {"review_id": review.id, "evidence_id": evidence.id},
                actor,
            )
            return {
                "task_id": task_id,
                "status": "waiting_for_publication_target",
                "review_id": review.id,
            }
        publication = self.publish_task(
            task_id,
            target,
            review.reviewer_agent_id,
            evidence_id=evidence.id,
        )
        self._record_default_review_observation(
            task_id,
            "workflow.default_review.published",
            "info",
            {
                "review_id": review.id,
                "publication_id": publication.id,
                "target": publication.target,
            },
            actor,
        )
        return {
            "task_id": task_id,
            "status": "published",
            "review_id": review.id,
            "publication_id": publication.id,
        }

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

    def _seed_beads_repositories_from_env(self) -> None:
        """Register operator-configured Beads repos once at process startup.

        Format:
            MAC_BEADS_REPOSITORIES="mac=/path/to/repo:repo-beads-mac:mac:python,ops:60;ACC=/path"

        Only the name and path are required. The remaining colon-delimited
        fields are source, project, required capabilities, and poll interval.
        Legacy pipe-delimited values are also accepted for compatibility.
        Bad entries are logged instead of failing service startup.
        """
        raw = os.environ.get("MAC_BEADS_REPOSITORIES", "").strip()
        if not raw and _truthy_env("MAC_BEADS_AUTO_REGISTER_SELF"):
            self_repo = os.environ.get("MAC_SELF_UPDATE_REPO", "").strip()
            if self_repo:
                raw = "mac=%s|repo-beads-mac|repo-beads-mac||60" % self_repo
        if not raw:
            return
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if "=" not in entry:
                    raise ValidationError("entry must be name=path")
                name, rest = entry.split("=", 1)
                parts = rest.split("|") if "|" in rest else rest.split(":")
                path = parts[0].strip()
                source = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                project = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
                caps = parts[3].strip() if len(parts) > 3 else ""
                interval = int(parts[4]) if len(parts) > 4 and parts[4].strip() else 60
                self.register_beads_repository(
                    name.strip(),
                    path,
                    source=source,
                    project=project,
                    required_capabilities=[item.strip() for item in caps.split("+") if item.strip()]
                    if "+" in caps
                    else [item.strip() for item in caps.split(",") if item.strip()],
                    poll_interval_seconds=interval,
                    actor="env",
                )
            except Exception as exc:  # noqa: BLE001 - bad env should not kill the API.
                try:
                    self.record_log(
                        "bridge.beads_repository.seed_failed",
                        layer="control_plane",
                        source="env",
                        level="warning",
                        detail={"entry": entry, "error": str(exc)},
                    )
                except Exception:
                    pass

    def import_project_item(
        self,
        source: str,
        external_id: str,
        title: str,
        payload: Dict[str, Any],
        required_capabilities: Optional[Iterable[str]] = None,
        *,
        description: Optional[str] = None,
        project: Optional[str] = None,
        priority: int = 0,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "bridge",
    ) -> ProjectItem:
        existing = self.store.query_one(
            "SELECT * FROM project_items WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        if existing is not None:
            return self._project_item_from_row(existing)
        task_metadata = {"source": source, "external_id": external_id}
        task_metadata.update(ensure_json_object(metadata))
        task = self.create_task(
            title,
            description=description if description is not None else json_dumps(payload),
            project=project or source,
            priority=priority,
            required_capabilities=required_capabilities,
            dependencies=dependencies,
            metadata=task_metadata,
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

    def register_beads_repository(
        self,
        name: str,
        path: str,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "beads-bridge",
    ) -> BeadsRepository:
        name = name.strip()
        if not name:
            raise ValidationError("beads repository name is required")
        repo_path = str(Path(path).expanduser())
        repo_source = (source or "repo-beads-%s" % _safe_slug(name)).strip()
        if not repo_source:
            raise ValidationError("beads repository source is required")
        repo_project = (project or repo_source).strip()
        now = utcnow()
        row = self.store.query_one("SELECT id FROM beads_repositories WHERE name = ?", (name,))
        repo_id = row["id"] if row is not None else new_id("beadsrepo")
        self.store.execute(
            """
            INSERT INTO beads_repositories (
                id, name, path, source, project, required_capabilities,
                enabled, poll_interval_seconds, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path = excluded.path,
                source = excluded.source,
                project = excluded.project,
                required_capabilities = excluded.required_capabilities,
                enabled = excluded.enabled,
                poll_interval_seconds = excluded.poll_interval_seconds,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                repo_id,
                name,
                repo_path,
                repo_source,
                repo_project,
                json_dumps(coerce_list(required_capabilities)),
                1 if enabled else 0,
                max(1, int(poll_interval_seconds)),
                json_dumps(ensure_json_object(metadata)),
                now,
                now,
            ),
        )
        self.record_log(
            "bridge.beads_repository.registered",
            layer="control_plane",
            source=actor,
            subject_type="environment",
            subject_id=repo_id,
            detail={"name": name, "path": repo_path, "source": repo_source, "enabled": enabled},
        )
        return self.get_beads_repository(repo_id)

    def get_beads_repository(self, repo_id_or_name: str) -> BeadsRepository:
        row = self.store.query_one(
            "SELECT * FROM beads_repositories WHERE id = ? OR name = ?",
            (repo_id_or_name, repo_id_or_name),
        )
        if row is None:
            raise NotFoundError("beads repository not found: %s" % repo_id_or_name)
        return self._beads_repository_from_row(row)

    def list_beads_repositories(self, enabled: Optional[bool] = None) -> List[BeadsRepository]:
        if enabled is None:
            rows = self.store.query_all("SELECT * FROM beads_repositories ORDER BY name, id")
        else:
            rows = self.store.query_all(
                "SELECT * FROM beads_repositories WHERE enabled = ? ORDER BY name, id",
                (1 if enabled else 0,),
            )
        return [self._beads_repository_from_row(row) for row in rows]

    def poll_beads_repositories(
        self,
        repo_id_or_name: Optional[str] = None,
        *,
        force: bool = False,
        actor: str = "beads-bridge",
    ) -> JsonDict:
        if repo_id_or_name:
            repos = [self.get_beads_repository(repo_id_or_name)]
        else:
            repos = self.list_beads_repositories(enabled=True)
        report: JsonDict = {
            "schema": "mac.beads_bridge.poll.v1",
            "actor": actor,
            "repository_count": len(repos),
            "imported_count": 0,
            "existing_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "repositories": [],
        }
        for repo in repos:
            repo_report = self._poll_beads_repository(repo, force=force, actor=actor)
            report["repositories"].append(repo_report)
            report["imported_count"] += int(repo_report.get("imported_count", 0))
            report["existing_count"] += int(repo_report.get("existing_count", 0))
            report["skipped_count"] += int(repo_report.get("skipped_count", 0))
            if repo_report.get("status") == "error":
                report["error_count"] += 1
        if report["imported_count"] or report["error_count"]:
            self.record_log(
                "bridge.beads.poll",
                layer="control_plane",
                source=actor,
                detail=report,
            )
        return report

    def _poll_beads_repository(
        self,
        repo: BeadsRepository,
        *,
        force: bool,
        actor: str,
    ) -> JsonDict:
        now = utcnow()
        if not force and repo.last_polled_at:
            elapsed = parse_time(now) - parse_time(repo.last_polled_at)
            if elapsed.total_seconds() < repo.poll_interval_seconds:
                return {
                    "repository_id": repo.id,
                    "name": repo.name,
                    "status": "not_due",
                    "next_poll_in_seconds": repo.poll_interval_seconds - int(elapsed.total_seconds()),
                    "imported_count": 0,
                    "existing_count": 0,
                    "skipped_count": 0,
                }
        try:
            issues = self._ready_beads_issues(repo)
            imported = 0
            existing = 0
            for issue in issues:
                prior = self.store.query_one(
                    "SELECT id, task_id FROM project_items WHERE source = ? AND external_id = ?",
                    (repo.source, str(issue["id"])),
                )
                self._import_bead_issue(repo, issue, actor=actor)
                if prior is None:
                    imported += 1
                else:
                    existing += 1
                    self._sync_existing_beads_task(self.get_task(prior["task_id"]), actor)
            self._update_beads_repository_poll_state(
                repo.id,
                now,
                last_imported_at=now if imported else repo.last_imported_at,
                last_error=None,
            )
            return {
                "repository_id": repo.id,
                "name": repo.name,
                "status": "ok",
                "ready_count": len(issues),
                "imported_count": imported,
                "existing_count": existing,
                "skipped_count": 0,
            }
        except Exception as exc:  # noqa: BLE001 - one broken repo must not break heartbeats.
            self._update_beads_repository_poll_state(
                repo.id,
                now,
                last_imported_at=repo.last_imported_at,
                last_error=str(exc),
            )
            return {
                "repository_id": repo.id,
                "name": repo.name,
                "status": "error",
                "error": str(exc),
                "imported_count": 0,
                "existing_count": 0,
                "skipped_count": 0,
            }

    def _ready_beads_issues(self, repo: BeadsRepository) -> List[JsonDict]:
        repo_path = Path(repo.path).expanduser()
        if not repo_path.exists():
            raise ValidationError("beads repository path does not exist: %s" % repo.path)
        if repo_path.is_file():
            return self._ready_beads_issues_from_jsonl(repo_path)
        try:
            completed = subprocess.run(
                [_beads_cli(), "ready", "--json"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            output = completed.stdout.strip()
            if not output:
                return []
            data = json_loads(output, [])
            if not isinstance(data, list):
                raise ValidationError("bd ready --json did not return a list for %s" % repo.path)
            return [issue for issue in data if self._bead_issue_is_importable(issue)]
        return self._ready_beads_issues_from_jsonl(repo_path / ".beads" / "issues.jsonl")

    def _ready_beads_issues_from_jsonl(self, issues_path: Path) -> List[JsonDict]:
        if not issues_path.exists():
            raise ValidationError("beads issues file not found: %s" % issues_path)
        issues: List[JsonDict] = []
        for raw in issues_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            issue = json_loads(raw, {})
            if isinstance(issue, dict) and issue.get("_type", "issue") == "issue":
                issues.append(issue)
        by_id = {str(issue.get("id")): issue for issue in issues if issue.get("id")}
        ready: List[JsonDict] = []
        for issue in issues:
            if not self._bead_issue_is_importable(issue):
                continue
            dependencies = issue.get("dependencies") or []
            if int(issue.get("dependency_count") or 0) > 0 and not dependencies:
                continue
            blocked = False
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    blocked = True
                    break
                dep_id = str(dependency.get("depends_on_id") or "").strip()
                dep_issue = by_id.get(dep_id)
                if dep_issue is None or str(dep_issue.get("status") or "") != "closed":
                    blocked = True
                    break
            if not blocked:
                ready.append(issue)
        ready.sort(key=lambda item: (int(item.get("priority") or 2), str(item.get("created_at") or ""), str(item.get("id") or "")))
        return ready

    def _bead_issue_is_importable(self, issue: Any) -> bool:
        if not isinstance(issue, dict):
            return False
        if not str(issue.get("id") or "").strip():
            return False
        return str(issue.get("status") or "").strip().lower() == "open"

    def _import_bead_issue(self, repo: BeadsRepository, issue: JsonDict, actor: str) -> ProjectItem:
        issue_id = str(issue["id"])
        priority = 100 - int(issue.get("priority") or 2)
        payload = {
            "schema": "mac.beads_bridge.issue.v1",
            "repository": repo.to_dict(),
            "issue": issue,
        }
        metadata = {
            "origin": {
                "type": "beads",
                "repository_id": repo.id,
                "repository_name": repo.name,
                "repository_path": repo.path,
                "source": repo.source,
                "bead_id": issue_id,
            },
            "acc_metadata": {
                "source": "mac-beads-bridge",
                "beads_id": issue_id,
                "beads_path": str(Path(repo.path).expanduser() / ".beads" / "issues.jsonl"),
                "repo_beads_workflow": True,
                "workflow_role": "work",
                "beads_sync_claim_on_claim": True,
                "beads_sync_close_on_complete": True,
            },
        }
        return self.import_project_item(
            repo.source,
            issue_id,
            str(issue.get("title") or issue_id),
            payload,
            required_capabilities=repo.required_capabilities,
            description=str(issue.get("description") or ""),
            project=repo.project,
            priority=priority,
            metadata=metadata,
            actor=actor,
        )

    def _update_beads_repository_poll_state(
        self,
        repo_id: str,
        last_polled_at: str,
        *,
        last_imported_at: Optional[str],
        last_error: Optional[str],
    ) -> None:
        self.store.execute(
            """
            UPDATE beads_repositories
            SET last_polled_at = ?, last_imported_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (last_polled_at, last_imported_at, last_error, utcnow(), repo_id),
        )

    def _beads_binding_for_task(self, task: Task) -> Optional[JsonDict]:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        origin = metadata.get("origin")
        acc_metadata = metadata.get("acc_metadata")
        if not isinstance(origin, dict) or origin.get("type") != "beads":
            return None
        if not isinstance(acc_metadata, dict):
            return None
        bead_id = str(origin.get("bead_id") or acc_metadata.get("beads_id") or "").strip()
        repo_path = str(origin.get("repository_path") or "").strip()
        if not bead_id or not repo_path:
            return None
        return {"bead_id": bead_id, "repo_path": repo_path}

    def _run_bd_for_task(self, task: Task, args: List[str], actor: str, action: str) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        repo_path = Path(binding["repo_path"]).expanduser()
        if not repo_path.exists():
            return
        try:
            completed = subprocess.run(
                [_beads_cli(), "--actor", actor, *args],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                raise ValidationError((completed.stderr or completed.stdout or "").strip())
            self.record_log(
                "bridge.beads.sync.%s" % action,
                layer="control_plane",
                source=actor,
                subject_type="task",
                subject_id=task.id,
                detail={"bead_id": binding["bead_id"], "repo_path": str(repo_path)},
            )
        except Exception as exc:  # noqa: BLE001 - Beads sync is secondary to task state.
            self.record_log(
                "bridge.beads.sync_failed",
                layer="control_plane",
                source=actor,
                level="warning",
                subject_type="task",
                subject_id=task.id,
                detail={
                    "action": action,
                    "bead_id": binding["bead_id"],
                    "repo_path": str(repo_path),
                    "error": str(exc),
                },
            )

    def _sync_beads_claim(self, task: Task, actor: str) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        acc_metadata = task.metadata.get("acc_metadata") if isinstance(task.metadata, dict) else {}
        if isinstance(acc_metadata, dict) and acc_metadata.get("beads_sync_claim_on_claim") is False:
            return
        self._run_bd_for_task(task, ["update", binding["bead_id"], "--claim"], actor, "claim")

    def _sync_existing_beads_task(self, task: Task, actor: str) -> None:
        if task.state not in {
            TaskState.CLAIMED.value,
            TaskState.RUNNING.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.REVIEWING.value,
        }:
            return
        self._sync_beads_claim(task, task.owner_agent_id or actor)

    def _sync_beads_close(self, task: Task, actor: str) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        acc_metadata = task.metadata.get("acc_metadata") if isinstance(task.metadata, dict) else {}
        if isinstance(acc_metadata, dict) and acc_metadata.get("beads_sync_close_on_complete") is False:
            return
        self._run_bd_for_task(
            task,
            [
                "close",
                binding["bead_id"],
                "--reason",
                "Completed by mac task %s" % task.id,
            ],
            actor,
            "close",
        )

    def _sync_beads_reopen(
        self,
        task: Task,
        actor: str,
        state: str,
        detail: Dict[str, Any],
    ) -> None:
        binding = self._beads_binding_for_task(task)
        if binding is None:
            return
        reason = "mac task %s moved to %s: %s" % (
            task.id,
            state,
            json_dumps(detail or {}),
        )
        self._run_bd_for_task(
            task,
            ["update", binding["bead_id"], "--status", "open", "--append-notes", reason],
            actor,
            "reopen",
        )

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

    # Rollouts: thin facade over ``self.rollouts``.

    def create_rollout(self, *args: Any, **kwargs: Any) -> Rollout:
        return self.rollouts.create_rollout(*args, **kwargs)

    def get_rollout(self, rollout_id: str) -> Rollout:
        return self.rollouts.get_rollout(rollout_id)

    def list_rollouts(self, *args: Any, **kwargs: Any) -> List[Rollout]:
        return self.rollouts.list_rollouts(*args, **kwargs)

    def list_rollout_events(self, rollout_id: str) -> List[JsonDict]:
        return self.rollouts.list_rollout_events(rollout_id)

    def verify_rollout_artifact(self, *args: Any, **kwargs: Any) -> Rollout:
        return self.rollouts.verify_rollout_artifact(*args, **kwargs)

    def advance_rollout(self, *args: Any, **kwargs: Any) -> Rollout:
        return self.rollouts.advance_rollout(*args, **kwargs)

    def evaluate_rollout_health(self, *args: Any, **kwargs: Any) -> JsonDict:
        return self.rollouts.evaluate_rollout_health(*args, **kwargs)

    def rescue_rollout(self, *args: Any, **kwargs: Any) -> Tuple[Rollout, Task]:
        return self.rollouts.rescue_rollout(*args, **kwargs)


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
        keys = row.keys() if hasattr(row, "keys") else []
        hardware = json_loads(row["hardware"], {}) if "hardware" in keys else {}
        return Machine(
            row["id"],
            row["hostname"],
            json_loads(row["labels"], {}),
            json_loads(row["resources"], {}),
            bool(row["trusted"]),
            row["created_at"],
            row["updated_at"],
            row["last_seen_at"],
            hardware,
        )

    def _agent_from_row(self, row: Any) -> Agent:
        keys = row.keys() if hasattr(row, "keys") else []
        running_digest = row["running_digest"] if "running_digest" in keys else None
        role_id = row["role_id"] if "role_id" in keys else None
        hermes_instance_id = (
            row["hermes_instance_id"] if "hermes_instance_id" in keys else None
        )
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
            role_id,
            hermes_instance_id,
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

    def _beads_repository_from_row(self, row: Any) -> BeadsRepository:
        return BeadsRepository(
            row["id"],
            row["name"],
            row["path"],
            row["source"],
            row["project"],
            json_loads(row["required_capabilities"], []),
            bool(row["enabled"]),
            int(row["poll_interval_seconds"]),
            row["last_polled_at"],
            row["last_imported_at"],
            row["last_error"],
            json_loads(row["metadata"], {}),
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
        # Role + hardware gates. Both no-op when neither the agent nor the
        # task carry role/hardware metadata, so the legacy capability path
        # below stays the dominant matcher for un-roled fleets.
        required_role = task.metadata.get("required_role") if isinstance(task.metadata, dict) else None
        if required_role:
            if agent.role_id is None:
                return False
            try:
                role = self.roles.get_role(agent.role_id)
            except NotFoundError:
                return False
            if role.slug != required_role:
                return False
        role_required_caps: set = set()
        if agent.role_id is not None:
            try:
                role = self.roles.get_role(agent.role_id)
            except NotFoundError:
                role = None
            if role is not None:
                ok, _reasons = self.roles.validate_hardware(role, machine)
                if not ok:
                    return False
                # Soul-role compatibility is re-checked at dispatch time
                # rather than only at assignment time, so a persona edit
                # that narrows the allowed role list immediately stops
                # affected agents from being eligible.
                if not self.roles.soul_accepts_role(agent, role):
                    return False
                role_required_caps = set(role.required_capabilities)
        capabilities = set(agent.capabilities)
        required = set(task.required_capabilities) | role_required_caps
        return required.issubset(capabilities)

    def _default_review_disabled(self, task: Task) -> bool:
        policy = task.metadata.get("review") or task.metadata.get("default_review") or {}
        if not isinstance(policy, dict):
            return False
        mode = str(policy.get("mode") or policy.get("workflow") or "").strip().lower()
        return (
            mode == "manual"
            or policy.get("manual") is True
            or policy.get("auto") is False
            or policy.get("enabled") is False
        )

    def _default_review_evidence(self, task: Task) -> Tuple[Optional[Evidence], JsonDict]:
        evidence = self.list_evidence(task.id)
        if not evidence:
            return None, {"reason": "no_evidence"}
        successful = [
            item
            for item in evidence
            if self._evidence_returncode(item) == 0
        ]
        if not successful:
            return None, {"reason": "no_successful_evidence"}
        rejected: List[JsonDict] = []
        for item in reversed(successful):
            assessment = self._assess_default_review_evidence(task, item)
            if assessment["valid"]:
                return item, assessment
            rejected.append(
                {
                    "evidence_id": item.id,
                    "reason": assessment["reason"],
                    "problems": assessment.get("problems", []),
                }
            )
        return None, {
            "reason": "evidence_not_verifiable",
            "rejected_evidence": rejected[:5],
        }

    def _assess_default_review_evidence(self, task: Task, evidence: Evidence) -> JsonDict:
        if self._evidence_returncode(evidence) != 0:
            return {
                "valid": False,
                "reason": "executor_not_successful",
                "problems": ["evidence returncode is not zero"],
            }
        manifest = evidence.metadata.get("verification") or evidence.metadata.get("mac_evidence")
        if not isinstance(manifest, dict):
            return {
                "valid": False,
                "reason": "missing_verification_manifest",
                "problems": ["evidence metadata lacks verification manifest"],
            }
        problems: List[str] = []
        schema = str(manifest.get("schema") or "").strip()
        if schema != VERIFICATION_SCHEMA:
            problems.append("verification.schema must be %s" % VERIFICATION_SCHEMA)
        # Canonical names only (mac-q38). Aliases were a maintainability
        # multiplier — every alias is a separate door downstream
        # validation must remember. Status must be ``complete``; the
        # alternative aliases (verified/pass/passed) are rejected at the
        # boundary. Same for evidence_type below.
        status = str(manifest.get("status") or "").strip().lower()
        if status != "complete":
            problems.append('verification.status must be "complete"')
        evidence_type = str(manifest.get("evidence_type") or "").strip().lower()
        if not evidence_type:
            problems.append("verification.evidence_type is required")
        if problems:
            return {
                "valid": False,
                "reason": "invalid_verification_manifest",
                "evidence_type": evidence_type or None,
                "problems": problems,
            }
        # Root of trust (mac-ng2). The verification manifest must carry
        # ``signed_by`` (an agent_id) and ``signature`` (HMAC v1) made
        # with that agent's attestation key. Without this any executor
        # could self-approve by writing valid-looking JSON. Verification
        # is per-agent: the signer's key must be on file in the
        # ``agents.attestation_key_ciphertext`` column.
        signed_by = str(manifest.get("signed_by") or "").strip()
        signature = str(manifest.get("signature") or "").strip()
        if not signed_by or not signature:
            return {
                "valid": False,
                "reason": "manifest_not_signed",
                "evidence_type": evidence_type,
                "problems": ["verification.signed_by and verification.signature are required"],
            }
        signer_key = self._agent_attestation_key(signed_by)
        if signer_key is None:
            return {
                "valid": False,
                "reason": "signer_unknown",
                "evidence_type": evidence_type,
                "problems": ["verification.signed_by does not match a known agent with an attestation key"],
            }
        if not verify_verification_manifest_signature(signer_key, manifest, signature):
            return {
                "valid": False,
                "reason": "signature_invalid",
                "evidence_type": evidence_type,
                "problems": ["verification.signature does not verify against signed_by's attestation key"],
            }
        type_problems = self._verification_type_problems(task, manifest, evidence_type)
        if type_problems:
            return {
                "valid": False,
                "reason": "verification_contract_failed",
                "evidence_type": evidence_type,
                "problems": type_problems,
            }
        return {
            "valid": True,
            "reason": "verification_contract_satisfied",
            "evidence_type": evidence_type,
            "signed_by": signed_by,
            "verified_by": "default-review-evidence-v1",
        }

    def _verification_type_problems(
        self,
        task: Task,
        manifest: JsonDict,
        evidence_type: str,
    ) -> List[str]:
        # Canonical evidence_type vocabulary (mac-q38):
        #   repo_change | documentation | deployment | test | artifact | no_change
        # Aliases (code/git/investigation/decision_record) are rejected.
        if evidence_type == "repo_change":
            return self._repo_verification_problems(manifest, require_tests=True)
        if evidence_type == "documentation":
            repo_problems = self._repo_verification_problems(manifest, require_tests=False)
            if not repo_problems:
                return []
            artifacts = _manifest_list(manifest.get("artifacts"))
            if artifacts and self._passed_verification_check_count(manifest) > 0:
                return []
            return [
                "documentation evidence requires a pushed repo artifact "
                "or explicit artifacts plus passing checks"
            ]
        if evidence_type == "deployment":
            problems: List[str] = []
            if self._passed_verification_check_count(manifest) < 1:
                problems.append("deployment evidence requires at least one passing check")
            if not (
                _manifest_list(manifest.get("targets"))
                or _manifest_list(manifest.get("services"))
                or _manifest_list(manifest.get("artifacts"))
            ):
                problems.append("deployment evidence requires targets, services, or artifacts")
            return problems
        if evidence_type in {"test", "artifact"}:
            problems = []
            if self._passed_verification_check_count(manifest) < 1:
                problems.append("%s evidence requires at least one passing check or test" % evidence_type)
            if evidence_type == "artifact" and not _manifest_list(manifest.get("artifacts")):
                problems.append("artifact evidence requires artifacts")
            return problems
        if evidence_type == "no_change":
            if not str(manifest.get("reason") or manifest.get("no_change_reason") or "").strip():
                return ["no_change evidence requires a reason"]
            if self._passed_verification_check_count(manifest) < 1:
                return ["no_change evidence requires at least one passing check"]
            return []
        return ["unsupported verification.evidence_type: %s" % evidence_type]

    def _repo_verification_problems(self, manifest: JsonDict, require_tests: bool) -> List[str]:
        # Canonical field names only (mac-q38). The previous code
        # accepted ``git``/``commit``/``commit_sha``/``changed_files``/
        # ``pushed_ref``/``pull_request_url``/etc. — each alias is a
        # separate doorway. Single canonical schema:
        #   verification.repo: { head_sha, files_changed, dirty, pushed,
        #                        remote_ref, pr_url? }
        repo = manifest.get("repo")
        if not isinstance(repo, dict):
            return ["repo evidence requires verification.repo object"]
        problems: List[str] = []
        head_sha = str(repo.get("head_sha") or "").strip()
        if not _GIT_SHA_RE.match(head_sha):
            problems.append("repo.head_sha must be a git SHA")
        files_changed = _manifest_list(repo.get("files_changed"))
        if not files_changed:
            problems.append("repo evidence requires changed files")
        dirty = repo.get("dirty")
        if dirty not in {False, "false", "False", 0, "0"}:
            problems.append("repo evidence must declare dirty=false")
        pushed = repo.get("pushed") is True or str(repo.get("pushed") or "").lower() == "true"
        remote_ref = str(repo.get("remote_ref") or "").strip()
        pr_url = str(repo.get("pr_url") or "").strip()
        if not (pushed and remote_ref) and not pr_url:
            problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
        if require_tests and self._passed_verification_check_count(manifest) < 1:
            problems.append("repo code evidence requires at least one passing test/check")
        return problems

    def _passed_verification_check_count(self, manifest: JsonDict) -> int:
        # Canonical names only (mac-q38): ``tests`` and ``checks``.
        # ``test_runs`` was an alias; rejecting it here.
        count = 0
        for item in _manifest_list(manifest.get("tests")):
            if self._verification_item_passed(item):
                count += 1
        for item in _manifest_list(manifest.get("checks")):
            if self._verification_item_passed(item):
                count += 1
        return count

    def _verification_item_passed(self, item: Any) -> bool:
        # Canonical: returncode=0 OR status=="pass" (mac-q38). The
        # earlier accept-list (passed/success/succeeded/ok plus a
        # ``result`` alias) was four extra doors; reject them.
        if not isinstance(item, dict):
            return False
        if "returncode" in item:
            try:
                return int(item["returncode"]) == 0
            except (TypeError, ValueError):
                return False
        return str(item.get("status") or "").strip().lower() == "pass"

    def _evidence_returncode(self, evidence: Evidence) -> Optional[int]:
        value = evidence.metadata.get("returncode")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _default_review_for_task(self, task_id: str) -> Optional[Review]:
        """Return the unambiguous review row to act on, or None.

        Refuses to pick when the task has more than one pending review
        (mac-d9c) — that's an ambiguous state and in an autonomous
        swarm there's no operator to break the tie. The caller logs
        ``workflow.default_review.ambiguous`` and leaves the task
        alone for explicit resolution.
        """
        reviews = self.list_reviews(task_id)
        if not reviews:
            return None
        pending = [review for review in reviews if review.status == ReviewStatus.PENDING.value]
        if len(pending) > 1:
            return None
        if pending:
            return pending[0]
        approved = [review for review in reviews if review.status == ReviewStatus.APPROVED.value]
        if approved:
            return approved[-1]
        return None

    def _find_review_verdict_evidence(
        self,
        task_id: str,
        reviewer_agent_id: str,
        *,
        executor_evidence_id: str,
    ) -> Tuple[Optional[Evidence], List[str]]:
        """Locate the reviewer's signed verdict evidence row, or return
        ``(None, problems)`` if it doesn't exist yet (mac-jqb v1).

        The verdict is a separate Evidence row authored by the reviewer
        (not the executor) with a signed verification manifest of type
        ``review_verdict`` that names the executor's evidence_id.
        Without this row the workflow blocks — it will no longer
        auto-approve in the same process that selected the reviewer.

        Shape required for a valid verdict:
            evidence.metadata.returncode == 0
            evidence.metadata.verification:
                schema = mac.worker_evidence.v1
                status = complete
                evidence_type = review_verdict
                verdict in {approved, rejected}
                reviewed_evidence_id == executor_evidence_id
                signed_by = <reviewer_agent_id>
                signature = <HMAC of manifest under reviewer's key>
        """
        problems: List[str] = []
        for evidence in reversed(self.list_evidence(task_id)):
            if evidence.created_by != reviewer_agent_id:
                continue
            if self._evidence_returncode(evidence) != 0:
                problems.append("verdict evidence %s has nonzero returncode" % evidence.id)
                continue
            manifest = evidence.metadata.get("verification")
            if not isinstance(manifest, dict):
                problems.append("verdict evidence %s missing verification manifest" % evidence.id)
                continue
            if str(manifest.get("evidence_type") or "").strip().lower() != "review_verdict":
                continue  # not a verdict evidence row, skip silently
            if str(manifest.get("schema") or "").strip() != VERIFICATION_SCHEMA:
                problems.append("verdict %s schema mismatch" % evidence.id)
                continue
            if str(manifest.get("status") or "").strip().lower() != "complete":
                problems.append("verdict %s status not complete" % evidence.id)
                continue
            reviewed = str(manifest.get("reviewed_evidence_id") or "").strip()
            if reviewed != executor_evidence_id:
                problems.append(
                    "verdict %s references wrong executor evidence: %s != %s"
                    % (evidence.id, reviewed, executor_evidence_id)
                )
                continue
            signed_by = str(manifest.get("signed_by") or "").strip()
            signature = str(manifest.get("signature") or "").strip()
            if signed_by != reviewer_agent_id:
                problems.append("verdict %s signed_by != reviewer" % evidence.id)
                continue
            key = self._agent_attestation_key(signed_by)
            if key is None:
                problems.append("verdict %s signer has no attestation key" % evidence.id)
                continue
            if not verify_verification_manifest_signature(key, manifest, signature):
                problems.append("verdict %s signature does not verify" % evidence.id)
                continue
            return evidence, []
        return None, problems

    def _verdict_value(self, evidence: Evidence) -> str:
        manifest = evidence.metadata.get("verification") or {}
        verdict = str(manifest.get("verdict") or "").strip().lower()
        return verdict if verdict in {"approved", "rejected"} else "approved"

    def _select_default_reviewer(self, task: Task) -> Optional[Agent]:
        """Pick a default reviewer for ``task``.

        Trust boundaries enforced here (autonomous-review context where
        there is no human in the loop):

        * Tenancy (mac-dyk): the reviewer's persona tenant_id must
          match the task's tenant. Without a human to catch a misroute,
          the tenancy boundary IS the safety boundary.
        * Capability (mac-s1a): ``review`` capability is *required*,
          not preferred. An agent without it cannot be drafted.
        * Persona separation / anti-collusion (mac-v2i): the reviewer's
          persona slug must differ from the executor's persona slug.
          Two code-reviewer-souled agents cannot approve each other's
          work — the second-eyes role only matters if it's a different
          eye.
        * Not the executor (existing): agent_has_owned_task continues
          to exclude prior owners.
        """
        task_tenant = self._task_tenant_id(task)
        executor_persona_slug = self._task_executor_persona_slug(task)

        candidates: List[Agent] = []
        for agent in self.list_agents():
            if agent.health_status != HealthStatus.HEALTHY.value:
                continue
            if agent.status == AgentStatus.OFFLINE.value:
                continue
            if self.reviews.agent_has_owned_task(task.id, agent.id):
                continue
            if "review" not in set(agent.capabilities):
                continue
            agent_tenant, agent_persona_slug = self._agent_tenant_and_persona(agent)
            # Tenant gate: if the task has a tenant, the agent's soul
            # must be in the same one. Agents without a soul are
            # ineligible to act as reviewers in tenant-scoped flows.
            if task_tenant is not None:
                if agent_tenant is None or agent_tenant != task_tenant:
                    continue
            # Anti-collusion: peers from the same persona can't endorse
            # each other.
            if (
                executor_persona_slug is not None
                and agent_persona_slug is not None
                and agent_persona_slug == executor_persona_slug
            ):
                continue
            candidates.append(agent)
        if not candidates:
            return None
        candidates.sort(
            key=lambda agent: (
                0 if agent.status == AgentStatus.IDLE.value else 1,
                agent.name,
                agent.id,
            )
        )
        return candidates[0]

    def _agent_tenant_and_persona(self, agent: Agent) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(tenant_id, persona_slug)`` for an agent, both
        optional. Used by the reviewer-selection guards (tenancy +
        anti-collusion). Agents without a hermes_instance_id have
        neither and are treated as ineligible by the reviewer picker
        when the task is tenant-scoped."""
        if not agent.hermes_instance_id:
            return None, None
        try:
            instance = self.identity.get_hermes_instance(agent.hermes_instance_id)
        except NotFoundError:
            return None, None
        if not instance.persona_id:
            return instance.tenant_id, None
        try:
            persona = self.identity.get_persona(instance.persona_id)
        except NotFoundError:
            return instance.tenant_id, None
        slug = persona.name.strip().lower().replace(" ", "-").replace("_", "-") or None
        return instance.tenant_id, slug

    def _task_executor_persona_slug(self, task: Task) -> Optional[str]:
        """Find the persona slug of whichever agent owned the task last
        (the executor). Used by anti-collusion. Returns None when the
        task has no recorded owner — in that case no executor-side
        persona constraint applies."""
        executor_agent_id: Optional[str] = task.owner_agent_id
        if executor_agent_id is None:
            # Look for the last lease against this task — it identifies
            # the executor even after submit-for-review releases owner.
            row = self.store.query_one(
                """
                SELECT agent_id FROM leases
                WHERE task_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (task.id,),
            )
            if row is not None:
                executor_agent_id = row["agent_id"]
        if executor_agent_id is None:
            return None
        try:
            executor = self.get_agent(executor_agent_id)
        except NotFoundError:
            return None
        _, slug = self._agent_tenant_and_persona(executor)
        return slug

    def _default_publication_target(self, task: Task) -> Optional[str]:
        """Resolve the publication target from task metadata or return None.

        Returns ``None`` when no operator-set target is available
        (mac-w29). Previously this synthesized ``mac://tasks/{id}`` which
        is filler — no resolver exists for that URI. The auto-review
        workflow now treats ``None`` as "no publication destination
        configured; leave the task in REVIEWING and emit a waiting
        observability event."
        """
        metadata = task.metadata
        for key in ("publication_target", "publish_target"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        publication = metadata.get("publication")
        if isinstance(publication, dict):
            target = publication.get("target")
            if isinstance(target, str) and target.strip():
                return target.strip()
        acc_metadata = metadata.get("acc_metadata")
        if isinstance(acc_metadata, dict):
            beads_id = acc_metadata.get("beads_id")
            if isinstance(beads_id, str) and beads_id.strip():
                return "beads://%s" % beads_id.strip()
        return None

    def _record_default_review_observation(
        self,
        task_id: str,
        name: str,
        level: str,
        detail: JsonDict,
        actor: str,
    ) -> None:
        self.observability.record_log(
            name,
            level=level,
            layer="control_plane",
            source="default-review-workflow",
            subject_type="task",
            subject_id=task_id,
            detail={"actor": actor, **detail},
        )

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
        if isinstance(required, dict):
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
        # Structured hardware constraints on the task (set by the workflow
        # runtime when spawning a role-bound node task). Falls through the
        # shared matcher so the role-required-hardware vocabulary stays in
        # one place.
        hw_required = task.metadata.get("hardware") if isinstance(task.metadata, dict) else None
        if isinstance(hw_required, dict) and hw_required:
            from mac.roles_service import machine_hardware_satisfies

            ok, _reasons = machine_hardware_satisfies(hw_required, machine.hardware)
            if not ok:
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
