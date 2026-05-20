# Production Deployment

Two supported topologies:

1. **Single host, systemd** — one machine, one SQLite database, one FastAPI
   process. Suitable for dev fleets, personal Hermes runtimes, and pilot
   deployments. See `deploy/systemd/`.
2. **Containerized, single-instance** — image at `Dockerfile`. Same SQLite
   topology, but lifecycle is managed by the container runtime (Docker,
   Podman, k8s as a single-replica deployment). See the container section
   below.

`mac` is not designed for horizontal scale-out on SQLite. SQLite WAL handles
concurrent reads well and serializes writes through filesystem locks — so
`uvicorn --workers > 1` against the same DB *works*, but every write call
contends on the same lock. For read-heavy fleets this is fine; for write-heavy
loads (busy dispatcher, many heartbeats) the throughput ceiling is the single
writer. For multi-host or multi-region, swap `mac.store.SQLiteStore` for a
Postgres backend (the read/write helpers are small and isolated) before
deploying more than one writer.

## Required configuration

| Variable | Required | Purpose |
|---|---|---|
| `MAC_SECRET_KEY` | yes | 32+ char secret; HKDF input for the Fernet key that encrypts secret values. Refuses to start without it. |
| `MAC_DB` | no | SQLite file path. Default `./mac.db`. |
| `MAC_API_TOKEN` | no | Single admin bearer token. Set empty string is rejected. |
| `MAC_API_TOKENS` | no | JSON `{token: [scopes,...]}` for scoped auth. Mutually exclusive with `MAC_API_TOKEN`. |
| `HERMES_HOME` | no | Hermes state directory checked at startup. Default `~/.hermes`. |
| `ACC_DIR` | no | Legacy ACC data directory checked for migration/state references. Default `~/.acc`. |
| `MAC_HERMES_AGENT_DIR` | no | Hermes checkout inspected for the `slack_accounts.json` activation shim. Falls back to `HERMES_AGENT_DIR`, then `~/Src/hermes-agent` if present. |
| `MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM` | no | Set `0` to disable startup patching of an explicit `MAC_HERMES_AGENT_DIR`. Default enabled only when the checkout path is explicit. |
| `MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM` | no | Set `0` to disable startup patching of Hermes gateway model/runtime overrides. Default enabled for explicit checkout paths. |
| `MAC_HERMES_GATEWAY_MODEL` | no | Per-agent model used by Hermes gateway conversations and mirrored to `HERMES_INFERENCE_MODEL` for oneshot worker execution. |
| `MAC_HERMES_GATEWAY_PROVIDER` | no | Runtime provider for the per-agent model. Fleet deploy uses `custom` so TokenHub remains the shared OpenAI-compatible endpoint. |
| `MAC_HERMES_GATEWAY_BASE_URL` | no | Optional explicit OpenAI-compatible base URL. Usually omitted because deployed hosts derive TokenHub's `/v1` endpoint from `TOKENHUB_URL`. |
| `MAC_HERMES_STARTUP_CHECK` | no | Set `0` to disable Hermes state and Slack startup checks. Enabled by default. |
| `MAC_REQUIRE_HERMES_STARTUP_READY` | no | Set `1` to fail `mac` startup when Hermes soul/memory/state references or Slack activation are not ready. |
| `MAC_HERMES_SLACK_HOME_CHANNEL_NAME` | no | Slack home-channel name, without `#`, used to write `~/.hermes/slack_home_channels.json` from `slack_accounts.json`. Default `rockyandfriends`. |
| `MAC_HERMES_SYNC_SLACK_HOME_CHANNELS` | no | Set `0` to preserve existing Slack home-channel files without discovery. Default enabled. |

Generate a secret key once:

```bash
openssl rand -base64 48
```

Store it in a secrets manager. Rotating the key requires re-encrypting all
secret values; today this is a manual procedure (re-emit every secret with
the new key).

## Systemd

```bash
# 1. Create the service user and data directory.
sudo groupadd --system mac
sudo useradd --system --gid mac --home-dir /var/lib/mac \
    --shell /usr/sbin/nologin mac
sudo install -d -o mac -g mac -m 0750 /var/lib/mac

# 2. Install mac globally (or into a venv at /usr/local/lib/mac).
sudo pip install /path/to/mac-0.1.0-py3-none-any.whl

# 3. Write the env file (mode 0600, owner root:mac).
sudo install -d -o root -g mac -m 0750 /etc/mac
sudo install -o root -g mac -m 0640 deploy/systemd/mac.env.example /etc/mac/mac.env
sudo $EDITOR /etc/mac/mac.env       # set MAC_SECRET_KEY, optionally MAC_API_TOKEN

# 4. Install and start the unit.
sudo install -o root -g root -m 0644 deploy/systemd/mac.service \
    /etc/systemd/system/mac.service
sudo systemctl daemon-reload
sudo systemctl enable --now mac.service

# 5. Verify.
sudo systemctl status mac.service
curl -fsS http://127.0.0.1:8000/health
```

The unit binds to `127.0.0.1:8000`. Put a TLS-terminating reverse proxy
(nginx, Caddy) in front for external access — do not expose the bare port.

## One-Time ACC Replacement Deploy

For the current Rocky/Natasha/Bullwinkle cutover, use the fleet deploy script:

```bash
deploy/deploy-mac-fleet.sh
```

Per-agent deploy settings live in `deploy/agents/<agent>/config.env`. The
committed fleet configs set `MAC_DEPLOY_TARGET`, `MAC_DEPLOY_OS`,
`MAC_HERMES_SLACK_HOME_CHANNEL_NAME`, and a distinct
`MAC_HERMES_GATEWAY_MODEL` for each agent. Host-local secret env files still
own tokens. Override `MAC_DEPLOY_AGENT_CONFIG_DIR` to test an alternate config
set.

The default fleet intentionally avoids model monoculture:

| Agent | Hermes model |
|---|---|
| Rocky | `azure/openai/gpt-5.5` |
| Natasha | `azure/anthropic/claude-opus-4-7` |
| Bullwinkle | `gcp/google/gemini-2.5-pro` |

Fleet deploy mirrors each configured model into `ACC_HERMES_GATEWAY_MODEL`,
`HERMES_INFERENCE_MODEL`, and `ACC_LLM_MODEL` so upstream Hermes gateway turns
and `mac-hermes-task-executor` oneshot work use the same per-agent identity.
Credentials remain in TokenHub or inherited host-local env files; mac only
writes model/provider selectors.

It ships this repository to each host, installs `mac` into `~/.mac/venv`,
redeploys upstream `NousResearch/hermes-agent` into `~/.mac/hermes-agent`,
applies the minimal multi-Slack Hermes patch, preinstalls configured Hermes
messaging dependencies before service start, applies the Hermes gateway
model/runtime shim, runs the ACC SQLite migration
dry-run and import from `~/.acc/data/fleet.db` or `~/.acc/data/acc.db`, and
starts a local `mac` service. Linux hosts get `mac.service`; macOS hosts get
`com.mac.control-plane`. The same deployment also starts a mac-managed Hermes
gateway from the upstream checkout: `mac-hermes-gateway.service` on Linux and
`com.mac.hermes-gateway` on macOS. It also installs a persistent `mac-agent`
registration service: `mac-agent.service` on Linux and `com.mac.agent` on
macOS.

When the local Git remote is available, fleet deploy installs `~/.mac/src/mac`
as a branch-tracking Git worktree and sets `MAC_SELF_UPDATE_REPO` to that path.
That lets the AgentBus repo-update control message pull future changes and
restart the listening `mac-agent` process without another manual deploy pass.

The fleet topology is hub-and-spoke, matching ACC. Rocky is the default hub at
`http://100.125.137.89:8789`; Natasha, Bullwinkle, and other spokes keep a
host-local control plane for local state and Hermes startup checks, but their
`mac-agent` service registers and heartbeats against Rocky. By default the hub
binds `0.0.0.0` and spokes bind `127.0.0.1`.
Runtime lazy dependency installs are disabled after the preinstall step, and
`HERMES_REDACT_SECRETS=false` in inherited Hermes env files is corrected to
`true` because disabled redaction is treated as state drift.

Deployment logs and migration reports are written under `~/.mac/logs/` on each
host:

- `deploy-*.log`
- `deploy-manifest-*-pre.json`, `deploy-manifest-*-post.json`, and
  `deploy-manifest-latest.json`
- `rollback-*.sh` and `rollback-latest.sh`
- `acc-migration-dry-run.json`
- `acc-migration-import.json`
- `acc-migration-status.json`
- `startup-hermes.json`
- `hermes-messaging-deps.json`
- `hermes-home-channel-sync.json`
- `hermes-redaction-normalization.json`
- `hermes-log-summary.json`
- `mac-service-journal.txt` on Linux, or `mac-service.log` on macOS
- `hermes-gateway-journal.txt` on Linux, or `hermes-gateway.log` on macOS
- `mac-agent-journal.txt` on Linux, or `mac-agent.log` on macOS
- `hub-agents.json`

The activation shim for `slack_accounts.json` is intentionally applied by
`mac` startup, not by the deploy script, so this path exercises the startup
patch capability.

To roll back the most recent deployment on a host:

```bash
~/.mac/logs/rollback-latest.sh
```

The rollback script restores the prior mac source tree, mac venv, Hermes
checkout, and service definitions or launchd plists that existed before the
deploy pass, then restarts the mac-managed services.

## Worker Agents

The control-plane service does not execute tasks by itself. Each execution host
must run a worker process that registers or refreshes its machine/agent row,
heartbeats, then claims eligible open work with a real executor. Fleet deploy
installs that process as a service in `heartbeat` mode by default so hosts are
visible in Rocky's registry without claiming imported ACC work prematurely:

```bash
mac-agent --url http://100.125.137.89:8789 --register \
  --agent-name rocky --hostname rocky.local \
  --capabilities python,ops,review --resources '{"capacity":2}' \
  --heartbeat-only

mac-agent --url http://100.125.137.89:8789 --register \
  --agent-name rocky --capabilities python,ops,review \
  --workspace ~/.mac-agent/workspaces --loop \
  --executor ~/.mac/bin/mac-hermes-task-executor
```

Use `--heartbeat-only` during deploy validation when you want fleet visibility
without claiming migrated ACC work. Start the `--loop` form only after the
executor command is the intended production worker. Successful executions write
log evidence, move tasks to `needs_review`, and ask the control plane to run
the default review workflow. The default workflow assigns a healthy agent that
has never owned the task as reviewer, then waits for a separate signed
`review_verdict` evidence row from that reviewer. It publishes/completes the
task only when both executor evidence and reviewer verdict are verifiable.
Failed executions fail the task with evidence attached.

Executor success is not completion. A zero return code only means the executor
reported without crashing. For the default workflow to auto-approve and publish,
the evidence metadata must include a `mac.worker_evidence.v1` verification
manifest. Repo/code work must include a pushed git artifact (`head_sha`,
`remote_ref` or PR URL, `pushed=true`, `dirty=false`, changed files) plus at
least one passing test/check. Documentation or investigation work must include a
pushed repo artifact or explicit artifacts plus passing checks. Deployment work
must include deployment targets/services and passing checks. Thin reports,
local-only diffs, missing manifests, failing checks, or unverifiable claims stay
in `needs_review`/`reviewing` for manual handling.

During execution, the worker renews its active task lease. A successful renewal
also refreshes the owning agent's `last_seen_at`, keeps it `busy`, and preserves
`current_task_id`, so long-running work remains visible as live without allowing
an invalid `idle` heartbeat while the lease is active.

For already-migrated or pre-upgrade rows that are stuck in `needs_review`, run
the backlog tick against Rocky:

```bash
curl -X POST 'http://100.125.137.89:8789/reviews/default/tick?limit=100&actor=operator'
```

Before enabling executor-backed claiming, use `dry-run` mode to record routing
candidates without creating leases:

```bash
MAC_DEPLOY_WORKER_MODE=dry-run
MAC_DEPLOY_WORKER_REQUIRE_CANARY=1
MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=mac-canary
```

Dry-run mode emits `worker.routing.policy`,
`worker.routing.dry_run_candidate`, or `worker.routing.no_candidate` events
into `/observability` and `/observability/stream`. It must show only synthetic
canary work before loop mode is enabled.

To enable executor-backed claiming from deploy config, set:

```bash
MAC_DEPLOY_WORKER_MODE=loop
MAC_DEPLOY_WORKER_CAPABILITIES=ops,python,hermes,review
MAC_DEPLOY_WORKER_REQUIRE_CANARY=1
MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=mac-canary
```

The generated service then runs `mac-agent --register --loop` against
`MAC_HUB_URL`, using `MAC_WORKER_TOKEN` from `~/.mac/mac.env`. The default
executor wrapper is `~/.mac/bin/mac-hermes-task-executor`, which calls the
deployed upstream Hermes checkout in one-shot mode.

Workers advertise `review` by default so the default review workflow can pick
real second-eye reviewers. During registration the worker persists its
attestation key into `~/.mac/mac.env`; if an older deploy missed that one-time
key, the service rotates a replacement before it signs new evidence. Rotation
is explicit recovery behavior and invalidates old signatures from that agent.

Loop mode is canary-gated by default. To make a worker eligible for real
migrated work, explicitly set `MAC_DEPLOY_WORKER_REQUIRE_CANARY=0` and narrow
the blast radius with project or metadata filters first.

## Beads Bridge

Rocky's hub can turn ready Beads into durable mac tasks automatically. Register
each repository once:

```bash
mac --db ~/.mac/mac.db bridge beads register mac ~/.mac/src/mac \
  --source repo-beads-mac --project repo-beads-mac
```

Every registered repository must include a repository runtime contract at
`.mac/project.yaml`. The bridge validates that contract at registration and on
each poll, and rejects repositories that do not declare their supported host
families, bootstrap command, canonical test command, and required evidence.
See [Repository Runtime Contract](repository-runtime-contract.md).

The deploy script enables heartbeat polling on the hub agent (`rocky`) and
registers the deployed mac checkout by default through:

```bash
MAC_BEADS_BRIDGE_ON_HEARTBEAT=1
MAC_BEADS_BRIDGE_HUB_AGENT=rocky
MAC_BEADS_CLI=$HOME/.mac/bin/bd
MAC_BEADS_REPOSITORIES=mac=$HOME/.mac/src/mac:repo-beads-mac:repo-beads-mac::30
```

The deploy bootstrap installs the `bd` CLI into `~/.mac/bin/bd` from the
configured Beads source (`MAC_DEPLOY_BEADS_REPO_URL`, `MAC_DEPLOY_BEADS_REF`)
when it is not already present, then runs `bd bootstrap --yes` for each
configured Beads repository so fresh clones have a writable Beads database, not
only tracked JSONL. On each Rocky heartbeat or lease renewal, the control plane
polls every enabled registered repository whose poll interval has elapsed. The
poller runs `bd ready --json` when available, falling back to `.beads/issues.jsonl`
parsing for simple local fixtures. Only `open` Beads with no active blockers are
imported; blocked Beads wait until their blockers close. Imports are idempotent
through the `project_items(source, external_id)` unique key.

Useful operator commands:

```bash
mac --db ~/.mac/mac.db bridge beads repos
mac --db ~/.mac/mac.db bridge beads poll --force
```

Imported tasks keep Beads provenance in `task.metadata.origin` and
`task.metadata.acc_metadata`, use the repository source as their mac project,
and are immediately eligible for normal worker claiming.

## AgentBus

`/messages` remains the constrained structured control bus and still rejects
execution-shaped payloads. Agent-to-agent content exchange uses `/agentbus`
instead:

- `POST /agentbus/streams` opens a typed stream with `content_type`, `topic`,
  optional task linkage, and optional recipient.
- `POST /agentbus/streams/{id}/chunks` appends ordered JSON, text, or base64
  chunks. `final=true` closes the stream atomically after the chunk.
- `GET /agentbus/streams/{id}/chunks` reads durable chunks after a sequence.
- `GET /agentbus/streams/{id}/events` tails chunks as NDJSON for streaming
  consumers.

This is a typed transport channel, not an execution channel. Agents must still
turn received content into explicit task/evidence/review actions through the
normal API.

mac-agent also listens for one constrained AgentBus control topic before it
claims tasks:

- Topic: `mac.repo.update.v1`
- Content type: `application/vnd.mac.repo-update+json`
- Payload schema: `mac.agentbus.repo_update.v1`

The listener runs `git pull --ff-only` in `MAC_SELF_UPDATE_REPO` and exits for
service-manager restart only when the worktree HEAD changes. Dirty worktrees,
non-git source trees, invalid remotes/branches, and pull failures are reported
as result streams instead of being forced. Result streams use topic
`mac.repo.update.result.v1` and content type
`application/vnd.mac.repo-update-result+json`.

To broadcast a source update from the hub:

```bash
mac --db ~/.mac/mac.db agentbus repo-update agent_rocky --all-agents
```

## Docker / Podman

```bash
docker build -t mac:latest .

docker run -d --name mac \
    -e MAC_SECRET_KEY="$(openssl rand -base64 48)" \
    -e MAC_API_TOKEN="$(openssl rand -hex 32)" \
    -v mac-data:/var/lib/mac \
    -p 127.0.0.1:8000:8000 \
    --restart unless-stopped \
    mac:latest

# Healthcheck is built into the image; `docker ps` shows (healthy) once up.
curl -fsS http://127.0.0.1:8000/health
```

For Kubernetes, ship the same image as a single-replica `Deployment` with a
PVC mounted at `/var/lib/mac`. Use a `ConfigMap` for non-secret env and a
`Secret` for `MAC_SECRET_KEY` / `MAC_API_TOKEN`.

## Backups

`mac.db` is a SQLite WAL database. Snapshot with SQLite's online backup:

```bash
sqlite3 /var/lib/mac/mac.db ".backup '/backups/mac-$(date +%Y%m%dT%H%M%SZ).db'"
```

WAL means a plain `cp` is unsafe (you'll miss the WAL file or copy
inconsistent state). The `.backup` command coordinates with the running
process. Restore is a file copy while the service is stopped.

## Observability

- `GET /health` is the liveness/readiness signal. Returns `{"status":"ok"}`.
- `GET /startup/hermes` returns the redacted Hermes startup report: file
  existence/size/mtime metadata, warning strings, and Slack activation status.
- `GET /events` is the unified audit stream — point a log shipper (vector,
  promtail, fluent-bit) at it with `since=` advancing every poll, or scrape
  the SQLite tables directly.
- `mac --db /var/lib/mac/mac.db events list --since <iso>` is the operator's
  one-shot "what just happened" query.
- `POST /observability/metrics` and `POST /observability/logs` ingest
  layer/source/name/level observations from workers, Hermes adapters, deploy
  scripts, and external monitors. POST requires `agent` scope when API tokens
  are enabled.
- `GET /observability` lists persisted observations; `GET
  /observability/summary` returns dashboard aggregates and latest metric
  snapshots; `GET /observability/stream` tails observations as NDJSON for live
  dashboards or collectors.

The FastAPI middleware records per-request `http.request.duration_ms` metrics
and `http.request` logs. Control-plane task, agent, secret, environment,
rollout, and eval events are mirrored into the observability stream with their
original subject ids. The dashboard Observability tab uses the summary endpoint
and an NDJSON subscription to visualize the live stream.

## Upgrade procedure

1. Stop the service.
2. Snapshot `mac.db` (the `.backup` command above).
3. Install the new wheel / pull the new image.
4. Start the service. The schema migrator (`store._migrate`) is additive only
   — new columns get `_ensure_column`'d; old data survives.
5. Verify `GET /health` and a recent `GET /events` query.

If a migration fails, restore the snapshot and pin the prior version. The
project does not yet support downgrades through schema deletes.

## Known limitations

- Single-writer SQLite. See topology note above.
- No built-in TLS. Put a reverse proxy in front.
- `MAC_SECRET_KEY` rotation is manual.
