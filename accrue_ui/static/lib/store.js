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
export const activeTab = signal("grid"); // 'grid' | 'errors' | 'cost'
export const rowFilter = signal("all"); // 'all' | 'errors' | 'retrying' | 'skipped'
export const searchQuery = signal("");
export const showInternalFields = signal(false);
export const fieldChoice = signal({}); // stepName -> selected field name
export const sseStatus = signal("idle"); // 'idle' | 'connected'
export const cellDetail = signal(null); // /api/cell payload for selection
export const cellDetailLoading = signal(false);
export const elapsedS = signal(0); // ticks locally while run.live

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

// Merge one SSE delta (see docs/api-shapes.md, GET /api/events).
export function applyDelta(delta) {
  batch(() => {
    const snap = snapshot.value;
    const arr = cellStates.value;
    const nsteps = snap ? snap.steps.length : 0;
    if (arr && nsteps && Array.isArray(delta.cells) && delta.cells.length) {
      for (const triple of delta.cells) {
        const [row, stepIndex, state] = triple;
        const i = row * nsteps + stepIndex;
        if (i >= 0 && i < arr.length && state >= 0 && state <= 6) arr[i] = state;
      }
      cellsVersion.value++;
    }
    if (snap && delta.stats && Object.keys(delta.stats).length) {
      snapshot.value = { ...snapshot.value, stats: { ...snap.stats, ...delta.stats } };
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
}
