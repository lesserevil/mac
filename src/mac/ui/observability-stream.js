// Browser output for app.ts. Keep this file checked in so mac does not need a
// Node.js/npm frontend toolchain to serve the dashboard.
import { state } from "./state.js";
import { uniqueObservations } from "./utils.js";

export function syncObservabilitySubscription(render, renderSyncState) {
  if (state.activeView === "observability" && state.data) {
    startObservabilityStream(render, renderSyncState);
  } else {
    stopObservabilityStream();
  }
}

function startObservabilityStream(render, renderSyncState) {
  if (state.observabilityStream) return;
  const controller = new AbortController();
  state.observabilityStream = controller;
  state.observabilityStreamStatus = "connecting";
  const latest = uniqueObservations([
    ...state.observabilityLive,
    ...(state.data?.observability.latest || []),
  ]);
  const after = latest.length ? latest[0].sequence : 0;
  const headers = { Accept: "application/x-ndjson" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  fetch(`/observability/stream?after_sequence=${encodeURIComponent(after)}&timeout_seconds=60&poll_interval_seconds=0.5`, {
    headers,
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      state.observabilityStreamStatus = "connected";
      renderSyncState();
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          const text = line.trim();
          if (!text) continue;
          state.observabilityLive = uniqueObservations([
            JSON.parse(text),
            ...state.observabilityLive,
          ]).slice(0, 120);
        }
        if (state.activeView === "observability") render();
      }
    })
    .catch((error) => {
      if (!controller.signal.aborted) {
        state.observabilityStreamStatus = "error";
        state.actionMessage = `Observability stream failed: ${error instanceof Error ? error.message : String(error)}`;
        if (state.activeView === "observability") render();
      }
    })
    .finally(() => {
      if (state.observabilityStream === controller) state.observabilityStream = null;
      if (!controller.signal.aborted && state.activeView === "observability") {
        state.observabilityStreamStatus = "reconnecting";
        window.setTimeout(() => startObservabilityStream(render, renderSyncState), 1000);
      }
    });
}

export function stopObservabilityStream() {
  if (!state.observabilityStream) return;
  state.observabilityStream.abort();
  state.observabilityStream = null;
  state.observabilityStreamStatus = "idle";
}
