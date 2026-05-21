// Agents view: searchable, filterable grid of agent cards.
import { mustData, state } from "../state.js";
import { escapeHtml, formatAge, healthTone, jsonSummary, statusTone } from "../format.js";
import { chip, field, option } from "../forms.js";
export function renderAgents() {
    const data = mustData();
    const query = state.agentQuery.trim().toLowerCase();
    const agents = data.agents.filter((item) => {
        const haystack = [
            item.agent.name,
            item.agent.id,
            item.machine?.hostname || "",
            item.agent.status,
            item.agent.health_status,
            ...(item.agent.capabilities || []),
        ]
            .join(" ")
            .toLowerCase();
        const matchesQuery = !query || haystack.includes(query);
        const matchesFilter = state.agentFilter === "all" ||
            (state.agentFilter === "eligible" && item.availability.eligible) ||
            (state.agentFilter === "blocked" && !item.availability.eligible) ||
            item.agent.status === state.agentFilter ||
            item.agent.health_status === state.agentFilter;
        return matchesQuery && matchesFilter;
    });
    return `
    <section class="toolbar">
      <input id="agentSearch" type="search" placeholder="Search agents, hosts, capabilities" value="${escapeHtml(state.agentQuery)}">
      <select id="agentFilter">
        ${option("all", "All agents", state.agentFilter)}
        ${option("eligible", "Eligible", state.agentFilter)}
        ${option("blocked", "Blocked", state.agentFilter)}
        ${option("idle", "Idle", state.agentFilter)}
        ${option("busy", "Busy", state.agentFilter)}
        ${option("draining", "Draining", state.agentFilter)}
        ${option("offline", "Offline", state.agentFilter)}
        ${option("degraded", "Degraded", state.agentFilter)}
        ${option("unhealthy", "Unhealthy", state.agentFilter)}
      </select>
      <button type="button" id="clearAgentFilters">Clear</button>
    </section>
    <section class="agent-list">
      ${agents.length ? agents.map(agentCard).join("") : `<div class="empty-state">No matching agents</div>`}
    </section>
  `;
}
export function agentCard(item) {
    const agent = item.agent;
    const machine = item.machine;
    const reasons = item.availability.eligible
        ? chip("dispatch eligible", "good")
        : item.availability.reasons.map((reason) => chip(reason, "bad")).join("");
    return `
    <article class="agent-card ${item.availability.eligible ? "" : "is-blocked"}">
      <div class="agent-header">
        <div><h2 class="mono">${escapeHtml(agent.name)}</h2><p class="muted small">${escapeHtml(agent.id)}</p></div>
        <div class="chip-row">${chip(agent.status, statusTone(agent.status))}${chip(agent.health_status, healthTone(agent.health_status))}</div>
      </div>
      <div class="row-grid">
        ${field("Machine", machine?.hostname || "missing")}
        ${field("Trusted", machine?.trusted ? "yes" : "no")}
        ${field("Last seen", formatAge(agent.last_seen_at))}
        ${field("Capacity", `${item.active_lease_count} / ${item.capacity}`)}
        ${field("Current task", item.active_tasks[0]?.title || agent.current_task_id || "none")}
        ${field("Capabilities", (agent.capabilities || []).join(", ") || "none")}
        ${field("Resources", jsonSummary(agent.resources))}
        ${field("Machine resources", jsonSummary(machine?.resources))}
      </div>
      <div class="chip-row">${reasons}</div>
    </article>
  `;
}
