// Overview: the run's pipeline blueprint (steps + types + models + params +
// produced fields) and the enrichment-field schema, read from the run log's
// pipeline_start.manifest (server: overview_block in index.py). Read-only —
// accrue-ui observes a run, it never configures the pipeline. A log without a
// manifest (older/metadata runs) degrades to what steps[] alone reveals.
import { Fragment } from "preact";
import { html } from "../lib/html.js";
import { fmtClock, fmtInt, fmtMoney, fmtPct } from "../lib/fmt.js";
import { snapshot } from "../lib/store.js";

const DASH = "—";

// "LLMStep" -> "LLM", "FunctionStep" -> "fn"; anything else renders plainly,
// minus a trailing "Step" (an unintrospectable type stays readable, not blank).
const TYPE_LABEL = { LLMStep: "LLM", FunctionStep: "fn" };
function typeTag(type) {
  if (TYPE_LABEL[type]) return TYPE_LABEL[type];
  return String(type || "").replace(/Step$/, "") || "step";
}

// Model params as the mock shows them: "temp 0.2 · max 4000". A part the
// manifest left null is dropped rather than shown as "temp null".
function modelParams(model) {
  const parts = [];
  if (model.temperature != null) parts.push(`temp ${model.temperature}`);
  if (model.max_tokens != null) parts.push(`max ${fmtInt(model.max_tokens)}`);
  return parts.join(" · ");
}

// Field-type badge palette by the manifest's declared type: enum -> violet,
// int/float -> jade, internal -> dashed-muted, everything else (str, and an
// unintrospectable "unknown") -> the neutral chip so it still shows.
function typeClass(f) {
  if (f.internal) return "internal";
  if (f.type === "enum") return "enum";
  if (f.type === "int" || f.type === "float" || f.type === "number") return "num";
  return "str";
}

// Rail-node state for one step from its live outcome. `done` counts every
// settled cell (ok/cached/error/skipped); a step still filling in pulses only
// while the run itself is live, so a cold interrupted run doesn't fake motion.
function nodeState(o, live) {
  if (!o || !o.total) return "pending";
  if (o.done >= o.total) return "done";
  if (o.done > 0) return live ? "running" : "partial";
  return "pending";
}

function Metric({ value, label, tip = null, tone = "" }) {
  return html`<div class="ov-metric">
    <div class=${`ov-metric-v mono ${tone}`}>${value}</div>
    <div class="ov-metric-k">
      ${tip
        ? html`<span class="tip" data-tip=${tip}
            ><span class="tip-label">${label}</span></span
          >`
        : label}
    </div>
  </div>`;
}

function Summary({ snap, ov }) {
  const run = snap.run;
  const stats = snap.stats || {};
  const cfg = ov.config || {};
  const live = run.live;
  const workers = cfg.max_workers;
  const hit = stats.cache_hit_rate;
  const caching = cfg.caching
    ? hit
      ? html`on <span class="ov-metric-sub">· ${fmtPct(hit)}</span>`
      : "on"
    : "off";
  return html`<div class="ov-summary">
    <div class="ov-run">
      <div class="ov-run-title">
        <span class="ov-run-name">${run.name || run.id}</span>
        ${live
          ? html`<span class="ov-pill live"
              ><span class="live-dot pulse"></span>running</span
            >`
          : html`<span class="ov-pill done">finished</span>`}
      </div>
      <div class="ov-run-meta mono">
        run ${run.id}${run.started_at ? ` · started ${fmtClock(run.started_at)}` : ""}
        · ${fmtInt(ov.sample_size)} rows
      </div>
    </div>
    <div class="ov-divider"></div>
    <${Metric}
      value=${workers != null ? fmtInt(workers) : DASH}
      label="Workers"
      tip="max_workers — the enrichment pipeline's concurrency for this run."
    />
    <${Metric}
      value=${caching}
      label="Caching"
      tip="Response caching, and the share of calls served from cache so far."
    />
    <${Metric}
      value=${cfg.checkpointing ? "on" : "off"}
      label="Checkpointing"
      tip="Whether partial results were checkpointed so the run can resume."
    />
    <${Metric}
      value=${cfg.capture || DASH}
      label="Capture"
      tip="Prompt/response capture tier: metadata, prompts, or full."
    />
    <${Metric}
      value=${cfg.batch ? "on" : "off"}
      label="Batch"
      tone=${cfg.batch ? "violet" : ""}
      tip="Whether the run used a provider batch API (discounted, higher latency)."
    />
    <span class="ov-spring"></span>
    <div class="ov-versions mono">
      <div>accrue v${ov.accrue_version || "?"}</div>
      <div class="ov-providers">
        ${ov.providers.length ? ov.providers.join(" · ") : "no model providers"}
      </div>
    </div>
  </div>`;
}

function StepCard({ step, internalSet }) {
  const o = step.outcome || {};
  const model = step.model;
  const ok = Math.max(0, (o.done || 0) - (o.errors || 0));
  const cond = step.condition;
  return html`<div class="ov-card">
    <div class="ov-card-head">
      <span class="ov-level">L${step.level}</span>
      <span class="ov-name">${step.name}</span>
      ${step.type && html`<span class="ov-tag">${typeTag(step.type)}</span>`}
      ${step.mode === "batch" && html`<span class="ov-tag batch">batch</span>`}
      ${cond != null &&
      html`<span class="ov-tag conditional">conditional</span>
        ${typeof cond === "string" &&
        html`<span class="ov-cond mono">${cond}</span>`}`}
      <span class="ov-spring"></span>
      <span class="ov-ok mono">
        ${fmtInt(ok)} ok${o.errors > 0
          ? html` <span class="err">· ${fmtInt(o.errors)} err</span>`
          : ""}
      </span>
      <span class="ov-cost mono">${fmtMoney(o.cost) ?? DASH}</span>
    </div>
    <div class="ov-card-body">
      ${model
        ? html`<span class="ov-model mono">${model.id}</span>
            ${modelParams(model) &&
            html`<span class="ov-params mono">${modelParams(model)}</span>`}`
        : html`<span class="ov-model fn mono">function</span>`}
      ${step.produces.length > 0 && html`<span class="ov-arrow">→</span>`}
      ${step.produces.map(
        (p) => html`<span
          key=${p}
          class=${`ov-produces mono${
            p.startsWith("__") || internalSet.has(p) ? " internal" : ""
          }`}
          >${p}</span
        >`
      )}
    </div>
  </div>`;
}

function Pipeline({ steps, live, internalSet }) {
  const levels = new Set(steps.map((s) => s.level)).size;
  const nSteps = steps.length;
  return html`<div class="ov-panel">
    <div class="ov-panel-head">
      <div class="ov-panel-title">Pipeline</div>
      <div class="ov-panel-sub mono">
        ${nSteps} ${nSteps === 1 ? "step" : "steps"} ·
        ${levels} ${levels === 1 ? "level" : "levels"}
      </div>
    </div>
    ${nSteps === 0
      ? html`<div class="ov-empty">No steps recorded in this run log.</div>`
      : html`<div class="ov-flow">
          ${steps.map(
            (step, i) => html`<${Fragment} key=${step.name}>
              <div class="ov-rail">
                <span class=${`ov-node ${nodeState(step.outcome, live)}`}></span>
                ${i < nSteps - 1 && html`<span class="ov-line"></span>`}
              </div>
              <${StepCard} step=${step} internalSet=${internalSet} />
            </${Fragment}>`
          )}
        </div>`}
  </div>`;
}

function Fields({ fields }) {
  const outputs = fields.filter((f) => !f.internal).length;
  const internal = fields.length - outputs;
  return html`<div class="ov-panel">
    <div class="ov-panel-head">
      <div class="ov-panel-title">Enrichment fields</div>
      <div class="ov-panel-sub mono">
        ${outputs} output · ${internal} internal
      </div>
    </div>
    ${fields.length === 0
      ? html`<div class="ov-empty">No enrichment fields recorded.</div>`
      : html`<div class="ov-table">
          <div class="ov-thead mono">
            <div>Field</div>
            <div>Type</div>
            <div>From</div>
          </div>
          ${fields.map((f) => {
            const tip = [
              f.description,
              f.enum ? `options: ${f.enum.join(", ")}` : null,
            ]
              .filter(Boolean)
              .join("\n");
            return html`<div
              key=${f.name}
              class=${`ov-trow mono${f.internal ? " internal" : ""}`}
              data-tip=${tip}
            >
              <div class="ov-fname">
                ${tip
                  ? html`<span class="tip-label">${f.name}</span>`
                  : f.name}
              </div>
              <div>
                <span class=${`ov-type ${typeClass(f)}`}
                  >${f.internal ? "internal" : f.type}</span
                >
              </div>
              <div class="ov-from">${f.step ?? ""}</div>
            </div>`;
          })}
        </div>`}
  </div>`;
}

// Degrade a manifest-less snapshot to overview-shaped steps/fields from what
// steps[] alone gives: names, levels, modes, the model id string, live
// counters, and the output field names observed in row values. Types, params,
// dependencies, conditions and the field schema are simply unavailable.
function degradedSteps(snap) {
  const byStep = (snap.cost && snap.cost.by_step) || {};
  return (snap.steps || []).map((s) => ({
    name: s.name,
    type: null,
    model: s.model ? { id: s.model } : null,
    produces: s.fields || [],
    depends_on: [],
    condition: null,
    level: s.level,
    mode: s.mode,
    outcome: {
      done: s.done,
      total: s.total,
      errors: s.errors,
      cost: byStep[s.name] ?? null,
    },
  }));
}

function degradedFields(snap) {
  const out = [];
  for (const s of snap.steps || []) {
    for (const name of s.fields || []) {
      out.push({
        name,
        type: "unknown",
        enum: null,
        description: null,
        step: s.name,
        internal: name.startsWith("__"),
      });
    }
  }
  return out;
}

function EmptyState({ snap }) {
  const steps = degradedSteps(snap);
  const fields = degradedFields(snap);
  const internalSet = new Set(
    fields.filter((f) => f.internal).map((f) => f.name)
  );
  return html`<div class="overview">
    <div class="ov-hint">
      <div class="ov-hint-title">No pipeline manifest in this run log</div>
      <div class="ov-hint-body">
        This run has no${" "}
        <span class="mono">pipeline_start.manifest</span>${" "}record — it was
        written by an older accrue, or with a metadata-only capture tier — so the
        full pipeline blueprint and enrichment-field schema aren't available.
        Below is what the log's${" "}
        <span class="mono">steps[]</span>${" "}alone reveals.
      </div>
    </div>
    <div class="ov-body">
      <${Pipeline} steps=${steps} live=${snap.run.live} internalSet=${internalSet} />
      <${Fields} fields=${fields} />
    </div>
  </div>`;
}

export function Overview() {
  const snap = snapshot.value;
  const ov = snap.overview;
  if (!ov || !ov.present) {
    return html`<${EmptyState} snap=${snap} />`;
  }
  const internalSet = new Set(
    ov.fields.filter((f) => f.internal).map((f) => f.name)
  );
  return html`<div class="overview">
    <${Summary} snap=${snap} ov=${ov} />
    <div class="ov-body">
      <${Pipeline}
        steps=${ov.steps}
        live=${snap.run.live}
        internalSet=${internalSet}
      />
      <${Fields} fields=${ov.fields} />
    </div>
    <div class="ov-foot mono">
      source: pipeline_start manifest · the definition is read from the run log —
      nothing to configure here
    </div>
  </div>`;
}
