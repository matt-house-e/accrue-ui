// Search, per-column filters and sort for the data grid (accrue-ui#37): the
// search box with its scope, the header menus, and the filter bar that says
// what the grid is showing. Every control writes the store's query signals;
// the store turns them into one /api/rows request.
import { useEffect, useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { IconSearch } from "../lib/icons.js";
import { MenuButton } from "../lib/popover.js";
import { fmtInt } from "../lib/fmt.js";
import {
  STATE_NAMES,
  clearQuery,
  columnFilters,
  loadFacet,
  loadRows,
  queryActive,
  rowCount,
  rowFilter,
  rowsError,
  rowsLoading,
  rowsStale,
  searchQuery,
  searchScope,
  selectedSubKey,
  serverRows,
  setFilter,
  setSort,
  snapshot,
  sortSpec,
  subKeysFor,
} from "../lib/store.js";

const PRESENCE = [
  ["any", "Any value"],
  ["empty", "Is empty / null"],
  ["notempty", "Is not empty"],
];
const STRING_OPS = [
  ["contains", "contains"],
  ["eq", "equals"],
  ["regex", "matches regex"],
];
const NUMERIC_TYPES = new Set(["int", "float", "number", "integer"]);

const IconFilter = ({ size = 12 }) =>
  html`<svg width=${size} height=${size} viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <path
      d="M2 3h12l-4.5 5.5V13l-3 1V8.5L2 3z"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linejoin="round"
    />
  </svg>`;

// The manifest's schema entry for a column, when the log carries one.
export function fieldSpec(snap, step, field) {
  const ov = snap.overview;
  if (!ov || !ov.present) return null;
  return ov.fields.find((f) => f.step === step && f.name === field) || null;
}

// Which filter control a column gets: typed from the manifest; a log with no
// manifest, or an unknown type, gets the string controls.
function kindOf(spec) {
  if (!spec) return "str";
  if (spec.type === "enum" && Array.isArray(spec.enum)) return "enum";
  if (spec.type === "bool" || spec.type === "boolean") return "bool";
  if (NUMERIC_TYPES.has(spec.type)) return "number";
  return "str";
}

// Case-insensitive <mark>s on every occurrence of `q` in `text`.
export function highlight(text, q) {
  if (!q || typeof text !== "string" || !text) return text;
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  const parts = [];
  let i = 0;
  let j;
  while ((j = lower.indexOf(needle, i)) !== -1 && parts.length < 40) {
    if (j > i) parts.push(text.slice(i, j));
    parts.push(html`<mark>${text.slice(j, j + needle.length)}</mark>`);
    i = j + needle.length;
  }
  if (!parts.length) return text;
  if (i < text.length) parts.push(text.slice(i));
  return parts;
}

// ---- search ----------------------------------------------------------------

export function SearchBox() {
  const scope = searchScope.value;
  const scoped = scope !== "all" && scope !== "keys";
  return html`<label class="search" title="Search is server-side, over every row">
    <${IconSearch} />
    <input
      type="text"
      placeholder="Search values…"
      value=${searchQuery.value}
      onInput=${(e) => (searchQuery.value = e.target.value)}
    />
    <select
      class="search-scope"
      value=${scope}
      title="Where to search"
      onChange=${(e) => (searchScope.value = e.target.value)}
    >
      <option value="all">all fields</option>
      <option value="keys">keys only</option>
      ${scoped && html`<option value=${scope}>${scope.split("/").slice(1).join("/")}</option>`}
    </select>
  </label>`;
}

// ---- per-column filter -----------------------------------------------------

// A clause's list value when it is one. A hand-edited hash can carry any
// shape; the server refuses it (400, shown inline), but the chip and the
// editor must still render, or Clear all is out of reach.
function listOf(clause, op) {
  return clause && clause.op === op && Array.isArray(clause.v) ? clause.v : null;
}

function facetCounts(facet) {
  const counts = new Map();
  for (const [value, n] of (facet && facet.values) || []) counts.set(JSON.stringify(value), n);
  return counts;
}

function FilterEditor({ col, step, field, spec, close }) {
  const current = columnFilters.value[col] || null;
  const kind = kindOf(spec);
  const presenceNow =
    current && (current.op === "empty" || current.op === "notempty") ? current.op : "any";
  const [presence, setPresence] = useState(presenceNow);
  const [picked, setPicked] = useState(listOf(current, "in") || []);
  const range = listOf(current, "range") || [null, null];
  const [lo, setLo] = useState(range[0] == null ? "" : String(range[0]));
  const [hi, setHi] = useState(range[1] == null ? "" : String(range[1]));
  const [strOp, setStrOp] = useState(
    current && STRING_OPS.some(([op]) => op === current.op) ? current.op : "contains"
  );
  const [text, setText] = useState(current && typeof current.v === "string" ? current.v : "");
  const [facet, setFacet] = useState(null);
  const facets = kind === "enum" || kind === "bool";
  // Object-valued column: the filter, and the facet counts, apply to the
  // sub-key the column shows.
  const sub = subKeysFor(step, field) ? selectedSubKey(step, field) : null;
  useEffect(() => {
    if (facets) loadFacet(col, sub).then(setFacet);
  }, [col, sub]);

  const apply = (e) => {
    if (e) e.preventDefault();
    let clause = null;
    if (presence !== "any") {
      clause = { op: presence };
    } else if (facets) {
      if (picked.length) clause = { op: "in", v: picked };
    } else if (kind === "number") {
      const a = lo.trim() === "" ? null : Number(lo);
      const b = hi.trim() === "" ? null : Number(hi);
      if ((a != null && !Number.isNaN(a)) || (b != null && !Number.isNaN(b))) {
        clause = { op: "range", v: [Number.isNaN(a) ? null : a, Number.isNaN(b) ? null : b] };
      }
    } else if (text.trim()) {
      clause = { op: strOp, v: text };
    }
    if (clause && sub) clause.sub = sub;
    setFilter(col, clause);
    close();
  };
  const clear = () => {
    setFilter(col, null);
    close();
  };
  const toggle = (v) =>
    setPicked(picked.some((p) => p === v) ? picked.filter((p) => p !== v) : [...picked, v]);

  // Facet options: the manifest's members (zero counts included) plus any
  // value the rows actually hold. Counts are over settled cells matching the
  // other filters, so they need not add up to the row total.
  let options = null;
  if (facets) {
    const counts = facetCounts(facet);
    const values = kind === "bool" ? [true, false] : [...(spec.enum || [])];
    for (const [v] of (facet && facet.values) || []) if (!values.includes(v)) values.push(v);
    options = values.map((v) => [v, counts.get(JSON.stringify(v)) ?? (facet ? 0 : null)]);
    if (kind === "bool") options.push([null, facet ? facet.empty : null]);
  }

  return html`<form class="filter-editor" onSubmit=${apply}>
    <div class="popover-head">
      <span class="mono">${field}${sub ? html`<span class="dim">.${sub}</span>` : ""}</span>
      ${spec && html`<span class="type-chip">${spec.type}</span>`}
    </div>
    <select
      class="filter-select"
      value=${presence}
      onChange=${(e) => setPresence(e.target.value)}
    >
      ${PRESENCE.map(([v, label]) => html`<option value=${v}>${label}</option>`)}
    </select>
    ${presence === "any" &&
    options &&
    html`<div class="facet-list">
      ${options.map(
        ([v, n]) => html`<label class="facet-item" key=${JSON.stringify(v)}>
          <input type="checkbox" checked=${picked.includes(v)} onChange=${() => toggle(v)} />
          <span class="mono facet-value">${v === null ? "null" : String(v)}</span>
          ${n != null && html`<span class="facet-count">${fmtInt(n)}</span>`}
        </label>`
      )}
    </div>`}
    ${presence === "any" &&
    kind === "number" &&
    html`<div class="range-row">
      <input
        class="filter-input mono"
        type="number"
        step="any"
        placeholder="min"
        value=${lo}
        onInput=${(e) => setLo(e.target.value)}
      />
      <span class="dim">–</span>
      <input
        class="filter-input mono"
        type="number"
        step="any"
        placeholder="max"
        value=${hi}
        onInput=${(e) => setHi(e.target.value)}
      />
    </div>`}
    ${presence === "any" &&
    kind === "str" &&
    html`<div class="string-row">
      <select class="filter-select" value=${strOp} onChange=${(e) => setStrOp(e.target.value)}>
        ${STRING_OPS.map(([v, label]) => html`<option value=${v}>${label}</option>`)}
      </select>
      <input
        class="filter-input mono"
        type="text"
        placeholder=${strOp === "regex" ? "pattern" : "text"}
        value=${text}
        onInput=${(e) => setText(e.target.value)}
      />
    </div>`}
    <div class="filter-actions">
      <button type="button" class="link-btn" onClick=${() => (searchScope.value = col)}>
        Search this column
      </button>
      <span class="spacer"></span>
      ${current && html`<button type="button" class="chip" onClick=${clear}>Clear</button>`}
      <button type="submit" class="chip primary">Apply</button>
    </div>
  </form>`;
}

export function ColumnFilterButton({ col, step, field }) {
  const snap = snapshot.value;
  const active = !!columnFilters.value[col];
  return html`<${MenuButton}
    class="head-btn"
    title="Filter this column"
    active=${active}
    label=${html`<${IconFilter} />`}
    render=${(close) => html`<${FilterEditor}
      col=${col}
      step=${step}
      field=${field}
      spec=${fieldSpec(snap, step, field)}
      close=${close}
    />`}
  />`;
}

// ---- per-step state menu ---------------------------------------------------

function StateEditor({ step, close }) {
  const current = columnFilters.value[step];
  const [picked, setPicked] = useState(listOf(current, "state") || []);
  const sort = sortSpec.value;
  const toggle = (s) =>
    setPicked(picked.includes(s) ? picked.filter((p) => p !== s) : [...picked, s]);
  const apply = (e) => {
    e.preventDefault();
    setFilter(step, picked.length ? { op: "state", v: picked } : null);
    close();
  };
  return html`<form class="filter-editor" onSubmit=${apply}>
    <div class="popover-head"><span class="mono">${step}</span><span>cell state</span></div>
    <div class="facet-list">
      ${STATE_NAMES.map(
        (name, s) => html`<label class="facet-item" key=${s}>
          <input type="checkbox" checked=${picked.includes(s)} onChange=${() => toggle(s)} />
          <span class=${`legend-swatch s${s}`}></span>
          <span>${name}</span>
        </label>`
      )}
    </div>
    <div class="filter-actions">
      <button
        type="button"
        class=${`chip${sort && sort.col === step && sort.dir === "desc" ? " active" : ""}`}
        title="Worst first"
        onClick=${() => {
          setSort(step, sort && sort.col === step && sort.dir === "desc" ? null : "desc");
          close();
        }}
      >
        Sort by state ↓
      </button>
      <span class="spacer"></span>
      ${current && html`<button
        type="button"
        class="chip"
        onClick=${() => {
          setFilter(step, null);
          close();
        }}
      >
        Clear
      </button>`}
      <button type="submit" class="chip primary">Apply</button>
    </div>
  </form>`;
}

export function StateMenuButton({ step }) {
  const active = !!columnFilters.value[step];
  return html`<${MenuButton}
    class="head-btn"
    title="Filter or sort by this step's cell state"
    active=${active}
    label=${html`<${IconFilter} />`}
    render=${(close) => html`<${StateEditor} step=${step} close=${close} />`}
  />`;
}

// ---- the filter bar --------------------------------------------------------

function fieldOf(col) {
  const i = col.indexOf("/");
  return i === -1 ? col : col.slice(i + 1);
}

function clauseLabel(col, f) {
  const name = col === "*" ? "any step" : fieldOf(col);
  if (!f || typeof f !== "object") return `${name}: invalid filter`;
  const quote = (v) => (typeof v === "string" ? `“${v}”` : v === null ? "null" : String(v));
  const list = Array.isArray(f.v) ? f.v : null;
  switch (f.op) {
    case "empty":
      return `${name} is empty`;
    case "notempty":
      return `${name} is not empty`;
    case "in":
      if (list) return `${name} ${list.length === 1 ? "=" : "in"} ${list.map(quote).join(", ")}`;
      break;
    case "range": {
      if (!list) break;
      const [a, b] = list;
      if (a != null && b != null) return `${name} ${a}–${b}`;
      return a != null ? `${name} ≥ ${a}` : `${name} ≤ ${b}`;
    }
    case "contains":
      return `${name} contains ${quote(f.v)}`;
    case "eq":
      return `${name} = ${quote(f.v)}`;
    case "regex":
      return `${name} ~ /${f.v}/`;
    case "state":
      if (list) return `${name} state: ${list.map((s) => STATE_NAMES[s]).join(", ")}`;
      break;
  }
  return `${name} ${f.op}`;
}

function Chip({ label, onRemove }) {
  return html`<span class="filter-chip">
    <span>${label}</span>
    <button class="chip-x" title="Remove" onClick=${onRemove}>×</button>
  </span>`;
}

export function FilterBar() {
  if (!queryActive()) return null;
  const sr = serverRows.value;
  const total = sr ? sr.total : rowCount();
  const filters = columnFilters.value;
  const q = searchQuery.value.trim();
  const sort = sortSpec.value;
  const chip = rowFilter.value;
  const nFilters = Object.keys(filters).length + (chip !== "all" ? 1 : 0);
  const parts = [
    sr ? `${fmtInt(sr.matched)} of ${fmtInt(total)} rows` : `${fmtInt(total)} rows`,
    nFilters > 0 && `${nFilters} ${nFilters === 1 ? "filter" : "filters"}`,
    sort && `sorted by ${fieldOf(sort.col)}${sort.sub ? `.${sort.sub}` : ""} ${sort.dir === "asc" ? "↑" : "↓"}`,
  ].filter(Boolean);
  return html`<div class="filter-bar" role="status">
    <span class="filter-summary mono">${parts.join(" · ")}${rowsLoading.value ? " …" : ""}</span>
    ${q && html`<${Chip} label=${`search ${searchScope.value === "all" ? "" : `${fieldOf(searchScope.value)} `}“${q}”`} onRemove=${() => (searchQuery.value = "")} />`}
    ${chip !== "all" &&
    html`<${Chip} label=${`any step: ${chip}`} onRemove=${() => (rowFilter.value = "all")} />`}
    ${Object.entries(filters).map(
      ([col, f]) =>
        html`<${Chip} key=${col} label=${clauseLabel(col, f)} onRemove=${() => setFilter(col, null)} />`
    )}
    ${sort && html`<${Chip} label=${`sort ${fieldOf(sort.col)} ${sort.dir === "asc" ? "↑" : "↓"}`} onRemove=${() => setSort(sort.col, null)} />`}
    ${rowsStale.value &&
    html`<button class="chip stale" title="Cells changed since this list was computed" onClick=${() => loadRows()}>
      Results may be stale · Refresh
    </button>`}
    ${rowsError.value && html`<span class="filter-error">${rowsError.value}</span>`}
    <span class="spacer"></span>
    <button class="chip" onClick=${clearQuery}>Clear all</button>
  </div>`;
}
