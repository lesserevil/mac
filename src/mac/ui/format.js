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

