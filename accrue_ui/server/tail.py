"""Poll-based follower for append-only JSONL run logs.

Polling on purpose — **no inotify, it breaks on WSL2** (see CLAUDE.md). The
follower stats the file every ~250ms, reads whatever bytes were appended, and
yields complete parsed records. Partial lines are buffered until their newline
arrives, so a write split mid-line never produces a broken or duplicated
record. If the file is truncated or replaced, the follower reopens from the
start and bumps :attr:`Tail.generation` so consumers know their derived state
is stale.

Detecting that takes more than an inode check and a size check. A log
truncated **and rewritten past the old read position** between two polls
keeps its inode and never looks smaller — the follower would resume mid-file
and splice the new run's records onto the old run's index (verified
corruption). So each open also records a *signature*: the inode plus the
file's first 64 bytes, which begin the `pipeline_start` record and so differ
between runs. The signature is re-checked every poll; a mismatch means a new
file in the same place, and the reopen + generation bump follows.

Every yielded record carries its byte offset and length in the file, which is
what lets the index re-read raw lines on demand instead of holding them all
in memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_POLL_INTERVAL = 0.25
#: Bytes of the file head kept as part of its identity (see the module docs).
SIGNATURE_BYTES = 64


@dataclass(frozen=True)
class TailRecord:
    """One parsed JSONL record plus its position in the file."""

    offset: int  # byte offset of the start of the line
    length: int  # byte length of the line, including its newline
    data: dict[str, Any]


class Tail:
    """Follow one JSONL file, yielding each complete record exactly once."""

    def __init__(self, path: str | Path, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self.path = Path(path)
        self.poll_interval = poll_interval
        #: Incremented every time the file is reopened from the start after
        #: the initial open (truncation or replacement). When this changes,
        #: previously yielded offsets no longer describe the current file and
        #: any index built from them must be rebuilt.
        self.generation = 0
        self._file: BinaryIO | None = None
        self._ino: int | None = None
        self._head = b""  # first SIGNATURE_BYTES bytes seen at open
        self._buffer = b""
        self._line_start = 0  # file offset where the buffered partial line begins
        self._read_pos = 0  # file offset up to which bytes have been consumed

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _read_head(self, size: int) -> bytes:
        """The file's first *size* bytes, without disturbing the read cursor."""
        assert self._file is not None
        fd = self._file.fileno()
        if hasattr(os, "pread"):
            return os.pread(fd, size, 0)
        here = self._file.tell()  # pragma: no cover - Windows fallback
        try:
            self._file.seek(0)
            return self._file.read(size)
        finally:
            self._file.seek(here)

    def _open(self) -> None:
        first = self._file is None and self._ino is None
        self.close()
        try:
            # Long-lived handle by design (poll follower); closed via close().
            self._file = open(self.path, "rb")  # noqa: SIM115
        except FileNotFoundError:
            self._file = None
            return
        self._ino = os.fstat(self._file.fileno()).st_ino
        self._head = self._read_head(SIGNATURE_BYTES)
        self._buffer = b""
        self._line_start = 0
        self._read_pos = 0
        if not first:
            self.generation += 1

    def _same_file(self) -> bool:
        """Does the open handle still hold the file we started following?

        The head bytes are the discriminator a truncate-and-regrow cannot
        fake: they carry the run id and start time of whichever run wrote
        the file. A head shorter than SIGNATURE_BYTES (the log had barely
        started) is upgraded in place once more bytes exist, since a longer
        prefix of the same content is still the same file.
        """
        assert self._file is not None
        head = self._read_head(SIGNATURE_BYTES)
        if len(self._head) < SIGNATURE_BYTES and head.startswith(self._head):
            self._head = head
            return True
        return head == self._head

    def read_available(self) -> list[TailRecord]:
        """Read newly appended bytes and return the complete records in them.

        Returns records exactly once each; a trailing partial line stays
        buffered until a later call sees its newline. Safe to call as often
        as you like.
        """
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            # File gone (mid-replacement, or not written yet). Nothing to
            # read; a future stat will trigger a reopen via the inode check.
            return []
        stale = (
            self._file is None
            or st.st_ino != self._ino
            or st.st_size < self._read_pos
            or not self._same_file()
        )
        if stale:
            self._open()
            if self._file is None:
                return []
        assert self._file is not None
        chunk = self._file.read()
        if not chunk:
            return []
        self._read_pos += len(chunk)
        buf = self._buffer + chunk if self._buffer else chunk

        # Walk the buffer with an index — no per-line re-slicing of the
        # remainder, which would be O(n^2) over a large initial read.
        records: list[TailRecord] = []
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            line = buf[start:nl]
            offset = self._line_start
            length = nl + 1 - start
            self._line_start += length
            start = nl + 1
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # tolerate junk lines; the log contract is per-line
            if isinstance(data, dict):
                records.append(TailRecord(offset, length, data))
        self._buffer = buf[start:]
        return records

    def follow(self, stop: threading.Event | None = None) -> Iterator[TailRecord]:
        """Blocking generator: poll forever (or until *stop* is set)."""
        while stop is None or not stop.is_set():
            records = self.read_available()
            yield from records
            if not records:
                if stop is not None:
                    stop.wait(self.poll_interval)
                else:
                    time.sleep(self.poll_interval)

    async def afollow(self) -> AsyncIterator[TailRecord]:
        """Async wrapper around the poll loop; cancel the task to stop."""
        while True:
            records = await asyncio.to_thread(self.read_available)
            for record in records:
                yield record
            if not records:
                await asyncio.sleep(self.poll_interval)
