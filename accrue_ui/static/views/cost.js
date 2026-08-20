// Cost breakdown: CSS-only bar charts (no chart libraries), plan-vs-actual
// table, stat tiles, and token counts.
import { html } from "../lib/html.js";
import { IconInfo } from "../lib/icons.js";
import { fmtInt, fmtMoney } from "../lib/fmt.js";
import { snapshot } from "../lib/store.js";

function BarChart({ title, entries }) {
  const sorted = [...entries].sort((a, b) => b[1] - a[1]);
  const max = Math.max(...sorted.map(([, v]) => v), 0.0001);
  return html`<div class="cost-card">
    <h3>${title}</h3>
    ${sorted.map(
      ([label, value]) => html`<div class="bar-row" key=${label}>
        <span class="bar-label">${label}</span>
        <span class="bar-track">
          <span
            class="bar-fill"
            style=${{ width: `${Math.max((value / max) * 100, 1)}%` }}
          ></span>
        </span>
        <span class="bar-value">${fmtMoney(value)}</span>
      </div>`
    )}
  </div>`;
}

function Tile({ label, value, tone = "", tip = null, info = false }) {
  if (value == null) return null;
  let head;
  if (tip && info) {
    // Plain label + info icon carrying the tooltip.
    head = html`${label}
      <span class="tip" data-tip=${tip}><${IconInfo} /></span>`;
  } else if (tip) {
    head = html`<span class="tip" data-tip=${tip}>
      <span class="tip-label">${label}</span>
    </span>`;
  } else {
    head = label;
  }
  return html`<div class="cost-tile">
    <div class="k">${head}</div>
    <div class=${`v ${tone}`}>${value}</div>
  </div>`;
}

export function Cost() {
  const snap = snapshot.value;
  const cost = snap.cost || {};
  const plan = cost.plan || null;
  const byStep = Object.entries(cost.by_step || {});
  const byModel = Object.entries(cost.by_model || {});
  const actual = snap.stats ? snap.stats.spend : null;
  const cacheSaved = snap.stats ? snap.stats.cache_saved : null;
  const vsPlan = plan && actual != null ? actual - plan.est_total : null;

  // Plan table in pipeline order.
  const planRows = plan
    ? snap.steps
        .filter((s) => plan.per_step[s.name] != null)
        .map((s) => {
          const p = plan.per_step[s.name];
          const a = (cost.by_step || {})[s.name] ?? 0;
          return [s.name, p, a, a - p];
        })
    : [];

  return html`<div class="cost">
    <div>
      <${BarChart} title="Cost by step" entries=${byStep} />
      <${BarChart} title="By model" entries=${byModel} />
      ${plan &&
      html`<div class="cost-card">
        <h3>Plan vs actual by step</h3>
        <table class="plan-table">
          <thead>
            <tr>
              <th>Step</th>
              <th>Plan</th>
              <th>Actual</th>
              <th>Δ</th>
            </tr>
          </thead>
          <tbody>
            ${planRows.map(
              ([name, p, a, delta]) => html`<tr key=${name}>
                <td>${name}</td>
                <td>${fmtMoney(p)}</td>
                <td>${fmtMoney(a)}</td>
                <td class=${delta < 0 ? "delta-neg" : "delta-pos"}>
                  ${fmtMoney(delta)}
                </td>
              </tr>`
            )}
          </tbody>
        </table>
        <div class="caveat">
          Plan is a pre-run estimate at list prices — no cache or batch
          discounts, and steps still running will close the gap.
        </div>
      </div>`}
    </div>
    <div>
      <div class="cost-tiles">
        <${Tile}
          label="Plan estimate"
          value=${plan ? fmtMoney(plan.est_total) : null}
          tip="Estimated before the run by pipeline.plan() from token counts at list prices."
        />
        <${Tile} label="Actual" value=${fmtMoney(actual)} />
        <${Tile}
          label="Vs plan"
          value=${vsPlan != null ? fmtMoney(vsPlan) : null}
          tone=${vsPlan != null && vsPlan < 0 ? "green" : "red"}
        />
        <${Tile} label="Saved by cache" value=${fmtMoney(cacheSaved)} tone="green" />
        <${Tile}
          label="Saved by batch"
          value=${fmtMoney(cost.batch_saved)}
          tone="green"
          info=${true}
          tip="Batch-mode steps run at the provider's discounted batch pricing; this is the difference vs live pricing."
        />
        <${Tile} label="Wasted" value=${fmtMoney(cost.wasted)} tone="red" tip="Spend on cells that ultimately failed." />
      </div>
      ${cost.tokens &&
      html`<div class="cost-card">
        <h3>Tokens</h3>
        <div class="token-rows">
          <div class="token-row">
            <span class="k">Input</span>
            <span class="v">${fmtInt(cost.tokens.input)}</span>
          </div>
          <div class="token-row">
            <span class="k">Output</span>
            <span class="v">${fmtInt(cost.tokens.output)}</span>
          </div>
          <div class="token-row">
            <span class="k">Cache read</span>
            <span class="v">${fmtInt(cost.tokens.cache_read)}</span>
          </div>
          <div class="token-row">
            <span class="k">Cache write</span>
            <span class="v">${fmtInt(cost.tokens.cache_write)}</span>
          </div>
        </div>
      </div>`}
    </div>
  </div>`;
}
