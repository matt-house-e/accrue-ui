// Cell inspector: right slide-over with detail from /api/cell/{step}/{row}.
import { useState } from "preact/hooks";
import { html } from "../lib/html.js";
import { IconClose, IconCopy } from "../lib/icons.js";
import { fmtClock, fmtInt, fmtLatency, fmtMoney } from "../lib/fmt.js";
import {
  cellDetail,
  cellDetailLoading,
  closeInspector,
  selection,
  showInternalFields,
  snapshot,
} from "../lib/store.js";

const STATUS_BYTE = {
  pending: 0,
  running: 1,
  ok: 2,
  cached: 3,
  retrying: 4,
  error: 5,
  skipped: 6,
};

function bannerText(d) {
  switch (d.status) {
    case "error": {
      const n = d.attempts ? d.attempts.length : 0;
      const type = d.error ? d.error.type : "error";
      return n ? `Failed after ${n} attempts — ${type}` : `Failed — ${type}`;
    }
    case "cached":
      return "Served from cache";
    case "ok":
      return "Completed";
    case "running":
      return "Running";
    case "retrying": {
      const n = d.attempts ? d.attempts.length : 0;
      return n ? `Retrying — attempt ${n + 1} pending` : "Retrying";
    }
    case "skipped":
      return "Skipped by condition";
    default:
      return "Pending";
  }
}

function kvRows(d) {
  const rows = [];
  const lastAttempt = d.attempts ? d.attempts[d.attempts.length - 1] : null;
  if (lastAttempt && lastAttempt.latency_ms != null) {
    rows.push(["Latency", fmtLatency(lastAttempt.latency_ms)]);
  }
  if (d.usage) {
    rows.push(["Tokens", `${fmtInt(d.usage.in)} in · ${fmtInt(d.usage.out)} out`]);
    if (d.usage.cost != null) rows.push(["Cost", fmtMoney(d.usage.cost)]);
  }
  if (typeof d.from_cache === "boolean") {
    rows.push(["Cache", d.from_cache ? "hit" : "miss"]);
  }
  if (d.queued_at) rows.push(["Queued", fmtClock(d.queued_at)]);
  if (d.elapsed_ms != null) rows.push(["Elapsed", fmtLatency(d.elapsed_ms)]);
  return rows;
}

function AttemptRow({ attempt, isLast, failed }) {
  const finalFail = isLast && failed;
  return html`<div class=${`attempt${finalFail ? " final-fail" : ""}`}>
    <span class="kind-chip">${attempt.kind}</span>
    <span class="n">#${attempt.n}</span>
    <span>${fmtClock(attempt.at)}</span>
    <span>${fmtLatency(attempt.latency_ms)}</span>
    <span class=${`a-status ${attempt.status === "ok" ? "ok" : "err"}`}>
      ${attempt.status}
    </span>
    ${attempt.backoff_s != null &&
    html`<span class="backoff">backoff ${attempt.backoff_s}s</span>`}
  </div>`;
}

function ValuesTable({ values }) {
  if (!values) {
    return html`<div class="empty-state">
      No values yet — this cell has not completed.
    </div>`;
  }
  const showInternal = showInternalFields.value;
  const entries = Object.entries(values).filter(
    ([k]) => showInternal || !k.startsWith("__")
  );
  if (!entries.length) {
    return html`<div class="empty-state">No visible fields.</div>`;
  }
  return html`<table class="values-table">
    <tbody>
      ${entries.map(
        ([k, v]) => html`<tr key=${k}>
          <td class="field">${k}</td>
          <td class="value">
            ${typeof v === "string" ? v : JSON.stringify(v, null, 2)}
          </td>
        </tr>`
      )}
    </tbody>
  </table>`;
}

export function Inspector() {
  const sel = selection.value;
  const snap = snapshot.value;
  const d = cellDetail.value;
  const loading = cellDetailLoading.value;
  const [tab, setTab] = useState("prompt");
  const [copied, setCopied] = useState(false);
  if (!sel || !snap) return null;

  const step = snap.steps.find((s) => s.name === sel.step);
  const retry = snap.retry || { available: false, reason: null };

  const copyResume = () => {
    if (!retry.resume_command || !navigator.clipboard) return;
    navigator.clipboard.writeText(retry.resume_command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  let body;
  if (loading || !d) {
    body = html`<div class="empty-state">
      ${loading ? "Loading cell detail…" : "No detail available for this cell."}
    </div>`;
  } else {
    const sByte = STATUS_BYTE[d.status] ?? 0;
    const kv = kvRows(d);
    body = html`
      <div class=${`insp-banner s${sByte}`}>
        ${bannerText(d)}
        ${d.error && html`<span class="mono">· ${d.error.msg}</span>`}
      </div>
      ${kv.length > 0 &&
      html`<div class="kv-grid">
        ${kv.map(
          ([k, v]) => html`<div class="kv" key=${k}>
            <div class="k">${k}</div>
            <div class="v" title=${v}>${v}</div>
          </div>`
        )}
      </div>`}
      ${d.attempts &&
      d.attempts.length > 0 &&
      html`<div>
        <div class="insp-section-title">Attempts</div>
        ${d.attempts.map(
          (a, i) => html`<${AttemptRow}
            key=${a.n}
            attempt=${a}
            isLast=${i === d.attempts.length - 1}
            failed=${d.status === "error"}
          />`
        )}
      </div>`}
      <div class="insp-tabs">
        ${[
          ["prompt", "Prompt & response"],
          ["values", "Row values"],
          ["raw", "Raw events"],
        ].map(
          ([id, label]) => html`<button
            key=${id}
            class=${`insp-tab${tab === id ? " active" : ""}`}
            onClick=${() => setTab(id)}
          >
            ${label}
          </button>`
        )}
      </div>
      ${tab === "prompt" &&
      html`<div class="empty-state">
        Capture is off — prompt/response capture lands in v0.2
      </div>`}
      ${tab === "values" && html`<${ValuesTable} values=${d.values} />`}
      ${tab === "raw" &&
      html`<pre class="raw-json">${JSON.stringify(d.raw_events, null, 2)}</pre>`}
    `;
  }

  return html`
    <div class="scrim" onClick=${closeInspector}></div>
    <aside class="inspector" role="dialog" aria-label="Cell detail">
      <div class="insp-head">
        <div>
          <div class="insp-title">
            ${sel.row} · ${d && d.key ? d.key : sel.step}
          </div>
          <div class="insp-sub">
            Step ${sel.step}
            ${step && html` · L${step.level} · ${step.mode} · ${step.model}`}
          </div>
        </div>
        <button class="insp-close" onClick=${closeInspector} aria-label="Close">
          <${IconClose} />
        </button>
      </div>
      <div class="insp-body">${body}</div>
      <div class="insp-foot">
        <span
          class=${retry.available ? "" : "tip"}
          data-tip=${retry.available ? null : retry.reason}
        >
          <button class="btn btn-primary" disabled=${!retry.available}>
            Retry this row
          </button>
        </span>
        <button class="btn btn-secondary" onClick=${copyResume}>
          <${IconCopy} />
          ${copied ? "Copied" : "Copy resume command"}
        </button>
      </div>
    </aside>
  `;
}
