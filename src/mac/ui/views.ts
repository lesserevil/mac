// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import { TASK_STATES } from "./constants.js";
import { mustData, state } from "./state.js";
import type { AgentItem, ApiRecord, DashboardData, HermesStartup, JsonObject, OperatorNotification, RolloutStatus, TaskDetail } from "./types.js";
import {
  agentSelect,
  attentionList,
  chip,
  commandAuditRecord,
  escapeHtml,
  field,
  formatAge,
  healthTone,
  jsonSummary,
  labelize,
  metric,
  observationMetric,
  observationRecord,
  observationTone,
  option,
  rolloutTone,
  select,
  shortHash,
  stateBars,
  statusTone,
  taskOrigin,
  timelineItem,
  uniqueObservations,
} from "./utils.js";

export function renderOverview(): string {
  const data = mustData();
  const counts = data.overview.counts;
  const startup = data.hermes_startup;
  const startupStatus = startup?.operator_health?.status || (startup?.ready ? "healthy" : "degraded");
  return `
    <section class="metric-grid">
      ${metric("Agents", counts.agents || 0, `${counts.healthy_agents || 0} healthy, ${counts.busy_agents || 0} busy`)}
      ${metric("Machines", counts.machines || 0, `${counts.trusted_machines || 0} trusted`)}
      ${metric("Active Tasks", counts.active_tasks || 0, `${counts.dead_letters || 0} dead letters`)}
      ${metric("Hermes", counts.hermes_instances || 0, `${startupStatus}, ${counts.platform_bindings || 0} bindings`)}
    </section>
    <section class="surface">
      <h2>Dispatch</h2>
      <form class="action-form compact" data-action="dispatchTick">
        <label>Lease seconds <input name="lease_seconds" type="number" value="900" min="1"></label>
        <label>Limit <input name="limit" type="number" value="100" min="1"></label>
        <label>Stale after <input name="stale_after_seconds" type="number" placeholder="optional"></label>
        <button type="submit">Run Tick</button>
      </form>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Task States</h2>
        ${stateBars(TASK_STATES, data.overview.task_states, data.tasks.length)}
      </div>
      <div class="surface">
        <h2>Attention</h2>
        ${attentionList(data)}
      </div>
    </section>
  `;
}

export function renderAgents(): string {
  const data = mustData();
  const query = state.agentQuery.trim().toLowerCase();
  const agents = data.agents.filter((item) => {
    const haystack = [
      item.agent.name,
      item.agent.id,
      item.machine?.hostname || "",
      item.agent.status,
      item.agent.health_status,
      ...(item.agent.capabilities || []),
    ].join(" ").toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    const matchesFilter =
      state.agentFilter === "all" ||
      (state.agentFilter === "eligible" && item.availability.eligible) ||
      (state.agentFilter === "blocked" && !item.availability.eligible) ||
      item.agent.status === state.agentFilter ||
      item.agent.health_status === state.agentFilter;
    return matchesQuery && matchesFilter;
  });
  return `
    <section class="toolbar">
      <input id="agentSearch" type="search" placeholder="Search agents, hosts, capabilities" value="${escapeHtml(state.agentQuery)}">
      <select id="agentFilter">
        ${option("all", "All agents", state.agentFilter)}
        ${option("eligible", "Eligible", state.agentFilter)}
        ${option("blocked", "Blocked", state.agentFilter)}
        ${option("idle", "Idle", state.agentFilter)}
        ${option("busy", "Busy", state.agentFilter)}
        ${option("draining", "Draining", state.agentFilter)}
        ${option("offline", "Offline", state.agentFilter)}
        ${option("degraded", "Degraded", state.agentFilter)}
        ${option("unhealthy", "Unhealthy", state.agentFilter)}
      </select>
      <button type="button" id="clearAgentFilters">Clear</button>
    </section>
    <section class="agent-list">
      ${agents.length ? agents.map(agentCard).join("") : `<div class="empty-state">No matching agents</div>`}
    </section>
  `;
}

export function renderTasks(): string {
  const data = mustData();
  const tasks = state.taskFilter === "all"
    ? data.tasks
    : data.tasks.filter((detail) => detail.task.state === state.taskFilter);
  return `
    <section class="toolbar">
      <select id="taskFilter">
        ${option("all", "All states", state.taskFilter)}
        ${TASK_STATES.map((taskState) => option(taskState, labelize(taskState), state.taskFilter)).join("")}
      </select>
      <button type="button" id="clearTaskFilter">Clear</button>
    </section>
    <section class="task-lanes">
      ${TASK_STATES.filter((taskState) => state.taskFilter === "all" || state.taskFilter === taskState)
        .map((taskState) => taskLane(taskState, tasks, data.agents))
        .join("")}
    </section>
  `;
}

export function renderHermes(): string {
  const data = mustData();
  return `
    <section class="metric-grid">
      ${metric("Tenants", data.tenants.length, `${data.users.length} users`)}
      ${metric("Personas", data.personas.length, "soul refs only")}
      ${metric("Instances", data.hermes_instances.length, `${data.platform_bindings.length} bindings`)}
      ${metric("Interaction Tasks", data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id).length, "from Hermes")}
    </section>
    ${hermesStartupPanel(data.hermes_startup)}
    <section class="record-list">
      ${data.hermes_instances.length ? data.hermes_instances.map((instance) => hermesRecord(instance, data)).join("") : `<div class="empty-state">No Hermes instances</div>`}
    </section>
  `;
}

function hermesStartupPanel(startup?: HermesStartup | null): string {
  if (!startup) {
    return `<section class="surface"><h2>Startup Health</h2><div class="empty-state">No startup report</div></section>`;
  }
  const operator = startup.operator_health || {};
  const security = (startup.security?.secret_redaction || {}) as JsonObject;
  const slack = startup.slack || {};
  const logs = startup.logs || {};
  const warnings = startup.warnings || [];
  return `
    <section class="surface">
      <h2>Startup Health</h2>
      <div class="chip-row">
        ${chip(String(operator.status || (startup.ready ? "healthy" : "degraded")), startup.ready ? "good" : "bad")}
        ${chip(`redaction ${security.effective === false ? "off" : "on"}`, security.effective === false ? "bad" : "good")}
        ${chip(`logs ${Number(logs.actionable_count || 0)}`, Number(logs.actionable_count || 0) ? "bad" : "good")}
      </div>
      <div class="row-grid">
        ${field("State refs", operator.state_refs_existing ?? 0)}
        ${field("Slack activation", slack.activation_source || operator.slack_activation_source || "unknown")}
        ${field("Redaction source", security.source || "unknown")}
        ${field("Log classes", (logs.classes as unknown[] | undefined)?.length ?? 0)}
      </div>
      ${warnings.length ? `<div class="timeline">${warnings.map((warning) => timelineItem("warning", warning, "")).join("")}</div>` : ""}
    </section>
  `;
}

export function renderRuntime(): string {
  const data = mustData();
  return `
    <section class="split">
      <div class="surface">
        <h2>Runtime Environments</h2>
        <div class="runtime-list">
          ${data.runtimes.length ? data.runtimes.map((runtime) => runtimeRecord(runtime, data)).join("") : `<div class="empty-state">No runtimes</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Rollouts</h2>
        <div class="rollout-list">
          ${data.rollouts.length ? data.rollouts.map((status) => rolloutRecord(status, data)).join("") : `<div class="empty-state">No rollouts</div>`}
        </div>
      </div>
    </section>
  `;
}

export function renderSecrets(): string {
  const data = mustData();
  return `
    <section class="split">
      <div class="surface">
        <h2>Secrets</h2>
        <div class="record-list">
          ${data.secrets.length ? data.secrets.map((secret) => secretRecord(secret, data.agents)).join("") : `<div class="empty-state">No secrets</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Access Audit</h2>
        <div class="record-list">
          ${data.secret_audits.length ? data.secret_audits.map(secretAuditRecord).join("") : `<div class="empty-state">No audit records</div>`}
        </div>
      </div>
    </section>
  `;
}

export function renderObservability(): string {
  const data = mustData();
  const observability = data.observability || {
    counts: {},
    levels: {},
    layers: {},
    latest: [],
    latest_metrics: [],
  };
  const counts = observability.counts || {};
  const commandAudit = data.command_audit || [];
  const notifications = data.notifications || [];
  const pendingNotifications = notifications.filter((item) => item.status === "pending").length;
  const live = uniqueObservations([...state.observabilityLive, ...(observability.latest || [])]);
  const layerTotal = Object.values(observability.layers || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  const levelTotal = Object.values(observability.levels || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  return `
    <section class="metric-grid">
      ${metric("Observations", counts.events || 0, `${counts.logs || 0} logs, ${counts.metrics || 0} metrics`)}
      ${metric("Warnings", counts.warnings || 0, "warning observations")}
      ${metric("Errors", counts.errors || 0, "error observations")}
      ${metric("Notifications", notifications.length, `${pendingNotifications} pending`)}
      ${metric("Stream", state.observabilityStreamStatus, `${state.observabilityLive.length} live item(s)`)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Metric Snapshot</h2>
        <div class="metric-list">
          ${(observability.latest_metrics || []).length
            ? observability.latest_metrics.map(observationMetric).join("")
            : `<div class="empty-state">No metrics</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Distribution</h2>
        ${stateBars(Object.keys(observability.layers || {}).sort(), observability.layers || {}, layerTotal, "No layers")}
        ${stateBars(Object.keys(observability.levels || {}).sort(), observability.levels || {}, levelTotal, "No levels")}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Notifications</h2>
        ${chip(`${pendingNotifications} pending`, pendingNotifications ? "warn" : "good")}
      </div>
      <div class="observability-feed">
        ${notifications.length ? notifications.slice(0, 80).map(notificationRecord).join("") : `<div class="empty-state">No notifications</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Command Audit</h2>
        ${chip(`${commandAudit.length}`, commandAudit.length ? "info" : "warn")}
      </div>
      <div class="observability-feed">
        ${commandAudit.length ? commandAudit.slice(0, 80).map(commandAuditRecord).join("") : `<div class="empty-state">No command audit records</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Live Stream</h2>
        ${chip(state.observabilityStreamStatus, state.observabilityStreamStatus === "connected" ? "good" : state.observabilityStreamStatus === "error" ? "bad" : "info")}
      </div>
      <div class="observability-feed">
        ${live.length ? live.slice(0, 80).map(observationRecord).join("") : `<div class="empty-state">No observations</div>`}
      </div>
    </section>
  `;
}

function notificationRecord(item: OperatorNotification): string {
  return `
    <article class="feed-item">
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.body)}</p>
        <p class="muted small">${escapeHtml(item.event_type)} · ${escapeHtml(item.created_at)}</p>
      </div>
      <div class="chip-row">
        ${chip(item.status, item.status === "pending" ? "warn" : item.status === "failed" ? "bad" : "good")}
        ${(item.channels || []).map((channel) => chip(channel, "info")).join("")}
      </div>
    </article>
  `;
}

function agentCard(item: AgentItem): string {
  const agent = item.agent;
  const machine = item.machine;
  const reasons = item.availability.eligible
    ? chip("dispatch eligible", "good")
    : item.availability.reasons.map((reason) => chip(reason, "bad")).join("");
  return `
    <article class="agent-card ${item.availability.eligible ? "" : "is-blocked"}">
      <div class="agent-header">
        <div><h2 class="mono">${escapeHtml(agent.name)}</h2><p class="muted small">${escapeHtml(agent.id)}</p></div>
        <div class="chip-row">${chip(agent.status, statusTone(agent.status))}${chip(agent.health_status, healthTone(agent.health_status))}</div>
      </div>
      <div class="row-grid">
        ${field("Machine", machine?.hostname || "missing")}
        ${field("Trusted", machine?.trusted ? "yes" : "no")}
        ${field("Last seen", formatAge(agent.last_seen_at))}
        ${field("Capacity", `${item.active_lease_count} / ${item.capacity}`)}
        ${field("Current task", item.active_tasks[0]?.title || agent.current_task_id || "none")}
        ${field("Capabilities", (agent.capabilities || []).join(", ") || "none")}
        ${field("Resources", jsonSummary(agent.resources))}
        ${field("Machine resources", jsonSummary(machine?.resources))}
      </div>
      <div class="chip-row">${reasons}</div>
    </article>
  `;
}

function taskLane(taskState: string, tasks: TaskDetail[], agents: AgentItem[]): string {
  const laneTasks = tasks.filter((detail) => detail.task.state === taskState);
  return `
    <div class="task-lane">
      <h2><span>${escapeHtml(labelize(taskState))}</span><span class="pill">${laneTasks.length}</span></h2>
      ${laneTasks.length ? laneTasks.map((detail) => taskCard(detail, agents)).join("") : `<div class="empty-state">Empty</div>`}
    </div>
  `;
}

function taskCard(detail: TaskDetail, agents: AgentItem[]): string {
  const task = detail.task;
  const owner = agents.find((item) => item.agent.id === task.owner_agent_id)?.agent;
  const origin = taskOrigin(task);
  const evidenceOptions = detail.evidence.map((item) => option(String(item.id), String(item.id), "")).join("");
  const pendingReviews = detail.reviews.filter((review) => review.status === "pending");
  return `
    <article class="task-card">
      <h3>${escapeHtml(task.title)}</h3>
      <p class="muted small mono">${escapeHtml(task.id)}</p>
      <div class="chip-row">
        ${chip(`P${task.priority || 0}`, "info")}
        ${chip(`${task.attempt_count || 0}/${task.max_attempts || 0} attempts`, (task.attempt_count || 0) >= (task.max_attempts || 1) ? "bad" : "good")}
        ${owner ? chip(owner.name, "info") : chip("unowned", "warn")}
        ${origin.hermes_instance_id ? chip("Hermes origin", "info") : ""}
      </div>
      <p class="small muted">${escapeHtml(String(detail.summary?.summary || ""))}</p>
      <div class="timeline">
        ${detail.history.slice(-3).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("")}
      </div>
      <details class="action-box">
        <summary>Task actions</summary>
        <form class="action-form compact" data-action="taskClaim" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", agents, task.owner_agent_id || "")}</label>
          <label>Lease seconds <input name="lease_seconds" type="number" value="900" min="1"></label>
          <button type="submit">Claim</button>
        </form>
        <form class="action-form compact" data-action="taskStart" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", agents, task.owner_agent_id || "")}</label>
          <button type="submit">Start</button>
        </form>
        <form class="action-form compact" data-action="taskSubmitReview" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", agents, task.owner_agent_id || "")}</label>
          <button type="submit">Submit Review</button>
        </form>
        <form class="action-form" data-action="taskTransition" data-task-id="${escapeHtml(task.id)}">
          <label>State ${select("target_state", TASK_STATES, task.state)}</label>
          <label>Actor <input name="actor" value="human"></label>
          <label>Detail JSON <textarea name="detail" placeholder="{}"></textarea></label>
          <button type="submit">Transition</button>
        </form>
        <form class="action-form" data-action="addEvidence" data-task-id="${escapeHtml(task.id)}">
          <label>Kind ${select("kind", ["test", "review", "artifact", "publication", "log", "eval"], "test")}</label>
          <label>URI <input name="uri" placeholder="artifact://..."></label>
          <label>Summary <input name="summary" placeholder="What this proves"></label>
          <label>Checksum <input name="checksum" placeholder="optional"></label>
          <label>Created by <input name="created_by" value="${escapeHtml(task.owner_agent_id || "human")}"></label>
          <button type="submit">Add Evidence</button>
        </form>
        <form class="action-form compact" data-action="requestReview" data-task-id="${escapeHtml(task.id)}">
          <label>Reviewer ${agentSelect("reviewer_agent_id", agents, "")}</label>
          <label>Actor <input name="actor" value="dispatcher"></label>
          <button type="submit">Request Review</button>
        </form>
        ${pendingReviews.map((review) => `
          <form class="action-form" data-action="reviewDecision" data-review-id="${escapeHtml(review.id)}">
            <label>Status ${select("status", ["approved", "changes_requested", "rejected"], "approved")}</label>
            <label>Reviewer <input name="reviewer_agent_id" value="${escapeHtml(review.reviewer_agent_id)}"></label>
            <label>Evidence <select name="evidence_id"><option value="">None</option>${evidenceOptions}</select></label>
            <label>Reason <input name="reason" placeholder="optional"></label>
            <button type="submit">Submit Review</button>
          </form>`).join("")}
        <form class="action-form compact" data-action="publishTask">
          <input type="hidden" name="task_id" value="${escapeHtml(task.id)}">
          <label>Target <input name="target" placeholder="release://..."></label>
          <label>Created by <input name="created_by" value="human"></label>
          <label>Evidence <select name="evidence_id"><option value="">None</option>${evidenceOptions}</select></label>
          <button type="submit">Publish</button>
        </form>
      </details>
    </article>
  `;
}

function hermesRecord(instance: ApiRecord, data: DashboardData): string {
  const tenant = data.tenants.find((item) => item.id === instance.tenant_id);
  const persona = data.personas.find((item) => item.id === instance.persona_id);
  const bindings = data.platform_bindings.filter((binding) => binding.hermes_instance_id === instance.id);
  const tasks = data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id === instance.id);
  return `
    <article class="record">
      <div class="record-header"><div><h2>${escapeHtml(instance.name)}</h2><p class="muted small mono">${escapeHtml(instance.id)}</p></div>${chip(instance.status, instance.status === "active" ? "good" : "warn")}</div>
      <div class="row-grid">
        ${field("Tenant", tenant?.name || instance.tenant_id)}
        ${field("Persona", persona?.name || "none")}
        ${field("Soul ref", persona?.soul_ref || "none")}
        ${field("Memory scope", persona?.memory_scope || "none")}
        ${field("Home", instance.home_ref || "none")}
        ${field("Bindings", bindings.length)}
        ${field("Interaction tasks", tasks.length)}
        ${field("Last seen", formatAge(String(instance.last_seen_at || "")))}
      </div>
      <div class="chip-row">${bindings.length ? bindings.map((binding) => chip(`${binding.platform}:${binding.display_name || binding.external_id}`, "info")).join("") : chip("no platform bindings", "warn")}</div>
    </article>
  `;
}

function runtimeRecord(runtime: ApiRecord, data: DashboardData): string {
  const rollouts = data.rollouts.filter((item) => item.rollout.runtime_environment_id === runtime.id);
  const runs = data.runtime_runs.filter((run) => run.environment_id === runtime.id);
  return `
    <article class="runtime-record">
      <div class="runtime-header"><div><h3>${escapeHtml(runtime.name)}</h3><p class="muted small mono">${escapeHtml(runtime.id)}</p></div>${chip(shortHash(runtime.digest), "good")}</div>
      <div class="row-grid">
        ${field("Created by", runtime.created_by)}
        ${field("Created", formatAge(String(runtime.created_at || "")))}
        ${field("Rollouts", rollouts.length)}
        ${field("Runs", runs.length)}
        ${field("Manifest", jsonSummary(runtime.manifest))}
      </div>
    </article>
  `;
}

function rolloutRecord(status: RolloutStatus, data: DashboardData): string {
  const rollout = status.rollout;
  const evalSet = data.eval_sets.find((item) => item.id === rollout.required_eval_set_id);
  return `
    <article class="rollout-record">
      <div class="rollout-header"><div><h3>${escapeHtml(rollout.version)}</h3><p class="muted small mono">${escapeHtml(rollout.id)}</p></div>${chip(rollout.status, rolloutTone(String(rollout.status)))}</div>
      <div class="row-grid">
        ${field("Strategy", rollout.strategy)}
        ${field("Target", `${rollout.target_percent}%`)}
        ${field("Channel", rollout.channel)}
        ${field("Runtime", status.runtime?.name || "none")}
        ${field("Artifact", rollout.artifact_hash || "unverified")}
        ${field("Eval gate", evalSet?.name || "none")}
        ${field("Latest eval", status.latest_eval_run ? `${status.latest_eval_run.score} ${status.latest_eval_run.passed ? "pass" : "fail"}` : "none")}
        ${field("Health policy", jsonSummary(rollout.health_policy))}
      </div>
      <div class="timeline">${status.events.slice(-4).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("")}</div>
      <details class="action-box">
        <summary>Rollout actions</summary>
        <form class="action-form" data-action="rolloutAdvance" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Action ${select("action", ["start_canary", "promote", "pause", "resume", "rollback"], "start_canary")}</label>
          <label>Actor <input name="actor" value="human"></label>
          <label>Detail JSON <textarea name="detail" placeholder="{}"></textarea></label>
          <button type="submit">Advance</button>
        </form>
        <form class="action-form compact" data-action="rolloutHealth" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Actor <input name="actor" value="monitor"></label>
          <label>Checks JSON <textarea name="checks" placeholder='{"runtime":"healthy"}'></textarea></label>
          <button type="submit">Record Health</button>
        </form>
        <form class="action-form compact danger-action" data-action="rolloutRescue" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Actor <input name="actor" value="human"></label>
          <label>Reason <input name="reason" placeholder="why rescue is needed"></label>
          <button type="submit">Rescue</button>
        </form>
      </details>
    </article>
  `;
}

function secretRecord(secret: ApiRecord, agents: AgentItem[]): string {
  return `
    <article class="record">
      <div class="record-header"><div><h3>${escapeHtml(secret.name)}</h3><p class="muted small mono">${escapeHtml(secret.id)}</p></div>${chip(secret.enabled ? "enabled" : "disabled", secret.enabled ? "good" : "bad")}</div>
      <div class="row-grid">
        ${field("Value", "***REDACTED***")}
        ${field("Scopes", jsonSummary(secret.scopes))}
        ${field("Created by", secret.created_by)}
        ${field("Rotated", secret.rotated_at || "never")}
      </div>
      <form class="action-form compact" data-action="secretAccess" data-secret-id="${escapeHtml(secret.id)}">
        <label>Accessor ${agentSelect("accessor_agent_id", agents, "")}</label>
        <label>Purpose <input name="purpose" placeholder="deploy, test, audit"></label>
        <label>TTL seconds <input name="ttl_seconds" type="number" value="300" min="1"></label>
        <button type="submit">Request Handle</button>
      </form>
    </article>
  `;
}

function secretAuditRecord(audit: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(audit.result)}</h3><p class="muted small mono">${escapeHtml(audit.id)}</p></div>${chip(audit.result, audit.result === "granted" ? "good" : audit.result === "denied" ? "bad" : "warn")}</div>
      <div class="row-grid">
        ${field("Secret", audit.secret_id)}
        ${field("Accessor", audit.accessor_agent_id)}
        ${field("Purpose", audit.purpose)}
        ${field("Expires", audit.expires_at || "none")}
        ${field("Revealed", audit.revealed_at || "not revealed")}
        ${field("Created", formatAge(String(audit.created_at || "")))}
      </div>
    </article>
  `;
}
