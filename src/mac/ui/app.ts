// Maintained dashboard source. The browser modules are checked in as .js files so
// mac does not require Node.js/npm to serve or install the UI.
import type { DashboardData, DashboardNodes, DashboardState, ViewKey } from "./models.js";
import { TOKEN_KEY, VIEW_TITLES } from "./constants.js";
import { createDashboardApi } from "./api-client.js";
import { createActionHandler } from "./actions.js";
import { requiredElement } from "./dom.js";
import { formatTime } from "./format.js";
import { createObservabilityStream } from "./observability-stream.js";
import { bindViewControls } from "./view-controls.js";
import { renderView } from "./views.js";

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
  observabilityLive: [],
  observabilityStream: null,
  observabilityStreamStatus: "idle",
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

const api = createDashboardApi(() => state.token);
const observabilityStream = createObservabilityStream({
  state,
  getToken: () => state.token,
  render,
  renderSyncState,
});

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
  nodes.content.addEventListener("submit", createActionHandler({ api, state, loadDashboard, render }));
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
    state.data = (await api.requestJSON("/dashboard/state")) as DashboardData;
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
  nodes.content.innerHTML = renderView(state);
  bindViewControls(state, render);
  observabilityStream.sync();
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
