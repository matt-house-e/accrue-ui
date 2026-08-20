// Run grid: status + data render modes, hand-rolled row virtualization
// (fixed 38px pitch = 32px row + 6px gap; render visible rows ±overscan).
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { IconBolt, IconDot, StateIcon } from "../lib/icons.js";
import { fmtInt } from "../lib/fmt.js";
import {
  cellsVersion,
  cellStates,
  cycleField,
  errorGroupFor,
  fieldChoice,
  loadValues,
  rowFilter,
  searchQuery,
  select,
  selection,
  snapshot,
  sseStatus,
  stepFields,
  valuesCache,
  valuesVersion,
  viewMode,
} from "../lib/store.js";

const PITCH = 38; // px per row: 32px row + 6px gap
const OVERSCAN = 10;
// /api/values clamps count to 1000 (docs/api-shapes.md). A filtered window
// can span far more indices than that, so ask in pages instead of asking for
// the whole span and getting a short answer back forever.
const VALUES_PAGE_MAX = 1000;
const VALUES_MAX_PAGES = 8;

const LEGEND = [
  [0, "Pending"],
  [1, "Running"],
  [2, "Ok"],
  [3, "Cached"],
  [4, "Retrying"],
  [5, "Error"],
  [6, "Skipped"],
];

const FILTER_STATE = { errors: 5, retrying: 4, skipped: 6 };

function computeRows(arr, nsteps, filter, query) {
  if (!arr || !nsteps) return [];
  const total = Math.floor(arr.length / nsteps);
  const want = FILTER_STATE[filter];
  const q = query.trim().toLowerCase();
  const out = [];
  for (let r = 0; r < total; r++) {
    if (want !== undefined) {
      let hit = false;
      for (let s = 0; s < nsteps; s++) {
        if (arr[r * nsteps + s] === want) {
          hit = true;
          break;
        }
      }
      if (!hit) continue;
    }
    if (q) {
      const vrow = valuesCache.get(r);
      if (!vrow || !String(vrow.key).toLowerCase().includes(q)) continue;
    }
    out.push(r);
  }
  return out;
}

// [start, count] windows covering only the visible rows, first up to but
// not including last, that are NOT already cached — each at most
// VALUES_PAGE_MAX indices wide, which is what the server serves. Rows the
// cache already holds are never re-requested, so one pass fills the window
// and the next render has nothing left to ask for.
function missingWindows(rows, first, last) {
  const need = [];
  for (let i = first; i < last; i++) {
    const r = rows[i];
    if (r !== undefined && !valuesCache.has(r)) need.push(r);
  }
  const windows = [];
  let i = 0;
  while (i < need.length && windows.length < VALUES_MAX_PAGES) {
    const lo = need[i];
    let j = i;
    while (j < need.length && need[j] - lo < VALUES_PAGE_MAX) j++;
    windows.push([lo, need[j - 1] - lo + 1]);
    i = j;
  }
  return windows;
}

function ColumnHead({ step, stepIndex, mode }) {
  const fields = stepFields(step);
  const field = fieldChoice.value[step.name] || fields[0] || null;
  return html`<div class="col-head" key=${step.name}>
    <div class="step-name">
      ${step.name}
      <span class="level-chip">L${step.level}</span>
      ${step.mode === "batch" && html`<span class="batch-chip">batch</span>`}
    </div>
    <div class="done-count">${fmtInt(step.done)} / ${fmtInt(step.total)}</div>
    ${mode === "data" &&
    field &&
    html`<button
      class="field-chip"
      title="Cycle preview field"
      onClick=${() => cycleField(step)}
    >
      ${field}
    </button>`}
  </div>`;
}

function StatusCell({ stepName, stepIndex, row, state }) {
  const sel = selection.value;
  const isSel = sel && sel.step === stepName && sel.row === row;
  return html`<button
    class=${`cell s${state}${isSel ? " sel" : ""}`}
    title=${`row ${row} · ${stepName}`}
    onClick=${() => select(stepName, row)}
  >
    <${StateIcon} state=${state} />
  </button>`;
}

function DataCell({ stepName, row, state, vrow }) {
  const sel = selection.value;
  const isSel = sel && sel.step === stepName && sel.row === row;
  const cell = vrow && vrow.cells ? vrow.cells[stepName] : null;
  const v = cell ? cell.v : null;
  let body;
  if (state === 5) {
    const group = errorGroupFor(stepName, row);
    body = html`<span class="val">${v || (group ? group.type : "Error")}</span>`;
  } else if (state === 4) {
    body = html`<span class="val">${v || "retrying"}</span>`;
  } else if (state === 6) {
    body = html`<span class="val">${"—"}</span>`;
  } else if (state === 1) {
    body = html`<${IconDot} size=${12} />`;
  } else if (state === 3) {
    body = html`<span class="prefix"><${IconBolt} size=${11} /></span>
      <span class="val">${v || ""}</span>`;
  } else {
    body = html`<span class="val">${v || ""}</span>`;
  }
  return html`<button
    class=${`cell dcell s${state}${isSel ? " sel" : ""}`}
    onClick=${() => select(stepName, row)}
  >
    ${body}
  </button>`;
}

export function Grid() {
  const snap = snapshot.value;
  const version = cellsVersion.value; // subscribe to cell mutations
  const vversion = valuesVersion.value; // subscribe to values loads
  const mode = viewMode.value;
  const filter = rowFilter.value;
  const query = searchQuery.value;
  const arr = cellStates.value;
  const steps = snap.steps;
  const nsteps = steps.length;

  // vversion is READ above (so loaded keys re-render) but deliberately NOT a
  // dependency here: including it made loading values recompute the row
  // list, which changed its identity, which re-fired the fetch effect below,
  // which loaded values — a self-sustaining ~6Hz refetch loop.
  const rows = useMemo(
    () => computeRows(arr, nsteps, filter, query),
    [arr, nsteps, filter, query, version]
  );
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const total = arr ? Math.floor(arr.length / nsteps) : 0;

  // ---- virtualization ----
  const scrollerRef = useRef(null);
  const [viewport, setViewport] = useState({ top: 0, height: 640 });
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const update = () =>
      setViewport({ top: el.scrollTop, height: el.clientHeight });
    update();
    el.addEventListener("scroll", update, { passive: true });
    let ro = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(update);
      ro.observe(el);
    }
    return () => {
      el.removeEventListener("scroll", update);
      if (ro) ro.disconnect();
    };
  }, []);

  // Clamped to the last row: switching to a filter with fewer matches would
  // otherwise leave `first` past the end for one frame and render nothing.
  const first = Math.min(
    Math.max(0, Math.floor(viewport.top / PITCH) - OVERSCAN),
    Math.max(0, rows.length - 1)
  );
  const last = Math.min(
    rows.length,
    Math.ceil((viewport.top + viewport.height) / PITCH) + OVERSCAN
  );

  // Fetch row values for the visible window (debounced, cache-aware).
  // The deps are what changes the *row set*, not what changes cell states:
  // a live run bumps cellsVersion ~10x/second, and re-running this on every
  // bump would clear and re-arm the debounce forever (values never load) as
  // well as re-requesting windows already in flight.
  useEffect(() => {
    if (last <= first) return;
    const timer = setTimeout(() => {
      for (const [start, count] of missingWindows(rowsRef.current, first, last)) {
        loadValues(start, count);
      }
    }, 150);
    return () => clearTimeout(timer);
  }, [first, last, filter, query, rows.length]);

  const labelCol = mode === "data" ? "150px" : "200px";
  const stepCol = mode === "data" ? "minmax(180px, 1fr)" : "minmax(96px, 1fr)";
  const template = `${labelCol} repeat(${nsteps}, ${stepCol})`;

  const visible = [];
  for (let i = first; i < last; i++) {
    const r = rows[i];
    const vrow = valuesCache.get(r);
    visible.push(html`<div
      class="grid-row"
      key=${r}
      style=${{ top: `${i * PITCH}px`, gridTemplateColumns: template }}
    >
      <div class="row-label">
        <span class="row-idx">${r}</span>
        <span class="row-key">${vrow ? vrow.key : ""}</span>
      </div>
      ${steps.map((step, si) => {
        const state = arr[r * nsteps + si];
        return mode === "data"
          ? html`<${DataCell}
              key=${step.name}
              stepName=${step.name}
              row=${r}
              state=${state}
              vrow=${vrow}
            />`
          : html`<${StatusCell}
              key=${step.name}
              stepName=${step.name}
              stepIndex=${si}
              row=${r}
              state=${state}
            />`;
      })}
    </div>`);
  }

  const windowLabel =
    rows.length === 0
      ? "no rows match"
      : `showing ${rows[Math.min(first, rows.length - 1)]}–${rows[last - 1]}`;
  const countLabel =
    rows.length === total
      ? `${fmtInt(total)} rows`
      : `${fmtInt(rows.length)} of ${fmtInt(total)} rows`;

  return html`<div class="grid-wrap">
    <div class="grid-scroller" ref=${scrollerRef}>
      <div class="grid-head" style=${{ gridTemplateColumns: template }}>
        <div class="col-head">
          <div class="step-name">${mode === "data" ? "key" : "row"}</div>
        </div>
        ${steps.map(
          (step, si) =>
            html`<${ColumnHead} step=${step} stepIndex=${si} mode=${mode} />`
        )}
      </div>
      <div class="grid-canvas" style=${{ height: `${rows.length * PITCH}px` }}>
        ${visible}
      </div>
    </div>
    <div class="grid-foot">
      <span>${countLabel} · ${windowLabel}</span>
      <div class="legend">
        ${LEGEND.map(
          ([s, label]) =>
            html`<span class="legend-item" key=${s}>
              <span class=${`legend-swatch s${s}`}></span>${label}
            </span>`
        )}
      </div>
      <span class="sse-status">
        <span class=${`sse-dot${sseStatus.value === "connected" ? " on" : ""}`}></span>
        SSE ${sseStatus.value}
      </span>
    </div>
  </div>`;
}
