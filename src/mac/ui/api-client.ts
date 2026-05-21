import type { JsonObject } from "./models.js";

export interface DashboardApi {
  requestJSON(path: string, init?: RequestInit): Promise<unknown>;
  postJSON(path: string, body: JsonObject): Promise<unknown>;
}

export function createDashboardApi(getToken: () => string): DashboardApi {
  async function requestJSON(path: string, init: RequestInit = {}): Promise<unknown> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (init.body) headers["Content-Type"] = "application/json";
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
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

  function postJSON(path: string, body: JsonObject): Promise<unknown> {
    return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
  }

  return { requestJSON, postJSON };
}
