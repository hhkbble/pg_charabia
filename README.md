# pg_charabia

pg_charabia provides multilingual text analysis for PostgreSQL 18. It uses Rust, pgrx, and Charabia. The default `public.charabia` configuration works with native full-text search (FTS), GIN indexes, optional pg_textsearch BM25 indexes, and standard `ts_headline` highlighting.

Each language dictionary enables built-in stopwords and stemming when supported. Stopwords are words that the dictionary discards. Stemming reduces a word to a stem, not a general dictionary form. Terminal mode keeps the surface form and adds one distinct stem at the same position.

The extension covers 70 languages and `und` for an undetermined language. Of these languages, 52 have stopword tables and 19 have stemming algorithms. Each language uses its complete NLTK table when available. ISO supplies languages absent from NLTK. The tables are not merged.

## Supported environments and prerequisites

| Environment | Scope |
| --- | --- |
| Linux AMD64 + PostgreSQL 18 | Release build and validation target. The pinned PostgreSQL build and optional pg_textsearch version are in [environment.lock.json](docker/environment.lock.json). |
| macOS ARM64 + PostgreSQL 18 | Local development with the isolated runner. It does not provide local BM25 or recovery suites. |
| Other operating systems, CPU architectures, or PostgreSQL majors | No repository validation entry point is provided. |

Native FTS requires a UTF8 database and a matching pg_charabia library. It does not require pg_textsearch. BM25 also requires the pinned pg_textsearch build, its dynamic dependencies, and preload setup before server startup.

The project version and dependencies come from [Cargo.toml](Cargo.toml) and [Cargo.lock](Cargo.lock). Resources are embedded and identified in [resources/manifest.json](resources/manifest.json). There are no runtime data downloads.

## Installation

The repository supports source builds and locally generated release archives. It does not provide a public package service. For source-build instructions, read [Development](docs/development.md#build-and-test). The following installation procedure uses an extracted, verified Linux AMD64 archive.

1. Match the package to the target PostgreSQL major, operating system, CPU architecture, ABI, and dynamic dependencies. Read `delivery-manifest.json` first.
2. On the target host, enter the extracted package root. Set `PG_CONFIG` to that PostgreSQL 18 installation's `pg_config`.
3. Run the inspection commands below. Stop on a checksum, architecture, PostgreSQL version, or dependency mismatch.
4. Run the two `install` commands with administrative permissions. They change the target PostgreSQL installation.

```sh
: "${PG_CONFIG:?Set PG_CONFIG to the target PostgreSQL 18 pg_config}"
"$PG_CONFIG" --version
sha256sum --check SHA256SUMS
file extension/pg_charabia.so
ldd extension/pg_charabia.so
install -m 755 extension/pg_charabia.so "$("$PG_CONFIG" --pkglibdir)/pg_charabia.so"
install -m 644 extension/*.control extension/*.sql "$("$PG_CONFIG" --sharedir)/extension/"
```

A macOS library cannot load into Linux PostgreSQL. Keep the complete package, checksums, resource manifest, and dependency notices after installation.

For BM25, separately install the pg_textsearch build pinned in `docker/environment.lock.json`. Add `pg_textsearch` to `shared_preload_libraries`, preserving other required entries, then restart PostgreSQL. pg_charabia installation does not change this parameter. Native FTS users skip this step.

### Schema ownership and permissions

A database superuser runs `CREATE EXTENSION pg_charabia`. Installation reserves the fixed `charabia` schema and `public.charabia` configuration. The extension is not relocatable. Conflicting objects cause atomic failure rather than replacement.

If `charabia` already exists, the installer must own it and it must deny untrusted `CREATE` privileges. Otherwise, installation fails with `42501`. Installation grants `PUBLIC` schema `USAGE`, revokes `PUBLIC CREATE`, and leaves existing `public` schema permissions unchanged.

Create custom objects in an application-owned schema with normal PostgreSQL permissions. Use schema-qualified names in applications and indexes. Unqualified names require a trusted `search_path`. Names and prefixes do not establish ownership or permission to delete an object.

## Quick start: native FTS and headline

Connect to a fresh database with extension files installed. Use a trusted `search_path` that creates the example table in `public`. This block needs only PostgreSQL and pg_charabia. The three SQL blocks in this README run in order in the same database.

```sql
CREATE EXTENSION pg_charabia;
SELECT charabia.analysis_info();
SELECT * FROM charabia.analyze('中文全文检索 PostgreSQL');
SELECT * FROM ts_debug('public.charabia','中文全文检索 PostgreSQL');
CREATE TABLE documents (
    id bigint PRIMARY KEY,
    body text NOT NULL CHECK(octet_length(body)<=262144)
);
INSERT INTO documents VALUES
 (1,'中文全文检索 PostgreSQL 18 支持 BM25'),
 (2,'The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running.');
CREATE INDEX documents_gin ON documents USING gin(to_tsvector('public.charabia',body));
SELECT id FROM documents
 WHERE to_tsvector('public.charabia',body)@@plainto_tsquery('public.charabia','POSTGRESQL');
SELECT ts_headline('public.charabia',body,
                  plainto_tsquery('public.charabia','run'),'HighlightAll=true')
 FROM documents WHERE id=2;
```

The search returns document `1`. The headline marks both original `running` words in document `2` from a query for `run`. The GIN expression uses the same configuration for documents and queries.

## Public interfaces

| Interface | Purpose |
| --- | --- |
| `public.charabia` | Default Text Search Configuration. |
| `charabia.parser` | Standalone Text Search Parser for ordinary user configurations. |
| `charabia.template` | Dictionary Template used with the parameters below. |
| `charabia.eng`, `charabia.fra`, `charabia.cmn`, …, `charabia.und` | Default language dictionaries. Discover the complete set with `charabia.analysis_info()`. |
| `charabia.analysis_info() RETURNS jsonb` | Static build/resource identity, language capabilities, and defaults. It does not fingerprint user configurations or external dictionary files. |
| `charabia.analyze(text) RETURNS TABLE` | Parser facts: `raw`, `kind`, `script`, `charabia_language`, `language`, `language_source`, `byte_start`, `byte_end`, `char_start`, `char_end`, `token_type`. |
| `charabia.query_any(text, config regconfig DEFAULT 'public.charabia') RETURNS tsquery` | Plain query analysis with AND changed to OR in the new query tree. |
| `pg_catalog.ts_headline` | Standard text, JSON, and JSONB headline using the selected configuration. |

`analyze` offsets are zero-based, half-open ranges. Byte offsets refer to UTF-8. Character offsets count Unicode scalar values, not grapheme clusters. Missing original Charabia language remains NULL. Blank/display tokens have no selected language in the diagnostic output.

`analyze` does not report final terms from a Dictionary chain. Use `to_tsvector` and query functions to inspect effective analysis. Diagnostic/query functions use `SECURITY INVOKER` and a fixed `search_path=pg_catalog`. Internal callbacks do not grant direct `PUBLIC EXECUTE`.

The Parser emits 71 language types, `blank`, and `display_only`. The last two have no default mapping and consume no word position. Stopwords leave position holes. Surface and stem alternatives share one position. See [Architecture](docs/architecture.md) for the implementation.

## Native configuration and dictionary chains

Keep default extension members as an unchanged baseline. A configuration `COPY` copies mappings but shares dictionaries. Create a separate dictionary only where independent parameters are needed. ALTER on a shared dictionary affects every reference.

The next block replaces only the English mapping. It also creates a standalone configuration with a filter followed by PostgreSQL's `simple` dictionary.

```sql
CREATE SCHEMA app;
CREATE TEXT SEARCH CONFIGURATION app.search(COPY=public.charabia);
CREATE TEXT SEARCH DICTIONARY app.search_eng(
 TEMPLATE=charabia.template, Language='eng',
 Discard=true, Stem=false, Normalize=true, Filter=false
);
ALTER TEXT SEARCH CONFIGURATION app.search
 ALTER MAPPING FOR eng WITH app.search_eng;
-- Native ALTER keeps other stored options. This instance has no indexes yet.
ALTER TEXT SEARCH DICTIONARY app.search_eng(Stem=true);
SELECT ts_lexize('app.search_eng','running');
SELECT charabia.query_any('全文 banana','app.search');

CREATE TEXT SEARCH DICTIONARY app.fold_eng(
 TEMPLATE=charabia.template, Language='eng',
 Discard=false, Stem=false, Normalize=true, Filter=true
);
CREATE TEXT SEARCH CONFIGURATION app.filtered(PARSER=charabia.parser);
ALTER TEXT SEARCH CONFIGURATION app.filtered
 ADD MAPPING FOR eng WITH app.fold_eng, pg_catalog.simple;
SELECT to_tsvector('app.filtered',body) FROM documents WHERE id=2;
```

`app.filtered` maps only English. Add mappings for other language types that the application needs. The Parser stays independent of the selected dictionaries.

### Dictionary parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `Language` | Required | A supported lowercase ISO639-3 code or `und`. The Dictionary applies that language without detecting it again. Two-letter aliases are not accepted. |
| `Discard` | `true` if a table exists | Discard tokens whose prepared key matches that language's built-in stopword table. |
| `Stem` | `true` if an algorithm exists | Add one stem for prepared keys of at most 1000 bytes containing only letters or combining marks. Otherwise, keep the surface form. |
| `Normalize` | `true` | Apply final normalization to the surface and stem. `false` still performs required non-lossy preparation, NFC, and language-specific lowercase rules. |
| `Filter` | `false` | Terminal mode returns final terms. `true` passes one replacement onward and requires `Stem=false`. |

Preparation precedes stopword matching and stemming. It preserves accents needed by the stemmer. `Normalize=false` does not mean byte-for-byte output. Final normalization depends on language and script. It does not promise identical accent folding or Unicode form across languages.

Use unquoted parameter names and Boolean values `true`/`false`. PostgreSQL also accepts `on`/`off` and integer `0`/`1`, but rejects quoted `t`/`f` here.

| SQLSTATE | Condition |
| --- | --- |
| `22023` | Missing/unknown Language, unknown or duplicate options received by INIT, or `Filter=true` with `Stem=true`. |
| `0A000` | Explicit Discard/Stem activation without the required language capability. |
| `42601` | Invalid Boolean syntax under PostgreSQL's `defGetBoolean` rules. |
| `54000` | An input or filter replacement exceeds the extension's byte limit. |

Native `ALTER TEXT SEARCH DICTIONARY` keeps unlisted stored options. `ALTER ... (Stem)` removes the stored value and restores its capability default. It does not set `Stem=false`. Removing required Language fails and rolls back.

PostgreSQL merges same-spelling duplicate ALTER keys before INIT, with the last value taking effect. CREATE duplicates and quoted case variants can reach INIT and be rejected. A Language change recalculates omitted defaults. Explicit stored values remain and can fail for the new language's capabilities.

There are no proprietary management functions, editable built-in stopword tables, add/keep settings, or per-query analysis switches. Use independent native objects for independent index parameters. Ordinary CREATE, ALTER, rename, schema move, ownership, and DROP follow PostgreSQL rules. Use `RESTRICT` to expose dependencies before removing objects.

### Recognition versus filtering

| Dictionary result | Effect |
| --- | --- |
| NULL | Try the next dictionary with the current input, including an earlier filter replacement. |
| Stop, an empty non-NULL result | End the chain and leave a position hole. |
| Terminal terms | End the chain. Up to two distinct terms share one position. |
| Filter replacement | Pass one token to the next dictionary. It is not a final term. |
| Final filter, or filter followed only by NULL results | No indexed term and no position hole. |

Language types can use `simple`, Snowball, synonym, or filtering dictionaries. A synonym dictionary before a terminal Charabia dictionary can finish analysis first. A terminal Charabia result can skip later dictionaries. External dictionaries keep their own stopword, stemming, and normalization rules. These switches do not control them.

Keep `blank` and `display_only` unmapped unless deliberately changing their behavior. Swapping language dictionaries does not index default punctuation and whitespace. `ts_lexize` hides filter flags, and `ts_debug` does not fully simulate filtering. Validate real chains with vectors, queries, and indexes.

## BM25 with pg_textsearch

Complete the optional pg_textsearch installation and preload setup before this block. Native FTS users can skip it. The example uses the documents from the quick start.

```sql
CREATE EXTENSION pg_textsearch;
CREATE INDEX documents_bm25 ON documents USING bm25(body)WITH(text_config='public.charabia');
-- A tiny table can choose a fallback. Inspect the explicit index path locally.
SET enable_seqscan=off;
EXPLAIN(ANALYZE,FORMAT JSON) SELECT id FROM documents
 ORDER BY body <@> to_bm25query('全文','public.documents_bm25') LIMIT 10;
WITH q AS MATERIALIZED(SELECT charabia.query_any('全文','public.charabia') AS query),
 ranked AS MATERIALIZED(
  SELECT id,body,body <@> to_bm25query('全文','public.documents_bm25') AS score
  FROM documents WHERE (SELECT numnode(query)FROM q)>0
  ORDER BY body <@> to_bm25query('全文','public.documents_bm25') LIMIT 10)
SELECT id,score,ts_headline('public.charabia',body,q.query,'MaxFragments=2')
FROM ranked CROSS JOIN q ORDER BY score;
RESET enable_seqscan;
```

The actual index path returns document `1`. Its plan contains an `Index Scan` on `documents_bm25`. Disabling sequential scans demonstrates that path on a tiny example table. It is not an application-wide planner recommendation.

BM25 uses bag-of-terms matching. Native `plainto_tsquery` uses AND. Phrase queries impose position rules. `query_any` accepts ordinary valid configurations and does not analyze final terms again. Build it once per statement, then highlight only the top results. The BM25 query still performs its own required analysis.

## Standard headline

`ts_headline` matches real dictionary results to original text. Text, JSON, and JSONB share the implementation. Normalization, stemming, prefix matching, position holes, and external dictionary chains use PostgreSQL's matching path.

Headline output is not a retrieval filter. Apply the actual match condition first, especially for phrases and NOT queries. A headline can mark operands even when a phrase does not match.

| Option | Default and accepted values |
| --- | --- |
| `StartSel`, `StopSel` | `<b>`, `</b>`. Each marker is at most 256 UTF-8 bytes. |
| `FragmentDelimiter` | ` ... `. At most 256 bytes. |
| `MinWords`, `MaxWords` | 15 and 35. Require `1 <= MinWords < MaxWords <= 10000`. |
| `MaxFragments` | 0. Accepted range is 0 through 100. |
| `ShortWord` | Only 0 is supported. |
| `HighlightAll` | `false`. Conflicts with explicit MinWords/MaxWords or positive MaxFragments. |
| `Escape` | `true`. Escape source text, not markers or the delimiter. |

Unknown/duplicate options, invalid values, and conflicting settings fail with `22023`. Weighted query operands and unsupported ShortWord values fail with `0A000`.

`Escape=true` escapes `&`, `<`, `>`, double quotes, and apostrophes in source text. Markers and delimiters remain trusted markup. Supply trusted markup. `Escape=false` leaves source HTML unescaped, which the caller must handle.

## Limits and known quality failures

| Boundary | Behavior |
| --- | --- |
| Input | UTF8 database, valid UTF-8, no NUL, at most 262144 bytes per text or query input. |
| BM25 | Validate the complete document and query before upstream slicing. The Parser cannot recover the original full length. Keep a document CHECK like the quick start. |
| Raw token | At least 2047 bytes becomes display-only pieces of at most 2046 bytes, split on Unicode scalar boundaries. Default mappings do not index these pieces. |
| Final term | PostgreSQL stores a 2047-byte term and ignores a 2048-byte term. Diagnostic output does not prove indexability. |
| Positions | Positions saturate at 16383. A stored vector keeps at most 255 positions per term. |
| Headline | Each escaped entry is at most 65535 bytes. Output is at most 8 MiB per callback/string. Limit errors use `54000`. |

For JSON headline, limits apply to each string, not the complete JSON value. These callback limits do not bound earlier PostgreSQL matching allocations. Limit query complexity, highlight only selected results, and use statement timeouts for workload control.

Automatic language selection is imperfect, especially for short or mixed-language queries. The exact frozen cases are in [quality-known.json](tests/fixtures/quality-known.json):

| Query | Default failure |
| --- | --- |
| `den` | Does not find the English fox-den document. |
| `var` | Does not find the technical document about a variable named `var`. |
| `cats were running` | Fails the phrase expectation for the English quick-start document. |

Explicit `Discard=false` recovery is a separate result, not a fix for default behavior. Surface/stem alternatives do not fix stopword deletion or guarantee cross-form matches. They also change BM25 term frequencies, document lengths, and scores. Score changes do not necessarily change result order.

## Analysis changes and index maintenance

Dictionary parameters, mappings, chain order, external rules, and resource changes can invalidate old indexed terms. Cache invalidation rebuilds process state, not vectors or indexes. The build/resource fingerprint does not certify user chains or existing indexed data.

1. Inventory affected configurations, shared Dictionary references, stored vectors, and indexes. Include COPY relationships.
2. Save source text, ordinary object DDL, owners, package/resource identity, and external rules.
3. Pause affected reads/writes and end old transactions and backends before changing analysis or loaded binaries.
4. Apply the change, then recompute/rebuild using the table below. Use the same configuration for document and query analysis.
5. Reprepare queries affected by constant folding. Validate parameterized custom/generic plans separately.
6. Validate expected document IDs, stored values, actual GIN/BM25 plans, and original-text headline before reopening traffic.

| Representation or change | Required action |
| --- | --- |
| Expression GIN on `to_tsvector` | REINDEX reruns the expression and analysis. |
| Ordinary `tsvector` column with GIN | Recompute the column from source text, then maintain its index. REINDEX alone reuses the old vector. |
| Generated `tsvector` column with GIN | Update source values or redefine the expression to trigger recomputation, then maintain the index. |
| BM25, same configuration OID | REINDEX after parameter or mapping changes. |
| Replacement configuration OID | Recreate affected expressions/indexes for the new configuration. For BM25, replace the index or set the new text_config and rebuild. |
| Dictionary rename or schema move | Mappings keep the OID. A name-only change does not itself require reanalysis. |
| Configuration rename or schema move | Native OID references remain. Update BM25's text_config string and REINDEX, or create a replacement index. |

For BM25 name changes, use `ALTER INDEX ... SET (text_config='new_schema.new_name')` and REINDEX. Changing the reloption alone is insufficient. Before dropping a configuration, inspect the BM25 inventory. The pinned upstream version lacks complete configuration dependency protection.

## Backup and recovery requirements

Logical dumps preserve ordinary dictionaries, parameters, mappings, owners, and dependencies, but restored objects receive new OIDs. Prepare roles, schema permissions, installed extensions, and external rules before restore. Validate subsequent ALTER/DROP operations and analysis afterward.

Default extension members are rebuilt from the installed package. User changes to those members are not saved as independent member definitions. Keep custom settings in ordinary application objects.

Point-in-time recovery (PITR) needs a complete cluster base backup and continuous archived write-ahead log (WAL) through the target. The target cannot precede the backup's earliest consistent point. Retain the following outside the database backup:

- The exact extension package, control/SQL files, dynamic libraries, resource manifest, and checksums for the target time.
- Matching pg_textsearch files and preload setup when BM25 is used.
- External synonym or other dictionary rule files, plus required locale/OS dependencies and server/restore configuration.

WAL does not restore installation directories or replay later configuration-file edits. Embedded resources travel with their exact library. Do not substitute an unverified newer package for the target package.

Prefer a named restore point after analysis maintenance finishes. If the target falls between a parameter change and its rebuilds, keep traffic closed and finish maintenance. Physical replay does not rerun historical SQL or text analysis. A consistent restore cannot fix a semantic mismatch already present at the target.

After recovery, validate LSN/timeline, parameters, mappings, cold Dictionary initialization, loaded library hash, stored vectors, candidates, actual index plans, and headline. Changes to binaries, redo components, or index formats require a matching recovery rehearsal. See [PostgreSQL PITR](https://www.postgresql.org/docs/18/continuous-archiving.html) and the [technical recovery boundary](docs/architecture.md#persistent-state-and-recovery).
