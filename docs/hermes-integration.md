# Hermes Integration

Phase 2 provides a thin Hermes-facing adapter around the `mac` HTTP API. The
adapter lives in `mac.hermes_adapter` and is exposed as the `mac-hermes` CLI.

The adapter is intentionally not a worker runtime. It gives Hermes enough API
surface to register itself, create durable tasks from sanitized conversation
context, fetch concise task status for user replies, and prepare completed-task
memory write-back payloads.

## What Is Implemented

- `MacApiClient`: small standard-library HTTP client for the `mac` API.
- `HermesMacAdapter`: high-level helper for Hermes skills or gateway code.
- Idempotent identity registration for tenants, personas, Hermes instances,
  and platform bindings.
- Conversation-to-task creation that stores only sanitized operational context.
- `GET /tasks/{id}/summary` for user-facing status summaries.
- Completed-task memory write-back payloads targeted at the Hermes
  `memory_scope`.
- Optional operational record that a write-back was prepared or sent.

## Registration

Hermes should register its durable identity on startup:

```python
from mac.hermes_adapter import HermesMacAdapter, MacApiClient, PlatformBindingSpec

adapter = HermesMacAdapter(MacApiClient("http://127.0.0.1:8000"))
registration = adapter.register_identity(
    tenant_name="personal",
    persona_name="Rocky",
    instance_name="rocky",
    soul_ref="hermes://personal/rocky/SOUL.md",
    memory_scope="hermes://personal/rocky/memory",
    platform_bindings=[
        PlatformBindingSpec("slack", "T123/C456", "#ops"),
    ],
)
```

The returned `hermes_instance.id` is the durable identity used for later task
creation.

## Creating Tasks

Hermes should summarize the user request and pass sanitized context:

```python
from mac.hermes_adapter import ConversationTaskInput

task = adapter.create_task_from_conversation(
    registration["hermes_instance"]["id"],
    ConversationTaskInput(
        title="Investigate failed deploy",
        summary="Deploy failed after the package publish step.",
        platform_binding_id=registration["platform_bindings"][0]["id"],
        conversation_ref="slack://T123/C456/1712345678.000100",
        required_capabilities=["ops"],
        snippets=["User-visible error: publish returned 500"],
    ),
)
```

Do not pass raw transcripts, private memory, provider tokens, or full
`MEMORY.md` contents. The adapter drops obvious secret and private-memory
metadata keys, but Hermes should still choose a minimal task brief.

## User Replies

Hermes can poll a durable task summary:

```python
reply = adapter.user_reply_for_task(task["id"])
```

The reply is intentionally concise so Hermes can render it in the personality
and tone of the active agent.

## Memory Write-Back

When a task completes, Hermes may write a result into its own memory:

```python
result = adapter.write_completed_task_to_memory(
    registration["hermes_instance"]["id"],
    task["id"],
    sink=lambda payload: hermes_memory_write(payload["memory_scope"], payload["content"]),
)
```

The `sink` is supplied by Hermes because Hermes owns memory storage. `mac`
records only an operational memory record that the write-back was prepared.

## Contract

The adapter must call `mac` APIs. It must not import `ControlPlane` or mutate
SQLite directly in production gateway code. Tests may use an in-process FastAPI
transport to verify the same API contract without opening a port.
