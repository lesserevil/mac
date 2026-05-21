from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
DEPLOY_DOC = ROOT / "docs" / "production-deployment.md"


def script_text():
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def doc_text():
    return DEPLOY_DOC.read_text(encoding="utf-8")


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
    doc = doc_text()

    # The drain section must exist and name every operator-facing env var so a
    # human deploying mac knows how to wait / fail-fast / skip.
    assert "### Active-worker drain" in doc
    assert "MAC_DEPLOY_DRAIN_MODE" in doc
    assert "MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS" in doc
    assert "MAC_DEPLOY_DRAIN_POLL_SECONDS" in doc
    assert "mac-agent-drain.json" in doc
    # And the doc must spell out the three supported modes so operators do not
    # guess at the values the script accepts.
    assert "wait" in doc
    assert "fail-fast" in doc
    assert "skip" in doc
