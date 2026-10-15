// Data-grid column model (accrue-ui#37): one column per field, grouped under
// its step, plus the column picker that hides and shows them.
import { html } from "../lib/html.js";
import { MenuButton } from "../lib/popover.js";
import { colId, hiddenCols, showInternalFields, toggleColumn } from "../lib/store.js";

// A step's fields in column order: the manifest's declared `produces` first
// (so every column exists from the first frame of a live run, before any row
// has produced it), then anything the rows actually carried that the
// manifest did not name (internal `__` fields, a field added since).
export function fieldsOf(snap, step) {
  const produced = step.fields || [];
  const ov = snap.overview;
  const spec = ov && ov.present ? ov.steps.find((s) => s.name === step.name) : null;
  const declared = spec ? spec.produces || [] : [];
  const all = [...declared, ...produced.filter((f) => !declared.includes(f))];
  return showInternalFields.value ? all : all.filter((f) => !f.startsWith("__"));
}

// Every column the data grid lays out, left to right. A step whose fields
// are all hidden (or that has none yet) keeps one state column, so the step
// band still has something to span and its cells still show their state.
export function dataColumns(snap) {
  const hidden = new Set(hiddenCols.value);
  const cols = [];
  snap.steps.forEach((step, stepIndex) => {
    const fields = fieldsOf(snap, step).filter((f) => !hidden.has(colId(step.name, f)));
    if (fields.length === 0) {
      cols.push({ id: step.name, step, stepIndex, field: null });
    }
    for (const field of fields) {
      cols.push({ id: colId(step.name, field), step, stepIndex, field });
    }
  });
  return cols;
}

function PickerBody({ snap, close }) {
  const hidden = hiddenCols.value;
  const all = snap.steps.flatMap((step) =>
    fieldsOf(snap, step).map((f) => colId(step.name, f))
  );
  const hiddenHere = all.filter((id) => hidden.includes(id)).length;
  return html`<div class="picker">
    <div class="popover-head">
      <span>Columns</span>
      ${hiddenHere > 0 &&
      html`<button
        class="link-btn"
        onClick=${() => {
          hiddenCols.value = hidden.filter((id) => !all.includes(id));
          close();
        }}
      >
        Show all (${hiddenHere} hidden)
      </button>`}
    </div>
    ${snap.steps.map(
      (step) => html`<div class="picker-step" key=${step.name}>
        <div class="picker-step-name">${step.name}</div>
        ${fieldsOf(snap, step).map((f) => {
          const id = colId(step.name, f);
          return html`<label class="picker-field" key=${id}>
            <input
              type="checkbox"
              checked=${!hidden.includes(id)}
              onChange=${() => toggleColumn(id)}
            />
            <span class="mono">${f}</span>
          </label>`;
        })}
      </div>`
    )}
  </div>`;
}

export function ColumnPickerButton({ snap }) {
  const hidden = hiddenCols.value.length;
  return html`<${MenuButton}
    class="chip"
    title="Show or hide columns"
    active=${hidden > 0}
    label=${hidden > 0 ? `Columns · ${hidden} hidden` : "Columns"}
    render=${(close) => html`<${PickerBody} snap=${snap} close=${close} />`}
  />`;
}
