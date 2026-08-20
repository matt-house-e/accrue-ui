"""Tail: chunked appends, split-mid-line writes, truncation, async wrapper."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from accrue_ui.server.tail import Tail


def _lines(records: list[dict]) -> list[bytes]:
    return [json.dumps(r).encode() + b"\n" for r in records]


RECORDS = [
    {"v": 1, "t": 0.0, "type": "pipeline_start", "run_id": "r", "num_rows": 2},
    {"v": 1, "t": 0.1, "type": "step_start", "step": "a", "num_rows": 2},
    {"v": 1, "t": 0.2, "type": "row_complete", "step": "a", "row": 0, "status": "ok"},
    {"v": 1, "t": 0.3, "type": "pipeline_end", "num_rows": 2, "total_errors": 0},
]


def test_yields_each_record_exactly_once_across_chunked_writes(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b"")
    lines = _lines(RECORDS)
    tail = Tail(path)

    assert tail.read_available() == []

    # Chunk 1: first record plus HALF of the second (split mid-line).
    with open(path, "ab") as f:
        f.write(lines[0] + lines[1][:10])
    got = tail.read_available()
    assert [r.data for r in got] == RECORDS[:1]

    # Nothing new: the partial line must stay buffered, not re-yield.
    assert tail.read_available() == []

    # Chunk 2: the rest of the second record and the last two whole ones.
    with open(path, "ab") as f:
        f.write(lines[1][10:] + lines[2] + lines[3])
    got2 = tail.read_available()
    assert [r.data for r in got2] == RECORDS[1:]

    # Offsets and lengths point at the exact byte spans in the file.
    assert (got[0].offset, got[0].length) == (0, len(lines[0]))
    assert got2[0].offset == len(lines[0])
    assert got2[0].length == len(lines[1])
    blob = path.read_bytes()
    for rec in [*got, *got2]:
        assert json.loads(blob[rec.offset : rec.offset + rec.length]) == rec.data

    tail.close()


def test_tolerates_junk_and_blank_lines(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    lines = _lines(RECORDS[:2])
    path.write_bytes(lines[0] + b"\n" + b"{not json}\n" + b"[1, 2]\n" + lines[1])
    tail = Tail(path)
    got = tail.read_available()
    assert [r.data for r in got] == RECORDS[:2]
    tail.close()


def test_truncation_reopens_from_start(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    lines = _lines(RECORDS)
    path.write_bytes(lines[0] + lines[1] + lines[2])
    tail = Tail(path)
    assert len(tail.read_available()) == 3
    assert tail.generation == 0

    # Truncate in place (same inode, smaller size).
    path.write_bytes(lines[3])
    got = tail.read_available()
    assert [r.data for r in got] == [RECORDS[3]]
    assert got[0].offset == 0
    assert tail.generation == 1
    tail.close()


def test_replacement_reopens_new_inode(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    lines = _lines(RECORDS)
    path.write_bytes(lines[0])
    tail = Tail(path)
    assert len(tail.read_available()) == 1

    # Replace with a *larger* file at a new inode: size never shrinks, so
    # only the inode check can catch this.
    other = tmp_path / "other.jsonl"
    other.write_bytes(lines[0] + lines[1] + lines[2])
    os.replace(other, path)
    got = tail.read_available()
    assert [r.data for r in got] == RECORDS[:3]
    assert tail.generation == 1
    tail.close()


def test_truncate_and_regrow_past_the_read_position_reopens(tmp_path: Path):
    """A new run written over the same path, in place, and already longer.

    Same inode, and the size never dips below what we had consumed, so
    neither the inode check nor the size check sees it: the follower used to
    resume mid-file and splice the new run's records onto the old index.
    The head-bytes signature is what catches it.
    """
    path = tmp_path / "log.jsonl"
    lines = _lines(RECORDS)
    path.write_bytes(lines[0] + lines[1])
    tail = Tail(path)
    assert len(tail.read_available()) == 2
    assert tail.generation == 0
    consumed = len(lines[0] + lines[1])

    second_run = [{**RECORDS[0], "run_id": "second-run"}, *RECORDS[1:]]
    blob = b"".join(_lines(second_run))
    assert len(blob) > consumed  # the size check cannot see this
    path.write_bytes(blob)
    assert path.stat().st_ino  # (same inode: write_bytes truncates in place)

    got = tail.read_available()
    assert tail.generation == 1
    assert [r.data for r in got] == second_run  # from the start, not spliced
    assert got[0].offset == 0
    tail.close()


def test_growing_from_an_empty_file_is_not_a_new_generation(tmp_path: Path):
    """The head signature starts short; growing past it is the same file."""
    path = tmp_path / "log.jsonl"
    path.write_bytes(b"")
    tail = Tail(path)
    assert tail.read_available() == []
    for line in _lines(RECORDS):
        with open(path, "ab") as f:
            f.write(line)
        tail.read_available()
    assert tail.generation == 0
    tail.close()


def test_missing_file_yields_nothing_then_recovers(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    tail = Tail(path)
    assert tail.read_available() == []
    path.write_bytes(_lines(RECORDS[:1])[0])
    assert [r.data for r in tail.read_available()] == RECORDS[:1]
    tail.close()


async def test_afollow_yields_appended_records(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    lines = _lines(RECORDS)
    path.write_bytes(lines[0])
    tail = Tail(path, poll_interval=0.01)
    agen = tail.afollow()
    try:
        first = await asyncio.wait_for(agen.__anext__(), timeout=2)
        assert first.data == RECORDS[0]

        def append_second() -> None:
            with open(path, "ab") as f:
                f.write(lines[1])

        await asyncio.to_thread(append_second)
        second = await asyncio.wait_for(agen.__anext__(), timeout=2)
        assert second.data == RECORDS[1]
    finally:
        await agen.aclose()
        tail.close()
