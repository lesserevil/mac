# mac

Multi-agent coordinator control plane.

`mac` is a clean-room control plane for fleets of AI agents. It is designed to
sit underneath a human-facing agent runtime such as
`NousResearch/hermes-agent`, OpenClaw, or a compatible system.

Hermes owns conversation, personality, adaptive memory, skills, and messaging
gateways. `mac` owns durable operational truth: tasks, leases, routing,
reviews, evidence, secrets, runtime manifests, rollout state, and audit trails.

The goal is to let a user talk to a persistent Hermes agent with a real
personality and memory, then let that agent create durable work that a broader
fleet can execute, review, publish, and recover.

## Core Contracts

This project provides durable contracts for coordinating a fleet:

- SQLite-backed task ledger with state transitions, leases, history, evidence, dependencies, and recovery.
- Machine and agent registry with capabilities, resources, health, and availability.
- Dispatcher that matches open work to healthy capable agents and accounts for
  tenant pool policy, resources, capacity, stale heartbeats, and expired leases.
- Structured agent message bus that rejects arbitrary execution payloads.
- Review and publication pipeline that requires typed evidence, independent
  approved review, and publication hashes when policy requires them.
- Optional scoped API bearer tokens for read/write/agent/dispatch/secret/admin access.
- Tenant-scoped secret handles with audit records and redacted API/CLI output.
- Reproducible runtime manifests with stable digests and secret-value checks.
- Tenant, user, Persona, Hermes instance, and platform binding records for multi-user expansion.
- Project bridge, operational memory/provenance records, and gated rollout/rescue workflows.
- Evaluation contract: named `eval_sets` (scoring direction, baseline, regression threshold) and `eval_runs` against rollout versions, runtime environments, or agent builds; rollouts can require a passing `eval_run` before `promote`.
- FastAPI REST API and `mac` CLI.
- Hermes-side `mac-hermes` adapter for registration, sanitized task creation, status replies, and memory write-back payloads.

## Boundary With Hermes

Hermes is the primary interaction agent:

- Slack, Telegram, Discord, CLI, and other message apps terminate in Hermes.
- `SOUL.md`, `USER.md`, `MEMORY.md`, skills, and session memory belong to Hermes.
- Adaptive personality belongs to Hermes because it needs conversational context.
- Hermes may create work in `mac`, but should pass only the sanitized operational context needed by the task.

`mac` deliberately does not implement agent souls or personal memory. Its
`memory_records` are for operational provenance: imports, task evidence,
decisions, rollout events, and durable facts needed to audit work. User memory
and personality memory stay in Hermes.

The identity framework reflects that split:

- `tenant`: an organization or isolated user deployment.
- `user`: a human identity inside a tenant.
- `persona`: a named Hermes personality with a `soul_ref` and `memory_scope`.
- `hermes_instance`: a running or durable Hermes identity such as Rocky.
- `platform_binding`: a Slack workspace/channel, Telegram chat, or similar binding.
- interaction task: a durable task created from a Hermes conversation with origin metadata, not copied private memory.

## Quick Start

```bash
uv run --extra dev pytest

# Required: a 32+ char secret used to derive the Fernet key for the secrets table.
# Without it, the CLI and API both refuse to start.
export MAC_SECRET_KEY="$(openssl rand -base64 32)"

uv run mac --db mac.db init
uv run uvicorn mac.api:app --reload
uv run mac-hermes --url http://127.0.0.1:8000 --help
```

The CLI stores state in `mac.db` by default. Use `--db path/to/file.db` or `MAC_DB` to choose a different SQLite database.

## API

Run the REST API with `MAC_SECRET_KEY` set:

```bash
MAC_SECRET_KEY="..." uv run uvicorn mac.api:app --reload
# or use factory mode to be explicit:
MAC_SECRET_KEY="..." uv run uvicorn mac.api:create_app --factory --reload
```

Set `MAC_API_TOKEN` for one admin token, or `MAC_API_TOKENS` as JSON such as
`{"reader":["read"],"worker":["agent","dispatch"]}` to require scoped bearer
tokens. With no API token configured, the local prototype API remains open for
development.

The built-in dashboard is served at `/ui`. Static dashboard assets are public so
the browser can load the shell, while data requests still use the same API token
rules as the REST API. Enter a token with the needed read/write/dispatch/secret
scopes in the dashboard when API tokens are enabled. The dashboard source is
plain TypeScript in `src/mac/ui/app.ts`; the checked-in `app.js` browser output
is served directly so there is no Node.js, npm, bundler, or frontend build step.
The dashboard has read models for overview, agents, task timelines, Hermes
activity, runtime/rollout status, and redacted secret audits. Operator actions
cover dispatch ticks, task transitions, evidence, reviews, publication, rollout
advance/health/rescue, and secret handle requests. It deliberately does not
expose a casual secret reveal action.

Key route groups:

- `/tenants`, `/users`, `/personas`
- `/hermes-instances`, `/hermes-instances/{id}/context`, `/platform-bindings`
- `/dashboard/state`, `/dashboard/agents/{id}`, `/dashboard/tasks/{id}/timeline`, `/dashboard/dispatch/explain`, `/dashboard/hermes/{id}/activity`, `/dashboard/rollouts/{id}/status`
- `/tasks`, `/tasks/{id}/evidence`, `/tasks/{id}/reviews`
- `/machines`, `/agents`, `/dispatch/tick`, `/dispatch/dead-letters`
- `/messages`
- `/secrets`, `/secrets/{id}/access`, `/secrets/{id}/reveal`, `/secret-audits`
- `/runtimes`, `/runtime-runs`
- `/artifacts`, `/artifacts/{id_or_digest}` — canonical record for deliverables (kind, digest, uri, sbom_uri, signers); re-registering the same digest augments signers/metadata
- `/environments`, `/environments/{id}/deploy|current|deployments` — environment registry + artifact→environment deployment edges; deploy atomically retires the prior active deployment
- `/fleet/build-distribution` — aggregate live agents by `running_digest`; agents declare their build via `heartbeat`
- `/bridge/items`, `/memory`
- `/rollouts`, `/rollouts/{id}/artifact`, `/rollouts/{id}/health`, `/rollouts/{id}/rescue`
- `/eval-sets`, `/eval-sets/{id}/baseline`, `/eval-sets/{id}/events`, `/eval-runs`
- `/events` — unified audit stream across task/rollout/eval_set/secret surfaces; filter by `subject_type`, `subject_id`, `actor`, `event_type`, `event_type_prefix`, `since`, `until`, `limit`

## CLI Examples

```bash
mac --db mac.db machine register workstation-1
mac --db mac.db agent register machine_... worker --capabilities python,review
mac --db mac.db tenant register personal
mac --db mac.db persona register tenant_... Rocky --soul-ref hermes://personal/rocky/SOUL.md --memory-scope hermes://personal/rocky/memory
mac --db mac.db hermes register tenant_... rocky --persona-id persona_... --home-ref hermes://personal/rocky
mac --db mac.db binding register tenant_... hermes_... slack T123/C456 --display-name "#ops"
mac --db mac.db interaction task hermes_... "Investigate deployment failure" --platform-binding-id binding_...
mac --db mac.db task create "Implement feature" --required-capabilities python
mac --db mac.db dispatch tick
mac --db mac.db task show task_...

# Secrets: prefer stdin or file input over argv to keep values out of shell history.
echo -n "$GH_TOKEN" | mac --db mac.db secret set github-token \
    --from-stdin --scopes '{"capabilities":["deploy"]}' --created-by human
mac --db mac.db secret set release-key --from-file ./release.key \
    --scopes '{"capabilities":["deploy"]}' --created-by human

# Rollouts require a pinned runtime and verified sha256 artifact before install.
mac --db mac.db runtime create mac-runtime \
    --manifest '{"image":"python:3.12@sha256:abc123","dependencies":["fastapi==0.111.0"]}' \
    --created-by human
mac --db mac.db rollout create 1.2.0 canary --runtime runtime_... \
    --artifact-uri artifact://mac/1.2.0 --artifact-hash sha256:abc123 \
    --health-policy '{"required_checks":["runtime","canary"]}' \
    --created-by human
mac --db mac.db rollout advance rollout_... start_canary --actor human
mac --db mac.db rollout health rollout_... \
    --checks '{"runtime":"healthy","canary":"ok"}' --actor monitor

# Evaluation: define a scored eval set, record runs against rollout versions,
# and gate promotion on a passing run.
mac --db mac.db eval set create task-success-rate \
    --scoring higher_is_better --baseline-score 0.90 --regression-threshold 0.02
mac --db mac.db eval run record evalset_... rollout_version 1.2.0 0.93
mac --db mac.db rollout create 1.3.0 canary --runtime runtime_... \
    --artifact-uri artifact://mac/1.3.0 --artifact-hash sha256:def456 \
    --required-eval-set-id evalset_... --created-by human
mac --db mac.db rollout advance rollout_... start_canary --actor human
# promote refused until a passing eval run exists for version 1.3.0
mac --db mac.db rollout advance rollout_... promote --actor human

# Unified audit stream: one query across task/rollout/eval_set/secret/environment events.
mac --db mac.db events list --limit 50
mac --db mac.db events list --subject-type rollout --subject-id rollout_...
mac --db mac.db events list --prefix rollout. --since 2026-05-17T00:00:00+00:00
mac --db mac.db events list --actor monitor --event-type rollout.health_failure_during_rescue

# Artifact registry + environment deployments + fleet build inventory.
mac --db mac.db artifact register image sha256:abc... artifact://mac/v1.2.0 \
    --created-by ci --sbom-uri sbom://mac/v1.2.0.spdx --signers ci,release-manager
mac --db mac.db env register staging --channel release --created-by human
mac --db mac.db env deploy staging sha256:abc... --actor release-bot
mac --db mac.db env current staging
mac --db mac.db agent heartbeat agent_... --running-digest <runtime-digest>
mac --db mac.db fleet build-distribution

# One-time ACC migration: dry-run first, then import open ACC work into mac.
# Claimed/in-progress ACC tasks are blocked unless explicitly requeued with --allow-active.
mac --db mac.db migrate acc ~/.acc/data/acc.db --mode dry-run \
    --report acc-migration-dry-run.json
mac --db mac.db migrate acc ~/.acc/data/acc.db --mode import \
    --report acc-migration-import.json

# Minimal worker harness: claim one mac-owned task for a specific agent, run an
# executor, record log evidence, and submit successful work for review.
mac-agent --url http://127.0.0.1:8000 --agent-id agent_... \
    --workspace ~/.mac-agent/workspaces --executor -- hermes run-once
```

Hermes-facing API adapter:

```bash
mac-hermes --url http://127.0.0.1:8000 register \
  --tenant personal \
  --persona Rocky \
  --instance rocky \
  --soul-ref hermes://personal/rocky/SOUL.md \
  --memory-scope hermes://personal/rocky/memory \
  --binding slack:T123/C456:#ops

mac-hermes --url http://127.0.0.1:8000 task hermes_... \
  "Investigate deployment failure" \
  --summary "The Slack deployment thread reports a failed publish step." \
  --platform-binding-id binding_... \
  --conversation-ref slack://T123/C456/1712345678.000100 \
  --required-capabilities ops

mac-hermes --url http://127.0.0.1:8000 reply task_...
mac-hermes --url http://127.0.0.1:8000 writeback hermes_... task_...
```

## Design Docs

- [Hermes Boundary](docs/hermes-boundary.md)
- [Hermes Integration](docs/hermes-integration.md)
- [Scaling Plan](docs/scaling-plan.md)
