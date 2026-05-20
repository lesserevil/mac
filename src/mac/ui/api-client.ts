// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
import { state } from "./state.js";
import type { JsonObject } from "./types.js";

export async function requestJSON(path: string, init: RequestInit = {}): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail || detail;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return response.json();
}


export function postJSON(path: string, body: JsonObject): Promise<unknown> {
  return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
}
