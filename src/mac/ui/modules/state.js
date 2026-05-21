// Shared mutable dashboard state. A single module-scope object is intentional:
// the dashboard is a small SPA and threading reactive state through every
// function would cost more than it would buy. Modules read/write `state`
// directly and call requestRender() (see render.ts) when they need a redraw.
import { TOKEN_KEY } from "./constants.js";
export const state = {
    activeView: "overview",
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    loading: false,
    loadedAt: null,
    data: null,
    error: null,
    actionMessage: null,
    agentQuery: "",
    agentFilter: "all",
    taskFilter: "all",
    observabilityLive: [],
    observabilityStream: null,
    observabilityStreamStatus: "idle",
};
// Throws unless dashboard data is loaded. Views use this to guarantee
// non-null `state.data` without scattering `if (!state.data) return` guards.
export function mustData() {
    if (!state.data)
        throw new Error("dashboard data is not loaded");
    return state.data;
}
