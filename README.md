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
- Dispatcher that matches open work to healthy capable agents and recovers expired leases.
- Structured agent message bus that rejects arbitrary execution payloads.
- Review and publication pipeline that requires evidence and an approved review before completion.
- Scoped secret handles with audit records and redacted API/CLI output.
- Reproducible runtime manifests with stable digests and secret-value checks.
- Tenant, user, Persona, Hermes instance, and platform binding records for multi-user expansion.
- Project bridge, operational memory/provenance records, and rollout/rescue workflows.
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

Key route groups:

- `/tenants`, `/users`, `/personas`
- `/hermes-instances`, `/hermes-instances/{id}/context`, `/platform-bindings`
- `/tasks`, `/tasks/{id}/evidence`, `/tasks/{id}/reviews`
- `/machines`, `/agents`, `/dispatch/tick`
- `/messages`
- `/secrets`, `/secret-audits`
- `/runtimes`, `/runtime-runs`
- `/bridge/items`, `/memory`
- `/rollouts`

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
