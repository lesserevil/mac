from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


JsonDict = Dict[str, Any]
Transport = Callable[[str, str, Optional[JsonDict]], Any]
MemorySink = Callable[[JsonDict], None]


SECRET_FIELD_HINTS = ("secret", "token", "password", "private_key", "credential")


class MacApiError(RuntimeError):
    """Raised when the Hermes adapter cannot complete a mac API operation."""


def _path_part(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _query(params: Iterable[tuple[str, Any]]) -> str:
    encoded = []
    for key, value in params:
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        encoded.append((key, value))
    return urllib.parse.urlencode(encoded)


@dataclass
class PlatformBindingSpec:
    platform: str
    external_id: str
    display_name: str = ""
    scopes: JsonDict = field(default_factory=dict)
    metadata: JsonDict = field(default_factory=dict)


@dataclass
class ConversationTaskInput:
    title: str
    summary: str
    user_id: Optional[str] = None
    platform_binding_id: Optional[str] = None
    conversation_ref: Optional[str] = None
    project: Optional[str] = None
    priority: int = 0
    required_capabilities: Sequence[str] = field(default_factory=tuple)
    dependencies: Sequence[str] = field(default_factory=tuple)
    snippets: Sequence[str] = field(default_factory=tuple)
    links: Sequence[str] = field(default_factory=tuple)
    metadata: JsonDict = field(default_factory=dict)
    max_attempts: int = 3

    def description(self) -> str:
        sections = ["Summary:\n%s" % self.summary.strip()]
        snippets = [snippet.strip() for snippet in self.snippets if snippet.strip()]
        if snippets:
            sections.append("Relevant excerpts:\n" + "\n".join("- %s" % item for item in snippets))
        links = [link.strip() for link in self.links if link.strip()]
        if links:
            sections.append("References:\n" + "\n".join("- %s" % item for item in links))
        return "\n\n".join(sections)

    def sanitized_metadata(self) -> JsonDict:
        metadata = _sanitize_json_object(self.metadata)
        metadata.setdefault(
            "sanitized_conversation",
            {
                "summary": self.summary.strip(),
                "snippets": [snippet.strip() for snippet in self.snippets if snippet.strip()],
                "links": [link.strip() for link in self.links if link.strip()],
            },
        )
        return metadata


class MacApiClient:
    """Small HTTP client for Hermes-side integrations.

    The optional transport hook is intentionally narrow and exists so tests or
    in-process gateway adapters can call FastAPI without bypassing the API
    contract.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: float = 10.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def get(self, path: str) -> Any:
        return self.request("GET", path, None)

    def post(self, path: str, payload: JsonDict) -> Any:
        return self.request("POST", path, payload)

    def request(self, method: str, path: str, payload: Optional[JsonDict]) -> Any:
        if self.transport is not None:
            return self.transport(method, path, payload)
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise MacApiError("mac API %s %s failed: %s" % (method, path, detail)) from exc
        except urllib.error.URLError as exc:
            raise MacApiError("mac API %s %s failed: %s" % (method, path, exc.reason)) from exc
        return json.loads(raw) if raw else None


class HermesMacAdapter:
    """High-level helper intended to be called from Hermes skills/gateways."""

    def __init__(self, client: MacApiClient) -> None:
        self.client = client

    def register_identity(
        self,
        tenant_name: str,
        persona_name: str,
        instance_name: str,
        soul_ref: str,
        memory_scope: str,
        home_ref: str = "",
        platform_bindings: Iterable[PlatformBindingSpec] = (),
    ) -> JsonDict:
        tenant = self.client.post("/tenants", {"name": tenant_name})
        persona = self.client.post(
            "/personas",
            {
                "tenant_id": tenant["id"],
                "name": persona_name,
                "soul_ref": soul_ref,
                "memory_scope": memory_scope,
            },
        )
        instance = self.client.post(
            "/hermes-instances",
            {
                "tenant_id": tenant["id"],
                "name": instance_name,
                "persona_id": persona["id"],
                "home_ref": home_ref,
            },
        )
        bindings = []
        for binding in platform_bindings:
            bindings.append(
                self.client.post(
                    "/platform-bindings",
                    {
                        "tenant_id": tenant["id"],
                        "hermes_instance_id": instance["id"],
                        "platform": binding.platform,
                        "external_id": binding.external_id,
                        "display_name": binding.display_name,
                        "scopes": binding.scopes,
                        "metadata": binding.metadata,
                    },
                )
            )
        return {
            "tenant": tenant,
            "persona": persona,
            "hermes_instance": instance,
            "platform_bindings": bindings,
        }

    def create_task_from_conversation(
        self,
        hermes_instance_id: str,
        task_input: ConversationTaskInput,
    ) -> JsonDict:
        if not task_input.title.strip():
            raise MacApiError("conversation task title is required")
        if not task_input.summary.strip():
            raise MacApiError("conversation task summary is required")
        return self.client.post(
            "/hermes-instances/%s/tasks" % hermes_instance_id,
            {
                "title": task_input.title,
                "description": task_input.description(),
                "project": task_input.project,
                "priority": task_input.priority,
                "required_capabilities": list(task_input.required_capabilities),
                "dependencies": list(task_input.dependencies),
                "metadata": task_input.sanitized_metadata(),
                "max_attempts": task_input.max_attempts,
                "user_id": task_input.user_id,
                "platform_binding_id": task_input.platform_binding_id,
                "conversation_ref": task_input.conversation_ref,
                "actor": "hermes",
            },
        )

    def task_summary(self, task_id: str) -> JsonDict:
        return self.client.get("/tasks/%s/summary" % _path_part(task_id))

    def task_detail(self, task_id: str) -> JsonDict:
        return self.client.get("/tasks/%s" % _path_part(task_id))

    def work_context(
        self,
        hermes_instance_id: str,
        *,
        include_completed: bool = True,
        task_limit: int = 100,
    ) -> JsonDict:
        query = _query(
            (
                ("include_completed", include_completed),
                ("task_limit", int(task_limit)),
            )
        )
        return self.client.get(
            "/hermes-instances/%s/work-context?%s" % (_path_part(hermes_instance_id), query)
        )

    def work_context_brief(self, hermes_instance_id: str) -> str:
        context = self.work_context(hermes_instance_id, include_completed=False, task_limit=20)
        projects = context.get("projects") or []
        tasks = context.get("tasks") or []
        agents = context.get("agents") or []
        project_names = ", ".join(str(project.get("project")) for project in projects[:5])
        return (
            "MAC work context: %d active task(s), %d project(s), %d agent(s). "
            "Projects: %s."
            % (
                len(tasks),
                len(projects),
                len(agents),
                project_names or "none",
            )
        )

    def runtime_proof(self, hermes_instance_id: str) -> JsonDict:
        return self.client.get(
            "/hermes-instances/%s/runtime-proof" % _path_part(hermes_instance_id)
        )

    def import_project_item(
        self,
        source: str,
        external_id: str,
        title: str,
        *,
        payload: Optional[JsonDict] = None,
        required_capabilities: Sequence[str] = (),
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/bridge/items",
            {
                "source": source,
                "external_id": external_id,
                "title": title,
                "payload": _sanitize_json_object(payload or {}),
                "required_capabilities": list(required_capabilities),
                "actor": actor,
            },
        )

    def list_project_items(self) -> Any:
        return self.client.get("/bridge/items")

    def register_beads_repository(
        self,
        name: str,
        path: str,
        *,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Sequence[str] = (),
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[JsonDict] = None,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/bridge/beads/repositories",
            {
                "name": name,
                "path": path,
                "source": source,
                "project": project,
                "required_capabilities": list(required_capabilities),
                "enabled": enabled,
                "poll_interval_seconds": int(poll_interval_seconds),
                "metadata": _sanitize_json_object(metadata or {}),
                "actor": actor,
            },
        )

    def list_beads_repositories(self, *, enabled: Optional[bool] = None) -> Any:
        query = _query((("enabled", enabled),))
        path = "/bridge/beads/repositories"
        if query:
            path = "%s?%s" % (path, query)
        return self.client.get(path)

    def poll_beads_repositories(
        self,
        *,
        repository: Optional[str] = None,
        force: bool = False,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/bridge/beads/poll",
            {
                "repository": repository,
                "force": force,
                "actor": actor,
            },
        )

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        lease_seconds: int = 900,
    ) -> JsonDict:
        query = _query((("agent_id", agent_id), ("lease_seconds", int(lease_seconds))))
        return self.client.post("/tasks/%s/claim?%s" % (_path_part(task_id), query), {})

    def start_task(self, task_id: str, agent_id: str) -> JsonDict:
        query = _query((("agent_id", agent_id),))
        return self.client.post("/tasks/%s/start?%s" % (_path_part(task_id), query), {})

    def transition_task(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[JsonDict] = None,
    ) -> JsonDict:
        return self.client.post(
            "/tasks/%s/transition" % _path_part(task_id),
            {
                "target_state": target_state,
                "actor": actor,
                "detail": _sanitize_json_object(detail or {}),
            },
        )

    def add_evidence(
        self,
        task_id: str,
        kind: str,
        uri: str,
        summary: str,
        created_by: str,
        *,
        checksum: Optional[str] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        return self.client.post(
            "/tasks/%s/evidence" % _path_part(task_id),
            {
                "kind": kind,
                "uri": uri,
                "summary": summary,
                "created_by": created_by,
                "checksum": checksum,
                "metadata": _sanitize_json_object(metadata or {}),
            },
        )

    def submit_for_review(
        self,
        task_id: str,
        agent_id: str,
        *,
        advance_default_workflow: bool = False,
    ) -> JsonDict:
        query = _query(
            (
                ("agent_id", agent_id),
                ("advance_default_workflow", advance_default_workflow),
            )
        )
        return self.client.post(
            "/tasks/%s/submit-for-review?%s" % (_path_part(task_id), query),
            {},
        )

    def request_review(
        self,
        task_id: str,
        reviewer_agent_id: str,
        *,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/tasks/%s/reviews" % _path_part(task_id),
            {"reviewer_agent_id": reviewer_agent_id, "actor": actor},
        )

    def claim_review(
        self,
        review_id: str,
        reviewer_agent_id: str,
        *,
        executor_evidence_id: Optional[str] = None,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/reviews/%s/claim" % _path_part(review_id),
            {
                "reviewer_agent_id": reviewer_agent_id,
                "executor_evidence_id": executor_evidence_id,
                "actor": actor,
            },
        )

    def submit_review(
        self,
        review_id: str,
        status: str,
        reviewer_agent_id: str,
        *,
        reason: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> JsonDict:
        return self.client.post(
            "/reviews/%s/decision" % _path_part(review_id),
            {
                "status": status,
                "reviewer_agent_id": reviewer_agent_id,
                "reason": reason,
                "evidence_id": evidence_id,
            },
        )

    def publish_task(
        self,
        task_id: str,
        target: str,
        created_by: str,
        *,
        evidence_id: Optional[str] = None,
    ) -> JsonDict:
        return self.client.post(
            "/publications",
            {
                "task_id": task_id,
                "target": target,
                "created_by": created_by,
                "evidence_id": evidence_id,
            },
        )

    def user_reply_for_task(self, task_id: str) -> str:
        summary = self.task_summary(task_id)
        if summary["state"] == "completed":
            publications = summary.get("publications") or []
            if publications:
                return "%s is complete and published to %s." % (
                    summary["title"],
                    publications[-1]["target"],
                )
            return "%s is complete." % summary["title"]
        if summary["state"] in {"failed", "cancelled"}:
            return "%s is %s." % (summary["title"], summary["state"])
        return "%s is currently %s." % (summary["title"], summary["state"])

    def memory_writeback_payload(self, hermes_instance_id: str, task_id: str) -> JsonDict:
        context = self.client.get("/hermes-instances/%s/context" % hermes_instance_id)
        summary = self.task_summary(task_id)
        if summary["state"] != "completed":
            raise MacApiError("only completed tasks should be written back to Hermes memory")
        memory_contract = context["memory_contract"]
        memory_scope = memory_contract.get("memory_scope")
        if not memory_scope:
            raise MacApiError("Hermes instance has no memory_scope")
        return {
            "memory_scope": memory_scope,
            "content": self.user_reply_for_task(task_id),
            "metadata": {
                "source": "mac",
                "task_id": task_id,
                "hermes_instance_id": hermes_instance_id,
                "persona_id": context["hermes_instance"].get("persona_id"),
                "origin": summary.get("origin"),
            },
        }

    def record_memory_writeback(
        self,
        hermes_instance_id: str,
        task_id: str,
        payload: Optional[JsonDict] = None,
        created_by: str = "hermes",
    ) -> JsonDict:
        payload = payload or self.memory_writeback_payload(hermes_instance_id, task_id)
        return self.client.post(
            "/memory",
            {
                "task_id": task_id,
                "subject_type": "hermes_memory",
                "subject_id": hermes_instance_id,
                "record_type": "task_result_writeback",
                "content": payload["content"],
                "created_by": created_by,
            },
        )

    def write_completed_task_to_memory(
        self,
        hermes_instance_id: str,
        task_id: str,
        sink: Optional[MemorySink] = None,
        created_by: str = "hermes",
    ) -> JsonDict:
        payload = self.memory_writeback_payload(hermes_instance_id, task_id)
        if sink is not None:
            sink(payload)
        record = self.record_memory_writeback(
            hermes_instance_id,
            task_id,
            payload=payload,
            created_by=created_by,
        )
        return {"payload": payload, "record": record}


def _sanitize_json_object(value: JsonDict) -> JsonDict:
    return _sanitize_json(value, ())


def _sanitize_json(value: Any, path: Sequence[str]) -> Any:
    if isinstance(value, dict):
        sanitized: JsonDict = {}
        for key, nested in value.items():
            key_lower = str(key).lower()
            if any(hint in key_lower for hint in SECRET_FIELD_HINTS):
                continue
            if key_lower in {"raw_messages", "memory", "private_memory", "user_memory"}:
                continue
            sanitized[str(key)] = _sanitize_json(nested, path + (str(key),))
        return sanitized
    if isinstance(value, list):
        return [_sanitize_json(item, path + (str(index),)) for index, item in enumerate(value)]
    return value


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_arg(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _adapter(args: argparse.Namespace) -> HermesMacAdapter:
    client = MacApiClient(args.url, token=args.token)
    return HermesMacAdapter(client)


def _cmd_register(args: argparse.Namespace) -> None:
    bindings = []
    for raw in args.binding or []:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            raise MacApiError("--binding must be platform:external_id[:display_name]")
        bindings.append(
            PlatformBindingSpec(
                parts[0],
                parts[1],
                parts[2] if len(parts) == 3 else "",
            )
        )
    _print(
        _adapter(args).register_identity(
            args.tenant,
            args.persona,
            args.instance,
            args.soul_ref,
            args.memory_scope,
            home_ref=args.home_ref or "",
            platform_bindings=bindings,
        )
    )


def _cmd_task(args: argparse.Namespace) -> None:
    task_input = ConversationTaskInput(
        title=args.title,
        summary=args.summary,
        platform_binding_id=args.platform_binding_id,
        conversation_ref=args.conversation_ref,
        project=args.project,
        priority=args.priority,
        required_capabilities=_csv(args.required_capabilities),
        snippets=args.snippet or (),
        links=args.link or (),
    )
    _print(_adapter(args).create_task_from_conversation(args.hermes_instance_id, task_input))


def _cmd_summary(args: argparse.Namespace) -> None:
    _print(_adapter(args).task_summary(args.task_id))


def _cmd_task_detail(args: argparse.Namespace) -> None:
    _print(_adapter(args).task_detail(args.task_id))


def _cmd_work_context(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).work_context(
            args.hermes_instance_id,
            include_completed=not args.active_only,
            task_limit=args.task_limit,
        )
    )


def _cmd_work_brief(args: argparse.Namespace) -> None:
    print(_adapter(args).work_context_brief(args.hermes_instance_id))


def _cmd_runtime_proof(args: argparse.Namespace) -> None:
    _print(_adapter(args).runtime_proof(args.hermes_instance_id))


def _cmd_import_project_item(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).import_project_item(
            args.source,
            args.external_id,
            args.title,
            payload=_json_arg(args.payload, {}),
            required_capabilities=_csv(args.required_capabilities),
            actor=args.actor,
        )
    )


def _cmd_project_items(args: argparse.Namespace) -> None:
    _print(_adapter(args).list_project_items())


def _cmd_register_beads_repository(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).register_beads_repository(
            args.name,
            args.path,
            source=args.source,
            project=args.project,
            required_capabilities=_csv(args.required_capabilities),
            enabled=not args.disabled,
            poll_interval_seconds=args.poll_interval_seconds,
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def _cmd_beads_repositories(args: argparse.Namespace) -> None:
    _print(_adapter(args).list_beads_repositories(enabled=args.enabled))


def _cmd_poll_beads_repositories(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).poll_beads_repositories(
            repository=args.repository,
            force=args.force,
            actor=args.actor,
        )
    )


def _cmd_claim(args: argparse.Namespace) -> None:
    _print(_adapter(args).claim_task(args.task_id, args.agent_id, lease_seconds=args.lease_seconds))


def _cmd_start(args: argparse.Namespace) -> None:
    _print(_adapter(args).start_task(args.task_id, args.agent_id))


def _cmd_transition(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).transition_task(
            args.task_id,
            args.target_state,
            args.actor,
            _json_arg(args.detail, {}),
        )
    )


def _cmd_evidence(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).add_evidence(
            args.task_id,
            args.kind,
            args.uri,
            args.summary,
            args.created_by,
            checksum=args.checksum,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def _cmd_submit_review(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).submit_for_review(
            args.task_id,
            args.agent_id,
            advance_default_workflow=args.advance_default_workflow,
        )
    )


def _cmd_request_review(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).request_review(
            args.task_id,
            args.reviewer_agent_id,
            actor=args.actor,
        )
    )


def _cmd_claim_review(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).claim_review(
            args.review_id,
            args.reviewer_agent_id,
            executor_evidence_id=args.executor_evidence_id,
            actor=args.actor,
        )
    )


def _cmd_review_decision(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).submit_review(
            args.review_id,
            args.status,
            args.reviewer_agent_id,
            reason=args.reason,
            evidence_id=args.evidence_id,
        )
    )


def _cmd_publish(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).publish_task(
            args.task_id,
            args.target,
            args.created_by,
            evidence_id=args.evidence_id,
        )
    )


def _cmd_reply(args: argparse.Namespace) -> None:
    print(_adapter(args).user_reply_for_task(args.task_id))


def _cmd_writeback(args: argparse.Namespace) -> None:
    _print(_adapter(args).write_completed_task_to_memory(args.hermes_instance_id, args.task_id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac-hermes",
        description="Hermes-side adapter for the mac control plane",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL") or "http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MAC_TOKEN")
        or os.environ.get("MAC_WORKER_TOKEN")
        or os.environ.get("MAC_API_TOKEN"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register tenant, persona, Hermes instance, and optional platform bindings")
    register.add_argument("--tenant", required=True)
    register.add_argument("--persona", required=True)
    register.add_argument("--instance", required=True)
    register.add_argument("--soul-ref", required=True)
    register.add_argument("--memory-scope", required=True)
    register.add_argument("--home-ref")
    register.add_argument("--binding", action="append", help="platform:external_id[:display_name]")
    register.set_defaults(func=_cmd_register)

    task = sub.add_parser("task", help="create a durable task from sanitized conversation context")
    task.add_argument("hermes_instance_id")
    task.add_argument("title")
    task.add_argument("--summary", required=True)
    task.add_argument("--platform-binding-id")
    task.add_argument("--conversation-ref")
    task.add_argument("--project")
    task.add_argument("--priority", type=int, default=0)
    task.add_argument("--required-capabilities")
    task.add_argument("--snippet", action="append")
    task.add_argument("--link", action="append")
    task.set_defaults(func=_cmd_task)

    summary = sub.add_parser("summary", help="fetch a task summary for a user-facing response")
    summary.add_argument("task_id")
    summary.set_defaults(func=_cmd_summary)

    task_detail = sub.add_parser("task-detail", help="fetch MAC's full task detail")
    task_detail.add_argument("task_id")
    task_detail.set_defaults(func=_cmd_task_detail)

    work_context = sub.add_parser("work-context", help="fetch MAC's task/project/agent context for this Hermes instance")
    work_context.add_argument("hermes_instance_id")
    work_context.add_argument("--active-only", action="store_true")
    work_context.add_argument("--task-limit", type=int, default=100)
    work_context.set_defaults(func=_cmd_work_context)

    work_brief = sub.add_parser("work-brief", help="render a concise MAC work-context status line")
    work_brief.add_argument("hermes_instance_id")
    work_brief.set_defaults(func=_cmd_work_brief)

    runtime_proof = sub.add_parser("runtime-proof", help="prove MAC/Hermes task-project bridge readiness")
    runtime_proof.add_argument("hermes_instance_id")
    runtime_proof.set_defaults(func=_cmd_runtime_proof)

    import_project_item = sub.add_parser("import-project-item", help="import an external project item into MAC")
    import_project_item.add_argument("source")
    import_project_item.add_argument("external_id")
    import_project_item.add_argument("title")
    import_project_item.add_argument("--payload", default="{}")
    import_project_item.add_argument("--required-capabilities")
    import_project_item.add_argument("--actor", default="hermes")
    import_project_item.set_defaults(func=_cmd_import_project_item)

    project_items = sub.add_parser("project-items", help="list MAC bridge project items")
    project_items.set_defaults(func=_cmd_project_items)

    beads_repositories = sub.add_parser("beads-repositories", help="list registered Beads repositories")
    beads_repositories.add_argument("--enabled", action="store_true", default=None)
    beads_repositories.set_defaults(func=_cmd_beads_repositories)

    register_beads_repository = sub.add_parser("register-beads-repository", help="register a Beads-backed project repository")
    register_beads_repository.add_argument("name")
    register_beads_repository.add_argument("path")
    register_beads_repository.add_argument("--source")
    register_beads_repository.add_argument("--project")
    register_beads_repository.add_argument("--required-capabilities")
    register_beads_repository.add_argument("--poll-interval-seconds", type=int, default=60)
    register_beads_repository.add_argument("--metadata", default="{}")
    register_beads_repository.add_argument("--disabled", action="store_true")
    register_beads_repository.add_argument("--actor", default="hermes")
    register_beads_repository.set_defaults(func=_cmd_register_beads_repository)

    poll_beads_repositories = sub.add_parser("poll-beads-repositories", help="poll registered Beads repositories")
    poll_beads_repositories.add_argument("--repository")
    poll_beads_repositories.add_argument("--force", action="store_true")
    poll_beads_repositories.add_argument("--actor", default="hermes")
    poll_beads_repositories.set_defaults(func=_cmd_poll_beads_repositories)

    claim = sub.add_parser("claim", help="claim a MAC task for an agent")
    claim.add_argument("task_id")
    claim.add_argument("agent_id")
    claim.add_argument("--lease-seconds", type=int, default=900)
    claim.set_defaults(func=_cmd_claim)

    start = sub.add_parser("start", help="start a claimed MAC task")
    start.add_argument("task_id")
    start.add_argument("agent_id")
    start.set_defaults(func=_cmd_start)

    transition = sub.add_parser("transition", help="transition a MAC task")
    transition.add_argument("task_id")
    transition.add_argument("target_state")
    transition.add_argument("--actor", required=True)
    transition.add_argument("--detail", default="{}")
    transition.set_defaults(func=_cmd_transition)

    evidence = sub.add_parser("evidence", help="add MAC task evidence")
    evidence.add_argument("task_id")
    evidence.add_argument("--kind", required=True)
    evidence.add_argument("--uri", required=True)
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--created-by", required=True)
    evidence.add_argument("--checksum")
    evidence.add_argument("--metadata", default="{}")
    evidence.set_defaults(func=_cmd_evidence)

    submit_review = sub.add_parser("submit-review", help="submit a task for review")
    submit_review.add_argument("task_id")
    submit_review.add_argument("agent_id")
    submit_review.add_argument("--advance-default-workflow", action="store_true")
    submit_review.set_defaults(func=_cmd_submit_review)

    request_review = sub.add_parser("request-review", help="request a review for a task")
    request_review.add_argument("task_id")
    request_review.add_argument("reviewer_agent_id")
    request_review.add_argument("--actor", default="hermes")
    request_review.set_defaults(func=_cmd_request_review)

    claim_review = sub.add_parser("claim-review", help="claim a review with optional executor evidence context")
    claim_review.add_argument("review_id")
    claim_review.add_argument("reviewer_agent_id")
    claim_review.add_argument("--executor-evidence-id")
    claim_review.add_argument("--actor", default="hermes")
    claim_review.set_defaults(func=_cmd_claim_review)

    review_decision = sub.add_parser("review-decision", help="record a MAC review decision")
    review_decision.add_argument("review_id")
    review_decision.add_argument("status")
    review_decision.add_argument("reviewer_agent_id")
    review_decision.add_argument("--reason")
    review_decision.add_argument("--evidence-id")
    review_decision.set_defaults(func=_cmd_review_decision)

    publish = sub.add_parser("publish", help="publish an approved MAC task")
    publish.add_argument("task_id")
    publish.add_argument("target")
    publish.add_argument("created_by")
    publish.add_argument("--evidence-id")
    publish.set_defaults(func=_cmd_publish)

    reply = sub.add_parser("reply", help="render a concise user-facing task status")
    reply.add_argument("task_id")
    reply.set_defaults(func=_cmd_reply)

    writeback = sub.add_parser("writeback", help="prepare and record Hermes memory write-back for a completed task")
    writeback.add_argument("hermes_instance_id")
    writeback.add_argument("task_id")
    writeback.set_defaults(func=_cmd_writeback)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        args.func(args)
    except MacApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
