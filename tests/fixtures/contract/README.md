# Pinned run-log contract fixture

`run_small.jsonl` is a verbatim copy of the golden fixture published by accrue
core for its run-log contract (schema v1). It is the cross-repo handshake:
accrue-ui's contract tests run against this pinned copy, not against a live
checkout of accrue.

- Source repo: https://github.com/matt-house-e/accrue
- Source path: `tests/fixtures/run_small.jsonl`
- Copied at commit: `ff904baffec3a3c09c7fbcb503ce808994823de2`
- Contract spec: `docs/guides/run-log.md` in the source repo

Shape: a 12-row, 3-step run (`normalize` -> `score` -> `flag`). Row 7 errors
in `score` (ValueError); row 3 is skipped by `run_if` in `flag`. All steps are
function steps, so `model` is null and every `usage.cost` is null.

To refresh: re-copy the file from accrue main and update the commit SHA above.
Do not hand-edit the JSONL.
