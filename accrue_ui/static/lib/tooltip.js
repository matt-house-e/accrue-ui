// Floating tooltip layer. One element, portaled to <body>, shared by every
// `[data-tip]` trigger via event delegation on `document` — so it works for
// Preact nodes that mount/unmount without re-binding, and (crucially) escapes
// any overflow/stacking ancestor that would clip a CSS-only `::after` tooltip
// (the grid scroller, the inspector slide-over, the app shell). Geometry is
// computed here from the trigger's rect; the visual style lives in
// `.tip-floating` in style.css.

const MARGIN = 8; // keep this far from every viewport edge
const GAP = 8; // distance between the trigger and the tooltip
const TIP_ID = "accrue-tooltip";

let tipEl = null; // the single portaled element (lazily created)
let current = null; // the trigger the tooltip is currently describing
let initialized = false;

function ensureEl() {
  if (tipEl) return tipEl;
  tipEl = document.createElement("div");
  tipEl.id = TIP_ID;
  tipEl.className = "tip-floating";
  tipEl.setAttribute("role", "tooltip");
  tipEl.setAttribute("aria-hidden", "true");
  document.body.appendChild(tipEl);
  return tipEl;
}

// The nearest ancestor that actually carries a non-empty tooltip. A null or
// empty `data-tip` (e.g. inspector's retry button when a retry is available)
// shows nothing.
function triggerFor(node) {
  if (!node || typeof node.closest !== "function") return null;
  const el = node.closest("[data-tip]");
  if (!el) return null;
  const text = el.getAttribute("data-tip");
  if (text == null || text === "") return null;
  return el;
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(v, Math.max(lo, hi)));
}

// Place the tooltip in fixed-viewport coordinates: below the trigger by
// default, flipped above when below would overflow the bottom and there is
// room above; horizontally centered on the trigger but clamped so it never
// crosses a viewport edge.
function place(trigger, tip) {
  const r = trigger.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;

  let top = r.bottom + GAP;
  let placement = "below";
  if (top + th + MARGIN > vh && r.top - GAP - th >= MARGIN) {
    top = r.top - GAP - th;
    placement = "above";
  }
  top = clamp(top, MARGIN, vh - th - MARGIN);

  let left = r.left + r.width / 2 - tw / 2;
  left = clamp(left, MARGIN, vw - tw - MARGIN);

  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
  tip.dataset.placement = placement;
}

function show(trigger) {
  const text = trigger.getAttribute("data-tip");
  if (text == null || text === "") return hide();
  const tip = ensureEl();
  tip.textContent = text; // textContent, not HTML: data-tip is untrusted
  // Measure and position while still transparent, then reveal — so the first
  // frame is already in the right place (no visible jump).
  place(trigger, tip);
  tip.classList.add("is-visible");
  tip.setAttribute("aria-hidden", "false");
  trigger.setAttribute("aria-describedby", TIP_ID);
  current = trigger;
}

function hide() {
  if (current) current.removeAttribute("aria-describedby");
  current = null;
  if (!tipEl) return;
  tipEl.classList.remove("is-visible");
  tipEl.setAttribute("aria-hidden", "true");
}

// Wire the delegation once. Idempotent, so importing/initialising twice (or
// on hot-reload) is harmless.
export function initTooltips() {
  if (initialized || typeof document === "undefined") return;
  initialized = true;

  document.addEventListener("mouseover", (e) => {
    const trigger = triggerFor(e.target);
    if (trigger && trigger !== current) show(trigger);
  });
  document.addEventListener("mouseout", (e) => {
    if (!current) return;
    // Ignore moves that stay inside the current trigger (child to child).
    const to = e.relatedTarget;
    if (to && current.contains(to)) return;
    hide();
  });

  // Keyboard: focusing a trigger shows it, blurring it hides it.
  document.addEventListener("focusin", (e) => {
    const trigger = triggerFor(e.target);
    if (trigger) show(trigger);
    else if (current) hide();
  });
  document.addEventListener("focusout", (e) => {
    const trigger = triggerFor(e.target);
    if (trigger && trigger === current) hide();
  });

  // A stale tooltip must never float over the wrong place: any scroll (in any
  // container — hence capture) or a resize dismisses it.
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
}
