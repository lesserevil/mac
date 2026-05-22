from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


JsonDict = Dict[str, Any]
Transport = Callable[[str, str, Optional[JsonDict], Optional[str]], Any]


class HgMacError(RuntimeError):
    pass


class HgMacClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport or self._urllib_transport

    def request(self, method: str, path: str, body: Optional[JsonDict] = None) -> Any:
        return self._transport(method, self.base_url + path, body, self.token)

    def _urllib_transport(
        self,
        method: str,
        url: str,
        body: Optional[JsonDict],
        token: Optional[str],
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer %s" % token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HgMacError("HTTP %s %s: %s" % (exc.code, exc.reason, detail))
        except urllib.error.URLError as exc:
            raise HgMacError(str(exc.reason))
        return json.loads(payload) if payload else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hgmac", description="MAC hub agent operations CLI")
    parser.add_argument("--url", default=None, help="MAC API base URL")
    parser.add_argument("--token", default=None, help="MAC API bearer token")
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".config" / "hgmac" / "config.json"),
        help="JSON config with url/token defaults",
    )
    sub = parser.add_subparsers(dest="resource", required=True)

    agents = sub.add_parser("agents", help="Agent lifecycle and related operations")
    agent_sub = agents.add_subparsers(dest="action", required=True)

    _set(agent_sub.add_parser("list", help="List agents"), cmd_agents_list)

    show = agent_sub.add_parser("show", help="Show one agent")
    show.add_argument("agent_id")
    _set(show, cmd_agents_show)

    create = agent_sub.add_parser("create", help="Create or refresh an agent")
    create.add_argument("--machine-id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--agent-id")
    create.add_argument("--capability", action="append", default=[])
    create.add_argument("--capabilities", default="")
    create.add_argument("--resources-json", default="{}")
    create.add_argument("--hermes-instance-id")
    _set(create, cmd_agents_create)

    update = agent_sub.add_parser("update", help="Update agent fields")
    update.add_argument("agent_id")
    update.add_argument("--name")
    update.add_argument("--capability", action="append", default=None)
    update.add_argument("--capabilities")
    update.add_argument("--resources-json")
    update.add_argument("--status")
    update.add_argument("--health-status")
    update.add_argument("--hermes-instance-id")
    _set(update, cmd_agents_update)

    disable = agent_sub.add_parser("disable", help="Disable an agent")
    disable.add_argument("agent_id")
    _set(disable, cmd_agents_disable)

    delete = agent_sub.add_parser("delete", help="Delete an agent")
    delete.add_argument("agent_id")
    _set(delete, cmd_agents_delete)

    heartbeat = agent_sub.add_parser("heartbeat", help="Post agent heartbeat/status")
    heartbeat.add_argument("agent_id")
    heartbeat.add_argument("--status")
    heartbeat.add_argument("--health-status")
    heartbeat.add_argument("--resources-json")
    heartbeat.add_argument("--running-digest")
    _set(heartbeat, cmd_agents_heartbeat)

    claim = agent_sub.add_parser("claim-next", help="Claim or dry-run the next task")
    claim.add_argument("agent_id")
    claim.add_argument("--lease-seconds", type=int, default=900)
    claim.add_argument("--allowed-project", action="append", default=[])
    claim.add_argument("--required-metadata-json", default="{}")
    claim.add_argument("--require-canary", action="store_true")
    claim.add_argument("--dry-run", action="store_true")
    _set(claim, cmd_agents_claim_next)

    identity = agent_sub.add_parser("identity", help="Show composed soul/role/mood identity")
    identity.add_argument("agent_id")
    _set(identity, cmd_agents_identity)

    role = agent_sub.add_parser("role", help="Assign or unassign agent role")
    role_sub = role.add_subparsers(dest="role_action", required=True)
    role_assign = role_sub.add_parser("assign")
    role_assign.add_argument("agent_id")
    role_assign.add_argument("role_id_or_slug")
    _set(role_assign, cmd_agents_role_assign)
    role_unassign = role_sub.add_parser("unassign")
    role_unassign.add_argument("agent_id")
    _set(role_unassign, cmd_agents_role_unassign)

    mood = agent_sub.add_parser("mood", help="Agent mood overlay operations")
    mood_sub = mood.add_subparsers(dest="mood_action", required=True)
    mood_set = mood_sub.add_parser("set")
    mood_set.add_argument("agent_id")
    mood_set.add_argument("--mode", required=True)
    mood_set.add_argument("--set-by")
    mood_set.add_argument("--reason")
    mood_set.add_argument("--ttl-seconds", type=int)
    mood_set.add_argument("--metadata-json", default="{}")
    _set(mood_set, cmd_agents_mood_set)
    mood_show = mood_sub.add_parser("show")
    mood_show.add_argument("agent_id")
    _set(mood_show, cmd_agents_mood_show)
    mood_clear = mood_sub.add_parser("clear")
    mood_clear.add_argument("agent_id")
    mood_clear.add_argument("--cleared-by")
    mood_clear.add_argument("--reason")
    _set(mood_clear, cmd_agents_mood_clear)
    mood_history = mood_sub.add_parser("history")
    mood_history.add_argument("agent_id")
    mood_history.add_argument("--limit", type=int, default=50)
    _set(mood_history, cmd_agents_mood_history)

    nap = agent_sub.add_parser("nap", help="Agent nap lifecycle operations")
    nap_sub = nap.add_subparsers(dest="nap_action", required=True)
    nap_config = nap_sub.add_parser("configure")
    nap_config.add_argument("agent_id")
    nap_config.add_argument("--offset-minutes", type=int)
    nap_config.add_argument("--window-minutes", type=int, default=15)
    nap_config.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    nap_config.add_argument("--actor")
    _set(nap_config, cmd_agents_nap_configure)
    nap_show = nap_sub.add_parser("show")
    nap_show.add_argument("agent_id")
    _set(nap_show, cmd_agents_nap_show)
    nap_next = nap_sub.add_parser("next")
    nap_next.add_argument("agent_id")
    _set(nap_next, cmd_agents_nap_next)
    nap_begin = nap_sub.add_parser("begin")
    nap_begin.add_argument("agent_id")
    nap_begin.add_argument("--actor")
    nap_begin.add_argument("--detail-json", default="{}")
    _set(nap_begin, cmd_agents_nap_begin)
    nap_runs = nap_sub.add_parser("runs")
    nap_runs.add_argument("--agent-id")
    _set(nap_runs, cmd_agents_nap_runs)
    nap_complete = nap_sub.add_parser("complete")
    nap_complete.add_argument("run_id")
    nap_complete.add_argument("--summary-evidence-id")
    nap_complete.add_argument("--detail-json")
    nap_complete.add_argument("--actor")
    _set(nap_complete, cmd_agents_nap_complete)
    nap_fail = nap_sub.add_parser("fail")
    nap_fail.add_argument("run_id")
    nap_fail.add_argument("--reason", required=True)
    nap_fail.add_argument("--actor")
    _set(nap_fail, cmd_agents_nap_fail)

    audit = agent_sub.add_parser("command-audit", help="Agent command audit")
    audit_sub = audit.add_subparsers(dest="audit_action", required=True)
    audit_list = audit_sub.add_parser("list")
    audit_list.add_argument("--agent-id")
    audit_list.add_argument("--task-id")
    audit_list.add_argument("--command-id")
    audit_list.add_argument("--phase")
    audit_list.add_argument("--since")
    audit_list.add_argument("--until")
    audit_list.add_argument("--limit", type=int, default=200)
    _set(audit_list, cmd_agents_command_audit_list)
    audit_record = audit_sub.add_parser("record")
    audit_record.add_argument("agent_id")
    audit_record.add_argument("--phase", required=True)
    audit_record.add_argument("--argv-json", required=True)
    audit_record.add_argument("--cwd", required=True)
    audit_record.add_argument("--command-id")
    audit_record.add_argument("--task-id")
    audit_record.add_argument("--lease-id")
    audit_record.add_argument("--returncode", type=int)
    audit_record.add_argument("--metadata-json", default="{}")
    _set(audit_record, cmd_agents_command_audit_record)
    return parser


def _set(parser: argparse.ArgumentParser, func: Callable[[HgMacClient, argparse.Namespace], Any]) -> None:
    parser.set_defaults(func=func)


def cmd_agents_list(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents")


def cmd_agents_show(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents/%s" % quote(args.agent_id))


def cmd_agents_create(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents",
        {
            "machine_id": args.machine_id,
            "name": args.name,
            "agent_id": args.agent_id,
            "capabilities": _capabilities(args),
            "resources": _json_object(args.resources_json),
            "hermes_instance_id": args.hermes_instance_id,
        },
    )


def cmd_agents_update(client: HgMacClient, args: argparse.Namespace) -> Any:
    body: JsonDict = {}
    for key in ("name", "status", "health_status", "hermes_instance_id"):
        value = getattr(args, key)
        if value is not None:
            body[key] = value
    caps = _optional_capabilities(args)
    if caps is not None:
        body["capabilities"] = caps
    if args.resources_json is not None:
        body["resources"] = _json_object(args.resources_json)
    return client.request("PUT", "/agents/%s" % quote(args.agent_id), body)


def cmd_agents_disable(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("POST", "/agents/%s/disable" % quote(args.agent_id), {})


def cmd_agents_delete(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("DELETE", "/agents/%s" % quote(args.agent_id))


def cmd_agents_heartbeat(client: HgMacClient, args: argparse.Namespace) -> Any:
    body = _drop_none(
        {
            "status": args.status,
            "health_status": args.health_status,
            "resources": _json_object(args.resources_json) if args.resources_json else None,
            "running_digest": args.running_digest,
        }
    )
    return client.request("POST", "/agents/%s/heartbeat" % quote(args.agent_id), body)


def cmd_agents_claim_next(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents/%s/claim-next" % quote(args.agent_id),
        {
            "lease_seconds": args.lease_seconds,
            "allowed_projects": args.allowed_project,
            "required_metadata": _json_object(args.required_metadata_json),
            "require_canary": args.require_canary,
            "dry_run": args.dry_run,
        },
    )


def cmd_agents_identity(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents/%s/identity" % quote(args.agent_id))


def cmd_agents_role_assign(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents/%s/role" % quote(args.agent_id),
        {"role_id_or_slug": args.role_id_or_slug},
    )


def cmd_agents_role_unassign(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("DELETE", "/agents/%s/role" % quote(args.agent_id))


def cmd_agents_mood_set(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents/%s/mood" % quote(args.agent_id),
        _drop_none(
            {
                "mode": args.mode,
                "set_by": args.set_by,
                "reason": args.reason,
                "ttl_seconds": args.ttl_seconds,
                "metadata": _json_object(args.metadata_json),
            }
        ),
    )


def cmd_agents_mood_show(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents/%s/mood" % quote(args.agent_id))


def cmd_agents_mood_clear(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "DELETE",
        "/agents/%s/mood" % quote(args.agent_id),
        _drop_none({"cleared_by": args.cleared_by, "reason": args.reason}),
    )


def cmd_agents_mood_history(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents/%s/mood/history?limit=%d" % (quote(args.agent_id), args.limit))


def cmd_agents_nap_configure(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents/%s/nap-schedule" % quote(args.agent_id),
        _drop_none(
            {
                "offset_minutes": args.offset_minutes,
                "window_minutes": args.window_minutes,
                "enabled": args.enabled,
                "actor": args.actor,
            }
        ),
    )


def cmd_agents_nap_show(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents/%s/nap-schedule" % quote(args.agent_id))


def cmd_agents_nap_next(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request("GET", "/agents/%s/nap-schedule/next" % quote(args.agent_id))


def cmd_agents_nap_begin(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents/%s/nap-runs" % quote(args.agent_id),
        _drop_none({"actor": args.actor, "detail": _json_object(args.detail_json)}),
    )


def cmd_agents_nap_runs(client: HgMacClient, args: argparse.Namespace) -> Any:
    query = "?agent_id=%s" % quote(args.agent_id) if args.agent_id else ""
    return client.request("GET", "/nap-runs%s" % query)


def cmd_agents_nap_complete(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/nap-runs/%s/complete" % quote(args.run_id),
        _drop_none(
            {
                "summary_evidence_id": args.summary_evidence_id,
                "detail": _json_object(args.detail_json) if args.detail_json else None,
                "actor": args.actor,
            }
        ),
    )


def cmd_agents_nap_fail(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/nap-runs/%s/fail" % quote(args.run_id),
        _drop_none({"reason": args.reason, "actor": args.actor}),
    )


def cmd_agents_command_audit_list(client: HgMacClient, args: argparse.Namespace) -> Any:
    params = _query(
        {
            "agent_id": args.agent_id,
            "task_id": args.task_id,
            "command_id": args.command_id,
            "phase": args.phase,
            "since": args.since,
            "until": args.until,
            "limit": args.limit,
        }
    )
    return client.request("GET", "/command-audit%s" % params)


def cmd_agents_command_audit_record(client: HgMacClient, args: argparse.Namespace) -> Any:
    return client.request(
        "POST",
        "/agents/%s/command-audit" % quote(args.agent_id),
        _drop_none(
            {
                "command_id": args.command_id,
                "phase": args.phase,
                "argv": _json_list(args.argv_json),
                "cwd": args.cwd,
                "task_id": args.task_id,
                "lease_id": args.lease_id,
                "returncode": args.returncode,
                "metadata": _json_object(args.metadata_json),
            }
        ),
    )


def run(
    argv: Optional[List[str]] = None,
    *,
    transport: Optional[Transport] = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    config = _load_config(Path(args.config).expanduser())
    url = args.url or os.environ.get("HGMAC_URL") or os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL") or config.get("url")
    token = args.token or os.environ.get("HGMAC_TOKEN") or os.environ.get("MAC_API_TOKEN") or config.get("token")
    if not url:
        print("hgmac: --url, HGMAC_URL, MAC_URL, MAC_HUB_URL, or config url is required", file=err)
        return 2
    client = HgMacClient(str(url), token=str(token) if token else None, transport=transport)
    try:
        result = args.func(client, args)
    except HgMacError as exc:
        print("hgmac: %s" % exc, file=err)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True), file=out)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return run(argv)


def _load_config(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HgMacError("%s must contain a JSON object" % path)
    return data


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _query(values: JsonDict) -> str:
    filtered = {key: value for key, value in values.items() if value is not None}
    return "?" + urllib.parse.urlencode(filtered) if filtered else ""


def _json_object(value: str) -> JsonDict:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise HgMacError("expected a JSON object")
    return parsed


def _json_list(value: str) -> List[Any]:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list):
        raise HgMacError("expected a JSON list")
    return parsed


def _capabilities(args: argparse.Namespace) -> List[str]:
    values = list(args.capability or [])
    values.extend(_csv(args.capabilities))
    return sorted({item for item in values if item})


def _optional_capabilities(args: argparse.Namespace) -> Optional[List[str]]:
    if args.capability is None and args.capabilities is None:
        return None
    values = list(args.capability or [])
    values.extend(_csv(args.capabilities or ""))
    return sorted({item for item in values if item})


def _csv(value: str) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _drop_none(values: JsonDict) -> JsonDict:
    return {key: value for key, value in values.items() if value is not None}


if __name__ == "__main__":
    raise SystemExit(main())
