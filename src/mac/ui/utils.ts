// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import type { AgentItem, CommandAuditRecord, DashboardData, JsonObject, ObservabilityEvent, Tone } from "./types.js";

export function metric(label: string, value: unknown, note: string): string {
  return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
}

export function stateBars(states: string[], counts: Record<string, number>, total: number, emptyLabel = "No tasks"): string {
  if (!total) return `<div class="empty-state">${escapeHtml(emptyLabel)}</div>`;
  return `<div class="state-bar">${states.map((name) => {
    const count = counts[name] || 0;
    const width = Math.max(2, Math.round((count / total) * 100));
    return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
  }).join("")}</div>`;
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

export function attentionList(data: DashboardData): string {
  const items = [
    ...data.agents.filter((item) => !item.availability.eligible).map((item) => `${item.agent.name}: ${item.availability.reasons.join(", ")}`),
    ...data.dead_letters.map((task) => `Dead letter: ${task.title}`),
    ...data.rollouts.filter((item) => ["rescuing", "failed"].includes(String(item.rollout.status))).map((item) => `Rollout ${item.rollout.version}: ${item.rollout.status}`),
    ...data.dispatch.tasks.filter((item) => item.eligible_agent_count === 0).map((item) => `No eligible agent: ${item.task.title}`),
  ];
  return items.length
    ? `<div class="record-list">${items.slice(0, 8).map((item) => `<div class="record compact">${escapeHtml(item)}</div>`).join("")}</div>`
    : `<div class="empty-state">No attention items</div>`;
}

export function field(label: string, value: unknown): string {
  return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span></div>`;
}

export function chip(value: unknown, tone: Tone = "info"): string {
  return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}

export function timelineItem(eventType: string, actor: string, createdAt: string): string {
  return `<div class="timeline-item"><span class="mono small">${escapeHtml(labelize(eventType))}</span><br><span class="muted small">${escapeHtml(actor)} ${escapeHtml(formatAge(createdAt))}</span></div>`;
}

export function agentSelect(name: string, agents: AgentItem[], selected: string): string {
  return `<select name="${escapeHtml(name)}"><option value="">Select agent</option>${agents.map((item) => option(item.agent.id, item.agent.name, selected)).join("")}</select>`;
}

export function select(name: string, values: string[], selected: string): string {
  return `<select name="${escapeHtml(name)}">${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}

export function option(value: string, label: string, selected: string): string {
  return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

export function taskOrigin(task: TaskRecord): JsonObject {
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const origin = metadata.origin;
  return origin && typeof origin === "object" ? origin as JsonObject : {};
}

export function parseJsonObject(value: unknown): JsonObject {
  const text = String(value || "").trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("expected a JSON object");
  }
  return parsed as JsonObject;
}

export function requiredString(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) throw new Error("required field is blank");
  return text;
}

export function requiredDataset(form: HTMLFormElement, key: string): string {
  const value = form.dataset[key];
  if (!value) throw new Error(`missing action context: ${key}`);
  return value;
}

export function numberValue(value: unknown, fallback: number): number {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) throw new Error(`expected number: ${text}`);
  return parsed;
}

export export function optionalNumber(value: unknown): number | null {
  const text = String(value || "").trim();
  return text ? numberValue(text, 0) : null;
}

export function emptyToNull(value: unknown): string | null {
  const text = String(value || "").trim();
  return text || null;
}

export function redactedJson(value: unknown): string {
  return JSON.stringify(value, (key, item) => key === "value" ? "***REDACTED***" : item);
}

export function jsonSummary(value: unknown): string {
  if (value == null || typeof value !== "object") return value == null ? "none" : String(value);
  const keys = Object.keys(value as JsonObject);
  if (!keys.length) return "none";
  return keys.slice(0, 4).map((key) => `${key}:${compactValue((value as JsonObject)[key])}`).join(", ");
}

export function compactValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.slice(0, 3).join("|")}${value.length > 3 ? "|..." : ""}]`;
  if (value && typeof value === "object") return "{...}";
  return String(value);
}

export function shortHash(value: unknown): string {
  const text = String(value || "");
  return text.length > 16 ? `${text.slice(0, 12)}...` : text || "no digest";
}

export function statusTone(status: string): Tone {
  if (status === "idle") return "good";
  if (status === "busy") return "info";
  if (status === "draining") return "warn";
  return "bad";
}

export function healthTone(status: string): Tone {
  if (status === "healthy") return "good";
  if (status === "degraded") return "warn";
  return "bad";
}

export function observationTone(level: string): Tone {
  if (level === "critical" || level === "error") return "bad";
  if (level === "warning") return "warn";
  if (level === "debug") return "info";
  return "good";
}

export function formatMetricValue(item: ObservabilityEvent): string {
  if (item.value == null) return "none";
  const value = Math.abs(item.value) >= 100 ? Math.round(item.value) : Math.round(item.value * 100) / 100;
  return `${value}${item.unit ? ` ${item.unit}` : ""}`;
}

export function rolloutTone(status: string): Tone {
  if (status === "promoted") return "good";
  if (["planned", "canarying", "paused"].includes(status)) return "info";
  if (["rescuing", "rolled_back"].includes(status)) return "warn";
  return "bad";
}

export function formatAge(value: string | null | undefined): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "unknown";
  const diffMs = Date.now() - date.getTime();
  const suffix = diffMs >= 0 ? "ago" : "from now";
  const minutes = Math.max(1, Math.round(Math.abs(diffMs) / 60000));
  if (minutes < 60) return `${minutes}m ${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ${suffix}`;
  return `${Math.round(hours / 24)}d ${suffix}`;
}

export function formatTime(value: Date): string {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(value);
}

export function labelize(value: unknown): string {
  return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
}

export function escapeHtml(value: unknown): string {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
    const replacements: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return replacements[char];
  });
}

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element as T;
}
