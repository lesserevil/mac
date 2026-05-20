// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import type { ViewKey } from "./types.js";

const TOKEN_KEY = "mac.dashboard.token";
const TASK_STATES = [
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
const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);
const VIEW_TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  hermes: "Hermes",
  runtime: "Runtime",
  observability: "Observability",
  secrets: "Secrets",
};
