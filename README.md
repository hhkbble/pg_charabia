# pg_charabia

pg_charabia adds multilingual linguistic analysis to PostgreSQL 18 with [Charabia](https://github.com/meilisearch/charabia). Use it with native full-text search (FTS), GIN indexes, and `ts_headline`. Optional BM25 ranking uses [pg_textsearch](https://github.com/timescale/pg_textsearch).

Default dictionaries cover 15 languages plus `und` for an undetermined language. They enable built-in stopwords and stemming where supported. The default `public.charabia` and the named `public.charabia_linguistic` configuration use ordinary language analysis. Select `public.charabia_structured` to also recognize ASCII URLs, hosts, email addresses, file paths, and mixed letter/number forms.

[Usage reference](docs/reference.md) · [Installation and maintenance](docs/operations.md) · [Architecture](docs/architecture.md) · [Development](docs/development.md)

## Requirements

Use PostgreSQL 18 with a UTF8 database and a matching pg_charabia library. Native FTS does not require pg_textsearch. BM25 also requires the pinned pg_textsearch build, its dynamic dependencies, and preload setup before server startup.

Linux AMD64 is the release build and validation target. macOS ARM64 has an isolated local development runner without BM25 or recovery suites. Other platforms have no repository validation entry point. See [supported environments](docs/operations.md#supported-environments) for the exact scope and version sources.

## Installation

Download the Linux AMD64 package and its SHA256 file from the [v0.4.1 release](https://github.com/hhkbble/pg_charabia/releases/tag/v0.4.1). The complete archive includes the extension binary, source, resources, notices, and verification evidence.

- [Build from source](docs/development.md#build-and-test) with the pinned prerequisites and isolated runners.
- [Download, verify, and install the release archive](docs/operations.md#download-and-install) into a matching Linux AMD64 PostgreSQL 18 installation.

Version 0.4.1 uses the fresh installation baseline introduced in 0.4.0. It has no SQL upgrade script from earlier versions. Keep the exact old package for old recovery targets. Recreate application definitions and analysis in a new database when moving existing source text to this baseline.

The SQL below assumes that extension files are installed. A database superuser runs `CREATE EXTENSION pg_charabia`. Installation reserves the fixed `charabia` schema and the three configurations named above. Read [schema ownership and permissions](docs/operations.md#schema-ownership-and-permissions) before installation.

## Quick start

Connect to a fresh UTF8 database. Use a trusted `search_path` that creates the example table in `public`. Run this README's three SQL blocks in order in the same database. Native FTS users can skip the optional third block.

```sql
CREATE EXTENSION pg_charabia;
SELECT charabia.analysis_info();
SELECT * FROM charabia.analyze('中文全文检索 PostgreSQL');
SELECT * FROM charabia.analyze('GET https://Example.com/Running?q=100ms');
SELECT * FROM charabia.analyze('GET https://Example.com/Running?q=100ms',
                               'public.charabia_structured');
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

The first diagnostic uses ordinary tokens. The explicit structured diagnostic keeps the URL as one token. Native functions without an explicit configuration still follow PostgreSQL’s `default_text_search_config`. The extension does not change that setting.

The search returns document `1`. The headline marks both original `running` words in document `2` from a query for `run`. The GIN expression uses the same configuration for documents and queries.

## Custom configuration

`public.charabia` copies the linguistic Parser and mappings at installation. Later mapping changes are independent. A configuration `COPY` copies mappings but shares dictionaries. Create a separate dictionary for independent parameters. ALTER on a shared dictionary affects every reference. This example replaces only the English mapping.

```sql
CREATE SCHEMA app;
CREATE TEXT SEARCH CONFIGURATION app.search(COPY=public.charabia);
CREATE TEXT SEARCH DICTIONARY app.search_eng(
 TEMPLATE=charabia.linguistic_template, Language='eng',
 Discard=true, Stem=false, Normalize=true, Filter=false
);
ALTER TEXT SEARCH CONFIGURATION app.search
 ALTER MAPPING FOR eng WITH app.search_eng;
-- Native ALTER keeps other stored options. This instance has no indexes yet.
ALTER TEXT SEARCH DICTIONARY app.search_eng(Stem=true);
SELECT ts_lexize('app.search_eng','running');
SELECT charabia.query_any('全文 banana','app.search');
```

Keep default extension members as an unchanged baseline. For indexed configurations, [analysis changes require vector and index maintenance](docs/operations.md#analysis-changes-and-index-maintenance). See the [dictionary parameters and chains](docs/reference.md#configurations-and-dictionary-chains), [filter example](docs/reference.md#filter-chain-example), and [custom structured token example](docs/reference.md#custom-structured-dictionary-example).

## Optional BM25 ranking

Complete the [pg_textsearch installation and preload setup](docs/operations.md#optional-pg_textsearch-setup) before this block. Native FTS users can skip it. The example uses the documents from the quick start.

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

The index path returns document `1`. Its plan contains an `Index Scan` on `documents_bm25`. Disabling sequential scans demonstrates that path on a tiny example table. It is not an application-wide planner recommendation.

BM25 matches a bag of terms. Native AND, phrase, and structured token component queries have different semantics. See [Queries and BM25](docs/reference.md#queries-and-bm25) before combining ranking and match conditions.

## Limits and maintenance

Validate complete documents and queries against the 262144-byte input limit before BM25 slices them. Keep the document CHECK from the quick start. See [all limits and known quality issues](docs/reference.md#limits-and-known-quality-issues).

Short queries, mixed-language text, and phrase searches can miss expected matches. In the explicit structured mode, token components can match scattered words or shared parts. Headline is a presentation step, not a retrieval filter. See [structured token semantics](docs/reference.md#structured-ascii-tokens) and [headline rules](docs/reference.md#headline).

The mode choice does not resolve the known language false matches or ranking errors, including false matches for German `die`. Review the [known BM25 issues](docs/reference.md#known-bm25-issues) and test representative documents and queries before adoption.

Changes to dictionaries, mappings, chains, or resources can invalidate indexed analysis. Stored vectors need recomputation. REINDEX alone reuses an ordinary stored vector. Follow [analysis changes and index maintenance](docs/operations.md#analysis-changes-and-index-maintenance) for the required action for each representation.

Backups and WAL do not replace the exact extension package, resources, and external dictionary rules. Retain these materials for the recovery target. See [Backup and recovery](docs/operations.md#backup-and-recovery).
