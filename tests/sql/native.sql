\set ON_ERROR_STOP on
BEGIN;
CREATE SCHEMA pgcb_native_sql;
CREATE TEMP TABLE native_assertions(label text PRIMARY KEY);
CREATE FUNCTION pg_temp.assert_true(ok boolean, message text) RETURNS void
LANGUAGE plpgsql AS $fn$
BEGIN
    IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'native assertion: %', message; END IF;
    INSERT INTO native_assertions VALUES(message);
END
$fn$;

-- These published IDs and names are an external contract, not an implementation oracle.
CREATE TEMP TABLE native_routes(id integer PRIMARY KEY, alias text UNIQUE);
INSERT INTO native_routes VALUES
    (1,'basic'),(2,'case'),(3,'latin'),(4,'latin_swe'),(5,'greek'),(6,'marks'),
    (7,'arabic'),(8,'persian'),(9,'cj_chinese'),(10,'cj_japanese'),(11,'cj_unknown'),
    (12,'cj_other'),(13,'basic_swe'),(14,'case_swe'),(15,'greek_swe'),
    (16,'marks_swe'),(17,'arabic_swe'),(18,'cj_other_swe');
SELECT pg_temp.assert_true(
    NOT EXISTS((SELECT tokid,alias FROM ts_token_type('charabia.parser_v1')
        EXCEPT SELECT id,alias FROM native_routes)
        UNION ALL (SELECT id,alias FROM native_routes
        EXCEPT SELECT tokid,alias FROM ts_token_type('charabia.parser_v1'))),
    '18 stable route IDs and aliases');
SELECT pg_temp.assert_true((SELECT count(*)=18 FROM pg_ts_config_map
    WHERE mapcfg='charabia.default_v1'::regconfig), 'exactly 18 default mappings');
SELECT pg_temp.assert_true(NOT EXISTS(SELECT 1 FROM native_routes r
    LEFT JOIN pg_ts_config_map m ON m.mapcfg='charabia.default_v1'::regconfig AND m.maptokentype=r.id
    WHERE m.mapseqno IS DISTINCT FROM 1 OR m.mapdict IS DISTINCT FROM
        ('charabia.norm_' || r.alias || '_v1')::regdictionary::oid),
    'each route has its sole matching terminal dictionary');
SELECT pg_temp.assert_true((SELECT bool_and(d.dicttemplate=t.oid)
    FROM native_routes r JOIN pg_ts_dict d ON d.oid=('charabia.norm_'||r.alias||'_v1')::regdictionary
    CROSS JOIN pg_ts_template t WHERE t.tmplnamespace='charabia'::regnamespace AND t.tmplname='normalize_v1'),
    'all dictionaries use the registered template');
CREATE TEMP TABLE native_abi(signature text, result text, symbol text);
INSERT INTO native_abi VALUES
    ('charabia.parser_start_v1(internal,integer)','internal','parser_start_v1_wrapper'),
    ('charabia.parser_gettoken_v1(internal,internal,internal)','internal','parser_gettoken_v1_wrapper'),
    ('charabia.parser_end_v1(internal)','void','parser_end_v1_wrapper'),
    ('charabia.parser_lextypes_v1(internal)','internal','parser_lextypes_v1_wrapper'),
    ('charabia.dictionary_init_v1(internal)','internal','dictionary_init_v1_wrapper'),
    ('charabia.dictionary_lexize_v1(internal,internal,internal,internal)','internal','dictionary_lexize_v1_wrapper');
SELECT pg_temp.assert_true((SELECT bool_and(coalesce(p.prorettype=to_regtype(a.result)
        AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='c') AND p.proisstrict
        AND p.prosrc=a.symbol AND NOT p.proretset,false))
    FROM native_abi a LEFT JOIN pg_proc p ON p.oid=to_regprocedure(a.signature)),
    'callback SQL signatures and C symbols');
SELECT pg_temp.assert_true((SELECT prsstart='charabia.parser_start_v1(internal,integer)'::regprocedure
    AND prstoken='charabia.parser_gettoken_v1(internal,internal,internal)'::regprocedure
    AND prsend='charabia.parser_end_v1(internal)'::regprocedure
    AND prslextype='charabia.parser_lextypes_v1(internal)'::regprocedure AND prsheadline=0
    FROM pg_ts_parser WHERE prsnamespace='charabia'::regnamespace AND prsname='parser_v1'),
    'parser callbacks registered without HEADLINE');
SELECT pg_temp.assert_true((SELECT tmplinit='charabia.dictionary_init_v1(internal)'::regprocedure
    AND tmpllexize='charabia.dictionary_lexize_v1(internal,internal,internal,internal)'::regprocedure
    FROM pg_ts_template WHERE tmplnamespace='charabia'::regnamespace AND tmplname='normalize_v1'),
    'template callbacks registered');
SELECT pg_temp.assert_true((SELECT cfgparser=(SELECT oid FROM pg_ts_parser WHERE prsnamespace='charabia'::regnamespace AND prsname='parser_v1')
    FROM pg_ts_config WHERE oid='charabia.default_v1'::regconfig), 'configuration parser');
SELECT pg_temp.assert_true((SELECT bool_and(proparallel <> 's') FROM pg_proc
    WHERE pronamespace='charabia'::regnamespace), 'no unverified PARALLEL SAFE declaration');

SELECT pg_temp.assert_true((SELECT count(*)=0 FROM ts_parse('charabia.parser_v1','')),
    'empty parser EOF');
SELECT pg_temp.assert_true((SELECT string_agg(token,'' ORDER BY ord)='中文全文检索 PostgreSQL 18 支持 BM25'
    FROM ts_parse('charabia.parser_v1','中文全文检索 PostgreSQL 18 支持 BM25') WITH ORDINALITY t(id,token,ord)),
    'raw parser reconstruction');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','中文全文检索 PostgreSQL 18 支持 BM25')=
    $v$'18':5 'bm25':7 'postgresql':4 '中文':1 '全文':2 '檢索':3 '支持':6$v$::tsvector,
    'recorded direct Charabia native lexemes and positions');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','')=''::tsvector,
    'empty text has no lexemes');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','running runs runner')=
    $v$'runner':3 'running':1 'runs':2$v$::tsvector, 'no invented English stemming');
SELECT pg_temp.assert_true(ts_lexize('charabia.norm_latin_v1',' ') IS NULL,
    'direct separator Ignore');
SELECT pg_temp.assert_true(ts_lexize('charabia.norm_latin_v1','The')=ARRAY['the'],
    'direct Dictionary callable independently of parser');
SELECT pg_temp.assert_true(ts_lexize('charabia.norm_arabic_v1','ـ') IS NULL,
    'empty normalized Word is Ignore');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha. beta')=
    $v$'alpha':1 'beta':2$v$::tsvector, 'hard separator does not add a position');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha, beta')=
    $v$'alpha':1 'beta':2$v$::tsvector, 'soft separator does not add a position');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha. beta') @@
    phraseto_tsquery('charabia.default_v1','alpha beta'), 'phrase can cross hard separator');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','POSTGRESQL beta') @@
    plainto_tsquery('charabia.default_v1','postgresql BETA'), 'plain query analysis');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha beta') @@
    to_tsquery('charabia.default_v1','ALPHA & !gamma'), 'to_tsquery operators');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','postgresql') @@
    to_tsquery('charabia.default_v1','POSTGR:*'), 'prefix query');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha beta') @@
    to_tsquery('charabia.default_v1',$q$'alpha beta'$q$), 'quoted query operand analysis');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha beta') @@
    websearch_to_tsquery('charabia.default_v1','"alpha beta" OR gamma'), 'websearch phrase and OR');
SELECT pg_temp.assert_true(NOT(to_tsvector('charabia.default_v1','beta alpha') @@
    phraseto_tsquery('charabia.default_v1','alpha beta')), 'phrase word order');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha beta') @@
    to_tsquery('charabia.default_v1','alpha <-> beta'), 'explicit adjacent query');

-- A uniform stoplist must replace every route, including non-Latin routes.
CREATE TEXT SEARCH CONFIGURATION pgcb_native_sql.stop (COPY=charabia.default_v1);
DO $fn$
DECLARE r record;
BEGIN
    FOR r IN SELECT alias FROM native_routes ORDER BY id LOOP
        EXECUTE format('CREATE TEXT SEARCH DICTIONARY pgcb_native_sql.%I (TEMPLATE=charabia.normalize_v1,Route=%L,StopWords=%L)',
            'stop_'||r.alias,r.alias,'["the","的"]');
        EXECUTE format('ALTER TEXT SEARCH CONFIGURATION pgcb_native_sql.stop ALTER MAPPING FOR %I WITH pgcb_native_sql.%I',
            r.alias,'stop_'||r.alias);
    END LOOP;
END
$fn$;
SELECT pg_temp.assert_true((SELECT count(*)=18 AND bool_and(mapseqno=1)
    FROM pg_ts_config_map WHERE mapcfg='pgcb_native_sql.stop'::regconfig), 'custom uniform stoplist mapping');
SELECT pg_temp.assert_true(ts_lexize('pgcb_native_sql.stop_latin','the')=ARRAY[]::text[],
    'StopWord allocated empty lexeme array');
SELECT pg_temp.assert_true(ts_lexize('pgcb_native_sql.stop_latin','The')=ARRAY['the'],
    'stopword comparison precedes lowercase');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stop','alpha the beta')=
    $v$'alpha':1 'beta':3$v$::tsvector, 'StopWord consumes a position');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stop','alpha the beta') @@
    phraseto_tsquery('pgcb_native_sql.stop','alpha the beta'), 'phrase query retains stopword hole');
SELECT pg_temp.assert_true(NOT(to_tsvector('pgcb_native_sql.stop','alpha the beta') @@
    phraseto_tsquery('pgcb_native_sql.stop','alpha beta')), 'adjacent phrase excludes stopword hole');
SELECT pg_temp.assert_true(to_tsvector('pgcb_native_sql.stop','alpha the beta') @@
    to_tsquery('pgcb_native_sql.stop','alpha <2> beta'), 'explicit phrase distance');
SELECT pg_temp.assert_true(plainto_tsquery('pgcb_native_sql.stop','the')=''::tsquery,
    'all-stopword plain query is empty');
SELECT pg_temp.assert_true((SELECT lexemes=ARRAY[]::text[] AND dictionary='pgcb_native_sql.stop_latin'::regdictionary
    FROM ts_debug('pgcb_native_sql.stop','the')), 'ts_debug independent stopword classification');
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','alpha the beta')=
    $v$'alpha':1 'beta':3 'the':2$v$::tsvector, 'custom configuration leaves default unchanged');

-- PG18.6 limits are measured in bytes, before and after normalization respectively.
SELECT current_setting('server_version_num') AS native_boundary_pg_version;
DO $fn$
DECLARE n integer; raw text; normalized text; actual integer; expected integer;
BEGIN
    FOREACH n IN ARRAY ARRAY[2046,2047,2048] LOOP
        raw:=repeat('a',n);
        PERFORM pg_temp.assert_true((SELECT count(*)=1 AND max(octet_length(token))=n
            FROM ts_parse('charabia.parser_v1',raw)), 'raw parser length '||n);
        PERFORM pg_temp.assert_true(octet_length((ts_lexize('charabia.norm_latin_v1',raw))[1])=n,
            'direct lexize bypasses raw vector limit '||n);
        SELECT length(to_tsvector('charabia.default_v1',raw)) INTO actual;
        expected:=CASE WHEN n<2047 THEN 1 ELSE 0 END;
        PERFORM pg_temp.assert_true(actual=expected,'PG18.6 raw vector byte boundary '||n);
    END LOOP;
    -- U+FDFA NFKD expands to 33 UTF-8 bytes, including spaces, within one raw Arabic word.
    FOREACH n IN ARRAY ARRAY[2047,2048] LOOP
        raw:=CASE WHEN n=2047 THEN repeat('ﷺ',61)||repeat('ا',17) ELSE repeat('ﷺ',62)||'ا' END;
        PERFORM pg_temp.assert_true((SELECT count(*)=1 FROM ts_parse('charabia.parser_v1',raw)),
            'one raw token for normalized length '||n);
        normalized:=(ts_lexize('charabia.norm_arabic_v1',raw))[1];
        PERFORM pg_temp.assert_true(octet_length(raw)<2047 AND octet_length(normalized)=n,
            'independent NFKD expansion byte arithmetic '||n);
        SELECT length(to_tsvector('charabia.default_v1',raw)) INTO actual;
        PERFORM pg_temp.assert_true(actual=CASE WHEN n=2047 THEN 1 ELSE 0 END,
            'normalized vector byte boundary '||n);
    END LOOP;
END
$fn$;
DO $fn$
DECLARE n integer; actual smallint[];
BEGIN
    FOREACH n IN ARRAY ARRAY[255,256,300] LOOP
        SELECT positions INTO actual FROM unnest(to_tsvector('charabia.default_v1',repeat('alpha ',n)));
        PERFORM pg_temp.assert_true(cardinality(actual)=least(n,255)
            AND actual[1]=1 AND actual[cardinality(actual)]=least(n,255),
            'per-lexeme stored-position limit '||n);
    END LOOP;
    SELECT positions INTO actual FROM unnest(to_tsvector('charabia.default_v1',repeat('alpha ',16382)||'beta beta'))
        WHERE lexeme='beta';
    PERFORM pg_temp.assert_true(actual=ARRAY[16383]::smallint[],'positions saturate at 16383 and deduplicate');
END
$fn$;

DO $fn$
BEGIN
    BEGIN
        PERFORM to_tsvector('charabia.default_v1',repeat(' ',262145));
        RAISE EXCEPTION 'input bound not enforced';
    EXCEPTION WHEN program_limit_exceeded THEN NULL;
    END;
    BEGIN
        PERFORM ts_headline('charabia.default_v1','hello',plainto_tsquery('charabia.default_v1','hello'));
        RAISE EXCEPTION 'HEADLINE unexpectedly enabled';
    EXCEPTION WHEN feature_not_supported THEN NULL;
    END;
    PERFORM pg_temp.assert_true(to_tsvector('charabia.default_v1','PostgreSQL') @@
        plainto_tsquery('charabia.default_v1','POSTGRESQL'), 'same backend works after caught errors');
END
$fn$;
SELECT count(*) AS native_sql_assertions, array_agg(label ORDER BY label) AS assertions FROM native_assertions;
DROP SCHEMA pgcb_native_sql CASCADE;
COMMIT;
