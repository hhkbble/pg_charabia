# Coding-agent guide

## Inspect and choose the right document

1. Read actual HEAD, the working tree, and relevant source before editing. Preserve unrelated work and existing failure evidence.
2. Use the reading map below. Resolve conflicts against current source and executable contracts, and report specific mismatches.

| Task | Read first |
| --- | --- |
| Public SQL, configuration, installation, limits, maintenance, or backup requirements | [README.md](README.md). It owns the user contract. |
| Analysis, dictionary chains, headline, FFI, ownership, caching, or WAL mechanisms | [Architecture](docs/architecture.md), then the linked source and tests. |
| Build, debug, tests, failure diagnosis, cleanup, recovery rehearsal, or packaging | [Development](docs/development.md). |
| README examples | [tests/examples.py](tests/examples.py). Preserve its three ordered SQL blocks and independent assertions. |

Read versions and features from [Cargo.toml](Cargo.toml), [Cargo.lock](Cargo.lock), and [rust-toolchain.toml](rust-toolchain.toml). Read resource identities from [resources/manifest.json](resources/manifest.json), environment pins from [docker/environment.lock.json](docker/environment.lock.json), and suites/report rules from [report_contract.py](scripts/report_contract.py).

## Edit within scope

- Keep one extension crate. Preserve public behavior and dependencies unless the task authorizes a change.
- Keep each detailed topic in its assigned document. Link to its owner instead of copying parameter tables or procedures.
- Preserve licenses, notices, raw evidence, existing packages, and user files. Keep one-use validation adapters outside tracked files.
- For FFI changes, preserve RAII registration, the non-fallible pointer handoff, exact state types, END take, and unique reset destruction. Read [ownership and errors](docs/architecture.md#ownership-and-errors) before changing these boundaries.

## Validate and finish

1. Run the smallest relevant checks using real PostgreSQL for integration. Preserve independent expected results and the first failure.
2. If README SQL changes, execute the existing example test in an owned Linux AMD64 + PG18 environment. Keep known quality failures separate from mechanism success.
3. Save evidence before cleanup. Stop owned clusters and confirm process exit before removing PGDATA. If stopping fails, retain data and report failure. Follow [cleanup rules](docs/development.md#failure-diagnosis-and-cleanup).
4. Report changed files, commands, actual results, limitations, and cleanup. A targeted pass is not full acceptance.

Final release acceptance requires Linux AMD64 + PostgreSQL 18, with the current library built and loaded in that target. macOS, Linux ARM64, hooks, debug, or old-library results cannot replace it. Follow the [release procedure](docs/development.md#release-acceptance-and-packaging) before claiming delivery.
