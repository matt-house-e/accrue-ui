// Inline SVG icons — no emoji, no icon fonts. All stroke/fill from
// currentColor so state classes control the color.
import { html } from "./html.js";

const svg = (size, body, extra = "") => html`
  <svg
    width=${size}
    height=${size}
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    class=${extra}
  >${body}</svg>`;

export const IconCheck = ({ size = 14 }) =>
  svg(size, html`<path d="M3 8.5 6.5 12 13 4.5" />`);

export const IconBolt = ({ size = 14 }) =>
  svg(
    size,
    html`<path d="M8.8 1.5 3.5 9h3.2l-.8 5.5L11.5 7H8.3l.5-5.5Z" fill="currentColor" stroke="none" />`
  );

export const IconRotate = ({ size = 14 }) =>
  svg(size, html`<path d="M13 8a5 5 0 1 1-1.7-3.75" /><path d="M13 1.5V5h-3.5" stroke-width="1.6" />`);

export const IconX = ({ size = 14 }) =>
  svg(size, html`<path d="M4 4l8 8M12 4l-8 8" />`);

export const IconMinus = ({ size = 14 }) =>
  svg(size, html`<path d="M4 8h8" />`);

export const IconDot = ({ size = 14, pulse = true }) =>
  svg(size, html`<circle cx="8" cy="8" r="3" fill="currentColor" stroke="none" />`, pulse ? "pulse" : "");

export const IconChevronDown = ({ size = 12 }) =>
  svg(size, html`<path d="M4 6l4 4 4-4" />`);

export const IconClose = ({ size = 16 }) =>
  svg(size, html`<path d="M4 4l8 8M12 4l-8 8" />`);

export const IconSearch = ({ size = 13 }) =>
  svg(size, html`<circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5 14 14" />`);

export const IconBulb = ({ size = 14 }) =>
  svg(
    size,
    html`<path d="M5.5 11a4.5 4.5 0 1 1 5 0" /><path d="M6.5 11h3v1.5a1.5 1.5 0 0 1-3 0V11Z" /><path d="M8 14.5v0" />`
  );

export const IconInfo = ({ size = 13 }) =>
  svg(size, html`<circle cx="8" cy="8" r="6.5" stroke-width="1.5" /><path d="M8 7.5V11" /><path d="M8 5v.2" />`);

export const IconCopy = ({ size = 13 }) =>
  svg(
    size,
    html`<rect x="5.5" y="5.5" width="8" height="8" rx="1.5" /><path d="M10.5 5.5v-2A1.5 1.5 0 0 0 9 2H4a1.5 1.5 0 0 0-1.5 1.5V9A1.5 1.5 0 0 0 4 10.5h1.5" />`
  );

// Cell-state icon by state byte. Pending renders blank.
export function StateIcon({ state, size = 14 }) {
  switch (state) {
    case 1:
      return IconDot({ size });
    case 2:
      return IconCheck({ size });
    case 3:
      return IconBolt({ size });
    case 4:
      return IconRotate({ size });
    case 5:
      return IconX({ size });
    case 6:
      return IconMinus({ size });
    default:
      return null;
  }
}
