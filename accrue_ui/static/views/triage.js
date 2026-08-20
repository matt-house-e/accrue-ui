// Failure triage: error groups with burst histograms and retry affordances.
import { useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { IconBulb } from "../lib/icons.js";
import { fmtClock, fmtInt, fmtMoney } from "../lib/fmt.js";
import {
  errorGroupsStale,
  postRetry,
  retryBusy,
  retryError,
  snapshot,
} from "../lib/store.js";

function rangeLabel(rows) {
  if (!rows || !rows.length) return "";
  const lo = rows[0][0];
  const hi = rows[rows.length - 1][1];
  return lo === hi ? `row ${lo}` : `rows ${lo}–${hi}`;
}

function Histogram({ buckets }) {
  const max = Math.max(1, ...buckets);
  return html`<div class="histogram" role="img" aria-label="Error burst histogram">
    ${buckets.map(
      (b, i) => html`<span
        key=${i}
        style=${{ height: `${Math.round((b / max) * 36)}px` }}
      ></span>`
    )}
  </div>`;
}

// Inline failure notice: quiet red, next to the button that asked.
export function RetryNote({ source }) {
  const err = retryError.value;
  if (!err || (err.source !== source && err.source !== "*")) return null;
  return html`<span class="retry-note mono">${err.message}</span>`;
}

function RetryButton({ count, retry, body, source, primary = false }) {
  const busy = retryBusy();
  const label = primary
    ? `Retry all ${fmtInt(count)} failed rows`
    : `Retry ${fmtInt(count)} rows`;
  const btn = html`<button
    class=${`btn ${primary ? "btn-primary" : "btn-secondary"}`}
    disabled=${!retry.available || busy}
    onClick=${(e) => {
      e.stopPropagation();
      postRetry(body, source);
    }}
  >
    ${busy ? "Retrying…" : label}
  </button>`;
  const wrap = primary
    ? { marginLeft: "auto" }
    : { marginLeft: "auto", flex: "none" };
  if (retry.available) {
    return html`<span style=${wrap}><${RetryNote} source=${source} />${btn}</span>`;
  }
  return html`<span class="tip" data-tip=${retry.reason} style=${wrap}>
    ${btn}
  </span>`;
}

function GroupCard({ group, expanded, onToggle, retry }) {
  return html`<div class="group-card">
    <button class="group-head" onClick=${onToggle}>
      <span class="count-pill">${group.count}</span>
      <span class="group-title">
        <span class="mono">${group.step}</span> ·${" "}
        <span class="mono">${group.type}</span>
        <div class="group-sub">
          ${rangeLabel(group.rows)} · first ${fmtClock(group.first_t, false)} ·
          last ${fmtClock(group.last_t, false)}
        </div>
      </span>
      <${RetryButton}
        count=${group.count}
        retry=${retry}
        body=${{ group: { step: group.step, type: group.type } }}
        source=${`${group.step}:${group.type}`}
      />
    </button>
    ${expanded &&
    html`<div class="group-body">
      <${Histogram} buckets=${group.histogram || []} />
      <div class="hist-labels">
        <span>${fmtClock(group.first_t)}</span>
        <span>${fmtClock(group.last_t)}</span>
      </div>
      <div class="error-msg">${`“${group.message}”`}</div>
      <div class="range-chips">
        ${(group.rows || []).map(
          ([lo, hi]) => html`<span class="range-chip" key=${lo}>
            ${lo === hi ? lo : `${lo}–${hi}`}
          </span>`
        )}
      </div>
      ${group.hint &&
      html`<div class="hint-line"><${IconBulb} /> ${group.hint}</div>`}
    </div>`}
  </div>`;
}

export function Triage() {
  const snap = snapshot.value;
  const [expanded, setExpanded] = useState(0);
  const groups = snap.error_groups || [];
  const retry = snap.retry || { available: false, reason: null };
  // New failures arrived by SSE (which carries counters, not groups) and the
  // snapshot refetch that will bring their groups is still in flight.
  const stale = errorGroupsStale.value;

  if (!groups.length) {
    return html`<div class="triage" aria-busy=${stale}>
      <div class="empty-state">No failures — nothing to triage.</div>
    </div>`;
  }

  const totalFailed = groups.reduce((acc, g) => acc + g.count, 0);
  const firstT = groups.map((g) => g.first_t).sort()[0];
  const lastT = groups.map((g) => g.last_t).sort().slice(-1)[0];

  // Estimated cost of re-running the failed cells, from per-step actuals.
  const byStep = (snap.cost && snap.cost.by_step) || {};
  let est = 0;
  for (const g of groups) {
    const step = snap.steps.find((s) => s.name === g.step);
    const spent = byStep[g.step];
    if (step && step.done > 0 && spent != null) {
      est += (spent / step.done) * g.count;
    }
  }

  return html`<div class="triage" aria-busy=${stale}>
    <div class="triage-summary">
      <span class="mono">${fmtInt(totalFailed)}</span> failed across${" "}
      <span class="mono">${groups.length}</span> causes ·${" "}
      <span class="mono">${fmtClock(firstT, false)}–${fmtClock(lastT, false)}</span>
    </div>
    ${groups.map(
      (g, i) => html`<${GroupCard}
        key=${`${g.step}:${g.type}`}
        group=${g}
        expanded=${expanded === i}
        onToggle=${() => setExpanded(expanded === i ? -1 : i)}
        retry=${retry}
      />`
    )}
    <div class="triage-foot">
      <span class="note">
        Resume re-runs only failed cells — cache intact · est${" "}
        <span class="mono">${fmtMoney(Math.max(est, 0.01))}</span>
      </span>
      <${RetryButton}
        count=${totalFailed}
        retry=${retry}
        body=${{ all: true }}
        source="all"
        primary=${true}
      />
    </div>
  </div>`;
}
