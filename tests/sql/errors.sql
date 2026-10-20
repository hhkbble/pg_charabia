\set ON_ERROR_STOP on
CREATE SCHEMA errors;
CREATE FUNCTION pg_temp.expect_error(statement text, expected text) RETURNS void LANGUAGE plpgsql AS $f$
DECLARE actual text;
BEGIN
    BEGIN EXECUTE statement;
    EXCEPTION WHEN OTHERS THEN actual := SQLSTATE;
    END;
    IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'expected SQLSTATE %, got % for %',expected,actual,left(statement,150); END IF;
END $f$;
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.missing(TEMPLATE=charabia.normalize_v1)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.unknown(TEMPLATE=charabia.normalize_v1,Route='wrong')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.extra(TEMPLATE=charabia.normalize_v1,Route='latin',Other=1)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.duplicate(TEMPLATE=charabia.normalize_v1,Route='latin',"Route"='latin')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.object(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='{}')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.number(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='[1]')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.nested(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='[["x"]]')$s$,'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.nul(TEMPLATE=charabia.normalize_v1,Route=''latin'',StopWords=%L)', '["' || chr(92) || 'u0000"]'),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.bytes(TEMPLATE=charabia.normalize_v1,Route=''latin'',StopWords=%L)',repeat(' ',1048577)),'54000');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.count(TEMPLATE=charabia.normalize_v1,Route=''latin'',StopWords=%L)','['||repeat('"x",',65536)||'"x"]'),'54000');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.item(TEMPLATE=charabia.normalize_v1,Route=''latin'',StopWords=%L)','["'||repeat('x',262145)||'"]'),'54000');
SELECT pg_temp.expect_error($s$SELECT to_tsvector('charabia.default_v1',repeat(' ',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT ts_parse('charabia.parser_v1',repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT ts_lexize('charabia.norm_latin_v1',repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT * FROM charabia.analyze_v1(repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT to_tsquery('charabia.default_v1','cat &')$s$,'42601');
SELECT pg_temp.expect_error($s$SELECT ts_headline('charabia.default_v1','cat',plainto_tsquery('charabia.default_v1','cat'))$s$,'0A000');
DO $$ BEGIN
    IF length((ts_lexize('charabia.norm_latin_v1',repeat('A',262144)))[1])<>262144 THEN RAISE EXCEPTION 'direct lexize bound was truncated'; END IF;
    IF to_tsvector('charabia.default_v1','')<>''::tsvector THEN RAISE EXCEPTION 'empty vector'; END IF;
    IF to_tsvector('charabia.default_v1',NULL) IS NOT NULL THEN RAISE EXCEPTION 'SQL NULL semantics'; END IF;
    IF ts_lexize('charabia.norm_latin_v1',chr(769)) IS NOT NULL THEN RAISE EXCEPTION 'empty normalized Word must Ignore'; END IF;
END $$;
CREATE TEXT SEARCH DICTIONARY errors.empty_stop(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='[""]');
DO $$ BEGIN
    IF ts_lexize('errors.empty_stop',chr(1))<>ARRAY[]::text[] THEN RAISE EXCEPTION 'explicit empty stopword precedence'; END IF;
END $$;
DROP SCHEMA errors CASCADE;
