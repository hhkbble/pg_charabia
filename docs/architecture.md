# Architecture

pg_charabia is one Rust extension crate for PostgreSQL 18. It supplies text analysis through standard PostgreSQL Text Search objects. Indexes and queries consume that interface rather than an extension-specific index format.

The [README](../README.md) introduces the extension. The [Usage reference](reference.md) owns public parameters, query semantics, limits, and quality issues. [Installation and maintenance](operations.md) owns installation, index maintenance, and backup requirements. [Development](development.md) owns builds, tests, debugging, and release validation.

## Components and identities

[Cargo.toml](../Cargo.toml) and [Cargo.lock](../Cargo.lock) define dependencies and enabled features. [rust-toolchain.toml](../rust-toolchain.toml) defines the compiler. [resources/manifest.json](../resources/manifest.json) identifies embedded data, source archives, checksums, and notices. [environment.lock.json](../docker/environment.lock.json) pins the Linux acceptance environment and optional pg_textsearch source.

| Component | Responsibility |
| --- | --- |
| [analysis.rs](../src/analysis.rs) | `segment`, `prepare`, `inspect`, and `normalize`. Validated spans and bounded linguistic dictionary analysis. |
| [language.rs](../src/language.rs) | `complete` and `LanguageSource`. Reuse detected language and complete missing metadata. |
| [routes.rs](../src/routes.rs) | `LanguageId` and `TokenType`. Fixed language, script, stopword-code, algorithm, and type mappings. |
| [prepare.rs](../src/prepare.rs) and [language_data.rs](../src/language_data.rs) | Shared host/target preparation and the fixed language capabilities. |
| [resources.rs](../src/resources.rs), [stopwords.rs](../src/stopwords.rs), and [build.rs](../build.rs) | Build-generated immutable English/stopword FSTs and shared views. |
| [parser.rs](../src/parser.rs) | START/GETTOKEN/END/LEXTYPES callbacks and the standard HEADLINE callback. |
| [structured_tokens.rs](../src/structured_tokens.rs) and its private modules | Bounded ASCII candidates, borrowed structure parsing, English components, and full/component groups. |
| [structured_dictionary.rs](../src/structured_dictionary.rs) | Separate terminal INIT/LEXIZE state for the five structured token kinds. |
| [linguistic_dictionary.rs](../src/linguistic_dictionary.rs) | INIT validates options and owns state. LEXIZE analyzes one supplied token. |
| [headline.rs](../src/headline.rs) | Strict option parsing, HTML escaping, and output-size accounting. |
| [pg_ffi.rs](../src/pg_ffi.rs) | PG allocation, Datum conversion, guarded calls, callback ownership, query rewriting, and headline projection. |
| [lib.rs](../src/lib.rs) | Module registration and the diagnostic/query functions. |
| [pg_charabia.sql](../sql/pg_charabia.sql) | Public SQL signatures, default objects, permissions, and callback registration. |

The default installation has one Parser, two Templates, 16 linguistic dictionaries and five structured dictionaries, and `public.charabia`. Ordinary user configurations can share these dictionaries or use other standard dictionaries. There is no configuration table, proprietary object manager, global derived Dictionary cache, or cross-dictionary private payload.

```text
Original UTF-8 text
  -> Parser: segment once and complete language on the complete original text
       + ASCII candidate scan, validated non-overlapping raw coverage
       + ordinary classification/English restoration and structured token type assignment
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

`analysis::segment_counted` runs Charabia segmentation once with exactly 15
natural-language candidates. The fixed features enable German compound
segmentation and Latin camelCase. German runs when the Latin group selects deu.
The generic Latin path applies camelCase. These are alternative segmenters, not
two sequential passes. Swedish, Greek, Hebrew, and Khmer features are disabled.
The Japanese UniDic and Korean ko-dic resource choices remain fixed.

Each `Span` keeps original byte/scalar ranges, Script, original Charabia language,
selected route, type, and source. Input coverage must be contiguous, UTF-8 valid,
and byte-identical. `language::complete` reuses only candidate metadata compatible
with the Script. Unknown, outside, and incompatible metadata safely selects und.
Only missing Cyrillic, Hangul, and Thai map directly to rus, kor, and tha. The
pinned Latin/Cj/Arabic paths already request detection, so missing metadata does
not trigger a second attempt. There is no supplement detector, confidence gate,
English fallback, independent window, or per-token statistical classification.
Closed candidates can still misclassify foreign text with the same Script.

The language-independent public classification prefix runs once per raw span.
Word spans form maximal contiguous envelopes. A split complete ASCII-letter
envelope, bounded by non-letter/non-number/non-combining characters, is looked
up as one lowercase English key. A hit replaces all spans with one original-text
span, route eng, and source `english-merge`. Unsplit words and unmatched envelopes
keep their routes/spans. The code neither finds substrings nor deletes separators.
Original metadata remains diagnostic information rather than proof of English.

Language IDs follow the fixed order in `language_data.rs`: 1 through 16, with
und last. Blank is 17 and display-only is 18. Both stay unmapped by default. Structured token IDs 19 through 23 are url, host, email, file, and numword.
Oversized raw tokens split into bounded display fragments without indexing
subterms. Span checks and long-token handling run on the restored result.

START receives text and length, not configuration identity or Dictionary options. PostgreSQL maps types to the active chain. LEXIZE receives neither Parser type nor detected language. A Dictionary therefore uses its own `Language`, even if the mapping chooses a different language from the Parser's type.

## ASCII recognition and atomic structured tokens

The Parser completes language metadata before applying `structured_tokens::recognition::scan`.
It keeps the complete Charabia context. It never removes structured tokens before detection or re-segments leftover windows.
The coverage merge clips ordinary spans at accepted candidate boundaries and preserves their metadata.
Original byte/scalar coverage stays contiguous and byte-identical. Structured tokens prevent English restoration from merging across them.
A structured token has no selected language. Diagnostic metadata does not become a private Dictionary payload.

The recognizer scans maximal ASCII runs. It rejects candidates that cut a combining sequence.
A single URL lane finds scheme/envelope boundaries and uses borrowed `iri_string::types::UriStr` validation.
URL authority uses URI grammar, without applying standalone DNS restrictions to reg-names.
Percent-host spelling stays opaque, and only DNS-shaped names supply label components.
The email-only `LinkFinder` supplies candidates, then the finite mailbox rules validate them.
Explicit paths and finite host/file/number rules cover the other kinds. Arbitration keeps the leftmost, longest outer structure.
Invalid URL envelopes block internal structural retries, but leave every byte in ordinary analysis.
Oversized candidates return to the ordinary/display path rather than supplying a truncated full term.
No default-parser tag/entity state machine or serializer runs.
Outside accepted structured tokens, the adapter splits literal `<`, `>`, and `=` boundaries.
The upstream separator list omits them, which otherwise attaches markup delimiters to searchable words.
The split preserves language metadata and original spans, without a second segmentation pass or tag interpretation.

`structured_tokens::terms::Options` owns ordinary English options and borrows the existing immutable FSTs.
Each terminal call validates its current raw input again. Direct calls and preceding filters make this necessary.
Borrowed URI fields supply a finite, non-recursive component traversal.
Complete spelling has kind-specific rules. English leaves use the public Latin segmenter, existing whole-word restoration, and the same `analysis::english_component` branch.
Numeric literals take priority over implicit hex. Percent triplets remain opaque.

A `StructuredTokenTerms` result privately keeps full forms and one component group.
`pg_ffi::structured_lexemes` emits each full form as a singleton variant, followed by components with one shared variant.
All flags are zero. PostgreSQL plain-query construction ORs variants and ANDs entries inside a variant.
The vector stores every distinct term at the same original position.
Component terms equal to full terms are omitted individually. Other components remain searchable.

Expansion accounting happens before stop processing, normalization and deduplication.
Repeated leaves count toward the budget, then a bounded call-local cache reuses their English analysis.
There is no backend parse cache or unbounded graph representation.
The exact grammar, options, budgets and user tradeoffs belong in [Structured ASCII tokens](reference.md#structured-ascii-tokens) and [limits](reference.md#limits-and-known-quality-issues).

## Linguistic dictionary analysis

`LinguisticDictionaryState` owns validated `analysis::Options` and at most two stemmers.
It borrows immutable stopword FSTs. An FST is a finite-state transducer used as a
word set. Discard controls stopword references, not resource generation. Stem
controls algorithm instances. English independently enables an additional branch
for eligible terminal dictionaries and ASCII-letter tokens in the English FST.

For each token supplied by PostgreSQL, `analysis::inspect`:

1. Validates input independently of START, including direct calls and filters.
2. Prepares the original language branch with non-lossy normalization and
   classification, NFC, language-specific lowercase, and NFC again.
3. Checks that branch's prepared stop key before optional stemming.
4. Keeps its surface and one eligible stem, finalizes each separately, and
   removes empty or duplicate terms.
5. When English is enabled and the actual current ASCII token hits the English
   FST, performs the same operations with English rules. Turkish lowercase is
   not reused as the English lookup key.
6. Aggregates in native-surface/native-stem/English-surface/English-stem order.
   Any Terms win over another branch's Stop. Without Terms, any Stop produces
   Stop; otherwise the result is Ignore. Real errors are never suppressed.

`Terms` privately stores one through four distinct nonempty strings. Filter
rejects Stem and English, so it returns only one current-input replacement.
The Dictionary cannot recover the original whole word around an ASCII fragment.

Turkish preparation maps `I` to dotless `ı` and `İ` to `i`. Other characters follow Unicode lowercase. This happens before stopword lookup and stemming. Feeding an already lossy-folded surface into the stemmer changes the algorithm input.

`build.rs` uses the same `prepare.rs` and language metadata as runtime. Its
Charabia build dependency has no features; non-lossy preparation does not need
large segmenter data. The runtime dependency has the fixed feature set. The
lock and feature graph must preserve this host/target distinction.

Build generation selects each language's entire NLTK table, with ISO only for a
missing language, then prepares, excludes non-word/empty keys, sorts/deduplicates,
and serializes 15 FSTs. und has no FST. The committed English TXT generates one
additional FST. All generated bytes stay in OUT_DIR and are embedded. Runtime
uses bounded lazy parsing of static bytes, never raw stop arrays, network/files,
per-dictionary generation, or copies of the full data. The static view is an
immutable resource, not an OID/parameter-derived cache. The resource manifest
separates active embedded data, build-only source files, and generated FSTs. It
records source, generator/preparation identity, counts, and hashes. Hash records
for upstream raw tables do not mean those arrays enter the runtime library.

With Normalize disabled, surface retains the NFC prepared key and stem receives NFC. With Normalize enabled, both use Charabia's full finalizer. Finalization is script/language dependent. It is not a promise that every output is NFC or that every accent is removed.

The prepared stem-input bound applies after Unicode preparation. A token outside that bound or containing other character classes keeps its surface without invoking the algorithm. This bounds long stemmer input without changing segmentation.

The retained [direct Dictionary cases](../tests/fixtures/analysis/general/direct-lexize.json), `analysis.rs` tests, and `stopwords.rs` tests separate upstream examples, recorded observations, and extension-specific contracts.

## Dictionary chains and positions

`pg_ffi::lexemes` translates the owned Rust result into PG-allocated, zero-terminated `TSLexeme` storage. Terminal alternatives have distinct `nvariant` values 0 through at most 3, flags zero, and the same position. They are alternatives, not two sequential words.

Ignore returns a non-SQL-NULL Datum containing a null pointer. PostgreSQL tries the next Dictionary. Stop returns a non-null empty array, ends the chain, and leaves one position hole. If the chain never recognizes the token, it produces no position. Default blank/display types have no Dictionary mapping.

A filter returns one nonempty term with `TSL_FILTER`. PostgreSQL passes it as the current input to later dictionaries. It does not run two independent pipelines for surface and stem. Rejecting Filter with Stem or English preserves that distinction. Chain-tail filters and filters followed only by NULL do not become final terms.

These are [PostgreSQL Dictionary semantics](https://www.postgresql.org/docs/18/textsearch-dictionaries.html), exercised with simple, Snowball, synonym, unaccent, and controlled protocol dictionaries in [chain.py](../tests/chain.py). Tests also cover external `TSL_ADDPOS` output. Compatibility means following the protocol, not guaranteeing useful language effects for every dictionary order.

## Queries and index integration

Native vector and query functions use the selected standard Configuration and Dictionary chain. GIN stores the resulting vector representation. The extension does not implement its own index access method.

`pg_ffi::plain_query_any` calls PostgreSQL plain-query analysis, checks its item/operator domain, and changes AND to OR in that fresh request-owned tree. It preserves OR alternatives, never mutates a shared query, and never analyzes final terms again. It is not a phrase/Boolean query parser.

The pinned pg_textsearch uses PostgreSQL configuration-based vector analysis for index and query terms. It implements BM25 bag-of-terms retrieval separately from native AND and phrase semantics. Native/English surface/stem alternatives can change its term frequencies, document lengths, scores, and storage size. Candidate consistency and ranking quality therefore need separate judgments.

pg_textsearch's metapage configuration OID supports runtime analysis. Build and REINDEX resolve the stored `text_config` string again. This explains its rename/rebuild and dependency limitations, which are covered in [bm25.py](../tests/bm25.py) and [config.py](../tests/config.py). Required actions are in [Analysis changes and index maintenance](operations.md#analysis-changes-and-index-maintenance).

## Original-text headline

PostgreSQL core parses and lexizes source text with the actual configuration, preserving original pointers, matched operands, positions, and repeated entries. The extension's HEADLINE callback consumes those results. It does not normalize tokens again or require all mappings to use Charabia dictionaries.

`pg_ffi::headline` makes a request-owned scratch copy of core's entries. Non-repeated language and structured token entries project to the PG18 selector's word type. Blank, display, and repeated entries project to its blank type. The projection does not reinterpret Charabia IDs as default-parser IDs. It preserves original positions, query items, raw bytes, and repetition metadata.

The callback passes validated options to guarded `prsd_headline`, PostgreSQL's existing fragment selector. It verifies that the selector retained the scratch entry array. Then it computes escaped entry/output sizes, copies selection flags to core's original entries, and escapes only non-repeated source text. PostgreSQL renders the result once. Structured token matching projects the whole raw token, because TSLexeme supplies no component offsets. Repeated operand matches therefore do not duplicate original source output.

Source escaping and trusted markers remain separate. [headline.py](../tests/headline.py) and [chain.py](../tests/chain.py) test raw-text preservation, normalization/stem matching, positions, external chains, options, and budgets. JSON and JSONB use the same callback for each string.

Core matching and repeated-entry allocation happen before the callback. Its output checks cannot bound all earlier allocations. Interrupt checks also cannot interrupt every instruction inside Charabia, a stemmer, or the PG selector. These limits explain the user requirements for bounded queries, top-result highlighting, and timeouts.

## Ownership and errors

Rust owns analysis vectors, strings, and stemmers. FST views borrow static library bytes, which Dictionary resets never free. PostgreSQL owns callback arrays, strings returned to core, and headline scratch memory. PG allocation and pointer conversion stay in `pg_ffi.rs`.

`pg_ffi::owned_state` preserves a Box owner while allocating and initializing the reset callback in the original CurrentMemoryContext. After callback registration, it immediately uses `Box::into_raw` and installs that canonical pointer. No fallible operation or PG call can occur during that handoff. The guarded callback drops exactly that Box. Drop must not call PostgreSQL or panic.

Parser START registers exactly `Option<ParserState>`. The state owns spans/cursor and borrows the PG input pointer, which lives through END. GETTOKEN copies an output descriptor before any later PG operation and returns a type ID as an int32 Datum through the SQL `internal` signature. EOF uses a zero Datum. END takes the state once, releasing its large vector. Reset later drops the empty Option container, or the still-present state after an error.

Linguistic Dictionary INIT registers exactly `LinguisticDictionaryState` in the PG dictionary or DDL-validation context. LEXIZE borrows it only during pure Rust analysis, then ends the borrow before interrupt checks and PG output allocation. Code must not retain subsidiary-cache borrows across PG calls that can invalidate them.

The structured Template uses exactly `StructuredDictionaryState`, with its own paired callbacks.
It uses the same RAII registration and reset helper, not casts between state types.
Its bounded result owns every string before PG allocation. PG owns the zero-terminated emitted array and copied strings.
No request slice, URI view, or Rust string escapes through a PG pointer.

pgrx-generated callback wrappers and guarded PG bindings contain PG ERROR/Rust panic transitions. `pg_ffi::checked` maps invalid input, resource limits, and invariant errors to stable SQLSTATE classes. Normal Ignore and Stop use result variants, not exceptions. Interrupt checks surround analysis and occur in token/headline loops. No PG API runs on a Rust worker thread.

Registration-before/after failures, INIT failures, repeated calls, ERROR/panic/cancel, and reset ownership are exercised by [errors.py](../tests/errors.py) and [lifecycle.py](../tests/lifecycle.py). Test-hook statistics are diagnostic build features, not release interfaces.

## Dictionary initialization and invalidation

PostgreSQL owns the per-backend cache keyed by Dictionary OID. INIT reads that object's merged catalog options and creates its state. LEXIZE performs no SPI configuration lookup or language detection. The Dictionary state only binds shared static resource views and its small options/stemmers.

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
| Parser state, dictCtx, FST views, stemmers, headline scratch | Process/request state. New backends rebuild small state from restored catalogs and installed resources. Static FST bytes belong to the exact library. |
| Library, control/SQL files, dependencies, external dictionary rules | Installation resources outside database WAL. Embedded dictionaries are part of the exact library. |
| Server/restore configuration and environment | Files and external settings. A base backup can include files inside PGDATA, but later configuration-file edits are not replayed as catalog WAL. |

The extension adds no custom WAL resource manager, mutable data files, or runtime downloads. For logged relations, the pinned BM25 writes memtable-chain/spill changes with Generic WAL. Segment writes also use core full-page records, and page reuse uses a core Btree record. It does not use only Generic WAL. Relevant upstream sources are [memtable/log.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/memtable/log.c), [segment.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/segment/segment.c), and [freepage.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/index/freepage.c).

After restart, pg_textsearch rebuilds derived state from its metapage, durable chain, and segments. See [index/state.c](https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/index/state.c). Core/Generic redo does not call Charabia to reconstruct old terms. Queries and new writes after recovery need the correct installed analyzer.

Physical recovery establishes database consistency at a target, not agreement between old postings and a new analysis policy. Nor does it restore external installation files. Logical dump recreates extension members from installed SQL and ordinary user objects from their definitions. These boundaries explain the [user backup requirements](operations.md#backup-and-recovery).

The retained [recovery test](../tests/recovery.py) exercises online backup, archived WAL, named-target restore, crash restart, cold initialization, GIN/BM25 paths, Chinese terms, and headline. Its scope is those controlled paths. It is not proof of every crash timing, high availability, logical decoding, or application-level integration.

## Current tradeoffs

- Fixed model mappings and default segmentation avoid per-token detection and configuration-dependent Parser state. Short-query language errors remain possible.
- Independent Dictionary preparation is necessary for direct calls and earlier filters. Chains can repeat that preparation, but headline does not repeat it.
- English alternatives preserve some words discarded by the original language. They also retain cross-language homographs and change BM25 statistics. They do not guarantee word-form or phrase recall.
- Structured tokens have atomic positions, not internal phrase graphs. Their component conjunction can match scattered words, and BM25 discards variant groups.
- Standard Parser/Dictionary interfaces preserve index reuse and arbitrary native composition. They cannot carry private Parser metadata to Dictionary callbacks.
- Standard headline preserves raw source and PG matching. It is a presentation step, not a phrase or Boolean match decision.

Known quality failures and supported bounds remain explicit in the [Usage reference](reference.md#limits-and-known-quality-issues). There is no general lemmatizer, editable stopword platform, or separate application-specific integration layer.
