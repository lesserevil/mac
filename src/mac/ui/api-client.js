// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
import { state } from "./state.js";

export async function requestJSON(path, init = {}) {
  const headers = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return response.json();
}

export function postJSON(path, body) {
  return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
}
