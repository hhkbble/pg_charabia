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
-- INIT uses native DefElem options; old names are rejected rather than aliased.
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.missing(TEMPLATE=charabia.linguistic_template)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.retired(TEMPLATE=charabia.linguistic_template,Route='eng')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.unknown(TEMPLATE=charabia.linguistic_template,Language='wrong')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.extra(TEMPLATE=charabia.linguistic_template,Language='eng',Other=1)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.duplicate(TEMPLATE=charabia.linguistic_template,Language='eng',"Language"='eng')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.boolean(TEMPLATE=charabia.linguistic_template,Language='eng',Stem='maybe')$s$,'42601');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.capability(TEMPLATE=charabia.linguistic_template,Language='und',Discard=true)$s$,'0A000');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.conflict(TEMPLATE=charabia.linguistic_template,Language='eng',Filter=true)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.retired_stop(TEMPLATE=charabia.linguistic_template,Language='eng',AddStopWords='["x"]')$s$,'22023');
SELECT pg_temp.expect_error($s$SELECT to_tsvector('public.charabia',repeat(' ',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT ts_parse('charabia.parser',repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT ts_lexize('charabia.und',repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT * FROM charabia.analyze(repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT charabia.query_any(repeat('a',262145))$s$,'54000');
CREATE TABLE errors.toasted(body text);
INSERT INTO errors.toasted VALUES(repeat('a',262144)),(repeat('a',262145));
DO $boundary$
DECLARE covered bigint;
BEGIN
    SELECT sum(byte_end-byte_start) INTO covered FROM charabia.analyze(
        (SELECT body FROM errors.toasted WHERE octet_length(body)=262144));
    IF covered IS DISTINCT FROM 262144 THEN RAISE EXCEPTION 'toasted text boundary: %',covered; END IF;
END
$boundary$;
SELECT pg_temp.expect_error($s$SELECT * FROM charabia.analyze((SELECT body FROM errors.toasted WHERE octet_length(body)=262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT charabia.query_any((SELECT body FROM errors.toasted WHERE octet_length(body)=262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT to_tsquery('public.charabia','cat &')$s$,'42601');
SELECT pg_temp.expect_error($s$SELECT ts_headline('public.charabia','cat','cat'::tsquery,'Unknown=true')$s$,'22023');
SELECT pg_temp.expect_error($s$SELECT ts_headline('public.charabia','cat','cat:A'::tsquery)$s$,'0A000');
CREATE TEXT SEARCH DICTIONARY errors.filter_expansion(TEMPLATE=charabia.linguistic_template,Language='ara',Discard=false,Stem=false,Filter=true);
-- NFKD expands this three-byte scalar to 33 bytes in the fixed pipeline.
SELECT pg_temp.expect_error($s$SELECT ts_lexize('errors.filter_expansion',repeat('ﷺ',10000))$s$,'54000');
DO $$ BEGIN
    IF length((ts_lexize('charabia.und',repeat('A',262144)))[1])<>262144 THEN RAISE EXCEPTION 'direct lexize bound was truncated'; END IF;
    IF to_tsvector('public.charabia','')<>''::tsvector THEN RAISE EXCEPTION 'empty vector'; END IF;
    IF to_tsvector('public.charabia',NULL) IS NOT NULL THEN RAISE EXCEPTION 'SQL NULL semantics'; END IF;
    -- Charabia's nonspacing-mark removal includes Latin, not Script::Other.
    -- Use the explicit English dictionary to exercise an empty final lexeme.
    IF ts_lexize('charabia.eng',chr(769)) IS NOT NULL THEN RAISE EXCEPTION 'empty normalized Word must Ignore'; END IF;
    IF ts_lexize('charabia.und',chr(769)) IS DISTINCT FROM ARRAY[chr(769)] THEN RAISE EXCEPTION 'Other-script mark was incorrectly folded'; END IF;
    IF ts_lexize('charabia.und',chr(1)) IS NOT NULL THEN RAISE EXCEPTION 'empty prepared Word must Ignore'; END IF;
END $$;
DROP SCHEMA errors CASCADE;
