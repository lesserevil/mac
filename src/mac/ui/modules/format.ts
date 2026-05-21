// Pure value formatters used everywhere by views. Kept side-effect-free so
// they can be unit-tested without a DOM.
import { escapeHtml } from "./dom.js";
import type { JsonObject, ObservabilityEvent, Tone } from "./types.js";

export function labelize(value: unknown): string {
  return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
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

export function redactedJson(value: unknown): string {
  return JSON.stringify(value, (key, item) => (key === "value" ? "***REDACTED***" : item));
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

export function rolloutTone(status: string): Tone {
  if (status === "promoted") return "good";
  if (["planned", "canarying", "paused"].includes(status)) return "info";
  if (["rescuing", "rolled_back"].includes(status)) return "warn";
  return "bad";
}

export function formatMetricValue(item: ObservabilityEvent): string {
  if (item.value == null) return "none";
  const value = Math.abs(item.value) >= 100 ? Math.round(item.value) : Math.round(item.value * 100) / 100;
  return `${value}${item.unit ? ` ${item.unit}` : ""}`;
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
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(value);
}

// Re-export escapeHtml so consumers can `import { escapeHtml } from "./format.js"`
// alongside the other text helpers. Keeps the import surface focused.
export { escapeHtml };
