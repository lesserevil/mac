// Top-level render orchestrator. Picks the active view's renderer, paints
// the title/banner/sync state, and re-binds per-view controls.
import { VIEW_TITLES } from "./constants.js";
import { state } from "./state.js";
import { escapeHtml, formatTime } from "./format.js";
import { setObservabilityRenderCallback, setObservabilityRenderSyncStateCallback, syncObservabilitySubscription, } from "./observability_stream.js";
import { renderOverview } from "./views/overview.js";
import { renderAgents } from "./views/agents.js";
import { renderTasks } from "./views/tasks.js";
import { renderHermes } from "./views/hermes.js";
import { renderRuntime } from "./views/runtime.js";
import { renderObservability } from "./views/observability.js";
import { renderSecrets } from "./views/secrets.js";
let nodes = null;
export function setRenderNodes(value) {
    nodes = value;
    // Once nodes are bound we can let the stream module re-render through us.
    setObservabilityRenderCallback(render);
    setObservabilityRenderSyncStateCallback(renderSyncState);
}
export function render() {
    const dom = mustNodes();
    document.querySelectorAll("[data-view]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.view === state.activeView);
    });
    dom.title.textContent = VIEW_TITLES[state.activeView];
    renderSyncState();
    renderBanner();
    if (state.loading && !state.data) {
        dom.content.innerHTML = `<div class="empty-state">Loading</div>`;
        return;
    }
    if (!state.data) {
        dom.content.innerHTML = `<div class="empty-state">No dashboard data</div>`;
        return;
    }
    const action = state.actionMessage
        ? `<div class="action-status">${escapeHtml(state.actionMessage)}</div>`
        : "";
    const body = state.activeView === "agents"
        ? renderAgents()
        : state.activeView === "tasks"
            ? renderTasks()
            : state.activeView === "hermes"
                ? renderHermes()
                : state.activeView === "runtime"
                    ? renderRuntime()
                    : state.activeView === "observability"
                        ? renderObservability()
                        : state.activeView === "secrets"
                            ? renderSecrets()
                            : renderOverview();
    dom.content.innerHTML = `${action}${body}`;
    bindViewControls();
    syncObservabilitySubscription();
}
export function renderSyncState() {
    const dom = mustNodes();
    dom.syncState.textContent = state.loading
        ? "Loading"
        : state.loadedAt
            ? `Loaded ${formatTime(state.loadedAt)}`
            : "Not loaded";
}
export function renderBanner() {
    const dom = mustNodes();
    if (!state.error) {
        dom.banner.hidden = true;
        dom.banner.textContent = "";
        return;
    }
    dom.banner.hidden = false;
    dom.banner.textContent = state.error.includes("403")
        ? "Dashboard data needs a token with read scope."
        : state.error;
}
// Per-view event wiring: search box, filter dropdowns, clear buttons. Runs
// after every render() because innerHTML resets DOM references.
export function bindViewControls() {
    const search = document.querySelector("#agentSearch");
    if (search)
        search.addEventListener("input", (event) => {
            state.agentQuery = event.target.value;
            render();
        });
    const agentFilter = document.querySelector("#agentFilter");
    if (agentFilter)
        agentFilter.addEventListener("change", (event) => {
            state.agentFilter = event.target.value;
            render();
        });
    const clearAgents = document.querySelector("#clearAgentFilters");
    if (clearAgents)
        clearAgents.addEventListener("click", () => {
            state.agentQuery = "";
            state.agentFilter = "all";
            render();
        });
    const taskFilter = document.querySelector("#taskFilter");
    if (taskFilter)
        taskFilter.addEventListener("change", (event) => {
            state.taskFilter = event.target.value;
            render();
        });
    const clearTasks = document.querySelector("#clearTaskFilter");
    if (clearTasks)
        clearTasks.addEventListener("click", () => {
            state.taskFilter = "all";
            render();
        });
}
function mustNodes() {
    if (!nodes)
        throw new Error("render nodes not bound; call setRenderNodes() at boot");
    return nodes;
}
