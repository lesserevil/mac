// Maintained dashboard source. The compiled browser module is checked in as
// app.js so mac does not require Node.js/npm to serve or install the UI.
type ViewKey = "overview" | "agents" | "tasks" | "hermes" | "runtime";
type Tone = "good" | "warn" | "bad" | "info";
type JsonObject = Record<string, unknown>;

type EndpointKey =
  | "health"
  | "tenants"
  | "users"
  | "personas"
  | "hermes"
  | "bindings"
  | "machines"
  | "agents"
  | "tasks"
  | "deadLetters"
  | "messages"
  | "secrets"
  | "runtimes"
  | "rollouts"
  | "evalSets"
  | "evalRuns";

interface ApiRecord {
  id: string;
  [key: string]: unknown;
}

interface TenantRecord extends ApiRecord {
  name: string;
}

interface PersonaRecord extends ApiRecord {
  name: string;
  soul_ref: string;
  memory_scope: string;
}

interface HermesRecord extends ApiRecord {
  tenant_id: string;
  name: string;
  persona_id?: string | null;
  home_ref?: string;
  status: string;
  last_seen_at?: string;
}

interface PlatformBindingRecord extends ApiRecord {
  hermes_instance_id: string;
  platform: string;
  external_id: string;
  display_name?: string;
}

interface MachineRecord extends ApiRecord {
  hostname: string;
  labels?: JsonObject;
  resources?: JsonObject;
  trusted: boolean;
}

interface AgentRecord extends ApiRecord {
  machine_id: string;
  name: string;
  capabilities?: string[];
  resources?: JsonObject;
  status: string;
  health_status: string;
  current_task_id?: string | null;
  last_seen_at?: string;
}

interface TaskOrigin {
  hermes_instance_id?: string;
  [key: string]: unknown;
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

interface RuntimeRecord extends ApiRecord {
  name: string;
  manifest?: JsonObject;
  digest?: string;
  created_by?: string;
  created_at?: string;
}

interface RolloutRecord extends ApiRecord {
  version: string;
  strategy: string;
  status: string;
  target_percent: number;
  channel: string;
  runtime_environment_id?: string | null;
  artifact_hash?: string | null;
  health_policy?: JsonObject;
  required_eval_set_id?: string | null;
}

interface EvalSetRecord extends ApiRecord {
  name: string;
}

interface EvalRunRecord extends ApiRecord {
  target_id: string;
  score: number;
  passed: boolean;
}

interface AgentAnalysis {
  activeTasks: TaskRecord[];
  capacity: number;
  stale: boolean;
  eligible: boolean;
  reasons: string[];
}

interface DashboardState {
  activeView: ViewKey;
  token: string;
  loading: boolean;
  data: Partial<Record<EndpointKey, unknown>>;
  errors: string[];
  loadedAt: Date | null;
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
const STALE_AFTER_MS = 15 * 60 * 1000;
const TERMINAL_TASK_STATES = new Set<string>(["completed", "failed", "cancelled"]);
const TASK_STATES: string[] = [
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

const ENDPOINTS: Record<EndpointKey, string> = {
  health: "/health",
  tenants: "/tenants",
  users: "/users",
  personas: "/personas",
  hermes: "/hermes-instances",
  bindings: "/platform-bindings",
  machines: "/machines",
  agents: "/agents",
  tasks: "/tasks",
  deadLetters: "/dispatch/dead-letters",
  messages: "/messages",
  secrets: "/secrets",
  runtimes: "/runtimes",
  rollouts: "/rollouts",
  evalSets: "/eval-sets",
  evalRuns: "/eval-runs",
};

const viewTitles: Record<ViewKey, string> = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  hermes: "Hermes",
  runtime: "Runtime",
};

const state: DashboardState = {
  activeView: "overview",
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  loading: false,
  data: {},
  errors: [],
  loadedAt: null,
  agentQuery: "",
  agentFilter: "all",
  taskFilter: "all",
};

const nodes: DashboardNodes = {
  nav: requiredElement<HTMLElement>("#viewNav"),
  title: requiredElement<HTMLElement>("#viewTitle"),
  banner: requiredElement<HTMLElement>("#banner"),
  content: requiredElement<HTMLElement>("#content"),
  refresh: requiredElement<HTMLButtonElement>("#refreshButton"),
  syncState: requiredElement<HTMLElement>("#syncState"),
  tokenForm: requiredElement<HTMLFormElement>("#tokenForm"),
  tokenInput: requiredElement<HTMLInputElement>("#tokenInput"),
  clearToken: requiredElement<HTMLButtonElement>("#clearTokenButton"),
};

nodes.tokenInput.value = state.token;
bindEvents();
loadAll();

function bindEvents(): void {
  nodes.nav.addEventListener("click", (event) => {
    const target = event.target as Element | null;
    const button = target?.closest<HTMLElement>("[data-view]");
    if (!button) {
      return;
    }
    state.activeView = (button.dataset.view || "overview") as ViewKey;
    render();
  });

  nodes.refresh.addEventListener("click", () => {
    loadAll();
  });

  nodes.tokenForm.addEventListener("submit", (event) => {
    event.preventDefault();
    state.token = nodes.tokenInput.value.trim();
    if (state.token) {
      sessionStorage.setItem(TOKEN_KEY, state.token);
    } else {
      sessionStorage.removeItem(TOKEN_KEY);
    }
    loadAll();
  });

  nodes.clearToken.addEventListener("click", () => {
    state.token = "";
    nodes.tokenInput.value = "";
    sessionStorage.removeItem(TOKEN_KEY);
    loadAll();
  });
}

async function loadAll(): Promise<void> {
  state.loading = true;
  state.errors = [];
  renderSyncState();

  const entries = Object.entries(ENDPOINTS) as Array<[EndpointKey, string]>;
  const results = await Promise.allSettled(entries.map(([name, path]) => fetchJSON(path)));
  const nextData: Partial<Record<EndpointKey, unknown>> = {};
  const nextErrors: string[] = [];

  results.forEach((result, index) => {
    const [name, path] = entries[index];
    if (result.status === "fulfilled") {
      nextData[name] = result.value;
    } else {
      nextData[name] = name === "health" ? null : [];
      const error = result.reason instanceof Error ? result.reason : new Error(String(result.reason));
      nextErrors.push(`${path}: ${error.message}`);
    }
  });

  state.data = nextData;
  state.errors = nextErrors;
  state.loadedAt = new Date();
  state.loading = false;
  render();
}

async function fetchJSON(path: string): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }

  const response = await fetch(path, { headers });
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
  nodes.title.textContent = viewTitles[state.activeView] || "Overview";
  renderSyncState();
  renderBanner();

  if (state.loading && !state.loadedAt) {
    nodes.content.innerHTML = `<div class="empty-state">Loading</div>`;
    return;
  }

  if (state.activeView === "agents") {
    renderAgents();
  } else if (state.activeView === "tasks") {
    renderTasks();
  } else if (state.activeView === "hermes") {
    renderHermes();
  } else if (state.activeView === "runtime") {
    renderRuntime();
  } else {
    renderOverview();
  }
}

function renderSyncState(): void {
  if (state.loading) {
    nodes.syncState.textContent = "Loading";
  } else if (state.loadedAt) {
    nodes.syncState.textContent = `Loaded ${formatTime(state.loadedAt)}`;
  } else {
    nodes.syncState.textContent = "Not loaded";
  }
}

function renderBanner(): void {
  if (!state.errors.length) {
    nodes.banner.hidden = true;
    nodes.banner.textContent = "";
    return;
  }
  const authErrors = state.errors.filter((error) => error.includes("403"));
  nodes.banner.hidden = false;
  nodes.banner.textContent = authErrors.length
    ? "Some API calls need a token with read scope."
    : state.errors.slice(0, 3).join(" | ");
}

function renderOverview(): void {
  const agents = list<AgentRecord>("agents");
  const machines = list<MachineRecord>("machines");
  const tasks = list<TaskRecord>("tasks");
  const hermes = list<HermesRecord>("hermes");
  const rollouts = list<RolloutRecord>("rollouts");
  const staleAgents = agents.filter((agent) => isStale(agent.last_seen_at)).length;
  const healthyAgents = agents.filter((agent) => agent.health_status === "healthy").length;
  const busyAgents = agents.filter((agent) => agent.status === "busy").length;
  const activeTasks = tasks.filter((task) => !TERMINAL_TASK_STATES.has(task.state)).length;
  const taskCounts = countBy(tasks, "state");

  nodes.content.innerHTML = `
    <section class="metric-grid">
      ${metric("Agents", agents.length, `${healthyAgents} healthy, ${staleAgents} stale`)}
      ${metric("Machines", machines.length, `${machines.filter((machine) => machine.trusted).length} trusted`)}
      ${metric("Active Tasks", activeTasks, `${busyAgents} busy workers`)}
      ${metric("Hermes", hermes.length, `${rollouts.length} rollouts tracked`)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Task States</h2>
        ${stateBars(TASK_STATES, taskCounts, tasks.length)}
      </div>
      <div class="surface">
        <h2>Attention</h2>
        ${attentionList()}
      </div>
    </section>
  `;
}

function renderAgents(): void {
  const agents = list<AgentRecord>("agents");
  const machinesById = byId(list<MachineRecord>("machines"));
  const tasks = list<TaskRecord>("tasks");
  const query = state.agentQuery.trim().toLowerCase();

  const enriched = agents.map((agent) => {
    const machine = machinesById.get(agent.machine_id);
    return {
      agent,
      machine,
      analysis: analyzeAgent(agent, machine, tasks),
    };
  });

  const filtered = enriched.filter(({ agent, machine, analysis }) => {
    const haystack = [
      agent.name,
      agent.id,
      machine ? machine.hostname : "",
      agent.status,
      agent.health_status,
      ...(agent.capabilities || []),
    ]
      .join(" ")
      .toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    const matchesFilter =
      state.agentFilter === "all" ||
      (state.agentFilter === "eligible" && analysis.eligible) ||
      (state.agentFilter === "blocked" && !analysis.eligible) ||
      (state.agentFilter === "stale" && analysis.stale) ||
      agent.status === state.agentFilter ||
      agent.health_status === state.agentFilter;
    return matchesQuery && matchesFilter;
  });

  nodes.content.innerHTML = `
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
        ${option("stale", "Stale", state.agentFilter)}
        ${option("degraded", "Degraded", state.agentFilter)}
        ${option("unhealthy", "Unhealthy", state.agentFilter)}
      </select>
      <button type="button" id="clearAgentFilters">Clear</button>
    </section>
    <section class="agent-list">
      ${
        filtered.length
          ? filtered.map(({ agent, machine, analysis }) => agentCard(agent, machine, analysis)).join("")
          : `<div class="empty-state">No matching agents</div>`
      }
    </section>
  `;

  const search = requiredElement<HTMLInputElement>("#agentSearch");
  const filter = requiredElement<HTMLSelectElement>("#agentFilter");
  const clear = requiredElement<HTMLButtonElement>("#clearAgentFilters");
  search.addEventListener("input", (event) => {
    state.agentQuery = (event.target as HTMLInputElement).value;
    renderAgents();
  });
  filter.addEventListener("change", (event) => {
    state.agentFilter = (event.target as HTMLSelectElement).value;
    renderAgents();
  });
  clear.addEventListener("click", () => {
    state.agentQuery = "";
    state.agentFilter = "all";
    renderAgents();
  });
}

function renderTasks(): void {
  const allTasks = list<TaskRecord>("tasks");
  const tasks =
    state.taskFilter === "all"
      ? allTasks
      : allTasks.filter((task) => task.state === state.taskFilter);
  const agentsById = byId(list<AgentRecord>("agents"));
  const personasById = byId(list<PersonaRecord>("personas"));
  const hermesById = byId(list<HermesRecord>("hermes"));

  nodes.content.innerHTML = `
    <section class="toolbar">
      <select id="taskFilter">
        ${option("all", "All states", state.taskFilter)}
        ${TASK_STATES.map((taskState) => option(taskState, labelize(taskState), state.taskFilter)).join("")}
      </select>
      <button type="button" id="clearTaskFilter">Clear</button>
    </section>
    <section class="task-lanes">
      ${TASK_STATES.filter((taskState) => state.taskFilter === "all" || state.taskFilter === taskState)
        .map((taskState) => taskLane(taskState, tasks, agentsById, hermesById, personasById))
        .join("")}
    </section>
  `;

  requiredElement<HTMLSelectElement>("#taskFilter").addEventListener("change", (event) => {
    state.taskFilter = (event.target as HTMLSelectElement).value;
    renderTasks();
  });
  requiredElement<HTMLButtonElement>("#clearTaskFilter").addEventListener("click", () => {
    state.taskFilter = "all";
    renderTasks();
  });
}

function renderHermes(): void {
  const tenantsById = byId(list<TenantRecord>("tenants"));
  const personasById = byId(list<PersonaRecord>("personas"));
  const users = list<ApiRecord>("users");
  const bindings = list<PlatformBindingRecord>("bindings");
  const tasks = list<TaskRecord>("tasks");

  nodes.content.innerHTML = `
    <section class="metric-grid">
      ${metric("Tenants", list("tenants").length, `${users.length} users`)}
      ${metric("Personas", list("personas").length, "soul refs only")}
      ${metric("Instances", list("hermes").length, `${bindings.length} bindings`)}
      ${metric("Interaction Tasks", tasks.filter((task) => taskOrigin(task).hermes_instance_id).length, "from Hermes")}
    </section>
    <section class="record-list">
      ${
        list<HermesRecord>("hermes").length
          ? list<HermesRecord>("hermes")
              .map((instance) => hermesRecord(instance, tenantsById, personasById, bindings, tasks))
              .join("")
          : `<div class="empty-state">No Hermes instances</div>`
      }
    </section>
  `;
}

function renderRuntime(): void {
  const rollouts = list<RolloutRecord>("rollouts");
  const runtimesById = byId(list<RuntimeRecord>("runtimes"));
  const evalRuns = list<EvalRunRecord>("evalRuns");
  const evalSets = list<EvalSetRecord>("evalSets");

  nodes.content.innerHTML = `
    <section class="split">
      <div class="surface">
        <h2>Runtime Environments</h2>
        <div class="runtime-list">
          ${
            list<RuntimeRecord>("runtimes").length
              ? list<RuntimeRecord>("runtimes").map((runtime) => runtimeRecord(runtime, rollouts)).join("")
              : `<div class="empty-state">No runtimes</div>`
          }
        </div>
      </div>
      <div class="surface">
        <h2>Rollouts</h2>
        <div class="rollout-list">
          ${
            rollouts.length
              ? rollouts.map((rollout) => rolloutRecord(rollout, runtimesById, evalSets, evalRuns)).join("")
              : `<div class="empty-state">No rollouts</div>`
          }
        </div>
      </div>
    </section>
  `;
}

function agentCard(agent: AgentRecord, machine: MachineRecord | undefined, analysis: AgentAnalysis): string {
  const activeTask = analysis.activeTasks[0];
  const capabilityChips = (agent.capabilities || []).length
    ? agent.capabilities.map((capability) => chip(capability, "info")).join("")
    : chip("no capabilities", "warn");
  const reasons = analysis.eligible
    ? chip("dispatch eligible", "good")
    : analysis.reasons.map((reason) => chip(reason, "bad")).join("");

  return `
    <article class="agent-card ${analysis.eligible ? "" : "is-blocked"}">
      <div class="agent-header">
        <div>
          <h2 class="mono">${escapeHtml(agent.name)}</h2>
          <p class="muted small">${escapeHtml(agent.id)}</p>
        </div>
        <div class="chip-row">
          ${chip(agent.status, statusTone(agent.status))}
          ${chip(agent.health_status, healthTone(agent.health_status))}
        </div>
      </div>
      <div class="row-grid">
        ${field("Machine", machine ? machine.hostname : "missing")}
        ${field("Trusted", machine && machine.trusted ? "yes" : "no")}
        ${field("Last seen", formatAge(agent.last_seen_at))}
        ${field("Capacity", `${analysis.activeTasks.length} / ${analysis.capacity}`)}
        ${field("Current task", activeTask ? activeTask.title : agent.current_task_id || "none")}
        ${field("Lease", activeTask && activeTask.leased_until ? formatAge(activeTask.leased_until) : "none")}
        ${field("Resources", jsonSummary(agent.resources))}
        ${field("Machine resources", machine ? jsonSummary(machine.resources) : "missing")}
      </div>
      <div class="chip-row">${reasons}</div>
      <div class="chip-row">${capabilityChips}</div>
    </article>
  `;
}

function taskLane(
  taskState: string,
  tasks: TaskRecord[],
  agentsById: Map<string, AgentRecord>,
  hermesById: Map<string, HermesRecord>,
  personasById: Map<string, PersonaRecord>,
): string {
  const laneTasks = tasks.filter((task) => task.state === taskState);
  return `
    <div class="task-lane">
      <h2><span>${escapeHtml(labelize(taskState))}</span><span class="pill">${laneTasks.length}</span></h2>
      ${
        laneTasks.length
          ? laneTasks.map((task) => taskCard(task, agentsById, hermesById, personasById)).join("")
          : `<div class="empty-state">Empty</div>`
      }
    </div>
  `;
}

function taskCard(
  task: TaskRecord,
  agentsById: Map<string, AgentRecord>,
  hermesById: Map<string, HermesRecord>,
  personasById: Map<string, PersonaRecord>,
): string {
  const owner = task.owner_agent_id ? agentsById.get(task.owner_agent_id) : null;
  const origin = taskOrigin(task);
  const hermes = origin.hermes_instance_id ? hermesById.get(origin.hermes_instance_id) : null;
  const persona = hermes && hermes.persona_id ? personasById.get(hermes.persona_id) : null;
  const capabilities = task.required_capabilities || [];
  return `
    <article class="task-card">
      <h3>${escapeHtml(task.title)}</h3>
      <p class="muted small mono">${escapeHtml(task.id)}</p>
      <div class="chip-row">
        ${chip(`P${task.priority}`, "info")}
        ${chip(`${task.attempt_count || 0}/${task.max_attempts || 0} attempts`, task.attempt_count >= task.max_attempts ? "bad" : "good")}
        ${owner ? chip(owner.name, "info") : chip("unowned", "warn")}
      </div>
      <div class="chip-row">
        ${capabilities.length ? capabilities.map((capability) => chip(capability, "info")).join("") : chip("no capability gate", "warn")}
      </div>
      ${
        hermes
          ? `<p class="small muted">Origin: ${escapeHtml(hermes.name)}${persona ? ` / ${escapeHtml(persona.name)}` : ""}</p>`
          : ""
      }
    </article>
  `;
}

function hermesRecord(
  instance: HermesRecord,
  tenantsById: Map<string, TenantRecord>,
  personasById: Map<string, PersonaRecord>,
  bindings: PlatformBindingRecord[],
  tasks: TaskRecord[],
): string {
  const tenant = tenantsById.get(instance.tenant_id);
  const persona = instance.persona_id ? personasById.get(instance.persona_id) : null;
  const instanceBindings = bindings.filter((binding) => binding.hermes_instance_id === instance.id);
  const instanceTasks = tasks.filter((task) => taskOrigin(task).hermes_instance_id === instance.id);

  return `
    <article class="record">
      <div class="record-header">
        <div>
          <h2>${escapeHtml(instance.name)}</h2>
          <p class="muted small mono">${escapeHtml(instance.id)}</p>
        </div>
        ${chip(instance.status, instance.status === "active" ? "good" : "warn")}
      </div>
      <div class="row-grid">
        ${field("Tenant", tenant ? tenant.name : instance.tenant_id)}
        ${field("Persona", persona ? persona.name : "none")}
        ${field("Soul ref", persona ? persona.soul_ref : "none")}
        ${field("Memory scope", persona ? persona.memory_scope : "none")}
        ${field("Home", instance.home_ref || "none")}
        ${field("Bindings", String(instanceBindings.length))}
        ${field("Interaction tasks", String(instanceTasks.length))}
        ${field("Last seen", formatAge(instance.last_seen_at))}
      </div>
      <div class="chip-row">
        ${
          instanceBindings.length
            ? instanceBindings.map((binding) => chip(`${binding.platform}:${binding.display_name || binding.external_id}`, "info")).join("")
            : chip("no platform bindings", "warn")
        }
      </div>
    </article>
  `;
}

function runtimeRecord(runtime: RuntimeRecord, rollouts: RolloutRecord[]): string {
  const runtimeRollouts = rollouts.filter((rollout) => rollout.runtime_environment_id === runtime.id);
  return `
    <article class="runtime-record">
      <div class="runtime-header">
        <div>
          <h3>${escapeHtml(runtime.name)}</h3>
          <p class="muted small mono">${escapeHtml(runtime.id)}</p>
        </div>
        ${chip(shortHash(runtime.digest), "good")}
      </div>
      <div class="row-grid">
        ${field("Created by", runtime.created_by)}
        ${field("Created", formatAge(runtime.created_at))}
        ${field("Rollouts", String(runtimeRollouts.length))}
        ${field("Manifest", jsonSummary(runtime.manifest))}
      </div>
    </article>
  `;
}

function rolloutRecord(
  rollout: RolloutRecord,
  runtimesById: Map<string, RuntimeRecord>,
  evalSets: EvalSetRecord[],
  evalRuns: EvalRunRecord[],
): string {
  const runtime = rollout.runtime_environment_id ? runtimesById.get(rollout.runtime_environment_id) : null;
  const evalSet = rollout.required_eval_set_id
    ? evalSets.find((candidate) => candidate.id === rollout.required_eval_set_id)
    : null;
  const matchingRuns = evalRuns.filter((run) => run.target_id === rollout.version);
  const latestRun = matchingRuns[matchingRuns.length - 1];

  return `
    <article class="rollout-record">
      <div class="rollout-header">
        <div>
          <h3>${escapeHtml(rollout.version)}</h3>
          <p class="muted small mono">${escapeHtml(rollout.id)}</p>
        </div>
        ${chip(rollout.status, rolloutTone(rollout.status))}
      </div>
      <div class="row-grid">
        ${field("Strategy", rollout.strategy)}
        ${field("Target", `${rollout.target_percent}%`)}
        ${field("Channel", rollout.channel)}
        ${field("Runtime", runtime ? runtime.name : "none")}
        ${field("Artifact", rollout.artifact_hash || "unverified")}
        ${field("Eval gate", evalSet ? evalSet.name : "none")}
        ${field("Latest eval", latestRun ? `${latestRun.score} ${latestRun.passed ? "pass" : "fail"}` : "none")}
        ${field("Health policy", jsonSummary(rollout.health_policy))}
      </div>
    </article>
  `;
}

function analyzeAgent(
  agent: AgentRecord,
  machine: MachineRecord | undefined,
  tasks: TaskRecord[],
): AgentAnalysis {
  const activeTasks = tasks.filter(
    (task) => task.owner_agent_id === agent.id && !TERMINAL_TASK_STATES.has(task.state),
  );
  const capacity = capacityOf(agent.resources);
  const stale = isStale(agent.last_seen_at);
  const reasons: string[] = [];

  if (!machine) {
    reasons.push("missing machine");
  } else if (!machine.trusted) {
    reasons.push("untrusted machine");
  }
  if (agent.status === "offline") {
    reasons.push("offline");
  }
  if (agent.status === "draining") {
    reasons.push("draining");
  }
  if (agent.health_status !== "healthy") {
    reasons.push(agent.health_status || "unknown health");
  }
  if (stale) {
    reasons.push("stale heartbeat");
  }
  if (activeTasks.length >= capacity) {
    reasons.push("at capacity");
  }

  return {
    activeTasks,
    capacity,
    stale,
    eligible: reasons.length === 0,
    reasons,
  };
}

function attentionList(): string {
  const agents = list<AgentRecord>("agents");
  const tasks = list<TaskRecord>("tasks");
  const deadLetters = list<TaskRecord>("deadLetters");
  const rollouts = list<RolloutRecord>("rollouts");
  const items: string[] = [];

  const staleAgents = agents.filter((agent) => isStale(agent.last_seen_at));
  if (staleAgents.length) {
    items.push(`${staleAgents.length} stale agent heartbeat${staleAgents.length === 1 ? "" : "s"}`);
  }
  if (deadLetters.length) {
    items.push(`${deadLetters.length} dead letter task${deadLetters.length === 1 ? "" : "s"}`);
  }
  const failedTasks = tasks.filter((task) => task.state === "failed");
  if (failedTasks.length) {
    items.push(`${failedTasks.length} failed task${failedTasks.length === 1 ? "" : "s"}`);
  }
  const rescueRollouts = rollouts.filter((rollout) => rollout.status === "rescuing" || rollout.status === "failed");
  if (rescueRollouts.length) {
    items.push(`${rescueRollouts.length} rollout${rescueRollouts.length === 1 ? "" : "s"} in rescue/failure`);
  }

  if (!items.length) {
    return `<div class="empty-state">No attention items</div>`;
  }
  return `
    <div class="record-list">
      ${items.map((item) => `<div class="record compact">${escapeHtml(item)}</div>`).join("")}
    </div>
  `;
}

function metric(label: string, value: string | number, note: string): string {
  return `
    <div class="metric">
      <div class="metric-label">${escapeHtml(label)}</div>
      <div class="metric-value">${escapeHtml(value)}</div>
      <p class="metric-note">${escapeHtml(note)}</p>
    </div>
  `;
}

function stateBars(states: string[], counts: Record<string, number>, total: number): string {
  if (!total) {
    return `<div class="empty-state">No tasks</div>`;
  }
  return `
    <div class="state-bar">
      ${states
        .map((taskState) => {
          const count = counts[taskState] || 0;
          const width = Math.max(2, Math.round((count / total) * 100));
          return `
            <div class="state-row">
              <span>${escapeHtml(labelize(taskState))}</span>
              <span class="bar-track"><span class="bar-fill" style="width: ${width}%"></span></span>
              <span class="mono small">${count}</span>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function field(label: string, value: unknown): string {
  return `
    <div class="field">
      <span class="field-label">${escapeHtml(label)}</span>
      <span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span>
    </div>
  `;
}

function chip(value: unknown, tone: Tone = "info"): string {
  return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}

function option(value: string, label: string, selected: string): string {
  return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

function list<T extends ApiRecord>(key: EndpointKey): T[] {
  const value = state.data[key];
  return Array.isArray(value) ? (value as T[]) : [];
}

function byId<T extends ApiRecord>(records: T[]): Map<string, T> {
  return new Map(records.filter((record) => record && record.id).map((record) => [record.id, record]));
}

function countBy<T extends Record<string, unknown>>(records: T[], key: keyof T): Record<string, number> {
  return records.reduce((counts, record) => {
    const value = String(record[key] || "unknown");
    counts[value] = (counts[value] || 0) + 1;
    return counts;
  }, {} as Record<string, number>);
}

function taskOrigin(task: TaskRecord): TaskOrigin {
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const origin = metadata.origin && typeof metadata.origin === "object" ? metadata.origin : {};
  return origin as TaskOrigin;
}

function capacityOf(resources?: JsonObject | null): number {
  const data = resources && typeof resources === "object" ? resources : {};
  const candidates = [data.capacity, data.max_concurrent_tasks, data.max_sessions];
  const parsed = candidates.map((value) => Number(value)).find((value) => Number.isFinite(value) && value > 0);
  return parsed || 1;
}

function isStale(value?: string | null): boolean {
  const date = parseDate(value);
  if (!date) {
    return true;
  }
  return Date.now() - date.getTime() > STALE_AFTER_MS;
}

function parseDate(value?: string | null): Date | null {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatAge(value?: string | Date | null): string {
  const date = value instanceof Date ? value : parseDate(value);
  if (!date) {
    return "unknown";
  }
  const diffMs = Date.now() - date.getTime();
  const absMs = Math.abs(diffMs);
  const suffix = diffMs >= 0 ? "ago" : "from now";
  const minutes = Math.round(absMs / 60000);
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${minutes}m ${suffix}`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 48) {
    return `${hours}h ${suffix}`;
  }
  const days = Math.round(hours / 24);
  return `${days}d ${suffix}`;
}

function formatTime(value: Date): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(value);
}

function jsonSummary(value: unknown): string {
  if (value == null) {
    return "none";
  }
  if (typeof value !== "object") {
    return String(value);
  }
  const keys = Object.keys(value);
  if (!keys.length) {
    return "none";
  }
  return keys
    .slice(0, 4)
    .map((key) => `${key}:${compactValue(value[key])}`)
    .join(", ");
}

function compactValue(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.slice(0, 3).join("|")}${value.length > 3 ? "|..." : ""}]`;
  }
  if (value && typeof value === "object") {
    return "{...}";
  }
  return String(value);
}

function shortHash(value: unknown): string {
  if (!value) {
    return "no digest";
  }
  const text = String(value);
  return text.length > 16 ? `${text.slice(0, 12)}...` : text;
}

function labelize(value: unknown): string {
  return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
}

function statusTone(status: string): Tone {
  if (status === "idle") {
    return "good";
  }
  if (status === "busy") {
    return "info";
  }
  if (status === "draining") {
    return "warn";
  }
  return "bad";
}

function healthTone(status: string): Tone {
  if (status === "healthy") {
    return "good";
  }
  if (status === "degraded") {
    return "warn";
  }
  return "bad";
}

function rolloutTone(status: string): Tone {
  if (status === "promoted") {
    return "good";
  }
  if (status === "planned" || status === "canarying" || status === "paused") {
    return "info";
  }
  if (status === "rescuing" || status === "rolled_back") {
    return "warn";
  }
  return "bad";
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
  if (!element) {
    throw new Error(`Missing dashboard element: ${selector}`);
  }
  return element as T;
}
