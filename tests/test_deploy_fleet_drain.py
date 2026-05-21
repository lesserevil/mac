from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"


def script_text():
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


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


def test_stop_existing_services_waits_for_mac_agent_offline_on_controlled_shutdown():
    """Bullwinkle's lease was stranded on `launchctl bootout` because the
    deploy path didn't synchronously confirm the worker had reported
    status=offline. Make the gap impossible: the stop step must call
    wait_for_mac_agent_offline after bootout / systemctl stop, with a
    bounded timeout knob the operator can tune.
    """
    text = script_text()

    stop_fn = text.index("stop_existing_services_for_deploy()")
    next_fn = text.index("\nwait_for_mac_agent_offline()", stop_fn)
    stop_body = text[stop_fn:next_fn]

    # The stop body must call the offline-wait helper after the service
    # manager has been told to terminate the worker.
    assert "wait_for_mac_agent_offline" in stop_body, stop_body
    assert "launchctl bootout" in stop_body
    assert "systemctl stop" in stop_body
    bootout_pos = stop_body.index("launchctl bootout")
    wait_call_pos = stop_body.index("wait_for_mac_agent_offline")
    assert bootout_pos < wait_call_pos, "must bootout before waiting for offline"

    # The waiter itself must be a real function (not just a forward decl)
    # and must exit on the agent reporting status=offline.
    waiter = text[next_fn:text.index("\n}\n\n", next_fn) + 3]
    assert 'status" = "offline"' in waiter or 'status = "offline"' in waiter
    assert "MAC_DEPLOY_OFFLINE_WAIT_SECONDS" in waiter
    assert "MAC_DEPLOY_OFFLINE_POLL_SECONDS" in waiter
    assert "mac-agent-shutdown.json" in waiter


def test_launchd_agent_plist_has_exit_timeout_for_graceful_shutdown():
    """launchd's default ExitTimeOut is 20s. With a slow API the
    mac-agent SIGTERM handler can miss the window and get SIGKILLed
    before its offline heartbeat lands. The plist must extend the window
    so controlled shutdowns reliably release the active lease.
    """
    text = script_text()

    # Locate the com.mac.agent plist heredoc inside install_darwin_agent_service.
    install_fn = text.index("install_darwin_agent_service()")
    plist_block = text[install_fn:text.index("\n}\n", install_fn)]
    assert "<key>Label</key><string>com.mac.agent</string>" in plist_block
    assert "<key>ExitTimeOut</key>" in plist_block, (
        "com.mac.agent plist must declare ExitTimeOut to give the worker "
        "time to post its offline heartbeat on SIGTERM"
    )


def test_mac_agent_service_wrapper_traps_sigterm_and_marks_offline():
    """Heartbeat-only and dry-run modes loop in shell, so when launchd
    bootouts the wrapper SIGTERM kills the loop without ever notifying
    the API. Install a trap so any controlled shutdown explicitly posts
    status=offline through `mac-agent --mark-offline`.
    """
    text = script_text()

    wrapper_fn = text.index("install_mac_agent_wrapper()")
    # Slice until the heredoc terminator that closes the wrapper script
    # body inside install_mac_agent_wrapper().
    wrapper_end = text.index("\nEOF\n  chmod 700 \"$wrapper\"", wrapper_fn)
    wrapper_body = text[wrapper_fn:wrapper_end]
    assert "mac_agent_drain_offline" in wrapper_body
    assert "--mark-offline" in wrapper_body
    # Trap must be installed for the heartbeat / dry-run loops.
    assert "trap 'mac_agent_drain_offline" in wrapper_body
    # The loops must be SIGTERM-interruptible — bare `sleep` blocks the
    # trap on macOS, so the wrapper backgrounds sleep with `wait`.
    assert "sleep \"$interval\" &" in wrapper_body
    assert "wait $!" in wrapper_body
