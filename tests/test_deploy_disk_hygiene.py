from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"


def script_text():
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def test_deploy_reports_disk_hygiene_before_and_after_cleanup():
    text = script_text()

    assert "MAC_DEPLOY_BACKUP_RETENTION_DAYS" in text
    assert "MAC_DEPLOY_BACKUP_RETENTION_COUNT" in text
    assert "MAC_DEPLOY_OBSOLETE_ARTIFACT_RETENTION_DAYS" in text
    assert "DISK_HYGIENE_REPORT" in text
    assert "run_disk_hygiene_cleanup()" in text
    assert "disk_usage_before" in text
    assert "disk_usage_after" in text
    assert "disk_hygiene" in text

    pre_manifest_pos = text.index('write_deploy_manifest "pre"')
    cleanup_pos = text.index("run_disk_hygiene_cleanup", pre_manifest_pos)
    backup_pos = text.index("backup_existing_artifacts", cleanup_pos)
    assert pre_manifest_pos < cleanup_pos < backup_pos


def test_deploy_disk_hygiene_preserves_live_and_migrated_state():
    text = script_text()
    hygiene_block = text.split("run_disk_hygiene_cleanup() {", 1)[1].split(
        "backup_existing_artifacts() {", 1
    )[0]

    assert 'preserve_paths = {mac_home, Path(os.environ["SRC_DIR"]), Path(os.environ["VENV"]), Path(os.environ["HERMES_DIR"]), Path.home() / ".hermes", Path.home() / ".acc"}' in hygiene_block
    assert "acc-migration-import.json" in hygiene_block
    assert "deleted = [delete_path(candidate) for candidate in obsolete_targets]" in hygiene_block
    assert "obsolete_acc_replacement_artifacts" in hygiene_block
    assert '"status": "complete"' in hygiene_block
