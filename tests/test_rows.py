"""GET /api/rows — server-side search, filter and sort over cell values.

Driven on a synthetic manifest-shaped log: typed fields (str, int, bool,
enum, list), an amana-shaped ``{value, source}`` object field, nulls and
empty strings, a cached row, an errored row, a skipped cell and a row the run
never reached. Semantics under test are the ones docs/api-shapes.md promises
the grid: value filters see settled cells only, unknown columns are missing
rather than 400s, object fields filter on one sub-key, unknown sorts last.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from accrue_ui.server.index import RunIndex
from accrue_ui.server.tail import TailRecord
from tests.conftest import age_file, client_for, row, write_log

PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)
NROWS = 10

# extract: domain (key), revenue {value, source}, score int, active bool,
# tags list, note str. classify: icp enum.
REVENUE = {
    0: {"value": "$12M", "source": "https://a.example/one"},
    1: {"value": None, "source": "https://b.example/two"},
    2: {"value": "", "source": "https://c.example/three"},
    3: {"value": "$3M", "source": "https://d.example/four"},
    4: {"source": "no-value-key"},  # no `value` sub-key at all
    5: {"value": "$40M", "source": "https://f.example/six"},
    6: "plain string, not an object",
    7: {"value": "$7M", "source": "https://h.example/eight"},
}
SCORE = {0: 10, 1: 3, 2: 7, 3: "12", 4: None, 5: True, 6: 7, 7: float("nan")}
ACTIVE = {0: True, 1: False, 2: None, 3: True, 4: False, 5: True, 6: True, 7: False}
TAGS = {0: ["a", "b"], 1: [], 2: ["c"], 3: ["a"], 4: ["b"], 5: [], 6: ["x"], 7: ["a"]}
NOTE = {
    0: "Fintech payments",
    1: "SaaS tooling",
    2: "",
    3: "fintech lending",
    4: "Hardware",
    5: None,
    6: "FINTECH infra",
    7: "Retail",
}
ICP = {
    0: "strong",
    1: "weak",
    2: "good",
    4: "strong",
    5: "good",
    6: "strong",
    7: "weak",
}


def _records() -> list[dict]:
    manifest = {
        "accrue_version": "1.5.1",
        "config": {"checkpointing": True},
        "steps": [
            {
                "name": "extract",
                "type": "LLMStep",
                "produces": ["domain", "revenue", "score", "active", "tags", "note"],
                "depends_on": [],
            },
            {
                "name": "classify",
                "type": "LLMStep",
                "produces": ["icp"],
                "depends_on": ["extract"],
            },
        ],
        "fields": [
            {"name": "domain", "type": "str", "step": "extract"},
            {"name": "revenue", "type": "unknown", "step": "extract"},
            {"name": "score", "type": "int", "step": "extract"},
            {"name": "active", "type": "bool", "step": "extract"},
            {"name": "tags", "type": "list", "step": "extract"},
            {"name": "note", "type": "str", "step": "extract"},
            {
                "name": "icp",
                "type": "enum",
                "enum": ["strong", "good", "weak", "none"],
                "step": "classify",
            },
        ],
    }
    records = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "rows-run",
            "started_at": "2026-10-01T10:00:00+00:00",
            "num_rows": NROWS,
            "display_key": "domain",
            "steps": [
                {"name": "extract", "level": 0, "mode": "realtime", "model": None},
                {"name": "classify", "level": 1, "mode": "realtime", "model": None},
            ],
            "manifest": manifest,
        }
    ]
    t = 1.0
    for r in range(8):
        values = {
            "domain": f"site{r}.com",
            "revenue": REVENUE[r],
            "score": SCORE[r],
            "active": ACTIVE[r],
            "tags": TAGS[r],
            "note": NOTE[r],
            "__ctx": f"internal {r} needle-in-internal",
        }
        records.append(row("extract", r, t, values=values, from_cache=(r == 5)))
        t += 0.1
    records.append(
        row(
            "extract",
            8,
            t,
            status="error",
            error={"type": "LLMAPIError", "msg": "boom site8"},
        )
    )
    # Row 9: never reached (pending in both steps).
    for r in range(8):
        if r == 3:
            records.append(row("classify", r, t, status="skipped"))
        else:
            records.append(row("classify", r, t, values={"icp": ICP[r]}))
        t += 0.1
    records.append(row("classify", 8, t, status="skipped"))
    return records


def _write(path: Path) -> Path:
    records = _records()
    text = "\n".join(json.dumps(r) for r in records) + "\n"
    # json.dumps writes NaN as a bare token, which the tail's json.loads
    # accepts — exactly what a pandas-sourced log looks like.
    path.write_text(text, encoding="utf-8")
    age_file(path)
    return path


@pytest.fixture
def rows_log(tmp_path: Path) -> Path:
    return _write(tmp_path / "rows.jsonl")


def _rows(client, **params) -> dict:
    query = "&".join(
        f"{k}={json.dumps(v) if isinstance(v, (list, dict)) else v}"
        for k, v in params.items()
    )
    resp = client.get(f"/api/rows?{query}" if query else "/api/rows")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _filter(client, *clauses, **params) -> list[int]:
    return _rows(client, filter=list(clauses), **params)["rows"]


# ------------------------------------------------------------ no query


def test_no_query_returns_every_row_in_order(rows_log: Path):
    with client_for(rows_log) as client:
        body = _rows(client)
    assert body == {"rows": list(range(NROWS)), "total": NROWS, "matched": NROWS}


# -------------------------------------------------------------- search


def test_search_all_hits_keys_values_and_object_sub_keys(rows_log: Path):
    with client_for(rows_log) as client:
        assert _rows(client, q="site3")["rows"] == [3]  # the row key
        assert _rows(client, q="FINTECH")["rows"] == [0, 3, 6]  # case-insensitive
        # A sub-key of the object field matches on its own text.
        assert _rows(client, q="b.example")["rows"] == [1]
        # The errored row's message is not a value; row 8 never matches.
        assert _rows(client, q="site8")["rows"] == []
        # Internal fields are not searched.
        assert _rows(client, q="needle-in-internal")["rows"] == []


def test_search_scopes(rows_log: Path):
    with client_for(rows_log) as client:
        assert _rows(client, q="fintech", scope="keys")["rows"] == []
        assert _rows(client, q="site1", scope="keys")["rows"] == [1]
        assert _rows(client, q="example", scope="extract/revenue")["rows"] == [
            0,
            1,
            2,
            3,
            5,
            7,
        ]
        # Column scope: a value in another column is not a hit.
        assert _rows(client, q="fintech", scope="extract/revenue")["rows"] == []


# ------------------------------------------------------------- filters


def test_empty_on_an_object_field_uses_its_sub_key(rows_log: Path):
    with client_for(rows_log) as client:
        empty = _filter(client, {"col": "extract/revenue", "op": "empty"})
        # No `sub`: each object's own default sub-key. null value (1), ""
        # value (2). Row 4 has no `value`, so its default is `source`, which
        # is set. Row 6 is a plain string (not empty); 8 errored and 9 is
        # pending: unknown, not empty.
        assert empty == [1, 2]
        full = _filter(client, {"col": "extract/revenue", "op": "notempty"})
        assert full == [0, 3, 4, 5, 6, 7]
        # The column's sub-key, as the grid sends it: a missing sub-key is
        # missing, never a fallback onto another key.
        assert _filter(
            client, {"col": "extract/revenue", "op": "empty", "sub": "value"}
        ) == [1, 2, 4]
        # An explicit sub-key: every object has a source.
        assert (
            _filter(client, {"col": "extract/revenue", "op": "empty", "sub": "source"})
            == []
        )


def test_empty_covers_null_blank_list_and_nan(rows_log: Path):
    with client_for(rows_log) as client:
        assert _filter(client, {"col": "extract/note", "op": "empty"}) == [2, 5]
        assert _filter(client, {"col": "extract/tags", "op": "empty"}) == [1, 5]
        # None (4) and NaN (7) are both nothing.
        assert _filter(client, {"col": "extract/score", "op": "empty"}) == [4, 7]


def test_unknown_field_is_missing_everywhere_not_a_400(rows_log: Path):
    with client_for(rows_log) as client:
        # "is empty" on a field no row produced: every settled row.
        assert _filter(client, {"col": "extract/nope", "op": "empty"}) == list(range(8))
        assert (
            _filter(client, {"col": "extract/nope", "op": "contains", "v": "x"}) == []
        )
        # An unknown step too — a filter restored from the hash must survive
        # a run that has not started that step yet.
        assert _filter(client, {"col": "later/field", "op": "contains", "v": "x"}) == []
        # Sorting by it keeps row order.
        assert _rows(client, sort="extract/nope:desc")["rows"] == list(range(NROWS))


def test_in_matches_enum_members_and_null(rows_log: Path):
    with client_for(rows_log) as client:
        assert _filter(
            client, {"col": "classify/icp", "op": "in", "v": ["strong"]}
        ) == [
            0,
            4,
            6,
        ]
        # Booleans: true / false / null checkboxes. null covers a null value.
        assert _filter(client, {"col": "extract/active", "op": "in", "v": [False]}) == [
            1,
            4,
            7,
        ]
        assert _filter(client, {"col": "extract/active", "op": "in", "v": [None]}) == [
            2
        ]
        # True is not 1, and 1 is not True.
        assert _filter(client, {"col": "extract/score", "op": "in", "v": [True]}) == [5]
        assert _filter(client, {"col": "extract/score", "op": "in", "v": [1]}) == []


def test_range_takes_numbers_and_numeric_strings_but_not_booleans(rows_log: Path):
    with client_for(rows_log) as client:
        hits = _filter(client, {"col": "extract/score", "op": "range", "v": [7, None]})
        # 10 (0), 7 (2), "12" (3), 7 (6). True (5) is not a number; NaN (7) is nothing.
        assert hits == [0, 2, 3, 6]
        assert _filter(
            client, {"col": "extract/score", "op": "range", "v": [None, 3]}
        ) == [1]
        assert _filter(
            client, {"col": "extract/score", "op": "range", "v": [3, 7]}
        ) == [
            1,
            2,
            6,
        ]


def test_string_ops(rows_log: Path):
    with client_for(rows_log) as client:
        assert _filter(
            client, {"col": "extract/note", "op": "contains", "v": "tech"}
        ) == [
            0,
            3,
            6,
        ]
        assert _filter(
            client, {"col": "extract/note", "op": "eq", "v": "hardware"}
        ) == [4]
        assert _filter(
            client, {"col": "extract/note", "op": "regex", "v": r"^fintech (pay|len)"}
        ) == [0, 3]
        # Non-strings compare against their JSON rendering (what the cell shows).
        assert _filter(
            client, {"col": "extract/tags", "op": "contains", "v": '"a"'}
        ) == [
            0,
            3,
            7,
        ]
        assert _filter(client, {"col": "extract/score", "op": "eq", "v": 7}) == [2, 6]


def test_state_filters_per_step_and_any_step(rows_log: Path):
    with client_for(rows_log) as client:
        assert _filter(client, {"col": "classify", "op": "state", "v": [SKIPPED]}) == [
            3,
            8,
        ]
        assert _filter(client, {"col": "extract", "op": "state", "v": [CACHED]}) == [5]
        assert _filter(client, {"col": "extract", "op": "state", "v": [PENDING]}) == [9]
        # "*": any step — what the global Errors / Skipped chips send.
        assert _filter(client, {"col": "*", "op": "state", "v": [ERROR]}) == [8]
        assert _filter(client, {"col": "*", "op": "state", "v": [ERROR, SKIPPED]}) == [
            3,
            8,
        ]


def test_clauses_and_search_combine_with_and(rows_log: Path):
    with client_for(rows_log) as client:
        hits = _rows(
            client,
            q="fintech",
            filter=[{"col": "classify/icp", "op": "in", "v": ["strong"]}],
        )
        assert hits["rows"] == [0, 6]
        assert hits["matched"] == 2
        assert hits["total"] == NROWS


# --------------------------------------------------------------- sorting


def test_sort_by_value_puts_unknown_last_in_both_directions(rows_log: Path):
    with client_for(rows_log) as client:
        asc = _rows(client, sort="extract/score:asc")["rows"]
        desc = _rows(client, sort="extract/score:desc")["rows"]
    # bool < number < string: True (5), then 3 (1), 7 (2), 7 (6), 10 (0),
    # then "12" (3) as a string; None (4), NaN (7), error (8), pending (9)
    # trail in row order either way. Rows 2 and 6 tie on 7: row order.
    assert asc == [5, 1, 2, 6, 0, 3, 4, 7, 8, 9]
    assert desc == [3, 0, 2, 6, 1, 5, 4, 7, 8, 9]


def test_sort_strings_case_folded_and_object_sub_key(rows_log: Path):
    with client_for(rows_log) as client:
        notes = _rows(client, sort="extract/note:asc")["rows"]
        # "" (2) sorts as a string before letters; None (5) is unknown and last
        # with 8 and 9. fintech lending (3) < Fintech payments (0) < FINTECH
        # infra (6)? No: case-folded, "fintech infra" < "fintech lending" <
        # "fintech payments".
        assert notes == [2, 6, 3, 0, 4, 7, 1, 5, 8, 9]
        revenue = _rows(client, sort="extract/revenue:desc")["rows"]
        # On each object's default sub-key: "$7M" > "$40M" > "$3M" > "$12M"
        # as strings; row 6's plain string and row 4's default (`source`,
        # "no-value-key") sort above "$…"; row 2's "" is the smallest string;
        # row 1's null is unknown and trails with 8 and 9.
        assert revenue == [6, 4, 7, 5, 3, 0, 2, 1, 8, 9]
        by_source = _rows(client, sort="extract/revenue:asc", sort_sub="source")["rows"]
        assert by_source[:3] == [0, 1, 2]
        assert by_source[-3:] == [6, 8, 9]  # row 6 is not an object: no source


def test_sort_by_state_uses_the_explicit_rank(rows_log: Path):
    with client_for(rows_log) as client:
        desc = _rows(client, sort="classify:desc")["rows"]
    # worst first: error (none in classify) > ok (0,1,2,4,5,6,7) > cached >
    # skipped (3, 8) > retrying > running > pending (9).
    assert desc == [0, 1, 2, 4, 5, 6, 7, 3, 8, 9]


# ---------------------------------------------------------------- facets


def test_facet_counts_exclude_the_columns_own_clause(rows_log: Path):
    with client_for(rows_log) as client:
        body = _rows(
            client,
            filter=[
                {"col": "classify/icp", "op": "in", "v": ["weak"]},
                {"col": "extract/active", "op": "in", "v": [True]},
            ],
            facet="classify/icp",
        )
    # Rows still honour every clause: weak AND active -> none (1 and 7 are
    # not active).
    assert body["rows"] == []
    # The facet counts icp over rows matching the OTHER clause only (active:
    # 0, 3, 5, 6): strong (0, 6), good (5); row 3 is skipped -> unsettled.
    assert body["facet"] == {
        "col": "classify/icp",
        "values": [["strong", 2], ["good", 1]],
        "other": 0,
        "empty": 0,
    }


def test_facet_only_skips_the_row_list_and_counts_empty(rows_log: Path):
    with client_for(rows_log) as client:
        body = _rows(client, facet="extract/revenue", only="facet")
    assert body["rows"] == []
    assert body["matched"] is None
    assert body["facet"]["empty"] == 2  # rows 1, 2 (row 4 defaults to `source`)
    assert body["facet"]["values"][0][1] == 1  # every value distinct
    assert (
        len(body["facet"]["values"]) == 6
    )  # four "$…", no-value-key, the plain string


# ------------------------------------------------------------ bad input


@pytest.mark.parametrize(
    "query",
    [
        "filter=not-json",
        "filter=%7B%7D",  # an object, not an array
        'filter=[{"col":"extract/note","op":"nope"}]',
        'filter=[{"col":"extract/note","op":"state","v":[2]}]',  # state on a field
        'filter=[{"col":"extract","op":"contains","v":"x"}]',  # value op on a step
        'filter=[{"col":"*","op":"contains","v":"x"}]',
        'filter=[{"col":"extract/note","op":"regex","v":"("}]',
        'filter=[{"col":"extract/note","op":"regex","v":"' + "a" * 201 + '"}]',
        'filter=[{"col":"extract/score","op":"range","v":[1]}]',
        'filter=[{"col":"extract/score","op":"range","v":["x",null]}]',
        'filter=[{"col":"extract/active","op":"in","v":true}]',
        'filter=[{"col":"/note","op":"empty"}]',
        'filter=[{"col":"extract/","op":"empty"}]',
        "sort=extract/score",
        "sort=extract/score:up",
        "sort=*:asc",
        "scope=extract",
        "facet=extract",
        "only=rows",
        "only=facet",  # needs a facet column
    ],
)
def test_malformed_queries_are_400(rows_log: Path, query: str):
    with client_for(rows_log) as client:
        resp = client.get(f"/api/rows?{query}")
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]


def test_unknown_run_is_404(rows_log: Path):
    with client_for(rows_log) as client:
        assert client.get("/api/rows?run=nope").status_code == 404


# --------------------------------------------------------- values?rows=


def test_values_for_an_explicit_row_list(rows_log: Path):
    with client_for(rows_log) as client:
        body = client.get("/api/values?rows=7,2,99,-1,2").json()
        assert [r["row"] for r in body["rows"]] == [
            7,
            2,
            2,
        ]  # order kept, bounds dropped
        assert body["start"] == 7
        assert body["rows"][0]["cells"]["extract"]["f"]["revenue"] == {
            "o": {"value": "$7M", "source": "https://h.example/eight"},
            "d": "value",
        }
        assert client.get("/api/values?rows=").json()["rows"] == []
        assert client.get("/api/values?rows=1,x").status_code == 400


def test_values_row_list_is_capped(rows_log: Path):
    from accrue_ui.server.routes import VALUES_MAX_COUNT

    with client_for(rows_log) as client:
        rows = ",".join("0" for _ in range(VALUES_MAX_COUNT + 5))
        body = client.get(f"/api/values?rows={rows}").json()
    assert len(body["rows"]) == VALUES_MAX_COUNT


# ------------------------------------------------------ delta: fields


def test_step_delta_carries_fields_when_the_list_grows(rows_log: Path):
    index = RunIndex(rows_log)
    records = _records()
    index.apply(TailRecord(0, 1, records[0]))
    index.drain_delta()
    index.apply(TailRecord(0, 1, records[1]))  # first extract row: 7 fields
    delta = index.drain_delta()
    (step,) = delta["steps"]
    assert step["name"] == "extract"
    assert step["fields"] == [
        "domain",
        "revenue",
        "score",
        "active",
        "tags",
        "note",
        "__ctx",
    ]
    index.apply(TailRecord(0, 1, records[2]))  # same fields again: no list
    delta = index.drain_delta()
    assert "fields" not in delta["steps"][0]


def test_merge_keeps_fields_under_a_later_counters_only_delta():
    from accrue_ui.server.events import merge_deltas

    base = {
        "t": 1.0,
        "cells": [],
        "stats": {},
        "steps": [{"name": "s", "done": 1, "errors": 0, "fields": ["a"]}],
    }
    new = {
        "t": 2.0,
        "cells": [],
        "stats": {},
        "steps": [{"name": "s", "done": 2, "errors": 0}],
    }
    merged = merge_deltas(base, new)
    assert merged["steps"] == [{"name": "s", "done": 2, "errors": 0, "fields": ["a"]}]


# ----------------------------------------------------------------- cache


def test_results_are_cached_per_revision(rows_log: Path):
    from accrue_ui.server.query import parse_query, select_rows

    index = RunIndex(rows_log)
    for rec in _records():
        index.apply(TailRecord(0, 1, rec))
    spec = parse_query(q="fintech")
    first = select_rows(index, spec)
    assert select_rows(index, parse_query(q="fintech")) is first
    index.apply(TailRecord(0, 1, row("extract", 9, 99.0, values={"note": "fintech"})))
    second = select_rows(index, spec)
    assert second is not first
    assert second["rows"] == [0, 3, 6, 9]


def test_write_helper_round_trips_nan(tmp_path: Path):
    """The fixture relies on NaN surviving the log round trip."""
    path = tmp_path / "nan.jsonl"
    write_log(path, [row("s", 0, 1.0, values={"x": float("nan")})])
    assert "NaN" in path.read_text()
