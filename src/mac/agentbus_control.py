from __future__ import annotations

from typing import Any, Dict, Optional

JsonDict = Dict[str, Any]

REPO_UPDATE_SCHEMA = "mac.agentbus.repo_update.v1"
REPO_UPDATE_TOPIC = "mac.repo.update.v1"
REPO_UPDATE_CONTENT_TYPE = "application/vnd.mac.repo-update+json"

REPO_UPDATE_RESULT_SCHEMA = "mac.agentbus.repo_update_result.v1"
REPO_UPDATE_RESULT_TOPIC = "mac.repo.update.result.v1"
REPO_UPDATE_RESULT_CONTENT_TYPE = "application/vnd.mac.repo-update-result+json"


def repo_update_payload(
    *,
    repo_path: Optional[str] = None,
    remote: str = "origin",
    branch: str = "main",
    restart: bool = True,
    request_id: Optional[str] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": REPO_UPDATE_SCHEMA,
        "remote": remote,
        "branch": branch,
        "restart": bool(restart),
    }
    if repo_path:
        payload["repo_path"] = repo_path
    if request_id:
        payload["request_id"] = request_id
    return payload
