\set ON_ERROR_STOP on
-- This schema belongs only to the BM25 suite. The Python driver keeps it across
-- independently connected sessions and an owned-cluster restart, then removes it.
CREATE SCHEMA bm25_test;
CREATE FUNCTION bm25_test.assert_true(ok boolean, message text) RETURNS void
LANGUAGE plpgsql AS $$ BEGIN
  IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION '%', message; END IF;
END $$;

-- Application ingress validates the complete query BEFORE pg_textsearch can
-- split it into 256-KiB chunks. This is a test adapter, not a new extension API.
CREATE FUNCTION bm25_test.checked_query(value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$ BEGIN
  IF octet_length(value)>262144 THEN
    RAISE EXCEPTION USING ERRCODE='54000', MESSAGE='complete query exceeds 262144 bytes';
  END IF;
  RETURN value;
END $$;

CREATE TABLE bm25_test.docs(
  id integer PRIMARY KEY,
  body text NOT NULL CHECK(octet_length(body)<=262144)
);
CREATE TABLE bm25_test.stop_docs(LIKE bm25_test.docs INCLUDING ALL);
CREATE TEXT SEARCH CONFIGURATION bm25_test.stop_config(COPY=charabia.default_v1);
-- A global stoplist must map every route to its own terminal dictionary.
DO $body$
DECLARE route record; dictionary text;
BEGIN
  FOR route IN SELECT alias FROM ts_token_type('charabia.parser_v1') LOOP
    dictionary := 'stop_'||route.alias;
    EXECUTE format('CREATE TEXT SEARCH DICTIONARY bm25_test.%I (TEMPLATE=charabia.normalize_v1, Route=%L, StopWords=%L)',
      dictionary, route.alias, '["the"]');
    EXECUTE format('ALTER TEXT SEARCH CONFIGURATION bm25_test.stop_config ALTER MAPPING FOR %I WITH bm25_test.%I',
      route.alias,dictionary);
  END LOOP;
END $body$;
SELECT bm25_test.assert_true(to_tsvector('bm25_test.stop_config','the')=''::tsvector,'stop-only query analysis');
SELECT bm25_test.assert_true(
  tsvector_to_array(to_tsvector('bm25_test.stop_config','the PostgreSQL'))=
  tsvector_to_array(to_tsvector('bm25_test.stop_config','PostgreSQL')),
  'mixed stopword/content query preserves candidate lexemes');

-- The CHECK must constrain what the expression/array index actually flattens,
-- not only each independently valid source field.
CREATE TABLE bm25_test.expression_docs(
  id integer PRIMARY KEY,title text NOT NULL,body text NOT NULL,
  CHECK(octet_length(title||' '||body)<=262144)
);
CREATE TABLE bm25_test.array_docs(
  id integer PRIMARY KEY,parts text[] NOT NULL,
  CHECK(octet_length(array_to_string(parts,' '))<=262144)
);
CREATE INDEX expression_bm25 ON bm25_test.expression_docs USING bm25((title||' '||body))
  WITH(text_config='charabia.default_v1');
CREATE INDEX array_bm25 ON bm25_test.array_docs USING bm25(parts)
  WITH(text_config='charabia.default_v1');
INSERT INTO bm25_test.expression_docs VALUES(1,'PostgreSQL',repeat(' ',262133));
INSERT INTO bm25_test.array_docs VALUES(1,ARRAY['PostgreSQL',repeat(' ',262133)]);
SELECT bm25_test.assert_true(
  (SELECT octet_length(title||' '||body)=262144 FROM bm25_test.expression_docs WHERE id=1),
  'expression bound accepts exactly 262144 bytes');
SELECT bm25_test.assert_true(
  (SELECT octet_length(array_to_string(parts,' '))=262144 FROM bm25_test.array_docs WHERE id=1),
  'array bound accepts exactly 262144 bytes');
DO $$ BEGIN
  BEGIN
    INSERT INTO bm25_test.docs VALUES(-1,repeat(' ',262145));
    RAISE EXCEPTION 'missing document CHECK';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO bm25_test.expression_docs VALUES(-1,repeat(' ',131072),repeat(' ',131072));
    RAISE EXCEPTION 'missing flattened expression CHECK';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO bm25_test.array_docs VALUES(-1,ARRAY[repeat(' ',131072),repeat(' ',131072)]);
    RAISE EXCEPTION 'missing flattened array CHECK';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    PERFORM to_bm25query(bm25_test.checked_query(repeat(' ',262145)),'bm25_test.expression_bm25');
    RAISE EXCEPTION 'missing complete query guard';
  EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
END $$;
SELECT bm25_test.assert_true(octet_length(bm25_test.checked_query(repeat(' ',262144)))=262144,
  'complete query guard accepts exact byte boundary');
SELECT bm25_test.assert_true(octet_length(bm25_test.checked_query(repeat('中',87381)))=262143,
  'query guard counts UTF-8 bytes');
DO $$ BEGIN
  BEGIN
    PERFORM bm25_test.checked_query(repeat('中',87382));
    RAISE EXCEPTION 'missing multibyte complete query guard';
  EXCEPTION WHEN program_limit_exceeded THEN NULL; END;
END $$;

-- Deliberately bypass the app guard only for this built-in-simple control. It
-- demonstrates the upstream whole-input chunking boundary without promising
-- that Charabia can detect the length before pg_textsearch splits it.
CREATE TABLE bm25_test.chunk_docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO bm25_test.chunk_docs VALUES
  (1,'chunkleft '||repeat(' ',262144)||' chunkright');
CREATE INDEX chunk_bm25 ON bm25_test.chunk_docs USING bm25(body) WITH(text_config='pg_catalog.simple');
SELECT bm25_test.assert_true((SELECT octet_length(body)>262144 FROM bm25_test.chunk_docs),
  'upstream-only chunk control is outside supported whole-input bound');
SELECT bm25_test.assert_true((SELECT tsvector_to_array(to_tsvector('pg_catalog.simple',body))=
  ARRAY['chunkleft','chunkright'] FROM bm25_test.chunk_docs),'built-in whole-input control lexemes');

-- Native position saturation is part of the BM25 frequency input. These are
-- PostgreSQL representation assertions, not expected raw Charabia frequencies.
CREATE TABLE bm25_test.frequency_docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));
INSERT INTO bm25_test.frequency_docs VALUES(1,repeat('echo ',300)),
  (2,repeat('paddingword ',16384)||'tailword');
CREATE INDEX frequency_bm25 ON bm25_test.frequency_docs USING bm25(body) WITH(text_config='charabia.default_v1');
SELECT id,lexeme,cardinality(positions) AS recorded_frequency,
  positions[1] AS first_position,positions[cardinality(positions)] AS last_position
FROM bm25_test.frequency_docs,LATERAL unnest(to_tsvector('charabia.default_v1',body))
ORDER BY id,lexeme;
SELECT bm25_test.assert_true(
  (SELECT cardinality(positions)=255 FROM bm25_test.frequency_docs,
    LATERAL unnest(to_tsvector('charabia.default_v1',body)) WHERE id=1 AND lexeme='echo'),
  -- https://github.com/postgres/postgres/blob/REL_18_6/src/backend/tsearch/to_tsany.c#L138
  -- PG18 keeps apos[0] < MAXNUMPOS-1 (256-1). This
  -- construction path retains 255, even though binary/text tsvector inputs
  -- can represent 256 positions. Pin the real to_tsvector path used by BM25.
  'PG18 to_tsvector caps collected positions per lexeme at 255');
SELECT bm25_test.assert_true(
  (SELECT positions=ARRAY[16383]::smallint[] FROM bm25_test.frequency_docs,
    LATERAL unnest(to_tsvector('charabia.default_v1',body)) WHERE id=2 AND lexeme='tailword'),
  'PG clamps positions above 16383');
SELECT id,to_tsvector('charabia.default_v1',body) FROM bm25_test.frequency_docs;

-- Equal document lengths isolate a useful ranking requirement: greater term
-- frequency ranks ahead. No floating-point score or tied ordering is fixed.
CREATE TABLE bm25_test.ranking_docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO bm25_test.ranking_docs VALUES(1,'rankcanary filler'),
  (2,'rankcanary rankcanary'),(3,'unrelated filler');
CREATE INDEX ranking_bm25 ON bm25_test.ranking_docs USING bm25(body) WITH(text_config='charabia.default_v1');
