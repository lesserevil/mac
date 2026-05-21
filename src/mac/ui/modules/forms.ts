// Reusable form helpers and small UI primitives (chip/field/timeline/select).
// Form-value coercion lives here too so action handlers can stay declarative.
import { escapeHtml, formatAge, labelize } from "./format.js";
import type { AgentItem, JsonObject, Tone, TaskRecord } from "./types.js";

// ---- markup primitives ----------------------------------------------------

export function metric(label: string, value: unknown, note: string): string {
  return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
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

export function option(value: string, label: string, selected: string): string {
  return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

export function select(name: string, values: string[], selected: string): string {
  return `<select name="${escapeHtml(name)}">${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}

export function agentSelect(name: string, agents: AgentItem[], selected: string): string {
  return `<select name="${escapeHtml(name)}"><option value="">Select agent</option>${agents
    .map((item) => option(item.agent.id, item.agent.name, selected))
    .join("")}</select>`;
}

export function stateBars(
  states: string[],
  counts: Record<string, number>,
  total: number,
  emptyLabel = "No tasks",
): string {
  if (!total) return `<div class="empty-state">${escapeHtml(emptyLabel)}</div>`;
  return `<div class="state-bar">${states
    .map((name) => {
      const count = counts[name] || 0;
      const width = Math.max(2, Math.round((count / total) * 100));
      return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
    })
    .join("")}</div>`;
}

// ---- form-value coercion --------------------------------------------------

export function formValues(form: HTMLFormElement): JsonObject {
  const values: JsonObject = {};
  new FormData(form).forEach((value, key) => {
    values[key] = String(value);
  });
  return values;
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

export function optionalNumber(value: unknown): number | null {
  const text = String(value || "").trim();
  return text ? numberValue(text, 0) : null;
}

export function emptyToNull(value: unknown): string | null {
  const text = String(value || "").trim();
  return text || null;
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

// Convenience helper: pulls the origin block out of a task's metadata.
export function taskOrigin(task: TaskRecord): JsonObject {
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const origin = (metadata as JsonObject).origin;
  return origin && typeof origin === "object" ? (origin as JsonObject) : {};
}
