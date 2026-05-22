from mac.evidence_validators import registered_evidence_types, validate_evidence_type


def _repo_manifest(**overrides):
    manifest = {
        "schema": "mac.verification.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abcdef1234567890",
            "dirty": False,
            "pushed": True,
            "remote_ref": "origin/main",
            "files_changed": ["src/mac/services.py"],
        },
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    manifest.update(overrides)
    return manifest


def _passed_check_count(manifest):
    return sum(1 for item in manifest.get("checks", []) if item.get("returncode") == 0)


def test_evidence_validators_are_registry_backed_by_type():
    assert registered_evidence_types() == [
        "artifact",
        "deployment",
        "documentation",
        "no_change",
        "repo_change",
        "review_verdict",
        "test",
    ]
    assert validate_evidence_type(
        "repo_change",
        _repo_manifest(),
        passed_check_count=_passed_check_count,
    ) == []


def test_repo_change_validator_reuses_repo_anchor_and_check_gates():
    manifest = _repo_manifest()
    manifest["repo"]["dirty"] = True
    manifest["repo"]["files_changed"] = []
    manifest["checks"] = [{"name": "pytest", "returncode": 1}]

    problems = validate_evidence_type(
        "repo_change",
        manifest,
        passed_check_count=_passed_check_count,
    )

    assert "repo evidence must declare dirty=false" in problems
    assert "repo evidence requires changed files" in problems
    assert "repo code evidence requires at least one passing test/check" in problems


def test_non_code_validators_keep_type_specific_requirements():
    deployment = _repo_manifest(
        evidence_type="deployment",
        targets=["rocky"],
    )
    assert validate_evidence_type(
        "deployment",
        deployment,
        passed_check_count=_passed_check_count,
    ) == []

    artifact = _repo_manifest(evidence_type="artifact")
    problems = validate_evidence_type(
        "artifact",
        artifact,
        passed_check_count=_passed_check_count,
    )
    assert "artifact evidence requires artifacts" in problems

    no_change = _repo_manifest(
        evidence_type="no_change",
        repo={**_repo_manifest()["repo"], "files_changed": []},
        reason="already implemented",
    )
    assert validate_evidence_type(
        "no_change",
        no_change,
        passed_check_count=_passed_check_count,
    ) == []


def test_review_verdict_validator_requires_verdict_anchor_and_digest():
    manifest = _repo_manifest(
        evidence_type="review_verdict",
        verdict="approved",
        reviewed_evidence_id="ev_123",
        worktree_digest="sha256:" + "a" * 64,
    )

    assert validate_evidence_type(
        "review_verdict",
        manifest,
        passed_check_count=_passed_check_count,
    ) == []

    manifest.pop("reviewed_evidence_id")
    manifest["worktree_digest"] = "not-a-digest"
    problems = validate_evidence_type(
        "review_verdict",
        manifest,
        passed_check_count=_passed_check_count,
    )
    assert "review_verdict evidence requires reviewed_evidence_id" in problems
    assert "review_verdict evidence requires worktree_digest sha256" in problems
