\set ON_ERROR_STOP on
CREATE SCHEMA smoke;
CREATE FUNCTION pg_temp.assert_true(ok boolean,message text) RETURNS void LANGUAGE plpgsql AS $$BEGIN IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION '%',message; END IF; END$$;
SELECT pg_temp.assert_true((SELECT count(*)=73 FROM ts_token_type('charabia.parser')),'73 finite types');
SELECT pg_temp.assert_true((SELECT count(*)=71 FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig),'71 dictionary mappings');
SELECT pg_temp.assert_true((SELECT string_agg(token,'')='中文全文检索 PostgreSQL 18 支持 BM25' FROM ts_parse('charabia.parser','中文全文检索 PostgreSQL 18 支持 BM25')),'raw byte fidelity');
SELECT * FROM ts_debug('public.charabia','中文全文检索 PostgreSQL 18 支持 BM25');
SELECT charabia.analysis_info();
SELECT pg_temp.assert_true(ts_lexize('charabia.eng',' ' ) IS NULL,'separator Ignore');
SELECT pg_temp.assert_true(ts_lexize('charabia.eng','The')=ARRAY[]::text[],'stop before stem with canonical case');
SELECT pg_temp.assert_true(ts_lexize('charabia.eng','running')=ARRAY['running','run'],'same-position surface and stem');
SELECT pg_temp.assert_true(ts_headline('public.charabia','<x> CAFÉ & cafe','cafe'::tsquery,'HighlightAll=true')='&lt;x&gt; <b>CAFÉ</b> &amp; <b>cafe</b>','standard original headline and escape');
CREATE TEXT SEARCH CONFIGURATION smoke.search(COPY=public.charabia);
CREATE TEXT SEARCH DICTIONARY smoke.eng(TEMPLATE=charabia.template,Language='eng',Discard=true,Stem=true,Normalize=true,Filter=false);
ALTER TEXT SEARCH CONFIGURATION smoke.search ALTER MAPPING FOR eng WITH smoke.eng;
CREATE TABLE smoke.docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));
INSERT INTO smoke.docs VALUES
(1,'The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running.'),
(2,'中文全文检索 PostgreSQL 18 支持 BM25'),(3,'unrelated banana');
INSERT INTO smoke.docs SELECT g,'cobaltxylophone' FROM generate_series(4,100)g;
CREATE INDEX docs_bm25 ON smoke.docs USING bm25(body)WITH(text_config='smoke.search');
CREATE INDEX docs_gin ON smoke.docs USING gin(to_tsvector('smoke.search',body));
ANALYZE smoke.docs;
CREATE FUNCTION pg_temp.assert_smoke(q text,expected integer[]) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE plan jsonb;native_plan jsonb;actual integer[];native_actual integer[];statement text;native_statement text;
BEGIN
statement:='SELECT id FROM smoke.docs ORDER BY body <@> to_bm25query($1,''smoke.docs_bm25'') LIMIT 100';
EXECUTE 'EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) '||statement INTO plan USING q;
RAISE NOTICE 'BM25 query=% plan=%',q,plan;
IF NOT jsonb_path_exists(plan,'$.** ? (@."Node Type" == "Index Scan" && @."Index Name" == "docs_bm25" && @."Actual Loops" > 0)') THEN RAISE EXCEPTION 'BM25 Index Scan did not execute: %',plan;END IF;
EXECUTE 'SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::integer[]) FROM ('||statement||')q' INTO actual USING q;
IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'query %, expected %, actual %',q,expected,actual;END IF;
-- A real bitmap index execution is required even for an empty candidate set.
PERFORM set_config('enable_seqscan','off',true);
PERFORM set_config('enable_indexscan','off',true);
PERFORM set_config('enable_indexonlyscan','off',true);
PERFORM set_config('enable_bitmapscan','on',true);
native_statement:='SELECT id FROM smoke.docs WHERE to_tsvector(''smoke.search'',body) @@ charabia.query_any($1,''smoke.search'')';
EXECUTE 'EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) '||native_statement INTO native_plan USING q;
RAISE NOTICE 'GIN query=% plan=%',q,native_plan;
IF NOT jsonb_path_exists(native_plan,'$.** ? (@."Node Type" == "Bitmap Index Scan" && @."Index Name" == "docs_gin" && @."Actual Loops" > 0)') THEN RAISE EXCEPTION 'GIN Index Scan did not execute: %',native_plan;END IF;
EXECUTE 'SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::integer[]) FROM ('||native_statement||')q' INTO native_actual USING q;
IF native_actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'GIN query %, expected %, actual %',q,expected,native_actual;END IF;
END $fn$;
SELECT pg_temp.assert_smoke('全文',ARRAY[2]);
SELECT pg_temp.assert_smoke('running',ARRAY[1]);
SELECT pg_temp.assert_smoke('qzxnevermatchcanary',ARRAY[]::integer[]);
SET enable_seqscan=off;
SELECT pg_temp.assert_smoke('running',ARRAY[1]);
SELECT pg_temp.assert_true((SELECT count(*)=1 FROM smoke.docs WHERE to_tsvector('smoke.search',body) @@ plainto_tsquery('smoke.search','running')),'native GIN query candidates');
EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF)SELECT id FROM smoke.docs WHERE to_tsvector('smoke.search',body) @@ plainto_tsquery('smoke.search','running');
RESET enable_seqscan;
SELECT pg_temp.assert_true((SELECT a.positions=b.positions FROM smoke.docs d,LATERAL unnest(to_tsvector('smoke.search',d.body))a,LATERAL unnest(to_tsvector('smoke.search',d.body))b WHERE d.id=1 AND a.lexeme='run' AND b.lexeme='running'),'surface and stem share original position');
SELECT pg_temp.assert_true((SELECT position('<b>running</b>'IN ts_headline('smoke.search',body,charabia.query_any('running','smoke.search'),'HighlightAll=true'))>0 FROM smoke.docs WHERE id=1),'BM25 candidate highlighted with same configuration');
INSERT INTO smoke.docs VALUES(200,'中文全文检索 running');
SELECT pg_temp.assert_smoke('全文',ARRAY[2,200]);
SELECT pg_temp.assert_smoke('running',ARRAY[1,200]);
UPDATE smoke.docs SET body='unrelated'WHERE id=2;
SELECT pg_temp.assert_smoke('全文',ARRAY[200]);
UPDATE smoke.docs SET body='banana'WHERE id=1;
SELECT pg_temp.assert_smoke('running',ARRAY[200]);
DELETE FROM smoke.docs WHERE id=200;
SELECT pg_temp.assert_smoke('全文',ARRAY[]::integer[]);
SELECT pg_temp.assert_smoke('running',ARRAY[]::integer[]);
DROP SCHEMA smoke CASCADE;
