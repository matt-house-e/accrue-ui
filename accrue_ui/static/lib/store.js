// Signals-based state store + fetch helpers. All requests are same-origin
// relative /api/* paths (see docs/api-shapes.md).
import { signal, batch } from "@preact/signals";

export const STATE = {
  PENDING: 0,
  RUNNING: 1,
  OK: 2,
  CACHED: 3,
  RETRYING: 4,
  ERROR: 5,
  SKIPPED: 6,
};

export const STATE_NAMES = [
  "pending",
  "running",
  "ok",
  "cached",
  "retrying",
  "error",
  "skipped",
];

// ---- core signals -------------------------------------------------------

export const snapshot = signal(null); // /api/run payload (minus cells.data)
export const cellStates = signal(null); // Uint8Array, row-major [row*steps + step]
export const cellsVersion = signal(0); // bumped on any in-place cellStates change
export const selection = signal(null); // {step, row} | null
export const viewMode = signal("status"); // 'status' | 'data'
export const activeTab = signal("grid"); // 'grid' | 'overview' | 'errors' | 'cost'
export const rowFilter = signal("all"); // 'all' | 'errors' | 'retrying' | 'skipped'
export const searchQuery = signal("");
export const showInternalFields = signal(false);
export const fieldChoice = signal({}); // stepName -> selected field name
export const sseStatus = signal("idle"); // 'idle' | 'connected'
export const cellDetail = signal(null); // /api/cell payload for selection
export const cellDetailLoading = signal(false);
export const elapsedS = signal(0); // ticks locally while run.live
export const retryPending = signal(false); // POST in flight, before the poll
export const retryError = signal(null); // {source, message} | null, shown inline
export const errorGroupsStale = signal(false); // a refetch is on its way

// Row-values cache: rowIndex -> {row, key, cells}. Insertion-ordered Map
// gives us LRU-ish eviction at the cap.
export const valuesCache = new Map();
export const valuesVersion = signal(0);
const VALUES_CAP = 2000;

// ---- derived helpers ----------------------------------------------------

export function stepCount() {
  const snap = snapshot.value;
  return snap ? snap.steps.length : 0;
}

export function rowCount() {
  const arr = cellStates.value;
  const n = stepCount();
  return arr && n ? Math.floor(arr.length / n) : 0;
}

export function stateAt(row, stepIndex) {
  const arr = cellStates.value;
  if (!arr) return STATE.PENDING;
  return arr[row * stepCount() + stepIndex];
}

// Visible (non-internal unless toggled) field list for a step.
export function stepFields(step) {
  const fields = step.fields || [];
  return showInternalFields.value ? fields : fields.filter((f) => !f.startsWith("__"));
}

// Is a retry in flight? True from the moment we POST until the server stops
// reporting retry.running, so every retry button disables together.
export function retryBusy() {
  if (retryPending.value) return true;
  const snap = snapshot.value;
  return !!(snap && snap.retry && snap.retry.running);
}

// Error-group lookup for a (step, row): returns the group or null.
export function errorGroupFor(stepName, row) {
  const snap = snapshot.value;
  if (!snap) return null;
  for (const g of snap.error_groups || []) {
    if (g.step !== stepName) continue;
    for (const [lo, hi] of g.rows || []) {
      if (row >= lo && row <= hi) return g;
    }
  }
  return null;
}

// ---- fetch helpers ------------------------------------------------------

function decodeCells(cells) {
  const bin = atob(cells.data);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

export async function loadRun() {
  const res = await fetch("/api/run");
  if (!res.ok) throw new Error(`GET /api/run -> ${res.status}`);
  const data = await res.json();
  const cells = data.cells;
  batch(() => {
    snapshot.value = data;
    cellStates.value = decodeCells(cells);
    cellsVersion.value++;
    elapsedS.value = data.run.elapsed_s || 0;
  });
}

const pendingWindows = new Set();

export async function loadValues(start, count) {
  const key = `${start}:${count}`;
  if (pendingWindows.has(key)) return;
  pendingWindows.add(key);
  try {
    const res = await fetch(`/api/values?start=${start}&count=${count}`);
    if (!res.ok) return;
    const data = await res.json();
    for (const row of data.rows || []) {
      valuesCache.delete(row.row); // refresh position for LRU-ish eviction
      valuesCache.set(row.row, row);
    }
    while (valuesCache.size > VALUES_CAP) {
      valuesCache.delete(valuesCache.keys().next().value);
    }
    valuesVersion.value++;
  } catch {
    // dev stub / offline: values stay unloaded, grid shows indices only
  } finally {
    pendingWindows.delete(key);
  }
}

export async function loadCell(stepName, row) {
  cellDetailLoading.value = true;
  cellDetail.value = null;
  try {
    const res = await fetch(`/api/cell/${encodeURIComponent(stepName)}/${row}`);
    cellDetail.value = res.ok ? await res.json() : null;
  } catch {
    cellDetail.value = null;
  } finally {
    cellDetailLoading.value = false;
  }
}

export async function loadRuns() {
  try {
    const res = await fetch("/api/runs");
    if (!res.ok) return [];
    const data = await res.json();
    return data.runs || [];
  } catch {
    return [];
  }
}

// ---- retry --------------------------------------------------------------
//
// POST /api/retry is same-origin, so the HttpOnly launch-token cookie rides
// along automatically — no token header to attach. The server answers 202
// and runs the retry in the background; we poll /api/run until it reports
// retry.running false, which is also what refreshes error_groups as rows
// heal (deltas carry cell states, not groups).

const RETRY_POLL_MS = 1000;
let retryPoll = null;

function pollUntilRetryDone() {
  if (retryPoll) return;
  retryPoll = setInterval(async () => {
    try {
      await loadRun();
    } catch {
      // transient: keep polling, the next tick may succeed
    }
    const snap = snapshot.value;
    if (snap && snap.retry && snap.retry.running) return;
    clearInterval(retryPoll);
    retryPoll = null;
    retryPending.value = false;
    if (snap && snap.retry && snap.retry.last_error) {
      retryError.value = { source: "*", message: snap.retry.last_error };
    }
    const sel = selection.value; // the open cell may have healed
    if (sel) loadCell(sel.step, sel.row);
  }, RETRY_POLL_MS);
}

// body: {rows:[...]} | {group:{step,type}} | {all:true}. `source` tags which
// button asked, so the error renders next to that one.
export async function postRetry(body, source) {
  if (retryBusy()) return false;
  retryPending.value = true;
  retryError.value = null;
  let res;
  try {
    res = await fetch("/api/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    retryPending.value = false;
    retryError.value = { source, message: String(e) };
    return false;
  }
  if (!res.ok) {
    let message = `retry failed (${res.status})`;
    try {
      const payload = await res.json();
      message = payload.reason || payload.detail || message;
    } catch {
      // non-JSON error body: keep the status-code message
    }
    retryPending.value = false;
    retryError.value = { source, message };
    return false;
  }
  await loadRun().catch(() => {});
  pollUntilRetryDone();
  return true;
}

// ---- actions ------------------------------------------------------------

export function select(stepName, row) {
  selection.value = { step: stepName, row };
  loadCell(stepName, row);
}

export function closeInspector() {
  selection.value = null;
  cellDetail.value = null;
}

export function cycleField(step) {
  const fields = stepFields(step);
  if (fields.length < 2) return;
  const current = fieldChoice.value[step.name] || fields[0];
  const next = fields[(fields.indexOf(current) + 1) % fields.length];
  fieldChoice.value = { ...fieldChoice.value, [step.name]: next };
}

// Deltas carry cell states and counters — never error_groups. During a live
// run the Errors pill (stats.errors) would climb while the triage list still
// showed the groups from page load. When a delta moves stats.errors, mark
// the groups stale and pull a fresh snapshot; debounced, so a burst of
// failures costs one refetch rather than one per flush.
const ERROR_GROUP_REFRESH_MS = 750;
let errorGroupRefresh = null;

export function refreshErrorGroups() {
  errorGroupsStale.value = true;
  if (errorGroupRefresh) return;
  errorGroupRefresh = setTimeout(() => {
    errorGroupRefresh = null;
    loadRun()
      .catch(() => {})
      .finally(() => {
        errorGroupsStale.value = false;
      });
  }, ERROR_GROUP_REFRESH_MS);
}

// Merge one SSE delta (see docs/api-shapes.md, GET /api/events).
let gapRefetching = false;

export function applyDelta(delta) {
  // A reset delta means the server rebuilt its index (truncate-and-regrow):
  // cells that existed only in the old file are in-grid, so gap detection
  // cannot catch them — the whole snapshot is stale. Refetch it wholesale
  // and ignore the rest of this payload; the fresh snapshot is authoritative
  // and later deltas continue from live state (idempotent).
  if (delta.reset) {
    if (!gapRefetching) {
      gapRefetching = true;
      loadRun()
        .catch(() => {})
        .finally(() => {
          gapRefetching = false;
        });
    }
    return;
  }
  let gap = false;
  let errorsMoved = false;
  batch(() => {
    const snap = snapshot.value;
    const arr = cellStates.value;
    const nsteps = snap ? snap.steps.length : 0;
    if (arr && nsteps && Array.isArray(delta.cells) && delta.cells.length) {
      for (const triple of delta.cells) {
        const [row, stepIndex, state] = triple;
        const i = row * nsteps + stepIndex;
        const inGrid = row >= 0 && stepIndex >= 0 && stepIndex < nsteps && i < arr.length;
        if (inGrid && state >= 0 && state <= 6) arr[i] = state;
        else if (!inGrid) gap = true;
      }
      cellsVersion.value++;
    }
    if (snap && delta.stats && Object.keys(delta.stats).length) {
      // rows_done and live are the delta-only stats keys: they belong to
      // rows.done and run.live in the snapshot, not stats (docs/api-shapes.md,
      // GET /api/events). Pull them out before merging the rest into stats.
      const { rows_done: rowsDone, live, ...stats } = delta.stats;
      errorsMoved =
        typeof stats.errors === "number" && stats.errors !== snap.stats.errors;
      snapshot.value = { ...snapshot.value, stats: { ...snap.stats, ...stats } };
      if (typeof rowsDone === "number") {
        snapshot.value = {
          ...snapshot.value,
          rows: { ...snapshot.value.rows, done: rowsDone },
        };
      }
      if (typeof live === "boolean") {
        // Clears the LIVE badge and stops the elapsed ticker (app.js reads
        // run.live) the moment a finished run's log goes cold.
        snapshot.value = {
          ...snapshot.value,
          run: { ...snapshot.value.run, live },
        };
      }
    }
    if (snap && Array.isArray(delta.steps) && delta.steps.length) {
      const byName = new Map(delta.steps.map((s) => [s.name, s]));
      snapshot.value = {
        ...snapshot.value,
        steps: snapshot.value.steps.map((s) =>
          byName.has(s.name) ? { ...s, ...byName.get(s.name) } : s
        ),
      };
    }
    if (snap && typeof delta.t === "number" && delta.t > elapsedS.value) {
      elapsedS.value = Math.floor(delta.t);
    }
  });
  if (errorsMoved) refreshErrorGroups();
  if (gap && !gapRefetching) {
    // The delta references cells outside our known grid: the snapshot is
    // stale (connect race, or the log was rebuilt) — refetch it. Deltas are
    // idempotent state, so re-applying after the refetch is safe.
    gapRefetching = true;
    loadRun()
      .catch(() => {})
      .finally(() => {
        gapRefetching = false;
      });
  }
}
