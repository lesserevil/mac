from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
LIB_DIR = ROOT / "deploy" / "lib" / "mac-fleet"


def test_deploy_script_is_thin_entrypoint_to_structured_modules() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "LIB_DIR=" in script
    assert "source \"$LIB_DIR/hosts.sh\"" in script
    assert "source \"$LIB_DIR/local.sh\"" in script
    assert "source \"$LIB_DIR/remote-payload.sh\"" in script
    assert "source \"$LIB_DIR/main.sh\"" in script

    assert "function_body" not in script
    assert len(script.splitlines()) < 220


def test_deploy_modules_split_local_orchestration_from_remote_payload() -> None:
    expected_modules = {
        "hosts.sh",
        "local.sh",
        "main.sh",
        "remote-payload.sh",
    }

    assert {path.name for path in LIB_DIR.glob("*.sh")} == expected_modules

    remote_payload = (LIB_DIR / "remote-payload.sh").read_text(encoding="utf-8")
    assert "REMOTE_PAYLOAD=$(cat <<'__MAC_REMOTE_PAYLOAD__'" in remote_payload
    assert "write_deploy_manifest()" in remote_payload
    assert "install_linux_service()" in remote_payload
    assert "verify_hub_registration()" in remote_payload

    local_modules = "\n".join(
        (LIB_DIR / name).read_text(encoding="utf-8")
        for name in ["hosts.sh", "local.sh", "main.sh"]
    )
    assert "write_deploy_manifest()" not in local_modules
    assert "install_linux_service()" not in local_modules
    assert "verify_hub_registration()" not in local_modules


def test_deploy_script_help_still_works_without_git_or_network_side_effects() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: deploy/deploy-mac-fleet.sh [agent ...]" in result.stdout
    assert "Rocky is the default hub" in result.stdout
    assert result.stderr == ""


def test_deploy_script_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
