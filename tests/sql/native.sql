\set ON_ERROR_STOP on
BEGIN;
CREATE SCHEMA pgcb_native_sql;
CREATE TEMP TABLE native_assertions(label text PRIMARY KEY);
CREATE FUNCTION pg_temp.assert_true(ok boolean, message text) RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'native assertion: %', message; END IF;
    INSERT INTO native_assertions VALUES(message);
END
$fn$;

-- Fixed selected-language contract. IDs do not follow the full whatlang enum.
CREATE TEMP TABLE native_routes(id integer PRIMARY KEY, alias text UNIQUE);
INSERT INTO native_routes SELECT ord::integer,alias FROM unnest(ARRAY[
 'eng','deu','nld','rus','spa','fra','por','ita','ara','tur','cmn','jpn','kor','vie','tha','und','blank','display_only','url','host','email','file','numword'
]) WITH ORDINALITY AS r(alias,ord);
SELECT pg_temp.assert_true(NOT EXISTS(
 (SELECT tokid,alias FROM ts_token_type('charabia.parser') EXCEPT SELECT id,alias FROM native_routes)
 UNION ALL (SELECT id,alias FROM native_routes EXCEPT SELECT tokid,alias FROM ts_token_type('charabia.parser'))),
 '23 fixed token IDs and aliases');
SELECT pg_temp.assert_true((SELECT count(*)=21 FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig),
 'default has exactly 21 mappings');
SELECT pg_temp.assert_true((SELECT (i->>'language_count')::integer=16
 AND jsonb_array_length(i->'languages')=16
 AND (SELECT count(*)=15 FROM jsonb_array_elements(i->'languages') l WHERE (l->>'has_stopwords')::boolean)
 AND (SELECT count(*)=10 FROM jsonb_array_elements(i->'languages') l WHERE l->>'stemmer' IS NOT NULL)
 AND NOT (i ? 'settings') FROM (SELECT charabia.analysis_info() AS i) s),
 'analysis_info reports static resources and capabilities without a configuration snapshot');
SELECT pg_temp.assert_true(NOT EXISTS(SELECT 1 FROM native_routes r LEFT JOIN pg_ts_config_map m
 ON m.mapcfg='public.charabia'::regconfig AND m.maptokentype=r.id WHERE r.id<=16 AND
 (m.mapseqno IS DISTINCT FROM 1 OR m.mapdict IS DISTINCT FROM (SELECT d.oid FROM pg_ts_dict d WHERE d.dictnamespace='charabia'::regnamespace AND d.dictname=r.alias))),
 'every language route has exactly its terminal dictionary');
SELECT pg_temp.assert_true(NOT EXISTS(SELECT 1 FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig AND maptokentype IN(17,18)),
 'blank and display-only have no default dictionary mapping');
SELECT pg_temp.assert_true((SELECT bool_and(d.dicttemplate=(SELECT oid FROM pg_ts_template WHERE tmplnamespace='charabia'::regnamespace AND tmplname='template'))
 FROM native_routes r JOIN pg_ts_dict d ON d.dictnamespace='charabia'::regnamespace AND d.dictname=r.alias WHERE r.id<=16),
 'all default dictionaries use the parameterized template');

CREATE TEMP TABLE native_abi(signature text,result text,symbol text);
INSERT INTO native_abi VALUES
 ('charabia.parser_start(internal,integer)','internal','parser_start_wrapper'),
 ('charabia.parser_gettoken(internal,internal,internal)','internal','parser_gettoken_wrapper'),
 ('charabia.parser_end(internal)','void','parser_end_wrapper'),
 ('charabia.parser_lextypes(internal)','internal','parser_lextypes_wrapper'),
 ('charabia.parser_headline(internal,internal,tsquery)','internal','parser_headline_wrapper'),
 ('charabia.dictionary_init(internal)','internal','dictionary_init_wrapper'),
 ('charabia.dictionary_lexize(internal,internal,internal,internal)','internal','dictionary_lexize_wrapper'),
 ('charabia.symbol_dictionary_init(internal)','internal','symbol_dictionary_init_wrapper'),
 ('charabia.symbol_dictionary_lexize(internal,internal,internal,internal)','internal','symbol_dictionary_lexize_wrapper');
SELECT pg_temp.assert_true((SELECT bool_and(coalesce(p.prorettype=to_regtype(a.result)
 AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='c') AND p.proisstrict
 AND p.prosrc=a.symbol AND NOT p.proretset,false))
 FROM native_abi a LEFT JOIN pg_proc p ON p.oid=to_regprocedure(a.signature)), 'actual callback ABI and symbols');
SELECT pg_temp.assert_true((SELECT prsstart='charabia.parser_start(internal,integer)'::regprocedure
 AND prstoken='charabia.parser_gettoken(internal,internal,internal)'::regprocedure
 AND prsend='charabia.parser_end(internal)'::regprocedure
 AND prslextype='charabia.parser_lextypes(internal)'::regprocedure
 AND prsheadline='charabia.parser_headline(internal,internal,tsquery)'::regprocedure
 FROM pg_ts_parser WHERE prsnamespace='charabia'::regnamespace AND prsname='parser'), 'parser registers real HEADLINE and lifecycle callbacks');
SELECT pg_temp.assert_true((SELECT tmplinit='charabia.dictionary_init(internal)'::regprocedure
 AND tmpllexize='charabia.dictionary_lexize(internal,internal,internal,internal)'::regprocedure
 FROM pg_ts_template WHERE tmplnamespace='charabia'::regnamespace AND tmplname='template'), 'terminal template callbacks');
SELECT pg_temp.assert_true((SELECT cfgparser=(SELECT oid FROM pg_ts_parser WHERE prsnamespace='charabia'::regnamespace AND prsname='parser') FROM pg_ts_config WHERE oid='public.charabia'::regconfig),
 'public default uses charabia parser');
SELECT pg_temp.assert_true((SELECT bool_and(proparallel<>'s') FROM pg_proc WHERE pronamespace='charabia'::regnamespace),
 'no unverified PARALLEL SAFE declaration');
SELECT pg_temp.assert_true((SELECT count(*)=21 FROM pg_ts_dict WHERE dictnamespace='charabia'::regnamespace),
 'extension installs sixteen language and five symbol dictionaries');
SELECT pg_temp.assert_true(NOT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='charabia'::regnamespace
 AND proname IN('create_config','alter_config','drop_config')),
 'no proprietary configuration management or compatibility functions remain');
SELECT pg_temp.assert_true((SELECT count(*)=3 AND bool_and(NOT prosecdef AND provolatile='s' AND proparallel='r'
 AND proconfig=ARRAY['search_path=pg_catalog']) FROM pg_proc WHERE pronamespace='charabia'::regnamespace
 AND proname IN('analysis_info','analyze','query_any')),
 'diagnostics and query helper use invoker privileges and a fixed safe search path');
SELECT pg_temp.assert_true(NOT EXISTS(SELECT 1 FROM native_abi a JOIN pg_proc p ON p.oid=to_regprocedure(a.signature),
 LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) acl
 WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'),
 'internal callback functions do not grant direct PUBLIC execution');


-- A native simple-mapped instance isolates exact PG positions/query syntax
-- from language classification and this extension's terminal Dictionary.
CREATE TEXT SEARCH CONFIGURATION pgcb_native_sql.stable(PARSER=charabia.parser);
DO $map$
DECLARE types text;
BEGIN
 SELECT string_agg(quote_ident(alias),',') INTO types FROM native_routes WHERE id<=16;
 EXECUTE 'ALTER TEXT SEARCH CONFIGURATION pgcb_native_sql.stable ADD MAPPING FOR '||types||' WITH pg_catalog.simple';
END $map$;
SELECT pg_temp.assert_true((SELECT count(*)=0 FROM ts_parse('charabia.parser','')), 'empty parser EOF');
SELECT pg_temp.assert_true(to_tsvector('public.charabia','')=''::tsvector, 'empty vector');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha. beta')=$v$'alpha':1 'beta':2$v$::tsvector,
 'hard separator has no extra position');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha, beta')=$v$'alpha':1 'beta':2$v$::tsvector,
 'soft separator has no extra position');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable',E'alpha \x01 beta')=$v$'alpha':1 'beta':2$v$::tsvector,
 'empty normalized control is Ignore without position');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha. beta') @@ phraseto_tsquery('pgcb_native_sql.stable','alpha beta'),
 'phrase crosses hard separator');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','POSTGRESQL beta') @@ plainto_tsquery('pgcb_native_sql.stable','postgresql BETA'),
 'plain query normalizes each operand');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha beta') @@ to_tsquery('pgcb_native_sql.stable','ALPHA & !gamma'),
 'explicit Boolean query');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','postgresql') @@ to_tsquery('pgcb_native_sql.stable','POSTGR:*'), 'prefix query');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha beta') @@ to_tsquery('pgcb_native_sql.stable',$q$'alpha beta'$q$),
 'quoted operand is analyzed');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha beta') @@ websearch_to_tsquery('pgcb_native_sql.stable','"alpha beta" OR gamma'),
 'websearch phrase and OR');
SELECT pg_temp.assert_true(NOT(to_tsvector('pgcb_native_sql.stable','beta alpha') @@ phraseto_tsquery('pgcb_native_sql.stable','alpha beta')),
 'phrase preserves word order');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','alpha beta') @@ to_tsquery('pgcb_native_sql.stable','alpha <-> beta'),
 'explicit adjacent query');

-- Route all lexical types to an explicit English builtin-stop dictionary.
-- This tests Stop positions independently of statistical language selection.
CREATE TEXT SEARCH DICTIONARY pgcb_native_sql.eng_stop(TEMPLATE=charabia.template,
 Language='eng',Discard=true,Stem=false,Normalize=true,Filter=false);
CREATE TEXT SEARCH CONFIGURATION pgcb_native_sql.stop(PARSER=charabia.parser);
DO $map$
DECLARE types text;
BEGIN
 SELECT string_agg(quote_ident(alias),',') INTO types FROM native_routes WHERE id<=16;
 EXECUTE 'ALTER TEXT SEARCH CONFIGURATION pgcb_native_sql.stop ADD MAPPING FOR '||types||' WITH pgcb_native_sql.eng_stop';
END $map$;
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stop','alpha THE beta')=$v$'alpha':1 'beta':3$v$::tsvector,
 'canonical stop consumes exactly one position');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stop','alpha the beta') @@ phraseto_tsquery('pgcb_native_sql.stop','alpha the beta'),
 'phrase retains a stop hole');
SELECT pg_temp.assert_true(NOT(to_tsvector('pgcb_native_sql.stop','alpha the beta') @@ phraseto_tsquery('pgcb_native_sql.stop','alpha beta')),
 'adjacent phrase excludes stop hole');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stop','alpha the beta') @@ to_tsquery('pgcb_native_sql.stop','alpha <2> beta'),
 'explicit phrase distance honors stop hole');
SELECT pg_temp.assert_true(plainto_tsquery('pgcb_native_sql.stop','the')=''::tsquery, 'all-stop plain query empty');
SELECT pg_temp.assert_true((SELECT lexemes=ARRAY[]::text[] FROM ts_debug('pgcb_native_sql.stop','the')), 'ts_debug recognizes Stop array');

-- Frozen English paragraph from independent convergence evidence; the short
-- English phrase quality failure is intentionally not relabeled as a pass.
DO $fn$
DECLARE body text := 'The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running.';
        vector tsvector; surface smallint[]; stem smallint[];
BEGIN
 vector:=to_tsvector('public.charabia',body);
 SELECT positions INTO surface FROM unnest(vector) WHERE lexeme='running';
 SELECT positions INTO stem FROM unnest(vector) WHERE lexeme='run';
 PERFORM pg_temp.assert_true(surface=stem AND surface[1]=4 AND cardinality(surface)=2, 'surface and single stem share both original positions');
 PERFORM pg_temp.assert_true(strpos(plainto_tsquery('public.charabia',body)::text,'''running'' | ''run''')>0, 'plain query uses OR for same-position variants');
 PERFORM pg_temp.assert_true(vector @@ phraseto_tsquery('public.charabia',body), 'same-context native phrase matches dual variants and stop holes');
END
$fn$;

-- Force the independently calculated Arabic expansion through Arabic rules;
-- automatic language quality is not the normalized-byte boundary oracle.
CREATE TEXT SEARCH DICTIONARY pgcb_native_sql.ara(TEMPLATE=charabia.template,
 Language='ara',Discard=false,Stem=false,Normalize=true,Filter=false);
CREATE TEXT SEARCH CONFIGURATION pgcb_native_sql.arabic(PARSER=charabia.parser);
DO $map$
DECLARE types text;
BEGIN
 SELECT string_agg(quote_ident(alias),',') INTO types FROM native_routes WHERE id<=16;
 EXECUTE 'ALTER TEXT SEARCH CONFIGURATION pgcb_native_sql.arabic ADD MAPPING FOR '||types||' WITH pgcb_native_sql.ara';
END $map$;
CREATE TEXT SEARCH DICTIONARY pgcb_native_sql.eng_boundary(TEMPLATE=charabia.template,
 Language='eng',Discard=false,Stem=true,Normalize=false,Filter=false);
SELECT pg_temp.assert_true(cardinality(ts_lexize('pgcb_native_sql.eng_boundary',repeat('a',997)||'ies'))=2,
 'prepared 1000-byte Snowball input retains surface and different stem');
SELECT pg_temp.assert_true(ts_lexize('pgcb_native_sql.eng_boundary',repeat('a',998)||'ies')=ARRAY[repeat('a',998)||'ies'],
 'prepared 1001-byte word skips stemming and retains its surface');
SELECT pg_temp.assert_true(ts_lexize('pgcb_native_sql.eng_boundary',repeat('ﬃ',334))=ARRAY[repeat('ffi',334)],
 'compatibility expansion is checked after prepare before stemming');

-- Actual PG18.6 byte/position contracts, independently measured before general.
SELECT current_setting('server_version_num') AS native_boundary_pg_version;
DO $fn$
DECLARE n integer; raw text; normalized text; actual integer;
BEGIN
 FOREACH n IN ARRAY ARRAY[2046,2047,2048] LOOP
  raw:=repeat('a',n);
  PERFORM pg_temp.assert_true((SELECT string_agg(token,'' ORDER BY ord)=raw AND bool_and(octet_length(token)<=2046)
   FROM ts_parse('charabia.parser',raw) WITH ORDINALITY t(id,token,ord)), 'raw reconstruction and bounded span '||n);
  PERFORM pg_temp.assert_true((SELECT bool_and(CASE WHEN n>=2047 THEN tokid=18 ELSE tokid<>18 END)
   FROM ts_parse('charabia.parser',raw)), 'display-only boundary '||n);
  PERFORM pg_temp.assert_true(octet_length((ts_lexize('charabia.und',raw))[1])=n, 'direct lexize raw byte boundary '||n);
  SELECT length(to_tsvector('pgcb_native_sql.stable',raw)) INTO actual;
  PERFORM pg_temp.assert_true(actual=CASE WHEN n<2047 THEN 1 ELSE 0 END, 'oversized raw word is not indexed '||n);
 END LOOP;
 FOREACH n IN ARRAY ARRAY[2047,2048] LOOP
  raw:=CASE WHEN n=2047 THEN repeat('ﷺ',61)||repeat('ا',17) ELSE repeat('ﷺ',62)||'ا' END;
  PERFORM pg_temp.assert_true((SELECT count(*)=1 FROM ts_parse('charabia.parser',raw)), 'one raw expansion token '||n);
  normalized:=(ts_lexize('pgcb_native_sql.ara',raw))[1];
  PERFORM pg_temp.assert_true(octet_length(raw)<2047 AND octet_length(normalized)=n, 'independent NFKD byte arithmetic '||n);
  SELECT length(to_tsvector('pgcb_native_sql.arabic',raw)) INTO actual;
  PERFORM pg_temp.assert_true(actual=CASE WHEN n=2047 THEN 1 ELSE 0 END, 'PG normalized byte boundary '||n);
 END LOOP;
END
$fn$;
DO $fn$
DECLARE n integer; actual smallint[];
BEGIN
 FOREACH n IN ARRAY ARRAY[255,256,300] LOOP
  SELECT positions INTO actual FROM unnest(to_tsvector('pgcb_native_sql.stable',repeat('alpha ',n)));
  PERFORM pg_temp.assert_true(cardinality(actual)=least(n,255) AND actual[1]=1 AND actual[cardinality(actual)]=least(n,255),
   'stored per-lexeme position boundary '||n);
 END LOOP;
 SELECT positions INTO actual FROM unnest(to_tsvector('pgcb_native_sql.stable',repeat('alpha ',16382)||'beta beta')) WHERE lexeme='beta';
 PERFORM pg_temp.assert_true(actual=ARRAY[16383]::smallint[], 'positions saturate at 16383 and deduplicate');
END
$fn$;
DO $fn$
BEGIN
 BEGIN
  PERFORM to_tsvector('public.charabia',repeat(' ',262145));
  RAISE EXCEPTION 'input bound not enforced';
 EXCEPTION WHEN program_limit_exceeded THEN NULL;
 END;
 PERFORM pg_temp.assert_true(to_tsvector('pgcb_native_sql.stable','PostgreSQL') @@ plainto_tsquery('pgcb_native_sql.stable','POSTGRESQL'),
 'same backend recovers after caught ERROR');
 PERFORM pg_temp.assert_true(ts_headline('public.charabia','CAFÉ','cafe'::tsquery,'HighlightAll=true')='<b>CAFÉ</b>',
 'standard headline participates in the actual parser');
END
$fn$;
SELECT count(*) AS native_sql_assertions,array_agg(label ORDER BY label) AS assertions FROM native_assertions;
DROP SCHEMA pgcb_native_sql CASCADE;
COMMIT;
