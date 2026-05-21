// Typed API client used by every action and by the dashboard bootstrap. All
// dashboard ↔ control-plane HTTP traffic flows through requestJSON so token
// handling, header negotiation, and error parsing stay in one place.
import { state } from "./state.js";
export async function requestJSON(path, init = {}) {
    const headers = { Accept: "application/json" };
    if (init.body)
        headers["Content-Type"] = "application/json";
    if (state.token)
        headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(path, { ...init, headers });
    if (!response.ok) {
        let detail = response.statusText;
        try {
            const body = (await response.json());
            detail = body.detail || detail;
        }
        catch {
            detail = response.statusText;
        }
        throw new Error(`${response.status} ${detail}`);
    }
    return response.json();
}
export function postJSON(path, body) {
    return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
}
// Pull a fresh /dashboard/state snapshot into the module-scope `state`.
// Render orchestration lives in render.ts; this just refreshes the data.
export async function fetchDashboardState() {
    state.loading = true;
    state.error = null;
    try {
        state.data = (await requestJSON("/dashboard/state"));
        state.loadedAt = new Date();
    }
    catch (error) {
        state.error = error instanceof Error ? error.message : String(error);
    }
    finally {
        state.loading = false;
    }
}
