// Formatting helpers. All numbers render in IBM Plex Mono via CSS.

export function fmtInt(n) {
  if (n == null) return "";
  return Number(n).toLocaleString("en-US");
}

export function fmtMoney(n) {
  if (n == null) return null;
  const abs = Math.abs(n);
  const digits = abs > 0 && abs < 0.01 ? 4 : 2;
  const s = abs.toFixed(digits);
  return `${n < 0 ? "−" : ""}$${s}`;
}

export function fmtPct(x) {
  if (x == null) return null;
  return `${Math.round(x * 100)}%`;
}

export function fmtDuration(s) {
  if (s == null) return null;
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

export function fmtLatency(ms) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// "2026-08-17T09:41:02Z" -> "09:41:02" (UTC, matching the log timestamps).
export function fmtClock(iso, withSeconds = true) {
  if (!iso) return null;
  const m = String(iso).match(/T(\d{2}):(\d{2}):(\d{2})/);
  if (!m) return String(iso);
  return withSeconds ? `${m[1]}:${m[2]}:${m[3]}` : `${m[1]}:${m[2]}`;
}
