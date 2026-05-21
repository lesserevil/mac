// Tasks view: kanban-style lanes per task state with per-task action forms.
import { TASK_STATES } from "../constants.js";
import { mustData, state } from "../state.js";
import { escapeHtml } from "../format.js";
import {
  agentSelect,
  chip,
  option,
  select,
  taskOrigin,
  timelineItem,
} from "../forms.js";
import { labelize } from "../format.js";
import type { AgentItem, TaskDetail } from "../types.js";

export function renderTasks(): string {
  const data = mustData();
  const tasks =
    state.taskFilter === "all"
      ? data.tasks
      : data.tasks.filter((detail) => detail.task.state === state.taskFilter);
  return `
    <section class="toolbar">
      <select id="taskFilter">
        ${option("all", "All states", state.taskFilter)}
        ${TASK_STATES.map((taskState) => option(taskState, labelize(taskState), state.taskFilter)).join("")}
      </select>
      <button type="button" id="clearTaskFilter">Clear</button>
    </section>
    <section class="task-lanes">
      ${TASK_STATES.filter((taskState) => state.taskFilter === "all" || state.taskFilter === taskState)
        .map((taskState) => taskLane(taskState, tasks, data.agents))
        .join("")}
    </section>
  `;
}

export function taskLane(taskState: string, tasks: TaskDetail[], agents: AgentItem[]): string {
  const laneTasks = tasks.filter((detail) => detail.task.state === taskState);
  return `
    <div class="task-lane">
      <h2><span>${escapeHtml(labelize(taskState))}</span><span class="pill">${laneTasks.length}</span></h2>
      ${laneTasks.length ? laneTasks.map((detail) => taskCard(detail, agents)).join("") : `<div class="empty-state">Empty</div>`}
    </div>
  `;
}

export function taskCard(detail: TaskDetail, agents: AgentItem[]): string {
  const task = detail.task;
  const owner = agents.find((item) => item.agent.id === task.owner_agent_id)?.agent;
  const origin = taskOrigin(task);
  const evidenceOptions = detail.evidence.map((item) => option(String(item.id), String(item.id), "")).join("");
  const pendingReviews = detail.reviews.filter((review) => review.status === "pending");
  return `
    <article class="task-card">
      <h3>${escapeHtml(task.title)}</h3>
      <p class="muted small mono">${escapeHtml(task.id)}</p>
      <div class="chip-row">
        ${chip(`P${task.priority || 0}`, "info")}
        ${chip(`${task.attempt_count || 0}/${task.max_attempts || 0} attempts`, (task.attempt_count || 0) >= (task.max_attempts || 1) ? "bad" : "good")}
        ${owner ? chip(owner.name, "info") : chip("unowned", "warn")}
        ${origin.hermes_instance_id ? chip("Hermes origin", "info") : ""}
      </div>
      <p class="small muted">${escapeHtml(String(detail.summary?.summary || ""))}</p>
      <div class="timeline">
        ${detail.history.slice(-3).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("")}
      </div>
      <details class="action-box">
        <summary>Task actions</summary>
        <form class="action-form compact" data-action="taskClaim" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", agents, task.owner_agent_id || "")}</label>
          <label>Lease seconds <input name="lease_seconds" type="number" value="900" min="1"></label>
          <button type="submit">Claim</button>
        </form>
        <form class="action-form compact" data-action="taskStart" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", agents, task.owner_agent_id || "")}</label>
          <button type="submit">Start</button>
        </form>
        <form class="action-form compact" data-action="taskSubmitReview" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", agents, task.owner_agent_id || "")}</label>
          <button type="submit">Submit Review</button>
        </form>
        <form class="action-form" data-action="taskTransition" data-task-id="${escapeHtml(task.id)}">
          <label>State ${select("target_state", TASK_STATES, task.state)}</label>
          <label>Actor <input name="actor" value="human"></label>
          <label>Detail JSON <textarea name="detail" placeholder="{}"></textarea></label>
          <button type="submit">Transition</button>
        </form>
        <form class="action-form" data-action="addEvidence" data-task-id="${escapeHtml(task.id)}">
          <label>Kind ${select("kind", ["test", "review", "artifact", "publication", "log", "eval"], "test")}</label>
          <label>URI <input name="uri" placeholder="artifact://..."></label>
          <label>Summary <input name="summary" placeholder="What this proves"></label>
          <label>Checksum <input name="checksum" placeholder="optional"></label>
          <label>Created by <input name="created_by" value="${escapeHtml(task.owner_agent_id || "human")}"></label>
          <button type="submit">Add Evidence</button>
        </form>
        <form class="action-form compact" data-action="requestReview" data-task-id="${escapeHtml(task.id)}">
          <label>Reviewer ${agentSelect("reviewer_agent_id", agents, "")}</label>
          <label>Actor <input name="actor" value="dispatcher"></label>
          <button type="submit">Request Review</button>
        </form>
        ${pendingReviews
          .map(
            (review) => `
          <form class="action-form" data-action="reviewDecision" data-review-id="${escapeHtml(review.id)}">
            <label>Status ${select("status", ["approved", "changes_requested", "rejected"], "approved")}</label>
            <label>Reviewer <input name="reviewer_agent_id" value="${escapeHtml(review.reviewer_agent_id)}"></label>
            <label>Evidence <select name="evidence_id"><option value="">None</option>${evidenceOptions}</select></label>
            <label>Reason <input name="reason" placeholder="optional"></label>
            <button type="submit">Submit Review</button>
          </form>`,
          )
          .join("")}
        <form class="action-form compact" data-action="publishTask">
          <input type="hidden" name="task_id" value="${escapeHtml(task.id)}">
          <label>Target <input name="target" placeholder="release://..."></label>
          <label>Created by <input name="created_by" value="human"></label>
          <label>Evidence <select name="evidence_id"><option value="">None</option>${evidenceOptions}</select></label>
          <button type="submit">Publish</button>
        </form>
      </details>
    </article>
  `;
}
