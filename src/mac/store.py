from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence


class SQLiteStore:
    """Durable SQLite backing store for the control plane."""

    def __init__(self, path: str = "mac.db") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        # Autocommit semantics: SQLite commits a single statement on its own.
        # Inside an explicit transaction() block, statements run as part of that
        # transaction instead.
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    handle TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, handle)
                );
                CREATE INDEX IF NOT EXISTS idx_users_tenant
                    ON users (tenant_id);

                CREATE TABLE IF NOT EXISTS personas (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    soul_ref TEXT NOT NULL,
                    memory_scope TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_personas_tenant
                    ON personas (tenant_id);

                CREATE TABLE IF NOT EXISTS hermes_instances (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
                    home_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(tenant_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_hermes_instances_tenant
                    ON hermes_instances (tenant_id);

                CREATE TABLE IF NOT EXISTS platform_bindings (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    hermes_instance_id TEXT NOT NULL REFERENCES hermes_instances(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, platform, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance
                    ON platform_bindings (hermes_instance_id);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    project TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    required_capabilities TEXT NOT NULL,
                    dependencies TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    owner_agent_id TEXT,
                    lease_id TEXT,
                    leased_until TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_state_priority
                    ON tasks (state, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_owner
                    ON tasks (owner_agent_id);

                CREATE TABLE IF NOT EXISTS task_history (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_history_task_created
                    ON task_history (task_id, created_at);

                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    checksum TEXT,
                    metadata TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_task
                    ON evidence (task_id);

                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_leases_task_status
                    ON leases (task_id, status);
                CREATE INDEX IF NOT EXISTS idx_leases_agent_status
                    ON leases (agent_id, status);

                CREATE TABLE IF NOT EXISTS machines (
                    id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    resources TEXT NOT NULL,
                    trusted INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    resources TEXT NOT NULL,
                    status TEXT NOT NULL,
                    health_status TEXT NOT NULL,
                    current_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agents_status_health
                    ON agents (status, health_status);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender_agent_id TEXT NOT NULL,
                    recipient_agent_id TEXT,
                    task_id TEXT,
                    message_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
                    ON messages (recipient_agent_id, status);

                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    reviewer_agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    evidence_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reviews_task_status
                    ON reviews (task_id, status);

                CREATE TABLE IF NOT EXISTS publications (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secrets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    rotated_at TEXT,
                    enabled INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secret_access_audit (
                    id TEXT PRIMARY KEY,
                    secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE CASCADE,
                    accessor_agent_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    result TEXT NOT NULL,
                    expires_at TEXT,
                    revealed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_secret_audit_secret_created
                    ON secret_access_audit (secret_id, created_at);

                CREATE TABLE IF NOT EXISTS runtime_environments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    manifest TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL REFERENCES runtime_environments(id),
                    status TEXT NOT NULL,
                    evidence_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_items (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );

                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT,
                    record_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_task_created
                    ON memory_records (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_memory_subject
                    ON memory_records (subject_type, subject_id);

                CREATE TABLE IF NOT EXISTS rollouts (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_percent INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rollout_events (
                    id TEXT PRIMARY KEY,
                    rollout_id TEXT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()
