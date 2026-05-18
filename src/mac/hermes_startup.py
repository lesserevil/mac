from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


STATE_REF_CANDIDATES = (
    ("hermes_config", "config.yaml", True),
    ("soul", "SOUL.md", True),
    ("user_profile", "USER.md", True),
    ("long_term_memory", "MEMORY.md", True),
    ("memory_user_profile", "memories/USER.md", True),
    ("memory_long_term", "memories/MEMORY.md", True),
    ("conversation_state", "state.db", True),
    ("memory_store", "memory_store.db", True),
    ("kanban_state", "kanban.db", True),
    ("gateway_auth", "auth.json", True),
    ("gateway_env", ".env", True),
    ("slack_accounts", "slack_accounts.json", True),
    ("slack_channel_teams", "slack_channel_teams.json", True),
    ("slack_home_channels", "slack_home_channels.json", True),
)

ACC_STATE_REF_CANDIDATES = (
    ("acc_coding_sessions", "data/coding-sessions.json", True),
    ("acc_fleet_db", "data/fleet.db", True),
)

TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    return default


def _expand_path(value: Optional[str], default: str) -> Path:
    return Path(value or default).expanduser()


def _file_ref(path: Path, role: str, sensitive: bool) -> Dict[str, Any]:
    try:
        exists = path.exists()
    except OSError:
        exists = False
    ref: Dict[str, Any] = {
        "role": role,
        "path": str(path),
        "exists": exists,
        "sensitive": sensitive,
    }
    if exists:
        try:
            stat = path.stat()
            ref["kind"] = "dir" if path.is_dir() else "file"
            ref["size_bytes"] = stat.st_size
            ref["mtime_ns"] = stat.st_mtime_ns
        except OSError:
            ref["exists"] = False
    return ref


def _refs(root: Path, candidates: Iterable[tuple[str, str, bool]]) -> List[Dict[str, Any]]:
    return [
        _file_ref(root / relative_path, role, sensitive)
        for role, relative_path, sensitive in candidates
    ]


def _ref_exists(refs: Iterable[Dict[str, Any]], role: str) -> bool:
    return any(ref["role"] == role and ref["exists"] for ref in refs)


def _read_small_text(path: Path, limit: int = 262_144) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
        data = path.read_bytes()[:limit]
    except OSError:
        return ""
    return data.decode("utf-8", errors="ignore")


def _config_explicitly_enables_slack(config_path: Path) -> bool:
    text = _read_small_text(config_path)
    if not text:
        return False
    if re.search(r"(?mi)^\s*SLACK_BOT_TOKEN\s*[:=]", text):
        return True
    slack_block = re.search(r"(?ms)^\s*(slack|platforms)\s*:.*?^\S", text + "\nend:", re.M)
    if slack_block and re.search(r"(?mi)^\s*enabled\s*:\s*true\s*$", slack_block.group(0)):
        return True
    return bool(re.search(r"(?mi)^\s*slack\s*:\s*\{\s*enabled\s*:\s*true", text))


def _bool_value(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    value = raw.strip().strip("\"'").lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    return None


def _env_file_redaction_ref(path: Path, role: str) -> Dict[str, Any]:
    ref = _file_ref(path, role, True)
    ref["redact_secrets"] = "unset"
    ref["redact_secrets_disabled"] = False
    text = _read_small_text(path)
    if not text:
        return ref
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "HERMES_REDACT_SECRETS":
            continue
        parsed = _bool_value(value)
        if parsed is None:
            ref["redact_secrets"] = "invalid"
        else:
            ref["redact_secrets"] = "true" if parsed else "false"
            ref["redact_secrets_disabled"] = not parsed
        break
    return ref


def _config_redaction_value(config_path: Path) -> str:
    text = _read_small_text(config_path)
    if not text:
        return "unset"
    match = re.search(r"(?mi)^\s*redact_secrets\s*:\s*(true|false|yes|no|on|off|1|0)\s*$", text)
    if not match:
        return "unset"
    parsed = _bool_value(match.group(1))
    if parsed is None:
        return "invalid"
    return "true" if parsed else "false"


def _secret_redaction_report(hermes_home: Path, acc_dir: Path) -> Dict[str, Any]:
    env_value = _bool_value(os.environ.get("HERMES_REDACT_SECRETS"))
    config_value = _config_redaction_value(hermes_home / "config.yaml")
    env_files = [
        _env_file_redaction_ref(hermes_home / ".env", "hermes_env"),
        _env_file_redaction_ref(acc_dir / ".env", "acc_env"),
    ]
    disabled_files = [
        ref["role"] for ref in env_files if ref.get("redact_secrets_disabled")
    ]

    if env_value is not None:
        effective = env_value
        source = "environment"
    elif config_value in {"true", "false"}:
        effective = config_value == "true"
        source = "config"
    else:
        effective = True
        source = "default"

    warnings = []
    if env_value is False:
        warnings.append("Hermes secret redaction is disabled by HERMES_REDACT_SECRETS")
    if config_value == "false":
        warnings.append("Hermes config disables secret redaction")
    if disabled_files:
        warnings.append(
            "Inherited Hermes environment files disable secret redaction: %s"
            % ", ".join(disabled_files)
        )

    return {
        "effective": effective,
        "source": source,
        "environment": (
            "unset"
            if os.environ.get("HERMES_REDACT_SECRETS") is None
            else ("true" if env_value is True else "false" if env_value is False else "invalid")
        ),
        "config": config_value,
        "env_files": env_files,
        "drift_detected": bool(warnings),
        "warnings": warnings,
    }


def _log_classification_report() -> Dict[str, Any]:
    default_path = Path("~/.mac/logs/hermes-log-summary.json").expanduser()
    path = _expand_path(os.environ.get("MAC_HERMES_LOG_SUMMARY"), str(default_path))
    report: Dict[str, Any] = {
        "summary": _file_ref(path, "hermes_log_summary", False),
        "known_benign_classes": [
            "controlled_restart",
            "slack_file_public_unhandled",
        ],
        "actionable_count": 0,
        "benign_count": 0,
        "classes": [],
        "warnings": [],
    }
    try:
        import json

        if path.exists() and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            classes = data.get("classes") if isinstance(data, dict) else []
            if isinstance(classes, list):
                report["classes"] = classes
            report["actionable_count"] = int(data.get("actionable_count", 0))
            report["benign_count"] = int(data.get("benign_count", 0))
    except Exception as exc:
        report["warnings"].append("could not read Hermes log summary: %s" % exc)
    if report["actionable_count"]:
        report["warnings"].append(
            "Hermes gateway logs contain actionable classified warnings"
        )
    return report


def _hermes_agent_dir_info() -> tuple[Optional[Path], bool]:
    configured = os.environ.get("MAC_HERMES_AGENT_DIR") or os.environ.get("HERMES_AGENT_DIR")
    if configured:
        return Path(configured).expanduser(), True
    default = Path("~/Src/hermes-agent").expanduser()
    return (default, False) if default.exists() else (None, False)


def _slack_account_shim_present(agent_dir: Optional[Path]) -> bool:
    if agent_dir is None:
        return False
    config_py = agent_dir / "gateway" / "config.py"
    text = _read_small_text(config_py)
    return "_slack_accounts_file_configured" in text and "slack_accounts.json" in text


def _apply_slack_account_activation_shim(agent_dir: Path) -> Dict[str, Any]:
    config_py = agent_dir / "gateway" / "config.py"
    result = {
        "attempted": True,
        "applied": False,
        "path": str(config_py),
        "error": "",
    }
    try:
        text = config_py.read_text(encoding="utf-8")
    except OSError as exc:
        result["error"] = "cannot read Hermes gateway/config.py: %s" % exc
        return result

    changed = False
    helper = '''\

def _slack_accounts_file_configured() -> bool:
    """Return True when Hermes has at least one complete Slack account file."""
    import json

    accounts_file = get_hermes_home() / "slack_accounts.json"
    try:
        data = json.loads(accounts_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, list):
        return False
    for item in data:
        if (
            isinstance(item, dict)
            and str(item.get("bot_token") or "").strip()
            and str(item.get("app_token") or "").strip()
        ):
            return True
    return False
'''
    if "_slack_accounts_file_configured" not in text:
        helper_needle = "\n\n# -----------------------------------------------------------------------------\n# Built-in platform connection checkers\n"
        if helper_needle not in text:
            result["error"] = (
                "cannot patch Slack account-file detection; upstream gateway/config.py changed"
            )
            return result
        text = text.replace(helper_needle, helper + helper_needle, 1)
        changed = True

    checker_needle = "_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {\n"
    checker_patch = checker_needle + "    Platform.SLACK: lambda cfg: _slack_accounts_file_configured(),\n"
    if "Platform.SLACK: lambda cfg: _slack_accounts_file_configured()" not in text:
        if checker_needle not in text:
            result["error"] = "cannot patch Slack connected checker; upstream gateway/config.py changed"
            return result
        text = text.replace(checker_needle, checker_patch, 1)
        changed = True

    old = '''\
    # Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    if slack_token:
        if Platform.SLACK not in config.platforms:
            # No yaml config for Slack — env-only setup, enable it
            config.platforms[Platform.SLACK] = PlatformConfig()
            config.platforms[Platform.SLACK].enabled = True
        else:
            slack_config = config.platforms[Platform.SLACK]
            enabled_was_explicit = bool(slack_config.extra.pop("_enabled_explicit", False))
            if not slack_config.enabled and not enabled_was_explicit:
                # Top-level Slack settings such as channel prompts should not
                # turn an env-token setup into a disabled platform. Only an
                # explicit slack.enabled/platforms.slack.enabled false should.
                slack_config.enabled = True
        # If yaml config exists, respect its enabled flag (don't override
        # explicit enabled: false). Token is still stored so skills that
        # send Slack messages can use it without activating the gateway adapter.
        config.platforms[Platform.SLACK].token = slack_token
'''
    new = '''\
    # Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack_accounts_configured = _slack_accounts_file_configured()
    if slack_token or slack_accounts_configured:
        if Platform.SLACK not in config.platforms:
            # No yaml config for Slack — env-only or slack_accounts.json setup, enable it.
            config.platforms[Platform.SLACK] = PlatformConfig()
            config.platforms[Platform.SLACK].enabled = True
        else:
            slack_config = config.platforms[Platform.SLACK]
            enabled_was_explicit = bool(slack_config.extra.pop("_enabled_explicit", False))
            if not slack_config.enabled and not enabled_was_explicit:
                # Top-level Slack settings such as channel prompts should not
                # turn an env-token/account-file setup into a disabled platform.
                # Only an explicit slack.enabled/platforms.slack.enabled false should.
                slack_config.enabled = True
        # If yaml config exists, respect its enabled flag (don't override
        # explicit enabled: false). Token is still stored so skills that
        # send Slack messages can use it without activating the gateway adapter.
        if slack_token:
            config.platforms[Platform.SLACK].token = slack_token
'''
    if "slack_accounts_configured = _slack_accounts_file_configured()" not in text:
        if old not in text:
            result["error"] = "cannot patch Slack env override; upstream gateway/config.py changed"
            return result
        text = text.replace(old, new, 1)
        changed = True

    if changed:
        try:
            config_py.write_text(text, encoding="utf-8")
        except OSError as exc:
            result["error"] = "cannot write Hermes gateway/config.py: %s" % exc
            return result
    result["applied"] = changed
    return result


def _maybe_apply_slack_account_activation_shim(
    agent_dir: Optional[Path],
    explicit_agent_dir: bool,
    account_file_present: bool,
    env_token_present: bool,
    explicit_config: bool,
    shim_present: bool,
) -> Dict[str, Any]:
    result = {
        "enabled": explicit_agent_dir and _env_enabled("MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM", True),
        "attempted": False,
        "applied": False,
        "path": str(agent_dir / "gateway" / "config.py") if agent_dir is not None else None,
        "error": "",
    }
    if (
        not result["enabled"]
        or agent_dir is None
        or not account_file_present
        or shim_present
    ):
        return result
    return {
        "enabled": True,
        **_apply_slack_account_activation_shim(agent_dir),
    }


def _slack_activation_report(
    hermes_home: Path,
    hermes_refs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    account_ref = next(
        ref for ref in hermes_refs if ref["role"] == "slack_accounts"
    )
    account_file_present = bool(account_ref["exists"])
    env_token_present = bool(os.environ.get("SLACK_BOT_TOKEN"))
    explicit_config = _config_explicitly_enables_slack(hermes_home / "config.yaml")
    agent_dir, explicit_agent_dir = _hermes_agent_dir_info()
    shim_present = _slack_account_shim_present(agent_dir)
    shim_patch = _maybe_apply_slack_account_activation_shim(
        agent_dir,
        explicit_agent_dir,
        account_file_present,
        env_token_present,
        explicit_config,
        shim_present,
    )
    if shim_patch["applied"]:
        shim_present = _slack_account_shim_present(agent_dir)

    activation_source = "not_configured"
    can_activate = False
    needs_shim = False
    warning = ""
    if account_file_present:
        if env_token_present:
            activation_source = "slack_bot_token"
            can_activate = True
        elif explicit_config:
            activation_source = "explicit_config"
            can_activate = True
        elif shim_present:
            activation_source = "slack_accounts_file_shim"
            can_activate = True
        else:
            activation_source = "missing_account_file_activation"
            needs_shim = True
            if shim_patch["error"]:
                warning = (
                    "slack_accounts.json exists, but mac could not apply the upstream "
                    "Hermes account-file activation shim: %s" % shim_patch["error"]
                )
            else:
                warning = (
                    "slack_accounts.json exists, but upstream Hermes will not enable Slack "
                    "from that file unless SLACK_BOT_TOKEN, explicit Slack config, or the "
                    "account-file activation shim is present"
                )

    return {
        "account_file": account_ref,
        "account_file_present": account_file_present,
        "slack_bot_token_present": env_token_present,
        "explicit_config_present": explicit_config,
        "hermes_agent_dir": str(agent_dir) if agent_dir is not None else None,
        "hermes_agent_dir_explicit": explicit_agent_dir,
        "account_file_activation_shim_present": shim_present,
        "account_file_activation_shim_patch": shim_patch,
        "needs_account_file_activation_shim": needs_shim,
        "activation_source": activation_source,
        "can_activate": can_activate,
        "warning": warning,
    }


def build_hermes_startup_report() -> Dict[str, Any]:
    """Return a redacted startup report for Hermes-owned durable state.

    The report deliberately contains only file metadata and boolean activation
    facts. It never includes file contents from Hermes state, auth, memory, or
    Slack account files.
    """

    enabled = _env_enabled("MAC_HERMES_STARTUP_CHECK", True)
    if not enabled:
        return {
            "enabled": False,
            "ready": True,
            "warnings": [],
            "state_refs": [],
            "slack": {"activation_source": "startup_check_disabled"},
            "security": {
                "secret_redaction": {"effective": True, "source": "startup_check_disabled"}
            },
            "logs": {"classes": [], "actionable_count": 0, "benign_count": 0},
            "operator_health": {"status": "healthy"},
        }

    hermes_home = _expand_path(os.environ.get("HERMES_HOME"), "~/.hermes")
    acc_dir = _expand_path(os.environ.get("ACC_DIR"), "~/.acc")
    hermes_refs = _refs(hermes_home, STATE_REF_CANDIDATES)
    acc_refs = _refs(acc_dir, ACC_STATE_REF_CANDIDATES)
    state_refs = hermes_refs + acc_refs
    slack = _slack_activation_report(hermes_home, hermes_refs)
    secret_redaction = _secret_redaction_report(hermes_home, acc_dir)
    logs = _log_classification_report()

    warnings: List[str] = []
    if not hermes_home.exists():
        warnings.append("Hermes home does not exist: %s" % hermes_home)
    if not _ref_exists(hermes_refs, "hermes_config"):
        warnings.append("Hermes config.yaml is missing")
    if not _ref_exists(hermes_refs, "soul"):
        warnings.append("Hermes SOUL.md is missing")
    if not (_ref_exists(hermes_refs, "long_term_memory") or _ref_exists(hermes_refs, "memory_long_term")):
        warnings.append("Hermes MEMORY.md is missing")
    if not _ref_exists(hermes_refs, "conversation_state"):
        warnings.append("Hermes state.db is missing")
    if slack["warning"]:
        warnings.append(slack["warning"])
    warnings.extend(secret_redaction["warnings"])
    warnings.extend(logs["warnings"])

    checks = {
        "hermes_home_exists": hermes_home.exists(),
        "config_present": _ref_exists(hermes_refs, "hermes_config"),
        "soul_present": _ref_exists(hermes_refs, "soul"),
        "long_term_memory_present": _ref_exists(hermes_refs, "long_term_memory")
        or _ref_exists(hermes_refs, "memory_long_term"),
        "conversation_state_present": _ref_exists(hermes_refs, "conversation_state"),
        "slack_activates_if_configured": (
            not slack["account_file_present"] or bool(slack["can_activate"])
        ),
        "secret_redaction_enabled": bool(secret_redaction["effective"])
        and not secret_redaction["drift_detected"],
        "logs_have_no_actionable_classes": not bool(logs["actionable_count"]),
    }
    state_refs_existing = sum(1 for ref in state_refs if ref["exists"])
    operator_status = "healthy" if not warnings else "degraded"

    return {
        "enabled": True,
        "ready": not warnings,
        "hermes_home": str(hermes_home),
        "acc_dir": str(acc_dir),
        "checks": checks,
        "warnings": warnings,
        "state_refs": state_refs,
        "slack": slack,
        "security": {"secret_redaction": secret_redaction},
        "logs": logs,
        "operator_health": {
            "status": operator_status,
            "state_refs_existing": state_refs_existing,
            "slack_activation_source": slack["activation_source"],
            "secret_redaction_effective": secret_redaction["effective"],
            "log_actionable_count": logs["actionable_count"],
        },
    }
