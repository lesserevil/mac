// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
import { createDashboardApi } from "./dashboard_api.js";
"use strict";
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
const AGENT_PAGE_SIZE = 50;
const VIEW_TITLES = {
    overview: "Overview",
    work: "Work",
    map: "Map",
    agents: "Agents",
    tasks: "Tasks",
    workflows: "Workflows",
    hermes: "Hermes",
    ops: "Operations",
    integrations: "Integrations",
    runtime: "Runtime",
    observability: "Observability",
    secrets: "Secrets",
};
const VIEW_KEYS = new Set(Object.keys(VIEW_TITLES));
const DEFAULT_URL_STATE = readUrlState();
const state = {
    activeView: DEFAULT_URL_STATE.activeView,
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    loading: false,
    loadedAt: null,
    data: null,
    error: null,
    actionMessage: null,
    agentQuery: DEFAULT_URL_STATE.agentQuery,
    agentFilter: DEFAULT_URL_STATE.agentFilter,
    agentSort: DEFAULT_URL_STATE.agentSort,
    agentPage: DEFAULT_URL_STATE.agentPage,
    projectFilter: DEFAULT_URL_STATE.projectFilter,
    taskFilter: DEFAULT_URL_STATE.taskFilter,
    selectedId: DEFAULT_URL_STATE.selectedId,
    observabilityLive: [],
    observabilityStream: null,
    observabilityStreamStatus: "idle",
};
const nodes = {
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
const api = createDashboardApi(() => state.token);
nodes.tokenInput.value = state.token;
bindEvents();
loadDashboard();
function bindEvents() {
    nodes.nav.addEventListener("click", (event) => {
        const button = event.target?.closest("[data-view]");
        if (!button)
            return;
        state.activeView = (button.dataset.view || "overview");
        state.actionMessage = null;
        updateUrlState();
        render();
    });
    nodes.refresh.addEventListener("click", () => loadDashboard());
    nodes.content.addEventListener("click", handleContentClick);
    nodes.content.addEventListener("submit", handleActionSubmit);
    nodes.tokenForm.addEventListener("submit", (event) => {
        event.preventDefault();
        state.token = nodes.tokenInput.value.trim();
        if (state.token)
            sessionStorage.setItem(TOKEN_KEY, state.token);
        else
            sessionStorage.removeItem(TOKEN_KEY);
        loadDashboard();
    });
    nodes.clearToken.addEventListener("click", () => {
        state.token = "";
        nodes.tokenInput.value = "";
        sessionStorage.removeItem(TOKEN_KEY);
        loadDashboard();
    });
}
async function loadDashboard() {
    state.loading = true;
    state.error = null;
    renderSyncState();
    try {
        state.data = (await requestJSON("/dashboard/state"));
        state.loadedAt = new Date();
    }
    catch (error) {
        state.error = error instanceof Error ? error.message : String(error);
    }
    finally {
        state.loading = false;
        render();
    }
}
async function requestJSON(path, init = {}) {
    return api.request(path, init);
}
function render() {
    document.querySelectorAll("[data-view]").forEach((button) => {
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
    const body = state.activeView === "work"
        ? renderWork()
        : state.activeView === "map"
        ? renderMap()
        : state.activeView === "agents"
            ? renderAgents()
            : state.activeView === "tasks"
                ? renderTasks()
                : state.activeView === "workflows"
                    ? renderWorkflows()
                    : state.activeView === "hermes"
                        ? renderHermes()
                        : state.activeView === "ops"
                            ? renderOperations()
                            : state.activeView === "integrations"
                                ? renderIntegrations()
                                : state.activeView === "runtime"
                                    ? renderRuntime()
                                    : state.activeView === "observability"
                                        ? renderObservability()
                                        : state.activeView === "secrets"
                                            ? renderSecrets()
                                            : renderOverview();
    nodes.content.innerHTML = `${action}${body}`;
    bindViewControls();
    syncObservabilitySubscription();
}
function renderSyncState() {
    nodes.syncState.textContent = state.loading
        ? "Loading"
        : state.loadedAt
            ? `Loaded ${formatTime(state.loadedAt)}`
            : "Not loaded";
}
function renderBanner() {
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
function readUrlState() {
    const params = new URLSearchParams(window.location.search);
    const rawView = params.get("view") || "overview";
    const page = Number(params.get("agent_page") || "1");
    return {
        activeView: VIEW_KEYS.has(rawView) ? rawView : "overview",
        agentQuery: params.get("agent_q") || "",
        agentFilter: params.get("agent_filter") || "all",
        agentSort: params.get("agent_sort") || "name",
        agentPage: Number.isFinite(page) && page > 0 ? Math.floor(page) : 1,
        projectFilter: params.get("project") || "all",
        taskFilter: params.get("task_state") || "all",
        selectedId: params.get("selected") || "",
    };
}
function applyUrlState() {
    const next = readUrlState();
    state.activeView = next.activeView;
    state.agentQuery = next.agentQuery;
    state.agentFilter = next.agentFilter;
    state.agentSort = next.agentSort;
    state.agentPage = next.agentPage;
    state.projectFilter = next.projectFilter;
    state.taskFilter = next.taskFilter;
    state.selectedId = next.selectedId;
}
function updateUrlState(replace = false) {
    const params = new URLSearchParams();
    if (state.activeView !== "overview")
        params.set("view", state.activeView);
    if (state.agentQuery.trim())
        params.set("agent_q", state.agentQuery.trim());
    if (state.agentFilter !== "all")
        params.set("agent_filter", state.agentFilter);
    if (state.agentSort !== "name")
        params.set("agent_sort", state.agentSort);
    if (state.agentPage > 1)
        params.set("agent_page", String(state.agentPage));
    if (state.projectFilter !== "all")
        params.set("project", state.projectFilter);
    if (state.taskFilter !== "all")
        params.set("task_state", state.taskFilter);
    if (state.selectedId)
        params.set("selected", state.selectedId);
    const query = params.toString();
    const nextUrl = `${window.location.pathname}${query ? `?${query}` : ""}`;
    const method = replace ? "replaceState" : "pushState";
    window.history[method]({}, "", nextUrl);
}
window.addEventListener("popstate", () => {
    applyUrlState();
    state.actionMessage = null;
    render();
});
function renderOverview() {
    const data = mustData();
    const counts = data.overview.counts;
    const startup = data.hermes_startup;
    const startupStatus = startup?.operator_health?.status || (startup?.ready ? "healthy" : "degraded");
    const readyStories = data.project_summaries.reduce((sum, project) => sum + project.ready_count, 0);
    return `
    <section class="metric-grid">
      ${metric("Agents", counts.agents || 0, `${counts.healthy_agents || 0} healthy, ${counts.busy_agents || 0} busy`)}
      ${metric("Projects", counts.projects || 0, `${readyStories} ready stories`)}
      ${metric("Active Work", counts.active_tasks || 0, `${counts.dead_letters || 0} dead letters`)}
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
function renderWork() {
    const data = mustData();
    const projects = data.project_summaries;
    const selectedProject = selectedProjectSummary(data);
    const scopedProjects = state.projectFilter === "all" ? projects : projects.filter((project) => project.project === state.projectFilter);
    const selectedTask = selectedTaskDetail(data) || selectedProject?.frontier_tasks
        .map((task) => taskDetailById(data, task.id))
        .find(Boolean) || null;
    const readyStories = scopedProjects.reduce((sum, project) => sum + project.ready_count, 0);
    const blockedStories = scopedProjects.reduce((sum, project) => sum + project.blocked_count, 0);
    const activeAgents = new Set(scopedProjects.flatMap((project) => project.active_agent_ids)).size;
    return `
    <section class="toolbar">
      <select id="projectFilter">
        ${option("all", "All projects", state.projectFilter)}
        ${projects.map((project) => option(project.project, project.project, state.projectFilter)).join("")}
      </select>
      <button type="button" id="clearWorkScope">Clear Scope</button>
    </section>
    <section class="metric-grid">
      ${metric("Projects", projects.length, `${readyStories} ready stories`)}
      ${metric("Active Agents", activeAgents, "working in selected scope")}
      ${metric("Blocked Stories", blockedStories, "waiting on dependencies")}
      ${metric("Cross-Project Edges", projects.reduce((sum, project) => sum + project.cross_project_dependency_count, 0), "dependency order links")}
    </section>
    <section class="work-layout">
      <div class="surface">
        <div class="surface-heading">
          <h2>Epic / Project Frontier</h2>
          ${chip(selectedProject?.project || "all projects", "info")}
        </div>
        <div class="project-frontier-list">
          ${scopedProjects
        .map(projectFrontierRecord)
        .join("") || `<div class="empty-state">No projects</div>`}
        </div>
      </div>
      <div class="surface">
        <div class="surface-heading">
          <h2>Story Scope</h2>
          ${selectedTask ? chip(selectedTask.task.state, statusTone(selectedTask.task.state)) : chip("none selected", "warn")}
        </div>
        ${selectedTask ? storyScopePanel(data, selectedTask) : `<div class="empty-state">Select a story to inspect related agents</div>`}
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Project Agents</h2>
        ${projectAgentsPanel(data, selectedProject)}
      </div>
      <div class="surface">
        <h2>Dependency Order</h2>
        ${dependencyOrderPanel(data, selectedProject)}
      </div>
    </section>
  `;
}
function renderMap() {
    const data = mustData();
    const activeTasks = data.tasks.filter((detail) => !TERMINAL_TASK_STATES.has(detail.task.state));
    const dependencyCount = data.tasks.reduce((sum, detail) => sum + (detail.task.dependencies || []).length, 0);
    return `
    <section class="metric-grid">
      ${metric("Topology Nodes", data.machines.length + data.agents.length + activeTasks.length, "machines, agents, active tasks")}
      ${metric("Dispatch Queue", data.dispatch.open_task_count || 0, "open tasks awaiting agents")}
      ${metric("Dependencies", dependencyCount, "task dependency edges")}
      ${metric("AgentBus", data.agentbus_streams.length, "recent streams")}
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Fleet Relationship Map</h2>
        ${chip(state.selectedId || "nothing selected", state.selectedId ? "info" : "warn")}
      </div>
      ${relationshipGraph(data)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Dispatch Eligibility</h2>
        <div class="record-list">
          ${data.dispatch.tasks.length ? data.dispatch.tasks.slice(0, 20).map(dispatchRecord).join("") : `<div class="empty-state">No dispatch candidates</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Dependency Edges</h2>
        <div class="record-list">
          ${taskDependencyRecords(data)}
        </div>
      </div>
    </section>
  `;
}
function renderAgents() {
    const data = mustData();
    const agents = filteredAgents(data);
    const pageCount = Math.max(1, Math.ceil(agents.length / AGENT_PAGE_SIZE));
    if (state.agentPage > pageCount)
        state.agentPage = pageCount;
    const start = (state.agentPage - 1) * AGENT_PAGE_SIZE;
    const visible = agents.slice(start, start + AGENT_PAGE_SIZE);
    const visibleIds = visible.map((item) => item.agent.id);
    return `
    <section class="metric-grid">
      ${metric("Visible Agents", agents.length, `${data.agents.length} total`)}
      ${metric("Busy", agents.filter((item) => item.agent.status === "busy").length, "in current result")}
      ${metric("Blocked", agents.filter((item) => !item.availability.eligible).length, "not dispatch eligible")}
      ${metric("Page", `${state.agentPage}/${pageCount}`, `${visible.length} rows shown`)}
    </section>
    <section class="toolbar">
      <input id="agentSearch" type="search" placeholder="Search agents, hosts, capabilities" value="${escapeHtml(state.agentQuery)}">
      <select id="agentProjectFilter">
        ${option("all", "All projects", state.projectFilter)}
        ${data.project_summaries.map((project) => option(project.project, project.project, state.projectFilter)).join("")}
      </select>
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
      <select id="agentSort">
        ${option("name", "Sort by name", state.agentSort)}
        ${option("status", "Sort by status", state.agentSort)}
        ${option("project", "Sort by project", state.agentSort)}
        ${option("capacity", "Sort by capacity", state.agentSort)}
        ${option("last_seen", "Sort by last seen", state.agentSort)}
      </select>
      <button type="button" id="clearAgentFilters">Clear</button>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Agent Resource Table</h2>
        ${chip(`${agents.length} matching`, "info")}
      </div>
      <form class="action-form compact" data-action="agentBulkUpdate">
        <input type="hidden" name="agent_ids" value="${escapeHtml(visibleIds.join(","))}">
        <label>Status <select name="status">${option("", "No status change", "")}${["idle", "draining", "offline"].map((value) => option(value, labelize(value), "")).join("")}</select></label>
        <label>Health <select name="health_status">${option("", "No health change", "")}${["healthy", "degraded", "unhealthy"].map((value) => option(value, labelize(value), "")).join("")}</select></label>
        <button type="submit">Apply To Visible</button>
      </form>
      ${agentTable(visible, data)}
      <div class="pager">
        <button type="button" id="agentPrevPage" ${state.agentPage <= 1 ? "disabled" : ""}>Previous</button>
        <span class="muted small">Rows ${agents.length ? start + 1 : 0}-${start + visible.length} of ${agents.length}</span>
        <button type="button" id="agentNextPage" ${state.agentPage >= pageCount ? "disabled" : ""}>Next</button>
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Project Cohorts</h2>
        ${swarmBuckets(data.swarm_summary.project)}
      </div>
      <div class="surface">
        <h2>Capability Footprint</h2>
        ${swarmBuckets(data.swarm_summary.capability)}
      </div>
    </section>
  `;
}
function renderTasks() {
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
function renderWorkflows() {
    const data = mustData();
    const running = Number(data.workflow_runs.counts?.running || 0);
    const pendingDrafts = data.workflow_drafts.filter((draft) => draft.status !== "compiled" && draft.status !== "cancelled");
    return `
    <section class="metric-grid">
      ${metric("Definitions", data.workflows.length, `${data.workflow_runs.total || 0} total runs`)}
      ${metric("Running", running, "active workflow runs")}
      ${metric("Drafts", data.workflow_drafts.length, `${pendingDrafts.length} pending`)}
      ${metric("Notifier Channels", data.notifier_channels.length, "task progress sinks")}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Workflow Graph</h2>
        ${workflowGraph(data.workflows[0])}
      </div>
      <div class="surface">
        <h2>Create Draft</h2>
        <form class="action-form" data-action="workflowDraftCreate">
          <label>Goal <textarea name="goal" required></textarea></label>
          <label>Steps JSON <textarea name="proposed_steps" placeholder='[{"node_key":"step_1","role_required":"dev","instructions":"Do the work"}]'></textarea></label>
          <label>Questions JSON <textarea name="questions" placeholder="[]"></textarea></label>
          <label>Answers JSON <textarea name="answers" placeholder="{}"></textarea></label>
          <button type="submit">Create Draft</button>
        </form>
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Workflows</h2>
        <div class="record-list">
          ${data.workflows.length ? data.workflows.map(workflowRecord).join("") : `<div class="empty-state">No workflows</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Drafts</h2>
        <div class="record-list">
          ${data.workflow_drafts.length ? data.workflow_drafts.map(workflowDraftRecord).join("") : `<div class="empty-state">No workflow drafts</div>`}
        </div>
      </div>
    </section>
    <section class="surface">
      <h2>Notifier Channels</h2>
      <form class="action-form compact" data-action="notifierConfigure">
        <label>Name <input name="name" placeholder="ops-slack"></label>
        <label>Type ${select("channel_type", ["slack", "telegram", "hermes"], "slack")}</label>
        <label>Events <input name="event_types" value="task.*"></label>
        <label>Target JSON <textarea name="target" placeholder='{"platform":"slack"}'></textarea></label>
        <button type="submit">Save Channel</button>
      </form>
      <form class="action-form compact" data-action="notifierDeliver">
        <label>Limit <input name="limit" type="number" min="1" value="50"></label>
        <button type="submit">Deliver Pending</button>
      </form>
      <div class="record-list">
        ${data.notifier_channels.length ? data.notifier_channels.map(notifierChannelRecord).join("") : `<div class="empty-state">No notifier channels</div>`}
      </div>
    </section>
  `;
}
function workflowRecord(workflow) {
    return `
    <article class="record compact ${selectedClass(String(workflow.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(workflow.name || workflow.slug || workflow.id)}</h3><p class="muted small mono">${escapeHtml(workflow.id)}</p></div>
        <div class="chip-row">${chip(`v${workflow.version || 1}`, "info")}${chip(workflow.workflow_type || "workflow", "good")}</div>
      </div>
      <div class="row-grid compact-grid">
        ${field("Slug", workflow.slug)}
        ${field("Tenant", workflow.tenant_id || "global")}
        ${field("Nodes", workflow.definition?.nodes?.length || 0)}
        ${field("Enabled", workflow.enabled ? "yes" : "no")}
      </div>
      <form class="action-form compact" data-action="workflowPreview" data-workflow-id="${escapeHtml(workflow.id)}">
        <label>Input JSON <textarea name="input" placeholder="{}"></textarea></label>
        <button type="submit">Preview</button>
      </form>
      <form class="action-form compact" data-action="workflowStart" data-workflow-id="${escapeHtml(workflow.id)}">
        <label>Started by <input name="started_by" value="human"></label>
        <label>Input JSON <textarea name="input" placeholder="{}"></textarea></label>
        <button type="submit">Start</button>
      </form>
    </article>
  `;
}
function workflowDraftRecord(draft) {
    return `
    <article class="record compact ${selectedClass(String(draft.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(draft.goal)}</h3><p class="muted small mono">${escapeHtml(draft.id)}</p></div>
        ${chip(draft.status, draft.status === "compiled" ? "good" : "warn")}
      </div>
      <div class="row-grid compact-grid">
        ${field("Steps", draft.proposed_steps?.length || 0)}
        ${field("Questions", draft.questions?.length || 0)}
        ${field("Compiled", draft.compiled_workflow_id || "none")}
        ${field("Updated", formatAge(draft.updated_at))}
      </div>
      <form class="action-form compact" data-action="workflowDraftPreview" data-draft-id="${escapeHtml(draft.id)}">
        <label>Input JSON <textarea name="input" placeholder="{}"></textarea></label>
        <button type="submit">Preview</button>
      </form>
      <form class="action-form compact" data-action="workflowDraftApprove" data-draft-id="${escapeHtml(draft.id)}">
        <label>Slug <input name="slug" value="${escapeHtml(String(draft.goal || draft.id).toLowerCase().replaceAll(" ", "-").replace(/[^a-z0-9-]/g, ""))}"></label>
        <label>Name <input name="name" value="${escapeHtml(draft.goal)}"></label>
        <button type="submit">Approve</button>
      </form>
    </article>
  `;
}
function workflowGraph(workflow) {
    const definition = workflow?.definition;
    const nodes = definition?.nodes || [];
    const edges = definition?.edges || [];
    if (!workflow || !nodes.length)
        return `<div class="empty-state">No workflow graph</div>`;
    const width = 720;
    const height = Math.max(180, nodes.length * 70 + 60);
    const nodePositions = new Map(nodes.map((node, index) => [String(node.node_key), { x: 120 + (index % 3) * 240, y: 70 + Math.floor(index / 3) * 110 }]));
    const edgeSvg = edges.map((edge) => {
        const from = nodePositions.get(String(edge.from_node_key || ""));
        const to = nodePositions.get(String(edge.to_node_key || ""));
        if (!from || !to)
            return "";
        return `<path class="graph-edge graph-edge-dependency" d="M${from.x + 82},${from.y} C${from.x + 150},${from.y} ${to.x - 150},${to.y} ${to.x - 82},${to.y}"></path>`;
    }).join("");
    const nodeSvg = nodes.map((node) => {
        const pos = nodePositions.get(String(node.node_key)) || { x: 120, y: 70 };
        return `
      <g class="graph-node graph-node-task" transform="translate(${pos.x},${pos.y})">
        <rect x="-86" y="-24" width="172" height="48" rx="8"></rect>
        <text text-anchor="middle" y="-3">${escapeHtml(truncate(node.node_key, 20))}</text>
        <text class="graph-column-label" text-anchor="middle" y="15">${escapeHtml(truncate(node.role_required, 18))}</text>
      </g>
    `;
    }).join("");
    return `
    <div class="graph-wrap">
      <svg class="relationship-graph" viewBox="0 0 ${width} ${height}" role="img" aria-label="Workflow graph">
        ${edgeSvg}
        ${nodeSvg}
      </svg>
    </div>
  `;
}
function notifierChannelRecord(channel) {
    return `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(channel.name)}</h3><p class="muted small mono">${escapeHtml(channel.id)}</p></div>
        <div class="chip-row">${chip(channel.channel_type, "info")}${chip(channel.enabled ? "enabled" : "disabled", channel.enabled ? "good" : "warn")}</div>
      </div>
      <p class="muted small">${escapeHtml((channel.event_types || []).join(", ") || "task.*")}</p>
      <p class="muted small mono">${escapeHtml(jsonSummary(channel.target))}</p>
    </article>
  `;
}
function renderHermes() {
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
function hermesStartupPanel(startup) {
    if (!startup) {
        return `<section class="surface"><h2>Startup Health</h2><div class="empty-state">No startup report</div></section>`;
    }
    const operator = startup.operator_health || {};
    const security = (startup.security?.secret_redaction || {});
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
        ${field("Log classes", logs.classes?.length ?? 0)}
      </div>
      ${warnings.length ? `<div class="timeline">${warnings.map((warning) => timelineItem("warning", warning, "")).join("")}</div>` : ""}
    </section>
  `;
}
function renderOperations() {
    const data = mustData();
    const workflowCounts = data.workflow_runs.counts || {};
    const pendingProvisioning = data.provisioning_requests.filter((item) => item.status === "pending");
    const openStreams = data.agentbus_streams.filter((item) => item.status === "open");
    return `
    <section class="metric-grid">
      ${metric("Roles", data.roles.length, "agent personas and constraints")}
      ${metric("Provisioning", pendingProvisioning.length, "pending agent requests")}
      ${metric("Workflows", data.workflows.length, `${data.workflow_runs.total || 0} runs`)}
      ${metric("AgentBus", openStreams.length, `${data.agentbus_streams.length} recent streams`)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Workflow Runs</h2>
        ${stateBars(Object.keys(workflowCounts).sort(), workflowCounts, Number(data.workflow_runs.total || 0), "No workflow runs")}
        <div class="record-list">
          ${(data.workflow_runs.latest || []).length ? (data.workflow_runs.latest || []).map(workflowRunRecord).join("") : `<div class="empty-state">No workflow run records</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Provisioning Requests</h2>
        <div class="record-list">
          ${data.provisioning_requests.length ? data.provisioning_requests.map(provisioningRecord).join("") : `<div class="empty-state">No provisioning requests</div>`}
        </div>
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Roles</h2>
        <div class="record-list">
          ${data.roles.length ? data.roles.map(roleRecord).join("") : `<div class="empty-state">No roles</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>AgentBus And Messages</h2>
        <div class="record-list">
          ${data.agentbus_streams.length ? data.agentbus_streams.slice(0, 40).map(agentBusRecord).join("") : `<div class="empty-state">No AgentBus streams</div>`}
          ${data.messages.length ? data.messages.slice(0, 20).map(messageRecord).join("") : ""}
        </div>
      </div>
    </section>
    <section class="surface">
      <h2>Nap Schedules</h2>
      <div class="record-list">
        ${data.nap_schedules.length || data.nap_runs.length
        ? [...data.nap_schedules.map(napScheduleRecord), ...data.nap_runs.slice(0, 20).map(napRunRecord)].join("")
        : `<div class="empty-state">No nap activity</div>`}
      </div>
    </section>
  `;
}
function renderIntegrations() {
    const data = mustData();
    const failingEvalRuns = data.eval_runs.filter((run) => run.passed === false);
    return `
    <section class="metric-grid">
      ${metric("Beads Repos", data.beads_repositories.length, "registered issue sources")}
      ${metric("Bridge Items", data.bridge_items.length, "imported project items")}
      ${metric("Artifacts", data.artifacts.length, "registered outputs")}
      ${metric("Eval Runs", data.eval_runs.length, `${failingEvalRuns.length} failing`)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Beads Bridge</h2>
        <div class="record-list">
          ${data.beads_repositories.length ? data.beads_repositories.map(beadsRepositoryRecord).join("") : `<div class="empty-state">No Beads repositories</div>`}
          ${data.bridge_items.length ? data.bridge_items.slice(0, 30).map(bridgeItemRecord).join("") : ""}
        </div>
      </div>
      <div class="surface">
        <h2>Artifacts</h2>
        <div class="record-list">
          ${data.artifacts.length ? data.artifacts.slice(0, 40).map(artifactRecord).join("") : `<div class="empty-state">No artifacts</div>`}
        </div>
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Evaluations</h2>
        <div class="record-list">
          ${data.eval_sets.length ? data.eval_sets.map(evalSetRecord).join("") : `<div class="empty-state">No eval sets</div>`}
          ${data.eval_runs.length ? data.eval_runs.slice(0, 40).map(evalRunRecord).join("") : ""}
        </div>
      </div>
      <div class="surface">
        <h2>Memory</h2>
        <div class="record-list">
          ${data.memory_records.length ? data.memory_records.slice().reverse().slice(0, 40).map(memoryRecord).join("") : `<div class="empty-state">No memory records</div>`}
        </div>
      </div>
    </section>
  `;
}
function renderRuntime() {
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
function renderSecrets() {
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
function renderObservability() {
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
    const integrationFindings = data.integration_findings || [];
    const openIntegrationFindings = integrationFindings.filter((item) => item.status === "open");
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
      ${metric("Integration Findings", integrationFindings.length, `${openIntegrationFindings.length} open`)}
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
        <h2>Integration Findings</h2>
        ${chip(`${openIntegrationFindings.length} open`, openIntegrationFindings.length ? "warn" : "good")}
      </div>
      <div class="observability-feed">
        ${integrationFindings.length
        ? integrationFindings.slice(0, 80).map(integrationFindingRecord).join("")
        : `<div class="empty-state">No integration findings</div>`}
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
function integrationFindingRecord(item) {
    const repo = item.detail?.repository;
    const sourceLabel = typeof repo?.name === "string" ? repo.name : item.source_id;
    return `
    <article class="feed-item">
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <p class="muted small">${escapeHtml(item.finding_type)} · ${escapeHtml(sourceLabel)} · ${escapeHtml(formatAge(item.last_seen_at))}</p>
        <p class="muted small mono">${escapeHtml(item.fingerprint.slice(0, 16))}</p>
      </div>
      <div class="chip-row">
        ${chip(item.status, item.status === "open" ? "warn" : "good")}
        ${chip(item.severity, item.severity === "critical" || item.severity === "error" ? "bad" : item.severity === "warning" ? "warn" : "info")}
      </div>
    </article>
  `;
}
function notificationRecord(item) {
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
function dispatchRecord(item) {
    return `
    <article class="record compact ${selectedClass(item.task.id)}">
      <div class="record-header">
        <div><h3>${escapeHtml(item.task.title)}</h3><p class="muted small mono">${escapeHtml(item.task.id)}</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(item.task.id)}">Select</button>
      </div>
      <div class="chip-row">
        ${chip(`${item.eligible_agent_count} eligible`, item.eligible_agent_count ? "good" : "bad")}
        ${item.candidates.slice(0, 8).map((candidate) => chip(candidate.agent_name, candidate.eligible ? "good" : "warn")).join("")}
      </div>
    </article>
  `;
}
function taskDependencyRecords(data) {
    const tasksById = new Map(data.tasks.map((detail) => [detail.task.id, detail.task]));
    const edges = data.tasks.flatMap((detail) => (detail.task.dependencies || []).map((dependencyId) => ({ task: detail.task, dependency: tasksById.get(dependencyId), dependencyId })));
    if (!edges.length)
        return `<div class="empty-state">No task dependencies</div>`;
    return edges.slice(0, 40).map((edge) => `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(edge.dependency?.title || edge.dependencyId)}</h3><p class="muted small">blocks</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(edge.task.id)}">Select child</button>
      </div>
      <p>${escapeHtml(edge.task.title)}</p>
      <p class="muted small mono">${escapeHtml(edge.dependencyId)} -> ${escapeHtml(edge.task.id)}</p>
    </article>
  `).join("");
}
function roleRecord(role) {
    return `
    <article class="record compact ${selectedClass(String(role.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(role.display_name || role.name || role.slug || role.id)}</h3><p class="muted small mono">${escapeHtml(role.id)}</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(role.id)}">Select</button>
      </div>
      <div class="chip-row">
        ${chip(role.level || "role", "info")}
        ${(role.required_capabilities || []).slice(0, 6).map((cap) => chip(cap, "good")).join("")}
      </div>
      <p class="muted small">${escapeHtml(role.description || "")}</p>
    </article>
  `;
}
function provisioningRecord(item) {
    return `
    <article class="record compact ${selectedClass(String(item.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(item.reason || item.id)}</h3><p class="muted small mono">${escapeHtml(item.id)}</p></div>
        ${chip(item.status, item.status === "pending" ? "warn" : item.status === "fulfilled" ? "good" : "bad")}
      </div>
      <div class="chip-row">
        ${(item.capabilities || []).map((cap) => chip(cap, "info")).join("")}
        ${item.role_slug ? chip(item.role_slug, "good") : ""}
        ${item.task_id ? chip(`task ${shortHash(String(item.task_id))}`, "info") : ""}
      </div>
    </article>
  `;
}
function workflowRunRecord(run) {
    return `
    <article class="record compact ${selectedClass(String(run.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(run.workflow_id || run.id)}</h3><p class="muted small mono">${escapeHtml(run.id)}</p></div>
        ${chip(run.state, run.state === "completed" ? "good" : run.state === "failed" ? "bad" : "info")}
      </div>
      <div class="row-grid compact-grid">
        ${field("Tenant", run.tenant_id || "global")}
        ${field("Started", formatAge(String(run.started_at || run.created_at || "")))}
        ${field("Current node", run.current_node_key || "none")}
        ${field("Task", run.task_id || "none")}
      </div>
    </article>
  `;
}
function agentBusRecord(stream) {
    return `
    <article class="record compact ${selectedClass(String(stream.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(stream.topic || stream.content_type || stream.id)}</h3><p class="muted small mono">${escapeHtml(stream.id)}</p></div>
        ${chip(stream.status, stream.status === "open" ? "good" : "info")}
      </div>
      <p class="muted small">${escapeHtml(stream.sender_agent_id || "unknown")} -> ${escapeHtml(stream.recipient_agent_id || "broadcast")}</p>
    </article>
  `;
}
function messageRecord(message) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(message.message_type || message.id)}</h3><p class="muted small mono">${escapeHtml(message.id)}</p></div>${chip(message.status, message.status === "pending" ? "warn" : "good")}</div>
      <p class="muted small">${escapeHtml(message.sender_agent_id || "unknown")} -> ${escapeHtml(message.recipient_agent_id || "broadcast")}</p>
    </article>
  `;
}
function napScheduleRecord(schedule) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(schedule.agent_id)}</h3><p class="muted small mono">${escapeHtml(schedule.id)}</p></div>${chip(schedule.enabled ? "enabled" : "disabled", schedule.enabled ? "good" : "warn")}</div>
      <div class="row-grid compact-grid">
        ${field("Offset", schedule.offset_minutes)}
        ${field("Window", schedule.window_minutes)}
        ${field("Updated", formatAge(String(schedule.updated_at || "")))}
        ${field("Actor", schedule.actor || "agent")}
      </div>
    </article>
  `;
}
function napRunRecord(run) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(run.agent_id)}</h3><p class="muted small mono">${escapeHtml(run.id)}</p></div>${chip(run.status, run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "info")}</div>
      <p class="muted small">${escapeHtml(formatAge(String(run.started_at || run.created_at || "")))}</p>
    </article>
  `;
}
function beadsRepositoryRecord(repo) {
    return `
    <article class="record compact ${selectedClass(String(repo.id))}">
      <div class="record-header"><div><h3>${escapeHtml(repo.name)}</h3><p class="muted small mono">${escapeHtml(repo.id)}</p></div>${chip(repo.enabled ? "enabled" : "disabled", repo.enabled ? "good" : "warn")}</div>
      <div class="row-grid compact-grid">
        ${field("Project", repo.project || "none")}
        ${field("Source", repo.source || "none")}
        ${field("Poll", `${repo.poll_interval_seconds || 0}s`)}
        ${field("Path", repo.path || "none")}
      </div>
    </article>
  `;
}
function bridgeItemRecord(item) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(item.title || item.external_id)}</h3><p class="muted small mono">${escapeHtml(item.id)}</p></div>${chip(item.status || "imported", "info")}</div>
      <p class="muted small">${escapeHtml(item.source || "source")} / ${escapeHtml(item.project || "")}</p>
    </article>
  `;
}
function artifactRecord(artifact) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(artifact.kind || "artifact")}</h3><p class="muted small mono">${escapeHtml(artifact.id)}</p></div>${chip(shortHash(String(artifact.digest || "")), "good")}</div>
      <p class="muted small">${escapeHtml(artifact.uri || "")}</p>
    </article>
  `;
}
function evalSetRecord(evalSet) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(evalSet.name)}</h3><p class="muted small mono">${escapeHtml(evalSet.id)}</p></div>${chip(evalSet.scoring || "eval", "info")}</div>
      <div class="row-grid compact-grid">
        ${field("Baseline", evalSet.baseline_score ?? "none")}
        ${field("Regression", evalSet.regression_threshold ?? "none")}
        ${field("Created", formatAge(String(evalSet.created_at || "")))}
        ${field("Updated", formatAge(String(evalSet.updated_at || "")))}
      </div>
    </article>
  `;
}
function evalRunRecord(run) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(run.target_kind || "target")} ${escapeHtml(run.target_id || "")}</h3><p class="muted small mono">${escapeHtml(run.id)}</p></div>${chip(run.passed ? "passed" : "failed", run.passed ? "good" : "bad")}</div>
      <div class="score-line"><span class="bar-track"><span class="bar-fill" style="width:${Math.max(2, Math.min(100, Number(run.score || 0) * 100))}%"></span></span><span class="mono small">${escapeHtml(run.score ?? "n/a")}</span></div>
    </article>
  `;
}
function memoryRecord(memory) {
    return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(memory.record_type || "memory")}</h3><p class="muted small mono">${escapeHtml(memory.id)}</p></div>${chip(memory.subject_type || "memory", "info")}</div>
      <p>${escapeHtml(memory.content || "")}</p>
      <p class="muted small">${escapeHtml(memory.task_id || "")} ${escapeHtml(formatAge(String(memory.created_at || "")))}</p>
    </article>
  `;
}
function selectedProjectSummary(data) {
    if (state.projectFilter !== "all") {
        return data.project_summaries.find((project) => project.project === state.projectFilter) || null;
    }
    if (state.selectedId) {
        const selectedTask = taskDetailById(data, state.selectedId);
        if (selectedTask) {
            const project = taskProject(selectedTask.task);
            return data.project_summaries.find((item) => item.project === project) || null;
        }
    }
    return data.project_summaries[0] || null;
}
function selectedTaskDetail(data) {
    if (!state.selectedId)
        return null;
    return taskDetailById(data, state.selectedId);
}
function taskDetailById(data, taskId) {
    return data.tasks.find((detail) => detail.task.id === taskId) || null;
}
function taskProject(task) {
    if (task.project)
        return String(task.project);
    const metadata = task.metadata || {};
    for (const key of ["project", "repository", "repo"]) {
        const value = metadata[key];
        if (value)
            return String(value);
    }
    const origin = metadata.origin;
    if (origin) {
        for (const key of ["project", "repository", "repo", "source"]) {
            const value = origin[key];
            if (value)
                return String(value);
        }
    }
    return "unassigned";
}
function projectFrontierRecord(project) {
    const ready = project.frontier_tasks.slice(0, 4);
    return `
    <article class="project-row ${state.projectFilter === project.project ? "is-selected" : ""}">
      <div>
        <div class="record-header">
          <div><h3>${escapeHtml(project.project)}</h3><p class="muted small">${project.task_count} stories, ${project.active_agent_ids.length} active agents</p></div>
          <button class="link-button" type="button" data-project="${escapeHtml(project.project)}">Focus</button>
        </div>
        <div class="chip-row">
          ${chip(`${project.ready_count} ready`, project.ready_count ? "good" : "info")}
          ${chip(`${project.blocked_count} blocked`, project.blocked_count ? "warn" : "good")}
          ${chip(`${project.review_count} review`, project.review_count ? "warn" : "info")}
          ${project.cross_project_dependency_count ? chip(`${project.cross_project_dependency_count} cross-project`, "warn") : ""}
        </div>
      </div>
      <div class="story-stack">
        ${ready.length ? ready.map((task) => storyButton(task)).join("") : `<span class="muted small">No ready stories</span>`}
      </div>
    </article>
  `;
}
function storyButton(task) {
    return `<button class="story-button ${selectedClass(task.id)}" type="button" data-select-id="${escapeHtml(task.id)}"><span>${escapeHtml(task.title)}</span><span class="mono small">${escapeHtml(task.id)}</span></button>`;
}
function storyScopePanel(data, detail) {
    const task = detail.task;
    const related = relatedAgentsForTask(data, detail);
    const dependencyDetails = (task.dependencies || []).map((id) => taskDetailById(data, id)).filter(Boolean);
    const dependents = data.tasks.filter((candidate) => (candidate.task.dependencies || []).includes(task.id));
    return `
    <div class="story-scope">
      <div>
        <h3>${escapeHtml(task.title)}</h3>
        <p class="muted small mono">${escapeHtml(task.id)} / ${escapeHtml(taskProject(task))}</p>
        <div class="chip-row">
          ${chip(task.state, statusTone(task.state))}
          ${chip(`P${task.priority || 0}`, "info")}
          ${(task.required_capabilities || []).map((capability) => chip(capability, "info")).join("")}
        </div>
      </div>
      <div class="relationship-strip">
        ${related.length ? related.map(({ item, relation }) => scopedAgentPill(item, relation)).join("") : `<div class="empty-state">No agents attached to this story yet</div>`}
      </div>
      <div class="split compact-split">
        <div>
          <h3>Blocks This Story</h3>
          <div class="story-stack">${dependencyDetails.length ? dependencyDetails.map((item) => storyButton(item.task)).join("") : `<span class="muted small">No dependencies</span>`}</div>
        </div>
        <div>
          <h3>Unblocks Next</h3>
          <div class="story-stack">${dependents.length ? dependents.slice(0, 8).map((item) => storyButton(item.task)).join("") : `<span class="muted small">No dependents</span>`}</div>
        </div>
      </div>
    </div>
  `;
}
function relatedAgentsForTask(data, detail) {
    const relations = new Map();
    const add = (agentId, relation) => {
        const id = String(agentId || "").trim();
        if (!id)
            return;
        if (!relations.has(id))
            relations.set(id, new Set());
        relations.get(id)?.add(relation);
    };
    add(detail.task.owner_agent_id, "writing");
    for (const review of detail.reviews || [])
        add(review.reviewer_agent_id, "reviewing");
    for (const evidence of detail.evidence || []) {
        const kind = String(evidence.kind || "");
        add(evidence.created_by, kind === "test" ? "testing" : kind === "publication" ? "deploying" : "evidence");
    }
    for (const event of detail.history || [])
        add(event.actor, "history");
    for (const dependencyId of detail.task.dependencies || []) {
        const dependency = taskDetailById(data, dependencyId);
        add(dependency?.task.owner_agent_id, "dependency");
    }
    const byId = new Map(data.agents.map((item) => [item.agent.id, item]));
    return Array.from(relations.entries())
        .map(([agentId, relationSet]) => {
        const item = byId.get(agentId);
        return item ? { item, relation: Array.from(relationSet).join(", ") } : null;
    })
        .filter(Boolean);
}
function scopedAgentPill(item, relation) {
    return `
    <button class="agent-pill ${selectedClass(item.agent.id)}" type="button" data-select-id="${escapeHtml(item.agent.id)}">
      <span class="mono">${escapeHtml(item.agent.name)}</span>
      <span>${escapeHtml(relation)}</span>
      <span>${escapeHtml(item.agent.status)} / ${escapeHtml(item.agent.health_status)}</span>
    </button>
  `;
}
function projectAgentsPanel(data, project) {
    const projectName = project?.project || "all";
    const agents = data.agents.filter((item) => projectName === "all" ? item.active_tasks.length : (item.active_projects || []).includes(projectName));
    if (!agents.length)
        return `<div class="empty-state">No active agents in this scope</div>`;
    return agentTable(agents.slice(0, 40), data, true);
}
function dependencyOrderPanel(data, project) {
    const projects = project ? [project] : data.project_summaries;
    const waiting = projects.flatMap((item) => item.waiting_tasks.map((task) => ({ project: item.project, task }))).slice(0, 12);
    const edges = projects.flatMap((item) => item.cross_project_edges.map((edge) => ({ project: item.project, edge }))).slice(0, 12);
    return `
    <div class="record-list">
      ${waiting.map(({ project: projectName, task }) => `
        <article class="record compact">
          <div class="record-header"><div><h3>${escapeHtml(task.title)}</h3><p class="muted small">${escapeHtml(projectName)}</p></div>${chip("waiting", "warn")}</div>
          <p class="muted small mono">${escapeHtml((task.waiting_on || []).join(" -> "))}</p>
        </article>
      `).join("")}
      ${edges.map(({ project: projectName, edge }) => `
        <article class="record compact">
          <div class="record-header"><div><h3>${escapeHtml(String(edge.from_project || "project"))} -> ${escapeHtml(projectName)}</h3><p class="muted small">${escapeHtml(String(edge.from_task_title || edge.from_task_id || ""))}</p></div>${chip("cross-project", "warn")}</div>
          <p class="muted small">${escapeHtml(String(edge.to_task_title || edge.to_task_id || ""))}</p>
        </article>
      `).join("")}
      ${!waiting.length && !edges.length ? `<div class="empty-state">No dependency waits in scope</div>` : ""}
    </div>
  `;
}
function filteredAgents(data) {
    const query = state.agentQuery.trim().toLowerCase();
    const agents = data.agents.filter((item) => {
        const projects = item.active_projects || [];
        const haystack = [
            item.agent.name,
            item.agent.id,
            item.machine?.hostname || "",
            item.agent.status,
            item.agent.health_status,
            ...projects,
            ...(item.agent.capabilities || []),
        ].join(" ").toLowerCase();
        const matchesQuery = !query || haystack.includes(query);
        const matchesProject = state.projectFilter === "all" || projects.includes(state.projectFilter);
        const matchesFilter = state.agentFilter === "all" ||
            (state.agentFilter === "eligible" && item.availability.eligible) ||
            (state.agentFilter === "blocked" && !item.availability.eligible) ||
            item.agent.status === state.agentFilter ||
            item.agent.health_status === state.agentFilter;
        return matchesQuery && matchesProject && matchesFilter;
    });
    return agents.sort(agentSort);
}
function agentSort(left, right) {
    if (state.agentSort === "status")
        return compareText(`${left.agent.status} ${left.agent.name}`, `${right.agent.status} ${right.agent.name}`);
    if (state.agentSort === "project")
        return compareText((left.active_projects || []).join(",") || "idle", (right.active_projects || []).join(",") || "idle") || compareText(left.agent.name, right.agent.name);
    if (state.agentSort === "capacity")
        return (right.capacity - right.active_lease_count) - (left.capacity - left.active_lease_count) || compareText(left.agent.name, right.agent.name);
    if (state.agentSort === "last_seen")
        return compareText(String(right.agent.last_seen_at || ""), String(left.agent.last_seen_at || ""));
    return compareText(left.agent.name, right.agent.name);
}
function compareText(left, right) {
    return left.localeCompare(right, undefined, { sensitivity: "base", numeric: true });
}
function agentTable(agents, data, compact = false) {
    if (!agents.length)
        return `<div class="empty-state">No matching agents</div>`;
    return `
    <div class="table-wrap">
      <table class="data-table ${compact ? "compact-table" : ""}">
        <thead><tr><th>Agent</th><th>Project</th><th>Status</th><th>Capacity</th><th>Machine</th><th>Capabilities</th><th>Task</th><th></th></tr></thead>
        <tbody>
          ${agents.map((item) => agentRow(item, data)).join("")}
        </tbody>
      </table>
    </div>
  `;
}
function agentRow(item, data) {
    const task = item.active_tasks[0];
    return `
    <tr class="${selectedClass(item.agent.id)}">
      <td><button class="link-button mono" type="button" data-select-id="${escapeHtml(item.agent.id)}">${escapeHtml(item.agent.name)}</button><br><span class="muted small">${escapeHtml(item.agent.id)}</span></td>
      <td>${escapeHtml((item.active_projects || []).join(", ") || "idle")}</td>
      <td>${chip(item.agent.status, statusTone(item.agent.status))} ${chip(item.agent.health_status, healthTone(item.agent.health_status))}</td>
      <td class="mono">${item.active_lease_count} / ${item.capacity}</td>
      <td>${escapeHtml(item.machine?.hostname || "missing")}</td>
      <td>${escapeHtml((item.agent.capabilities || []).slice(0, 8).join(", ") || "none")}</td>
      <td>${task ? storyButton(task) : `<span class="muted small">none</span>`}</td>
      <td>
        <form class="inline-form" data-action="agentBulkUpdate">
          <input type="hidden" name="agent_ids" value="${escapeHtml(item.agent.id)}">
          <input type="hidden" name="status" value="draining">
          <button type="submit">Drain</button>
        </form>
      </td>
    </tr>
  `;
}
function swarmBuckets(items) {
    if (!items.length)
        return `<div class="empty-state">No data</div>`;
    const total = items.reduce((sum, item) => sum + item.count, 0) || 1;
    return `
    <div class="bucket-list">
      ${items.slice(0, 16).map((item) => `
        <button class="bucket-row" type="button" data-agent-filter-value="${escapeHtml(item.key)}">
          <span>${escapeHtml(item.key)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${Math.max(2, (item.count / total) * 100)}%"></span></span>
          <span class="mono small">${item.count}</span>
        </button>
      `).join("")}
    </div>
  `;
}
function agentCard(item) {
    const agent = item.agent;
    const machine = item.machine;
    const reasons = item.availability.eligible
        ? chip("dispatch eligible", "good")
        : item.availability.reasons.map((reason) => chip(reason, "bad")).join("");
    return `
    <article class="agent-card ${item.availability.eligible ? "" : "is-blocked"} ${selectedClass(agent.id)}">
      <div class="agent-header">
        <div><h2 class="mono">${escapeHtml(agent.name)}</h2><p class="muted small">${escapeHtml(agent.id)}</p></div>
        <div class="chip-row">${chip(agent.status, statusTone(agent.status))}${chip(agent.health_status, healthTone(agent.health_status))}<button class="link-button" type="button" data-select-id="${escapeHtml(agent.id)}">Select</button></div>
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
function taskLane(taskState, tasks, agents) {
    const laneTasks = tasks.filter((detail) => detail.task.state === taskState);
    return `
    <div class="task-lane">
      <h2><span>${escapeHtml(labelize(taskState))}</span><span class="pill">${laneTasks.length}</span></h2>
      ${laneTasks.length ? laneTasks.map((detail) => taskCard(detail, agents)).join("") : `<div class="empty-state">Empty</div>`}
    </div>
  `;
}
function taskCard(detail, agents) {
    const task = detail.task;
    const owner = agents.find((item) => item.agent.id === task.owner_agent_id)?.agent;
    const origin = taskOrigin(task);
    const evidenceOptions = detail.evidence.map((item) => option(String(item.id), String(item.id), "")).join("");
    const pendingReviews = detail.reviews.filter((review) => review.status === "pending");
    return `
    <article class="task-card ${selectedClass(task.id)}">
      <div class="record-header">
        <div><h3>${escapeHtml(task.title)}</h3><p class="muted small mono">${escapeHtml(task.id)}</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(task.id)}">Select</button>
      </div>
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
function hermesRecord(instance, data) {
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
function runtimeRecord(runtime, data) {
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
function rolloutRecord(status, data) {
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
function secretRecord(secret, agents) {
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
function secretAuditRecord(audit) {
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
function bindViewControls() {
    const search = document.querySelector("#agentSearch");
    if (search)
        search.addEventListener("input", (event) => {
            state.agentQuery = event.target.value;
            state.agentPage = 1;
            updateUrlState(true);
            render();
        });
    const projectFilter = document.querySelector("#projectFilter");
    if (projectFilter)
        projectFilter.addEventListener("change", (event) => {
            state.projectFilter = event.target.value;
            state.agentPage = 1;
            updateUrlState();
            render();
        });
    const agentProjectFilter = document.querySelector("#agentProjectFilter");
    if (agentProjectFilter)
        agentProjectFilter.addEventListener("change", (event) => {
            state.projectFilter = event.target.value;
            state.agentPage = 1;
            updateUrlState();
            render();
        });
    const agentFilter = document.querySelector("#agentFilter");
    if (agentFilter)
        agentFilter.addEventListener("change", (event) => {
            state.agentFilter = event.target.value;
            state.agentPage = 1;
            updateUrlState();
            render();
        });
    const agentSort = document.querySelector("#agentSort");
    if (agentSort)
        agentSort.addEventListener("change", (event) => {
            state.agentSort = event.target.value;
            updateUrlState();
            render();
        });
    const clearAgents = document.querySelector("#clearAgentFilters");
    if (clearAgents)
        clearAgents.addEventListener("click", () => {
            state.agentQuery = "";
            state.agentFilter = "all";
            state.agentSort = "name";
            state.agentPage = 1;
            state.projectFilter = "all";
            updateUrlState();
            render();
        });
    const clearWorkScope = document.querySelector("#clearWorkScope");
    if (clearWorkScope)
        clearWorkScope.addEventListener("click", () => {
            state.projectFilter = "all";
            state.selectedId = "";
            updateUrlState();
            render();
        });
    const prevAgentPage = document.querySelector("#agentPrevPage");
    if (prevAgentPage)
        prevAgentPage.addEventListener("click", () => {
            state.agentPage = Math.max(1, state.agentPage - 1);
            updateUrlState();
            render();
        });
    const nextAgentPage = document.querySelector("#agentNextPage");
    if (nextAgentPage)
        nextAgentPage.addEventListener("click", () => {
            state.agentPage += 1;
            updateUrlState();
            render();
        });
    const taskFilter = document.querySelector("#taskFilter");
    if (taskFilter)
        taskFilter.addEventListener("change", (event) => {
            state.taskFilter = event.target.value;
            updateUrlState();
            render();
        });
    const clearTasks = document.querySelector("#clearTaskFilter");
    if (clearTasks)
        clearTasks.addEventListener("click", () => {
            state.taskFilter = "all";
            updateUrlState();
            render();
        });
}
function handleContentClick(event) {
    const projectTarget = event.target?.closest("[data-project]");
    if (projectTarget) {
        state.projectFilter = projectTarget.dataset.project || "all";
        state.agentPage = 1;
        updateUrlState();
        render();
        return;
    }
    const bucketTarget = event.target?.closest("[data-agent-filter-value]");
    if (bucketTarget) {
        const value = bucketTarget.dataset.agentFilterValue || "";
        if (value && value !== "idle") {
            state.projectFilter = value;
            state.activeView = "agents";
            state.agentPage = 1;
            updateUrlState();
            render();
        }
        return;
    }
    const target = event.target?.closest("[data-select-id]");
    if (!target)
        return;
    const selectedId = target.dataset.selectId || "";
    if (!selectedId)
        return;
    state.selectedId = state.selectedId === selectedId ? "" : selectedId;
    updateUrlState();
    render();
}
function syncObservabilitySubscription() {
    if (state.activeView === "observability" && state.data) {
        startObservabilityStream();
    }
    else {
        stopObservabilityStream();
    }
}
function startObservabilityStream() {
    if (state.observabilityStream)
        return;
    const controller = new AbortController();
    state.observabilityStream = controller;
    state.observabilityStreamStatus = "connecting";
    const latest = uniqueObservations([
        ...state.observabilityLive,
        ...(state.data?.observability.latest || []),
    ]);
    const after = latest.length ? latest[0].sequence : 0;
    const headers = { Accept: "application/x-ndjson" };
    if (state.token)
        headers.Authorization = `Bearer ${state.token}`;
    fetch(`/observability/stream?after_sequence=${encodeURIComponent(after)}&timeout_seconds=60&poll_interval_seconds=0.5`, {
        headers,
        signal: controller.signal,
    })
        .then(async (response) => {
        if (!response.ok)
            throw new Error(`${response.status} ${response.statusText}`);
        state.observabilityStreamStatus = "connected";
        renderSyncState();
        const reader = response.body?.getReader();
        if (!reader)
            return;
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done)
                break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            for (const line of lines) {
                const text = line.trim();
                if (!text)
                    continue;
                state.observabilityLive = uniqueObservations([
                    JSON.parse(text),
                    ...state.observabilityLive,
                ]).slice(0, 120);
            }
            if (state.activeView === "observability")
                render();
        }
    })
        .catch((error) => {
        if (!controller.signal.aborted) {
            state.observabilityStreamStatus = "error";
            state.actionMessage = `Observability stream failed: ${error instanceof Error ? error.message : String(error)}`;
            if (state.activeView === "observability")
                render();
        }
    })
        .finally(() => {
        if (state.observabilityStream === controller)
            state.observabilityStream = null;
        if (!controller.signal.aborted && state.activeView === "observability") {
            state.observabilityStreamStatus = "reconnecting";
            window.setTimeout(startObservabilityStream, 1000);
        }
    });
}
function stopObservabilityStream() {
    if (!state.observabilityStream)
        return;
    state.observabilityStream.abort();
    state.observabilityStream = null;
    state.observabilityStreamStatus = "idle";
}
async function handleActionSubmit(event) {
    const form = event.target?.closest("form[data-action]");
    if (!form)
        return;
    event.preventDefault();
    const action = form.dataset.action || "";
    const values = formValues(form);
    try {
        const result = await runAction(action, form, values);
        state.actionMessage = `${labelize(action)} ok: ${redactedJson(result)}`;
        await loadDashboard();
    }
    catch (error) {
        state.actionMessage = `${labelize(action)} failed: ${error instanceof Error ? error.message : String(error)}`;
        render();
    }
}
async function runAction(action, form, values) {
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
    if (action === "agentBulkUpdate") {
        const body = {
            agent_ids: String(values.agent_ids || "").split(",").map((item) => item.trim()).filter(Boolean),
        };
        if (String(values.status || "").trim())
            body.status = String(values.status).trim();
        if (String(values.health_status || "").trim())
            body.health_status = String(values.health_status).trim();
        if (String(values.capabilities || "").trim()) {
            body.capabilities = String(values.capabilities).split(",").map((item) => item.trim()).filter(Boolean);
        }
        return postJSON("/agents/bulk", body);
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
    if (action === "workflowDraftCreate") {
        return postJSON("/workflows/drafts", {
            goal: requiredString(values.goal),
            proposed_steps: parseJsonArray(values.proposed_steps),
            questions: parseJsonArray(values.questions),
            answers: parseJsonObject(values.answers),
        });
    }
    if (action === "workflowDraftPreview") {
        return postJSON(`/workflows/drafts/${encodeURIComponent(requiredDataset(form, "draftId"))}/preview`, {
            input: parseJsonObject(values.input),
        });
    }
    if (action === "workflowDraftApprove") {
        return postJSON(`/workflows/drafts/${encodeURIComponent(requiredDataset(form, "draftId"))}/approve`, {
            slug: requiredString(values.slug),
            name: requiredString(values.name),
        });
    }
    if (action === "workflowPreview") {
        return postJSON(`/workflows/${encodeURIComponent(requiredDataset(form, "workflowId"))}/preview`, {
            input: parseJsonObject(values.input),
        });
    }
    if (action === "workflowStart") {
        return postJSON(`/workflows/${encodeURIComponent(requiredDataset(form, "workflowId"))}/start`, {
            started_by: requiredString(values.started_by),
            input: parseJsonObject(values.input),
        });
    }
    if (action === "notifierConfigure") {
        return postJSON("/notifier/channels", {
            name: requiredString(values.name),
            channel_type: requiredString(values.channel_type),
            event_types: String(values.event_types || "").split(",").map((item) => item.trim()).filter(Boolean),
            target: parseJsonObject(values.target),
            enabled: true,
        });
    }
    if (action === "notifierDeliver") {
        return postJSON("/notifier/deliver", {
            limit: numberValue(values.limit, 50),
        });
    }
    throw new Error(`unsupported action: ${action}`);
}
function postJSON(path, body) {
    return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
}
function formValues(form) {
    const values = {};
    new FormData(form).forEach((value, key) => {
        values[key] = String(value);
    });
    return values;
}
function relationshipGraph(data) {
    const machines = data.machines.slice(0, 8);
    const agents = data.agents.slice(0, 12).map((item) => item.agent);
    const activeTasks = data.tasks
        .filter((detail) => !TERMINAL_TASK_STATES.has(detail.task.state))
        .slice(0, 14)
        .map((detail) => detail.task);
    const nodes = [
        ...machines.map((machine, index) => graphNode(machine.id, machine.hostname, "machine", 80, 60 + index * 58)),
        ...agents.map((agent, index) => graphNode(agent.id, agent.name, "agent", 330, 46 + index * 48)),
        ...activeTasks.map((task, index) => graphNode(task.id, task.title, "task", 610, 44 + index * 46)),
    ];
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const edges = [];
    for (const agent of agents) {
        edges.push({ from: agent.machine_id, to: agent.id, tone: "machine-agent" });
    }
    for (const task of activeTasks) {
        if (task.owner_agent_id)
            edges.push({ from: task.owner_agent_id, to: task.id, tone: "agent-task" });
        for (const dependency of task.dependencies || []) {
            edges.push({ from: dependency, to: task.id, tone: "dependency" });
        }
    }
    const height = Math.max(360, 90 + Math.max(machines.length * 58, agents.length * 48, activeTasks.length * 46));
    const edgeSvg = edges.map((edge) => {
        const from = byId.get(edge.from);
        const to = byId.get(edge.to);
        if (!from || !to)
            return "";
        return `<path class="graph-edge graph-edge-${edge.tone}" d="M${from.x + 90},${from.y} C${from.x + 170},${from.y} ${to.x - 170},${to.y} ${to.x - 90},${to.y}"></path>`;
    }).join("");
    const nodeSvg = nodes.map((node) => `
    <g class="graph-node graph-node-${node.kind} ${selectedClass(node.id)}" data-select-id="${escapeHtml(node.id)}" transform="translate(${node.x},${node.y})">
      <rect x="-86" y="-18" width="172" height="36" rx="8"></rect>
      <text text-anchor="middle" y="4">${escapeHtml(truncate(node.label, 22))}</text>
    </g>
  `).join("");
    return `
    <div class="graph-wrap">
      <svg class="relationship-graph" viewBox="0 0 760 ${height}" role="img" aria-label="Fleet topology graph">
        <text class="graph-column-label" x="80" y="24">Machines</text>
        <text class="graph-column-label" x="330" y="24">Agents</text>
        <text class="graph-column-label" x="610" y="24">Active Tasks</text>
        ${edgeSvg}
        ${nodeSvg}
      </svg>
    </div>
  `;
}
function graphNode(id, label, kind, x, y) {
    return { id, label, kind, x, y };
}
function metric(label, value, note) {
    return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
}
function stateBars(states, counts, total, emptyLabel = "No tasks") {
    if (!total)
        return `<div class="empty-state">${escapeHtml(emptyLabel)}</div>`;
    return `<div class="state-bar">${states.map((name) => {
        const count = counts[name] || 0;
        const width = Math.max(2, Math.round((count / total) * 100));
        return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
    }).join("")}</div>`;
}
function observationMetric(item) {
    return `
    <article class="metric-observation">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <p class="muted small">${escapeHtml(item.layer)} / ${escapeHtml(item.source)} · ${escapeHtml(formatAge(item.created_at))}</p>
      </div>
      <div class="metric-observation-value">${escapeHtml(formatMetricValue(item))}</div>
    </article>
  `;
}
function observationRecord(item) {
    const subject = item.subject_type && item.subject_id ? `${item.subject_type}:${item.subject_id}` : "";
    return `
    <article class="observation-row tone-left-${observationTone(item.level)}">
      <div class="observation-main">
        <span class="mono small">#${escapeHtml(item.sequence)}</span>
        ${chip(item.kind, item.kind === "metric" ? "info" : observationTone(item.level))}
        ${chip(item.level, observationTone(item.level))}
        <strong>${escapeHtml(item.name)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.layer)} / ${escapeHtml(item.source)} ${subject ? `· ${escapeHtml(subject)}` : ""} · ${escapeHtml(formatAge(item.created_at))}</div>
      <div class="observation-detail">${escapeHtml(item.kind === "metric" ? formatMetricValue(item) : jsonSummary(item.detail))}</div>
    </article>
  `;
}
function commandAuditRecord(item) {
    const tone = item.phase === "completed" || item.phase === "started" ? "info" : "bad";
    const subject = item.task_id ? `task:${item.task_id}` : `agent:${item.agent_id}`;
    const argv = (item.argv || []).join(" ");
    const result = item.returncode === null || item.returncode === undefined ? "" : ` rc=${item.returncode}`;
    const duration = item.duration_ms === null || item.duration_ms === undefined ? "" : ` ${Math.round(item.duration_ms)}ms`;
    return `
    <article class="observation-row tone-left-${tone}">
      <div class="observation-main">
        ${chip(item.phase, tone)}
        <strong>${escapeHtml(item.command_id)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.agent_id)} · ${escapeHtml(subject)} · ${escapeHtml(formatAge(item.created_at))}${escapeHtml(result)}${escapeHtml(duration)}</div>
      <div class="observation-detail mono">${escapeHtml(argv)}</div>
      <div class="muted small">${escapeHtml(item.cwd)}</div>
    </article>
  `;
}
function uniqueObservations(items) {
    const seen = new Set();
    const unique = [];
    for (const item of items.sort((a, b) => Number(b.sequence || 0) - Number(a.sequence || 0))) {
        if (seen.has(item.sequence))
            continue;
        seen.add(item.sequence);
        unique.push(item);
    }
    return unique;
}
function attentionList(data) {
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
function field(label, value) {
    return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span></div>`;
}
function chip(value, tone = "info") {
    return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}
function timelineItem(eventType, actor, createdAt) {
    return `<div class="timeline-item"><span class="mono small">${escapeHtml(labelize(eventType))}</span><br><span class="muted small">${escapeHtml(actor)} ${escapeHtml(formatAge(createdAt))}</span></div>`;
}
function agentSelect(name, agents, selected) {
    return `<select name="${escapeHtml(name)}"><option value="">Select agent</option>${agents.map((item) => option(item.agent.id, item.agent.name, selected)).join("")}</select>`;
}
function select(name, values, selected) {
    return `<select name="${escapeHtml(name)}">${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}
function option(value, label, selected) {
    return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}
function taskOrigin(task) {
    const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
    const origin = metadata.origin;
    return origin && typeof origin === "object" ? origin : {};
}
function mustData() {
    if (!state.data)
        throw new Error("dashboard data is not loaded");
    return state.data;
}
function parseJsonObject(value) {
    const text = String(value || "").trim();
    if (!text)
        return {};
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("expected a JSON object");
    }
    return parsed;
}
function parseJsonArray(value) {
    const text = String(value || "").trim();
    if (!text)
        return [];
    const parsed = JSON.parse(text);
    if (!Array.isArray(parsed)) {
        throw new Error("expected a JSON array");
    }
    return parsed;
}
function requiredString(value) {
    const text = String(value || "").trim();
    if (!text)
        throw new Error("required field is blank");
    return text;
}
function requiredDataset(form, key) {
    const value = form.dataset[key];
    if (!value)
        throw new Error(`missing action context: ${key}`);
    return value;
}
function numberValue(value, fallback) {
    const text = String(value || "").trim();
    if (!text)
        return fallback;
    const parsed = Number(text);
    if (!Number.isFinite(parsed))
        throw new Error(`expected number: ${text}`);
    return parsed;
}
function optionalNumber(value) {
    const text = String(value || "").trim();
    return text ? numberValue(text, 0) : null;
}
function emptyToNull(value) {
    const text = String(value || "").trim();
    return text || null;
}
function redactedJson(value) {
    return JSON.stringify(value, (key, item) => key === "value" ? "***REDACTED***" : item);
}
function jsonSummary(value) {
    if (value == null || typeof value !== "object")
        return value == null ? "none" : String(value);
    const keys = Object.keys(value);
    if (!keys.length)
        return "none";
    return keys.slice(0, 4).map((key) => `${key}:${compactValue(value[key])}`).join(", ");
}
function compactValue(value) {
    if (Array.isArray(value))
        return `[${value.slice(0, 3).join("|")}${value.length > 3 ? "|..." : ""}]`;
    if (value && typeof value === "object")
        return "{...}";
    return String(value);
}
function shortHash(value) {
    const text = String(value || "");
    return text.length > 16 ? `${text.slice(0, 12)}...` : text || "no digest";
}
function selectedClass(id) {
    return state.selectedId && state.selectedId === id ? "is-selected" : "";
}
function truncate(value, limit) {
    const text = String(value || "");
    return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
}
function statusTone(status) {
    if (status === "idle")
        return "good";
    if (status === "busy")
        return "info";
    if (status === "draining")
        return "warn";
    return "bad";
}
function healthTone(status) {
    if (status === "healthy")
        return "good";
    if (status === "degraded")
        return "warn";
    return "bad";
}
function observationTone(level) {
    if (level === "critical" || level === "error")
        return "bad";
    if (level === "warning")
        return "warn";
    if (level === "debug")
        return "info";
    return "good";
}
function formatMetricValue(item) {
    if (item.value == null)
        return "none";
    const value = Math.abs(item.value) >= 100 ? Math.round(item.value) : Math.round(item.value * 100) / 100;
    return `${value}${item.unit ? ` ${item.unit}` : ""}`;
}
function rolloutTone(status) {
    if (status === "promoted")
        return "good";
    if (["planned", "canarying", "paused"].includes(status))
        return "info";
    if (["rescuing", "rolled_back"].includes(status))
        return "warn";
    return "bad";
}
function formatAge(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime()))
        return "unknown";
    const diffMs = Date.now() - date.getTime();
    const suffix = diffMs >= 0 ? "ago" : "from now";
    const minutes = Math.max(1, Math.round(Math.abs(diffMs) / 60000));
    if (minutes < 60)
        return `${minutes}m ${suffix}`;
    const hours = Math.round(minutes / 60);
    if (hours < 48)
        return `${hours}h ${suffix}`;
    return `${Math.round(hours / 24)}d ${suffix}`;
}
function formatTime(value) {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(value);
}
function labelize(value) {
    return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
}
function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
        const replacements = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        };
        return replacements[char];
    });
}
function requiredElement(selector) {
    const element = document.querySelector(selector);
    if (!element)
        throw new Error(`Missing dashboard element: ${selector}`);
    return element;
}
