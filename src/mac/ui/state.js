// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
import { TOKEN_KEY } from "./constants.js";
import { requiredElement } from "./dom.js";

export const state = {
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

export const nodes = {
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

export function mustData() {
  if (!state.data) throw new Error("dashboard data is not loaded");
  return state.data;
}
