# Scaling Plan

`mac` should scale from one personal fleet to many users, many Hermes
instances, and many worker pools without losing the clean control-plane shape.

## Principles

1. Keep the control plane boring.
   Durable state, permissions, routing, evidence, and audit trails should be
   easy to inspect and easy to test.

2. Keep personality out of the control plane.
   Hermes or another agent runtime owns souls, memory, conversation style, and
   adaptive behavior.

3. Make identity explicit.
   Every task that comes from a conversation should carry tenant, Hermes
   instance, persona, optional user, optional platform binding, and
   conversation reference metadata.

4. Do not copy private memory into work.
   Hermes should pass a task brief, constraints, and relevant sanitized context,
   not its raw user memory or private conversation history.

5. Share fleet capacity through policy.
   Agents and machines can serve many tenants only when capability,
   visibility, secret, and runtime policies allow it.

## Phases

### Phase 1: Local Control Plane

Status: implemented in the local prototype.

- SQLite storage.
- One FastAPI process.
- One CLI.
- Durable task/review/secret/runtime/rollout contracts.
- Hermes identity records and interaction task metadata.

This phase proves the clean contracts without operational complexity.

### Phase 2: Real Hermes Integration

Status: implemented as a thin API adapter, ready for Hermes-side wiring.

- `mac.hermes_adapter` registers tenant/persona/instance/platform bindings.
- `ConversationTaskInput` creates tasks from sanitized context.
- `GET /tasks/{id}/summary` and adapter reply rendering provide user-facing
  status summaries.
- Completed tasks can produce a Hermes memory write-back payload and record the
  operational write-back event in `mac`.

The adapter calls `mac` APIs. It does not bypass the task ledger. A production
Hermes gateway still needs to import or vendor this adapter and connect the
memory `sink` to Hermes' actual memory writer.

### Phase 3: Multi-Tenant Policy

Status: implemented as local policy gates.

Harden the identity model:

- API authentication and scoped tokens are available through `MAC_API_TOKEN`,
  `MAC_API_TOKENS`, or explicit `create_app(auth_tokens=...)`.
- Tenant-scoped task visibility uses Hermes interaction origin metadata.
- Secret leases are scoped by tenant, agent, machine policy, and capability.
- Machine pools use `labels.tenant_policy` with shared/private/denied behavior.
- Platform binding ownership checks prevent cross-tenant or cross-instance use.

### Phase 4: Fleet-Scale Dispatch

Status: implemented as deterministic local dispatch policy.

Improve dispatch without changing the core task contract:

- Heartbeat freshness can mark stale agents offline during `tick`.
- Agent resources can declare `capacity` / `max_concurrent_tasks`.
- Dispatch round-robins tenants inside a tick while preserving per-tenant priority.
- Task metadata can declare numeric/list/exact `resources` requirements.
- Expired leases retry until `max_attempts`; exhausted tasks appear in
  `/dispatch/dead-letters`.

### Phase 5: Review, Evidence, and Publication Hardening

Status: implemented.

Move from prototype gates to production gates:

- Reviewer independence from current or prior task owners is enforced.
- Approved reviews must reference task evidence.
- Evidence kinds are explicit: `test`, `review`, `artifact`, `publication`, `log`.
- Publications carry `content_hash` from publication evidence when provided.
- Tasks with `metadata.policy.require_publication_evidence` cannot publish
  without publication evidence and checksum.

### Phase 6: Rollout and Rescue

Make self-update safe:

- Runtime manifests with strict pins and content digests.
- Canary rollout health gates.
- Automatic pause/rescue task creation on failed health checks.
- Per-tenant or per-fleet rollout channels.
- Artifact verification before install or promotion.

## Near-Term Contract Tests

The next tests should make these risks explicit:

- API calls require scoped identity for mutating routes.
- A worker cannot approve its own task.
- Completion requires an approved review with evidence.
- Two dispatchers cannot claim the same task concurrently.
- A Hermes interaction task cannot use a user or platform binding from another tenant.
- Secret handles cannot be revealed without a granted audit.
- Runtime manifests reject unpinned images and version ranges.

## Success Definition

A user can talk to a remembered Hermes identity. Hermes can turn intent into a
durable `mac` task. The fleet can execute, review, and publish the work. Hermes
can report the outcome in the user's conversation and update its own memory.

At no point should private personality memory become a fleet-wide operational
secret, and at no point should fleet work depend on unrecorded chat state.
