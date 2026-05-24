from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


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


def _repository_contract(workspace_path: Path) -> Dict[str, Any]:
    contract_path = workspace_path / ".mac" / "project.yaml"
    contract: Dict[str, Any] = {
        "path": str(contract_path),
        "exists": contract_path.exists(),
        "schema": None,
        "project": "",
        "required_commands": [],
        "bootstrap_command": "",
        "test_command": "scripts/run-contract-tests.sh",
        "evidence_required": [],
    }
    if not contract_path.exists() or not contract_path.is_file():
        return contract
    try:
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        contract["error"] = str(exc)
        return contract
    if not isinstance(data, dict):
        contract["error"] = "repository contract root is not an object"
        return contract
    toolchain = data.get("toolchain") if isinstance(data.get("toolchain"), dict) else {}
    bootstrap = data.get("bootstrap") if isinstance(data.get("bootstrap"), dict) else {}
    test = data.get("test") if isinstance(data.get("test"), dict) else {}
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    contract.update(
        {
            "schema": data.get("schema"),
            "project": data.get("project") or "",
            "required_commands": sorted(
                str(item)
                for item in (toolchain.get("required_commands") or [])
                if str(item).strip()
            ),
            "bootstrap_command": str(bootstrap.get("command") or ""),
            "test_command": str(test.get("command") or contract["test_command"]),
            "evidence_required": sorted(
                str(item)
                for item in (evidence.get("required") or [])
                if str(item).strip()
            ),
        }
    )
    return contract


def _session_capability_contract(
    *,
    workspace_path: Path,
    mac_home: Path,
    mac_url: str,
    hermes_instance_id: str,
    agent_id: str,
    repository_contract: Dict[str, Any],
) -> Dict[str, Any]:
    test_command = str(repository_contract.get("test_command") or "scripts/run-contract-tests.sh")
    return {
        "schema": "mac.hermes.session_capabilities.v1",
        "parity_target": "direct_codex_or_claude_session_in_mac_repo",
        "workspace": {
            "path": str(workspace_path),
            "project_contract": repository_contract,
            "working_directory_rule": "Run repository commands from this workspace unless a task repository contract says otherwise.",
        },
        "capabilities": [
            {
                "name": "mac_api",
                "kind": "api",
                "required": True,
                "endpoint": mac_url,
                "purpose": "Read and mutate MAC task, project, agent, review, memory, and dashboard state.",
            },
            {
                "name": "mac_cli",
                "kind": "cli",
                "required": True,
                "command": "mac --help",
                "expected_path": str(mac_home / "venv" / "bin" / "mac"),
                "purpose": "Operator-grade MAC API access from a shell session.",
            },
            {
                "name": "mac_hermes_cli",
                "kind": "cli",
                "required": True,
                "command": "mac-hermes work-context %s --active-only" % hermes_instance_id,
                "expected_path": str(mac_home / "venv" / "bin" / "mac-hermes"),
                "purpose": "Hermes-safe task/project lifecycle bridge.",
            },
            {
                "name": "hgmac_agent_ops_cli",
                "kind": "cli",
                "required": True,
                "command": "hgmac agents list",
                "expected_path": str(mac_home / "venv" / "bin" / "hgmac"),
                "purpose": "Full CRUD and operational control for MAC agents.",
            },
            {
                "name": "beads_issue_tracker",
                "kind": "cli",
                "required": True,
                "command": "bd prime",
                "expected_path": str(mac_home / "bin" / "bd"),
                "purpose": "Project issue workflow and persistent work tracking.",
            },
            {
                "name": "git_source_control",
                "kind": "cli",
                "required": True,
                "command": "git status --short --branch",
                "cwd": str(workspace_path),
                "purpose": "Inspect, commit, rebase, and push repository state.",
            },
            {
                "name": "quality_gate",
                "kind": "command",
                "required": True,
                "command": test_command,
                "cwd": str(workspace_path),
                "purpose": "Run the repository contract test gate before completion.",
            },
            {
                "name": "web_search",
                "kind": "service",
                "required": True,
                "command": "mac-firecrawl-gateway --help",
                "expected_path": str(mac_home / "venv" / "bin" / "mac-firecrawl-gateway"),
                "environment": ["FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "MAC_WEB_SEARCH_PROVIDER"],
                "purpose": "Web search, extraction, and crawl access through the hub Firecrawl-compatible service.",
            },
        ],
        "direct_session_workflow": [
            "cd %s" % workspace_path,
            "bd prime",
            "mac-hermes runtime-proof %s" % hermes_instance_id,
            "mac-hermes work-context %s --active-only" % hermes_instance_id,
            "mac-hermes project-items",
            "mac-hermes beads-repositories",
            "hgmac agents identity %s" % agent_id,
            "git status --short --branch",
            test_command,
        ],
        "rules": [
            "Treat MAC tasks, projects, agents, reviews, and publications as first-class operational objects.",
            "Use Beads for issue tracking in registered project repositories that declare it.",
            "Use hgmac for agent CRUD and operational agent state, not ad hoc database edits.",
            "Use Firecrawl-backed web search when current external information is required.",
            "Commit, pull/rebase, push Beads, and push Git before reporting completed code work.",
        ],
    }


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
    workspace_path: Optional[Path] = None,
) -> Dict[str, Any]:
    resolved_mac_url = connection_url(mac_url) if mac_url else ""
    resolved_tenant_id = tenant_id or stable_id("tenant", fleet_name)
    resolved_persona_id = persona_id or stable_id("persona", agent_name)
    resolved_instance_id = hermes_instance_id or stable_id("hermes", agent_name)
    resolved_agent_id = agent_id or stable_id("agent", agent_name)
    resolved_workspace = workspace_path or Path(
        os.environ.get("MAC_HERMES_WORKSPACE")
        or os.environ.get("SRC_DIR")
        or (mac_home / "src" / "mac")
    )
    repository_contract = _repository_contract(resolved_workspace)
    session_capabilities = _session_capability_contract(
        workspace_path=resolved_workspace,
        mac_home=mac_home,
        mac_url=resolved_mac_url,
        hermes_instance_id=resolved_instance_id,
        agent_id=resolved_agent_id,
        repository_contract=repository_contract,
    )
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
            "MAC_HERMES_WORKSPACE": str(resolved_workspace),
            "MAC_PROJECT_CONTRACT_FILE": str(resolved_workspace / ".mac" / "project.yaml"),
        },
        "operations": {
            "refresh_context": [
                "mac hermes work-context %s" % resolved_instance_id,
                "mac-hermes work-context %s --active-only" % resolved_instance_id,
                "mac-hermes work-brief %s" % resolved_instance_id,
            ],
            "project_bridge": [
                "mac-hermes import-project-item <source> <external_id> <title>",
                "mac-hermes project-items",
                "mac-hermes beads-repositories",
                "mac-hermes register-beads-repository <name> <path> --project <project>",
                "mac-hermes poll-beads-repositories --repository <repository> --force --actor %s"
                % resolved_agent_id,
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
        "workspace": session_capabilities["workspace"],
        "session_capabilities": session_capabilities,
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
    session = context.get("session_capabilities") if isinstance(context.get("session_capabilities"), dict) else {}
    workspace = session.get("workspace") if isinstance(session.get("workspace"), dict) else {}
    project_contract = (
        workspace.get("project_contract")
        if isinstance(workspace.get("project_contract"), dict)
        else {}
    )
    capabilities = [
        item
        for item in (session.get("capabilities") or [])
        if isinstance(item, dict)
    ]
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
    lines.extend(["", "## Project Bridge", ""])
    for command in operations.get("project_bridge", []):
        lines.append("- `%s`" % command)
    lines.extend(["", "## Task Lifecycle", ""])
    for command in operations["create_task"] + operations["task_lifecycle"]:
        lines.append("- `%s`" % command)
    lines.extend(["", "## Direct Session Parity", ""])
    lines.append("- Workspace: `%s`" % (workspace.get("path") or context["environment"].get("MAC_HERMES_WORKSPACE") or "unconfigured"))
    lines.append("- Repository contract: `%s`" % (project_contract.get("path") or "unconfigured"))
    if project_contract.get("project"):
        lines.append("- Project: `%s`" % project_contract["project"])
    if project_contract.get("test_command"):
        lines.append("- Quality gate: `%s`" % project_contract["test_command"])
    lines.extend(["", "### Required Session Capabilities", ""])
    for capability in capabilities:
        command = capability.get("command") or capability.get("endpoint") or capability.get("name")
        lines.append(
            "- `%s`: %s (`%s`)"
            % (
                capability.get("name"),
                capability.get("purpose") or capability.get("kind") or "available to Hermes",
                command,
            )
        )
    workflow = [str(item) for item in (session.get("direct_session_workflow") or []) if str(item).strip()]
    if workflow:
        lines.extend(["", "### Direct Work Loop", ""])
        lines.extend("- `%s`" % command for command in workflow)
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
    workspace_path: Optional[Path] = None,
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
        workspace_path=workspace_path,
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
            "MAC_HERMES_WORKSPACE": context["environment"]["MAC_HERMES_WORKSPACE"],
            "MAC_PROJECT_CONTRACT_FILE": context["environment"]["MAC_PROJECT_CONTRACT_FILE"],
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
    parser.add_argument(
        "--workspace",
        default=os.environ.get("MAC_HERMES_WORKSPACE") or os.environ.get("SRC_DIR"),
    )
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
        workspace_path=Path(args.workspace).expanduser() if args.workspace else None,
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
