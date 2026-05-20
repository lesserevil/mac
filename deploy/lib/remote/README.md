# deploy/lib/remote

These shell module files compose the body of the remote bash script that
`deploy/deploy-mac-fleet.sh` ships to each fleet host. The orchestrator
concatenates them in lexical order and pipes the result over SSH to `bash -s`.

## Why split?

The historical single-file `deploy-mac-fleet.sh` reached ~2400 lines mixing
host orchestration, generated service files, embedded Python, Hermes patching,
bootstrap logic, and migration behavior in one shell surface. Small changes
required reasoning over the whole script. This directory carves the remote
payload into focused modules ordered by concern.

## Order matters

The remote script is procedural — it defines helpers, then runs them, then
defines more helpers, then runs them. We preserve the original execution order:

  00-header                : `set -euo pipefail`, env vars, paths, `mkdir`, `exec >…`
  10-utils                 : `log`, `python_bin`, DNS, venv probes
  20-manifest              : `write_deploy_manifest` (pre/post manifest JSON)
  30-rollback-and-backup   : rollback script writer, backup of existing artifacts, service-stop helper
  40-drain                 : drain API helpers (`mac_api_json`, `drain_mac_agent_before_deploy`, …)
  50-beads                 : beads CLI install, repo bootstrap, tracked-export restore
  60-hermes-runtime        : Hermes redaction env normalization, gateway shim, messaging deps, home channels
  65-hermes-kanban         : `repair_hermes_kanban_schema` (large embedded Python)
  68-procedural-install    : PROCEDURAL — deploy log, ensure_*, pre-manifest, drain, install src, beads, Hermes patches, DB init, ACC DB detection
  70-migration-report      : `summarize_report` + `write_migration_status`
  75-procedural-migration  : PROCEDURAL — invoke migrate + summarize_report
  80-services-common       : `install_linux_service`, `install_hermes_gateway_wrapper`, `install_mac_agent_wrapper`
  81-services-linux        : Linux Hermes/agent service installers
  82-services-darwin       : Darwin launchd plist installers
  90-verify                : gateway-log classifier, hub-registration verifier
  99-procedural-finalize   : PROCEDURAL — OS-dispatch service install, health checks, hub verify, drain clear, post manifest

Modules whose names start with `NN-procedural-*` contain top-level
**runtime statements** rather than function definitions. They are intentionally
separated so reviewers can see the linear deployment flow without scrolling
through long helper bodies.

## Editing rules

1. **Never edit `deploy-mac-fleet.sh` to add new remote logic.** Add it to the
   appropriate module here; the orchestrator picks up changes automatically.
2. Keep modules byte-clean: a trailing newline at EOF, no carriage returns.
3. New helper functions should land in a topical module (e.g. drain helpers go
   in `40-drain.sh`). New procedural steps go in the `*procedural*` module that
   matches their execution stage.
4. The assembled payload is verified against the contract in
   `tests/test_deploy_modules.py` — run `pytest tests/test_deploy_modules.py`
   after any change here.
