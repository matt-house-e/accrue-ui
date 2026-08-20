"""Standalone HTML report for a run — a shareable, offline artifact.

Renders the in-memory :class:`~accrue_ui.server.index.RunIndex` the dashboard
already holds into a **single self-contained HTML file**: all CSS inlined, all
data embedded in the markup, **no external references** (no CDN scripts, no
Google Fonts link, no remote images) and no JavaScript. It opens from
``file://`` with no server running and is CSP-clean.

This is deliberately accrue-ui-only — it reads the same snapshot the API
serves, so shipping it needs no new accrue core release. The look mirrors the
dashboard's dark theme (the CLAUDE.md token table) and its fonts (IBM Plex
Sans/Mono, *named* so an installed copy is used, with system fallbacks — never
fetched, so the file stays offline-clean).
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .index import RunIndex

DASH = "—"  # em-dash: an honestly-unknown number, never a fabricated 0
STATE_LABELS = ("pending", "running", "ok", "cached", "retrying", "error", "skipped")
#: One swatch color per cell-state byte, from the token table (bg tints).
STATE_COLORS = (
    "#212220",  # pending
    "#2a2c28",  # running (no dedicated token; a faint tick above pending)
    "#0f2e22",  # ok
    "#291f43",  # cached
    "#302008",  # retrying
    "#3b1219",  # error
    "#181917",  # skipped
)

_STYLE = """
:root{
  --ground:#111210;--surface:#181917;--component:#212220;--border-1:#383a36;
  --border-2:#454843;--faint-1:#687066;--faint-2:#767d74;--muted:#afb5ad;
  --ink:#eceeec;--jade-9:#29a383;--jade-11:#1fd8a4;--error-text:#ff9592;
  --font-sans:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
  --font-mono:'IBM Plex Mono',ui-monospace,'Cascadia Mono',Menlo,monospace;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--ground);color:var(--ink);
  font:400 13px/1.5 var(--font-sans);-webkit-font-smoothing:antialiased;}
.wrap{max-width:960px;margin:0 auto;padding:32px 24px 64px;}
.mono{font-family:var(--font-mono);}
header.rep{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding-bottom:16px;border-bottom:1px solid var(--border-1);margin-bottom:24px;}
header.rep h1{font-size:18px;font-weight:600;margin:0;}
header.rep .id{font-family:var(--font-mono);color:var(--jade-11);font-size:13px;}
.badge{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  padding:2px 8px;border-radius:999px;border:1px solid var(--border-2);color:var(--muted);}
.badge.done{color:var(--jade-11);border-color:var(--jade-9);}
.meta{color:var(--faint-2);font-size:12px;margin-left:auto;text-align:right;}
h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.05em;margin:32px 0 12px;}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;}
.tile{background:var(--surface);border:1px solid var(--border-1);border-radius:10px;
  padding:12px 14px;}
.tile .k{font-size:11px;color:var(--faint-2);text-transform:uppercase;letter-spacing:.04em;}
.tile .v{font-family:var(--font-mono);font-size:20px;margin-top:6px;}
.tile .v .dim{color:var(--faint-1);font-size:15px;}
.tile .v.red{color:var(--error-text);}
table{width:100%;border-collapse:collapse;font-size:12.5px;}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border-1);
  vertical-align:top;}
th{color:var(--faint-2);font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.04em;}
td.num,th.num{text-align:right;font-family:var(--font-mono);}
td.err{color:var(--error-text);font-family:var(--font-mono);}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:8px 0 16px;font-size:12px;
  color:var(--muted);}
.legend span{display:inline-flex;align-items:center;gap:6px;}
.sw{width:12px;height:12px;border-radius:3px;border:1px solid var(--border-2);display:inline-block;}
.statebar{display:flex;height:14px;border-radius:4px;overflow:hidden;
  border:1px solid var(--border-1);}
.statebar i{display:block;height:100%;}
.bar-row{display:grid;grid-template-columns:160px 1fr 90px;align-items:center;gap:12px;
  margin:6px 0;}
.bar-row .lbl{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;}
.bar-track{background:var(--component);border-radius:4px;overflow:hidden;height:14px;}
.bar-fill{display:block;height:14px;background:var(--jade-9);border-radius:0 4px 4px 0;}
.bar-val{font-family:var(--font-mono);text-align:right;font-size:12px;}
.egroup{background:var(--surface);border:1px solid var(--border-1);border-radius:10px;
  padding:12px 14px;margin:10px 0;}
.egroup .top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;}
.egroup .type{font-family:var(--font-mono);color:var(--error-text);font-weight:600;}
.egroup .cnt{margin-left:auto;font-family:var(--font-mono);color:var(--muted);}
.egroup .msg{color:var(--faint-2);font-size:12px;margin-top:6px;
  font-family:var(--font-mono);word-break:break-word;}
.egroup .hint{color:var(--jade-11);font-size:12px;margin-top:6px;}
.empty{color:var(--faint-1);font-size:12.5px;}
footer.rep{margin-top:40px;padding-top:16px;border-top:1px solid var(--border-1);
  color:var(--faint-1);font-size:11.5px;}
"""


def report_filename(index: RunIndex) -> str:
    """Safe ``<run_id>.html`` download name (no path or ``..`` surprises).

    Only ``[A-Za-z0-9_-]`` survive in the stem — dots included are dropped, so
    a hostile ``../../x`` run id cannot smuggle a path or a ``..`` into the
    Content-Disposition filename. The ``.html`` suffix is added unconditionally.
    """
    stem = index.run_id or index.path.stem
    safe = "".join(c if (c.isalnum() or c in "_-") else "-" for c in stem)
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-")
    return f"{safe or 'run'}.html"


def render_report(index: RunIndex) -> str:
    """Render *index* to a self-contained HTML document string."""
    snap = index.snapshot()
    run = snap["run"]
    counts = _state_counts(index)

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>accrue report {DASH} {_esc(run['id'])}</title>",
        f"<style>{_STYLE}</style>",
        "</head><body><div class='wrap'>",
        _header(run, finished=index.finished),
        _tiles(snap),
        _state_section(counts),
        _steps_section(snap),
        _cost_section(snap),
        _errors_section(snap),
        _footer(run),
        "</div></body></html>",
    ]
    return "".join(parts)


# --------------------------------------------------------------------- pieces


def _header(run: dict[str, Any], *, finished: bool) -> str:
    # A report is a static snapshot, so completion wins over mtime recency:
    # a finished run reads "Finished" even if its log was just touched, and a
    # mid-run export reads "In progress" (a snapshot), never a misleading
    # "Live" that implies the saved file keeps updating.
    if finished:
        badge = '<span class="badge done">Finished</span>'
    elif run["live"]:
        badge = '<span class="badge">In progress</span>'
    else:
        badge = '<span class="badge">Ended</span>'
    name = run["name"]
    id_ = run["id"]
    title = _esc(name) if name and name != id_ else ""
    started = run.get("started_at") or DASH
    elapsed = _duration(run.get("elapsed_s"))
    id_html = f"<span class='id mono'>{_esc(id_)}</span>"
    heading = f"<h1>{title}</h1>{id_html}" if title else f"<h1>{id_html}</h1>"
    return (
        "<header class='rep'>"
        f"{heading}{badge}"
        f"<span class='meta'>started {_esc(started)}<br>elapsed {elapsed}"
        f" · schema v{run.get('schema_v', 1)}</span>"
        "</header>"
    )


def _tiles(snap: dict[str, Any]) -> str:
    stats = snap["stats"]
    rows = snap["rows"]
    cost = snap["cost"]
    done = _int(rows["done"])
    total = _int(rows["total"])
    tiles = [
        _tile("Rows enriched", f"{done} <span class='dim'>/ {total}</span>", raw=True),
        _tile("Spend", _money(stats["spend"])),
        _tile("Cache hits", _pct(stats["cache_hit_rate"])),
        _tile("Cache saved", _money(stats["cache_saved"])),
        _tile("Errors", _int(stats["errors"]), tone="red" if stats["errors"] else ""),
        _tile("Wasted spend", _money(cost["wasted"])),
    ]
    if cost.get("batch_saved"):
        tiles.append(_tile("Batch saved", _money(cost["batch_saved"])))
    return f"<section class='tiles'>{''.join(tiles)}</section>"


def _tile(key: str, value: str, *, tone: str = "", raw: bool = False) -> str:
    body = value if raw else _esc(value)
    tone_cls = f" {tone}" if tone else ""
    return (
        f"<div class='tile'><div class='k'>{_esc(key)}</div>"
        f"<div class='v{tone_cls}'>{body}</div></div>"
    )


def _state_section(counts: list[int]) -> str:
    total = sum(counts)
    if not total:
        return ""
    segs = []
    legend = []
    for state, n in enumerate(counts):
        if not n:
            continue
        pct = n / total * 100
        color = STATE_COLORS[state]
        segs.append(f"<i style='width:{pct:.4f}%;background:{color}'></i>")
        legend.append(
            f"<span><span class='sw' style='background:{color}'></span>"
            f"{STATE_LABELS[state]} <span class='mono'>{_int(n)}</span></span>"
        )
    return (
        "<h2>Cell states</h2>"
        f"<div class='statebar'>{''.join(segs)}</div>"
        f"<div class='legend' style='margin-top:10px'>{''.join(legend)}</div>"
    )


def _steps_section(snap: dict[str, Any]) -> str:
    by_step = snap["cost"]["by_step"]
    rows = []
    for s in snap["steps"]:
        cost = by_step.get(s["name"])
        rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(s['name'])}</td>"
            f"<td class='num'>L{s['level']}</td>"
            f"<td>{_esc(s['mode'])}</td>"
            f"<td class='mono'>{_esc(s['model'] or DASH)}</td>"
            f"<td class='num'>{_int(s['done'])} / {_int(s['total'])}</td>"
            f"<td class='{'err' if s['errors'] else 'num'}'>{_int(s['errors'])}</td>"
            f"<td class='num'>{_money(cost) if cost is not None else DASH}</td>"
            "</tr>"
        )
    return (
        "<h2>Steps</h2><table><thead><tr>"
        "<th>Step</th><th class='num'>Level</th><th>Mode</th><th>Model</th>"
        "<th class='num'>Done</th><th class='num'>Errors</th><th class='num'>Cost</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _cost_section(snap: dict[str, Any]) -> str:
    cost = snap["cost"]
    by_model = cost["by_model"]
    tokens = cost["tokens"]
    out = ["<h2>Cost</h2>"]
    if by_model:
        mx = max(by_model.values()) or 1.0
        bars = []
        for model, usd in sorted(by_model.items(), key=lambda kv: -kv[1]):
            width = usd / mx * 100
            bars.append(
                "<div class='bar-row'>"
                f"<span class='lbl mono'>{_esc(model)}</span>"
                f"<span class='bar-track'><span class='bar-fill' "
                f"style='width:{width:.3f}%'></span></span>"
                f"<span class='bar-val'>{_esc(_money(usd))}</span></div>"
            )
        out.append("".join(bars))
    else:
        out.append("<p class='empty'>No priced models in this run.</p>")
    out.append(
        "<div class='tiles' style='margin-top:16px'>"
        + _tile("Input tokens", _int(tokens["input"]))
        + _tile("Output tokens", _int(tokens["output"]))
        + _tile("Cache read", _int(tokens["cache_read"]))
        + _tile("Cache write", _int(tokens["cache_write"]))
        + "</div>"
    )
    plan = cost.get("plan")
    if isinstance(plan, dict) and plan.get("est_total") is not None:
        out.append(
            "<p class='empty' style='margin-top:14px'>Planned estimate: "
            f"<span class='mono'>{_esc(_money(plan['est_total']))}</span></p>"
        )
    return "".join(out)


def _errors_section(snap: dict[str, Any]) -> str:
    groups = snap["error_groups"]
    if not groups:
        return "<h2>Errors</h2><p class='empty'>No errors — clean run.</p>"
    cards = []
    for g in groups:
        hint = f"<div class='hint'>{_esc(g['hint'])}</div>" if g.get("hint") else ""
        cards.append(
            "<div class='egroup'><div class='top'>"
            f"<span class='type'>{_esc(g['type'])}</span>"
            f"<span class='mono' style='color:var(--faint-2)'>{_esc(g['step'])}</span>"
            f"<span class='cnt'>{_int(g['count'])} rows</span></div>"
            f"<div class='msg'>{_esc(g['message'] or DASH)}</div>{hint}</div>"
        )
    return f"<h2>Errors</h2>{''.join(cards)}"


def _footer(run: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "<footer class='rep'>Generated by accrue watch for run "
        f"<span class='mono'>{_esc(run['id'])}</span> at {now}. "
        "Self-contained snapshot — opens offline.</footer>"
    )


# -------------------------------------------------------------------- helpers


def _state_counts(index: RunIndex) -> list[int]:
    """Count each cell-state byte across the whole grid (0..6)."""
    counts = [0] * len(STATE_LABELS)
    # Same-package read of the state bytearray; cheaper than re-decoding the
    # snapshot's base64 just to tally it.
    for byte in index._cells:
        if 0 <= byte < len(counts):
            counts[byte] += 1
    return counts


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _int(value: Any) -> str:
    return f"{int(value):,}" if isinstance(value, (int, float)) else DASH


def _money(value: Any) -> str:
    if value is None or not isinstance(value, (int, float)):
        return DASH
    return f"${value:,.2f}"


def _pct(value: Any) -> str:
    if value is None or not isinstance(value, (int, float)):
        return DASH
    return f"{value * 100:.0f}%"


def _duration(seconds: Any) -> str:
    if seconds is None or not isinstance(seconds, (int, float)):
        return DASH
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
