// Observability view: metric snapshot, distribution bars, notifications,
// command audit, and the rolling live-stream feed.
import { mustData, state } from "../state.js";
import { escapeHtml, formatAge, formatMetricValue, jsonSummary, observationTone } from "../format.js";
import { chip, metric, stateBars } from "../forms.js";
import type {
  CommandAuditRecord,
  ObservabilityEvent,
  OperatorNotification,
} from "../types.js";

export function renderObservability(): string {
  const data = mustData();
  const observability = data.observability || {
    counts: {},
    levels: {},
    layers: {},
    latest: [],
    latest_metrics: [],
  };
  const counts = observability.counts || {};
  const commandAudit = data.command_audit || [];
  const notifications = data.notifications || [];
  const pendingNotifications = notifications.filter((item) => item.status === "pending").length;
  const live = uniqueObservations([...state.observabilityLive, ...(observability.latest || [])]);
  const layerTotal = Object.values(observability.layers || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  const levelTotal = Object.values(observability.levels || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  return `
    <section class="metric-grid">
      ${metric("Observations", counts.events || 0, `${counts.logs || 0} logs, ${counts.metrics || 0} metrics`)}
      ${metric("Warnings", counts.warnings || 0, "warning observations")}
      ${metric("Errors", counts.errors || 0, "error observations")}
      ${metric("Notifications", notifications.length, `${pendingNotifications} pending`)}
      ${metric("Stream", state.observabilityStreamStatus, `${state.observabilityLive.length} live item(s)`)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Metric Snapshot</h2>
        <div class="metric-list">
          ${(observability.latest_metrics || []).length
            ? observability.latest_metrics.map(observationMetric).join("")
            : `<div class="empty-state">No metrics</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Distribution</h2>
        ${stateBars(Object.keys(observability.layers || {}).sort(), observability.layers || {}, layerTotal, "No layers")}
        ${stateBars(Object.keys(observability.levels || {}).sort(), observability.levels || {}, levelTotal, "No levels")}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Notifications</h2>
        ${chip(`${pendingNotifications} pending`, pendingNotifications ? "warn" : "good")}
      </div>
      <div class="observability-feed">
        ${notifications.length ? notifications.slice(0, 80).map(notificationRecord).join("") : `<div class="empty-state">No notifications</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Command Audit</h2>
        ${chip(`${commandAudit.length}`, commandAudit.length ? "info" : "warn")}
      </div>
      <div class="observability-feed">
        ${commandAudit.length ? commandAudit.slice(0, 80).map(commandAuditRecord).join("") : `<div class="empty-state">No command audit records</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Live Stream</h2>
        ${chip(state.observabilityStreamStatus, state.observabilityStreamStatus === "connected" ? "good" : state.observabilityStreamStatus === "error" ? "bad" : "info")}
      </div>
      <div class="observability-feed">
        ${live.length ? live.slice(0, 80).map(observationRecord).join("") : `<div class="empty-state">No observations</div>`}
      </div>
    </section>
  `;
}

export function notificationRecord(item: OperatorNotification): string {
  return `
    <article class="feed-item">
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.body)}</p>
        <p class="muted small">${escapeHtml(item.event_type)} · ${escapeHtml(item.created_at)}</p>
      </div>
      <div class="chip-row">
        ${chip(item.status, item.status === "pending" ? "warn" : item.status === "failed" ? "bad" : "good")}
        ${(item.channels || []).map((channel) => chip(channel, "info")).join("")}
      </div>
    </article>
  `;
}

export function observationMetric(item: ObservabilityEvent): string {
  return `
    <article class="metric-observation">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <p class="muted small">${escapeHtml(item.layer)} / ${escapeHtml(item.source)} · ${escapeHtml(formatAge(item.created_at))}</p>
      </div>
      <div class="metric-observation-value">${escapeHtml(formatMetricValue(item))}</div>
    </article>
  `;
}

export function observationRecord(item: ObservabilityEvent): string {
  const subject = item.subject_type && item.subject_id ? `${item.subject_type}:${item.subject_id}` : "";
  return `
    <article class="observation-row tone-left-${observationTone(item.level)}">
      <div class="observation-main">
        <span class="mono small">#${escapeHtml(item.sequence)}</span>
        ${chip(item.kind, item.kind === "metric" ? "info" : observationTone(item.level))}
        ${chip(item.level, observationTone(item.level))}
        <strong>${escapeHtml(item.name)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.layer)} / ${escapeHtml(item.source)} ${subject ? `· ${escapeHtml(subject)}` : ""} · ${escapeHtml(formatAge(item.created_at))}</div>
      <div class="observation-detail">${escapeHtml(item.kind === "metric" ? formatMetricValue(item) : jsonSummary(item.detail))}</div>
    </article>
  `;
}

export function commandAuditRecord(item: CommandAuditRecord): string {
  const tone = item.phase === "completed" || item.phase === "started" ? "info" : "bad";
  const subject = item.task_id ? `task:${item.task_id}` : `agent:${item.agent_id}`;
  const argv = (item.argv || []).join(" ");
  const result = item.returncode === null || item.returncode === undefined ? "" : ` rc=${item.returncode}`;
  const duration = item.duration_ms === null || item.duration_ms === undefined ? "" : ` ${Math.round(item.duration_ms)}ms`;
  return `
    <article class="observation-row tone-left-${tone}">
      <div class="observation-main">
        ${chip(item.phase, tone)}
        <strong>${escapeHtml(item.command_id)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.agent_id)} · ${escapeHtml(subject)} · ${escapeHtml(formatAge(item.created_at))}${escapeHtml(result)}${escapeHtml(duration)}</div>
      <div class="observation-detail mono">${escapeHtml(argv)}</div>
      <div class="muted small">${escapeHtml(item.cwd)}</div>
    </article>
  `;
}

// De-dupe observability events by sequence, newest-first. Exported because the
// stream module appends/prepends here and the view sorts the union.
export function uniqueObservations(items: ObservabilityEvent[]): ObservabilityEvent[] {
  const seen = new Set<number>();
  const unique: ObservabilityEvent[] = [];
  for (const item of items.sort((a, b) => Number(b.sequence || 0) - Number(a.sequence || 0))) {
    if (seen.has(item.sequence)) continue;
    seen.add(item.sequence);
    unique.push(item);
  }
  return unique;
}
