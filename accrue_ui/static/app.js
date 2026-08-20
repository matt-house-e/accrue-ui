// accrue watch — app shell. Boot: fetch /api/run, then subscribe to SSE.
import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { html } from "./lib/html.js";
import { IconChevronDown, IconSearch } from "./lib/icons.js";
import { fmtDuration, fmtInt, fmtMoney, fmtPct } from "./lib/fmt.js";
import { connect } from "./lib/sse.js";
import {
  activeTab,
  closeInspector,
  elapsedS,
  loadRun,
  loadRuns,
  rowFilter,
  searchQuery,
  selection,
  showInternalFields,
  snapshot,
  viewMode,
} from "./lib/store.js";
import { Grid } from "./views/grid.js";
import { Inspector } from "./views/inspector.js";
import { Triage } from "./views/triage.js";
import { Cost } from "./views/cost.js";

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

function RunChip({ run }) {
  const [open, setOpen] = useState(false);
  const [runs, setRuns] = useState([]);
  const toggle = async () => {
    if (!open) setRuns(await loadRuns());
    setOpen(!open);
  };
  return html`<button class="run-chip" onClick=${toggle}>
    ${runLabel(run)}
    <${IconChevronDown} />
    ${open &&
    html`<div class="run-menu">
      ${runs.map(
        (r) => html`<span class="run-menu-item" key=${r.id}>
          ${runLabel(r)}
          ${r.live && html`<span class="live-dot"></span>`}
        </span>`
      )}
    </div>`}
  </button>`;
}

function TopBar({ snap }) {
  const run = snap.run;
  return html`<header class="topbar">
    <${Logo} />
    <span class="app-title">accrue watch</span>
    <${RunChip} run=${run} />
    <span class="spacer"></span>
    ${run.live &&
    html`<span class="live-badge">
      <span class="live-dot pulse"></span>LIVE
    </span>`}
    <span class="elapsed">${fmtDuration(elapsedS.value)}</span>
  </header>`;
}

// Rendered wherever a number is genuinely unknown, never a bare blank and
// never a fabricated 0 (docs/api-shapes.md: these fields are nullable).
const DASH = "—";

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
      <label class="search">
        <${IconSearch} />
        <input
          type="text"
          placeholder="Search loaded keys…"
          value=${searchQuery.value}
          onInput=${(e) => (searchQuery.value = e.target.value)}
        />
      </label>
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
  return html`<div class="stats-strip">
    <${StatTile}
      label="Rows enriched"
      tip="Rows complete through the final step."
      value=${html`${fmtInt(rows.done)} <span class="dim">/ ${fmtInt(rows.total)}</span>`}
      bar=${progress}
    />
    <${StatTile}
      label="Spend"
      tip="Token spend so far, at list prices."
      value=${fmtMoney(stats.spend) ?? DASH}
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
    <${TabRow} snap=${snap} />
    <${StatsStrip} snap=${snap} />
    ${tab === "grid" && html`<${Grid} />`}
    ${tab === "errors" && html`<${Triage} />`}
    ${tab === "cost" && html`<${Cost} />`}
    ${selection.value && html`<${Inspector} />`}
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
