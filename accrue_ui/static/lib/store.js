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
// stepName -> the sub-key its data column shows for object-valued fields.
// Per column, not per field: one choice covers every {value, source} field
// the field-chip cycles through (accrue-ui#36).
export const subKeyChoice = signal({});
export const sseStatus = signal("idle"); // 'idle' | 'connected'
export const cellDetail = signal(null); // /api/cell payload for selection
export const cellDetailLoading = signal(false);
export const elapsedS = signal(0); // ticks locally while run.live
export const retryPending = signal(false); // POST in flight, before the poll
export const retryError = signal(null); // {source, message} | null, shown inline
export const errorGroupsStale = signal(false); // a refetch is on its way
// The run the picker pinned, by id, when the log file holds several (a rerun
// appended to the same path, accrue-ui#33). null follows the file's latest
// run — through a new one appearing — which is what a one-run file always is.
export const selectedRun = signal(null);

// Row-values cache: rowIndex -> {row, key, cells}. Insertion-ordered Map
// gives us LRU-ish eviction at the cap.
export const valuesCache = new Map();
export const valuesVersion = signal(0);
const VALUES_CAP = 2000;

// stepName -> field -> {keys, d}: the sub-keys seen under an object-valued
// field across loaded rows (first-seen order, capped) and the first default
// the server named. Grown as values load rather than scanned from the cache
// on every render — a live run re-renders the column heads ~10x/second.
const subKeysSeen = new Map();
const SUBKEYS_CAP = 32; // the server's SUBKEYS_MAX; here it caps the union across rows

function noteSubKeys(row) {
  for (const [stepName, cell] of Object.entries(row.cells || {})) {
    if (!cell || !cell.f) continue;
    for (const [field, entry] of Object.entries(cell.f)) {
      if (!entry || typeof entry !== "object") continue;
      let byField = subKeysSeen.get(stepName);
      if (!byField) subKeysSeen.set(stepName, (byField = new Map()));
      let seen = byField.get(field);
      if (!seen) byField.set(field, (seen = { keys: [], d: entry.d }));
      for (const k of Object.keys(entry.o)) {
        if (seen.keys.length >= SUBKEYS_CAP) break;
        if (!seen.keys.includes(k)) seen.keys.push(k);
      }
    }
  }
}

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

// The field a step's data column is currently showing: the chip's choice,
// but only while it is still visible. Cycling onto an internal field and
// then hiding internal fields would otherwise leave the column rendering a
// field the toggle says is hidden — fall back to the first visible one.
export function selectedField(step) {
  const fields = stepFields(step);
  const choice = fieldChoice.value[step.name];
  return choice && fields.includes(choice) ? choice : fields[0] || null;
}

// The sub-keys seen under a step's object-valued field, or null when no
// loaded row holds an object there (the column then has no sub-key chip).
export function subKeysFor(stepName, field) {
  valuesVersion.value; // subscribe: the registry grows as values load
  const byField = subKeysSeen.get(stepName);
  return (byField && byField.get(field)) || null;
}

// The sub-key a column's chip names for *field*: the column's choice while
// this field has it, else the field's default.
export function selectedSubKey(stepName, field) {
  const seen = subKeysFor(stepName, field);
  if (!seen) return null;
  const choice = subKeyChoice.value[stepName];
  return choice && seen.keys.includes(choice) ? choice : seen.d;
}

// The sub-key one cell shows for its {o, d} entry: the column's choice when
// this object has it — rows need not share keys — else its own default.
export function subKeyOf(stepName, entry) {
  const choice = subKeyChoice.value[stepName];
  return choice && choice in entry.o ? choice : entry.d;
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

// Does *row* have a failure accrue says will come back unchanged on retry?
// "Retry this row" re-runs every failed cell of the row, so any step counts.
export function rowFailsAgain(row) {
  const snap = snapshot.value;
  if (!snap) return false;
  return (snap.error_groups || []).some(
    (g) =>
      g.retryable === false &&
      (g.rows || []).some(([lo, hi]) => row >= lo && row <= hi)
  );
}

// ---- fetch helpers ------------------------------------------------------

// `path` scoped to the selected run: every run-scoped /api/* route takes an
// optional ?run=<id>, and leaving it off means the latest run.
export function apiPath(path) {
  const run = selectedRun.value;
  if (run == null) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}run=${encodeURIComponent(run)}`;
}

function decodeCells(cells) {
  const bin = atob(cells.data);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

export async function loadRun() {
  const res = await fetch(apiPath("/api/run"));
  if (res.status === 404 && selectedRun.value != null) {
    // The pinned run is gone (the log was rewritten): follow the latest.
    selectedRun.value = null;
    return loadRun();
  }
  if (!res.ok) throw new Error(`GET /api/run -> ${res.status}`);
  const data = await res.json();
  const cells = data.cells;
  const prev = snapshot.value;
  batch(() => {
    if (prev && prev.run.id !== data.run.id) forgetRun();
    snapshot.value = data;
    cellStates.value = decodeCells(cells);
    cellsVersion.value++;
    elapsedS.value = data.run.elapsed_s || 0;
  });
}

// Drop everything that belonged to the run on screen: another run (picked,
// or a new latest one) is replacing it, and its rows share indices with it.
function forgetRun() {
  valuesCache.clear();
  subKeysSeen.clear();
  valuesVersion.value++;
  selection.value = null;
  cellDetail.value = null;
  retryError.value = null;
}

const pendingWindows = new Set();

export async function loadValues(start, count) {
  const key = `${start}:${count}`;
  if (pendingWindows.has(key)) return;
  pendingWindows.add(key);
  try {
    const res = await fetch(apiPath(`/api/values?start=${start}&count=${count}`));
    if (!res.ok) return;
    const data = await res.json();
    for (const row of data.rows || []) {
      valuesCache.delete(row.row); // refresh position for LRU-ish eviction
      valuesCache.set(row.row, row);
      noteSubKeys(row);
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
    const res = await fetch(
      apiPath(`/api/cell/${encodeURIComponent(stepName)}/${row}`)
    );
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

// body: {rows:[...]} | {group:{step,type,key}} | {all:true}. `source` tags which
// button asked, so the error renders next to that one.
export async function postRetry(body, source) {
  if (retryBusy()) return false;
  retryPending.value = true;
  retryError.value = null;
  let res;
  try {
    res = await fetch(apiPath("/api/retry"), {
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
  const current = selectedField(step);
  const next = fields[(fields.indexOf(current) + 1) % fields.length];
  fieldChoice.value = { ...fieldChoice.value, [step.name]: next };
}

export function cycleSubKey(step, field) {
  const seen = subKeysFor(step.name, field);
  if (!seen || seen.keys.length < 2) return;
  const current = selectedSubKey(step.name, field);
  const next = seen.keys[(seen.keys.indexOf(current) + 1) % seen.keys.length];
  subKeyChoice.value = { ...subKeyChoice.value, [step.name]: next };
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
