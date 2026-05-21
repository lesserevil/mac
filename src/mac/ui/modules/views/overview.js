// Overview view: top-level dashboard summary, task-state bars, attention list.
import { TASK_STATES } from "../constants.js";
import { mustData } from "../state.js";
import { escapeHtml } from "../format.js";
import { metric, stateBars } from "../forms.js";
export function renderOverview() {
    const data = mustData();
    const counts = data.overview.counts;
    const startup = data.hermes_startup;
    const startupStatus = startup?.operator_health?.status || (startup?.ready ? "healthy" : "degraded");
    return `
    <section class="metric-grid">
      ${metric("Agents", counts.agents || 0, `${counts.healthy_agents || 0} healthy, ${counts.busy_agents || 0} busy`)}
      ${metric("Machines", counts.machines || 0, `${counts.trusted_machines || 0} trusted`)}
      ${metric("Active Tasks", counts.active_tasks || 0, `${counts.dead_letters || 0} dead letters`)}
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
export function attentionList(data) {
    const items = [
        ...data.agents.filter((item) => !item.availability.eligible).map((item) => `${item.agent.name}: ${item.availability.reasons.join(", ")}`),
        ...data.dead_letters.map((task) => `Dead letter: ${task.title}`),
        ...data.rollouts
            .filter((item) => ["rescuing", "failed"].includes(String(item.rollout.status)))
            .map((item) => `Rollout ${item.rollout.version}: ${item.rollout.status}`),
        ...data.dispatch.tasks
            .filter((item) => item.eligible_agent_count === 0)
            .map((item) => `No eligible agent: ${item.task.title}`),
    ];
    return items.length
        ? `<div class="record-list">${items.slice(0, 8).map((item) => `<div class="record compact">${escapeHtml(item)}</div>`).join("")}</div>`
        : `<div class="empty-state">No attention items</div>`;
}
