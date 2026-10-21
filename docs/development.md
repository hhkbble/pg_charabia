# Development and validation

This guide is for contributors working in a Git source checkout. The [README](../README.md) owns installation, public configuration, user limits, and required index maintenance. [Architecture](architecture.md) explains the implementation and ownership rules.

## Prerequisites and supported environments

Use Python 3.11 or later and Git on the host. Run repository commands from the checkout root. Use the versions/features in [Cargo.toml](../Cargo.toml), [Cargo.lock](../Cargo.lock), and [rust-toolchain.toml](../rust-toolchain.toml). Keep resource identities and notices in [resources/manifest.json](../resources/manifest.json).

| Environment | Prerequisites and purpose |
| --- | --- |
| macOS ARM64 | Homebrew PostgreSQL 18 binaries/headers, Rustup with the pinned toolchain/components, C compiler, LLVM/libclang, pkg-config, and OpenSSL development libraries. Local debug/development tests. |
| Linux AMD64 through Docker | A running Docker engine capable of `linux/amd64`, with emulation if needed. The pinned image supplies target tools, PostgreSQL, and pg_textsearch. Release acceptance. |

The native local runner requires macOS ARM64. The Docker runner targets Linux AMD64, regardless of host architecture. Neither provides Linux ARM64 extension validation. `PG_CONFIG` does not bypass platform or PostgreSQL-major checks. macOS results do not replace Linux AMD64 + PostgreSQL 18 acceptance.

First builds need network access for dependencies, model archives, and notices, or verified caches. [prepare-resources.py](../scripts/prepare-resources.py) validates real archives and seeds the dictionary build cache. `DOCS_RS` dummy resources are prohibited. Runtime analysis performs no downloads.

## Build and test

### macOS ARM64

Set explicit paths for the installed packages. This example requires Homebrew packages `postgresql@18` and `llvm`:

```sh
export PG_CONFIG="$(brew --prefix postgresql@18)/bin/pg_config"
export LIBCLANG_PATH="$(brew --prefix llvm)/lib"
./scripts/dev-test --suite build
./scripts/dev-test --suite native,chain,headline,config
./scripts/dev-test
```

The runner provisions its pinned cargo-pgrx under its cache. It packages/stages the extension and creates a private cluster and Unix socket. It does not install into the PostgreSQL distribution or change an existing database/service. The local package uses the debug profile.

`--suite build` still builds the package, generates schema, creates a cluster, and loads the real extension. It is not a compile-only command. A targeted suite run also builds/stages and checks runtime identity before its tests.

### Linux AMD64

[run-linux.py](../scripts/run-linux.py) builds the image from [verify.Dockerfile](../docker/verify.Dockerfile) and [environment.lock.json](../docker/environment.lock.json). It mounts source read-only, uses separate cache/evidence paths, and runs PostgreSQL as a non-root user. It publishes no database port and mounts no host PGDATA.

```sh
# Early real FTS/BM25/headline integration.
./scripts/verify-linux-amd64 --suite smoke,chain,headline
# Targeted fault injection, with separate evidence.
./scripts/verify-linux-amd64 --suite errors,lifecycle,stress --test-hooks --debug --no-build-image
# Physical backup and target recovery with the staged library.
./scripts/verify-linux-amd64 --suite recovery --no-build-image
# Full release acceptance.
./scripts/verify-linux-amd64
```

Use `--no-build-image` only after validating the existing image against repository pins. It skips the image build, not extension compilation, package staging, library checks, or cluster startup. The default Linux profile is release. `--debug` selects a diagnostic build and cannot provide release evidence.

The runner builds and loads Linux AMD64 code inside that target, including under emulation. It does not use a macOS library or a cross-compiled artifact as proof of target execution. Base images and source identities are fixed. Resolved system package versions are recorded per run, not claimed to be bit-identical across package-repository changes.

### Controls and cache locations

| Variable | Supported scope |
| --- | --- |
| `PG_CONFIG` | Explicit PostgreSQL 18 pg_config for the local runner. Its bindir supplies server/client binaries. |
| `LIBCLANG_PATH` | Explicit local libclang directory. |
| `PGCB_CACHE` | Local cache root, including cargo-pgrx tooling and default build caches. |
| `PGCB_TARGET_BASE` | Local parent for target build caches. |
| `LINDERA_BUILD_DICTIONARY_CACHE_DIR` | Local dictionary-build cache. |
| `PGCB_RESOURCE_ARCHIVES` | Local source-archive directory used by resource preparation. |
| `PGCB_ARTIFACTS`, `PGCB_RUN_ID` | Local evidence root and unique run ID. An existing evidence directory is not overwritten. |
| `CARGO_BUILD_JOBS` | Local build concurrency. |
| `PGCB_LINUX_CACHE` | Host bind-mounted cache root for the Docker wrapper. Linux evidence remains under `artifacts/`. |

The Docker wrapper does not forward local runner environment controls. The image sets container paths. Cache keys separate target OS/CPU, PostgreSQL, toolchain, lock file, and features. Never store database data in these caches.

Inherited libpq connection defaults are removed by [isolated_pg_environment](../scripts/report_contract.py). The runner sets its owned socket/database explicitly, preventing a host/service setting from redirecting tests.

## Suites and executable documentation

[report_contract.py](../scripts/report_contract.py) is the shared source for suite selection, report format, and packaging requirements. Both wrappers accept repeated `--suite` flags or comma-separated names. Unknown, duplicate, unimplemented, or unsupported selections fail before starting the environment. `build` must be selected alone.

| Selection | Actual behavior |
| --- | --- |
| Local default | `unit,native,chain,headline,config,lifecycle,errors` |
| Linux default / `--suite all` | `unit,smoke,native,chain,bm25,headline,config,lifecycle,errors,stress,recovery,benchmark` |
| Local Linux-only selections | `smoke,bm25,stress,recovery,benchmark` are rejected. Local `all` also fails rather than silently dropping them. |
| Full Linux default | Also runs fmt, Clippy, schema, ABI, default-object, loaded-library, and README-example checks. |
| Targeted Linux run | Runs the selected suites and shared build/identity checks. It does not automatically run README examples or full-only lint checks. |

[tests/examples.py](../tests/examples.py) extracts exactly three README `sql` fences and executes them in order in an owned empty database. Preserve its independent assertions for installation, native queries, COPY/ALTER, filtering, actual BM25 Index Scan, and headline. The test removes that database on success or failure.

There is no `examples` suite or examples-only wrapper flag. Full Linux and extracted-package validation invoke the test automatically. For a documentation-only change, a one-use adapter can stage the same verified library in a private Linux AMD64 + PG18 cluster. Then invoke the existing example test. Keep that adapter outside tracked files. Supply the owned libpq environment, `PG_CONFIG`, `PGCB_PLATFORM`, and `PGCB_RUN_DIR` required by [support.py](../tests/support.py). Preload the pinned pg_textsearch for the optional BM25 block. Record the actually loaded library and cleanup separately from full release acceptance.

Validate Markdown structure, local links/anchors, shell fences with `bash -n`, and flags against source/`--help`. Compare README SQL with the previous working-tree content. If executable examples change, run them. Preserve existing documentation-test safeguards rather than redesigning the runner.

## Test organization and expectations

| Tests | What they establish |
| --- | --- |
| Rust module tests | Real Charabia/stemmer calls, Unicode/key preparation, finite model mappings, bounds, stopword source priority, and result invariants. |
| [native.py](../tests/native.py), [chain.py](../tests/chain.py), SQL fixtures | Real Parser/Dictionary/vector/query calls, standard chains, positions, and GIN behavior. Controlled external dictionaries test the PG protocol. |
| [bm25.py](../tests/bm25.py), [corpus.json](../tests/fixtures/bm25/corpus.json) | Independent domain candidates, native-term overlap consistency, actual BM25 plans, CRUD, and maintenance paths. |
| [headline.py](../tests/headline.py), [config.py](../tests/config.py) | Original text, options, external chains, permissions, native DDL, shared dictionaries, and logical restore. |
| [errors.py](../tests/errors.py), [lifecycle.py](../tests/lifecycle.py), [stress.py](../tests/stress.py) | Registration/cache failure, error/cancel recovery, prepared plans, concurrent snapshots, actual worker execution, and resource stability. |
| [recovery.py](../tests/recovery.py), [benchmark.py](../tests/benchmark.py) | Controlled physical recovery and recorded performance/ranking observations. |

Keep algorithm examples, recorded probe observations, explicit extension contracts, domain expectations, and index-consistency checks distinct. A fixture's recorded observation is not an independent upstream guarantee. Never generate new expected values from the extension and accept them without review. Finite-state checks and test counts do not establish natural-language accuracy.

Format-2 reports separate mechanism, quality, and cleanup. [quality-known.json](../tests/fixtures/quality-known.json) freezes three unresolved domain cases. Their inputs, queries, configurations, expected values, and actual failure signatures must remain exact. A diagnostic control or a new failure cannot expand that set.

- `verified` means the selected mechanisms and cleanup passed without reported quality failures.
- `verified-with-known-quality-failures` permits local delivery only for exact frozen failures. They remain failures, not passed quality cases.
- New regressions, unexpected changes, missing identity, unexplained resource growth, or cleanup failure block success and packaging.
- Invalid selections exit 2. Failures and interruptions exit nonzero.

Review unexpected improvement before changing a frozen case. Report explicit Discard=false recovery separately from default failures. Native term overlap checks BM25 consistency, not domain recall. An index plan does not prove relevance or ranking quality.

Keep hooks and release evidence separate and traceable to the same final commit. Hooks exercise allocation/link registration, INIT/LEXIZE, and headline fault points. Release tests cover actual errors, timeout/cancel, concurrency, and resource behavior. Each build has its own library hash. Registered failed-INIT state follows dictCtx reset, not an assumed transaction-abort drop.

Resource tests preserve the fixed warmup/control/measurement protocol and existing context/RSS gates. State, callback, and FST counts must reflect actually initialized dictionaries. Do not relax thresholds to obtain a pass. Worker-launch counts alone do not prove analysis ran in workers.

Performance observations separate detection, preparation, FST cold/warm cost, filtering, dual terms, index build/size, writes, queries, and headline. Preserve raw samples and relevance labels. Scores and ranks are separate observations. Label emulated results and avoid unsupported SLA claims.

## Failure diagnosis and cleanup

1. Find the first unexpected failure in the run report. Read its command, original input/SQL, server log, and actual plan before retrying.
2. Identify the layer: analysis, FFI, configuration, planner, upstream, or harness. Preserve the first failure and fix only the affected scope.
3. Record commit/dirty state, lock/features/resources, image, PostgreSQL/upstream versions, loaded-library path/architecture/dependencies/hash, exit status, and assertions. For recovery, also record WAL checksums, target LSN, and timeline.
4. Save evidence before cleanup. Cancel clients, join reader/test threads, stop every owned cluster, and confirm process exit.
5. Validate ownership markers before deleting owned PGDATA, sockets, temporary files, recovery directories, containers, and anonymous volumes.
6. If stopping fails, retain PGDATA and report cleanup failure. Outer container removal does not establish inner cleanup. Preserve user data/services, existing packages, raw failures, and declared caches.

The Docker wrapper removes its container only after a final matching inner report proves ownership and cleanup, including recovery children. If proof is absent or invalid, it keeps the stopped container and volumes, records `container_retained` and `retention_reason`, and exits with `cleanup-failed`. Preserve that evidence and recover the owned data before manually completing cleanup. A stopped container alone does not prove that its inner cleanup procedure finished.

A targeted pass is not full release acceptance. Report what ran and what remains untested. Do not overwrite expected values, broaden known-failure rules, or reinterpret cleanup failure as success.

[check-runner-cleanup](../scripts/check-runner-cleanup) exercises owned success/failure/interruption paths when runner cleanup changes. Read its supported platform/probe behavior before use. It is not a replacement for an individual recovery child's cleanup record.

## Recovery validation

User backup/restore actions belong in the [README](../README.md#backup-and-recovery-requirements). The `recovery` suite validates them with the current staged library, not a separate prototype.

Run `./scripts/verify-linux-amd64 --suite recovery` in the supported target environment. [recovery.py](../tests/recovery.py) creates separate source, backup, archive, restore, socket, and staging directories under the run's owned temporary root. It enables logged writes, fsync, full-page writes, archive WAL, and pg_textsearch preload without modifying a host installation.

The test performs this sequence:

1. Take online `pg_basebackup -X stream` and run `pg_verifybackup`.
2. Commit distinguishable Dictionary parameter, mapping/chain, and row changes. Recompute vectors and maintain indexes. Retain a disk segment and unspilled BM25 documents.
3. Roll back a parameter change. Establish a named restore point, commit negative controls afterward, switch WAL, and confirm archival.
4. Validate source crash restart. Restore the base backup into another owned directory using archived WAL, stop at the named target, and promote.
5. Compare catalog state, independent IDs, stored vectors, cold INIT, expression/stored GIN, actual BM25 scans, and original-text headline. Include actual Chinese terms, not only Latin terms in mixed text.
6. Compare BM25 durable summaries with the target snapshot, not live heap row counts. Validate segment and memtable paths.
7. Demonstrate that the external extension library is absent from the base backup. A fresh backend fails when it is unavailable and works after the same-hash file returns.

The report keeps SQL, plans, LSN/timeline, archive checksums, library maps/hashes, source/restore observations, and cleanup. A missing recovery child report prevents the parent from deleting possibly active PGDATA. Controlled error/interruption checks must prove child cleanup as well as outer cleanup.

This establishes the tested recovery paths, not a high-availability platform, logical replication, or every possible crash timing. No test bypasses a required package identity mismatch with the latest library.

## Release acceptance and packaging

Complete implementation, tests, and static documentation before freezing a clean source commit. Run default full Linux release acceptance, the local macOS development flow, and separate same-commit hooks checks. The actual current library must build/load in Linux AMD64 + PostgreSQL 18.

The packager requires the complete successful release report, clean tested/current checkouts, the same commit, lock/manifest, and actual loaded-library hash. Partial suites, missing recovery evidence, old report formats, hooks/debug builds, and existing output names are rejected. No public upload or deployment occurs.

Source documents, scripts, and tests come from the tested Git commit. Ignored and local-only source files stay outside the archive and remain untouched.

Replace the uppercase placeholders with the completed outer run directory and resulting archive name:

```sh
./scripts/package-release --run artifacts/FINAL_RUN
./scripts/verify-linux-amd64 --archive dist/ARCHIVE.tar.gz
```

The archive contains the verified library, control/SQL, resources/notices, manifests, `SHA256SUMS`, and evidence. It is a binary delivery, not a complete source checkout. The extracted-package verifier checks checksums and loads the extracted library without compiling a replacement. It executes smoke, chain, headline, configuration, recovery, and the archive's README examples. `--archive` cannot combine with suite, hooks, or debug selections.

After release content changes, form a new final commit and rerun full acceptance before packaging. Do not overwrite old archives or evidence. Keep the delivery report in `artifacts/` or `dist/`, recording source/package identity, tests, quality failures, performance scope, recovery scope, and cleanup. Later tracked-file edits cannot retain an earlier tested commit identity.
