from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional


JsonDict = Dict[str, Any]


class MACError(Exception):
    """Base exception for recoverable control-plane errors."""


class NotFoundError(MACError):
    """Raised when a requested durable object does not exist."""


class ValidationError(MACError):
    """Raised when user or agent input violates a contract."""


class TransitionError(MACError):
    """Raised when a state transition is not allowed."""


class AuthorizationError(MACError):
    """Raised when an agent lacks explicit authority."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def json_loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def coerce_list(value: Optional[Iterable[str]]) -> List[str]:
    return sorted({item for item in (value or []) if item})


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TaskState(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATES = {
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.CANCELLED.value,
}


TASK_TRANSITIONS = {
    TaskState.OPEN.value: {
        TaskState.BLOCKED.value,
        TaskState.CLAIMED.value,
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    },
    TaskState.BLOCKED.value: {
        TaskState.OPEN.value,
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    },
    TaskState.CLAIMED.value: {
        TaskState.OPEN.value,
        TaskState.RUNNING.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.RUNNING.value: {
        TaskState.NEEDS_REVIEW.value,
        TaskState.OPEN.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.NEEDS_REVIEW.value: {
        TaskState.REVIEWING.value,
        TaskState.RUNNING.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.REVIEWING.value: {
        TaskState.RUNNING.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    },
    TaskState.COMPLETED.value: set(),
    TaskState.FAILED.value: {TaskState.OPEN.value},
    TaskState.CANCELLED.value: {TaskState.OPEN.value},
}


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    DRAINING = "draining"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    RENEWED = "renewed"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class PublicationStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class RuntimeRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


EVIDENCE_KINDS = {"test", "review", "artifact", "publication", "log", "eval"}


class EvalScoringDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class EvalTargetKind(StrEnum):
    ROLLOUT_VERSION = "rollout_version"
    RUNTIME_ENVIRONMENT = "runtime_environment"
    AGENT_BUILD = "agent_build"


class MessageType(StrEnum):
    HELP_REQUEST = "help_request"
    EVIDENCE_REQUEST = "evidence_request"
    STATUS_UPDATE = "status_update"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    NUDGE = "nudge"
    DECISION_RECORD = "decision_record"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    REJECTED = "rejected"


class SecretAuditResult(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    ROTATED = "rotated"


class RolloutStrategy(StrEnum):
    CANARY = "canary"
    FULL = "full"
    RESCUE = "rescue"


class RolloutStatus(StrEnum):
    PLANNED = "planned"
    CANARYING = "canarying"
    PROMOTED = "promoted"
    PAUSED = "paused"
    RESCUING = "rescuing"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


ROLLOUT_ACTIONS = {
    "start_canary": {
        "from": {RolloutStatus.PLANNED.value},
        "to": RolloutStatus.CANARYING.value,
    },
    "promote": {
        "from": {RolloutStatus.PLANNED.value, RolloutStatus.CANARYING.value, RolloutStatus.PAUSED.value},
        "to": RolloutStatus.PROMOTED.value,
        "target_percent": 100,
    },
    "pause": {
        "from": {RolloutStatus.PLANNED.value, RolloutStatus.CANARYING.value},
        "to": RolloutStatus.PAUSED.value,
    },
    "resume": {
        "from": {RolloutStatus.PAUSED.value},
        "to": RolloutStatus.CANARYING.value,
    },
    "rollback": {
        "from": {
            RolloutStatus.CANARYING.value,
            RolloutStatus.PAUSED.value,
            RolloutStatus.PROMOTED.value,
            RolloutStatus.RESCUING.value,
        },
        "to": RolloutStatus.ROLLED_BACK.value,
        "target_percent": 0,
    },
}


class HermesInstanceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


@dataclass
class Tenant:
    id: str
    name: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class User:
    id: str
    tenant_id: str
    handle: str
    display_name: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Persona:
    id: str
    tenant_id: str
    name: str
    soul_ref: str
    memory_scope: str
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class HermesInstance:
    id: str
    tenant_id: str
    name: str
    persona_id: Optional[str]
    home_ref: str
    status: str
    metadata: JsonDict
    created_at: str
    updated_at: str
    last_seen_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class PlatformBinding:
    id: str
    tenant_id: str
    hermes_instance_id: str
    platform: str
    external_id: str
    display_name: str
    scopes: JsonDict
    metadata: JsonDict
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Task:
    id: str
    title: str
    description: str
    project: Optional[str]
    priority: int
    state: str
    required_capabilities: List[str]
    dependencies: List[str]
    metadata: JsonDict
    owner_agent_id: Optional[str]
    lease_id: Optional[str]
    leased_until: Optional[str]
    attempt_count: int
    max_attempts: int
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class HistoryEvent:
    id: str
    task_id: str
    event_type: str
    actor: str
    from_state: Optional[str]
    to_state: Optional[str]
    detail: JsonDict
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Evidence:
    id: str
    task_id: str
    kind: str
    uri: str
    summary: str
    checksum: Optional[str]
    metadata: JsonDict
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Lease:
    id: str
    task_id: str
    agent_id: str
    expires_at: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Machine:
    id: str
    hostname: str
    labels: JsonDict
    resources: JsonDict
    trusted: bool
    created_at: str
    updated_at: str
    last_seen_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Agent:
    id: str
    machine_id: str
    name: str
    capabilities: List[str]
    resources: JsonDict
    status: str
    health_status: str
    current_task_id: Optional[str]
    created_at: str
    updated_at: str
    last_seen_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class AgentMessage:
    id: str
    sender_agent_id: str
    recipient_agent_id: Optional[str]
    task_id: Optional[str]
    message_type: str
    payload: JsonDict
    status: str
    created_at: str
    delivered_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Review:
    id: str
    task_id: str
    reviewer_agent_id: str
    status: str
    reason: Optional[str]
    evidence_id: Optional[str]
    created_at: str
    completed_at: Optional[str]

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Publication:
    id: str
    task_id: str
    target: str
    status: str
    evidence_id: Optional[str]
    content_hash: Optional[str]
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class SecretRecord:
    id: str
    name: str
    scopes: JsonDict
    created_by: str
    created_at: str
    updated_at: str
    rotated_at: Optional[str]
    enabled: bool

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["value"] = "***REDACTED***"
        return data


@dataclass
class SecretAccess:
    id: str
    secret_id: str
    accessor_agent_id: str
    purpose: str
    result: str
    expires_at: Optional[str]
    revealed_at: Optional[str]
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class SecretHandle:
    secret_id: str
    audit_id: str
    handle: str
    granted: bool

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RuntimeEnvironment:
    id: str
    name: str
    manifest: JsonDict
    digest: str
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class RuntimeRun:
    id: str
    task_id: str
    agent_id: str
    environment_id: str
    status: str
    evidence_id: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ProjectItem:
    id: str
    source: str
    external_id: str
    title: str
    payload: JsonDict
    task_id: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class MemoryRecord:
    id: str
    task_id: Optional[str]
    subject_type: str
    subject_id: Optional[str]
    record_type: str
    content: str
    evidence_id: Optional[str]
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Rollout:
    id: str
    version: str
    strategy: str
    status: str
    target_percent: int
    tenant_id: Optional[str]
    channel: str
    runtime_environment_id: Optional[str]
    artifact_uri: Optional[str]
    artifact_hash: Optional[str]
    health_policy: JsonDict
    required_eval_set_id: Optional[str]
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class EvalSet:
    id: str
    name: str
    description: str
    scoring: str
    baseline_score: Optional[float]
    regression_threshold: float
    metadata: JsonDict
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class EvalRun:
    id: str
    eval_set_id: str
    target_kind: str
    target_id: str
    score: float
    baseline_score: Optional[float]
    delta: Optional[float]
    threshold: float
    passed: bool
    detail: JsonDict
    evidence_id: Optional[str]
    created_by: str
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


def validate_transition(current: str, target: str) -> None:
    allowed = TASK_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise TransitionError("cannot transition task from %s to %s" % (current, target))


def ensure_json_object(value: Optional[Mapping[str, Any]]) -> JsonDict:
    if value is None:
        return {}
    return dict(value)
