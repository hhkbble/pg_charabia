# Current architecture

pg_charabia is one Rust extension crate for PostgreSQL 18. It supplies text analysis through standard PostgreSQL Text Search objects. Indexes and queries consume that interface rather than an extension-specific index format.

The [README](../README.md) owns public parameters, limits, installation, and required maintenance. [Development](development.md) owns builds, tests, debugging, and release validation.

## Components and identities

[Cargo.toml](../Cargo.toml) and [Cargo.lock](../Cargo.lock) define dependencies and enabled features. [rust-toolchain.toml](../rust-toolchain.toml) defines the compiler. [resources/manifest.json](../resources/manifest.json) identifies embedded data, source archives, checksums, and notices. [environment.lock.json](../docker/environment.lock.json) pins the Linux acceptance environment and optional pg_textsearch source.

| Component | Responsibility |
| --- | --- |
| [analysis.rs](../src/analysis.rs) | `segment`, `prepare`, `inspect`, and `normalize`. Validated spans and bounded Dictionary analysis. |
| [language.rs](../src/language.rs) | `complete` and `LanguageSource`. Reuse detected language and complete missing metadata. |
| [routes.rs](../src/routes.rs) | `LanguageId` and `TokenType`. Fixed language, script, stopword-code, algorithm, and type mappings. |
| [stopwords.rs](../src/stopwords.rs) | `build` creates one effective stopword set for a Dictionary instance. |
| [parser.rs](../src/parser.rs) | START/GETTOKEN/END/LEXTYPES callbacks and the standard HEADLINE callback. |
| [dictionary.rs](../src/dictionary.rs) | INIT validates options and owns state. LEXIZE analyzes one supplied token. |
| [headline.rs](../src/headline.rs) | Strict option parsing, HTML escaping, and output-size accounting. |
| [pg_ffi.rs](../src/pg_ffi.rs) | PG allocation, Datum conversion, guarded calls, callback ownership, query rewriting, and headline projection. |
| [lib.rs](../src/lib.rs) | Module registration and the diagnostic/query functions. |
| [pg_charabia.sql](../sql/pg_charabia.sql) | Public SQL signatures, default objects, permissions, and callback registration. |

The default installation has one Parser, one Template, 71 language dictionaries, and `public.charabia`. Ordinary user configurations can share these dictionaries or use other standard dictionaries. There is no configuration table, proprietary object manager, global derived Dictionary cache, or cross-dictionary private payload.

```text
Original UTF-8 text
  -> Parser: segment once, complete missing language, classify raw spans
  -> PostgreSQL Configuration mapping for each token type
  -> Dictionary chain: NULL / Stop / terminal terms / filter replacement
  -> native tsvector or tsquery
       -> native FTS and GIN
       -> pg_textsearch BM25 analysis and index
       -> PostgreSQL headline matching against original spans
          -> HEADLINE callback: selector projection, escape, output budget
          -> PostgreSQL generates the result
```

## Segmentation, language, and raw spans

`analysis::segment_counted` uses a default Charabia Tokenizer's `segment` iterator once. It does not run a second tokenizer for Dictionary preparation. Enabled features exclude German compound segmentation and Swedish recomposition. German compounds therefore do not receive that dictionary split. Swedish final normalization uses accent folding, while preparation preserves accents for stemming.

Each `Span` stores byte/character ranges, script, original Charabia language, selected route, token type, and language source. Validation requires nonempty contiguous UTF-8 ranges, unchanged raw bytes, correct scalar counts, and complete input coverage. Offsets always refer to original text.

`language::complete` first reuses existing Charabia language. For missing metadata, 17 scripts with one model language use direct mappings. Missing Latin, Cyrillic, Hebrew, or Devanagari metadata triggers one `whatlang::detect_lang` call for the original contiguous script group. `Script::Other` text uses `und`. Missing metadata on an unexpected path, or a script/language mismatch, is an invariant error.

There is no project confidence gate, English fallback, separate detection window, per-token detection, or second normalization context. All 70 model languages remain candidates. The tests in `language.rs` and `analysis.rs` validate the fixed model/grouping assumptions. They do not prove natural-language detection accuracy.

The Parser classifies raw spans using the public compatibility-decomposition, control-character, and classifier prefix. It returns original bytes rather than that prefix's output. Language types occupy IDs 1 through 71, `blank` is 72, and `display_only` is 73. Oversized raw tokens become bounded display fragments, preserving source bytes without indexing word fragments by default.

START receives text and length, not configuration identity or Dictionary options. PostgreSQL maps types to the active chain. LEXIZE receives neither Parser type nor detected language. A Dictionary therefore uses its own `Language`, even if the mapping chooses a different language from the Parser's type.

## Dictionary analysis

`DictionaryState` owns validated `analysis::Options`, an optional stopword FST, and an optional stemmer. An FST is a finite-state transducer used here as a word set. Disabling Discard avoids building that set. Disabling Stem avoids creating a stemmer. The two switches are independent.

For each token supplied by PostgreSQL, `analysis::inspect` follows this order:

1. Validate the input independently of START. Direct `ts_lexize` and earlier filters can supply different bytes.
2. Run non-lossy Charabia normalization/classification, then NFC, language-specific lowercase, then NFC again. NFC composes canonically equivalent Unicode sequences.
3. Return Ignore for a non-word or empty prepared key.
4. Match the prepared key against this Dictionary's stopword set. A match returns Stop before stemming.
5. Keep a surface key and, when eligible, compute one stem from the prepared key.
6. Apply the selected finalization to surface and stem independently. Remove empty and duplicate terms.
7. Return terminal alternatives, one filter replacement, or Ignore when no term remains.

Turkish preparation maps `I` to dotless `ı` and `İ` to `i`. Other characters follow Unicode lowercase. This happens before stopword lookup and stemming. Feeding an already lossy-folded surface into the stemmer changes the algorithm input.

`stopwords::build` uses the per-language mapping and the dependency's NLTK-first, ISO-for-missing-language lookup. It prepares source entries with the same rules as tokens, excludes non-word/empty entries, sorts/deduplicates, and builds the FST. It neither merges all languages nor splits source entries through another tokenizer.

With Normalize disabled, surface retains the NFC prepared key and stem receives NFC. With Normalize enabled, both use Charabia's full finalizer. Finalization is script/language dependent. It is not a promise that every output is NFC or that every accent is removed.

The prepared stem-input bound applies after Unicode preparation. A token outside that bound or containing other character classes keeps its surface without invoking the algorithm. This bounds long stemmer input without changing segmentation.

The retained [direct Dictionary cases](../tests/fixtures/analysis/general/direct-lexize.json), `analysis.rs` tests, and `stopwords.rs` tests separate upstream examples, recorded observations, and extension-specific contracts.

## Dictionary chains and positions

`pg_ffi::lexemes` translates the owned Rust result into PG-allocated, zero-terminated `TSLexeme` storage. Terminal alternatives have distinct `nvariant` values 0 and 1, flags zero, and the same position. They are alternatives, not two sequential words.

Ignore returns a non-SQL-NULL Datum containing a null pointer. PostgreSQL tries the next Dictionary. Stop returns a non-null empty array, ends the chain, and leaves one position hole. If the chain never recognizes the token, it produces no position. Default blank/display types have no Dictionary mapping.

A filter returns one nonempty term with `TSL_FILTER`. PostgreSQL passes it as the current input to later dictionaries. It does not run two independent pipelines for surface and stem. Rejecting Filter with Stem preserves that distinction. Chain-tail filters and filters followed only by NULL do not become final terms.

These are [PostgreSQL Dictionary semantics](https://www.postgresql.org/docs/18/textsearch-dictionaries.html), exercised with simple, Snowball, synonym, unaccent, and controlled protocol dictionaries in [chain.py](../tests/chain.py). Tests also cover external `TSL_ADDPOS` output. Compatibility means following the protocol, not guaranteeing useful language effects for every dictionary order.

## Queries and index integration

Native vector and query functions use the selected standard Configuration and Dictionary chain. GIN stores the resulting vector representation. The extension does not implement its own index access method.

`pg_ffi::plain_query_any` calls PostgreSQL plain-query analysis, checks its item/operator domain, and changes AND to OR in that fresh request-owned tree. It preserves OR alternatives, never mutates a shared query, and never analyzes final terms again. It is not a phrase/Boolean query parser.

The pinned pg_textsearch uses PostgreSQL configuration-based vector analysis for index and query terms. It implements BM25 bag-of-terms retrieval separately from native AND and phrase semantics. Surface/stem alternatives can change its term frequencies, document lengths, scores, and storage size. Candidate consistency and ranking quality therefore need separate judgments.

pg_textsearch's metapage configuration OID supports runtime analysis. Build and REINDEX resolve the stored `text_config` string again. This explains its rename/rebuild and dependency limitations, which are covered in [bm25.py](../tests/bm25.py) and [config.py](../tests/config.py). Required maintenance is in the [README](../README.md#analysis-changes-and-index-maintenance).

## Original-text headline

PostgreSQL core parses and lexizes source text with the actual configuration, preserving original pointers, matched operands, positions, and repeated entries. The extension's HEADLINE callback consumes those results. It does not normalize tokens again or require all mappings to use Charabia dictionaries.

`pg_ffi::headline` makes a request-owned scratch copy of core's entries. Non-repeated language entries project to the PG18 selector's word type. Blank, display, and repeated entries project to its blank type. The projection does not reinterpret Charabia IDs as default-parser IDs. It preserves original positions, query items, raw bytes, and repetition metadata.

The callback passes validated options to guarded `prsd_headline`, PostgreSQL's existing fragment selector. It verifies that the selector retained the scratch entry array. Then it computes escaped entry/output sizes, copies selection flags to core's original entries, and escapes only non-repeated source text. PostgreSQL renders the result once. Repeated operand matches therefore do not duplicate original source output.

Source escaping and trusted markers remain separate. [headline.py](../tests/headline.py) and [chain.py](../tests/chain.py) test raw-text preservation, normalization/stem matching, positions, external chains, options, and budgets. JSON and JSONB use the same callback for each string.

Core matching and repeated-entry allocation happen before the callback. Its output checks cannot bound all earlier allocations. Interrupt checks also cannot interrupt every instruction inside Charabia, a stemmer, or the PG selector. These limits explain the user requirements for bounded queries, top-result highlighting, and timeouts.

## Ownership and errors

Rust owns analysis vectors, strings, FSTs, and stemmers. PostgreSQL owns callback arrays, strings returned to core, and headline scratch memory. PG allocation and pointer conversion stay in `pg_ffi.rs`.

`pg_ffi::owned_state` preserves a Box owner while allocating and initializing the reset callback in the original CurrentMemoryContext. After callback registration, it immediately uses `Box::into_raw` and installs that canonical pointer. No fallible operation or PG call can occur during that handoff. The guarded callback drops exactly that Box. Drop must not call PostgreSQL or panic.

Parser START registers exactly `Option<ParserState>`. The state owns spans/cursor and borrows the PG input pointer, which lives through END. GETTOKEN copies an output descriptor before any later PG operation and returns a type ID as an int32 Datum through the SQL `internal` signature. EOF uses a zero Datum. END takes the state once, releasing its large vector. Reset later drops the empty Option container, or the still-present state after an error.

Dictionary INIT registers exactly `DictionaryState` in the PG dictionary or DDL-validation context. LEXIZE borrows it only during pure Rust analysis, then ends the borrow before interrupt checks and PG output allocation. Code must not retain subsidiary-cache borrows across PG calls that can invalidate them.

pgrx-generated callback wrappers and guarded PG bindings contain PG ERROR/Rust panic transitions. `pg_ffi::checked` maps invalid input, resource limits, and invariant errors to stable SQLSTATE classes. Normal Ignore and Stop use result variants, not exceptions. Interrupt checks surround analysis and occur in token/headline loops. No PG API runs on a Rust worker thread.

Registration-before/after failures, INIT failures, repeated calls, ERROR/panic/cancel, and reset ownership are exercised by [errors.py](../tests/errors.py) and [lifecycle.py](../tests/lifecycle.py). Test-hook statistics are diagnostic build features, not release interfaces.

## Dictionary initialization and invalidation

PostgreSQL owns the per-backend cache keyed by Dictionary OID. INIT reads that object's merged catalog options and creates its state. LEXIZE performs no SPI configuration lookup or language detection. The only derived stopword cache belongs to the Dictionary state.

Catalog changes mark the PG text-search cache invalid. A later lookup resets the dictionary context and reruns INIT. A failed INIT never becomes valid for LEXIZE. Registration-before failures release Rust ownership immediately. A registered state from failed cached INIT can remain until the next dictCtx reset. Transaction abort does not necessarily destroy that cache context.

Rollback and cross-session invalidation use native catalog/cache behavior. Ordinary restored objects need no remembered pre-restore OIDs. Cache invalidation cannot update precomputed vectors or postings. Constant-folded prepared queries are also distinct from normal parameterized-plan invalidation.

See PostgreSQL's [ts_cache.c](https://github.com/postgres/postgres/blob/REL_18_6/src/backend/utils/cache/ts_cache.c) for the subsidiary-memory lifetime. [lifecycle.py](../tests/lifecycle.py) and [stress.py](../tests/stress.py) cover invalidation, failed rebuilds, actual workers, concurrent snapshots, and resource stability.

## Persistent state and recovery

| State | Persistence and replay |
| --- | --- |
| Parser/Template/Dictionary/Configuration definitions, options, and mappings | Standard PostgreSQL catalogs and transactional WAL. |
| Source rows and stored vectors | Table storage and core WAL for logged relations. |
| Native GIN | PostgreSQL index pages and GIN WAL/redo. |
| BM25 | Upstream index pages plus its WAL-backed memtable chain and segments. Shared-memory caches are reconstructed. |
| Parser state, dictCtx, FST, stemmer, headline scratch | Process/request state. New backends rebuild what they need from restored catalogs and installed resources. |
| Library, control/SQL files, dependencies, external dictionary rules | Installation resources outside database WAL. Embedded dictionaries are part of the exact library. |
| Server/restore configuration and environment | Files and external settings. A base backup can include files inside PGDATA, but later configuration-file edits are not replayed as catalog WAL. |

The extension adds no custom WAL resource manager, mutable data files, or runtime downloads. For logged relations, the pinned BM25 writes memtable-chain/spill changes with Generic WAL. Segment writes also use core full-page records, and page reuse uses a core Btree record. It does not use only Generic WAL. Relevant upstream sources are [memtable/log.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/memtable/log.c), [segment.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/segment/segment.c), and [freepage.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/index/freepage.c).

After restart, pg_textsearch rebuilds derived state from its metapage, durable chain, and segments. See [index/state.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/index/state.c). Core/Generic redo does not call Charabia to reconstruct old terms. Queries and new writes after recovery need the correct installed analyzer.

Physical recovery establishes database consistency at a target, not agreement between old postings and a new analysis policy. Nor does it restore external installation files. Logical dump recreates extension members from installed SQL and ordinary user objects from their definitions. These boundaries explain the [user backup requirements](../README.md#backup-and-recovery-requirements).

The retained [recovery test](../tests/recovery.py) exercises online backup, archived WAL, named-target restore, crash restart, cold initialization, GIN/BM25 paths, Chinese terms, and headline. Its scope is those controlled paths. It is not proof of every crash timing, high availability, logical decoding, or application-level integration.

## Current tradeoffs

- Fixed model mappings and default segmentation avoid per-token detection and configuration-dependent Parser state. Short-query language errors remain possible.
- Independent Dictionary preparation is necessary for direct calls and earlier filters. Chains can repeat that preparation, but headline does not repeat it.
- Surface retention improves some matches but changes BM25 statistics. It does not repair stopword deletion or guarantee all word-form/phrase recall.
- Standard Parser/Dictionary interfaces preserve index reuse and arbitrary native composition. They cannot carry private Parser metadata to Dictionary callbacks.
- Standard headline preserves raw source and PG matching. It is a presentation step, not a phrase or Boolean match decision.

Known quality failures and supported bounds remain explicit in the [README](../README.md#limits-and-known-quality-failures). There is no general lemmatizer, editable stopword platform, or separate application-specific integration layer.
