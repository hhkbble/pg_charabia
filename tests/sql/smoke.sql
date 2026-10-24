\set ON_ERROR_STOP on
CREATE SCHEMA smoke;
CREATE FUNCTION pg_temp.assert_true(ok boolean,message text) RETURNS void LANGUAGE plpgsql AS $$BEGIN IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION '%',message; END IF; END$$;
SELECT pg_temp.assert_true((SELECT count(*)=18 FROM ts_token_type('charabia.linguistic_parser')),'18 finite linguistic types');
SELECT pg_temp.assert_true((SELECT count(*)=16 FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig),'16 linguistic dictionary mappings');
SELECT pg_temp.assert_true((SELECT string_agg(token,'')='中文全文检索 PostgreSQL 18 支持 BM25' FROM ts_parse('charabia.linguistic_parser','中文全文检索 PostgreSQL 18 支持 BM25')),'raw byte fidelity');
SELECT pg_temp.assert_true((SELECT count(*)=23 FROM ts_token_type('charabia.structured_parser')),'23 finite structured types');
SELECT pg_temp.assert_true(plainto_tsquery('public.charabia','100ms')='100ms'::tsquery,'implicit linguistic default keeps the ordinary unit');
SELECT pg_temp.assert_true(NOT(to_tsvector('public.charabia','100ms') @@ 'ms'::tsquery),'linguistic default has no structured components');
SELECT pg_temp.assert_true(to_tsvector('public.charabia_structured','100ms') @@ 'ms'::tsquery,'explicit structured mode expands components');
SELECT * FROM ts_debug('public.charabia','中文全文检索 PostgreSQL 18 支持 BM25');
SELECT charabia.analysis_info();
SELECT pg_temp.assert_true(ts_lexize('charabia.eng',' ' ) IS NULL,'separator Ignore');
SELECT pg_temp.assert_true(ts_lexize('charabia.eng','The')=ARRAY[]::text[],'stop before stem with canonical case');
SELECT pg_temp.assert_true(ts_lexize('charabia.eng','running')=ARRAY['running','run'],'same-position surface and stem');
SELECT pg_temp.assert_true(ts_headline('public.charabia','<x> CAFÉ & cafe','cafe'::tsquery,'HighlightAll=true')='&lt;x&gt; <b>CAFÉ</b> &amp; <b>cafe</b>','standard original headline and escape');
-- Four independent variants are same-position alternatives, not an AND group.
CREATE TEXT SEARCH DICTIONARY smoke.tur4(TEMPLATE=charabia.linguistic_template,Language='tur',English=true,Discard=false,Stem=true,Normalize=false,Filter=false);
CREATE TEXT SEARCH CONFIGURATION smoke.four(PARSER=charabia.linguistic_parser);
DO $map$ DECLARE types text; BEGIN
 SELECT string_agg(quote_ident(l->>'language'),',') INTO types FROM jsonb_array_elements(charabia.analysis_info()->'languages')l;
 EXECUTE 'ALTER TEXT SEARCH CONFIGURATION smoke.four ADD MAPPING FOR '||types||' WITH smoke.tur4';
END $map$;
SELECT pg_temp.assert_true(ts_lexize('smoke.tur4','ABBREVIATE')=ARRAY['abbrevıate','abbrevıa','abbreviate','abbrevi'],'four independently prepared native and English terms');
SELECT pg_temp.assert_true((SELECT bool_and(to_tsvector('smoke.four','ABBREVIATE') @@ to_tsquery('simple',quote_literal(term))) FROM unnest(ARRAY['abbrevıate','abbrevıa','abbreviate','abbrevi'])term),'each variant alone matches the four-term vector');
SELECT pg_temp.assert_true((SELECT bool_and(positions=ARRAY[1]::smallint[]) FROM unnest(to_tsvector('smoke.four','ABBREVIATE'))),'all four terms have one original position');
SELECT pg_temp.assert_true(ts_headline('smoke.four','ABBREVIATE','abbrevi'::tsquery,'HighlightAll=true')='<b>ABBREVIATE</b>','English branch highlights original Turkish-routed source');
SELECT pg_temp.assert_true((SELECT count(*)=1 AND min(token)='PostgreSQL' FROM ts_parse('charabia.linguistic_parser','PostgreSQL')),'whole-word recovery preserves one original ASCII word');
SELECT pg_temp.assert_true((SELECT string_agg(token,'')='post greSQL' FROM ts_parse('charabia.linguistic_parser','post greSQL')),'word recovery cannot remove whitespace');
SELECT pg_temp.assert_true((SELECT string_agg(raw,'')='Ελληνικά বাংলা 123' FROM charabia.analyze('Ελληνικά বাংলা 123')),'valid outside and unknown text stays raw without an internal range error');
CREATE TEXT SEARCH CONFIGURATION smoke.search(COPY=public.charabia_structured);
CREATE TEXT SEARCH DICTIONARY smoke.eng(TEMPLATE=charabia.linguistic_template,Language='eng',Discard=true,Stem=true,Normalize=true,Filter=false);
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
-- Actual Rust structured dictionaries run through the same GIN/BM25 indexes.
INSERT INTO smoke.docs VALUES
 (50001,'alpha https://uraniumtoken.test/Running?v=100ms omega'),
 (50002,'alpha User+tag@argonmail.test omega'),
 (50003,'alpha /cobaltleaf/UniqueFilename.rs omega'),
 (50004,'alpha 100ms omega'),
 (50005,'alpha xenononly.test omega');
SELECT pg_temp.assert_smoke('100ms',ARRAY[50001,50004]);
SELECT pg_temp.assert_smoke('xenononly.test',ARRAY[50005]);
SELECT pg_temp.assert_smoke('User+tag@argonmail.test',ARRAY[50002]);
SELECT pg_temp.assert_smoke('/cobaltleaf/UniqueFilename.rs',ARRAY[50003]);
SELECT pg_temp.assert_smoke('https://uraniumtoken.test/Running?v=100ms',ARRAY[50001,50004]);
SELECT pg_temp.assert_true(ts_headline('smoke.search','alpha 100ms omega',
 plainto_tsquery('smoke.search','ms'),'HighlightAll=true')='alpha <b>100ms</b> omega',
 'component matches whole original structured');
UPDATE smoke.docs SET body='alpha 200kg omega' WHERE id=50004;
SELECT pg_temp.assert_smoke('100ms',ARRAY[50001]);
DELETE FROM smoke.docs WHERE id=50001;
SELECT pg_temp.assert_smoke('100ms',ARRAY[]::integer[]);
-- Separate native full-bundle matching from BM25 bag-of-terms candidates.
-- The four fixed rows and post-CRUD IDs are independent domain expectations.
CREATE TABLE smoke.mode_ling(id integer PRIMARY KEY,body text NOT NULL);
CREATE TABLE smoke.mode_structured(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO smoke.mode_ling VALUES(1,'alpha 100ms beta'),(2,'alpha ms beta'),
 (3,'alpha 100 beta'),(4,'alpha cats beta');
INSERT INTO smoke.mode_structured SELECT * FROM smoke.mode_ling;
CREATE INDEX mode_ling_gin ON smoke.mode_ling USING gin(to_tsvector('public.charabia',body));
CREATE INDEX mode_structured_gin ON smoke.mode_structured USING gin(to_tsvector('public.charabia_structured',body));
CREATE INDEX mode_ling_bm25 ON smoke.mode_ling USING bm25(body)WITH(text_config='public.charabia');
CREATE INDEX mode_structured_bm25 ON smoke.mode_structured USING bm25(body)WITH(text_config='public.charabia_structured');
ANALYZE smoke.mode_ling;
ANALYZE smoke.mode_structured;
CREATE FUNCTION pg_temp.assert_mode_indexes(stage text,table_name text,config text,gin_index text,
 bm25_index text,native_expected integer[],bm25_expected integer[]) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE native_statement text;bm25_statement text;plan jsonb;actual integer[];
BEGIN
 native_statement:=format('SELECT id FROM %s WHERE to_tsvector(%L,body) @@ plainto_tsquery(%L,''100ms'')',table_name,config,config);
 PERFORM set_config('enable_seqscan','off',true);
 PERFORM set_config('enable_indexscan','off',true);
 PERFORM set_config('enable_indexonlyscan','off',true);
 PERFORM set_config('enable_bitmapscan','on',true);
 EXECUTE 'EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) '||native_statement INTO plan;
 RAISE NOTICE 'dual-mode stage=% config=% GIN plan=%',stage,config,plan;
 IF NOT jsonb_path_exists(plan,'$.** ? (@."Node Type" == "Bitmap Index Scan" && @."Index Name" == $idx && @."Actual Loops" > 0)',jsonb_build_object('idx',gin_index)) THEN
  RAISE EXCEPTION 'dual-mode % % target GIN BitmapIndexScan did not execute: %',stage,config,plan;
 END IF;
 EXECUTE 'SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::integer[]) FROM ('||native_statement||')found' INTO actual;
 IF actual IS DISTINCT FROM native_expected THEN
  RAISE EXCEPTION 'dual-mode % % native expected %, actual %',stage,config,native_expected,actual;
 END IF;
 PERFORM set_config('enable_indexscan','on',true);
 PERFORM set_config('enable_indexonlyscan','on',true);
 bm25_statement:=format('SELECT id FROM %s ORDER BY body <@> to_bm25query(''100ms'',%L) LIMIT 100',table_name,'smoke.'||bm25_index);
 EXECUTE 'EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) '||bm25_statement INTO plan;
 RAISE NOTICE 'dual-mode stage=% config=% BM25 plan=%',stage,config,plan;
 IF NOT jsonb_path_exists(plan,'$.** ? (@."Node Type" == "Index Scan" && @."Index Name" == $idx && @."Actual Loops" > 0)',jsonb_build_object('idx',bm25_index)) THEN
  RAISE EXCEPTION 'dual-mode % % target BM25 IndexScan did not execute: %',stage,config,plan;
 END IF;
 EXECUTE 'SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::integer[]) FROM ('||bm25_statement||')found' INTO actual;
 IF actual IS DISTINCT FROM bm25_expected THEN
  RAISE EXCEPTION 'dual-mode % % BM25 expected %, actual %',stage,config,bm25_expected,actual;
 END IF;
 PERFORM set_config('enable_seqscan','on',true);
END $fn$;
SELECT pg_temp.assert_mode_indexes('initial','smoke.mode_ling','public.charabia','mode_ling_gin','mode_ling_bm25',ARRAY[1],ARRAY[1]);
SELECT pg_temp.assert_mode_indexes('initial','smoke.mode_structured','public.charabia_structured','mode_structured_gin','mode_structured_bm25',ARRAY[1],ARRAY[1,2,3]);
INSERT INTO smoke.mode_ling VALUES(5,'alpha 100ms beta');
INSERT INTO smoke.mode_structured VALUES(5,'alpha 100ms beta');
UPDATE smoke.mode_ling SET body='alpha cats beta' WHERE id=1;
UPDATE smoke.mode_structured SET body='alpha cats beta' WHERE id=1;
DELETE FROM smoke.mode_ling WHERE id=2;
DELETE FROM smoke.mode_structured WHERE id=2;
SELECT pg_temp.assert_mode_indexes('mutated','smoke.mode_ling','public.charabia','mode_ling_gin','mode_ling_bm25',ARRAY[5],ARRAY[5]);
SELECT pg_temp.assert_mode_indexes('mutated','smoke.mode_structured','public.charabia_structured','mode_structured_gin','mode_structured_bm25',ARRAY[5],ARRAY[3,5]);
DROP SCHEMA smoke CASCADE;
