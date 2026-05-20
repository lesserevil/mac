// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import { requestJSON } from "./api-client.js";
import { handleActionSubmit } from "./actions.js";
import { VIEW_TITLES } from "./constants.js";
import { syncObservabilitySubscription } from "./observability-stream.js";
import { nodes, state } from "./state.js";
import type { DashboardData, ViewKey } from "./types.js";
import { escapeHtml, formatTime } from "./utils.js";
import { bindViewControls } from "./view-controls.js";
import { renderAgents, renderHermes, renderObservability, renderOverview, renderRuntime, renderSecrets, renderTasks } from "./views.js";

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
  nodes.content.addEventListener("submit", (event) => {
    void handleActionSubmit(event as SubmitEvent, { loadDashboard, render });
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
  const body = renderActiveView();
  nodes.content.innerHTML = `${action}${body}`;
  bindViewControls(render);
  syncObservabilitySubscription(render, renderSyncState);
}

function renderActiveView(): string {
  if (state.activeView === "agents") return renderAgents();
  if (state.activeView === "tasks") return renderTasks();
  if (state.activeView === "hermes") return renderHermes();
  if (state.activeView === "runtime") return renderRuntime();
  if (state.activeView === "observability") return renderObservability();
  if (state.activeView === "secrets") return renderSecrets();
  return renderOverview();
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
