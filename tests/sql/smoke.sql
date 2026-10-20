\set ON_ERROR_STOP on
CREATE SCHEMA smoke;
CREATE FUNCTION pg_temp.assert_true(ok boolean, message text) RETURNS void
LANGUAGE plpgsql AS $$ BEGIN IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION '%', message; END IF; END $$;

SELECT pg_temp.assert_true((SELECT count(*)=18 FROM ts_token_type('charabia.parser_v1')), '18 stable token types');
SELECT pg_temp.assert_true((SELECT string_agg(token,'')='中文全文检索 PostgreSQL 18 支持 BM25'
    FROM ts_parse('charabia.parser_v1','中文全文检索 PostgreSQL 18 支持 BM25')), 'parser preserves every raw byte');
SELECT * FROM ts_debug('charabia.default_v1','中文全文检索 PostgreSQL 18 支持 BM25');
SELECT charabia.analysis_info_v1();
SELECT pg_temp.assert_true(to_tsvector('charabia.default_v1','中文全文检索 PostgreSQL 18 支持 BM25') =
    $v$'18':5 'bm25':7 'postgresql':4 '中文':1 '全文':2 '檢索':3 '支持':6$v$::tsvector, 'direct Charabia golden and PG positions');
SELECT pg_temp.assert_true(ts_lexize('charabia.norm_latin_v1',' ') IS NULL, 'separator is pointer NULL, not SQL NULL ERROR');
CREATE TEXT SEARCH DICTIONARY smoke.stop_latin(TEMPLATE=charabia.normalize_v1, Route='latin', StopWords='["the"]');
SELECT pg_temp.assert_true(ts_lexize('smoke.stop_latin','the')=ARRAY[]::text[], 'StopWord allocated empty result');
SELECT pg_temp.assert_true(ts_lexize('smoke.stop_latin','The')=ARRAY['the'], 'stopword check is before lowercase');

CREATE TABLE smoke.docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));
INSERT INTO smoke.docs VALUES
    (1,'中文全文检索 PostgreSQL 18 支持 BM25'),
    (2,'unrelated banana'),(3,'POSTGRESQL English documentation');
CREATE INDEX docs_bm25 ON smoke.docs USING bm25(body) WITH(text_config='charabia.default_v1');
ANALYZE smoke.docs;
EXPLAIN(FORMAT JSON,COSTS OFF) SELECT id FROM smoke.docs
    ORDER BY body <@> to_bm25query('全文','smoke.docs_bm25') LIMIT 20;
SET enable_seqscan=off;
CREATE FUNCTION pg_temp.assert_smoke(q text,expected integer[]) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE plan json; actual integer[]; result_query text;
BEGIN
    result_query := 'SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::integer[]) FROM (SELECT id FROM smoke.docs ORDER BY body <@> to_bm25query($1,''smoke.docs_bm25'') LIMIT 20) s';
    EXECUTE 'EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF,SUMMARY OFF) ' || result_query
        INTO plan USING q;
    IF position('"Index Name": "docs_bm25"' IN plan::text)=0 OR position('"Node Type": "Index Scan"' IN plan::text)=0 THEN
        RAISE EXCEPTION 'not the real BM25 Index Scan for %: %',q,plan;
    END IF;
    RAISE NOTICE 'BM25 plan for %: %',q,plan;
    IF NOT jsonb_path_exists(plan::jsonb, '$.** ? (@."Node Type" == "Index Scan" && @."Index Name" == "docs_bm25" && @."Actual Loops" > 0)') THEN
        RAISE EXCEPTION 'BM25 node did not execute for %: %',q,plan;
    END IF;
    EXECUTE result_query INTO actual USING q;
    IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'query %, expected %, actual %',q,expected,actual; END IF;
END $fn$;
SELECT pg_temp.assert_smoke('全文',ARRAY[1]);
SELECT pg_temp.assert_smoke('POSTGRESQL',ARRAY[1,3]);
SELECT pg_temp.assert_smoke('qzxnevermatchcanary',ARRAY[]::integer[]);
INSERT INTO smoke.docs VALUES(4,'中文全文检索');
SELECT pg_temp.assert_smoke('全文',ARRAY[1,4]);
UPDATE smoke.docs SET body='unrelated' WHERE id=1;
SELECT pg_temp.assert_smoke('全文',ARRAY[4]);
DELETE FROM smoke.docs WHERE id=4;
SELECT pg_temp.assert_smoke('全文',ARRAY[]::integer[]);
RESET enable_seqscan;
DROP SCHEMA smoke CASCADE;
