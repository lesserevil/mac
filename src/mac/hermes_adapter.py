from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


JsonDict = Dict[str, Any]
Transport = Callable[[str, str, Optional[JsonDict]], Any]
MemorySink = Callable[[JsonDict], None]


SECRET_FIELD_HINTS = ("secret", "token", "password", "private_key", "credential")


class MacApiError(RuntimeError):
    """Raised when the Hermes adapter cannot complete a mac API operation."""


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
        return self.client.get("/tasks/%s/summary" % task_id)

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


def _cmd_reply(args: argparse.Namespace) -> None:
    print(_adapter(args).user_reply_for_task(args.task_id))


def _cmd_writeback(args: argparse.Namespace) -> None:
    _print(_adapter(args).write_completed_task_to_memory(args.hermes_instance_id, args.task_id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac-hermes",
        description="Hermes-side adapter for the mac control plane",
    )
    parser.add_argument("--url", default=os.environ.get("MAC_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("MAC_TOKEN"))
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
