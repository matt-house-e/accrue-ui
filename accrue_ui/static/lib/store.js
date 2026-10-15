// Signals-based state store + fetch helpers. All requests are same-origin
// relative /api/* paths (see docs/api-shapes.md).
import { signal, batch, effect } from "@preact/signals";

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
// colId ("step/field") -> the sub-key that data column shows for object-valued
// fields (accrue-ui#36). Per column: with one column per field (accrue-ui#37)
// a per-step choice would flip the other 48 columns on one click.
export const subKeyChoice = signal({});
// ---- data-grid layout (accrue-ui#37) -----------------------------------
export const hiddenCols = signal([]); // colIds the column picker hid
export const wrapCells = signal(false); // truncate (one line) | wrap (3 lines)
export const colWidths = signal({}); // colId -> px; memory only, not in the hash
// ---- query: search, filters, sort (accrue-ui#37) -----------------------
// The server answers /api/rows whenever any of these is set; the grid then
// shows exactly its row list (docs/api-shapes.md).
export const columnFilters = signal({}); // colId -> {op, v?, sub?}
export const sortSpec = signal(null); // {col, dir, sub?} | null
export const searchScope = signal("all"); // "all" | "keys" | colId
export const serverRows = signal(null); // {rows, total, matched} | null
export const rowsError = signal(null); // string | null: the last query failed
export const rowsLoading = signal(false);
export const rowsStale = signal(false); // cells changed since the last query
// The global state chips, as the server clause they ride (any step).
export const FILTER_STATE = { errors: 5, retrying: 4, skipped: 6 };
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

// Column identity for the data grid: one column per (step, field); a bare
// step name is the step's state column (docs/api-shapes.md, /api/rows).
export function colId(stepName, field) {
  return field == null ? stepName : `${stepName}/${field}`;
}

// Visible (non-internal unless toggled) field list for a step.
export function stepFields(step) {
  const fields = step.fields || [];
  return showInternalFields.value ? fields : fields.filter((f) => !f.startsWith("__"));
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
  const choice = subKeyChoice.value[colId(stepName, field)];
  return choice && seen.keys.includes(choice) ? choice : seen.d;
}

// The sub-key one cell shows for its {o, d} entry: the column's choice when
// this object has it — rows need not share keys — else its own default.
export function subKeyOf(stepName, field, entry) {
  const choice = subKeyChoice.value[colId(stepName, field)];
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
  // The query inputs stay (they are hash state); the answer belonged to the
  // old run's rows.
  serverRows.value = null;
  scheduleRows(0);
}

// Rows with a request in flight, so two renders never ask twice.
const pendingRows = new Set();
// /api/values?rows= serves at most this many per request (docs/api-shapes.md).
export const VALUES_PAGE_MAX = 1000;

// Values for an explicit row list — the visible rows, in whatever order a
// sort or filter put them (accrue-ui#37). A contiguous window could not
// serve a sorted grid without paging most of the run.
export async function loadValuesFor(rows) {
  const want = rows.filter((r) => !pendingRows.has(r));
  if (!want.length) return;
  for (const r of want) pendingRows.add(r);
  try {
    for (let i = 0; i < want.length; i += VALUES_PAGE_MAX) {
      const page = want.slice(i, i + VALUES_PAGE_MAX);
      const res = await fetch(apiPath(`/api/values?rows=${page.join(",")}`));
      if (!res.ok) continue;
      const data = await res.json();
      for (const row of data.rows || []) {
        valuesCache.delete(row.row); // refresh position for LRU-ish eviction
        valuesCache.set(row.row, row);
        noteSubKeys(row);
      }
    }
    while (valuesCache.size > VALUES_CAP) {
      valuesCache.delete(valuesCache.keys().next().value);
    }
    valuesVersion.value++;
  } catch {
    // dev stub / offline: values stay unloaded, grid shows indices only
  } finally {
    for (const r of want) pendingRows.delete(r);
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

export function cycleSubKey(step, field) {
  const seen = subKeysFor(step.name, field);
  if (!seen || seen.keys.length < 2) return;
  const current = selectedSubKey(step.name, field);
  const next = seen.keys[(seen.keys.indexOf(current) + 1) % seen.keys.length];
  subKeyChoice.value = { ...subKeyChoice.value, [colId(step.name, field)]: next };
}

export function toggleColumn(id) {
  const hidden = hiddenCols.value;
  hiddenCols.value = hidden.includes(id)
    ? hidden.filter((h) => h !== id)
    : [...hidden, id];
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
      // A filtered or sorted list was computed over cells that just moved.
      // Say so rather than re-query: re-sorting under the reader every flush
      // would yank rows out from under them (the live behaviour is #53).
      if (serverRows.value) rowsStale.value = true;
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

// ---- query: /api/rows -----------------------------------------------------
//
// Search, per-column filters, sort and the global state chip all become one
// server query (docs/api-shapes.md, GET /api/rows); the grid shows exactly
// the row list it returns. Nothing is filtered client-side any more, so the
// summary line and the grid can never disagree.

export function queryActive() {
  return !!(
    searchQuery.value.trim() ||
    Object.keys(columnFilters.value).length ||
    sortSpec.value ||
    rowFilter.value !== "all"
  );
}

// The filter clauses, in the server's shape.
export function queryClauses() {
  const clauses = [];
  for (const [col, f] of Object.entries(columnFilters.value)) {
    clauses.push({ col, ...f });
  }
  const want = FILTER_STATE[rowFilter.value];
  if (want !== undefined) clauses.push({ col: "*", op: "state", v: [want] });
  return clauses;
}

export function queryParams({ facet = null, facetSub = null } = {}) {
  const p = new URLSearchParams();
  const q = searchQuery.value.trim();
  if (q) {
    p.set("q", q);
    if (searchScope.value !== "all") p.set("scope", searchScope.value);
  }
  const clauses = queryClauses();
  if (clauses.length) p.set("filter", JSON.stringify(clauses));
  const s = sortSpec.value;
  if (s) {
    p.set("sort", `${s.col}:${s.dir}`);
    if (s.sub) p.set("sort_sub", s.sub);
  }
  if (facet) {
    p.set("facet", facet);
    if (facetSub) p.set("facet_sub", facetSub);
    p.set("only", "facet");
  }
  return p.toString();
}

export const ROWS_DEBOUNCE_MS = 200;
let rowsSeq = 0; // a response older than the latest request is dropped
let rowsTimer = null;

export function scheduleRows(delay = ROWS_DEBOUNCE_MS) {
  clearTimeout(rowsTimer);
  rowsTimer = setTimeout(() => {
    rowsTimer = null;
    loadRows();
  }, delay);
}

export async function loadRows() {
  const seq = ++rowsSeq;
  if (!queryActive()) {
    batch(() => {
      serverRows.value = null;
      rowsError.value = null;
      rowsLoading.value = false;
      rowsStale.value = false;
    });
    return;
  }
  rowsLoading.value = true;
  try {
    const res = await fetch(apiPath(`/api/rows?${queryParams()}`));
    if (seq !== rowsSeq) return;
    if (!res.ok) {
      // Keep the last good list on screen; say what went wrong beside it.
      let message = `query failed (${res.status})`;
      try {
        const payload = await res.json();
        message = payload.detail || message;
      } catch {
        // non-JSON error body: keep the status-code message
      }
      rowsError.value = message;
      return;
    }
    const data = await res.json();
    if (seq !== rowsSeq) return;
    batch(() => {
      serverRows.value = data;
      rowsError.value = null;
      rowsStale.value = false;
    });
  } catch (e) {
    if (seq === rowsSeq) rowsError.value = String(e);
  } finally {
    if (seq === rowsSeq) rowsLoading.value = false;
  }
}

// Value counts for one column, for its checkbox facets — on the sub-key the
// column shows when it is object-valued, the same key its clause will carry,
// so each count says what ticking that box would yield. The full query goes
// along; the server counts over the rows matching every OTHER clause, leaving
// the column's own out (docs/api-shapes.md, `facet=`).
export async function loadFacet(col, sub = null) {
  try {
    const res = await fetch(apiPath(`/api/rows?${queryParams({ facet: col, facetSub: sub })}`));
    if (!res.ok) return null;
    const data = await res.json();
    return data.facet || null;
  } catch {
    return null;
  }
}

export function setFilter(col, clause) {
  const next = { ...columnFilters.value };
  if (clause) next[col] = clause;
  else delete next[col];
  columnFilters.value = next;
}

export function setSort(col, dir, sub = null) {
  sortSpec.value = dir ? { col, dir, ...(sub ? { sub } : {}) } : null;
}

export function clearQuery() {
  batch(() => {
    searchQuery.value = "";
    searchScope.value = "all";
    columnFilters.value = {};
    sortSpec.value = null;
    rowFilter.value = "all";
  });
}

// Any input change re-queries, debounced. effect() tracks only what is read
// synchronously here, so loadRows' own reads (apiPath -> selectedRun) do not
// re-arm it.
effect(() => {
  searchQuery.value;
  searchScope.value;
  columnFilters.value;
  sortSpec.value;
  rowFilter.value;
  scheduleRows();
});

// ---- URL hash: the query is a bookmark ------------------------------------
//
// #q=&in=&f=<json>&sort=&sub=&rows=&hide=&wrap=1&view=data. Written with
// replaceState (no history entry per keystroke) and read once at boot, before
// the first loadRun, so a reload or a shared link restores the same view. The
// launch-token scrub in app.js keeps the hash.

let hashReady = false; // readHash() has run: writing before it would erase it

export function hashFor() {
  const p = new URLSearchParams();
  const q = searchQuery.value.trim();
  if (q) p.set("q", q);
  if (searchScope.value !== "all") p.set("in", searchScope.value);
  const filters = columnFilters.value;
  if (Object.keys(filters).length) p.set("f", JSON.stringify(filters));
  const s = sortSpec.value;
  if (s) {
    p.set("sort", `${s.col}:${s.dir}`);
    if (s.sub) p.set("sub", s.sub);
  }
  if (rowFilter.value !== "all") p.set("rows", rowFilter.value);
  if (hiddenCols.value.length) p.set("hide", hiddenCols.value.join(","));
  if (wrapCells.value) p.set("wrap", "1");
  if (viewMode.value === "data") p.set("view", "data");
  return p.toString();
}

export function readHash() {
  try {
    const raw = window.location.hash.replace(/^#/, "");
    if (!raw) return;
    const p = new URLSearchParams(raw);
    batch(() => {
      if (p.has("q")) searchQuery.value = p.get("q");
      if (p.has("in")) searchScope.value = p.get("in");
      if (p.has("f")) {
        try {
          const f = JSON.parse(p.get("f"));
          if (f && typeof f === "object" && !Array.isArray(f)) columnFilters.value = f;
        } catch {
          // a hand-edited hash: ignore the filters, keep the rest
        }
      }
      const sort = p.get("sort");
      const m = sort && /^(.+):(asc|desc)$/.exec(sort);
      if (m) sortSpec.value = { col: m[1], dir: m[2], ...(p.get("sub") ? { sub: p.get("sub") } : {}) };
      const chip = p.get("rows");
      if (chip && (chip === "all" || chip in FILTER_STATE)) rowFilter.value = chip;
      if (p.has("hide")) hiddenCols.value = p.get("hide").split(",").filter(Boolean);
      if (p.get("wrap") === "1") wrapCells.value = true;
      if (p.get("view") === "data") viewMode.value = "data";
    });
  } finally {
    hashReady = true;
  }
}

export function writeHash(hash) {
  if (!hashReady || typeof window === "undefined") return;
  try {
    const { pathname, search } = window.location;
    const url = `${pathname}${search}${hash ? `#${hash}` : ""}`;
    if (url !== `${pathname}${search}${window.location.hash}`) {
      window.history.replaceState(null, "", url);
    }
  } catch {
    // No history API: the view still works, it just is not bookmarkable.
  }
}

effect(() => writeHash(hashFor()));
