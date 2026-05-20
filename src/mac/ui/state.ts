// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import { TOKEN_KEY } from "./constants.js";
import { requiredElement } from "./dom.js";
import type { DashboardData, DashboardNodes, DashboardState } from "./types.js";

export const state: DashboardState = {
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

export const nodes: DashboardNodes = {
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

export function mustData(): DashboardData {
  if (!state.data) throw new Error("dashboard data is not loaded");
  return state.data;
}
