// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
type ViewKey = "overview" | "agents" | "tasks" | "hermes" | "runtime" | "secrets";
type Tone = "good" | "warn" | "bad" | "info";
type JsonObject = Record<string, unknown>;

interface ApiRecord {
  id: string;
  [key: string]: unknown;
}

interface AgentRecord extends ApiRecord {
  name: string;
  machine_id: string;
  capabilities?: string[];
  resources?: JsonObject;
  status: string;
  health_status: string;
  current_task_id?: string | null;
  last_seen_at?: string;
}

interface MachineRecord extends ApiRecord {
  hostname: string;
  trusted: boolean;
  labels?: JsonObject;
  resources?: JsonObject;
}

interface TaskRecord extends ApiRecord {
  title: string;
  state: string;
  priority?: number;
  required_capabilities?: string[];
  metadata?: JsonObject;
  owner_agent_id?: string | null;
  leased_until?: string | null;
  attempt_count?: number;
  max_attempts?: number;
}

interface TaskDetail {
  task: TaskRecord;
  history: ApiRecord[];
  evidence: ApiRecord[];
  reviews: ApiRecord[];
  publications: ApiRecord[];
  summary?: JsonObject;
}

interface AgentItem {
  agent: AgentRecord;
  machine: MachineRecord | null;
  active_tasks: TaskRecord[];
  capacity: number;
  active_lease_count: number;
  availability: { eligible: boolean; reasons: string[] };
}

interface DispatchCandidate {
  agent_id: string;
  agent_name: string;
  eligible: boolean;
  reasons: string[];
}

interface DispatchTask {
  task: TaskRecord;
  tenant_id?: string | null;
  eligible_agent_count: number;
  candidates: DispatchCandidate[];
}

interface RolloutStatus {
  rollout: ApiRecord;
  runtime: ApiRecord | null;
  events: ApiRecord[];
  latest_eval_run: ApiRecord | null;
}

interface DashboardData {
  overview: {
    counts: Record<string, number>;
    task_states: Record<string, number>;
    agent_statuses: Record<string, number>;
  };
  tenants: ApiRecord[];
  users: ApiRecord[];
  personas: ApiRecord[];
  hermes_instances: ApiRecord[];
  platform_bindings: ApiRecord[];
  machines: MachineRecord[];
  agents: AgentItem[];
  tasks: TaskDetail[];
  dead_letters: TaskRecord[];
  dispatch: { open_task_count: number; tasks: DispatchTask[] };
  messages: ApiRecord[];
  secrets: ApiRecord[];
  secret_audits: ApiRecord[];
  runtimes: ApiRecord[];
  runtime_runs: ApiRecord[];
  rollouts: RolloutStatus[];
  eval_sets: ApiRecord[];
  eval_runs: ApiRecord[];
}

interface DashboardState {
  activeView: ViewKey;
  token: string;
  loading: boolean;
  loadedAt: Date | null;
  data: DashboardData | null;
  error: string | null;
  actionMessage: string | null;
  agentQuery: string;
  agentFilter: string;
  taskFilter: string;
}

interface DashboardNodes {
  nav: HTMLElement;
  title: HTMLElement;
  banner: HTMLElement;
  content: HTMLElement;
  refresh: HTMLButtonElement;
  syncState: HTMLElement;
  tokenForm: HTMLFormElement;
  tokenInput: HTMLInputElement;
  clearToken: HTMLButtonElement;
}

const TOKEN_KEY = "mac.dashboard.token";
const TASK_STATES = [
  "open",
  "blocked",
  "claimed",
  "running",
  "needs_review",
  "reviewing",
  "completed",
  "failed",
  "cancelled",
];
const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);
const VIEW_TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  hermes: "Hermes",
  runtime: "Runtime",
  secrets: "Secrets",
};

const state: DashboardState = {
  activeView: "overview",
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  loading: false,
  loadedAt: null,
  data: null,
  error: null,
  actionMessage: null,
  agentQuery: "",
  agentFilter: "all",
  taskFilter: "all",
};

const nodes: DashboardNodes = {
  nav: requiredElement("#viewNav"),
  title: requiredElement("#viewTitle"),
  banner: requiredElement("#banner"),
  content: requiredElement("#content"),
  refresh: requiredElement("#refreshButton"),
  syncState: requiredElement("#syncState"),
  tokenForm: requiredElement("#tokenForm"),
  tokenInput: requiredElement("#tokenInput"),
  clearToken: requiredElement("#clearTokenButton"),
};

nodes.tokenInput.value = state.token;
bindEvents();
loadDashboard();

function bindEvents(): void {
  nodes.nav.addEventListener("click", (event) => {
    const button = (event.target as Element | null)?.closest<HTMLElement>("[data-view]");
    if (!button) return;
    state.activeView = (button.dataset.view || "overview") as ViewKey;
    state.actionMessage = null;
    render();
  });
  nodes.refresh.addEventListener("click", () => loadDashboard());
  nodes.content.addEventListener("submit", handleActionSubmit);
  nodes.tokenForm.addEventListener("submit", (event) => {
    event.preventDefault();
    state.token = nodes.tokenInput.value.trim();
    if (state.token) sessionStorage.setItem(TOKEN_KEY, state.token);
    else sessionStorage.removeItem(TOKEN_KEY);
    loadDashboard();
  });
  nodes.clearToken.addEventListener("click", () => {
    state.token = "";
    nodes.tokenInput.value = "";
    sessionStorage.removeItem(TOKEN_KEY);
    loadDashboard();
  });
}

async function loadDashboard(): Promise<void> {
  state.loading = true;
  state.error = null;
  renderSyncState();
  try {
    state.data = (await requestJSON("/dashboard/state")) as DashboardData;
    state.loadedAt = new Date();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  } finally {
    state.loading = false;
    render();
  }
}

async function requestJSON(path: string, init: RequestInit = {}): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail || detail;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return response.json();
}

function render(): void {
  document.querySelectorAll<HTMLElement>("[data-view]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === state.activeView);
  });
  nodes.title.textContent = VIEW_TITLES[state.activeView];
  renderSyncState();
  renderBanner();
  if (state.loading && !state.data) {
    nodes.content.innerHTML = `<div class="empty-state">Loading</div>`;
    return;
  }
  if (!state.data) {
    nodes.content.innerHTML = `<div class="empty-state">No dashboard data</div>`;
    return;
  }
  const action = state.actionMessage ? `<div class="action-status">${escapeHtml(state.actionMessage)}</div>` : "";
  const body =
    state.activeView === "agents"
      ? renderAgents()
      : state.activeView === "tasks"
        ? renderTasks()
        : state.activeView === "hermes"
          ? renderHermes()
          : state.activeView === "runtime"
            ? renderRuntime()
            : state.activeView === "secrets"
              ? renderSecrets()
              : renderOverview();
  nodes.content.innerHTML = `${action}${body}`;
  bindViewControls();
}

function renderSyncState(): void {
  nodes.syncState.textContent = state.loading
    ? "Loading"
    : state.loadedAt
      ? `Loaded ${formatTime(state.loadedAt)}`
      : "Not loaded";
}

function renderBanner(): void {
  if (!state.error) {
    nodes.banner.hidden = true;
    nodes.banner.textContent = "";
    return;
  }
  nodes.banner.hidden = false;
  nodes.banner.textContent = state.error.includes("403")
    ? "Dashboard data needs a token with read scope."
    : state.error;
}

function renderOverview(): string {
  const data = mustData();
  const counts = data.overview.counts;
  return `
    <section class="metric-grid">
      ${metric("Agents", counts.agents || 0, `${counts.healthy_agents || 0} healthy, ${counts.busy_agents || 0} busy`)}
      ${metric("Machines", counts.machines || 0, `${counts.trusted_machines || 0} trusted`)}
      ${metric("Active Tasks", counts.active_tasks || 0, `${counts.dead_letters || 0} dead letters`)}
      ${metric("Hermes", counts.hermes_instances || 0, `${counts.platform_bindings || 0} bindings`)}
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

function renderAgents(): string {
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

function renderTasks(): string {
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

function renderHermes(): string {
  const data = mustData();
  return `
    <section class="metric-grid">
      ${metric("Tenants", data.tenants.length, `${data.users.length} users`)}
      ${metric("Personas", data.personas.length, "soul refs only")}
      ${metric("Instances", data.hermes_instances.length, `${data.platform_bindings.length} bindings`)}
      ${metric("Interaction Tasks", data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id).length, "from Hermes")}
    </section>
    <section class="record-list">
      ${data.hermes_instances.length ? data.hermes_instances.map((instance) => hermesRecord(instance, data)).join("") : `<div class="empty-state">No Hermes instances</div>`}
    </section>
  `;
}

function renderRuntime(): string {
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

function renderSecrets(): string {
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

function bindViewControls(): void {
  const search = document.querySelector<HTMLInputElement>("#agentSearch");
  if (search) search.addEventListener("input", (event) => {
    state.agentQuery = (event.target as HTMLInputElement).value;
    render();
  });
  const agentFilter = document.querySelector<HTMLSelectElement>("#agentFilter");
  if (agentFilter) agentFilter.addEventListener("change", (event) => {
    state.agentFilter = (event.target as HTMLSelectElement).value;
    render();
  });
  const clearAgents = document.querySelector<HTMLButtonElement>("#clearAgentFilters");
  if (clearAgents) clearAgents.addEventListener("click", () => {
    state.agentQuery = "";
    state.agentFilter = "all";
    render();
  });
  const taskFilter = document.querySelector<HTMLSelectElement>("#taskFilter");
  if (taskFilter) taskFilter.addEventListener("change", (event) => {
    state.taskFilter = (event.target as HTMLSelectElement).value;
    render();
  });
  const clearTasks = document.querySelector<HTMLButtonElement>("#clearTaskFilter");
  if (clearTasks) clearTasks.addEventListener("click", () => {
    state.taskFilter = "all";
    render();
  });
}

async function handleActionSubmit(event: SubmitEvent): Promise<void> {
  const form = (event.target as Element | null)?.closest<HTMLFormElement>("form[data-action]");
  if (!form) return;
  event.preventDefault();
  const action = form.dataset.action || "";
  const values = formValues(form);
  try {
    const result = await runAction(action, form, values);
    state.actionMessage = `${labelize(action)} ok: ${redactedJson(result)}`;
    await loadDashboard();
  } catch (error) {
    state.actionMessage = `${labelize(action)} failed: ${error instanceof Error ? error.message : String(error)}`;
    render();
  }
}

async function runAction(action: string, form: HTMLFormElement, values: JsonObject): Promise<unknown> {
  if (action === "dispatchTick") {
    return postJSON("/dispatch/tick", {
      lease_seconds: numberValue(values.lease_seconds, 900),
      limit: numberValue(values.limit, 100),
      stale_after_seconds: optionalNumber(values.stale_after_seconds),
    });
  }
  if (action === "taskClaim") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/claim?agent_id=${encodeURIComponent(requiredString(values.agent_id))}&lease_seconds=${numberValue(values.lease_seconds, 900)}`, {});
  }
  if (action === "taskStart") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/start?agent_id=${encodeURIComponent(requiredString(values.agent_id))}`, {});
  }
  if (action === "taskSubmitReview") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/submit-for-review?agent_id=${encodeURIComponent(requiredString(values.agent_id))}`, {});
  }
  if (action === "taskTransition") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/transition`, {
      target_state: requiredString(values.target_state),
      actor: requiredString(values.actor),
      detail: parseJsonObject(values.detail),
    });
  }
  if (action === "addEvidence") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/evidence`, {
      kind: requiredString(values.kind),
      uri: requiredString(values.uri),
      summary: requiredString(values.summary),
      created_by: requiredString(values.created_by),
      checksum: emptyToNull(values.checksum),
      metadata: {},
    });
  }
  if (action === "requestReview") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/reviews`, {
      reviewer_agent_id: requiredString(values.reviewer_agent_id),
      actor: requiredString(values.actor),
    });
  }
  if (action === "reviewDecision") {
    return postJSON(`/reviews/${encodeURIComponent(requiredDataset(form, "reviewId"))}/decision`, {
      status: requiredString(values.status),
      reviewer_agent_id: requiredString(values.reviewer_agent_id),
      reason: emptyToNull(values.reason),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "publishTask") {
    return postJSON("/publications", {
      task_id: requiredString(values.task_id),
      target: requiredString(values.target),
      created_by: requiredString(values.created_by),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "rolloutAdvance") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/advance`, {
      action: requiredString(values.action),
      actor: requiredString(values.actor),
      detail: parseJsonObject(values.detail),
    });
  }
  if (action === "rolloutHealth") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/health`, {
      actor: requiredString(values.actor),
      checks: parseJsonObject(values.checks),
    });
  }
  if (action === "rolloutRescue") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/rescue`, {
      actor: requiredString(values.actor),
      reason: requiredString(values.reason),
      detail: {},
    });
  }
  if (action === "secretAccess") {
    return postJSON(`/secrets/${encodeURIComponent(requiredDataset(form, "secretId"))}/access`, {
      accessor_agent_id: requiredString(values.accessor_agent_id),
      purpose: requiredString(values.purpose),
      ttl_seconds: numberValue(values.ttl_seconds, 300),
    });
  }
  throw new Error(`unsupported action: ${action}`);
}

function postJSON(path: string, body: JsonObject): Promise<unknown> {
  return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
}

function formValues(form: HTMLFormElement): JsonObject {
  const values: JsonObject = {};
  new FormData(form).forEach((value, key) => {
    values[key] = String(value);
  });
  return values;
}

function metric(label: string, value: unknown, note: string): string {
  return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
}

function stateBars(states: string[], counts: Record<string, number>, total: number): string {
  if (!total) return `<div class="empty-state">No tasks</div>`;
  return `<div class="state-bar">${states.map((name) => {
    const count = counts[name] || 0;
    const width = Math.max(2, Math.round((count / total) * 100));
    return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
  }).join("")}</div>`;
}

function attentionList(data: DashboardData): string {
  const items = [
    ...data.agents.filter((item) => !item.availability.eligible).map((item) => `${item.agent.name}: ${item.availability.reasons.join(", ")}`),
    ...data.dead_letters.map((task) => `Dead letter: ${task.title}`),
    ...data.rollouts.filter((item) => ["rescuing", "failed"].includes(String(item.rollout.status))).map((item) => `Rollout ${item.rollout.version}: ${item.rollout.status}`),
    ...data.dispatch.tasks.filter((item) => item.eligible_agent_count === 0).map((item) => `No eligible agent: ${item.task.title}`),
  ];
  return items.length
    ? `<div class="record-list">${items.slice(0, 8).map((item) => `<div class="record compact">${escapeHtml(item)}</div>`).join("")}</div>`
    : `<div class="empty-state">No attention items</div>`;
}

function field(label: string, value: unknown): string {
  return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span></div>`;
}

function chip(value: unknown, tone: Tone = "info"): string {
  return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}

function timelineItem(eventType: string, actor: string, createdAt: string): string {
  return `<div class="timeline-item"><span class="mono small">${escapeHtml(labelize(eventType))}</span><br><span class="muted small">${escapeHtml(actor)} ${escapeHtml(formatAge(createdAt))}</span></div>`;
}

function agentSelect(name: string, agents: AgentItem[], selected: string): string {
  return `<select name="${escapeHtml(name)}"><option value="">Select agent</option>${agents.map((item) => option(item.agent.id, item.agent.name, selected)).join("")}</select>`;
}

function select(name: string, values: string[], selected: string): string {
  return `<select name="${escapeHtml(name)}">${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}

function option(value: string, label: string, selected: string): string {
  return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

function taskOrigin(task: TaskRecord): JsonObject {
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const origin = metadata.origin;
  return origin && typeof origin === "object" ? origin as JsonObject : {};
}

function mustData(): DashboardData {
  if (!state.data) throw new Error("dashboard data is not loaded");
  return state.data;
}

function parseJsonObject(value: unknown): JsonObject {
  const text = String(value || "").trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("expected a JSON object");
  }
  return parsed as JsonObject;
}

function requiredString(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) throw new Error("required field is blank");
  return text;
}

function requiredDataset(form: HTMLFormElement, key: string): string {
  const value = form.dataset[key];
  if (!value) throw new Error(`missing action context: ${key}`);
  return value;
}

function numberValue(value: unknown, fallback: number): number {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) throw new Error(`expected number: ${text}`);
  return parsed;
}

function optionalNumber(value: unknown): number | null {
  const text = String(value || "").trim();
  return text ? numberValue(text, 0) : null;
}

function emptyToNull(value: unknown): string | null {
  const text = String(value || "").trim();
  return text || null;
}

function redactedJson(value: unknown): string {
  return JSON.stringify(value, (key, item) => key === "value" ? "***REDACTED***" : item);
}

function jsonSummary(value: unknown): string {
  if (value == null || typeof value !== "object") return value == null ? "none" : String(value);
  const keys = Object.keys(value as JsonObject);
  if (!keys.length) return "none";
  return keys.slice(0, 4).map((key) => `${key}:${compactValue((value as JsonObject)[key])}`).join(", ");
}

function compactValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.slice(0, 3).join("|")}${value.length > 3 ? "|..." : ""}]`;
  if (value && typeof value === "object") return "{...}";
  return String(value);
}

function shortHash(value: unknown): string {
  const text = String(value || "");
  return text.length > 16 ? `${text.slice(0, 12)}...` : text || "no digest";
}

function statusTone(status: string): Tone {
  if (status === "idle") return "good";
  if (status === "busy") return "info";
  if (status === "draining") return "warn";
  return "bad";
}

function healthTone(status: string): Tone {
  if (status === "healthy") return "good";
  if (status === "degraded") return "warn";
  return "bad";
}

function rolloutTone(status: string): Tone {
  if (status === "promoted") return "good";
  if (["planned", "canarying", "paused"].includes(status)) return "info";
  if (["rescuing", "rolled_back"].includes(status)) return "warn";
  return "bad";
}

function formatAge(value: string | null | undefined): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "unknown";
  const diffMs = Date.now() - date.getTime();
  const suffix = diffMs >= 0 ? "ago" : "from now";
  const minutes = Math.max(1, Math.round(Math.abs(diffMs) / 60000));
  if (minutes < 60) return `${minutes}m ${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ${suffix}`;
  return `${Math.round(hours / 24)}d ${suffix}`;
}

function formatTime(value: Date): string {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(value);
}

function labelize(value: unknown): string {
  return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
}

function escapeHtml(value: unknown): string {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
    const replacements: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return replacements[char];
  });
}

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element as T;
}
