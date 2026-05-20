// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
export const TOKEN_KEY = "mac.dashboard.token";
export const TASK_STATES = ["open", "blocked", "claimed", "running", "needs_review", "reviewing", "completed", "failed", "cancelled"];
export const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);
export const VIEW_TITLES = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  hermes: "Hermes",
  runtime: "Runtime",
  observability: "Observability",
  secrets: "Secrets",
};
