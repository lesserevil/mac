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
committed fleet configs set `MAC_DEPLOY_TARGET`, `MAC_DEPLOY_OS`, and
`MAC_HERMES_SLACK_HOME_CHANNEL_NAME`; host-local secret env files still own
tokens. Override `MAC_DEPLOY_AGENT_CONFIG_DIR` to test an alternate config set.

It ships this repository to each host, installs `mac` into `~/.mac/venv`,
redeploys upstream `NousResearch/hermes-agent` into `~/.mac/hermes-agent`,
applies the minimal multi-Slack Hermes patch, preinstalls configured Hermes
messaging dependencies before service start, runs the ACC SQLite migration
dry-run and import from `~/.acc/data/fleet.db` or `~/.acc/data/acc.db`, and
starts a local `mac` service on `127.0.0.1:8789`. Linux hosts get
`mac.service`; macOS hosts get `com.mac.control-plane`. The same deployment
also starts a mac-managed Hermes gateway from the upstream checkout:
`mac-hermes-gateway.service` on Linux and `com.mac.hermes-gateway` on macOS.
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
heartbeats, then claims eligible open work with a real executor:

```bash
mac-agent --url http://127.0.0.1:8789 --register \
  --agent-name rocky --hostname rocky.local \
  --capabilities python,ops --resources '{"capacity":2}' \
  --heartbeat-only

mac-agent --url http://127.0.0.1:8789 --register \
  --agent-name rocky --capabilities python,ops \
  --workspace ~/.mac-agent/workspaces --loop \
  --executor -- hermes run-once
```

Use `--heartbeat-only` during deploy validation when you want fleet visibility
without claiming migrated ACC work. Start the `--loop` form only after the
executor command is the intended production worker. Successful executions write
log evidence and move tasks to `needs_review`; failed executions fail the task
with evidence attached.

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

There is no built-in metrics endpoint yet. If you need Prometheus, wrap the
process with `prometheus-fastapi-instrumentator` in your own deployment;
upstream may grow this later.

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
- No metric ingestion endpoint. Track via the events stream or external
  scraping.
- `MAC_SECRET_KEY` rotation is manual.
