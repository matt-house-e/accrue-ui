// SSE client: subscribes to /api/events and merges `delta` events into the
// store (shape: docs/api-shapes.md). Degrades gracefully when the endpoint
// is unreachable (dev stub gone, server stopped): it stops retrying after a
// few consecutive failures and the footer shows "SSE idle".
import { applyDelta, sseStatus } from "./store.js";

let source = null;
let failures = 0;
const MAX_FAILURES = 3;

export function connect() {
  if (source || typeof EventSource === "undefined") return;
  try {
    source = new EventSource("/api/events");
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
  sseStatus.value = "idle";
}
