// Anchored popover for header menus and the column picker. Positioned
// `fixed` from the trigger's rect (like the tooltip layer), so it escapes the
// grid scroller's overflow clipping; closes on an outside mousedown, Escape,
// or any scroll — a menu must never float over the wrong column.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "./html.js";

const MARGIN = 8;

export function Popover({ anchor, onClose, width = 260, children }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !anchor) return;
    const r = anchor.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const h = el.offsetHeight;
    let top = r.bottom + 6;
    if (top + h + MARGIN > vh && r.top - 6 - h >= MARGIN) top = r.top - 6 - h;
    let left = r.left;
    if (left + width + MARGIN > vw) left = Math.max(MARGIN, vw - width - MARGIN);
    setPos({ top: Math.round(top), left: Math.round(left) });

    const onDown = (e) => {
      if (el.contains(e.target) || anchor.contains(e.target)) return;
      onClose();
    };
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    const onScroll = (e) => {
      if (el.contains(e.target)) return; // the popover's own list may scroll
      onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onClose);
    };
  }, [anchor]);

  return html`<div
    class=${`popover${pos ? " placed" : ""}`}
    ref=${ref}
    role="dialog"
    style=${{ width: `${width}px`, top: pos ? `${pos.top}px` : 0, left: pos ? `${pos.left}px` : 0 }}
    onMouseDown=${(e) => e.stopPropagation()}
  >
    ${children}
  </div>`;
}

// A button that owns one popover: `render(close)` builds its body.
export function MenuButton({ class: cls = "", title, label, active = false, render }) {
  const [open, setOpen] = useState(false);
  const btn = useRef(null);
  const close = () => setOpen(false);
  return html`<span class="menu-anchor">
    <button
      ref=${btn}
      class=${`${cls}${active ? " active" : ""}${open ? " open" : ""}`}
      title=${title}
      aria-haspopup="dialog"
      aria-expanded=${open}
      onClick=${(e) => {
        e.stopPropagation();
        setOpen(!open);
      }}
    >
      ${label}
    </button>
    ${open && html`<${Popover} anchor=${btn.current} onClose=${close}>${render(close)}<//>`}
  </span>`;
}
