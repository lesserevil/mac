"""Helpers for tests that inspect the fleet deploy orchestrator + modules.

Historically `deploy/deploy-mac-fleet.sh` was a single ~2,400-line shell file.
The body of the remote payload (everything between `<<'REMOTE'` and the closing
`REMOTE`) now lives in `deploy/lib/remote/*.sh` modules, with local
orchestration helpers under `deploy/lib/orchestrator/`. Tests historically
asserted text patterns against the single file; they now use
:func:`deploy_script_text`, which returns a logical concatenation:

    orchestrator entry-point + orchestrator modules + assembled remote payload

This preserves the original "everything you can see in one place" property
that the assertions rely on, while letting the source live in focused files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEPLOY_DIR: Final[Path] = REPO_ROOT / "deploy"
DEPLOY_ENTRY: Final[Path] = DEPLOY_DIR / "deploy-mac-fleet.sh"
ORCHESTRATOR_LIB_DIR: Final[Path] = DEPLOY_DIR / "lib" / "orchestrator"
REMOTE_LIB_DIR: Final[Path] = DEPLOY_DIR / "lib" / "remote"


def remote_payload_text() -> str:
    """Return the assembled remote bash script as it is shipped to fleet hosts.

    Modules are concatenated in lexical order, matching the deploy_host
    runtime behaviour in deploy/lib/orchestrator/dispatch.sh::build_remote_payload.
    """

    pieces = []
    for module in sorted(REMOTE_LIB_DIR.glob("*.sh")):
        pieces.append(module.read_text(encoding="utf-8"))
    return "".join(pieces)


def orchestrator_text() -> str:
    """Return the orchestrator entry point plus its sourced helper modules.

    Order matches the `source` statements inside deploy-mac-fleet.sh:
    arguments.sh, archive.sh, dispatch.sh.
    """

    pieces = [DEPLOY_ENTRY.read_text(encoding="utf-8")]
    for name in ("arguments.sh", "archive.sh", "dispatch.sh"):
        pieces.append((ORCHESTRATOR_LIB_DIR / name).read_text(encoding="utf-8"))
    return "".join(pieces)


def deploy_script_text() -> str:
    """Return the logical content of the historical single-file deploy script.

    This is what test assertions should grep against. It concatenates the
    orchestrator (entry point + helper modules) with the assembled remote
    payload, which together hold every line that used to live in the
    monolithic deploy/deploy-mac-fleet.sh.
    """

    return orchestrator_text() + remote_payload_text()
