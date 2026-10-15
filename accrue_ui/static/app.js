// accrue watch — app shell. Boot: fetch /api/run, then subscribe to SSE.
import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { html } from "./lib/html.js";
import { IconChevronDown } from "./lib/icons.js";
import {
  fmtDuration,
  fmtInt,
  fmtMoney,
  fmtPct,
  fmtUnpriced,
  unpricedTip,
} from "./lib/fmt.js";
import { connect, disconnect } from "./lib/sse.js";
import { MenuButton } from "./lib/popover.js";
import { initTooltips } from "./lib/tooltip.js";
import {
  activeTab,
  apiPath,
  closeInspector,
  elapsedS,
  loadRun,
  loadRuns,
  readHash,
  rowFilter,
  selectLog,
  selectedRun,
  selection,
  showInternalFields,
  snapshot,
  viewMode,
  wrapCells,
} from "./lib/store.js";
import { ColumnPickerButton } from "./views/columns.js";
import { FilterBar, SearchBox } from "./views/filters.js";
import { Grid } from "./views/grid.js";
import { Inspector } from "./views/inspector.js";
import { Triage } from "./views/triage.js";
import { Cost } from "./views/cost.js";
import { Overview } from "./views/overview.js";

function Logo() {
  return html`<svg
    class="logo"
    width="20"
    height="20"
    viewBox="0 0 20 20"
    aria-hidden="true"
  >
    <rect x="1" y="1" width="8" height="8" rx="2" fill="var(--jade-11)" />
    <rect x="11" y="1" width="8" height="8" rx="2" fill="var(--border-2)" />
    <rect x="1" y="11" width="8" height="8" rx="2" fill="var(--border-2)" />
    <rect x="11" y="11" width="8" height="8" rx="2" fill="var(--border-2)" />
  </svg>`;
}

// `name` is log-derived (the log file's stem) and `id` is the run id, which
// are usually the same string — "2026-08-17a / 2026-08-17a" reads as a bug.
// Show the pair only when it actually says two things.
function runLabel(run) {
  return run.name && run.name !== run.id ? `${run.name} / ${run.id}` : run.id;
}

// The run logs /api/runs lists — the served log's siblings, or every log
// under --runs-dir (accrue-ui#38). Picking one asks the server to serve it.
function RunMenu({ close }) {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    loadRuns().then(setRuns);
  }, []);
  const pick = async (r) => {
    if (r.served) return close();
    const failed = await switchLog(r);
    if (failed) setError(failed);
    else close();
  };
  if (runs == null) return html`<div class="run-menu-note">Loading runs…</div>`;
  return html`<div class="run-menu">
    ${runs.map(
      (r) => html`<button
        class=${`run-menu-item${r.served ? " current" : ""}`}
        key=${r.path}
        title=${r.path}
        aria-current=${r.served ? "true" : undefined}
        onClick=${() => pick(r)}
      >
        <span class="run-menu-name">${runLabel(r)}</span>
        ${r.live && html`<span class="live-dot"></span>`}
      </button>`
    )}
    ${error && html`<div class="run-menu-note error">${error}</div>`}
  </div>`;
}

function RunChip({ run }) {
  return html`<${MenuButton}
    class="run-chip"
    title="Switch run log"
    width=${400}
    label=${html`${runLabel(run)} <${IconChevronDown} />`}
    render=${(close) => html`<${RunMenu} close=${close} />`}
  />`;
}

// Trigger the report download. Same-origin GET, so the HttpOnly launch-token
// cookie authenticates it (no header to attach); the endpoint answers with
// Content-Disposition: attachment, so the browser saves <run_id>.html rather
// than navigating away from the dashboard.
function exportReport() {
  const a = document.createElement("a");
  a.href = apiPath("/api/report");
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function TopBar({ snap }) {
  const run = snap.run;
  return html`<header class="topbar">
    <${Logo} />
    <span class="app-title">accrue watch</span>
    <${RunChip} run=${run} />
    <span class="spacer"></span>
    <button
      class="export-btn"
      onClick=${exportReport}
      title="Download a self-contained HTML report of this run"
    >
      Export report
    </button>
    ${run.live &&
    html`<span class="live-badge">
      <span class="live-dot pulse"></span>LIVE
    </span>`}
    <span class="elapsed">${fmtDuration(elapsedS.value)}</span>
  </header>`;
}

// Serve another log (accrue-ui#38): a run pinned in the old log means
// nothing in the new one, so follow its latest. The stream is closed across
// the swap and reopened after, like switchRun(). Resolves to null, or why
// the server refused.
async function switchLog(entry) {
  disconnect();
  try {
    const failed = await selectLog(entry.path);
    if (failed) return failed;
    selectedRun.value = null;
    await loadRun();
    return null;
  } finally {
    connect();
  }
}

// Pin the picked run — or follow the latest again — and reload: every fetch
// and the event stream are scoped to the selected run, so the stream is
// reopened for it rather than left delivering the old run's deltas.
async function switchRun(run) {
  disconnect();
  selectedRun.value = run.latest ? null : run.id;
  try {
    await loadRun();
  } finally {
    connect();
  }
}

// "2026-10-13T11:00:00Z" -> "2026-10-13 11:00 UTC"
function fmtStarted(iso) {
  const m = iso && String(iso).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]} UTC` : null;
}

function runOption(run) {
  const started = fmtStarted(run.started_at);
  return [run.id, started && `started ${started}`, run.latest && "latest"]
    .filter(Boolean)
    .join(" · ");
}

// A rerun pointed at the same output path appends to the same log file
// (accrue-ui#33). The server indexes each run on its own; say so, and let
// the reader switch between them. The latest is shown until they pick one.
function RunsBanner({ snap }) {
  const runs = snap.file_runs || [];
  if (runs.length < 2) return null;
  const onChange = (e) => {
    const run = runs.find((r) => r.id === e.target.value);
    if (run) switchRun(run);
  };
  return html`<div class="runs-banner" role="status">
    <span class="runs-banner-text">
      This file holds <strong>${runs.length} runs</strong>.
      <span class="dim">Totals, cells and errors are for the selected run only.</span>
    </span>
    <label class="runs-picker">
      Showing
      <select value=${snap.run.id} onChange=${onChange}>
        ${runs.map(
          (r) => html`<option key=${r.id} value=${r.id}>${runOption(r)}</option>`
        )}
      </select>
    </label>
  </div>`;
}

// Rendered wherever a number is genuinely unknown, never a bare blank and
// never a fabricated 0 (docs/api-shapes.md: these fields are nullable).
const DASH = "—";

// Spend is null because a model has no price, not because nothing was
// spent: say which (stats.unpriced) instead of the em-dash.
function unpricedValue(models) {
  const text = fmtUnpriced(models);
  return text && html`<span class="unpriced">${text}</span>`;
}

const FILTERS = [
  ["all", "All rows"],
  ["errors", "Errors only"],
  ["retrying", "Retrying"],
  ["skipped", "Skipped"],
];

function TabRow({ snap }) {
  const tab = activeTab.value;
  const mode = viewMode.value;
  const errors = snap.stats ? snap.stats.errors : 0;
  return html`<nav class="tabrow">
    <button
      class=${`tab${tab === "grid" ? " active" : ""}`}
      onClick=${() => (activeTab.value = "grid")}
    >
      Run grid
    </button>
    <button
      class=${`tab${tab === "overview" ? " active" : ""}`}
      onClick=${() => (activeTab.value = "overview")}
    >
      Overview
    </button>
    <button
      class=${`tab${tab === "errors" ? " active" : ""}`}
      onClick=${() => (activeTab.value = "errors")}
    >
      Errors
      ${errors > 0 && html`<span class="count-pill">${errors}</span>`}
    </button>
    <button
      class=${`tab${tab === "cost" ? " active" : ""}`}
      onClick=${() => (activeTab.value = "cost")}
    >
      Cost
    </button>
    ${tab === "grid" &&
    html`<div class="toolbar">
      <div class="segmented">
        <button
          class=${mode === "status" ? "active" : ""}
          onClick=${() => (viewMode.value = "status")}
        >
          Status
        </button>
        <button
          class=${mode === "data" ? "active" : ""}
          onClick=${() => (viewMode.value = "data")}
        >
          Data
        </button>
      </div>
      ${FILTERS.map(
        ([id, label]) => html`<button
          key=${id}
          class=${`chip${rowFilter.value === id ? " active" : ""}`}
          onClick=${() => (rowFilter.value = id)}
        >
          ${label}
        </button>`
      )}
      <${SearchBox} />
      ${mode === "data" && html`<${ColumnPickerButton} snap=${snap} />`}
      ${mode === "data" &&
      html`<button
        class=${`chip${wrapCells.value ? " active" : ""}`}
        title="Truncate values to one line, or wrap them to three"
        onClick=${() => (wrapCells.value = !wrapCells.value)}
      >
        ${wrapCells.value ? "Wrap" : "Truncate"}
      </button>`}
      ${mode === "data" &&
      html`<button
        class=${`chip${showInternalFields.value ? " active" : ""}`}
        onClick=${() => (showInternalFields.value = !showInternalFields.value)}
      >
        Internal fields: ${showInternalFields.value ? "shown" : "hidden"}
      </button>`}
    </div>`}
  </nav>`;
}

function StatTile({ label, tip, value, tone = "", bar = null }) {
  return html`<div class="stat-tile">
    <span class="tip" data-tip=${tip}>
      <span class="stat-label tip-label">${label}</span>
    </span>
    <div class=${`stat-value ${tone}`}>${value}</div>
    ${bar != null &&
    html`<div class="stat-bar">
      <span style=${{ width: `${Math.round(bar * 100)}%` }}></span>
    </div>`}
  </div>`;
}

// Throughput and Time remaining only exist while something is still being
// written: for a finished or abandoned run they would show stale arithmetic
// over a rate that stopped, so they are dropped rather than frozen.
function StatsStrip({ snap }) {
  const stats = snap.stats;
  const rows = snap.rows;
  const live = snap.run.live;
  const progress = rows.total ? rows.done / rows.total : 0;
  const throughput = stats.throughput_per_min;
  const saved = fmtMoney(stats.cache_saved);
  const unpriced = stats.unpriced || [];
  const spendTip = "Token spend so far, at list prices.";
  return html`<div class="stats-strip">
    <${StatTile}
      label="Rows enriched"
      tip="Rows complete through the final step."
      value=${html`${fmtInt(rows.done)} <span class="dim">/ ${fmtInt(rows.total)}</span>`}
      bar=${progress}
    />
    <${StatTile}
      label="Spend"
      tip=${unpriced.length ? `${spendTip} ${unpricedTip(unpriced)}` : spendTip}
      value=${fmtMoney(stats.spend) ?? unpricedValue(unpriced) ?? DASH}
    />
    <${StatTile}
      label="Cache hits"
      tip=${`${fmtPct(stats.cache_hit_rate)} of calls served from cache${saved ? ` — ${saved} saved so far this run` : ""}.`}
      value=${fmtPct(stats.cache_hit_rate)}
    />
    <${StatTile}
      label="Errors"
      tip="Cells that exhausted retries — triage them in the Errors tab."
      value=${fmtInt(stats.errors)}
      tone="red"
    />
    ${live &&
    html`<${StatTile}
      label="Throughput"
      tip="Completed cells per minute over the recent window."
      value=${throughput == null
        ? DASH
        : html`${fmtInt(Math.round(throughput))}<span class="dim">/min</span>`}
    />`}
    ${live &&
    html`<${StatTile}
      label="Time remaining"
      tip="Estimated from current throughput and the cells still pending."
      value=${stats.eta_s != null ? fmtDuration(stats.eta_s) : DASH}
    />`}
  </div>`;
}

// The launch token arrives as ?token=… and is exchanged for an HttpOnly
// cookie by the first page hit. Once the snapshot proves the cookie works,
// drop it from the address bar so it stops riding along in copy-pasted
// links, bookmarks and the browser's own history.
function scrubTokenFromUrl() {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("token")) return;
    url.searchParams.delete("token");
    const query = url.searchParams.toString();
    window.history.replaceState(
      null,
      "",
      `${url.pathname}${query ? `?${query}` : ""}${url.hash}`
    );
  } catch {
    // No history API (or an exotic URL): leaving it visible is not fatal.
  }
}

function App() {
  const [error, setError] = useState(null);

  useEffect(() => {
    // The hash first: a reload or a shared link restores the same search,
    // filters, sort and columns (accrue-ui#37), and the query it implies
    // fires alongside the snapshot fetch.
    readHash();
    loadRun()
      .then(() => {
        scrubTokenFromUrl();
        connect();
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape" && selection.value) closeInspector();
    };
    window.addEventListener("keydown", onKey);
    const tick = setInterval(() => {
      const snap = snapshot.value;
      if (snap && snap.run.live) elapsedS.value++;
    }, 1000);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearInterval(tick);
    };
  }, []);

  const snap = snapshot.value;
  if (error) {
    return html`<div class="boot">Failed to load run: ${error}</div>`;
  }
  if (!snap) {
    return html`<div class="boot">Loading run…</div>`;
  }
  const tab = activeTab.value;
  return html`<div class="app-root">
    <${TopBar} snap=${snap} />
    <${RunsBanner} snap=${snap} />
    <${TabRow} snap=${snap} />
    ${tab === "grid" && html`<${FilterBar} />`}
    <${StatsStrip} snap=${snap} />
    ${tab === "grid" && html`<${Grid} />`}
    ${tab === "overview" && html`<${Overview} />`}
    ${tab === "errors" && html`<${Triage} />`}
    ${tab === "cost" && html`<${Cost} />`}
    ${selection.value && html`<${Inspector} />`}
  </div>`;
}

// One global init activates `[data-tip]` tooltip delegation for the whole app.
initTooltips();

render(html`<${App} />`, document.getElementById("app"));
