// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import { state } from "./state.js";

export function bindViewControls(render: () => void): void {
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
