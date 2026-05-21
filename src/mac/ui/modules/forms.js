// Reusable form helpers and small UI primitives (chip/field/timeline/select).
// Form-value coercion lives here too so action handlers can stay declarative.
import { escapeHtml, formatAge, labelize } from "./format.js";
// ---- markup primitives ----------------------------------------------------
export function metric(label, value, note) {
    return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
}
export function field(label, value) {
    return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span></div>`;
}
export function chip(value, tone = "info") {
    return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}
export function timelineItem(eventType, actor, createdAt) {
    return `<div class="timeline-item"><span class="mono small">${escapeHtml(labelize(eventType))}</span><br><span class="muted small">${escapeHtml(actor)} ${escapeHtml(formatAge(createdAt))}</span></div>`;
}
export function option(value, label, selected) {
    return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}
export function select(name, values, selected) {
    return `<select name="${escapeHtml(name)}">${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}
export function agentSelect(name, agents, selected) {
    return `<select name="${escapeHtml(name)}"><option value="">Select agent</option>${agents
        .map((item) => option(item.agent.id, item.agent.name, selected))
        .join("")}</select>`;
}
export function stateBars(states, counts, total, emptyLabel = "No tasks") {
    if (!total)
        return `<div class="empty-state">${escapeHtml(emptyLabel)}</div>`;
    return `<div class="state-bar">${states
        .map((name) => {
        const count = counts[name] || 0;
        const width = Math.max(2, Math.round((count / total) * 100));
        return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
    })
        .join("")}</div>`;
}
// ---- form-value coercion --------------------------------------------------
export function formValues(form) {
    const values = {};
    new FormData(form).forEach((value, key) => {
        values[key] = String(value);
    });
    return values;
}
export function requiredString(value) {
    const text = String(value || "").trim();
    if (!text)
        throw new Error("required field is blank");
    return text;
}
export function requiredDataset(form, key) {
    const value = form.dataset[key];
    if (!value)
        throw new Error(`missing action context: ${key}`);
    return value;
}
export function numberValue(value, fallback) {
    const text = String(value || "").trim();
    if (!text)
        return fallback;
    const parsed = Number(text);
    if (!Number.isFinite(parsed))
        throw new Error(`expected number: ${text}`);
    return parsed;
}
export function optionalNumber(value) {
    const text = String(value || "").trim();
    return text ? numberValue(text, 0) : null;
}
export function emptyToNull(value) {
    const text = String(value || "").trim();
    return text || null;
}
export function parseJsonObject(value) {
    const text = String(value || "").trim();
    if (!text)
        return {};
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("expected a JSON object");
    }
    return parsed;
}
// Convenience helper: pulls the origin block out of a task's metadata.
export function taskOrigin(task) {
    const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
    const origin = metadata.origin;
    return origin && typeof origin === "object" ? origin : {};
}
