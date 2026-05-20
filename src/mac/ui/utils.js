// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
export function metric(label, value, note) {
  return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
}

export function stateBars(states, counts, total, emptyLabel = "No tasks") {
  if (!total) return `<div class="empty-state">${escapeHtml(emptyLabel)}</div>`;
  return `<div class="state-bar">${states.map((name) => {
    const count = counts[name] || 0;
    const width = Math.max(2, Math.round((count / total) * 100));
    return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
  }).join("")}</div>`;
}

export function observationMetric(item) {
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

export function observationRecord(item) {
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

export function commandAuditRecord(item) {
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

export function uniqueObservations(items) {
  const seen = new Set();
  const unique = [];
  for (const item of items.sort((a, b) => Number(b.sequence || 0) - Number(a.sequence || 0))) {
    if (seen.has(item.sequence)) continue;
    seen.add(item.sequence);
    unique.push(item);
  }
  return unique;
}

export function attentionList(data) {
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

export function field(label, value) {
  return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span></div>`;
}

export function chip(value, tone = "info") {
  return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}

export function timelineItem(eventType, actor, createdAt) {
  return `<div class="timeline-item"><span class="mono small">${escapeHtml(labelize(eventType))}</span><br><span class="muted small">${escapeHtml(actor)} ${escapeHtml(formatAge(createdAt))}</span></div>`;
}

export function agentSelect(name, agents, selected) {
  return `<select name="${escapeHtml(name)}"><option value="">Select agent</option>${agents.map((item) => option(item.agent.id, item.agent.name, selected)).join("")}</select>`;
}

export function select(name, values, selected) {
  return `<select name="${escapeHtml(name)}">${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}

export function option(value, label, selected) {
  return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

export function taskOrigin(task) {
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const origin = metadata.origin;
  return origin && typeof origin === "object" ? origin : {};
}

export function parseJsonObject(value) {
  const text = String(value || "").trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("expected a JSON object");
  return parsed;
}

export function requiredString(value) {
  const text = String(value || "").trim();
  if (!text) throw new Error("required field is blank");
  return text;
}

export function requiredDataset(form, key) {
  const value = form.dataset[key];
  if (!value) throw new Error(`missing action context: ${key}`);
  return value;
}

export function numberValue(value, fallback) {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) throw new Error(`expected number: ${text}`);
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

export function redactedJson(value) {
  return JSON.stringify(value, (key, item) => key === "value" ? "***REDACTED***" : item);
}

export function jsonSummary(value) {
  if (value == null || typeof value !== "object") return value == null ? "none" : String(value);
  const keys = Object.keys(value);
  if (!keys.length) return "none";
  return keys.slice(0, 4).map((key) => `${key}:${compactValue(value[key])}`).join(", ");
}

export function compactValue(value) {
  if (Array.isArray(value)) return `[${value.slice(0, 3).join("|")}${value.length > 3 ? "|..." : ""}]`;
  if (value && typeof value === "object") return "{...}";
  return String(value);
}

export function shortHash(value) {
  const text = String(value || "");
  return text.length > 16 ? `${text.slice(0, 12)}...` : text || "no digest";
}

export function statusTone(status) {
  if (status === "idle") return "good";
  if (status === "busy") return "info";
  if (status === "draining") return "warn";
  return "bad";
}

export function healthTone(status) {
  if (status === "healthy") return "good";
  if (status === "degraded") return "warn";
  return "bad";
}

export function observationTone(level) {
  if (level === "critical" || level === "error") return "bad";
  if (level === "warning") return "warn";
  if (level === "debug") return "info";
  return "good";
}

export function formatMetricValue(item) {
  if (item.value == null) return "none";
  const value = Math.abs(item.value) >= 100 ? Math.round(item.value) : Math.round(item.value * 100) / 100;
  return `${value}${item.unit ? ` ${item.unit}` : ""}`;
}

export function rolloutTone(status) {
  if (status === "promoted") return "good";
  if (["planned", "canarying", "paused"].includes(status)) return "info";
  if (["rescuing", "rolled_back"].includes(status)) return "warn";
  return "bad";
}

export function formatAge(value) {
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

export function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(value);
}

export function labelize(value) {
  return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
}

export function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
    const replacements = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return replacements[char];
  });
}

function requiredElement(selector) {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element;
}
