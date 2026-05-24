from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional


RUNTIME_CONTEXT_SCHEMA = "mac.hermes.runtime_context.v1"


def stable_id(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.lower()).strip("_")
    return "%s_%s" % (prefix, safe or "default")


def connection_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw.strip())
    if not parsed.scheme or not parsed.netloc:
        return raw.strip()
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    netloc = host
    if parsed.port is not None:
        netloc = "%s:%s" % (netloc, parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def set_env(path: Path, updates: Dict[str, Optional[str]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            if updates[key] is not None:
                output.append("%s=%s" % (key, updates[key]))
            seen.add(key)
        else:
            output.append(line)
    for key in sorted(updates):
        if key not in seen and updates[key] is not None:
            output.append("%s=%s" % (key, updates[key]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def build_runtime_context(
    *,
    agent_name: str,
    fleet_name: str,
    mac_url: str,
    hermes_home: Path,
    mac_home: Path,
    tenant_id: Optional[str] = None,
    persona_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_mac_url = connection_url(mac_url) if mac_url else ""
    resolved_tenant_id = tenant_id or stable_id("tenant", fleet_name)
    resolved_persona_id = persona_id or stable_id("persona", agent_name)
    resolved_instance_id = hermes_instance_id or stable_id("hermes", agent_name)
    resolved_agent_id = agent_id or stable_id("agent", agent_name)
    return {
        "schema": RUNTIME_CONTEXT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fleet": fleet_name,
        "agent": {
            "name": agent_name,
            "agent_id": resolved_agent_id,
            "hermes_instance_id": resolved_instance_id,
        },
        "identity": {
            "tenant_id": resolved_tenant_id,
            "persona_id": resolved_persona_id,
            "hermes_instance_id": resolved_instance_id,
            "agent_id": resolved_agent_id,
            "soul_ref": str(hermes_home / "SOUL.md"),
            "memory_scope": str(hermes_home),
            "home_ref": str(hermes_home),
        },
        "authority": {
            "tasks": "mac",
            "projects": "mac",
            "agents": "mac",
            "personality": "hermes",
            "user_memory": "hermes",
            "conversation_state": "hermes",
        },
        "endpoints": {
            "mac_api": resolved_mac_url,
            "work_context_api": (
                "%s/hermes-instances/%s/work-context" % (resolved_mac_url, resolved_instance_id)
                if resolved_mac_url
                else ""
            ),
        },
        "environment": {
            "MAC_URL": resolved_mac_url,
            "MAC_HERMES_INSTANCE_ID": resolved_instance_id,
            "MAC_WORKER_HERMES_INSTANCE_ID": resolved_instance_id,
            "MAC_AGENT_ID": resolved_agent_id,
            "MAC_WORKER_AGENT_NAME": agent_name,
            "MAC_HOME": str(mac_home),
            "HERMES_HOME": str(hermes_home),
        },
        "operations": {
            "refresh_context": [
                "mac hermes work-context %s" % resolved_instance_id,
                "mac-hermes work-context %s --active-only" % resolved_instance_id,
                "mac-hermes work-brief %s" % resolved_instance_id,
            ],
            "create_task": [
                "mac-hermes task %s <title> --summary <summary> --project <project>"
                % resolved_instance_id,
            ],
            "task_lifecycle": [
                "mac-hermes task-detail {task_id}",
                "mac-hermes claim {task_id} %s" % resolved_agent_id,
                "mac-hermes start {task_id} %s" % resolved_agent_id,
                "mac-hermes evidence {task_id} --kind test --uri artifact://... --summary ... --created-by %s"
                % resolved_agent_id,
                "mac-hermes submit-review {task_id} %s" % resolved_agent_id,
                "mac-hermes request-review {task_id} {reviewer_agent_id}",
                "mac-hermes claim-review {review_id} %s" % resolved_agent_id,
                "mac-hermes review-decision {review_id} approved %s --evidence-id {evidence_id}"
                % resolved_agent_id,
                "mac-hermes publish {task_id} git://main %s --evidence-id {evidence_id}"
                % resolved_agent_id,
                "mac-hermes writeback %s {task_id}" % resolved_instance_id,
            ],
        },
        "runtime_rules": [
            "MAC is authoritative for task, project, dependency, assignment, review, and publication state.",
            "Hermes is authoritative for soul, personality, private memory, and conversation state.",
            "Refresh MAC work context before selecting, changing, or reporting on work.",
            "Do not copy MAC task state into Hermes memory as a source of truth; write only completed-task summaries back to Hermes memory.",
        ],
    }


def render_runtime_markdown(context: Dict[str, Any]) -> str:
    identity = context["identity"]
    agent = context["agent"]
    operations = context["operations"]
    authority = context["authority"]
    lines = [
        "# MAC Task and Project Runtime",
        "",
        "This Hermes runtime is coupled to MAC for operational work.",
        "",
        "## Identity",
        "",
        "- Agent: `%s` (`%s`)" % (agent["name"], agent["agent_id"]),
        "- Hermes instance: `%s`" % identity["hermes_instance_id"],
        "- Tenant: `%s`" % identity["tenant_id"],
        "- Persona: `%s`" % identity["persona_id"],
        "",
        "## Authority",
        "",
        "- Tasks, projects, agents: `%s`, `%s`, `%s`"
        % (authority["tasks"], authority["projects"], authority["agents"]),
        "- Personality, user memory, conversation state: `%s`, `%s`, `%s`"
        % (authority["personality"], authority["user_memory"], authority["conversation_state"]),
        "",
        "## Refresh Work Context",
        "",
    ]
    lines.extend("- `%s`" % command for command in operations["refresh_context"])
    lines.extend(["", "## Task Lifecycle", ""])
    for command in operations["create_task"] + operations["task_lifecycle"]:
        lines.append("- `%s`" % command)
    lines.extend(
        [
            "",
            "## Runtime Rules",
            "",
        ]
    )
    lines.extend("- %s" % rule for rule in context["runtime_rules"])
    return "\n".join(lines) + "\n"


def write_runtime_context(
    *,
    context_path: Path,
    markdown_path: Path,
    hermes_env_path: Path,
    agent_name: str,
    fleet_name: str,
    mac_url: str,
    hermes_home: Path,
    mac_home: Path,
    tenant_id: Optional[str] = None,
    persona_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    context = build_runtime_context(
        agent_name=agent_name,
        fleet_name=fleet_name,
        mac_url=mac_url,
        hermes_home=hermes_home,
        mac_home=mac_home,
        tenant_id=tenant_id,
        persona_id=persona_id,
        hermes_instance_id=hermes_instance_id,
        agent_id=agent_id,
    )
    context_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    context_path.chmod(0o600)
    markdown_path.write_text(render_runtime_markdown(context), encoding="utf-8")
    markdown_path.chmod(0o600)
    set_env(
        hermes_env_path,
        {
            "MAC_HERMES_RUNTIME_CONTEXT_FILE": str(context_path),
            "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN": str(markdown_path),
            "MAC_HERMES_RUNTIME_CONTEXT_REQUIRED": "1",
            "MAC_HERMES_INSTANCE_ID": context["identity"]["hermes_instance_id"],
            "MAC_WORKER_HERMES_INSTANCE_ID": context["identity"]["hermes_instance_id"],
            "MAC_AGENT_ID": context["identity"]["agent_id"],
            "MAC_URL": context["environment"]["MAC_URL"],
        },
    )
    return context


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="write Hermes-visible MAC runtime context")
    parser.add_argument("context_path")
    parser.add_argument("markdown_path")
    parser.add_argument("hermes_env_path")
    parser.add_argument("--agent-name", default=os.environ.get("AGENT") or os.environ.get("MAC_WORKER_AGENT_NAME", "agent"))
    parser.add_argument("--fleet-name", default=os.environ.get("FLEET_NAME", "mac"))
    parser.add_argument("--mac-url", default=os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL", ""))
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--mac-home", default=os.environ.get("MAC_HOME", str(Path.home() / ".mac")))
    parser.add_argument("--tenant-id", default=os.environ.get("MAC_FLEET_TENANT_ID"))
    parser.add_argument("--persona-id", default=os.environ.get("MAC_HERMES_PERSONA_ID"))
    parser.add_argument("--hermes-instance-id", default=os.environ.get("MAC_HERMES_INSTANCE_ID"))
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID"))
    args = parser.parse_args(argv)
    context = write_runtime_context(
        context_path=Path(args.context_path).expanduser(),
        markdown_path=Path(args.markdown_path).expanduser(),
        hermes_env_path=Path(args.hermes_env_path).expanduser(),
        agent_name=args.agent_name,
        fleet_name=args.fleet_name,
        mac_url=args.mac_url,
        hermes_home=Path(args.hermes_home).expanduser(),
        mac_home=Path(args.mac_home).expanduser(),
        tenant_id=args.tenant_id,
        persona_id=args.persona_id,
        hermes_instance_id=args.hermes_instance_id,
        agent_id=args.agent_id,
    )
    print(
        "runtime context: agent=%s hermes_instance=%s mac_url=%s"
        % (
            context["agent"]["agent_id"],
            context["identity"]["hermes_instance_id"],
            context["endpoints"]["mac_api"] or "unconfigured",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
