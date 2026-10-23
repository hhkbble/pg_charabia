\set ON_ERROR_STOP on
-- Owned by this suite across sessions/restart; Python removes it on every exit.
CREATE SCHEMA bm25_test;
CREATE FUNCTION bm25_test.assert_true(ok boolean, message text) RETURNS void
LANGUAGE plpgsql AS $$ BEGIN
  IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION '%', message; END IF;
END $$;
CREATE FUNCTION bm25_test.checked_query(value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$ BEGIN
  IF octet_length(value)>262144 THEN
    RAISE EXCEPTION USING ERRCODE='54000', MESSAGE='complete query exceeds 262144 bytes';
  END IF;
  RETURN value;
END $$;
CREATE TABLE bm25_test.docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));
CREATE TABLE bm25_test.no_stop_docs(LIKE bm25_test.docs INCLUDING ALL);

-- Guard the flattened indexed expression, not each individually valid part.
CREATE TABLE bm25_test.expression_docs(id integer PRIMARY KEY,title text NOT NULL,body text NOT NULL,
  CHECK(octet_length(title||' '||body)<=262144));
CREATE TABLE bm25_test.array_docs(id integer PRIMARY KEY,parts text[] NOT NULL,
  CHECK(octet_length(array_to_string(parts,' '))<=262144));
CREATE INDEX expression_bm25 ON bm25_test.expression_docs USING bm25((title||' '||body))
  WITH(text_config='public.charabia');
CREATE INDEX array_bm25 ON bm25_test.array_docs USING bm25(parts) WITH(text_config='public.charabia');
INSERT INTO bm25_test.expression_docs VALUES(1,'PostgreSQL',repeat(' ',262133));
INSERT INTO bm25_test.array_docs VALUES(1,ARRAY['PostgreSQL',repeat(' ',262133)]);
SELECT bm25_test.assert_true((SELECT octet_length(title||' '||body)=262144 FROM bm25_test.expression_docs),
  'expression accepts exactly 262144 indexed bytes');
SELECT bm25_test.assert_true((SELECT octet_length(array_to_string(parts,' '))=262144 FROM bm25_test.array_docs),
  'array accepts exactly 262144 indexed bytes');
DO $$ BEGIN
  BEGIN INSERT INTO bm25_test.docs VALUES(-1,repeat(' ',262145));
    RAISE EXCEPTION 'missing document CHECK'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO bm25_test.expression_docs VALUES(-1,repeat(' ',131072),repeat(' ',131072));
    RAISE EXCEPTION 'missing flattened expression CHECK'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO bm25_test.array_docs VALUES(-1,ARRAY[repeat(' ',131072),repeat(' ',131072)]);
    RAISE EXCEPTION 'missing flattened array CHECK'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN PERFORM to_bm25query(bm25_test.checked_query(repeat(' ',262145)),'bm25_test.expression_bm25');
    RAISE EXCEPTION 'missing complete query guard'; EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
END $$;
SELECT bm25_test.assert_true(octet_length(bm25_test.checked_query(repeat(' ',262144)))=262144,
  'complete query accepts exact byte boundary');
SELECT bm25_test.assert_true(octet_length(bm25_test.checked_query(repeat('中',87381)))=262143,
  'query guard counts UTF8 bytes');
DO $$ BEGIN
  BEGIN PERFORM bm25_test.checked_query(repeat('中',87382));
    RAISE EXCEPTION 'missing multibyte complete query guard'; EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
END $$;

-- Deliberate upstream-only bypass, not a Charabia whole-document guarantee.
CREATE TABLE bm25_test.chunk_docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO bm25_test.chunk_docs VALUES(1,'chunkleft '||repeat(' ',262144)||' chunkright');
CREATE INDEX chunk_bm25 ON bm25_test.chunk_docs USING bm25(body) WITH(text_config='pg_catalog.simple');
SELECT bm25_test.assert_true((SELECT octet_length(body)>262144 FROM bm25_test.chunk_docs),
  'upstream control deliberately exceeds supported whole-input bound');
SELECT bm25_test.assert_true((SELECT tsvector_to_array(to_tsvector('pg_catalog.simple',body))=
  ARRAY['chunkleft','chunkright'] FROM bm25_test.chunk_docs),'independent built-in whole-input lexemes');

-- PG representation limits affect BM25 term frequency inputs.
CREATE TABLE bm25_test.frequency_docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));
INSERT INTO bm25_test.frequency_docs VALUES(1,repeat('echo ',300)),
  (2,repeat('paddingword ',16384)||'tailword');
CREATE INDEX frequency_bm25 ON bm25_test.frequency_docs USING bm25(body) WITH(text_config='public.charabia');
SELECT bm25_test.assert_true((SELECT cardinality(positions)=255 FROM bm25_test.frequency_docs,
  LATERAL unnest(to_tsvector('public.charabia',body)) WHERE id=1 AND lexeme='echo'),
  'PG18 to_tsvector retains 255 positions per lexeme');
SELECT bm25_test.assert_true((SELECT positions=ARRAY[16383]::smallint[] FROM bm25_test.frequency_docs,
  LATERAL unnest(to_tsvector('public.charabia',body)) WHERE id=2 AND lexeme='tailword'),
  'PG18 clamps positions above 16383');
CREATE TABLE bm25_test.ranking_docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO bm25_test.ranking_docs VALUES(1,'rankcanary filler'),(2,'rankcanary rankcanary'),(3,'unrelated filler');
-- Python creates the native no_stem fixture and its ranking index before queries.
-- The actual adapter cannot see the caller's pre-chunked full input. Prove
-- rejection at native ingress and record bypassed BM25 behavior separately.
CREATE TABLE bm25_test.bypass_docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO bm25_test.bypass_docs SELECT * FROM bm25_test.chunk_docs;
DO $$ BEGIN
  BEGIN PERFORM to_tsvector('public.charabia',body) FROM bm25_test.bypass_docs;
    RAISE EXCEPTION 'native full input was not rejected';
  EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
END $$;
CREATE INDEX bypass_bm25 ON bm25_test.bypass_docs USING bm25(body) WITH(text_config='public.charabia');

-- Force one Dictionary language without passing Parser-private metadata. This
-- isolates the four-variant protocol from statistical language identification.
CREATE TEXT SEARCH DICTIONARY bm25_test.four_terms_dict(
  TEMPLATE=charabia.linguistic_template,Language='tur',Discard=false,Stem=true,
  Normalize=false,Filter=false,English=true);
CREATE TEXT SEARCH DICTIONARY bm25_test.native_terms_dict(
  TEMPLATE=charabia.linguistic_template,Language='tur',Discard=false,Stem=true,
  Normalize=false,Filter=false,English=false);
CREATE TEXT SEARCH CONFIGURATION bm25_test.four_terms(PARSER=charabia.parser);
CREATE TEXT SEARCH CONFIGURATION bm25_test.native_terms(PARSER=charabia.parser);
DO $fixture$ DECLARE l jsonb; BEGIN
  FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP
    EXECUTE format('ALTER TEXT SEARCH CONFIGURATION bm25_test.four_terms ADD MAPPING FOR %I WITH bm25_test.four_terms_dict',l->>'language');
    EXECUTE format('ALTER TEXT SEARCH CONFIGURATION bm25_test.native_terms ADD MAPPING FOR %I WITH bm25_test.native_terms_dict',l->>'language');
  END LOOP;
END $fixture$;
CREATE TABLE bm25_test.four_docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));
INSERT INTO bm25_test.four_docs VALUES
  (1,'ABBREVIATE'),(2,'abbrevıate'),(3,'abbrevıa'),(4,'abbreviate'),(5,'abbrevi'),(6,'unrelatedcanary');
CREATE TABLE bm25_test.four_native_docs(LIKE bm25_test.four_docs INCLUDING ALL);
INSERT INTO bm25_test.four_native_docs SELECT * FROM bm25_test.four_docs;
CREATE INDEX four_bm25 ON bm25_test.four_docs USING bm25(body)WITH(text_config='bm25_test.four_terms');
CREATE INDEX four_native_bm25 ON bm25_test.four_native_docs USING bm25(body)WITH(text_config='bm25_test.native_terms');
ANALYZE bm25_test.four_docs;
ANALYZE bm25_test.four_native_docs;
