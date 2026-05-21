// Runtime view: runtime environments and rollouts, with rollout action forms.
import { mustData } from "../state.js";
import { escapeHtml, formatAge, jsonSummary, rolloutTone, shortHash } from "../format.js";
import { chip, field, select, timelineItem } from "../forms.js";
import type { ApiRecord, DashboardData, RolloutStatus } from "../types.js";

export function renderRuntime(): string {
  const data = mustData();
  return `
    <section class="split">
      <div class="surface">
        <h2>Runtime Environments</h2>
        <div class="runtime-list">
          ${data.runtimes.length ? data.runtimes.map((runtime) => runtimeRecord(runtime, data)).join("") : `<div class="empty-state">No runtimes</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Rollouts</h2>
        <div class="rollout-list">
          ${data.rollouts.length ? data.rollouts.map((status) => rolloutRecord(status, data)).join("") : `<div class="empty-state">No rollouts</div>`}
        </div>
      </div>
    </section>
  `;
}

export function runtimeRecord(runtime: ApiRecord, data: DashboardData): string {
  const rollouts = data.rollouts.filter((item) => item.rollout.runtime_environment_id === runtime.id);
  const runs = data.runtime_runs.filter((run) => run.environment_id === runtime.id);
  return `
    <article class="runtime-record">
      <div class="runtime-header"><div><h3>${escapeHtml(runtime.name)}</h3><p class="muted small mono">${escapeHtml(runtime.id)}</p></div>${chip(shortHash(runtime.digest), "good")}</div>
      <div class="row-grid">
        ${field("Created by", runtime.created_by)}
        ${field("Created", formatAge(String(runtime.created_at || "")))}
        ${field("Rollouts", rollouts.length)}
        ${field("Runs", runs.length)}
        ${field("Manifest", jsonSummary(runtime.manifest))}
      </div>
    </article>
  `;
}

export function rolloutRecord(status: RolloutStatus, data: DashboardData): string {
  const rollout = status.rollout;
  const evalSet = data.eval_sets.find((item) => item.id === rollout.required_eval_set_id);
  return `
    <article class="rollout-record">
      <div class="rollout-header"><div><h3>${escapeHtml(rollout.version)}</h3><p class="muted small mono">${escapeHtml(rollout.id)}</p></div>${chip(rollout.status, rolloutTone(String(rollout.status)))}</div>
      <div class="row-grid">
        ${field("Strategy", rollout.strategy)}
        ${field("Target", `${rollout.target_percent}%`)}
        ${field("Channel", rollout.channel)}
        ${field("Runtime", status.runtime?.name || "none")}
        ${field("Artifact", rollout.artifact_hash || "unverified")}
        ${field("Eval gate", evalSet?.name || "none")}
        ${field("Latest eval", status.latest_eval_run ? `${status.latest_eval_run.score} ${status.latest_eval_run.passed ? "pass" : "fail"}` : "none")}
        ${field("Health policy", jsonSummary(rollout.health_policy))}
      </div>
      <div class="timeline">${status.events.slice(-4).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("")}</div>
      <details class="action-box">
        <summary>Rollout actions</summary>
        <form class="action-form" data-action="rolloutAdvance" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Action ${select("action", ["start_canary", "promote", "pause", "resume", "rollback"], "start_canary")}</label>
          <label>Actor <input name="actor" value="human"></label>
          <label>Detail JSON <textarea name="detail" placeholder="{}"></textarea></label>
          <button type="submit">Advance</button>
        </form>
        <form class="action-form compact" data-action="rolloutHealth" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Actor <input name="actor" value="monitor"></label>
          <label>Checks JSON <textarea name="checks" placeholder='{"runtime":"healthy"}'></textarea></label>
          <button type="submit">Record Health</button>
        </form>
        <form class="action-form compact danger-action" data-action="rolloutRescue" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Actor <input name="actor" value="human"></label>
          <label>Reason <input name="reason" placeholder="why rescue is needed"></label>
          <button type="submit">Rescue</button>
        </form>
      </details>
    </article>
  `;
}
