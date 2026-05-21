from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
PROD_DEPLOY_DOC = ROOT / "docs" / "production-deployment.md"


def script_text():
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def doc_text():
    return PROD_DEPLOY_DOC.read_text(encoding="utf-8")


def test_deploy_drains_worker_before_stopping_services():
    text = script_text()

    assert "drain_mac_agent_before_deploy()" in text
    assert "wait_for_agent_active_leases" in text
    assert "MAC_DEPLOY_DRAIN_MODE" in text
    assert "MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS" in text
    assert 'health_status":"degraded' in text

    drain_pos = text.index("drain_mac_agent_before_deploy")
    stop_call_pos = text.index("stop_existing_services_for_deploy", text.index('write_deploy_manifest "pre"'))
    assert drain_pos < stop_call_pos


def test_deploy_clears_worker_drain_after_restart():
    text = script_text()

    assert "clear_mac_agent_drain_after_deploy()" in text
    assert 'health_status":"healthy' in text
    verify_pos = text.index("verify_hub_registration")
    clear_pos = text.index("clear_mac_agent_drain_after_deploy", verify_pos)
    post_manifest_pos = text.index('write_deploy_manifest "post"')
    assert verify_pos < clear_pos < post_manifest_pos


def test_production_deployment_doc_describes_drain_env_vars():
    """Lock in the operator-facing contract.

    The drain implementation (commits 82a5bd7, ec50d5f on main) exposes three
    env vars and writes a per-host JSON summary. Operators should not have to
    read deploy-mac-fleet.sh to discover this; production-deployment.md is the
    documented entry point. If any of these knobs disappear from the doc, the
    deploy is silently undocumented and the bead can resurrect.
    """
    text = doc_text()

    assert "Active-worker drain" in text, "drain subsection missing from prod deploy doc"
    assert "MAC_DEPLOY_DRAIN_MODE" in text
    assert "MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS" in text
    assert "MAC_DEPLOY_DRAIN_POLL_SECONDS" in text
    # All three drain modes must be described.
    for mode in ("wait", "fail-fast", "skip"):
        assert mode in text, f"drain mode {mode!r} missing from prod deploy doc"
    # Per-host drain summary artifact must be named.
    assert "mac-agent-drain.json" in text
    # The doc must place drain before stop_existing_services_for_deploy in
    # the documented order, matching the script.
    drain_doc_pos = text.index("drain_mac_agent_before_deploy")
    stop_doc_pos = text.index("stop_existing_services_for_deploy", drain_doc_pos)
    assert drain_doc_pos < stop_doc_pos
