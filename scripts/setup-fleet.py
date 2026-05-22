#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except Exception:  # noqa: BLE001 - deploy will surface PyYAML requirement too.
    yaml = None


ROOT = Path(__file__).resolve().parents[1]


def prompt(
    label: str,
    *,
    default: str = "",
    required: bool = False,
    choices: List[str] | None = None,
) -> str:
    suffix = ""
    if choices:
        suffix += " [%s]" % "/".join(choices)
    if default:
        suffix += " [%s]" % default
    while True:
        value = input("%s%s: " % (label, suffix)).strip()
        if not value and default:
            value = default
        if not value and required:
            print("Required.")
            continue
        if choices and value and value not in choices:
            print("Choose one of: %s" % ", ".join(choices))
            continue
        return value


def prompt_bool(label: str, *, default: bool) -> bool:
    value = prompt(label, default="y" if default else "n", choices=["y", "n"])
    return value == "y"


def host_from_target(target: str) -> str:
    host = target.rsplit("@", 1)[-1].strip()
    return host or "127.0.0.1"


def qdrant_url_from_hub(hub_url: str) -> str:
    if hub_url.startswith("http://") or hub_url.startswith("https://"):
        scheme, rest = hub_url.split("://", 1)
        host = rest.split("/", 1)[0].rsplit(":", 1)[0]
        return "%s://%s:6333" % (scheme, host)
    return ""


def yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    return json.dumps(text)


def write_yaml_lines(value: Any, indent: int = 0) -> List[str]:
    prefix = " " * indent
    lines: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append("%s%s:" % (prefix, key))
                lines.extend(write_yaml_lines(item, indent + 2))
            else:
                lines.append("%s%s: %s" % (prefix, key, yaml_scalar(item)))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lines.append("%s-" % prefix)
                lines.extend(write_yaml_lines(item, indent + 2))
            elif isinstance(item, list):
                lines.append("%s-" % prefix)
                lines.extend(write_yaml_lines(item, indent + 2))
            else:
                lines.append("%s- %s" % (prefix, yaml_scalar(item)))
    else:
        lines.append("%s%s" % (prefix, yaml_scalar(value)))
    return lines


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name("%s.backup-%s" % (path.name, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
    shutil.copy2(path, backup)
    return backup


def load_fleet_registry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "fleets": {}}
    if yaml is None:
        raise RuntimeError("PyYAML is required to update an existing fleet registry")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {"version": 1, "fleets": {}}
    if not isinstance(data, dict):
        raise RuntimeError("%s must contain a YAML mapping" % path)
    if isinstance(data.get("fleets"), dict):
        data.setdefault("version", 1)
        return data
    if isinstance(data.get("fleets"), list):
        fleets: Dict[str, Any] = {}
        for item in data["fleets"]:
            if not isinstance(item, dict) or not str(item.get("hub_agent") or "").strip():
                raise RuntimeError("each fleet in %s must have hub_agent" % path)
            fleets[str(item["hub_agent"]).strip()] = item
        data["fleets"] = fleets
        data.setdefault("version", 1)
        return data
    if data.get("hub_agent") and data.get("agents"):
        hub = str(data["hub_agent"]).strip()
        return {"version": 1, "fleets": {hub: data}}
    data.setdefault("version", 1)
    data["fleets"] = {}
    return data


def build_agent(
    *,
    name: str,
    target: str,
    os_kind: str,
    model: str,
    supervisor: str,
    mode: str,
    require_canary: bool,
    control_bind_host: str = "",
) -> Dict[str, Any]:
    agent: Dict[str, Any] = {
        "name": name,
        "enabled": True,
        "target": target,
        "os": os_kind,
        "supervisor": supervisor,
    }
    if control_bind_host:
        agent["control_bind_host"] = control_bind_host
    agent["hermes"] = {"gateway_model": model}
    agent["worker"] = {"mode": mode, "require_canary": require_canary}
    return agent


def env_line(key: str, value: str) -> str:
    return "%s=%s" % (key, shlex.quote(value))


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Interactive first-run mac fleet setup wizard.")
    parser.add_argument(
        "--fleets-config",
        default=str(Path.home() / ".mac" / "fleets.yaml"),
        help="Path to the home-scoped multi-fleet registry.",
    )
    parser.add_argument(
        "--env-file",
        default=str(Path.home() / ".mac" / ".env"),
        help="Path to write caller-machine deploy env/secrets.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files after backing them up.")
    parser.add_argument("--dry-run", action="store_true", help="Print generated files without writing them.")
    args = parser.parse_args(argv)

    fleets_config = Path(args.fleets_config).expanduser()
    env_file = Path(args.env_file).expanduser()

    if not args.force:
        for path in (fleets_config, env_file):
            if path.exists() and not prompt_bool("Overwrite %s after making a backup?" % path, default=False):
                print("Aborted before writing %s" % path)
                return 2

    print("mac fleet setup wizard")
    print("Do not paste provider API keys here. Put upstream model/provider keys in TokenHub.")
    fleet_name = prompt("Fleet name", default="my-fleet")
    hub_name = prompt("Hub node name", required=True)
    hub_target = prompt("Hub SSH target (user@host or host)", required=True)
    hub_os = prompt("Hub OS", default="linux", choices=["linux", "darwin"])
    hub_url = prompt(
        "Hub URL agents should use",
        default="http://%s:8789" % host_from_target(hub_target),
        required=True,
    )
    supervisor = prompt("Default supervisor", default="auto", choices=["auto", "systemd", "launchd", "supervisord"])
    home_channel = prompt("Slack home channel name without # (blank to skip)", default="")
    hub_model = prompt("Hub Hermes model selector (blank to configure later)", default="")
    hub_worker_mode = prompt("Hub worker mode", default="loop", choices=["heartbeat", "dry-run", "loop"])
    hub_require_canary = prompt_bool("Require canary metadata on hub tasks?", default=False)
    qdrant_required = prompt_bool("Require shared Qdrant memory readiness?", default=True)
    qdrant_url = prompt(
        "Shared Qdrant URL",
        default=qdrant_url_from_hub(hub_url) if qdrant_required else "",
    )
    qdrant_bind_addr = prompt("Hub Qdrant bind address override (blank for Tailscale/loopback auto)", default="")

    print("")
    print("Fleet mesh networking connects agents across networks without manual VPN config.")
    print("headscale is self-hosted — no Tailscale account or external auth key needed.")
    use_headscale = prompt_bool("Enable headscale fleet mesh networking?", default=True)
    tailscale_install = "auto"
    tailscale_headscale = "auto"
    tailscale_headscale_port = "8080"
    tailscale_headscale_public_addr = ""
    tailscale_hostname_prefix = ""
    tailscale_auth_key = ""
    if use_headscale:
        tailscale_headscale = "yes"
        tailscale_headscale_port = prompt("Headscale port (workers must reach hub on this port)", default="8080")
        # Derive hub public addr for headscale server_url from SSH target
        hub_host = hub_target.rsplit("@", 1)[-1].strip() or hub_target
        tailscale_headscale_public_addr = prompt(
            "Hub's publicly routable address for headscale",
            default=hub_host,
            required=True,
        )
        # Hub gets first IP in headscale's default prefix (100.64.0.1)
        ts_hub_ip = "100.64.0.1"
        ts_hub_url = "http://%s:8789" % ts_hub_ip
        if prompt_bool("Set hub URL to headscale IP %s?" % ts_hub_url, default=True):
            hub_url = ts_hub_url
            ts_qdrant_url = "http://%s:6333" % ts_hub_ip
            if prompt_bool("Set Qdrant URL to headscale IP %s?" % ts_qdrant_url, default=True):
                qdrant_url = ts_qdrant_url
        tailscale_hostname_prefix = prompt(
            "Tailscale hostname prefix for fleet agents (blank for none)",
            default="",
        )
    else:
        # Cloud Tailscale fallback — requires an auth key
        tailscale_headscale = "no"
        tailscale_auth_key = prompt("Tailscale cloud auth key (blank to skip mesh networking)", default="")
        if tailscale_auth_key:
            tailscale_hostname_prefix = prompt(
                "Tailscale hostname prefix for fleet agents (blank for none)",
                default="",
            )
            ts_hub_name = "%s%s" % (tailscale_hostname_prefix, hub_name)
            if prompt_bool(
                "Set hub URL to Tailscale MagicDNS name http://%s:8789?" % ts_hub_name,
                default=True,
            ):
                hub_url = "http://%s:8789" % ts_hub_name
                if prompt_bool(
                    "Set Qdrant URL to http://%s:6333?" % ts_hub_name,
                    default=True,
                ):
                    qdrant_url = "http://%s:6333" % ts_hub_name

    agents = [
        build_agent(
            name=hub_name,
            target=hub_target,
            os_kind=hub_os,
            model=hub_model,
            supervisor=supervisor,
            mode=hub_worker_mode,
            require_canary=hub_require_canary,
            control_bind_host="0.0.0.0",
        )
    ]
    while prompt_bool("Add another agent?", default=False):
        name = prompt("Agent name", required=True)
        target = prompt("Agent SSH target", required=True)
        os_kind = prompt("Agent OS", default="linux", choices=["linux", "darwin"])
        model = prompt("Agent Hermes model selector (blank to configure later)", default="")
        agent_supervisor = prompt("Agent supervisor", default=supervisor, choices=["auto", "systemd", "launchd", "supervisord"])
        mode = prompt("Agent worker mode", default="loop", choices=["heartbeat", "dry-run", "loop"])
        require_canary = prompt_bool("Require canary metadata on this agent?", default=False)
        agents.append(
            build_agent(
                name=name,
                target=target,
                os_kind=os_kind,
                model=model,
                supervisor=agent_supervisor,
                mode=mode,
                require_canary=require_canary,
            )
        )

    fleet_config: Dict[str, Any] = {
        "sample": False,
        "fleet_name": fleet_name,
        "hub_agent": hub_name,
        "hub_url": hub_url,
        "shared_services_manager_agent": hub_name,
        "defaults": {
            "supervisor": supervisor,
            "hermes": {
                "slack_home_channel_name": home_channel,
                "gateway_provider": "custom",
                "gateway_base_url": "",
            },
            "worker": {
                "mode": "heartbeat",
                "capabilities": ["ops", "python", "hermes", "review"],
                "allowed_projects": "",
                "required_metadata": "",
                "require_canary": True,
            },
            "qdrant": {
                "install": "auto",
                "required": qdrant_required,
                "url": qdrant_url,
                "bind_addr": qdrant_bind_addr,
                "port": 6333,
                "image": "docker.io/qdrant/qdrant:latest",
                "memory_limit": "2g",
            },
            "tailscale": {
                "install": tailscale_install,
                "headscale": tailscale_headscale,
                "headscale_port": int(tailscale_headscale_port) if tailscale_headscale_port.isdigit() else 8080,
                "headscale_public_addr": tailscale_headscale_public_addr,
                "hostname_prefix": tailscale_hostname_prefix,
            },
        },
        "agents": agents,
    }

    env_values = {
        "MAC_DEPLOY_FLEET_CONFIG": str(ROOT / "deploy" / "fleet" / "config.yaml"),
        "MAC_DEPLOY_FLEETS_CONFIG": str(fleets_config),
        "MAC_DEPLOY_HUB_AGENT": hub_name,
        "MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT": hub_name,
    }
    if prompt_bool("Generate MAC_SECRET_KEY in %s?" % env_file, default=True):
        env_values["MAC_SECRET_KEY"] = secrets.token_urlsafe(48)
    if prompt_bool("Generate MAC_API_TOKEN in %s?" % env_file, default=True):
        env_values["MAC_API_TOKEN"] = secrets.token_urlsafe(32)
    hub_token = prompt("Existing hub token for spoke deploys (blank to read it from hub during deploy)", default="")
    if hub_token:
        env_values["MAC_DEPLOY_HUB_TOKEN"] = hub_token
    if tailscale_auth_key:
        env_values["MAC_DEPLOY_TAILSCALE_AUTH_KEY"] = tailscale_auth_key

    registry = load_fleet_registry(fleets_config)
    fleets = registry.get("fleets")
    if not isinstance(fleets, dict):
        fleets = {}
        registry["fleets"] = fleets
    fleets[hub_name] = fleet_config
    registry["version"] = registry.get("version") or 1
    config_content = "\n".join(write_yaml_lines(registry)) + "\n"
    env_content = "\n".join(
        [
            "# Generated by scripts/setup-fleet.py.",
            "# Contains local deploy secrets; keep mode 0600.",
            *[env_line(key, value) for key, value in env_values.items()],
            "",
        ]
    )

    if args.dry_run:
        print("--- %s" % fleets_config)
        print(config_content, end="")
        print("--- %s" % env_file)
        print(env_content, end="")
        return 0

    fleets_backup = backup_existing(fleets_config)
    env_backup = backup_existing(env_file)
    atomic_write(fleets_config, config_content, 0o600)
    atomic_write(env_file, env_content, 0o600)

    if fleets_backup:
        print("Backed up previous fleet registry to %s" % fleets_backup)
    if env_backup:
        print("Backed up previous env file to %s" % env_backup)
    print("Wrote %s" % fleets_config)
    print("Wrote %s" % env_file)
    print("")
    print("Next:")
    print("  set -a; . %s; set +a" % env_file)
    print("  bash deploy/deploy-mac-fleet.sh --hub %s" % hub_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
