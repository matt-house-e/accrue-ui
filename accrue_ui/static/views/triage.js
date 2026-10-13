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

// A retry of errors that will fail again takes two clicks: the first arms
// the button with the warning, the second sends. Armed state is the source
// string, so moving to another group or row disarms it, and so does leaving
// the button.
export function useRetryConfirm() {
  const [armed, setArmed] = useState(null);
  return {
    isArmed: (source) => armed === source,
    click(warn, source, send) {
      if (warn && armed !== source) {
        setArmed(source);
        return;
      }
      setArmed(null);
      send();
    },
    disarm: () => setArmed(null),
  };
}

// Inline failure notice: quiet red, next to the button that asked.
export function RetryNote({ source }) {
  const err = retryError.value;
  if (!err || (err.source !== source && err.source !== "*")) return null;
  return html`<span class="retry-note mono">${err.message}</span>`;
}

function RetryButton({
  count,
  retry,
  body,
  source,
  primary = false,
  warn = false,
}) {
  const busy = retryBusy();
  const confirm = useRetryConfirm();
  const armed = warn && confirm.isArmed(source);
  let label = primary
    ? `Retry all ${fmtInt(count)} failed rows`
    : `Retry ${fmtInt(count)} rows`;
  if (armed) {
    label = primary
      ? "Some fail again — retry anyway?"
      : "Fails again — retry anyway?";
  }
  const kind = armed ? "btn-warn" : primary ? "btn-primary" : "btn-secondary";
  const btn = html`<button
    class=${`btn ${kind}`}
    disabled=${!retry.available || busy}
    onClick=${(e) => {
      e.stopPropagation();
      confirm.click(warn, source, () => postRetry(body, source));
    }}
    onMouseLeave=${confirm.disarm}
    onBlur=${confirm.disarm}
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
  const failsAgain = group.retryable === false;
  return html`<div class="group-card">
    <button class="group-head" onClick=${onToggle}>
      <span class="count-pill">${group.count}</span>
      <span class="group-title">
        <span class="mono">${group.step}</span> ·${" "}
        <span class="mono">${group.type}</span>
        ${failsAgain &&
        html`<span
          class="cause-tag tip"
          data-tip="Not transient: a retry re-sends the identical request"
          ><span class="tip-label">fails again</span></span
        >`}
        <div class="group-msg">${group.message}</div>
        <div class="group-sub">
          ${rangeLabel(group.rows)} · first ${fmtClock(group.first_t, false)} ·
          last ${fmtClock(group.last_t, false)}
        </div>
      </span>
      <${RetryButton}
        count=${group.count}
        retry=${retry}
        body=${{ group: { step: group.step, type: group.type, key: group.key } }}
        source=${`${group.step}:${group.type}:${group.key}`}
        warn=${failsAgain}
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
        key=${`${g.step}:${g.type}:${g.key}`}
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
        warn=${groups.some((g) => g.retryable === false)}
      />
    </div>
  </div>`;
}
