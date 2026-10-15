"""Server-side search, filter and sort over cell values (``GET /api/rows``).

The index already holds every cell's raw ``values`` dict in memory, so a
query is a pure read: no accrue-core change and no run-log contract change
(accrue-ui#28). This module turns the route's parameters into a
:class:`QuerySpec`, then :func:`select_rows` walks the grid once and returns
the matching row indices in the requested order, plus optional facet counts.

Semantics the grid relies on (docs/api-shapes.md, ``GET /api/rows``):

- **Value filters see settled cells only.** A pending, running, retrying,
  errored or skipped cell has no value; it is *unknown*, not null, and never
  matches a value filter. ``state`` filters cover those cells.
- **A field the run never produced is missing everywhere**, never a 400: a
  filter restored from the URL hash must survive a live run whose rows have
  not reached that field yet, and the index itself tolerates growing logs.
- **Object-valued fields filter, sort and facet on one sub-key** — the
  clause's ``sub`` (``sort_sub`` / ``facet_sub``), else the object's own default (the same rule ``_render_field``
  uses to pick what the cell shows). A sub-key the object lacks is missing,
  never a fallback onto another key.
- **Unknown sorts last in both directions**, and the sort is stable over
  ascending row index, so equal values keep row order as the tiebreak.

The scan runs off the event loop (the route uses ``asyncio.to_thread``):
the tail keeps applying records while it walks, so it reads references
captured at the start — ``_resize`` swaps in a new byte array rather than
mutating the old one, so a captured array always matches its captured
column count — and reads cells with ``dict.get`` rather than iterating the
dicts the loop mutates.
"""

from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .index import CACHED, OK, RunIndex, StepState, default_sub_key

FACET_MAX = 50
REGEX_MAX_CHARS = 200
QUERY_CACHE_MAX = 8
VALUE_OPS = frozenset({"in", "range", "contains", "eq", "regex", "empty", "notempty"})
OPS = VALUE_OPS | {"state"}
ANY_STEP = "*"

#: Sort rank for the state pseudo-column. The raw bytes put skipped after
#: error and retrying between cached and error, which reads as noise; this
#: orders by settledness, so ``desc`` is worst-first.
STATE_RANK = {0: 0, 1: 1, 4: 2, 6: 3, 3: 4, 2: 5, 5: 6}


class QueryError(ValueError):
    """A malformed query — the route turns it into a 400."""


# Sentinels for a cell that has no value to compare.
class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


UNKNOWN = _Sentinel("UNKNOWN")  # the cell is not settled (no values at all)
MISSING = _Sentinel("MISSING")  # settled, but the field (or sub-key) is absent


@dataclass
class Clause:
    step: str  # a step name, or ANY_STEP (state filters only)
    field: str | None  # None: the step's state pseudo-column
    op: str
    value: Any = None
    sub: str | None = None
    regex: re.Pattern[str] | None = field(default=None, repr=False)


@dataclass
class QuerySpec:
    q: str = ""
    scope: tuple[str, str | None] | None = None  # None: all; ("", None): keys
    clauses: list[Clause] = field(default_factory=list)
    sort: tuple[str, str | None, str, str | None] | None = None  # step, field, dir, sub
    facet: tuple[str, str] | None = None
    facet_sub: str | None = None  # sub-key to count an object-valued facet on
    only_facet: bool = False

    @property
    def active(self) -> bool:
        return bool(self.q or self.clauses or self.sort or self.facet)

    def cache_key(self) -> tuple:
        return (
            self.q,
            self.scope,
            tuple(
                (c.step, c.field, c.op, json.dumps(c.value, sort_keys=True), c.sub)
                for c in self.clauses
            ),
            self.sort,
            self.facet,
            self.facet_sub,
            self.only_facet,
        )


# ----------------------------------------------------------------- parsing


def parse_col(ref: Any) -> tuple[str, str | None]:
    """``"step/field"`` -> (step, field); ``"step"`` -> (step, None).

    Split on the first ``/``: step names are identifiers, field names are
    free text and may contain one.
    """
    if not isinstance(ref, str) or not ref:
        raise QueryError("column must be a non-empty string")
    step, sep, fld = ref.partition("/")
    if not step:
        raise QueryError(f"column {ref!r} names no step")
    if sep and not fld:
        raise QueryError(f"column {ref!r} names no field")
    return step, (fld if sep else None)


def _parse_clause(raw: Any) -> Clause:
    if not isinstance(raw, dict):
        raise QueryError("each filter clause must be an object")
    step, fld = parse_col(raw.get("col"))
    op = raw.get("op")
    if op not in OPS:
        raise QueryError(f"unknown filter op {op!r}")
    value = raw.get("v")
    sub = raw.get("sub")
    if sub is not None and not isinstance(sub, str):
        raise QueryError('"sub" must be a string')

    if op == "state":
        if fld is not None:
            raise QueryError('"state" applies to a step, not a field')
        valid = isinstance(value, list) and all(
            isinstance(s, int) and not isinstance(s, bool) and 0 <= s <= 6
            for s in value
        )
        if not valid:
            raise QueryError('"state" needs a list of state bytes 0..6')
        return Clause(step, None, op, list(value))

    if fld is None:
        raise QueryError(f'op {op!r} needs a field column ("step/field")')
    if step == ANY_STEP:
        raise QueryError('"*" is only valid with the "state" op')

    clause = Clause(step, fld, op, value, sub)
    if op == "in":
        if not isinstance(value, list):
            raise QueryError('"in" needs a list of values')
    elif op == "range":
        ok = (
            isinstance(value, list)
            and len(value) == 2
            and all(v is None or _as_number(v) is not None for v in value)
        )
        if not ok:
            raise QueryError('"range" needs [min|null, max|null]')
        clause.value = [None if v is None else _as_number(v) for v in value]
    elif op in ("contains", "regex"):
        if not isinstance(value, str) or not value:
            raise QueryError(f'"{op}" needs a non-empty string')
        if op == "regex":
            if len(value) > REGEX_MAX_CHARS:
                raise QueryError(f"regex longer than {REGEX_MAX_CHARS} characters")
            try:
                clause.regex = re.compile(value, re.IGNORECASE)
            except re.error as exc:
                raise QueryError(f"invalid regex: {exc}") from None
    elif op == "eq" and isinstance(value, (list, dict)):
        raise QueryError('"eq" compares against a scalar')
    # empty / notempty take no value.
    return clause


def parse_filter(raw: str | None) -> list[Clause]:
    if raw is None or raw == "":
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise QueryError("filter must be JSON") from None
    if not isinstance(parsed, list):
        raise QueryError("filter must be a JSON array of clauses")
    return [_parse_clause(c) for c in parsed]


def parse_sort(
    raw: str | None, sub: str | None
) -> tuple[str, str | None, str, str | None] | None:
    if raw is None or raw == "":
        return None
    col, sep, direction = raw.rpartition(":")
    if not sep or direction not in ("asc", "desc"):
        raise QueryError('sort must be "<col>:asc" or "<col>:desc"')
    step, fld = parse_col(col)
    if step == ANY_STEP:
        raise QueryError('"*" cannot be sorted')
    return step, fld, direction, (sub or None)


def parse_scope(raw: str | None) -> tuple[str, str | None] | None:
    if raw is None or raw == "" or raw == "all":
        return None
    if raw == "keys":
        return ("", None)
    step, fld = parse_col(raw)
    if fld is None or step == ANY_STEP:
        raise QueryError('scope must be "all", "keys" or "step/field"')
    return step, fld


def parse_query(
    *,
    q: str = "",
    scope: str | None = None,
    filter: str | None = None,
    sort: str | None = None,
    sort_sub: str | None = None,
    facet: str | None = None,
    facet_sub: str | None = None,
    only: str | None = None,
) -> QuerySpec:
    spec = QuerySpec(
        q=(q or "").strip(),
        scope=parse_scope(scope),
        clauses=parse_filter(filter),
        sort=parse_sort(sort, sort_sub),
    )
    if facet:
        step, fld = parse_col(facet)
        if fld is None or step == ANY_STEP:
            raise QueryError('facet must name a field column ("step/field")')
        spec.facet = (step, fld)
        spec.facet_sub = facet_sub or None
    if only is not None and only != "":
        if only != "facet":
            raise QueryError('only must be "facet"')
        if spec.facet is None:
            raise QueryError("only=facet needs a facet column")
        spec.only_facet = True
    return spec


# ------------------------------------------------------------------ values


def _as_number(v: Any) -> float | None:
    """A comparable number, or None. Booleans are not numbers here, and a
    numeric string counts (an int-typed field that came back as ``"12"`` is
    exactly what a range filter is for). NaN is nothing."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else float(v)
    if isinstance(v, str):
        try:
            n = float(v.strip())
        except ValueError:
            return None
        return None if math.isnan(n) or math.isinf(n) else n
    return None


def is_empty(v: Any) -> bool:
    """null, ``""``, ``[]``, ``{}``, NaN, or absent from the values map."""
    if v is UNKNOWN:
        return False
    if v is MISSING or v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, (str, list, dict)) and len(v) == 0


def _text(v: Any) -> str | None:
    """What the preview shows for *v*: a string as-is, anything else as JSON."""
    if v is UNKNOWN or v is MISSING or v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


def _search_texts(v: Any) -> list[str]:
    """Every string a search can hit in one field: each sub-value of an
    object on its own (so a ``{value, source}`` field matches on either),
    else the field's text."""
    if isinstance(v, dict) and v:
        out = []
        for sub in v.values():
            t = _text(sub)
            if t is not None:
                out.append(t)
        return out
    t = _text(v)
    return [t] if t is not None else []


def field_value(cell: Any, state: int, fld: str, sub: str | None) -> Any:
    """The comparable value of one field in one cell, or a sentinel."""
    if state not in (OK, CACHED) or cell is None or cell.values is None:
        return UNKNOWN
    if fld not in cell.values:
        return MISSING
    v = cell.values[fld]
    if isinstance(v, dict) and v:
        key = sub if sub is not None else default_sub_key(v)
        return v.get(key, MISSING)
    return v


def _scalar_eq(v: Any, m: Any) -> bool:
    """Equality that keeps ``True`` apart from ``1`` and ``"1"``."""
    if isinstance(m, bool) or isinstance(v, bool):
        return isinstance(m, bool) and isinstance(v, bool) and v is m
    if isinstance(m, (int, float)):
        n = _as_number(v)
        return n is not None and isinstance(v, (int, float)) and n == float(m)
    if isinstance(m, str):
        return isinstance(v, str) and v == m
    if m is None:
        return v is None or v is MISSING
    return False


def _matches(clause: Clause, v: Any) -> bool:
    op = clause.op
    if op == "empty":
        return is_empty(v)
    if op == "notempty":
        return v is not UNKNOWN and not is_empty(v)
    if v is UNKNOWN:
        return False
    if op == "in":
        return any(_scalar_eq(v, m) for m in clause.value)
    if op == "range":
        n = _as_number(v)
        if n is None:
            return False
        lo, hi = clause.value
        return (lo is None or n >= lo) and (hi is None or n <= hi)
    text = _text(v)
    if op == "eq":
        if isinstance(clause.value, str):
            return text is not None and text.lower() == clause.value.lower()
        return _scalar_eq(v, clause.value)
    if text is None:
        return False
    if op == "contains":
        return clause.value.lower() in text.lower()
    if op == "regex":
        return clause.regex.search(text) is not None
    return False


def sort_key(v: Any) -> tuple | None:
    """Type-ranked key: bool < number < string (case-folded) < other (JSON).
    None for anything unknown or empty, which sorts last either way."""
    if v is UNKNOWN or v is MISSING or v is None:
        return None
    if isinstance(v, bool):
        return (0, int(v), "")
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return (1, float(v), "")
    if isinstance(v, str):
        return (2, 0.0, v.casefold())
    return (3, 0.0, json.dumps(v, sort_keys=True, default=str))


def _facet_key(v: Any) -> Any:
    """A JSON-safe, hashable facet bucket for a settled, non-empty value."""
    if isinstance(v, (bool, int, float, str)):
        return v
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


# ------------------------------------------------------------------- scan


class _View:
    """References captured once, so a scan is consistent while the loop
    keeps applying records (see the module docstring)."""

    def __init__(self, index: RunIndex):
        self.steps: list[StepState] = list(index.steps)
        self.states = index.states
        self.nsteps = len(self.steps)
        self.nrows = min(index.nrows, len(self.states) // max(1, self.nsteps))
        self.by_name = {s.name: i for i, s in enumerate(self.steps)}
        self.row_key = index.row_key

    def state(self, row: int, step_i: int) -> int:
        return self.states[row * self.nsteps + step_i]

    def value(self, row: int, step: str, fld: str, sub: str | None) -> Any:
        i = self.by_name.get(step)
        if i is None:
            return MISSING if self._settled_somewhere(row) else UNKNOWN
        return field_value(self.steps[i].cells.get(row), self.state(row, i), fld, sub)

    def _settled_somewhere(self, row: int) -> bool:
        # An unknown step: the row has no cell for it. Treat as missing when
        # the row has settled at all (so "is empty" on an unproduced field
        # still finds rows), unknown otherwise.
        return any(self.state(row, i) in (OK, CACHED) for i in range(self.nsteps))

    def clause_hit(self, clause: Clause, row: int) -> bool:
        if clause.op == "state":
            if clause.step == ANY_STEP:
                return any(
                    self.state(row, i) in clause.value for i in range(self.nsteps)
                )
            i = self.by_name.get(clause.step)
            if i is None:
                return False
            return self.state(row, i) in clause.value
        return _matches(clause, self.value(row, clause.step, clause.field, clause.sub))

    def search_hit(self, spec: QuerySpec, row: int) -> bool:
        needle = spec.q.lower()
        scope = spec.scope
        if scope is None or scope == ("", None):
            if needle in self.row_key(row).lower():
                return True
            if scope is not None:
                return False
        if scope is not None:
            i = self.by_name.get(scope[0])
            if i is None:
                return False
            cell = self.steps[i].cells.get(row)
            if self.state(row, i) not in (OK, CACHED) or not cell or not cell.values:
                return False
            if scope[1] not in cell.values:
                return False
            return any(
                needle in t.lower() for t in _search_texts(cell.values[scope[1]])
            )
        for i, step in enumerate(self.steps):
            if self.state(row, i) not in (OK, CACHED):
                continue
            cell = step.cells.get(row)
            if not cell or not cell.values:
                continue
            for name, v in cell.values.items():
                if name.startswith("__"):
                    continue
                if any(needle in t.lower() for t in _search_texts(v)):
                    return True
        return False


def _select(index: RunIndex, spec: QuerySpec) -> dict[str, Any]:
    view = _View(index)
    facet_col = spec.facet
    main = spec.clauses
    # Facet counts follow every clause EXCEPT the facet column's own, so the
    # checkboxes say what each choice would yield (faceted-search convention).
    others = [c for c in main if facet_col is None or (c.step, c.field) != facet_col]
    own = [c for c in main if c not in others]

    rows: list[int] = []
    counts: dict[Any, int] = {}
    facet_empty = 0
    for r in range(view.nrows):
        if spec.q and not view.search_hit(spec, r):
            continue
        if not all(view.clause_hit(c, r) for c in others):
            continue
        if facet_col is not None:
            v = view.value(r, facet_col[0], facet_col[1], spec.facet_sub)
            if v is not UNKNOWN:
                if is_empty(v):
                    facet_empty += 1
                else:
                    k = _facet_key(v)
                    counts[k] = counts.get(k, 0) + 1
        if spec.only_facet:
            continue
        if all(view.clause_hit(c, r) for c in own):
            rows.append(r)

    if spec.sort is not None and rows:
        rows = _sorted(view, spec.sort, rows)

    out: dict[str, Any] = {"rows": rows, "total": view.nrows, "matched": len(rows)}
    if spec.only_facet:
        out["matched"] = None
    if facet_col is not None:
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        top = ranked[:FACET_MAX]
        out["facet"] = {
            "col": f"{facet_col[0]}/{facet_col[1]}",
            "values": [[k, n] for k, n in top],
            "other": sum(n for _, n in ranked[FACET_MAX:]),
            "empty": facet_empty,
        }
    return out


def _sorted(
    view: _View, sort: tuple[str, str | None, str, str | None], rows: list[int]
) -> list[int]:
    step, fld, direction, sub = sort
    reverse = direction == "desc"
    if fld is None:
        i = view.by_name.get(step)
        if i is None:
            return rows
        # Every row has a state, so there is no unknown bucket here.
        return sorted(rows, key=lambda r: STATE_RANK[view.state(r, i)], reverse=reverse)
    known: list[tuple[tuple, int]] = []
    unknown: list[int] = []
    for r in rows:
        k = sort_key(view.value(r, step, fld, sub))
        if k is None:
            unknown.append(r)
        else:
            known.append((k, r))
    # sorted() is stable with reverse=True too, so equal values keep their
    # ascending row order in both directions: the row index is the tiebreak.
    known.sort(key=lambda kr: kr[0], reverse=reverse)
    return [r for _, r in known] + unknown


def select_rows(index: RunIndex, spec: QuerySpec) -> dict[str, Any]:
    """Run *spec* against *index*, through a small per-index result cache.

    The cache is keyed on the index revision too, so a repeated query
    (a popover re-opening, a hash restore) is free while nothing changed and
    is never served stale once something did.
    """
    cache: OrderedDict = index.query_cache
    key = (index.revision, spec.cache_key())
    hit = cache.get(key)
    if hit is not None:
        cache.move_to_end(key)
        return hit
    result = _select(index, spec)
    cache[key] = result
    while len(cache) > QUERY_CACHE_MAX:
        cache.popitem(last=False)
    return result
