// Constants shared across dashboard modules. Imported by state, views, and
// actions so the canonical task-state vocabulary lives in one place.
import type { ViewKey } from "./types.js";

export const TOKEN_KEY = "mac.dashboard.token";

export const TASK_STATES: string[] = [
  "open",
  "blocked",
  "claimed",
  "running",
  "needs_review",
  "reviewing",
  "completed",
  "failed",
  "cancelled",
];

export const TERMINAL_TASK_STATES: Set<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

export const VIEW_TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  hermes: "Hermes",
  runtime: "Runtime",
  observability: "Observability",
  secrets: "Secrets",
};
