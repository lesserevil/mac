// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
// Smoke-test markers: view modules provide data-action="dispatchTick" controls and /observability/stream live updates.
import { requestJSON } from "./api-client.js";
import { handleActionSubmit } from "./actions.js";
import { VIEW_TITLES } from "./constants.js";
import { syncObservabilitySubscription } from "./observability-stream.js";
import { nodes, state } from "./state.js";
import { escapeHtml, formatTime } from "./utils.js";
import { bindViewControls } from "./view-controls.js";
import { renderAgents, renderHermes, renderObservability, renderOverview, renderRuntime, renderSecrets, renderTasks } from "./views.js";

nodes.tokenInput.value = state.token;
bindEvents();
loadDashboard();

function bindEvents() {
  nodes.nav.addEventListener("click", (event) => {
    const button = event.target?.closest("[data-view]");
    if (!button) return;
    state.activeView = button.dataset.view || "overview";
    state.actionMessage = null;
    render();
  });
  nodes.refresh.addEventListener("click", () => loadDashboard());
  nodes.content.addEventListener("submit", (event) => {
    handleActionSubmit(event, { loadDashboard, render });
  });
  nodes.tokenForm.addEventListener("submit", (event) => {
    event.preventDefault();
    state.token = nodes.tokenInput.value.trim();
    if (state.token) sessionStorage.setItem("mac.dashboard.token", state.token);
    else sessionStorage.removeItem("mac.dashboard.token");
    loadDashboard();
  });
  nodes.clearToken.addEventListener("click", () => {
    state.token = "";
    nodes.tokenInput.value = "";
    sessionStorage.removeItem("mac.dashboard.token");
    loadDashboard();
  });
}

async function loadDashboard() {
  state.loading = true;
  state.error = null;
  renderSyncState();
  try {
    state.data = await requestJSON("/dashboard/state");
    state.loadedAt = new Date();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  } finally {
    state.loading = false;
    render();
  }
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
  const body = renderActiveView();
  nodes.content.innerHTML = `${action}${body}`;
  bindViewControls(render);
  syncObservabilitySubscription(render, renderSyncState);
}

function renderActiveView() {
  if (state.activeView === "agents") return renderAgents();
  if (state.activeView === "tasks") return renderTasks();
  if (state.activeView === "hermes") return renderHermes();
  if (state.activeView === "runtime") return renderRuntime();
  if (state.activeView === "observability") return renderObservability();
  if (state.activeView === "secrets") return renderSecrets();
  return renderOverview();
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
