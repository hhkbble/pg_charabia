# pg_charabia

pg_charabia provides multilingual text analysis for PostgreSQL 18. It uses Rust, pgrx, and Charabia. The default `public.charabia` configuration works with native full-text search (FTS), GIN indexes, optional pg_textsearch BM25 indexes, and standard `ts_headline` highlighting.

Each language dictionary enables built-in stopwords and stemming when supported. Stopwords are words that the dictionary discards. Stemming reduces a word to a stem, not a general dictionary form. Terminal mode keeps each enabled branch's surface and distinct stem at the same position. Up to four terms are OR alternatives.

The extension supports five ASCII symbol dictionaries in addition to its language analysis.
They recognize URLs, hosts, email addresses, file paths, and mixed letter/number forms in original text.

The extension supports 15 languages and `und` for an undetermined language:
`eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und`.
All 15 natural languages have stopword tables. Ten have native stemming
algorithms. NLTK supplies the complete table when available. ISO supplies missing
languages. The tables are not merged.

Eight non-English Latin dictionaries also enable an English analysis branch by
default: `deu,nld,spa,fra,por,ita,tur,vie`. It uses a fixed English word set and
keeps the original language branch. Word-set membership is a routing rule, not
proof of language. The Parser can restore a split complete ASCII English word
without changing its original text. `English=false` controls the Dictionary
branch, not that Parser rule.

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
SELECT * FROM charabia.analyze('GET https://Example.com/Running?q=100ms');
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
| `charabia.template` | Language Dictionary Template used with the parameters below. |
| `charabia.symbol_template` | Terminal Dictionary Template for ASCII structures. |
| `charabia.url`, `charabia.host`, `charabia.email`, `charabia.file`, `charabia.numword` | Default symbol dictionaries. |
| `charabia.eng`, `charabia.fra`, `charabia.cmn`, …, `charabia.und` | Default language dictionaries. Discover the complete set with `charabia.analysis_info()`. |
| `charabia.analysis_info() RETURNS jsonb` | Static build/resource identity, language capabilities, and defaults. It does not fingerprint user configurations or external dictionary files. |
| `charabia.analyze(text) RETURNS TABLE` | Parser facts: `raw`, `kind`, `script`, `charabia_language`, `language`, `language_source`, `byte_start`, `byte_end`, `char_start`, `char_end`, `token_type`. |
| `charabia.query_any(text, config regconfig DEFAULT 'public.charabia') RETURNS tsquery` | Plain query analysis with AND changed to OR in the new query tree. |
| `pg_catalog.ts_headline` | Standard text, JSON, and JSONB headline using the selected configuration. |

`analyze` offsets are zero-based, half-open ranges. Byte offsets refer to UTF-8. Character offsets count Unicode scalar values, not grapheme clusters. Missing original Charabia language remains NULL. Blank/display and symbol tokens have no selected language in the diagnostic output. A symbol reports its own kind and token type.

`analyze` does not report final terms from a Dictionary chain. Use `to_tsvector` and query functions to inspect effective analysis. Diagnostic/query functions use `SECURITY INVOKER` and a fixed `search_path=pg_catalog`. Internal callbacks do not grant direct `PUBLIC EXECUTE`.

The Parser emits 16 language types, `blank`, `display_only`, and five symbol types, for 23 types in total. `blank` and `display_only` have no default mapping and consume no word position. Stopwords leave position holes. Language alternatives share one position. Each symbol also occupies one position, with the query grouping described below. See [Architecture](docs/architecture.md) for the implementation.

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

CREATE TEXT SEARCH DICTIONARY app.units(
 TEMPLATE=charabia.symbol_template, Kind='numword',
 Discard=true, Stem=false, Normalize=true
);
ALTER TEXT SEARCH CONFIGURATION app.search
 ALTER MAPPING FOR numword WITH app.units;
SELECT plainto_tsquery('app.search','100ms');
SELECT ts_headline('app.search','alpha 100ms beta',
                  plainto_tsquery('app.search','ms'),'HighlightAll=true');
```

`app.filtered` maps only English. Add mappings for other language types that the application needs. The Parser stays independent of the selected dictionaries.

### Dictionary parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `Language` | Required | A supported lowercase ISO639-3 code or `und`. The Dictionary applies that language without detecting it again. Two-letter aliases are not accepted. |
| `Discard` | `true` for the 15 natural languages; `false` for und | Apply each enabled branch's own built-in stopword table before stemming. A Stop in one branch does not remove another branch's terms. |
| `Stem` | `true` when a native algorithm or the English branch is enabled | Add one stem per eligible branch for prepared keys of at most 1000 bytes containing only letters or combining marks. Vietnamese can stem its English branch without a native algorithm. |
| `Normalize` | `true` | Apply final normalization to each surface and stem. `false` still performs non-lossy preparation, NFC, and language-specific lowercase rules. |
| `Filter` | `false` | Pass one replacement onward. Requires `Stem=false` and `English=false`. |
| `English` | `true` for the eight eligible languages in terminal mode; otherwise `false` | For a current nonempty ASCII-letter token in the fixed English set, add English preparation, stopword matching and optional stemming. It does not replace Language or detect it again. |

Preparation precedes stopword matching and stemming. It preserves accents needed by the stemmer. `Normalize=false` does not mean byte-for-byte output. Final normalization depends on language and script. It does not promise identical accent folding or Unicode form across languages.

Use unquoted parameter names and Boolean values `true`/`false`. PostgreSQL also accepts `on`/`off` and integer `0`/`1`, but rejects quoted `t`/`f` here.

| SQLSTATE | Condition |
| --- | --- |
| `22023` | Missing/unknown Language, unknown or duplicate options received by INIT, or `Filter=true` with `Stem=true` or `English=true`, or English enabled for eng, und, or a non-Latin language. |
| `0A000` | Discard without a table, or Stem without an enabled native or English algorithm. |
| `42601` | Invalid Boolean syntax under PostgreSQL's `defGetBoolean` rules. |
| `54000` | An input or filter replacement exceeds the extension's byte limit. |

Native `ALTER TEXT SEARCH DICTIONARY` keeps unlisted stored options. `ALTER ... (Stem)` removes the stored value and restores its capability default. It does not set `Stem=false`. Removing required Language fails and rolls back.

PostgreSQL merges same-spelling duplicate ALTER keys before INIT, with the last value taking effect. CREATE duplicates and quoted case variants can reach INIT and be rejected. A Language change recalculates omitted defaults. Explicit stored values remain and can fail for the new language's capabilities.
For Vietnamese with a saved `Stem=true`, disabling English also requires
`Stem=false` or removal of the saved Stem value. Otherwise, ALTER fails with
`0A000`. For a native stemmer, enabling Filter requires an explicit `Stem=false`.
Filter does not silently disable conflicting stored values.

There are no proprietary management functions, editable built-in stopword tables, add/keep settings, or per-query analysis switches. Use independent native objects for independent index parameters. Ordinary CREATE, ALTER, rename, schema move, ownership, and DROP follow PostgreSQL rules. Use `RESTRICT` to expose dependencies before removing objects.

### Recognition versus filtering

| Dictionary result | Effect |
| --- | --- |
| NULL | Try the next dictionary with the current input, including an earlier filter replacement. |
| Stop, an empty non-NULL result | End the chain and leave a position hole. |
| Terminal terms | End the chain. Language results have up to four alternatives. Symbol results use the bounded grouping below. |
| Filter replacement | Pass one token to the next dictionary. It is not a final term. |
| Final filter, or filter followed only by NULL results | No indexed term and no position hole. |

Language types can use `simple`, Snowball, synonym, or filtering dictionaries. A synonym dictionary before a terminal Charabia dictionary can finish analysis first. A terminal Charabia result can skip later dictionaries. External dictionaries keep their own stopword, stemming, and normalization rules. These switches do not control them.

Keep `blank` and `display_only` unmapped unless deliberately changing their behavior. Swapping language dictionaries does not index default punctuation and whitespace. `ts_lexize` hides filter flags, and `ts_debug` does not fully simulate filtering. Validate real chains with vectors, queries, and indexes.

## ASCII symbols

The default configuration maps all five symbol types to their corresponding dictionaries.
A custom configuration can use other standard dictionaries for these types.
Symbols do not carry private data from Parser to Dictionary. Each Dictionary validates its actual current input, including direct `ts_lexize` calls and earlier filter replacements.

| Dictionary | Recognized input and full-term spelling |
| --- | --- |
| `charabia.url` | ASCII hierarchical URLs with `scheme://` and an authority. Lowercase the scheme and literal host letters. Preserve percent-triplet spelling, path case, query order, fragment, dot segments, and literal port. |
| `charabia.host` | Finite DNS-shaped names, IPv4, or bracketed IPv6, with an optional decimal port. Lowercase host spelling without IP compression or numeric conversion. |
| `charabia.email` | ASCII dot-atom subset, including common plus addresses. Preserve local-part case and lowercase the domain. Single-label domains are accepted. |
| `charabia.file` | Explicit POSIX/Windows/relative paths, dotfiles, and dotted filenames. Preserve the full original spelling. No filesystem access or escape processing occurs. |
| `charabia.numword` | Mixed letters/numbers and bounded underscore/hyphen forms. Keep original and distinct lowercase full forms. Integers, decimals, scientific numbers, and versions keep their literal spelling. |

The symbol Template requires lowercase `Kind='url'`, `'host'`, `'email'`, `'file'`, or `'numword'`.
Its `Discard`, `Stem`, and `Normalize` options all default to true. Every combination is valid for each Kind.
Discard removes English word components with the existing English stop table. Stem adds an eligible English stem.
Normalize controls the final English normalization. Preparation and English casing still run when it is false.
These options do not lowercase or stem complete paths, change structural spelling, or delete complete forms and structural literals.
Language, English, and Filter are not accepted by this Template. New symbol dictionaries produce terminal results only.
An invalid Kind, unsupported option, or duplicate option that reaches INIT fails with `22023`.
Invalid Boolean syntax follows PostgreSQL's `42601` behavior. Native ALTER still merges same-spelling duplicate keys before INIT.

Components include bounded host labels, paths, segments, query keys/values, numeric parts, and English surface/stem terms.
Bare protocols and the final DNS label are excluded. No public-suffix database is used.
`100ms` retains `100ms`, `100`, and `ms`. Explicit `0x5fff` and qualified bare `5fff` retain both hex spellings.
Bare hex requires 4 through 64 characters, at least one digit and one a-f letter, and only hex characters.
Numeric literals take priority. `dead`, `cafe`, `1234`, `5ff`, and scientific `1e10` do not gain hex aliases.
The rule also classifies `abc123` as hex. That is a known ambiguity, not proof of its meaning.

Each complete form is an OR alternative. All remaining components form one AND group at the same position.
For example, plain analysis of `100ms` produces `100ms | (100 & ms)`.
The component group can match words scattered through a document. It does not establish structural equality.
`query_any` changes that AND to OR too. BM25 uses all terms as a bag and loses the group.
Shared parts such as `ms` can therefore retrieve other unit forms. Surface/stem parts can also make a complete-query fallback stricter across word forms.

A symbol is one position between surrounding words. Components have no internal phrase positions or Stop holes.
Ordinary surrounding stopwords still leave holes. Prefix flags apply to every analyzed term, including the component group.
Correctly quote punctuation-bearing operands under PostgreSQL query syntax. Prefix analysis is not a whole-URL starts-with operation.
Apply an explicit native match condition before the final LIMIT when filtering BM25 candidates.
Use application fields or structural predicates for literal equality. Headline is not a filter.

Recognition never decodes URLs, entities, escapes, values, or units.
Markup is ordinary source text: tags do not hide internal words or supported symbols.
Outside symbols, literal `<`, `>`, and `=` split ordinary words and remain unindexed separators.
Opaque URIs, quoted/international email, non-ASCII structures, and paths with spaces have no dedicated whole-form guarantee.
Unsupported or malformed structures retain ordinary analysis. Invalid URL envelopes are not shortened into valid prefixes.
URL authority uses URI grammar. A reg-name does not need to be a DNS name.
Percent escapes in the host retain their spelling. Non-DNS reg-names supply an opaque host component instead of DNS labels.
A valid URL can keep one additional sentence-punctuation recall alias. Its original whole token, including accepted trailing punctuation, remains visible in headline.
Dotted names prefer file for these suffixes: `c,h,cc,cpp,rs,py,js,ts,tsx,json,toml,yaml,yml,md,txt,log,sql,html,xml,css,sh,wasm,so,dll,exe`.
A `.rs` domain can therefore be treated as a file. Other DNS-shaped dotted names prefer host. No DNS or filesystem lookup resolves that ambiguity.

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

`ts_headline` matches real dictionary results to original text. Text, JSON, and JSONB share the implementation. A full or component hit marks the whole original symbol once. Subrange symbol highlighting is not supported. Normalization, stemming, prefix matching, position holes, and external dictionary chains use PostgreSQL's matching path.

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
| Raw token | At least 2047 bytes becomes display-only pieces of at most 2046 bytes, split on Unicode scalar boundaries. Default mappings do not index these pieces. A long symbol candidate follows this ordinary/display path instead of indexing a shortened full form. |
| Symbol expansion | At most 32 units after camel splitting/whole-word recovery and before Stop processing or deduplication. Structural literals also count. At most 66 emitted terms and 16 KiB of final term bytes per symbol. Exceeding a budget fails with `54000`, without truncation. |
| Final term | PostgreSQL stores a 2047-byte term and ignores a 2048-byte term. Diagnostic output does not prove indexability. |
| Positions | Positions saturate at 16383. A stored vector keeps at most 255 positions per term. |
| Headline | Each escaped entry is at most 65535 bytes. Output is at most 8 MiB per callback/string. Limit errors use `54000`. |

For JSON headline, limits apply to each string, not the complete JSON value. These callback limits do not bound earlier PostgreSQL matching allocations. Limit query complexity, highlight only selected results, and use statement timeouts for workload control.

Automatic selection uses only the 15 natural-language candidates. Existing
metadata outside that set becomes und. Missing Cyrillic, Hangul, and Thai
metadata maps to rus, kor, and tha. Other missing metadata becomes und without a
second detector. Same-script foreign text can still select a supported language.
There is no confidence threshold or English statistical fallback.

German compound segmentation and Latin camelCase segmentation are enabled.
Only a complete split ASCII-letter envelope in the fixed English set is restored
as one word. No spaces or punctuation are removed. Unsplit words are not forced
to English. A Dictionary sees only its current token, so ASCII fragments from a
non-ASCII word can also enter its English branch.

Short-query and mixed-language errors remain possible. Japanese and Korean
segmentation does not extract base forms or guarantee inflection matching.
The retained [quality cases](tests/fixtures/quality-known.json) preserve the
original inputs and failure records. Current validation finds the `den` and `var`
documents with English enabled. The short `cats were running` phrase still fails.
The two reviewed closures do not add any failure exemption. English alternatives can
also retain a foreign stopword such as German `die`, causing a false match.
Use an explicit `English=false` dictionary when that tradeoff is unsuitable.

Explicit controls do not establish that defaults satisfy every domain case.
Alternatives do not guarantee cross-form or phrase recall. They change BM25
term frequencies, document lengths, scores, and potentially ranks. Performance
and ranking reports keep these observations separate from index consistency.

## Analysis changes and index maintenance

Dictionary parameters, candidate/route sets, segmentation features, whole-word restoration, mappings, chain order, external rules, and resource changes can invalidate old indexed terms. Cache invalidation rebuilds process state, not vectors or indexes. A route/type-ID
change also requires correct new Configuration mappings and affected expression
or index definitions. Replacing a library or rebuilding postings alone does not
repair old numeric mappings. The build/resource fingerprint does not certify user chains or existing indexed data.
An existing Configuration COPY does not automatically gain new mappings or objects added to its source configuration.

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

- The exact extension package, control/SQL files, dynamic libraries, resource manifest, checksums, English TXT/attribution, and source/rebuild materials for the target time.
- Matching pg_textsearch files and preload setup when BM25 is used.
- External synonym or other dictionary rule files, plus required locale/OS dependencies and server/restore configuration.

WAL does not restore installation directories or replay later configuration-file edits. Embedded resources travel with their exact library. Do not substitute an unverified newer package for the target package.

Prefer a named restore point after analysis maintenance finishes. If the target falls between a parameter change and its rebuilds, keep traffic closed and finish maintenance. Physical replay does not rerun historical SQL or text analysis. A consistent restore cannot fix a semantic mismatch already present at the target.

After recovery, validate LSN/timeline, parameters, mappings, cold Dictionary initialization, loaded library hash, stored vectors, candidates, actual index plans, and headline. Changes to binaries, redo components, or index formats require a matching recovery rehearsal. See [PostgreSQL PITR](https://www.postgresql.org/docs/18/continuous-archiving.html) and the [technical recovery boundary](docs/architecture.md#persistent-state-and-recovery).
