// SSE client: subscribes to /api/events and merges `delta` events into the
// store (shape: docs/api-shapes.md). Degrades gracefully when the endpoint
// is unreachable (dev stub gone, server stopped): it stops retrying after a
// few consecutive failures and the footer shows "SSE idle".
import { apiPath, applyDelta, selectedRun, sseStatus } from "./store.js";

let source = null;
let sourceRun = null; // selectedRun when `source` was opened
let failures = 0;
const MAX_FAILURES = 3;

export function connect() {
  if (source || typeof EventSource === "undefined") return;
  try {
    // Scoped like every fetch: the selected run's deltas, or the latest's.
    sourceRun = selectedRun.value;
    source = new EventSource(apiPath("/api/events"));
  } catch {
    sseStatus.value = "idle";
    return;
  }
  source.addEventListener("open", () => {
    failures = 0;
    sseStatus.value = "connected";
  });
  source.addEventListener("delta", (e) => {
    try {
      applyDelta(JSON.parse(e.data));
    } catch {
      // malformed delta: ignore, snapshot refetch covers gaps
    }
  });
  source.addEventListener("error", () => {
    sseStatus.value = "idle";
    failures += 1;
    if (failures >= MAX_FAILURES && source) {
      source.close();
      source = null;
    }
  });
}

export function disconnect() {
  if (source) {
    source.close();
    source = null;
  }
  failures = 0;
  sseStatus.value = "idle";
}

// The selection can change under an open stream: when the log is rewritten
// without the pinned run, loadRun() falls back to following the latest. A
// stream left on the vanished run's ?run= would only ever carry resets, so
// reopen it for the run now on screen. switchRun() closes the stream before
// it changes the selection, so this stays out of its way.
selectedRun.subscribe((run) => {
  if (source && run !== sourceRun) {
    disconnect();
    connect();
  }
});
