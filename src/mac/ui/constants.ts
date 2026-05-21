import type { ViewKey } from "./models.js";

export const TOKEN_KEY = "mac.dashboard.token";
export const TASK_STATES = [
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
export const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);
export const VIEW_TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  hermes: "Hermes",
  runtime: "Runtime",
  observability: "Observability",
  secrets: "Secrets",
};
