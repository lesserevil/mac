from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mac.hermes_startup import build_hermes_startup_report
from mac.models import AuthorizationError, MACError, NotFoundError, ValidationError
from mac.services import ControlPlane
from mac.store import SQLiteStore, default_db_path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenPrincipal:
    """Authenticated bearer principal.

    ``scopes`` is the set of scope strings the token may use; ``"admin"``
    implicitly grants every scope. ``tenant_id`` is the tenant binding; ``None``
    means cross-tenant (admin-like) and any other value means the token may
    only write resources scoped to that tenant. Reads currently ignore the
    tenant binding — that surface returns full fleet state by design today.
    """

    scopes: frozenset = field(default_factory=frozenset)
    tenant_id: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes

    def has_scope(self, scope: str) -> bool:
        return self.is_admin or scope in self.scopes

    def assert_tenant(self, target_tenant_id: Optional[str]) -> None:
        if self.is_admin or self.tenant_id is None:
            return
        if target_tenant_id is None or target_tenant_id != self.tenant_id:
            raise AuthorizationError(
                "token is bound to a tenant and cannot write to a different tenant"
            )

    def require_global_fleet(self) -> None:
        """Refuse the call for tenant-bound, non-admin tokens.

        Machines, agents, runtimes, environments, and rollouts are part of the
        shared fleet today. A tenant-bound token has no business reaching them
        until we extend the schema to be tenant-aware.
        """
        if self.is_admin or self.tenant_id is None:
            return
        raise AuthorizationError(
            "token is bound to a tenant and cannot operate on global fleet resources"
        )


AuthTokenMapping = Mapping[str, Union[List[str], Dict[str, Any], TokenPrincipal]]


def _coerce_principal(value: Union[List[str], Dict[str, Any], TokenPrincipal]) -> TokenPrincipal:
    if isinstance(value, TokenPrincipal):
        return value
    if isinstance(value, dict):
        scopes = frozenset(str(s) for s in value.get("scopes", []))
        tenant = value.get("tenant_id")
        return TokenPrincipal(scopes=scopes, tenant_id=tenant)
    return TokenPrincipal(scopes=frozenset(str(s) for s in value))


def _normalize_auth_tokens(
    raw: Optional[AuthTokenMapping],
) -> Dict[str, TokenPrincipal]:
    if not raw:
        return {}
    return {str(token): _coerce_principal(value) for token, value in raw.items()}


def _resolve_principal(
    token: str, tokens: Mapping[str, TokenPrincipal]
) -> Optional[TokenPrincipal]:
    """Constant-time lookup over the registered tokens.

    Iterates every registered token so timing does not leak which prefix
    matched; ``hmac.compare_digest`` short-circuits in constant time within
    each pair.
    """
    candidate_bytes = token.encode("utf-8")
    matched: Optional[TokenPrincipal] = None
    for registered, principal in tokens.items():
        if hmac.compare_digest(candidate_bytes, registered.encode("utf-8")):
            matched = principal
    return matched


def _get_principal(request: Request) -> TokenPrincipal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        # No auth tokens configured — treat as admin to keep dev mode working.
        return TokenPrincipal(scopes=frozenset({"admin"}))
    return principal


def _data(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS = 60.0
AGENTBUS_MIN_EVENT_POLL_SECONDS = 0.25
AGENTBUS_MAX_EVENT_POLL_SECONDS = 5.0


def _agentbus_clamp_timeout(value: float) -> float:
    return min(AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS, max(0.0, float(value)))


def _agentbus_clamp_poll_interval(value: float) -> float:
    return min(
        AGENTBUS_MAX_EVENT_POLL_SECONDS,
        max(AGENTBUS_MIN_EVENT_POLL_SECONDS, float(value)),
    )


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    project: Optional[str] = None
    priority: int = 0
    required_capabilities: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = 3
    actor: str = "human"


class TenantRegister(BaseModel):
    name: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None


class UserRegister(BaseModel):
    tenant_id: str
    handle: str
    display_name: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None


class PersonaRegister(BaseModel):
    tenant_id: str
    name: str
    soul_ref: str
    memory_scope: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    persona_id: Optional[str] = None


class HermesInstanceRegister(BaseModel):
    tenant_id: str
    name: str
    persona_id: Optional[str] = None
    home_ref: str = ""
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    instance_id: Optional[str] = None


class PlatformBindingRegister(BaseModel):
    tenant_id: str
    hermes_instance_id: str
    platform: str
    external_id: str
    display_name: str = ""
    scopes: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    binding_id: Optional[str] = None


class InteractionTaskCreate(TaskCreate):
    user_id: Optional[str] = None
    platform_binding_id: Optional[str] = None
    conversation_ref: Optional[str] = None
    actor: str = "hermes"


class TransitionRequest(BaseModel):
    target_state: str
    actor: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class EvidenceCreate(BaseModel):
    kind: str
    uri: str
    summary: str
    created_by: str
    checksum: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MachineRegister(BaseModel):
    hostname: str
    labels: Dict[str, Any] = Field(default_factory=dict)
    resources: Dict[str, Any] = Field(default_factory=dict)
    trusted: bool = True
    machine_id: Optional[str] = None


class AgentRegister(BaseModel):
    machine_id: str
    name: str
    capabilities: List[str] = Field(default_factory=list)
    resources: Dict[str, Any] = Field(default_factory=dict)
    agent_id: Optional[str] = None


class HeartbeatRequest(BaseModel):
    status: Optional[str] = None
    health_status: Optional[str] = None
    resources: Optional[Dict[str, Any]] = None
    running_digest: Optional[str] = None


class DispatchRequest(BaseModel):
    lease_seconds: int = 900
    limit: int = 100
    stale_after_seconds: Optional[int] = None


class AgentClaimNextRequest(BaseModel):
    lease_seconds: int = 900
    allowed_projects: List[str] = Field(default_factory=list)
    required_metadata: Dict[str, Any] = Field(default_factory=dict)
    require_canary: bool = False
    dry_run: bool = False


class MessageCreate(BaseModel):
    sender_agent_id: str
    recipient_agent_id: Optional[str] = None
    task_id: Optional[str] = None
    message_type: str
    payload: Dict[str, Any]


class AgentBusOpen(BaseModel):
    sender_agent_id: str
    recipient_agent_id: str
    task_id: Optional[str] = None
    topic: str = "content"
    content_type: str = "application/json"
    headers: Dict[str, Any] = Field(default_factory=dict)
    stream_id: Optional[str] = None


class AgentBusAppend(BaseModel):
    sender_agent_id: str
    content_type: Optional[str] = None
    payload: Any = None
    payload_encoding: str = "json"
    final: bool = False


class AgentBusPublish(BaseModel):
    sender_agent_id: str
    recipient_agent_id: str
    task_id: Optional[str] = None
    topic: str = "content"
    content_type: str = "application/json"
    headers: Dict[str, Any] = Field(default_factory=dict)
    payload: Any = None
    payload_encoding: str = "json"


class ObservabilityMetricCreate(BaseModel):
    name: str
    value: float
    unit: str = ""
    layer: str = "external"
    source: str = "agent"
    level: str = "info"
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ObservabilityLogCreate(BaseModel):
    name: str
    level: str = "info"
    layer: str = "external"
    source: str = "agent"
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    reviewer_agent_id: str
    actor: str = "dispatcher"


class ReviewDecision(BaseModel):
    status: str
    reviewer_agent_id: str
    reason: Optional[str] = None
    evidence_id: Optional[str] = None


class PublicationCreate(BaseModel):
    task_id: str
    target: str
    created_by: str
    evidence_id: Optional[str] = None


class SecretCreate(BaseModel):
    name: str
    value: str
    scopes: Dict[str, Any]
    created_by: str


class SecretAccessRequest(BaseModel):
    accessor_agent_id: str
    purpose: str
    ttl_seconds: int = 300


class SecretRevealRequest(BaseModel):
    audit_id: str
    accessor_agent_id: str


class ArtifactRegister(BaseModel):
    kind: str
    digest: str
    uri: str
    created_by: str
    sbom_uri: Optional[str] = None
    signers: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MoodSet(BaseModel):
    mode: str
    set_by: Optional[str] = None
    reason: Optional[str] = None
    ttl_seconds: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MoodClear(BaseModel):
    cleared_by: Optional[str] = None
    reason: Optional[str] = None


class NapConfigure(BaseModel):
    offset_minutes: Optional[int] = None
    window_minutes: int = 15
    enabled: bool = True
    actor: Optional[str] = None


class NapBegin(BaseModel):
    actor: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class NapComplete(BaseModel):
    summary_evidence_id: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    actor: Optional[str] = None


class NapFail(BaseModel):
    reason: str
    actor: Optional[str] = None


class ConversationThreadTrack(BaseModel):
    platform_binding_id: str
    external_thread_id: str
    summary: str = ""
    latest_task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class VectorRefRecord(BaseModel):
    memory_id: str
    vector_db: str
    collection: str
    point_id: str
    embedding_model: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class EnvironmentRegister(BaseModel):
    name: str
    tenant_id: Optional[str] = None
    channel: str = "fleet"
    promotes_from: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class DeploymentCreate(BaseModel):
    artifact_id: str
    actor: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RuntimeCreate(BaseModel):
    name: str
    manifest: Dict[str, Any]
    created_by: str


class RuntimeRunCreate(BaseModel):
    task_id: str
    agent_id: str
    environment_id: str


class RuntimeRunComplete(BaseModel):
    evidence_id: str
    status: str = "completed"


class ProjectImport(BaseModel):
    source: str
    external_id: str
    title: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    required_capabilities: List[str] = Field(default_factory=list)
    actor: str = "bridge"


class MemoryCreate(BaseModel):
    task_id: Optional[str] = None
    subject_type: str
    subject_id: Optional[str] = None
    record_type: str
    content: str
    evidence_id: Optional[str] = None
    created_by: str


class RolloutCreate(BaseModel):
    version: str
    strategy: str
    target_percent: int
    created_by: str
    tenant_id: Optional[str] = None
    channel: str = "fleet"
    runtime_environment_id: Optional[str] = None
    artifact_uri: Optional[str] = None
    artifact_hash: Optional[str] = None
    health_policy: Dict[str, Any] = Field(default_factory=dict)
    required_eval_set_id: Optional[str] = None


class EvalSetCreate(BaseModel):
    name: str
    scoring: str = "higher_is_better"
    description: str = ""
    baseline_score: Optional[float] = None
    regression_threshold: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "human"


class EvalSetBaselineUpdate(BaseModel):
    baseline_score: float
    actor: str = "human"


class EvalRunRecord(BaseModel):
    eval_set_id: str
    target_kind: str
    target_id: str
    score: float
    detail: Dict[str, Any] = Field(default_factory=dict)
    evidence_id: Optional[str] = None
    created_by: str = "human"


class RolloutAdvance(BaseModel):
    action: str
    actor: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class RolloutRescue(BaseModel):
    actor: str
    reason: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class RolloutArtifactVerify(BaseModel):
    artifact_uri: str
    artifact_hash: str
    actor: str


class RolloutHealthReport(BaseModel):
    actor: str
    checks: Dict[str, Any]


def _load_auth_tokens_from_env() -> Dict[str, TokenPrincipal]:
    raw = os.environ.get("MAC_API_TOKENS")
    if raw:
        loaded = json.loads(raw)
        return _normalize_auth_tokens(loaded)
    single = os.environ.get("MAC_API_TOKEN")
    if single is None:
        return {}
    single = single.strip()
    if not single:
        # Refuse silent-fail: an empty token would disable auth without intent.
        raise ValueError(
            "MAC_API_TOKEN is set but empty; unset it to leave the API open, or provide a non-empty token"
        )
    return {single: TokenPrincipal(scopes=frozenset({"admin"}))}


def _required_scope(method: str, path: str) -> Optional[str]:
    if path == "/health":
        return None
    if path == "/ui" or path.startswith("/ui/"):
        return None
    if method == "GET":
        return "read"
    if path.startswith("/agents/") and (
        path.endswith("/heartbeat") or path.endswith("/messages/deliver")
    ):
        return "agent"
    if path.startswith("/agentbus"):
        return "agent"
    if path.startswith("/observability"):
        return "agent"
    if path.startswith("/dispatch"):
        return "dispatch"
    if path.startswith("/secrets") or path.startswith("/secret-audits"):
        return "secret"
    if (
        path.startswith("/runtimes")
        or path.startswith("/environments")
        or path.startswith("/rollouts")
    ):
        return "deploy"
    return "write"


def _authorize_request(
    method: str,
    path: str,
    authorization: Optional[str],
    auth_tokens: Mapping[str, TokenPrincipal],
) -> Optional[TokenPrincipal]:
    required = _required_scope(method, path)
    if required is None or not auth_tokens:
        return None
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthorizationError("missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    principal = _resolve_principal(token, auth_tokens)
    if principal is None:
        raise AuthorizationError("unknown bearer token")
    if not principal.has_scope(required):
        raise AuthorizationError("token lacks required scope: %s" % required)
    return principal


def _should_record_http_observation(path: str) -> bool:
    return not (
        path == "/health"
        or path.startswith("/ui/assets")
        or path.startswith("/observability")
    )


def _resolve_record_http_observations(flag: Optional[bool]) -> bool:
    if flag is not None:
        return flag
    raw = os.environ.get("MAC_RECORD_HTTP_OBSERVATIONS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


MAX_REGISTRATION_PAYLOAD_BYTES = 64 * 1024


def _ensure_payload_bounded(value: Any, field: str) -> None:
    """Cap registration-style metadata/labels/resources dicts.

    The control plane stores these as JSON blobs in SQLite forever, so an
    unbounded dict from a single client becomes permanent table bloat. 64 KB
    after JSON encoding is well above any legitimate label/metadata payload
    and well below the body-size limit that protects the HTTP layer.
    """
    if value is None:
        return
    try:
        encoded = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be JSON serializable" % field) from exc
    if len(encoded.encode("utf-8")) > MAX_REGISTRATION_PAYLOAD_BYTES:
        raise ValidationError(
            "%s exceeds %d-byte limit" % (field, MAX_REGISTRATION_PAYLOAD_BYTES)
        )




TERMINAL_DASHBOARD_STATES = {"completed", "failed", "cancelled"}


def _task_origin(task: Dict[str, Any]) -> Dict[str, Any]:
    metadata = task.get("metadata") or {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else None
    return origin if isinstance(origin, dict) else {}


def _state_counts(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _dashboard_task(cp: ControlPlane, task_id: str) -> Dict[str, Any]:
    detail = cp.task_detail(task_id)
    summary = cp.task_summary(task_id)
    detail["summary"] = summary
    detail["publications"] = [
        publication.to_dict() for publication in cp.list_publications(task_id)
    ]
    return detail


def _dashboard_agent_base(
    cp: ControlPlane,
    agent: Any,
    tasks: List[Any],
    machines_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    machine = machines_by_id.get(agent.machine_id)
    active_tasks = [
        task.to_dict()
        for task in tasks
        if task.owner_agent_id == agent.id and task.state not in TERMINAL_DASHBOARD_STATES
    ]
    reasons: List[str] = []
    if machine is None:
        reasons.append("missing machine")
    elif not machine.trusted:
        reasons.append("untrusted machine")
    if agent.status not in {"idle", "busy"}:
        reasons.append(agent.status)
    if agent.health_status != "healthy":
        reasons.append(agent.health_status)
    capacity = cp._agent_capacity(agent)
    active_lease_count = cp._agent_active_lease_count(agent.id)
    if active_lease_count >= capacity:
        reasons.append("at capacity")
    return {
        "agent": agent.to_dict(),
        "machine": machine.to_dict() if machine is not None else None,
        "active_tasks": active_tasks,
        "capacity": capacity,
        "active_lease_count": active_lease_count,
        "availability": {
            "eligible": not reasons,
            "reasons": reasons,
        },
    }


def _dashboard_dispatch_reasons(
    cp: ControlPlane,
    agent: Any,
    task: Any,
    machine: Optional[Any],
) -> List[str]:
    reasons: List[str] = []
    if agent.status not in {"idle", "busy"}:
        reasons.append("agent status is %s" % agent.status)
    if agent.health_status != "healthy":
        reasons.append("agent health is %s" % agent.health_status)
    if machine is None:
        reasons.append("agent machine is missing")
    elif not machine.trusted:
        reasons.append("machine is not trusted")
    if cp._agent_active_lease_count(agent.id) >= cp._agent_capacity(agent):
        reasons.append("agent is at capacity")
    if machine is not None and not cp._machine_allows_tenant(machine, cp._task_tenant_id(task)):
        reasons.append("machine tenant policy blocks task")
    if machine is not None and not cp._agent_resources_satisfy(agent, machine, task):
        reasons.append("resources do not satisfy task")
    missing = sorted(set(task.required_capabilities) - set(agent.capabilities))
    if missing:
        reasons.append("missing capabilities: %s" % ", ".join(missing))
    return reasons


def _dashboard_dispatch_explain(
    cp: ControlPlane,
    tasks: Optional[List[Any]] = None,
    agents: Optional[List[Any]] = None,
    machines_by_id: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tasks = tasks if tasks is not None else cp.list_tasks()
    agents = agents if agents is not None else cp.list_agents()
    machines_by_id = machines_by_id if machines_by_id is not None else {
        machine.id: machine for machine in cp.list_machines()
    }
    open_tasks = [task for task in tasks if task.state == "open"]
    explanations = []
    for task in open_tasks:
        candidates = []
        for agent in agents:
            machine = machines_by_id.get(agent.machine_id)
            eligible = cp._agent_available_for(agent, task)
            reasons = [] if eligible else _dashboard_dispatch_reasons(cp, agent, task, machine)
            if not eligible and not reasons:
                reasons.append("dispatch policy rejected pair")
            candidates.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "eligible": eligible,
                    "reasons": reasons,
                }
            )
        explanations.append(
            {
                "task": task.to_dict(),
                "tenant_id": cp._task_tenant_id(task),
                "candidates": candidates,
                "eligible_agent_count": sum(1 for candidate in candidates if candidate["eligible"]),
            }
        )
    return {"open_task_count": len(open_tasks), "tasks": explanations}


def _dashboard_hermes_activity(
    cp: ControlPlane,
    instance_id: str,
    tasks: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    context = cp.hermes_context(instance_id)
    tasks = tasks if tasks is not None else cp.list_tasks()
    interaction_tasks = [
        task.to_dict()
        for task in tasks
        if _task_origin(task.to_dict()).get("hermes_instance_id") == instance_id
    ]
    return {"context": context, "interaction_tasks": interaction_tasks}


def _dashboard_rollout_status(cp: ControlPlane, rollout_id: str) -> Dict[str, Any]:
    rollout = cp.get_rollout(rollout_id)
    runtime = (
        cp.get_runtime(rollout.runtime_environment_id).to_dict()
        if rollout.runtime_environment_id
        else None
    )
    latest_eval = None
    if rollout.required_eval_set_id is not None:
        latest = cp.latest_eval_run(
            rollout.required_eval_set_id,
            "rollout_version",
            rollout.version,
        )
        latest_eval = latest.to_dict() if latest is not None else None
    return {
        "rollout": rollout.to_dict(),
        "runtime": runtime,
        "events": cp.list_rollout_events(rollout_id),
        "latest_eval_run": latest_eval,
    }


def _dashboard_state(
    cp: ControlPlane,
    hermes_startup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tenants = [tenant.to_dict() for tenant in cp.list_tenants()]
    users = [user.to_dict() for user in cp.list_users()]
    personas = [persona.to_dict() for persona in cp.list_personas()]
    hermes_instances = [instance.to_dict() for instance in cp.list_hermes_instances()]
    bindings = [binding.to_dict() for binding in cp.list_platform_bindings()]
    machines = cp.list_machines()
    machines_by_id = {machine.id: machine for machine in machines}
    agents = cp.list_agents()
    tasks = cp.list_tasks()
    task_dicts = [task.to_dict() for task in tasks]
    dead_letters = [task.to_dict() for task in cp.list_dead_letters()]
    rollouts = cp.list_rollouts()
    secrets = [secret.to_dict() for secret in cp.list_secrets()]
    secret_audits = [audit.to_dict() for audit in cp.list_secret_audits()]
    runtime_runs = [run.to_dict() for run in cp.list_runtime_runs()]
    task_details = [_dashboard_task(cp, task.id) for task in tasks]
    rollout_statuses = [_dashboard_rollout_status(cp, rollout.id) for rollout in rollouts]
    return {
        "overview": {
            "counts": {
                "tenants": len(tenants),
                "users": len(users),
                "personas": len(personas),
                "hermes_instances": len(hermes_instances),
                "platform_bindings": len(bindings),
                "machines": len(machines),
                "trusted_machines": sum(1 for machine in machines if machine.trusted),
                "agents": len(agents),
                "healthy_agents": sum(1 for agent in agents if agent.health_status == "healthy"),
                "busy_agents": sum(1 for agent in agents if agent.status == "busy"),
                "active_tasks": sum(
                    1 for task in tasks if task.state not in TERMINAL_DASHBOARD_STATES
                ),
                "dead_letters": len(dead_letters),
                "rollouts": len(rollouts),
                "secrets": len(secrets),
                "secret_audits": len(secret_audits),
            },
            "task_states": _state_counts(task_dicts, "state"),
            "agent_statuses": _state_counts([agent.to_dict() for agent in agents], "status"),
        },
        "tenants": tenants,
        "users": users,
        "personas": personas,
        "hermes_instances": hermes_instances,
        "platform_bindings": bindings,
        "machines": [machine.to_dict() for machine in machines],
        "agents": [
            _dashboard_agent_base(cp, agent, tasks, machines_by_id)
            for agent in agents
        ],
        "tasks": task_details,
        "dead_letters": dead_letters,
        "dispatch": _dashboard_dispatch_explain(cp, tasks, agents, machines_by_id),
        "messages": [message.to_dict() for message in cp.list_messages()],
        "secrets": secrets,
        "secret_audits": secret_audits,
        "runtimes": [runtime.to_dict() for runtime in cp.list_runtimes()],
        "runtime_runs": runtime_runs,
        "rollouts": rollout_statuses,
        "eval_sets": [eval_set.to_dict() for eval_set in cp.list_eval_sets()],
        "eval_runs": [run.to_dict() for run in cp.list_eval_runs()],
        "observability": cp.observability_summary(),
        "hermes_startup": hermes_startup,
    }


def create_app(
    db_path: Optional[str] = None,
    control_plane: Optional[ControlPlane] = None,
    auth_tokens: Optional[AuthTokenMapping] = None,
    record_http_observations: Optional[bool] = None,
) -> FastAPI:
    cp = control_plane or ControlPlane(
        SQLiteStore(db_path or default_db_path())
    )
    tokens: Dict[str, TokenPrincipal] = (
        _normalize_auth_tokens(auth_tokens)
        if auth_tokens is not None
        else _load_auth_tokens_from_env()
    )
    record_http_obs = _resolve_record_http_observations(record_http_observations)
    app = FastAPI(title="MAC Control Plane", version="0.1.0")
    app.state.control_plane = cp
    app.state.auth_tokens = tokens
    app.state.hermes_startup = build_hermes_startup_report()
    if (
        os.environ.get("MAC_REQUIRE_HERMES_STARTUP_READY", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and not app.state.hermes_startup["ready"]
    ):
        raise ValidationError(
            "Hermes startup readiness failed: %s"
            % "; ".join(app.state.hermes_startup["warnings"])
        )
    ui_dir = Path(__file__).with_name("ui")
    if ui_dir.exists():
        app.mount("/ui/assets", StaticFiles(directory=str(ui_dir)), name="ui-assets")

    @app.exception_handler(MACError)
    async def handle_mac_error(request: Any, exc: MACError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        if isinstance(exc, AuthorizationError):
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def _emit_http_observation(
        request: Request, status_code: int, started: float, error_name: str
    ) -> None:
        if not record_http_obs or not _should_record_http_observation(request.url.path):
            return
        duration_ms = (time.monotonic() - started) * 1000.0
        level = "error" if status_code >= 500 else "warning" if status_code >= 400 else "info"
        detail = {
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3),
        }
        if error_name:
            detail["error"] = error_name
        try:
            cp.record_metric(
                "http.request.duration_ms",
                duration_ms,
                unit="ms",
                layer="api",
                source="http",
                level=level,
                detail=detail,
            )
        except (MACError, sqlite3.Error):
            _log.warning("failed to record http observation for %s", request.url.path, exc_info=True)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        status_code = 500
        error_name = ""
        try:
            principal = _authorize_request(
                request.method,
                request.url.path,
                request.headers.get("authorization"),
                tokens,
            )
            request.state.principal = principal
        except AuthorizationError as exc:
            status_code = 403
            error_name = exc.__class__.__name__
            _emit_http_observation(request, status_code, started, error_name)
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})
        try:
            response = await call_next(request)
            status_code = int(getattr(response, "status_code", 500))
            return response
        except Exception as exc:
            error_name = exc.__class__.__name__
            raise
        finally:
            _emit_http_observation(request, status_code, started, error_name)

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/startup/hermes")
    def hermes_startup() -> Dict[str, Any]:
        app.state.hermes_startup = build_hermes_startup_report()
        return app.state.hermes_startup

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    @app.get("/dashboard/state")
    def dashboard_state() -> Dict[str, Any]:
        app.state.hermes_startup = build_hermes_startup_report()
        return _dashboard_state(cp, app.state.hermes_startup)

    @app.get("/dashboard/agents/{agent_id}")
    def dashboard_agent(agent_id: str) -> Dict[str, Any]:
        agent = cp.get_agent(agent_id)
        tasks = cp.list_tasks()
        machines_by_id = {machine.id: machine for machine in cp.list_machines()}
        model = _dashboard_agent_base(cp, agent, tasks, machines_by_id)
        model["messages"] = [message.to_dict() for message in cp.list_messages(agent_id)]
        model["dispatch"] = [
            item
            for item in _dashboard_dispatch_explain(cp, tasks, [agent], machines_by_id)["tasks"]
            if item["eligible_agent_count"] or item["candidates"]
        ]
        return model

    @app.get("/dashboard/tasks/{task_id}/timeline")
    def dashboard_task_timeline(task_id: str) -> Dict[str, Any]:
        return _dashboard_task(cp, task_id)

    @app.get("/dashboard/dispatch/explain")
    def dashboard_dispatch_explain() -> Dict[str, Any]:
        return _dashboard_dispatch_explain(cp)

    @app.get("/dashboard/hermes/{instance_id}/activity")
    def dashboard_hermes_activity(instance_id: str) -> Dict[str, Any]:
        return _dashboard_hermes_activity(cp, instance_id)

    @app.get("/dashboard/rollouts/{rollout_id}/status")
    def dashboard_rollout_status(rollout_id: str) -> Dict[str, Any]:
        return _dashboard_rollout_status(cp, rollout_id)

    @app.post("/tenants")
    def register_tenant(
        body: TenantRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        # Creating tenants is a cross-tenant operation; only admin/unbound
        # principals can perform it.
        principal.require_global_fleet()
        return cp.register_tenant(**_data(body)).to_dict()

    @app.get("/tenants")
    def list_tenants() -> List[Dict[str, Any]]:
        return [tenant.to_dict() for tenant in cp.list_tenants()]

    @app.post("/users")
    def register_user(
        body: UserRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_user(**_data(body)).to_dict()

    @app.get("/users")
    def list_users(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [user.to_dict() for user in cp.list_users(tenant_id)]

    @app.post("/personas")
    def register_persona(
        body: PersonaRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_persona(**_data(body)).to_dict()

    @app.get("/personas")
    def list_personas(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [persona.to_dict() for persona in cp.list_personas(tenant_id)]

    @app.post("/hermes-instances")
    def register_hermes_instance(
        body: HermesInstanceRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_hermes_instance(**_data(body)).to_dict()

    @app.get("/hermes-instances")
    def list_hermes_instances(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [instance.to_dict() for instance in cp.list_hermes_instances(tenant_id)]

    @app.get("/hermes-instances/{instance_id}/context")
    def hermes_context(instance_id: str) -> Dict[str, Any]:
        return cp.hermes_context(instance_id)

    @app.post("/hermes-instances/{instance_id}/tasks")
    def create_interaction_task(
        instance_id: str,
        body: InteractionTaskCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        instance = cp.get_hermes_instance(instance_id)
        principal.assert_tenant(instance.tenant_id)
        data = _data(body)
        actor = data.pop("actor", "hermes")
        return cp.create_interaction_task(instance_id, actor=actor, **data).to_dict()

    @app.post("/platform-bindings")
    def register_platform_binding(
        body: PlatformBindingRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_platform_binding(**_data(body)).to_dict()

    @app.get("/platform-bindings")
    def list_platform_bindings(
        tenant_id: Optional[str] = Query(default=None),
        hermes_instance_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            binding.to_dict()
            for binding in cp.list_platform_bindings(
                tenant_id=tenant_id,
                hermes_instance_id=hermes_instance_id,
            )
        ]

    @app.post("/tasks")
    def create_task(
        body: TaskCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        _ensure_payload_bounded(body.metadata, "task.metadata")
        data = _data(body)
        actor = data.pop("actor", "human")
        metadata = dict(data.get("metadata") or {})
        origin = dict(metadata.get("origin") or {}) if isinstance(metadata.get("origin"), dict) else {}
        existing_tenant = origin.get("tenant_id") or metadata.get("tenant_id")
        if principal.tenant_id is not None and not principal.is_admin:
            if existing_tenant is not None and existing_tenant != principal.tenant_id:
                principal.assert_tenant(existing_tenant)
            # Stamp the principal's tenant onto the task so downstream filters
            # see it even when the caller forgot to set it explicitly.
            origin["tenant_id"] = principal.tenant_id
            metadata["origin"] = origin
            data["metadata"] = metadata
        return cp.create_task(actor=actor, **data).to_dict()

    @app.get("/tasks")
    def list_tasks(
        state: Optional[str] = Query(default=None),
        tenant_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in cp.list_tasks(state, tenant_id)]

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> Dict[str, Any]:
        return cp.task_detail(task_id)

    @app.get("/tasks/{task_id}/summary")
    def task_summary(task_id: str) -> Dict[str, Any]:
        return cp.task_summary(task_id)

    @app.post("/tasks/{task_id}/transition")
    def transition_task(task_id: str, body: TransitionRequest) -> Dict[str, Any]:
        return cp.transition_task(task_id, body.target_state, body.actor, body.detail).to_dict()

    @app.post("/tasks/{task_id}/claim")
    def claim_task(task_id: str, agent_id: str, lease_seconds: int = 900) -> Dict[str, Any]:
        task, lease = cp.claim_task(task_id, agent_id, lease_seconds)
        return {"task": task.to_dict(), "lease": lease.to_dict()}

    @app.post("/tasks/{task_id}/start")
    def start_task(task_id: str, agent_id: str) -> Dict[str, Any]:
        return cp.start_task(task_id, agent_id).to_dict()

    @app.post("/tasks/{task_id}/submit-for-review")
    def submit_for_review(task_id: str, agent_id: str) -> Dict[str, Any]:
        return cp.submit_for_review(task_id, agent_id).to_dict()

    @app.post("/tasks/{task_id}/evidence")
    def add_evidence(task_id: str, body: EvidenceCreate) -> Dict[str, Any]:
        return cp.add_evidence(task_id=task_id, **_data(body)).to_dict()

    @app.post("/machines")
    def register_machine(
        body: MachineRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.labels, "machine.labels")
        _ensure_payload_bounded(body.resources, "machine.resources")
        return cp.register_machine(**_data(body)).to_dict()

    @app.get("/machines")
    def list_machines() -> List[Dict[str, Any]]:
        return [machine.to_dict() for machine in cp.list_machines()]

    @app.post("/agents")
    def register_agent(
        body: AgentRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        _ensure_payload_bounded(body.resources, "agent.resources")
        return cp.register_agent(**_data(body)).to_dict()

    @app.get("/agents")
    def list_agents() -> List[Dict[str, Any]]:
        return [agent.to_dict() for agent in cp.list_agents()]

    @app.get("/fleet/build-distribution")
    def fleet_build_distribution() -> Dict[str, Any]:
        return cp.fleet_build_distribution()

    # Mood — agent-self-reported emotional state
    @app.put("/agents/{agent_id}/mood")
    @app.post("/agents/{agent_id}/mood")
    def set_mood(agent_id: str, body: MoodSet) -> Dict[str, Any]:
        return cp.set_mood(agent_id, **_data(body)).to_dict()

    @app.get("/agents/{agent_id}/mood")
    def get_mood(agent_id: str) -> Optional[Dict[str, Any]]:
        overlay = cp.get_current_mood(agent_id)
        return overlay.to_dict() if overlay is not None else None

    @app.delete("/agents/{agent_id}/mood")
    def clear_mood(agent_id: str, body: MoodClear) -> Optional[Dict[str, Any]]:
        cleared = cp.clear_mood(agent_id, **_data(body))
        return cleared.to_dict() if cleared is not None else None

    @app.get("/agents/{agent_id}/mood/history")
    def list_mood_history(agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [overlay.to_dict() for overlay in cp.list_mood_history(agent_id, limit=limit)]

    # Nap — daily memory-consolidation lifecycle
    @app.put("/agents/{agent_id}/nap-schedule")
    @app.post("/agents/{agent_id}/nap-schedule")
    def configure_nap(agent_id: str, body: NapConfigure) -> Dict[str, Any]:
        return cp.configure_nap(agent_id, **_data(body)).to_dict()

    @app.get("/agents/{agent_id}/nap-schedule")
    def get_nap_schedule(agent_id: str) -> Optional[Dict[str, Any]]:
        schedule = cp.get_nap_schedule(agent_id)
        return schedule.to_dict() if schedule is not None else None

    @app.get("/agents/{agent_id}/nap-schedule/next")
    def next_nap_window(agent_id: str) -> Optional[Dict[str, Any]]:
        return cp.next_nap_window(agent_id)

    @app.get("/nap-schedules")
    def list_nap_schedules() -> List[Dict[str, Any]]:
        return [schedule.to_dict() for schedule in cp.list_nap_schedules()]

    @app.post("/agents/{agent_id}/nap-runs")
    def begin_nap(agent_id: str, body: NapBegin) -> Dict[str, Any]:
        return cp.begin_nap(agent_id, **_data(body)).to_dict()

    @app.get("/nap-runs")
    def list_nap_runs(agent_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [run.to_dict() for run in cp.list_nap_runs(agent_id)]

    @app.get("/nap-runs/{run_id}")
    def get_nap_run(run_id: str) -> Dict[str, Any]:
        return cp.get_nap_run(run_id).to_dict()

    @app.post("/nap-runs/{run_id}/complete")
    def complete_nap(run_id: str, body: NapComplete) -> Dict[str, Any]:
        return cp.complete_nap(run_id, **_data(body)).to_dict()

    @app.post("/nap-runs/{run_id}/fail")
    def fail_nap(run_id: str, body: NapFail) -> Dict[str, Any]:
        return cp.fail_nap(run_id, **_data(body)).to_dict()

    @app.post("/agents/{agent_id}/heartbeat")
    def heartbeat_agent(agent_id: str, body: HeartbeatRequest) -> Dict[str, Any]:
        return cp.heartbeat_agent(agent_id, **_data(body)).to_dict()

    @app.post("/agents/{agent_id}/claim-next")
    def claim_next_for_agent(agent_id: str, body: AgentClaimNextRequest) -> Optional[Dict[str, Any]]:
        return cp.claim_next_for_agent(
            agent_id,
            lease_seconds=body.lease_seconds,
            allowed_projects=body.allowed_projects,
            required_metadata=body.required_metadata,
            require_canary=body.require_canary,
            dry_run=body.dry_run,
        )

    @app.post("/dispatch/assign")
    def dispatch_once(body: DispatchRequest) -> Optional[Dict[str, Any]]:
        return cp.dispatch_once(body.lease_seconds)

    @app.post("/dispatch/tick")
    def dispatch_tick(body: DispatchRequest) -> Dict[str, Any]:
        return cp.tick(body.lease_seconds, body.limit, body.stale_after_seconds)

    @app.get("/dispatch/dead-letters")
    def dead_letters(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in cp.list_dead_letters(tenant_id)]

    @app.get("/events")
    def list_events(
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        actor: Optional[str] = Query(default=None),
        event_type: Optional[str] = Query(default=None),
        event_type_prefix: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return cp.list_events(
            subject_type=subject_type,
            subject_id=subject_id,
            actor=actor,
            event_type=event_type,
            event_type_prefix=event_type_prefix,
            since=since,
            until=until,
            limit=limit,
        )

    @app.post("/observability/metrics")
    def record_observability_metric(body: ObservabilityMetricCreate) -> Dict[str, Any]:
        return cp.record_metric(**_data(body)).to_dict()

    @app.post("/observability/logs")
    def record_observability_log(body: ObservabilityLogCreate) -> Dict[str, Any]:
        return cp.record_log(**_data(body)).to_dict()

    @app.get("/observability/metrics")
    def list_observability_metrics(
        layer: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        after_sequence: Optional[int] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_observability(
                kind="metric",
                layer=layer,
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
                after_sequence=after_sequence,
                limit=limit,
            )
        ]

    @app.get("/observability/logs")
    def list_observability_logs(
        layer: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        after_sequence: Optional[int] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_observability(
                kind="log",
                layer=layer,
                level=level,
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
                after_sequence=after_sequence,
                limit=limit,
            )
        ]

    @app.get("/observability")
    def list_observability(
        kind: Optional[str] = Query(default=None),
        layer: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        after_sequence: Optional[int] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            event.to_dict()
            for event in cp.list_observability(
                kind=kind,
                layer=layer,
                level=level,
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
                after_sequence=after_sequence,
                limit=limit,
            )
        ]

    @app.get("/observability/summary")
    def observability_summary(limit: int = Query(default=80)) -> Dict[str, Any]:
        return cp.observability_summary(limit)

    @app.get("/observability/stream")
    async def observability_stream(
        after_sequence: int = Query(default=0),
        timeout_seconds: float = Query(default=30.0),
        poll_interval_seconds: float = Query(default=0.5),
        kind: Optional[str] = Query(default=None),
        layer: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        cp.list_observability(
            kind=kind,
            layer=layer,
            level=level,
            after_sequence=max(0, int(after_sequence)),
            limit=1,
        )

        async def iter_observations() -> Any:
            cursor = max(0, int(after_sequence))
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            while True:
                observations = cp.list_observability(
                    kind=kind,
                    layer=layer,
                    level=level,
                    after_sequence=cursor,
                    limit=100,
                )
                for observation in observations:
                    cursor = observation.sequence
                    yield json.dumps(observation.to_dict(), sort_keys=True) + "\n"
                if observations:
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0)
                    continue
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(iter_observations(), media_type="application/x-ndjson")

    @app.post("/messages")
    def send_message(body: MessageCreate) -> Dict[str, Any]:
        return cp.send_message(**_data(body)).to_dict()

    @app.get("/messages")
    def list_messages(agent_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [message.to_dict() for message in cp.list_messages(agent_id)]

    @app.post("/agents/{agent_id}/messages/deliver")
    def deliver_messages(agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [message.to_dict() for message in cp.deliver_messages(agent_id, limit)]

    @app.post("/agentbus/streams")
    def open_agentbus_stream(body: AgentBusOpen) -> Dict[str, Any]:
        return cp.open_agentbus_stream(**_data(body)).to_dict()

    @app.get("/agentbus/streams")
    def list_agentbus_streams(
        agent_id: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            stream.to_dict()
            for stream in cp.list_agentbus_streams(agent_id=agent_id, status=status, limit=limit)
        ]

    @app.post("/agentbus/streams/{stream_id}/chunks")
    def append_agentbus_chunk(stream_id: str, body: AgentBusAppend) -> Dict[str, Any]:
        return cp.append_agentbus_chunk(stream_id, **_data(body)).to_dict()

    @app.get("/agentbus/streams/{stream_id}/chunks")
    def read_agentbus_chunks(
        stream_id: str,
        agent_id: str,
        after_sequence: int = Query(default=0),
        limit: int = Query(default=100),
    ) -> List[Dict[str, Any]]:
        return [
            chunk.to_dict()
            for chunk in cp.read_agentbus_chunks(agent_id, stream_id, after_sequence, limit)
        ]

    @app.post("/agentbus/streams/{stream_id}/close")
    def close_agentbus_stream(
        stream_id: str,
        sender_agent_id: str,
        status: str = "closed",
    ) -> Dict[str, Any]:
        return cp.close_agentbus_stream(stream_id, sender_agent_id, status).to_dict()

    @app.post("/agentbus")
    def publish_agentbus_content(body: AgentBusPublish) -> Dict[str, Any]:
        return cp.publish_agentbus_content(**_data(body))

    @app.get("/agentbus/streams/{stream_id}/events")
    async def agentbus_stream_events(
        stream_id: str,
        agent_id: str,
        after_sequence: int = Query(default=0),
        timeout_seconds: float = Query(default=30.0),
        poll_interval_seconds: float = Query(default=0.25),
    ) -> StreamingResponse:
        # Authorize before we start streaming so denials surface as proper HTTP
        # errors rather than a half-open response.
        cp.assert_agentbus_authorized(agent_id, stream_id)

        async def iter_events() -> Any:
            cursor = max(0, int(after_sequence))
            deadline = time.monotonic() + _agentbus_clamp_timeout(timeout_seconds)
            poll_interval = _agentbus_clamp_poll_interval(poll_interval_seconds)
            while True:
                chunks = cp.read_agentbus_chunks(agent_id, stream_id, cursor, limit=100)
                for chunk in chunks:
                    cursor = chunk.sequence
                    yield json.dumps(chunk.to_dict(), sort_keys=True) + "\n"
                if chunks:
                    if time.monotonic() >= deadline:
                        break
                    # Yield control between batches so we don't starve the event
                    # loop while draining a backlog.
                    await asyncio.sleep(0)
                    continue
                if cp.get_agentbus_stream(stream_id).status != "open":
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(iter_events(), media_type="application/x-ndjson")

    @app.post("/tasks/{task_id}/reviews")
    def request_review(task_id: str, body: ReviewRequest) -> Dict[str, Any]:
        return cp.request_review(task_id, body.reviewer_agent_id, body.actor).to_dict()

    @app.post("/reviews/{review_id}/decision")
    def submit_review(review_id: str, body: ReviewDecision) -> Dict[str, Any]:
        return cp.submit_review(review_id, **_data(body)).to_dict()

    @app.post("/publications")
    def publish(body: PublicationCreate) -> Dict[str, Any]:
        return cp.publish_task(**_data(body)).to_dict()

    @app.post("/secrets")
    def create_secret(body: SecretCreate) -> Dict[str, Any]:
        return cp.create_secret(**_data(body)).to_dict()

    @app.get("/secrets")
    def list_secrets() -> List[Dict[str, Any]]:
        return [secret.to_dict() for secret in cp.list_secrets()]

    @app.post("/secrets/{secret_id}/access")
    def request_secret(secret_id: str, body: SecretAccessRequest) -> Dict[str, Any]:
        return cp.request_secret(
            secret_id,
            body.accessor_agent_id,
            body.purpose,
            body.ttl_seconds,
        ).to_dict()

    @app.post("/secrets/{secret_id}/reveal")
    def reveal_secret(secret_id: str, body: SecretRevealRequest) -> Dict[str, Any]:
        return {
            "secret_id": secret_id,
            "value": cp.reveal_secret(secret_id, body.audit_id, body.accessor_agent_id),
        }

    @app.get("/secret-audits")
    def list_secret_audits(secret_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [audit.to_dict() for audit in cp.list_secret_audits(secret_id)]

    @app.post("/artifacts")
    def register_artifact(body: ArtifactRegister) -> Dict[str, Any]:
        return cp.register_artifact(**_data(body)).to_dict()

    @app.get("/artifacts")
    def list_artifacts(kind: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [artifact.to_dict() for artifact in cp.list_artifacts(kind)]

    @app.get("/artifacts/{artifact_id_or_digest}")
    def get_artifact(artifact_id_or_digest: str) -> Dict[str, Any]:
        return cp.get_artifact(artifact_id_or_digest).to_dict()

    @app.post("/conversation-threads")
    def track_conversation(body: ConversationThreadTrack) -> Dict[str, Any]:
        return cp.track_conversation(**_data(body)).to_dict()

    @app.get("/conversation-threads")
    def list_conversation_threads(
        platform_binding_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [thread.to_dict() for thread in cp.list_conversation_threads(platform_binding_id)]

    @app.get("/conversation-threads/{thread_id}")
    def get_conversation_thread(thread_id: str) -> Dict[str, Any]:
        return cp.get_conversation_thread(thread_id).to_dict()

    @app.post("/vector-refs")
    def record_vector_ref(body: VectorRefRecord) -> Dict[str, Any]:
        return cp.record_vector_ref(**_data(body)).to_dict()

    @app.get("/vector-refs")
    def list_vector_refs(
        memory_id: Optional[str] = Query(default=None),
        vector_db: Optional[str] = Query(default=None),
        collection: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [
            ref.to_dict()
            for ref in cp.list_vector_refs(memory_id, vector_db, collection)
        ]

    @app.post("/environments")
    def register_environment(
        body: EnvironmentRegister,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.register_environment(**_data(body)).to_dict()

    @app.get("/environments")
    def list_environments(
        tenant_id: Optional[str] = Query(default=None),
        channel: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [env.to_dict() for env in cp.list_environments(tenant_id, channel)]

    @app.get("/environments/{env_id}")
    def get_environment(env_id: str) -> Dict[str, Any]:
        return cp.get_environment(env_id).to_dict()

    @app.post("/environments/{env_id}/deploy")
    def deploy_artifact(env_id: str, body: DeploymentCreate) -> Dict[str, Any]:
        return cp.deploy_artifact(env_id, body.artifact_id, body.actor, body.metadata).to_dict()

    @app.get("/environments/{env_id}/current")
    def current_deployment(env_id: str) -> Optional[Dict[str, Any]]:
        current = cp.current_deployment(env_id)
        return current.to_dict() if current is not None else None

    @app.get("/environments/{env_id}/deployments")
    def list_deployments(env_id: str) -> List[Dict[str, Any]]:
        return [d.to_dict() for d in cp.list_deployments(env_id)]

    @app.post("/runtimes")
    def create_runtime(
        body: RuntimeCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.require_global_fleet()
        return cp.create_runtime(**_data(body)).to_dict()

    @app.get("/runtimes")
    def list_runtimes() -> List[Dict[str, Any]]:
        return [runtime.to_dict() for runtime in cp.list_runtimes()]

    @app.post("/runtime-runs")
    def create_runtime_run(body: RuntimeRunCreate) -> Dict[str, Any]:
        return cp.create_runtime_run(**_data(body)).to_dict()

    @app.post("/runtime-runs/{run_id}/complete")
    def complete_runtime_run(run_id: str, body: RuntimeRunComplete) -> Dict[str, Any]:
        return cp.complete_runtime_run(run_id, body.evidence_id, body.status).to_dict()

    @app.post("/bridge/items")
    def import_project_item(body: ProjectImport) -> Dict[str, Any]:
        return cp.import_project_item(**_data(body)).to_dict()

    @app.get("/bridge/items")
    def list_project_items() -> List[Dict[str, Any]]:
        return [item.to_dict() for item in cp.list_project_items()]

    @app.post("/memory")
    def add_memory(body: MemoryCreate) -> Dict[str, Any]:
        data = _data(body)
        data.setdefault("evidence_id", None)
        return cp.add_memory(**data).to_dict()

    @app.get("/memory")
    def search_memory(
        task_id: Optional[str] = Query(default=None),
        subject_type: Optional[str] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [record.to_dict() for record in cp.search_memory(task_id, subject_type, subject_id)]

    @app.post("/eval-sets")
    def create_eval_set(body: EvalSetCreate) -> Dict[str, Any]:
        return cp.create_eval_set(**_data(body)).to_dict()

    @app.get("/eval-sets")
    def list_eval_sets() -> List[Dict[str, Any]]:
        return [eval_set.to_dict() for eval_set in cp.list_eval_sets()]

    @app.get("/eval-sets/{eval_set_id}")
    def get_eval_set(eval_set_id: str) -> Dict[str, Any]:
        return cp.get_eval_set(eval_set_id).to_dict()

    @app.post("/eval-sets/{eval_set_id}/baseline")
    def update_eval_set_baseline(eval_set_id: str, body: EvalSetBaselineUpdate) -> Dict[str, Any]:
        return cp.update_eval_set_baseline(eval_set_id, body.baseline_score, body.actor).to_dict()

    @app.get("/eval-sets/{eval_set_id}/events")
    def list_eval_set_events(eval_set_id: str) -> List[Dict[str, Any]]:
        return cp.list_eval_set_events(eval_set_id)

    @app.post("/eval-runs")
    def record_eval_run(body: EvalRunRecord) -> Dict[str, Any]:
        data = _data(body)
        eval_set_id = data.pop("eval_set_id")
        return cp.record_eval_run(eval_set_id, **data).to_dict()

    @app.get("/eval-runs")
    def list_eval_runs(
        eval_set_id: Optional[str] = Query(default=None),
        target_id: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [run.to_dict() for run in cp.list_eval_runs(eval_set_id, target_id)]

    @app.post("/rollouts")
    def create_rollout(
        body: RolloutCreate,
        principal: TokenPrincipal = Depends(_get_principal),
    ) -> Dict[str, Any]:
        principal.assert_tenant(body.tenant_id)
        return cp.create_rollout(**_data(body)).to_dict()

    @app.get("/rollouts")
    def list_rollouts(
        tenant_id: Optional[str] = Query(default=None),
        channel: Optional[str] = Query(default=None),
    ) -> List[Dict[str, Any]]:
        return [rollout.to_dict() for rollout in cp.list_rollouts(tenant_id, channel)]

    @app.post("/rollouts/{rollout_id}/advance")
    def advance_rollout(rollout_id: str, body: RolloutAdvance) -> Dict[str, Any]:
        return cp.advance_rollout(rollout_id, body.action, body.actor, body.detail).to_dict()

    @app.post("/rollouts/{rollout_id}/artifact")
    def verify_rollout_artifact(rollout_id: str, body: RolloutArtifactVerify) -> Dict[str, Any]:
        return cp.verify_rollout_artifact(
            rollout_id,
            body.artifact_uri,
            body.artifact_hash,
            body.actor,
        ).to_dict()

    @app.post("/rollouts/{rollout_id}/health")
    def evaluate_rollout_health(rollout_id: str, body: RolloutHealthReport) -> Dict[str, Any]:
        return cp.evaluate_rollout_health(rollout_id, body.checks, body.actor)

    @app.post("/rollouts/{rollout_id}/rescue")
    def rescue_rollout(rollout_id: str, body: RolloutRescue) -> Dict[str, Any]:
        rollout, task = cp.rescue_rollout(rollout_id, body.actor, body.reason, body.detail)
        return {"rollout": rollout.to_dict(), "task": task.to_dict()}

    return app


# Only build the default app when a secret key is present, so importing the
# module (e.g. from tests) does not require MAC_SECRET_KEY. Run uvicorn with
# `mac.api:create_app --factory` to be explicit, or set MAC_SECRET_KEY before
# `mac.api:app`.
if os.environ.get("MAC_SECRET_KEY"):
    app = create_app()
