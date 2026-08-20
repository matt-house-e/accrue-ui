"""Export report (issue #15): the /api/report download.

The report is a single self-contained HTML file — all CSS inlined, all data
embedded, no external references, no JavaScript — so it opens offline from
``file://`` and is CSP-clean. These tests pin that self-containment (the DoD),
the download headers, and that the same launch-token auth guards it.
"""

from __future__ import annotations

import json
from pathlib import Path

from accrue_ui.server.index import RunIndex
from accrue_ui.server.report import render_report, report_filename
from accrue_ui.server.tail import TailRecord
from tests.conftest import age_file, client_for, row, write_log


def _report_records(run_id: str = "2026-08-19-report") -> list[dict]:
    return [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": run_id,
            "started_at": "2026-08-19T10:00:00+00:00",
            "num_rows": 4,
            "display_key": "domain",
            "steps": [
                {"name": "fetch", "level": 0, "mode": "realtime", "model": "gpt-5.2"},
                {"name": "score", "level": 1, "mode": "realtime", "model": "gpt-5.2"},
            ],
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.1,
            "type": "step_start",
            "step": "fetch",
            "level": 0,
            "mode": "realtime",
            "num_rows": 4,
        },
        row(
            "fetch",
            0,
            1.0,
            values={"domain": "a.com"},
            usage={"in": 1000, "out": 100, "cost": None},
        ),
        row(
            "fetch",
            1,
            1.1,
            from_cache=True,
            values={"domain": "b.com"},
            usage={"in": 1000, "out": 100, "cost": None},
        ),
        row(
            "fetch",
            2,
            1.2,
            status="error",
            error={"type": "RateLimitError", "msg": "slow down please"},
        ),
        row("fetch", 3, 1.3, status="skipped"),
        {"v": 1, "t": 2.0, "type": "pipeline_end", "elapsed_s": 12.0},
    ]


def _log(tmp_path: Path, run_id: str = "2026-08-19-report") -> Path:
    path = tmp_path / "report.jsonl"
    write_log(path, _report_records(run_id))
    age_file(path)  # finished + cold, so run.live is False
    return path


# ---- render_report: self-containment (the DoD) ----------------------------


def _rendered(tmp_path: Path, run_id: str = "2026-08-19-report") -> str:
    index = RunIndex(_log(tmp_path, run_id))
    with open(index.path, "rb") as f:
        raw = f.read()
    offset = 0
    for line in raw.splitlines(keepends=True):
        index.apply(TailRecord(offset, len(line), json.loads(line)))
        offset += len(line)
    return render_report(index)


def test_report_is_a_self_contained_html_document(tmp_path: Path):
    html_doc = _rendered(tmp_path)
    assert html_doc.startswith("<!doctype html>")
    assert "<style>" in html_doc and "</style>" in html_doc
    # No external references of any kind — this is what lets it open offline.
    assert "http://" not in html_doc
    assert "https://" not in html_doc
    # No remote asset tags, and no JavaScript at all.
    assert "<script" not in html_doc.lower()
    assert "<link" not in html_doc.lower()
    assert "src=" not in html_doc.lower()  # no img/iframe/script src
    assert "url(" not in html_doc.lower()  # no CSS-fetched fonts/images
    assert "@import" not in html_doc.lower()
    assert "srcset" not in html_doc.lower()


def test_report_reflects_the_run(tmp_path: Path):
    html_doc = _rendered(tmp_path)
    assert "2026-08-19-report" in html_doc  # the run id
    assert "fetch" in html_doc and "score" in html_doc  # step names
    assert "RateLimitError" in html_doc  # the error group
    assert "slow down please" in html_doc  # the error message
    assert "gpt-5.2" in html_doc  # the model


def test_report_escapes_dynamic_text(tmp_path: Path):
    """An error message with markup must be escaped, never injected raw."""
    path = tmp_path / "xss.jsonl"
    records = _report_records()
    records[4] = row(
        "fetch",
        2,
        1.2,
        status="error",
        error={"type": "BoomError", "msg": "<script>alert(1)</script>"},
    )
    write_log(path, records)
    age_file(path)
    with client_for(path) as client:
        html_doc = client.get("/api/report").text
    assert "<script>alert(1)</script>" not in html_doc
    assert "&lt;script&gt;" in html_doc


def test_report_filename_is_sanitized(tmp_path: Path):
    index = RunIndex(_log(tmp_path, run_id="../../etc/passwd v2"))
    index.run_id = "../../etc/passwd v2"
    name = report_filename(index)
    assert name.endswith(".html")
    assert "/" not in name and ".." not in name and " " not in name


# ---- /api/report endpoint: download + auth --------------------------------


def test_report_endpoint_downloads_html(tmp_path: Path):
    with client_for(_log(tmp_path)) as client:
        resp = client.get("/api/report")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    disp = resp.headers["content-disposition"]
    assert disp == 'attachment; filename="2026-08-19-report.html"'
    assert resp.text.startswith("<!doctype html>")


def test_report_requires_the_launch_token(tmp_path: Path):
    """The export route sits under /api/*, so the same auth guards it."""
    with client_for(_log(tmp_path), authed=False) as client:
        resp = client.get("/api/report")
    assert resp.status_code == 401
