"""Regression tests for mac-33z: worker bearer tokens must not appear in
deployment log output.

Two complementary guarantees are asserted by reading the deploy script as
text (matching the style of ``test_deploy_agent_configs.py`` and
``test_deploy_fleet_drain.py`` -- this script is a giant heredoc generator
and there is no Python module to import from it):

1. The Linux ``mac-agent-service`` wrapper script that systemd execs does
   NOT pass ``--token "$MAC_WORKER_TOKEN"`` (or any literal token value) as
   an argv argument. The wrapper instead exports ``MAC_TOKEN`` so the
   ``mac-agent`` CLI can pick it up from the environment, where it stays
   out of ``/proc/<pid>/cmdline`` and out of ``systemctl status`` output.

2. The deploy script no longer invokes ``systemctl status <mac unit>`` for
   any of the managed units. ``systemctl status`` prints the full
   ``ExecStart`` command line and renders the cgroup process tree (which
   includes each child's argv) -- both vectors leak any token that ever
   ends up on a command line. The replacement is the
   ``print_redacted_unit_summary`` helper, which pulls a curated list of
   ``systemctl show`` properties and runs them through a credential-scrubber.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _script_text() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def _extract_mac_agent_wrapper(text: str) -> str:
    """Pull the body of the ``mac-agent-service`` wrapper heredoc out of the
    deploy script. The wrapper is what systemd actually execs, so it is the
    surface that must not put the token on a command line.
    """

    match = re.search(
        r'cat > "\$wrapper" <<\'EOF\'\n(?P<body>.*?\n)EOF',
        text,
        flags=re.DOTALL,
    )
    # There are multiple ``$wrapper`` heredocs in the deploy script (one per
    # service). We want the agent wrapper specifically; identify it by the
    # ``MAC_WORKER_TOKEN`` precondition that only the agent wrapper enforces.
    for body in re.findall(
        r'cat > "\$wrapper" <<\'EOF\'\n(.*?\n)EOF',
        text,
        flags=re.DOTALL,
    ):
        if "MAC_WORKER_TOKEN" in body and "mac-agent" in body:
            return body
    assert match is not None, "could not locate any wrapper heredoc"
    raise AssertionError(
        "could not locate the mac-agent-service wrapper heredoc in deploy-mac-fleet.sh"
    )


def test_mac_agent_wrapper_does_not_pass_token_on_argv():
    """The wrapper must NEVER assemble ``--token <secret>`` as an argv
    argument. If this regresses, the secret becomes visible to
    ``systemctl status``, ``ps``, ``/proc/<pid>/cmdline``, and any journald
    consumer that scrapes cmdline fields.
    """

    wrapper_body = _extract_mac_agent_wrapper(_script_text())

    # Hard ban: the literal CLI flag plus the env var as the next token.
    assert "--token \"$MAC_WORKER_TOKEN\"" not in wrapper_body, (
        "mac-agent wrapper still passes --token on the command line; "
        "this leaks the bearer token via systemctl status / ps."
    )
    # Belt-and-suspenders: even if someone reworks quoting, neither
    # MAC_WORKER_TOKEN nor MAC_TOKEN should appear immediately after --token.
    assert not re.search(r"--token\s+\"?\$\{?MAC_(WORKER_)?TOKEN", wrapper_body), (
        "mac-agent wrapper passes a token-shaped variable on argv after "
        "--token; route it through the environment instead."
    )


def test_mac_agent_wrapper_exports_token_via_environment():
    """The wrapper must place the token in the environment under the name
    that ``mac-agent``'s argparse default already understands (``MAC_TOKEN``).
    """

    wrapper_body = _extract_mac_agent_wrapper(_script_text())
    assert re.search(r"export\s+MAC_TOKEN=\"\$MAC_WORKER_TOKEN\"", wrapper_body), (
        "mac-agent wrapper must export MAC_TOKEN so the worker can pick the "
        "bearer up via the environment instead of argv."
    )


def test_deploy_script_never_runs_raw_systemctl_status_for_mac_units():
    """``systemctl status <unit>`` is forbidden for the managed mac units:
    it prints ExecStart verbatim and renders the cgroup process tree, both
    of which expose any argv-resident credential.
    """

    text = _script_text()
    # Strip comments first so the regex below isn't tripped up by the
    # explanatory documentation on print_redacted_unit_summary.
    code_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    forbidden = re.compile(
        r"systemctl\s+(?:[^\n]*\s)?status\s+"
        r"(mac\.service|mac-agent\.service|mac-hermes-gateway\.service)"
    )
    matches = forbidden.findall(code)
    assert not matches, (
        "deploy-mac-fleet.sh still calls `systemctl status` on managed "
        "units (%s); use print_redacted_unit_summary instead." % matches
    )


def test_deploy_script_defines_and_uses_redacted_unit_summary():
    """Make sure the replacement helper exists AND is actually used by the
    install_*_service functions. Catches accidental reverts where the helper
    survives but the call sites flip back to systemctl status.
    """

    text = _script_text()
    assert "print_redacted_unit_summary()" in text, (
        "print_redacted_unit_summary helper must be defined"
    )
    for call in (
        "print_redacted_unit_summary mac.service",
        "print_redacted_unit_summary mac-hermes-gateway.service",
        "print_redacted_unit_summary mac-agent.service",
    ):
        assert call in text, (
            "deploy script must invoke `%s` after restarting that unit" % call
        )


def test_redacted_unit_summary_uses_systemctl_show_with_allow_list():
    """``systemctl show`` is fine -- it prints key=value properties and we
    control which properties we ask for. ``systemctl status`` is NOT. Make
    sure the helper actually goes through ``show -p`` and asks for a small,
    review-able allow-list that does NOT include ExecStart, Environment, or
    other argv-bearing fields.
    """

    text = _script_text()
    assert re.search(
        r"sudo\s+systemctl\s+show\s+\"\$unit\"[^\"]*-p\s+ActiveState",
        text,
        flags=re.DOTALL,
    ), "redacted summary helper must call `systemctl show -p ActiveState ...`"

    # Negative: the explicit allow-list must NOT include argv-bearing props.
    helper_start = text.index("print_redacted_unit_summary()")
    helper_end = text.index("install_linux_service()", helper_start)
    helper_body = text[helper_start:helper_end]
    for forbidden_prop in ("ExecStart", "Environment", "EnvironmentFiles"):
        assert (
            "-p %s" % forbidden_prop not in helper_body
        ), (
            "redacted summary helper requests %s, which can leak argv or "
            "secrets; remove it from the allow-list." % forbidden_prop
        )
