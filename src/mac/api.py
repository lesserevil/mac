from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mac.models import AuthorizationError, MACError, NotFoundError
from mac.services import ControlPlane
from mac.store import SQLiteStore


def _data(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


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


class DispatchRequest(BaseModel):
    lease_seconds: int = 900
    limit: int = 100
    stale_after_seconds: Optional[int] = None


class MessageCreate(BaseModel):
    sender_agent_id: str
    recipient_agent_id: Optional[str] = None
    task_id: Optional[str] = None
    message_type: str
    payload: Dict[str, Any]


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


def _load_auth_tokens_from_env() -> Dict[str, List[str]]:
    raw = os.environ.get("MAC_API_TOKENS")
    if raw:
        loaded = json.loads(raw)
        return {str(token): [str(scope) for scope in scopes] for token, scopes in loaded.items()}
    single = os.environ.get("MAC_API_TOKEN")
    return {single: ["admin"]} if single else {}


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
    if path.startswith("/dispatch"):
        return "dispatch"
    if path.startswith("/secrets") or path.startswith("/secret-audits"):
        return "secret"
    return "write"


def _authorize_request(
    method: str,
    path: str,
    authorization: Optional[str],
    auth_tokens: Dict[str, List[str]],
) -> None:
    required = _required_scope(method, path)
    if required is None or not auth_tokens:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthorizationError("missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    scopes = set(auth_tokens.get(token) or [])
    if not scopes:
        raise AuthorizationError("unknown bearer token")
    if "admin" not in scopes and required not in scopes:
        raise AuthorizationError("token lacks required scope: %s" % required)


def create_app(
    db_path: Optional[str] = None,
    control_plane: Optional[ControlPlane] = None,
    auth_tokens: Optional[Dict[str, List[str]]] = None,
) -> FastAPI:
    cp = control_plane or ControlPlane(SQLiteStore(db_path or "mac.db"))
    tokens = auth_tokens if auth_tokens is not None else _load_auth_tokens_from_env()
    app = FastAPI(title="MAC Control Plane", version="0.1.0")
    app.state.control_plane = cp
    app.state.auth_tokens = tokens
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

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        try:
            _authorize_request(
                request.method,
                request.url.path,
                request.headers.get("authorization"),
                tokens,
            )
        except AuthorizationError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        return await call_next(request)

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    @app.post("/tenants")
    def register_tenant(body: TenantRegister) -> Dict[str, Any]:
        return cp.register_tenant(**_data(body)).to_dict()

    @app.get("/tenants")
    def list_tenants() -> List[Dict[str, Any]]:
        return [tenant.to_dict() for tenant in cp.list_tenants()]

    @app.post("/users")
    def register_user(body: UserRegister) -> Dict[str, Any]:
        return cp.register_user(**_data(body)).to_dict()

    @app.get("/users")
    def list_users(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [user.to_dict() for user in cp.list_users(tenant_id)]

    @app.post("/personas")
    def register_persona(body: PersonaRegister) -> Dict[str, Any]:
        return cp.register_persona(**_data(body)).to_dict()

    @app.get("/personas")
    def list_personas(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [persona.to_dict() for persona in cp.list_personas(tenant_id)]

    @app.post("/hermes-instances")
    def register_hermes_instance(body: HermesInstanceRegister) -> Dict[str, Any]:
        return cp.register_hermes_instance(**_data(body)).to_dict()

    @app.get("/hermes-instances")
    def list_hermes_instances(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [instance.to_dict() for instance in cp.list_hermes_instances(tenant_id)]

    @app.get("/hermes-instances/{instance_id}/context")
    def hermes_context(instance_id: str) -> Dict[str, Any]:
        return cp.hermes_context(instance_id)

    @app.post("/hermes-instances/{instance_id}/tasks")
    def create_interaction_task(instance_id: str, body: InteractionTaskCreate) -> Dict[str, Any]:
        data = _data(body)
        actor = data.pop("actor", "hermes")
        return cp.create_interaction_task(instance_id, actor=actor, **data).to_dict()

    @app.post("/platform-bindings")
    def register_platform_binding(body: PlatformBindingRegister) -> Dict[str, Any]:
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
    def create_task(body: TaskCreate) -> Dict[str, Any]:
        data = _data(body)
        actor = data.pop("actor", "human")
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
    def register_machine(body: MachineRegister) -> Dict[str, Any]:
        return cp.register_machine(**_data(body)).to_dict()

    @app.get("/machines")
    def list_machines() -> List[Dict[str, Any]]:
        return [machine.to_dict() for machine in cp.list_machines()]

    @app.post("/agents")
    def register_agent(body: AgentRegister) -> Dict[str, Any]:
        return cp.register_agent(**_data(body)).to_dict()

    @app.get("/agents")
    def list_agents() -> List[Dict[str, Any]]:
        return [agent.to_dict() for agent in cp.list_agents()]

    @app.post("/agents/{agent_id}/heartbeat")
    def heartbeat_agent(agent_id: str, body: HeartbeatRequest) -> Dict[str, Any]:
        return cp.heartbeat_agent(agent_id, **_data(body)).to_dict()

    @app.post("/dispatch/assign")
    def dispatch_once(body: DispatchRequest) -> Optional[Dict[str, Any]]:
        return cp.dispatch_once(body.lease_seconds)

    @app.post("/dispatch/tick")
    def dispatch_tick(body: DispatchRequest) -> Dict[str, Any]:
        return cp.tick(body.lease_seconds, body.limit, body.stale_after_seconds)

    @app.get("/dispatch/dead-letters")
    def dead_letters(tenant_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in cp.list_dead_letters(tenant_id)]

    @app.post("/messages")
    def send_message(body: MessageCreate) -> Dict[str, Any]:
        return cp.send_message(**_data(body)).to_dict()

    @app.get("/messages")
    def list_messages(agent_id: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
        return [message.to_dict() for message in cp.list_messages(agent_id)]

    @app.post("/agents/{agent_id}/messages/deliver")
    def deliver_messages(agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [message.to_dict() for message in cp.deliver_messages(agent_id, limit)]

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

    @app.post("/runtimes")
    def create_runtime(body: RuntimeCreate) -> Dict[str, Any]:
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
    def create_rollout(body: RolloutCreate) -> Dict[str, Any]:
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
