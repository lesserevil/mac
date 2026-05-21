// NDJSON observability stream lifecycle. Subscribes when the observability
// view is active and tears the subscription down when it isn't. Render
// re-entry is delegated via a registered callback to avoid a circular import
// between this module and render.ts.
import { state } from "./state.js";
import { uniqueObservations } from "./views/observability.js";
import type { ObservabilityEvent } from "./types.js";

type RenderFn = () => void;

let renderCallback: RenderFn = () => {};
let renderSyncStateCallback: RenderFn = () => {};

export function setObservabilityRenderCallback(fn: RenderFn): void {
  renderCallback = fn;
}

export function setObservabilityRenderSyncStateCallback(fn: RenderFn): void {
  renderSyncStateCallback = fn;
}

export function syncObservabilitySubscription(): void {
  if (state.activeView === "observability" && state.data) {
    startObservabilityStream();
  } else {
    stopObservabilityStream();
  }
}

export function startObservabilityStream(): void {
  if (state.observabilityStream) return;
  const controller = new AbortController();
  state.observabilityStream = controller;
  state.observabilityStreamStatus = "connecting";
  const latest = uniqueObservations([
    ...state.observabilityLive,
    ...((state.data?.observability.latest || []) as ObservabilityEvent[]),
  ]);
  const after = latest.length ? latest[0].sequence : 0;
  const headers: Record<string, string> = { Accept: "application/x-ndjson" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  fetch(
    `/observability/stream?after_sequence=${encodeURIComponent(after)}&timeout_seconds=60&poll_interval_seconds=0.5`,
    { headers, signal: controller.signal },
  )
    .then(async (response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      state.observabilityStreamStatus = "connected";
      renderSyncStateCallback();
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
            JSON.parse(text) as ObservabilityEvent,
            ...state.observabilityLive,
          ]).slice(0, 120);
        }
        if (state.activeView === "observability") renderCallback();
      }
    })
    .catch((error) => {
      if (!controller.signal.aborted) {
        state.observabilityStreamStatus = "error";
        state.actionMessage = `Observability stream failed: ${error instanceof Error ? error.message : String(error)}`;
        if (state.activeView === "observability") renderCallback();
      }
    })
    .finally(() => {
      if (state.observabilityStream === controller) state.observabilityStream = null;
      if (!controller.signal.aborted && state.activeView === "observability") {
        state.observabilityStreamStatus = "reconnecting";
        window.setTimeout(startObservabilityStream, 1000);
      }
    });
}

export function stopObservabilityStream(): void {
  if (!state.observabilityStream) return;
  state.observabilityStream.abort();
  state.observabilityStream = null;
  state.observabilityStreamStatus = "idle";
}
