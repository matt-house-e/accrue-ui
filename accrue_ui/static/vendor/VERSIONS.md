# Vendored frontend runtime

Committed, pinned, never fetched from a CDN at runtime. Downloaded from unpkg;
exact versions resolved from the unpkg redirect on 2026-08-19.

| File | Package | Exact version |
|------|---------|---------------|
| `preact.module.js` | `preact` | 10.29.8 |
| `hooks.module.js` | `preact` (hooks entry) | 10.29.8 |
| `signals.module.js` | `@preact/signals` | 2.11.1 |
| `signals-core.module.js` | `@preact/signals-core` | 1.14.4 |
| `htm.module.js` | `htm` | 3.1.1 |

To upgrade: re-download from `https://unpkg.com/<package>@<major>/dist/...`,
record the newly resolved versions here, and sanity-check each file is
JavaScript (not an HTML error page) before committing.
