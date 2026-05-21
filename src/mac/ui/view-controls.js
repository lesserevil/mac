export function bindViewControls(state, render) {
  const search = document.querySelector("#agentSearch");
  if (search) search.addEventListener("input", (event) => {
    state.agentQuery = event.target.value;
    render();
  });
  const agentFilter = document.querySelector("#agentFilter");
  if (agentFilter) agentFilter.addEventListener("change", (event) => {
    state.agentFilter = event.target.value;
    render();
  });
  const clearAgents = document.querySelector("#clearAgentFilters");
  if (clearAgents) clearAgents.addEventListener("click", () => {
    state.agentQuery = "";
    state.agentFilter = "all";
    render();
  });
  const taskFilter = document.querySelector("#taskFilter");
  if (taskFilter) taskFilter.addEventListener("change", (event) => {
    state.taskFilter = event.target.value;
    render();
  });
  const clearTasks = document.querySelector("#clearTaskFilter");
  if (clearTasks) clearTasks.addEventListener("click", () => {
    state.taskFilter = "all";
    render();
  });
}

