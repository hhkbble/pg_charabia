# Development and validation

This guide is for contributors working in a Git source checkout. Start with the [README](../README.md) for a first query. The [Usage reference](reference.md) owns public configuration and limits. [Installation and maintenance](operations.md) owns installation, required index maintenance, and backup requirements. [Architecture](architecture.md) explains the implementation and ownership rules.

## Prerequisites and supported environments

Use Python 3.11 or later and Git on the host. Run repository commands from the checkout root. Use the versions/features in [Cargo.toml](../Cargo.toml), [Cargo.lock](../Cargo.lock), and [rust-toolchain.toml](../rust-toolchain.toml). Keep resource identities and notices in [resources/manifest.json](../resources/manifest.json).

| Environment | Prerequisites and purpose |
| --- | --- |
| macOS ARM64 | Homebrew PostgreSQL 18 binaries/headers, Rustup with the pinned toolchain/components, C compiler, LLVM/libclang, pkg-config, and OpenSSL development libraries. Local debug/development tests. |
| Linux AMD64 through Docker | A running Docker engine capable of `linux/amd64`, with emulation if needed. The pinned image supplies target tools, PostgreSQL, and pg_textsearch. Release acceptance. |

The native local runner requires macOS ARM64. The Docker runner targets Linux AMD64, regardless of host architecture. Neither provides Linux ARM64 extension validation. `PG_CONFIG` does not bypass platform or PostgreSQL-major checks. macOS results do not replace Linux AMD64 + PostgreSQL 18 acceptance.

First builds need network access for dependencies, model archives, and notices, or verified caches. [prepare-resources.py](../scripts/prepare-resources.py) validates real archives and seeds the dictionary build cache. `DOCS_RS` dummy resources are prohibited. Runtime analysis performs no downloads.

## Resource generation and identity

Ordinary builds use the committed English TXT and generate FSTs in `OUT_DIR`.
They prepare the 15 selected stopword tables with the same source module as
runtime. The host Charabia dependency has no features. The target keeps the
manifest's exact segmentation/normalization features. Check the resolved graph
instead of assuming host and target features were unified.

`stop-words` is a build dependency only. Runtime binds static FST views and does
not rebuild tables at INIT. The manifest records canonical keys, generated-byte
identities, source inputs, notices, and generator identities. The resource
verifier rejects missing or changed committed materials. Do not fix such a
failure by accepting a new hash without reviewing the cause.

To intentionally regenerate English data, first retain the exact inputs named
in `resources/english/sources.json`. Use the commands and attribution rules in
[the English data notice](../resources/english/NOTICE.md). The normal build never
downloads that large source. Run the small independent extraction tests with
`./scripts/check-english-generator`; full unit verification also runs them.

[German rebuild instructions](../resources/german/README.md) provide the pinned
source, all-line FST reconstruction, and recipient modification/relink procedure.
The binary archive includes the complete extension source and required build
materials. Its resources keep their own licenses, attribution, and source duties.
Keep large raw input archives outside Git with exact checksums and a declared
retention location. Do not make builds depend on local research or experiment
files.

## Build and test

Debug builds leave the extension unoptimized. [Cargo.toml](../Cargo.toml) optimizes only `whatlang`, with debug assertions, overflow checks, and debug information retained. This targets measured language-detection cost without changing release compilation or test budgets.

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
./scripts/verify-linux-amd64 --suite smoke,chain,headline,structured
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
| Local default | `unit,native,structured,chain,headline,config,lifecycle,errors` |
| Linux default / `--suite all` | `unit,smoke,native,structured,chain,bm25,headline,config,lifecycle,errors,stress,recovery,benchmark` |
| Local Linux-only selections | `smoke,bm25,stress,recovery,benchmark` are rejected. Local `all` also fails rather than silently dropping them. |
| Full Linux default | Also runs fmt, Clippy, schema, ABI, default-object, loaded-library, and README/reference example checks. |
| Targeted Linux run | Runs the selected suites and shared build/identity checks. It does not automatically run README examples or full-only lint checks. |

[tests/examples.py](../tests/examples.py) extracts exactly three README `sql` fences, then the two `sql` fences in the [Usage reference](reference.md). It executes all five in that order in one owned empty database. The reference examples extend the README's native quick start and custom configuration. They do not require BM25 for manual use. Preserve the test's independent assertions for installation, native queries, COPY/ALTER, filtering, actual BM25 Index Scan, and headline. The report records each document's hash and each block's source and number. The test removes its database on success or failure.

There is no `examples` suite or examples-only wrapper flag. Full Linux and extracted-package validation invoke the test automatically. For a documentation-only change, first build, stage, and load the current library:

```sh
./scripts/verify-linux-amd64 --suite build
```

Then use a one-use adapter to stage that same verified library in a private Linux AMD64 + PG18 cluster in the same image. Keep the adapter outside the repository. Mount current documents/tests and the staging package read-only, and use an owned UTF8 cluster and Unix socket without a published port. Clear inherited libpq defaults with [isolated_pg_environment](../scripts/report_contract.py). Supply the owned libpq environment, `PG_CONFIG`, `PGCB_PLATFORM=linux-amd64`, and `PGCB_RUN_DIR` required by [support.py](../tests/support.py). Preload the pinned pg_textsearch before startup. Check its version and source identity, extension dependencies, static identity, library hash, and actual backend mappings. Invoke `python3 /workspace/tests/examples.py` in that environment. Save evidence and follow the [cleanup rules](#failure-diagnosis-and-cleanup). Record the actual loaded library and cleanup separately from full release acceptance.

Validate Markdown structure, local links/anchors, shell fences with `bash -n`, and flags against source/`--help`. Compare README and reference SQL with the previous working-tree content. If executable examples change, run them. Preserve existing documentation-test safeguards rather than redesigning the runner.

## Test organization and expectations

| Tests | What they establish |
| --- | --- |
| Rust module tests | Real Charabia/stemmer calls, Unicode/key preparation, finite model mappings, bounds, stopword source priority, and result invariants. |
| [native.py](../tests/native.py), [chain.py](../tests/chain.py), SQL fixtures | Real Parser/Dictionary/vector/query calls, standard chains, positions, and GIN behavior. Controlled external dictionaries test the PG protocol. |
| [bm25.py](../tests/bm25.py), [corpus.json](../tests/fixtures/bm25/corpus.json) | Independent domain candidates, native-term overlap consistency, actual BM25 plans, CRUD, and maintenance paths. |
| [structured.py](../tests/structured.py) | Native-only ASCII recognition, full/component groups, independent literal vectors, original spans, chains, GIN CRUD, parameters and expansion budgets. It runs on both supported environments. |
| [headline.py](../tests/headline.py), [config.py](../tests/config.py) | Original text, options, external chains, permissions, native DDL, shared dictionaries, and logical restore. |
| [errors.py](../tests/errors.py), [lifecycle.py](../tests/lifecycle.py), [stress.py](../tests/stress.py) | Registration/cache failure, error/cancel recovery, prepared plans, concurrent snapshots, actual worker execution, and resource stability. |
| [recovery.py](../tests/recovery.py), [benchmark.py](../tests/benchmark.py) | Controlled physical recovery and recorded performance/ranking observations. |

Keep algorithm examples, recorded probe observations, explicit extension contracts, domain expectations, and index-consistency checks distinct. A fixture's recorded observation is not an independent upstream guarantee. Never generate new expected values from the extension and accept them without review. Finite-state checks and test counts do not establish natural-language accuracy.

Format-2 reports separate mechanism, quality, and cleanup.
`structured_token_quality_costs` retains disclosed atomic structured token false positives outside quality PASS totals.
It does not expand the required-domain failure exemptions. Packages retain these observations.
[quality-known.json](../tests/fixtures/quality-known.json) preserves three original
domain failures and their exact signatures. The current report contract explicitly
closes `den` and `var` after reviewed native/index evidence. Their original records
stay unchanged; returning to the old false result is now a regression. The phrase
case remains unresolved. A diagnostic control or new failure cannot expand the
exemption set.

The [reviewed release warning policy](#reviewed-release-warnings) defines the temporary packaging exceptions separately from those frozen cases.
Version 0.3.1 retains the exact exceptions introduced in v0.3.0.

Quality summaries include separate `passed`, `known-failure`, `warning`, and `regression` counts. Their `warnings` list and `warning_review` identity are retained in reports and the delivery manifest. Full acceptance requires every named reviewed case, not a matching total. The extracted-package verifier checks the original warning evidence and preserves the full acceptance summary.

- `verified` means the selected mechanisms and cleanup passed without warnings or quality-registry failures. Structured token precision costs remain separately reported.
- `verified-with-known-quality-failures` permits packaging with exact frozen failures. They remain failures, not passed quality cases.
- `verified-with-quality-warnings` permits packaging with exact reviewed warnings. Any frozen failures remain separately listed. Neither category counts as quality PASS.
- New differences, changed warning signatures, complete-candidate loss, mechanism failure, missing identity, unexplained resource growth, or cleanup failure block success and packaging.
- Invalid selections exit 2. Failures and interruptions exit nonzero.

Review unexpected improvement before changing a frozen case. Report explicit Discard=false recovery separately from default failures. Native term overlap checks BM25 consistency, not domain recall. An index plan does not prove relevance or ranking quality.

Keep hooks and release evidence separate and traceable to the same final commit. Hooks exercise allocation/link registration, INIT/LEXIZE, and headline fault points. Release tests cover actual errors, timeout/cancel, concurrency, and resource behavior. Each build has its own library hash. Registered failed-INIT state follows dictCtx reset, not an assumed transaction-abort drop.

Resource tests preserve the fixed warmup/control/measurement protocol and existing context/RSS gates. State/callback counts must reflect actually initialized dictionaries. Separate
owned FST bytes (zero per Dictionary), logical references to static bytes, unique
embedded bytes, and ordinary state/stemmer allocations. A reset releases a view
and state, not the static data. Reference counters are not resident-memory
measurements. Do not relax thresholds to obtain a pass. Worker-launch counts alone do not prove analysis ran in workers.

Performance observations separate detection, ASCII scanning/validation, component expansion, preparation, FST cold/warm cost, filtering, dual terms, index build/size, writes, queries, and headline. Preserve raw samples and relevance labels. Scores and ranks are separate observations. Label emulated results and avoid unsupported SLA claims.

### Reviewed release warnings

Release v0.3.0 introduced temporary acceptance of 47 reviewed BM25 differences as `warning`.
Release v0.3.1 retains exactly the same 47 cases and signatures.
They cover extra candidates across prepared plans and index lifecycle stages,
German `die` false matches, and the confirmed TopK ranking failure.
These records remain quality failures and do not count as quality PASS.
See the [Usage reference](reference.md#known-bm25-issues) for their effect on users.

Only the cases explicitly named in [report_contract.py](../scripts/report_contract.py)
qualify. Every case ID, input, query, configuration, expected result, and actual
result must match the reviewed record exactly. Names, prefixes, and record
counts cannot grant a warning. Any signature change needs review and blocks
release under this policy.

The complete [original failure report](../tests/fixtures/quality-warnings-original.json)
is retained byte for byte from run `20261023T221121Z-73050-amd64-inner`.
Its SHA256 is pinned by the report contract. The release archive includes that
original report and the current warning records through its source,
verification evidence, and delivery manifest.

The warning policy does not expand the existing frozen phrase exception or
change the reviewed `den` and `var` resolutions. The TopK warning does not
disable complete-candidate checks, actual index execution, LIMIT checks, or
headline assertions. All blocking conditions in
[Test organization and expectations](#test-organization-and-expectations) still apply.

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

User backup/restore actions belong in [Backup and recovery](operations.md#backup-and-recovery). The `recovery` suite validates them with the current staged library, not a separate prototype.

Run `./scripts/verify-linux-amd64 --suite recovery` in the supported target environment. [recovery.py](../tests/recovery.py) creates separate source, backup, archive, restore, socket, and staging directories under the run's owned temporary root. It enables logged writes, fsync, full-page writes, archive WAL, and pg_textsearch preload without modifying a host installation.

The test performs this sequence:

1. Take online `pg_basebackup -X stream` and run `pg_verifybackup`.
2. Commit distinguishable linguistic and structured Dictionary parameters, mapping/chain, and row changes. Recompute vectors and maintain indexes. Retain a disk segment and unspilled BM25 documents.
3. Roll back a parameter change. Establish a named restore point, commit negative controls afterward, switch WAL, and confirm archival.
4. Validate source crash restart. Restore the base backup into another owned directory using archived WAL, stop at the named target, and promote.
5. Compare catalog state, independent IDs, stored vectors, cold INIT, expression/stored GIN, actual BM25 scans, and original-text headline, including component hits on whole structured tokens. Include actual Chinese terms, not only Latin terms in mixed text.
6. Compare BM25 durable summaries with the target snapshot, not live heap row counts. Validate segment and memtable paths.
7. Demonstrate that the external extension library is absent from the base backup. A fresh backend fails when it is unavailable and works after the same-hash file returns.

The report keeps SQL, plans, LSN/timeline, archive checksums, library maps/hashes, source/restore observations, and cleanup. A missing recovery child report prevents the parent from deleting possibly active PGDATA. Controlled error/interruption checks must prove child cleanup as well as outer cleanup.

This establishes the tested recovery paths, not a high-availability platform, logical replication, or every possible crash timing. No test bypasses a required package identity mismatch with the latest library.

## Release acceptance and packaging

Complete implementation, tests, and static documentation before freezing a clean source commit. Run default full Linux release acceptance, the local macOS development flow, and separate same-commit hooks checks. The actual current library must build/load in Linux AMD64 + PostgreSQL 18.

Apply the [reviewed release warning policy](#reviewed-release-warnings) when assessing quality results for packaging.

The packager requires the complete successful release report, clean tested/current checkouts, the same commit, lock/manifest, and actual loaded-library hash. Partial suites, missing recovery evidence, old report formats, hooks/debug builds, and existing output names are rejected. No public upload or deployment occurs.

Source documents, scripts, and tests come from the tested Git commit. Ignored and local-only source files stay outside the archive and remain untouched.

Replace the uppercase placeholders with the completed outer run directory and resulting archive name:

```sh
./scripts/package-release --run artifacts/FINAL_RUN
./scripts/verify-linux-amd64 --archive dist/ARCHIVE.tar.gz
```

The archive contains the verified library, control/SQL, resources/notices,
English TXT/attribution, manifests, `SHA256SUMS`, and evidence. It also includes
the committed extension source, lockfile, build script, SQL, resource tools, and
container build files for rebuilding and relinking. It also supplies the exact hash-checked Charabia and fst crate source archives
under `dependency-sources/`, including the original German source/FST. It does
not bundle huge raw source inputs or claim an offline cache of every dependency. The extracted-package verifier checks checksums and loads the extracted library without compiling a replacement. It executes smoke, structured, chain, headline, configuration, recovery, and the archive's README and reference examples. `--archive` cannot combine with suite, hooks, or debug selections.

After release content changes, freeze the clean final source commit again and rerun the required same-commit acceptance before packaging. An amended candidate has a new identity and needs new acceptance and packaging evidence. Do not overwrite old archives or evidence. To rebuild from an extracted archive, enter its root and use the same prerequisites
and pinned toolchain. The archive has source files but no `.git` directory. A
recipient can create a new local Git checkout before using the evidence runners.
A direct isolated build can instead package without Git metadata:

```sh
: "${PG_CONFIG:?Set PG_CONFIG to the target PostgreSQL 18 pg_config}"
cargo pgrx package --pg-config "$PG_CONFIG" --out-dir /path/to/owned-stage \
  --prefix-dir extension --no-default-features --features pg18 --cargo=--locked
```

This creates a new recipient build. It does not replace extracted-library
verification or inherit the original delivery's tested commit/hash identity.

Keep the delivery report in `artifacts/` or `dist/`, recording source/package identity, tests, quality failures, performance scope, recovery scope, and cleanup. Later tracked-file edits cannot retain an earlier tested commit identity.

### GitHub publication

The packager creates local files. GitHub upload and publication happen separately. The [v0.3.1 release page](https://github.com/hhkbble/pg_charabia/releases/tag/v0.3.1) is the stable download entry point. Archive filenames include the tested commit, so documentation links to the version page.

1. Finish full Linux acceptance, local macOS development validation, and separate hooks checks on the same clean final commit.
2. Generate the archive and SHA256 file with the existing packager, then pass extracted-package validation.
3. Push the final tested commit and a matching version tag. Preserve unrelated remote commits, tags, and assets.
4. Create a draft Release for the existing tag. Upload the complete packager archive and its SHA256 file without changing their structure.
5. Download the draft assets and compare their hashes, source commit, manifest, and verification records with the tested package.
6. Publish the Release with its actual acceptance results, quality warnings, and frozen failures. Check the public tag, download links, and assets again.

Keep the binary inside the complete archive. GitHub's automatic source attachments do not replace that archive. A release must not claim support or production readiness beyond the recorded validation.
