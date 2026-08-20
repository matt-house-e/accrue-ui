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

## `run_captured.jsonl` (+ `run_captured.prompts.jsonl`)

The v0.2 capture-tier fixture: a 3-row, single-`classify` run captured with
`capture="prompts"`, so it carries real `row_attempt` records and a prompt
sidecar. Row 0 succeeds first try; row 1 hits a `rate_limited` api attempt then
retries (an `api` attempt followed by a `parse` attempt); row 2 hits a
`parse_error` then retries. Each captured attempt's `prompt_ref` points at a
`{messages, response, parsed}` body in `run_captured.prompts.jsonl` by byte
offset — the pair drives the attempt-timeline and Prompt/Response inspector
tabs (accrue-ui #14).

- Source path: `tests/fixtures/run_captured.jsonl` (+ `.prompts.jsonl`)
- Copied at commit: `79f0e06b068139c72cc880a117c5aad28d3e1c6d`

To refresh: re-copy the file(s) from accrue main and update the commit SHAs
above. Do not hand-edit the JSONL.
