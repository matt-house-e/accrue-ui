// Run grid: status + data render modes, hand-rolled virtualization (fixed
// pitch: 32px row + 6px gap, or a taller fixed pitch when cells wrap; render
// visible rows ±overscan). Data mode lays out one column per field under a
// step band, pins the key column, and windows columns horizontally too — 49
// fields × 60 rows must not mean 3,000 live cells (accrue-ui#37).
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { IconBolt, IconDot, StateIcon } from "../lib/icons.js";
import { fmtInt } from "../lib/fmt.js";
import {
  cellsVersion,
  cellStates,
  colWidths,
  columnFilters,
  cycleSubKey,
  errorGroupFor,
  hiddenCols,
  loadValuesFor,
  queryActive,
  rowsError,
  rowsLoading,
  searchQuery,
  searchScope,
  select,
  selectedSubKey,
  selection,
  serverRows,
  setSort,
  showInternalFields,
  snapshot,
  sortSpec,
  sseStatus,
  subKeyOf,
  subKeysFor,
  valuesCache,
  valuesVersion,
  viewMode,
  wrapCells,
} from "../lib/store.js";
import { dataColumns } from "./columns.js";
import { ColumnFilterButton, StateMenuButton, highlight } from "./filters.js";

const ROW_H = 32; // px, one-line cells
const ROW_H_WRAP = 66; // px, three clamped lines
const ROW_GAP = 6;
const OVERSCAN = 10; // rows above and below the viewport
const COL_OVERSCAN = 2; // columns left and right of the viewport
const COL_W = 180; // default data column width, px
const COL_MIN_W = 72;
const LABEL_W = 150; // the pinned key column, data mode
const LABEL_W_STATUS = 200;
// A value longer than this is probably clipped in a default-width column:
// it gets the hover tooltip (dotted underline). Objects always do.
const TIP_MIN_CHARS = 20;

const LEGEND = [
  [0, "Pending"],
  [1, "Running"],
  [2, "Ok"],
  [3, "Cached"],
  [4, "Retrying"],
  [5, "Error"],
  [6, "Skipped"],
];

// The row list: the server's answer whenever a search, filter, sort or the
// global state chip is active (accrue-ui#37) — nothing is filtered on the
// client any more, so the summary and the grid agree — else every row. While
// a query is active with no answer yet, the list is empty, not unfiltered.
function computeRows(arr, nsteps, sr, active) {
  if (!arr || !nsteps) return [];
  const total = Math.floor(arr.length / nsteps);
  if (active) return sr ? sr.rows.filter((r) => r < total) : [];
  const out = new Array(total);
  for (let r = 0; r < total; r++) out[r] = r;
  return out;
}

// The visible rows (positions first..last-1 of `rows`) whose values are not
// cached yet. Asked for by explicit row list, in whatever order the list has:
// a sorted or filtered grid shows scattered indices, which a contiguous
// window could not serve without paging most of the run.
function missingRows(rows, first, last) {
  const need = [];
  for (let i = first; i < last; i++) {
    const r = rows[i];
    if (r !== undefined && !valuesCache.has(r)) need.push(r);
  }
  return need;
}

// ---- columns -------------------------------------------------------------

// Horizontal layout of the data columns: each column's x offset (after the
// pinned label column) and the row's total width. Columns are px throughout
// — a `1fr` would let the header bands and body rows drift apart once one is
// resized, and sticky positioning needs rows as wide as their content.
function layout(cols, widths, labelW) {
  const xs = [];
  let x = labelW + ROW_GAP;
  for (let i = 0; i < cols.length; i++) {
    xs.push(x);
    x += widths[i] + ROW_GAP;
  }
  return { xs, total: x };
}

// Columns intersecting [left, left + width), ±overscan.
function colWindow(xs, widths, left, width) {
  const n = xs.length;
  let a = 0;
  while (a < n && xs[a] + widths[a] < left) a++;
  let b = a;
  while (b < n && xs[b] < left + width) b++;
  return [Math.max(0, a - COL_OVERSCAN), Math.min(n, b + COL_OVERSCAN)];
}

// Grid template for the pinned label, a spacer standing in for the columns
// scrolled out on the left, the rendered columns, and a right spacer.
function template(labelW, widths, cFirst, cLast) {
  const parts = [`${labelW}px`];
  if (cFirst > 0) {
    const lead = widths.slice(0, cFirst).reduce((s, w) => s + w, 0) + ROW_GAP * (cFirst - 1);
    parts.push(`${lead}px`);
  }
  for (let i = cFirst; i < cLast; i++) parts.push(`${widths[i]}px`);
  if (cLast < widths.length) {
    const rest = widths.slice(cLast);
    const tail = rest.reduce((s, w) => s + w, 0) + ROW_GAP * (rest.length - 1);
    parts.push(`${tail}px`);
  }
  return parts.join(" ");
}

function startResize(e, id, width) {
  e.preventDefault();
  e.stopPropagation();
  const x0 = e.clientX;
  const move = (ev) => {
    const w = Math.max(COL_MIN_W, Math.round(width + ev.clientX - x0));
    colWidths.value = { ...colWidths.value, [id]: w };
  };
  const up = () => {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    document.body.classList.remove("resizing");
  };
  document.body.classList.add("resizing");
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

// ---- headers -------------------------------------------------------------

function StepBand({ step, span }) {
  return html`<div class="col-head step-band" style=${{ gridColumn: `span ${span}` }}>
    <div class="step-name">
      ${step.name}
      <span class="level-chip">L${step.level}</span>
      ${step.mode === "batch" && html`<span class="batch-chip">batch</span>`}
      <${StateMenuButton} step=${step.name} />
    </div>
    <div class="done-count">${fmtInt(step.done)} / ${fmtInt(step.total)}</div>
  </div>`;
}

// Clicking a field name cycles its sort: none -> asc -> desc -> none.
function cycleSort(col, step, field) {
  const s = sortSpec.value;
  const dir = s && s.col === col ? (s.dir === "asc" ? "desc" : null) : "asc";
  const sub = subKeysFor(step, field) ? selectedSubKey(step, field) : null;
  setSort(col, dir, sub);
}

function FieldHead({ col, width }) {
  const { step, field } = col;
  if (field === null) {
    return html`<div class="col-head field-head" key=${col.id}>
      <div class="field-name dim">state</div>
    </div>`;
  }
  const subKeys = subKeysFor(step.name, field);
  const sort = sortSpec.value;
  const sorted = sort && sort.col === col.id ? sort.dir : null;
  const filtered = !!columnFilters.value[col.id];
  return html`<div
    class=${`col-head field-head${sorted ? " sorted" : ""}${filtered ? " filtered" : ""}`}
    key=${col.id}
  >
    <button
      class="field-name"
      title=${`${field} — click to sort`}
      onClick=${() => cycleSort(col.id, step.name, field)}
    >
      ${field}${sorted && html`<span class="sort-glyph">${sorted === "asc" ? "↑" : "↓"}</span>`}
    </button>
    <${ColumnFilterButton} col=${col.id} step=${step.name} field=${field} />
    ${subKeys &&
    html`<button
      class="field-chip subkey-chip"
      title="Cycle sub-key shown"
      onClick=${() => cycleSubKey(step, field)}
    >
      .${selectedSubKey(step.name, field)}
    </button>`}
    <div
      class="col-resize"
      title="Drag to resize"
      onMouseDown=${(e) => startResize(e, col.id, width)}
    ></div>
  </div>`;
}

// Status mode keeps one column per step; its state menu filters and sorts
// the shared row list just like the data-mode band's does.
function StatusHead({ step }) {
  return html`<div class="col-head" key=${step.name}>
    <div class="step-name">
      ${step.name}
      <span class="level-chip">L${step.level}</span>
      ${step.mode === "batch" && html`<span class="batch-chip">batch</span>`}
      <${StateMenuButton} step=${step.name} />
    </div>
    <div class="done-count">${fmtInt(step.done)} / ${fmtInt(step.total)}</div>
  </div>`;
}

// ---- cells ---------------------------------------------------------------

function StatusCell({ stepName, row, state, data = false }) {
  const sel = selection.value;
  const isSel = sel && sel.step === stepName && sel.row === row;
  return html`<button
    class=${`cell s${state}${data ? " dcell state-only" : ""}${isSel ? " sel" : ""}`}
    title=${`row ${row} · ${stepName}`}
    onClick=${() => select(stepName, row)}
  >
    <${StateIcon} state=${state} />
  </button>`;
}

// Hover text for an object-valued field: every sub-key, one per line.
function subKeysTip(entry) {
  return Object.entries(entry.o)
    .map(([k, v]) => `${k}: ${v === null ? "null" : v}`)
    .join("\n");
}

// A completed cell's value: a field present but null renders as a faint
// "null", so it never looks like an empty string (accrue-ui#36). `hl` is the
// search text to mark, when the search covers this column.
function Val({ v, isNull, hl }) {
  return isNull
    ? html`<span class="val null">null</span>`
    : html`<span class="val">${hl ? highlight(v || "", hl) : v || ""}</span>`;
}

function DataCell({ step, stepName, field, row, state, vrow, hl }) {
  const sel = selection.value;
  const isSel = sel && sel.step === stepName && sel.row === row;
  const cell = vrow && vrow.cells ? vrow.cells[stepName] : null;
  // cell.f carries every field the step produced, internal ones included
  // (accrue-ui#25), keyed by name; cell.v (its first non-internal entry) is
  // the fallback for cells with no field map (error/retrying/etc).
  let v = cell && cell.f ? cell.f[field] : cell ? cell.v : null;
  // An object-valued field arrives as {o, d} (docs/api-shapes.md): show the
  // column's sub-key, the whole object on hover.
  let tip = null;
  if (v && typeof v === "object") {
    tip = subKeysTip(v);
    v = v.o[subKeyOf(stepName, field, v)];
  } else if (typeof v === "string" && v.length > TIP_MIN_CHARS) {
    tip = v; // the preview; the full value is one click away in the inspector
  }
  // null only when the field is there and null — an absent field stays blank.
  const isNull = v === null && !!(cell && cell.f && field in cell.f);
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
      <${Val} v=${v} isNull=${isNull} hl=${hl} />`;
  } else {
    body = html`<${Val} v=${v} isNull=${isNull} hl=${hl} />`;
  }
  return html`<button
    class=${`cell dcell s${state}${isSel ? " sel" : ""}${tip ? " has-tip" : ""}`}
    data-tip=${tip}
    onClick=${() => select(stepName, row)}
  >
    ${body}
  </button>`;
}

// ---- grid ----------------------------------------------------------------

export function Grid() {
  const snap = snapshot.value;
  const version = cellsVersion.value; // subscribe to cell mutations
  const vversion = valuesVersion.value; // subscribe to values loads
  const mode = viewMode.value;
  const active = queryActive();
  const sr = serverRows.value;
  const query = searchQuery.value.trim();
  const scope = searchScope.value;
  const arr = cellStates.value;
  const steps = snap.steps;
  const nsteps = steps.length;
  const data = mode === "data";
  const wrap = data && wrapCells.value;
  const hidden = hiddenCols.value;
  const internal = showInternalFields.value;
  const widthsChoice = colWidths.value;

  // vversion is READ above (so loaded keys re-render) but deliberately NOT a
  // dependency here: including it made loading values recompute the row
  // list, which changed its identity, which re-fired the fetch effect below,
  // which loaded values — a self-sustaining ~6Hz refetch loop.
  const rows = useMemo(() => computeRows(arr, nsteps, sr, active), [arr, nsteps, sr, active]);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const total = arr ? Math.floor(arr.length / nsteps) : 0;

  // Data mode: one column per field. The sub-key registry grows as values
  // load (vversion, read above), which is what puts a sub-key chip on a column.
  const cols = useMemo(
    () =>
      data
        ? dataColumns(snap)
        : steps.map((step, stepIndex) => ({ id: step.name, step, stepIndex, field: null })),
    [snap, data, hidden, internal]
  );
  const labelW = data ? LABEL_W : LABEL_W_STATUS;
  const widths = cols.map((c) => (data ? widthsChoice[c.id] || COL_W : 0));
  const { xs, total: rowW } = layout(cols, widths, labelW);

  // ---- virtualization ----
  const scrollerRef = useRef(null);
  const [viewport, setViewport] = useState({ top: 0, height: 640, left: 0, width: 1200 });
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const update = () =>
      setViewport({
        top: el.scrollTop,
        height: el.clientHeight,
        left: el.scrollLeft,
        width: el.clientWidth,
      });
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

  const rowH = wrap ? ROW_H_WRAP : ROW_H;
  const pitch = rowH + ROW_GAP;
  // Clamped to the last row: switching to a filter with fewer matches would
  // otherwise leave `first` past the end for one frame and render nothing.
  const first = Math.min(
    Math.max(0, Math.floor(viewport.top / pitch) - OVERSCAN),
    Math.max(0, rows.length - 1)
  );
  const last = Math.min(
    rows.length,
    Math.ceil((viewport.top + viewport.height) / pitch) + OVERSCAN
  );
  const [cFirst, cLast] = data
    ? colWindow(xs, widths, viewport.left, viewport.width)
    : [0, cols.length];

  // Fetch row values for the visible window (debounced, cache-aware).
  // The deps are what changes the *row set*, not what changes cell states:
  // a live run bumps cellsVersion ~10x/second, and re-running this on every
  // bump would clear and re-arm the debounce forever (values never load) as
  // well as re-requesting rows already in flight.
  useEffect(() => {
    if (last <= first) return;
    const timer = setTimeout(() => {
      loadValuesFor(missingRows(rowsRef.current, first, last));
    }, 150);
    return () => clearTimeout(timer);
  }, [first, last, rows]);

  // Highlight search hits in the columns the search covers.
  const hlKeys = query && (scope === "all" || scope === "keys") ? query : null;
  const hlFor = (col) =>
    query && (scope === "all" || scope === col.id) ? query : null;

  const gridTemplate = data
    ? template(labelW, widths, cFirst, cLast)
    : `${labelW}px repeat(${nsteps}, minmax(96px, 1fr))`;
  const rowStyle = data
    ? { gridTemplateColumns: gridTemplate, width: `${rowW}px` }
    : { gridTemplateColumns: gridTemplate };
  const lead = data && cFirst > 0 ? html`<div class="col-spacer"></div>` : null;
  const tail = data && cLast < cols.length ? html`<div class="col-spacer"></div>` : null;

  const visible = [];
  for (let i = first; i < last; i++) {
    const r = rows[i];
    const vrow = valuesCache.get(r);
    visible.push(html`<div
      class=${`grid-row${data ? " fixed" : ""}`}
      key=${r}
      style=${{ top: `${i * pitch}px`, ...rowStyle }}
    >
      <div class=${`row-label${data ? " pinned" : ""}`}>
        <span class="row-idx">${r}</span>
        <span class="row-key">${vrow ? highlight(String(vrow.key), hlKeys) : ""}</span>
      </div>
      ${lead}
      ${cols.slice(cFirst, cLast).map((col) => {
        const state = arr[r * nsteps + col.stepIndex];
        if (!data || col.field === null) {
          return html`<${StatusCell}
            key=${col.id}
            stepName=${col.step.name}
            row=${r}
            state=${state}
            data=${data}
          />`;
        }
        return html`<${DataCell}
          key=${col.id}
          step=${col.step}
          stepName=${col.step.name}
          field=${col.field}
          row=${r}
          state=${state}
          vrow=${vrow}
          hl=${hlFor(col)}
        />`;
      })}
      ${tail}
    </div>`);
  }

  // Step bands span the rendered columns of each step, in order.
  const bands = [];
  if (data) {
    for (let i = cFirst; i < cLast; ) {
      const step = cols[i].step;
      let j = i;
      while (j < cLast && cols[j].step === step) j++;
      bands.push(html`<${StepBand} key=${step.name} step=${step} span=${j - i} />`);
      i = j;
    }
  }

  // Positions in the list, not row indices: a sorted list has no span.
  const windowLabel =
    rows.length === 0
      ? active && !sr
        ? rowsError.value
          ? "query failed"
          : "loading…"
        : "no rows match"
      : `showing ${fmtInt(Math.min(first, rows.length - 1) + 1)}–${fmtInt(last)}`;
  const countLabel =
    rows.length === total
      ? `${fmtInt(total)} rows`
      : `${fmtInt(rows.length)} of ${fmtInt(total)} rows`;
  const empty =
    rows.length === 0 &&
    html`<div class="grid-empty">
      ${active && !sr
        ? rowsError.value
          ? html`<span class="filter-error">${rowsError.value}</span>`
          : rowsLoading.value
            ? "Querying…"
            : "Querying…"
        : "No rows match."}
    </div>`;

  const head = data
    ? html`<div class="head-row" style=${rowStyle}>
          <div class="col-head pinned"></div>
          ${lead}${bands}${tail}
        </div>
        <div class="head-row fields" style=${rowStyle}>
          <div class="col-head pinned">
            <div class="field-name">key</div>
          </div>
          ${lead}
          ${cols
            .slice(cFirst, cLast)
            .map((col, k) => html`<${FieldHead} col=${col} width=${widths[cFirst + k]} />`)}
          ${tail}
        </div>`
    : html`<div class="head-row" style=${rowStyle}>
        <div class="col-head">
          <div class="step-name">row</div>
        </div>
        ${steps.map((step) => html`<${StatusHead} step=${step} />`)}
      </div>`;

  return html`<div
    class=${`grid-wrap${wrap ? " wrap" : ""}`}
    style=${{ "--row-h": `${rowH}px` }}
  >
    <div class="grid-scroller" ref=${scrollerRef}>
      <div
        class=${`grid-head${data ? " fixed" : ""}`}
        style=${data ? { width: `${rowW}px` } : null}
      >
        ${head}
      </div>
      <div
        class="grid-canvas"
        style=${{
          height: `${rows.length * pitch}px`,
          ...(data ? { width: `${rowW}px` } : {}),
        }}
      >
        ${visible}
      </div>
      ${empty}
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
