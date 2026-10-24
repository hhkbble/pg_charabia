# Usage reference

This reference covers public SQL, dictionaries, queries, highlighting, and analysis limits. Start with the [README](../README.md) for runnable native FTS and optional BM25 examples. See [Installation and maintenance](operations.md) before installing files or changing indexed analysis. [Architecture](architecture.md) explains the implementation.

- [Languages and defaults](#languages-and-defaults)
- [Public SQL](#public-sql)
- [Configurations and dictionary chains](#configurations-and-dictionary-chains)
- [Structured ASCII tokens](#structured-ascii-tokens)
- [Queries and BM25](#queries-and-bm25)
- [Headline](#headline)
- [Limits and known quality issues](#limits-and-known-quality-issues)

## Languages and defaults

`public.charabia` and `public.charabia_linguistic` use ordinary linguistic analysis.
Use `public.charabia_structured` explicitly to add recognition and expansion of ASCII structures.
Both modes use the same language analysis and built-in resources.
Linguistic mode does not scan, overlay, or expand structured tokens.
It keeps ordinary token boundaries, including English whole-word restoration and literal markup separators.

Extension functions with an omitted configuration use `public.charabia`.
Native PostgreSQL functions without a configuration use the host's `default_text_search_config`.
The extension does not change that setting. Pass a configuration explicitly for stored vectors, indexes, queries, and headline.

Each linguistic dictionary enables built-in stopwords and stemming when supported. Stopwords are words that the dictionary discards. Stemming reduces a word to a stem, not a general dictionary form. Terminal mode keeps each enabled branch's surface and distinct stem at the same position. Up to four terms are OR alternatives.

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

## Public SQL

| Interface | Purpose |
| --- | --- |
| `public.charabia` | Default linguistic Text Search Configuration. |
| `public.charabia_linguistic` | Explicit linguistic Text Search Configuration. |
| `public.charabia_structured` | Explicit linguistic plus structured Text Search Configuration. |
| `charabia.linguistic_parser` | Ordinary Parser with 18 token types. |
| `charabia.structured_parser` | Parser with 18 ordinary and five structured token types. |
| `charabia.linguistic_template` | Linguistic Dictionary Template used with the parameters below. |
| `charabia.structured_template` | Terminal Dictionary Template for ASCII structures. |
| `charabia.url`, `charabia.host`, `charabia.email`, `charabia.file`, `charabia.numword` | Default structured dictionaries. |
| `charabia.eng`, `charabia.fra`, `charabia.cmn`, …, `charabia.und` | Default linguistic dictionaries. Discover the complete set with `charabia.analysis_info()`. |
| `charabia.analysis_info() RETURNS jsonb` | Static build/resource identity, language capabilities, rules for structured tokens, and defaults. It does not fingerprint user configurations or external dictionary files. |
| `charabia.analyze(text, config regconfig DEFAULT 'public.charabia') RETURNS TABLE` | Parser facts: `raw`, `kind`, `script`, `charabia_language`, `language`, `language_source`, `byte_start`, `byte_end`, `char_start`, `char_end`, `token_type`. |
| `charabia.query_any(text, config regconfig DEFAULT 'public.charabia') RETURNS tsquery` | Plain query analysis with AND changed to OR in the new query tree. |
| `pg_catalog.ts_headline` | Standard text, JSON, and JSONB headline using the selected configuration. |

`analyze` offsets are zero-based, half-open ranges. Byte offsets refer to UTF-8. Character offsets count Unicode scalar values, not grapheme clusters. Missing original Charabia language remains NULL. Blank/display and structured tokens have no selected language in the diagnostic output. A structured token reports its own kind and token type in structured mode.

`analyze` selects its mode from the configuration's actual extension Parser.
Configuration copies and renamed configurations, Parsers, or START functions retain their mode.
A configuration with a third-party Parser fails with `0A000`.
`analyze` reports Parser facts, so configurations with the same Parser can report the same rows despite different mappings.
It does not report final terms from a Dictionary chain. Use `to_tsvector` and query functions to inspect effective analysis. Diagnostic/query functions use `SECURITY INVOKER` and a fixed `search_path=pg_catalog`. Internal callbacks do not grant direct `PUBLIC EXECUTE`.

The linguistic Parser emits 16 language types, `blank`, and `display_only`, for 18 types.
The structured Parser adds five structured types, for 23 types.
`blank` and `display_only` have no default mapping and consume no word position.
Each final ordinary Parser token advances one position when its chain returns terms or Stop.
Stopwords leave position holes. Alternatives for one token share a position.
For example, ordinary `foo_bar.rs` has successive `foo`, `bar`, and `rs` positions.
Ordinary `100ms` stays one raw word without dedicated `100` or `ms` components.
Each structured token occupies one atomic position, with the query grouping described below.
Custom chains retain native PostgreSQL position behavior. See [Architecture](architecture.md) for the implementation.

## Configurations and dictionary chains

Keep default extension members as an unchanged baseline.
At installation, `public.charabia` copies `public.charabia_linguistic` and has the same Parser and mappings.
These are distinct configurations. Later mapping changes are independent, with no implicit synchronization.
PostgreSQL configurations do not forward to another configuration as an alias.

A configuration `COPY` copies mappings but shares dictionaries. Create a separate dictionary only where independent parameters are needed. ALTER on a shared dictionary affects every reference, including references from both modes. The [README custom configuration](../README.md#custom-configuration) replaces only the English mapping.

### Linguistic dictionary parameters

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
| Terminal terms | End the chain. Linguistic results have up to four alternatives. Structured token results use the bounded grouping below. |
| Filter replacement | Pass one token to the next dictionary. It is not a final term. |
| Final filter, or filter followed only by NULL results | No indexed term and no position hole. |

Language types can use `simple`, Snowball, synonym, or filtering dictionaries. A synonym dictionary before a terminal Charabia dictionary can finish analysis first. A terminal Charabia result can skip later dictionaries. External dictionaries keep their own stopword, stemming, and normalization rules. These switches do not control them.

Keep `blank` and `display_only` unmapped unless deliberately changing their behavior. Swapping linguistic dictionaries does not index default punctuation and whitespace. `ts_lexize` hides filter flags, and `ts_debug` does not fully simulate filtering. Validate real chains with vectors, queries, and indexes.

### Filter chain example

First run the [README quick start](../README.md#quick-start) and [custom configuration](../README.md#custom-configuration) in the same database. BM25 is not required. This example passes a normalized English surface to PostgreSQL's `simple` dictionary.

```sql
CREATE TEXT SEARCH DICTIONARY app.fold_eng(
 TEMPLATE=charabia.linguistic_template, Language='eng',
 Discard=false, Stem=false, Normalize=true, Filter=true
);
CREATE TEXT SEARCH CONFIGURATION app.filtered(PARSER=charabia.linguistic_parser);
ALTER TEXT SEARCH CONFIGURATION app.filtered
 ADD MAPPING FOR eng WITH app.fold_eng, pg_catalog.simple;
SELECT to_tsvector('app.filtered',body) FROM documents WHERE id=2;
```

`app.filtered` maps only English. Add mappings for other language types that the application needs. The Parser stays independent of the selected dictionaries.

## Structured ASCII tokens

`public.charabia_structured` maps all five structured token types to their corresponding dictionaries.
The two linguistic configurations have no structured token types or mappings.
Changing mappings cannot change a configuration's Parser or activate structured recognition.
A custom configuration can use other standard dictionaries for these types.
Structured tokens do not carry private data from Parser to Dictionary. Each Dictionary validates its actual current input, including direct `ts_lexize` calls and earlier filter replacements.

| Dictionary | Recognized input and full-term spelling |
| --- | --- |
| `charabia.url` | ASCII hierarchical URLs with `scheme://` and an authority. Lowercase the scheme and literal host letters. Preserve percent-triplet spelling, path case, query order, fragment, dot segments, and literal port. |
| `charabia.host` | Finite DNS-shaped names, IPv4, or bracketed IPv6, with an optional decimal port. Lowercase host spelling without IP compression or numeric conversion. |
| `charabia.email` | ASCII dot-atom subset, including common plus addresses. Preserve local-part case and lowercase the domain. Single-label domains are accepted. |
| `charabia.file` | Explicit POSIX/Windows/relative paths, dotfiles, and dotted filenames. Preserve the full original spelling. No filesystem access or escape processing occurs. |
| `charabia.numword` | Mixed letters/numbers and bounded underscore/hyphen forms. Keep original and distinct lowercase full forms. Integers, decimals, scientific numbers, and versions keep their literal spelling. |

The structured Template requires lowercase `Kind='url'`, `'host'`, `'email'`, `'file'`, or `'numword'`.
Its `Discard`, `Stem`, and `Normalize` options all default to true. Every combination is valid for each Kind.
Discard removes English word components with the existing English stop table. Stem adds an eligible English stem.
Normalize controls the final English normalization. Preparation and English casing still run when it is false.
These options do not lowercase or stem complete paths, change structural spelling, or delete complete forms and structural literals.
Language, English, and Filter are not accepted by this Template. New structured dictionaries produce terminal results only.
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

A structured token is one position between surrounding words. Components have no internal phrase positions or Stop holes.
Ordinary surrounding stopwords still leave holes. Prefix flags apply to every analyzed term, including the component group.
Correctly quote punctuation-bearing operands under PostgreSQL query syntax. Prefix analysis is not a whole-URL starts-with operation.
Apply an explicit native match condition before the final LIMIT when filtering BM25 candidates.
Use application fields or structural predicates for literal equality. Headline is not a filter.

Recognition never decodes URLs, entities, escapes, values, or units.
Markup is ordinary source text: tags do not hide internal words or supported structured tokens.
Outside structured tokens, literal `<`, `>`, and `=` split ordinary words and remain unindexed separators.
Opaque URIs, quoted/international email, non-ASCII structures, and paths with spaces have no dedicated whole-form guarantee.
Unsupported or malformed structures retain ordinary analysis. Invalid URL envelopes are not shortened into valid prefixes.
URL authority uses URI grammar. A reg-name does not need to be a DNS name.
Percent escapes in the host retain their spelling. Non-DNS reg-names supply an opaque host component instead of DNS labels.
A valid URL can keep one additional sentence-punctuation recall alias. Its original whole token, including accepted trailing punctuation, remains visible in headline.
Dotted names prefer file for these suffixes: `c,h,cc,cpp,rs,py,js,ts,tsx,json,toml,yaml,yml,md,txt,log,sql,html,xml,css,sh,wasm,so,dll,exe`.
A `.rs` domain can therefore be treated as a file. Other DNS-shaped dotted names prefer host. No DNS or filesystem lookup resolves that ambiguity.

### Custom structured dictionary example

Continue in the database from the [README quick start](../README.md#quick-start) and [custom configuration](../README.md#custom-configuration). BM25 and the filter example are not required. This example copies the structured configuration and replaces its numword mapping with an independent structured dictionary.

```sql
CREATE TEXT SEARCH CONFIGURATION app.structured(COPY=public.charabia_structured);
CREATE TEXT SEARCH DICTIONARY app.units(
 TEMPLATE=charabia.structured_template, Kind='numword',
 Discard=true, Stem=false, Normalize=true
);
ALTER TEXT SEARCH CONFIGURATION app.structured
 ALTER MAPPING FOR numword WITH app.units;
SELECT plainto_tsquery('app.structured','100ms');
SELECT ts_headline('app.structured','alpha 100ms beta',
                  plainto_tsquery('app.structured','ms'),'HighlightAll=true');
```

The component query for `ms` highlights the complete original `100ms` token. See [limits](#limits-and-known-quality-issues) for structured token expansion budgets.

## Queries and BM25

Complete the [optional pg_textsearch setup](operations.md#optional-pg_textsearch-setup) before running the [README BM25 example](../README.md#optional-bm25-ranking). Native FTS does not need pg_textsearch.

BM25 uses the configuration named by the index's required `text_config` option.
Its query analysis uses the index configuration. Select the intended mode when building the index.
Changing Dictionary parameters does not update existing postings.
See [index maintenance](operations.md#analysis-changes-and-index-maintenance) before changing indexed analysis.

BM25 uses bag-of-terms matching. Native `plainto_tsquery` uses AND. Phrase queries impose position rules. `query_any` accepts ordinary valid configurations and does not analyze final terms again. Build it once per statement, then highlight only the top results. The BM25 query still performs its own required analysis.

`query_any` analyzes plain text. It is not a phrase or Boolean query parser. Structured token components also become OR alternatives. Apply a native match condition before the final LIMIT when the application needs native query semantics for BM25 candidates. Headline output does not establish that condition.

## Headline

`ts_headline` uses the selected configuration and matches real dictionary results to original text.
Text, JSON, and JSONB share the implementation.
Linguistic mode marks matching ordinary raw tokens.
For example, `run` can mark only `Running` inside `https://Example.com/Running?q=100ms`.
It does not match `100ms` from a query for `ms`.
In structured mode, a full or component hit marks the whole original structured token once.
The same URL query can mark the complete URL.
Subrange structured token highlighting is not supported.
Normalization, stemming, prefix matching, position holes, and external dictionary chains use PostgreSQL's matching path.

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

Unknown or duplicate options, invalid syntax, and conflicting or out-of-range fragment settings fail with `22023`. Weighted query operands and unsupported ShortWord values fail with `0A000`. Marker or delimiter lengths above 256 bytes and output limit errors use `54000`.

`Escape=true` escapes `&`, `<`, `>`, double quotes, and apostrophes in source text. Markers and delimiters remain trusted markup. Supply trusted markup. `Escape=false` leaves source HTML unescaped, which the caller must handle.

## Limits and known quality issues

| Boundary | Behavior |
| --- | --- |
| Input | UTF8 database, valid UTF-8, no NUL, at most 262144 bytes per text or query input. |
| BM25 | Validate the complete document and query before upstream slicing. The Parser cannot recover the original full length. Keep a document CHECK like the quick start. |
| Raw token | At least 2047 bytes becomes display-only pieces of at most 2046 bytes, split on Unicode scalar boundaries. Default mappings do not index these pieces. A long structured token candidate follows this ordinary/display path instead of indexing a shortened full form. |
| Structured token expansion | Structured mode only. At most 32 units after camel splitting/whole-word recovery and before Stop processing or deduplication. Structural literals also count, except a literal identical to a complete form. At most 66 emitted terms and 16 KiB of final term bytes per structured token. Exceeding a budget fails with `54000`, without truncation. |
| Final term | PostgreSQL stores a 2047-byte term and ignores a 2048-byte term. Diagnostic output does not prove indexability. |
| Positions | Positions saturate at 16383. A stored vector keeps at most 255 positions per term. |
| Headline | Each escaped entry is at most 65535 bytes. Output is at most 8 MiB per callback/string. Limit errors use `54000`. |

For JSON headline, limits apply to each string, not the complete JSON value. These callback limits do not bound earlier PostgreSQL matching allocations. Limit query complexity, highlight only selected results, and use statement timeouts for workload control.

Short-query and mixed-language errors remain possible. Japanese and Korean
segmentation does not extract base forms or guarantee inflection matching.

A phrase query for `cats were running` can miss an English document that contains
that exact phrase. With stopword removal disabled, a BM25 search for the same
words can still return extra results.

English alternatives can retain a foreign stopword such as German `die`,
causing a false match in both native FTS and BM25.
Use an explicit `English=false` dictionary when that tradeoff is unsuitable.

Language and dictionary settings do not guarantee matching across word forms or
correct phrase results. English alternatives change BM25 term frequencies,
document lengths, scores, and potentially ranks. Test the default analysis and
any custom settings against representative documents and queries.

<a id="reviewed-release-warnings"></a>

### Known BM25 issues

BM25 can return extra results for queries such as `running`, `aimées`, and `hördes` in either mode. Extra matches occur with custom and generic prepared plans.
The `running` difference also occurs after index updates, maintenance, and
server restarts.

BM25 also has a known ranking error. A query such as `全文 running` can rank an
unexpected document first. Check result relevance and ordering before relying
on the first few matches.

Mode-specific release evidence separates mechanism checks, language quality failures, and reviewed warnings.
The release archive includes the original observations and verification evidence. See [Development](development.md#reviewed-release-warnings) for report
and release acceptance rules.
