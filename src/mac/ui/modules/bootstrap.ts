// Dashboard bootstrap: wires up DOM nodes, top-level events, and the initial
// load. The control flow is intentionally tiny — each subsystem (api/forms/
// views/actions/render) lives in its own module under modules/.
import { TOKEN_KEY } from "./constants.js";
import { resolveDashboardNodes } from "./dom.js";
import { state } from "./state.js";
import { fetchDashboardState } from "./api.js";
import { render, setRenderNodes } from "./render.js";
import { formValues, runAction } from "./actions.js";
import { labelize, redactedJson } from "./format.js";
import type { DashboardNodes, ViewKey } from "./types.js";

export function bootstrap(): void {
  const nodes = resolveDashboardNodes();
  setRenderNodes(nodes);
  nodes.tokenInput.value = state.token;
  bindEvents(nodes);
  loadDashboard();
}

async function loadDashboard(): Promise<void> {
  await fetchDashboardState();
  render();
}

function bindEvents(nodes: DashboardNodes): void {
  nodes.nav.addEventListener("click", (event) => {
    const button = (event.target as Element | null)?.closest<HTMLElement>("[data-view]");
    if (!button) return;
    state.activeView = (button.dataset.view || "overview") as ViewKey;
    state.actionMessage = null;
    render();
  });
  nodes.refresh.addEventListener("click", () => loadDashboard());
  nodes.content.addEventListener("submit", (event) => {
    handleActionSubmit(event as SubmitEvent, loadDashboard);
  });
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

async function handleActionSubmit(event: SubmitEvent, reload: () => Promise<void>): Promise<void> {
  const form = (event.target as Element | null)?.closest<HTMLFormElement>("form[data-action]");
  if (!form) return;
  event.preventDefault();
  const action = form.dataset.action || "";
  const values = formValues(form);
  try {
    const result = await runAction(action, form, values);
    state.actionMessage = `${labelize(action)} ok: ${redactedJson(result)}`;
    await reload();
  } catch (error) {
    state.actionMessage = `${labelize(action)} failed: ${error instanceof Error ? error.message : String(error)}`;
    render();
  }
}
