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
-- Single INIT validation is independent of the managed configuration snapshot.
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.missing(TEMPLATE=charabia.template)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.fingerprint(TEMPLATE=charabia.template,Route='eng')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.unknown(TEMPLATE=charabia.template,Route='wrong')$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.extra(TEMPLATE=charabia.template,Route='eng',Other=1)$s$,'22023');
SELECT pg_temp.expect_error($s$CREATE TEXT SEARCH DICTIONARY errors.duplicate(TEMPLATE=charabia.template,Route='eng',"Route"='eng')$s$,'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.boolean(TEMPLATE=charabia.template,Route=''eng'',Stemming=''maybe'',SettingsFingerprint=%L)',repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.object(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=''{}'',SettingsFingerprint=%L)',repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.number(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=''[1]'',SettingsFingerprint=%L)',repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.nested(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=''[["x"]]'',SettingsFingerprint=%L)',repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.nul(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=%L,SettingsFingerprint=%L)','["'||chr(92)||'u0000"]',repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.bytes(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=%L,SettingsFingerprint=%L)',repeat(' ',1048577),repeat('0',64)),'54000');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.count(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=%L,SettingsFingerprint=%L)','['||repeat('"x",',65536)||'"x"]',repeat('0',64)),'54000');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.combined_count(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=%L,KeepStopWords=%L,SettingsFingerprint=%L)','['||repeat('"x",',32768)||'"x"]','['||repeat('"y",',32768)||'"y"]',repeat('0',64)),'54000');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.item(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=%L,SettingsFingerprint=%L)','["'||repeat('x',2047)||'"]',repeat('0',64)),'54000');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.empty(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=''[""]'',SettingsFingerprint=%L)',repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.empty_key(TEMPLATE=charabia.template,Route=''eng'',AddStopWords=%L,SettingsFingerprint=%L)',json_build_array(chr(1))::text,repeat('0',64)),'22023');
SELECT pg_temp.expect_error(format('CREATE TEXT SEARCH DICTIONARY errors.words(TEMPLATE=charabia.template,Route=''eng'',KeepStopWords=''["two words"]'',SettingsFingerprint=%L)',repeat('0',64)),'22023');
SELECT pg_temp.expect_error($s$SELECT to_tsvector('public.charabia',repeat(' ',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT ts_parse('charabia.parser',repeat('a',262145))$s$,'54000');
SELECT pg_temp.expect_error($s$SELECT ts_lexize('charabia.norm_und',repeat('a',262145))$s$,'54000');
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
DO $exact$
BEGIN
    EXECUTE format('CREATE TEXT SEARCH DICTIONARY errors.exact_json(TEMPLATE=charabia.template,Route=''und'',BuiltinStopWords=false,AddStopWords=%L,SettingsFingerprint=%L)','["a"]'||repeat(' ',1048576-5),repeat('0',64));
    EXECUTE format('CREATE TEXT SEARCH DICTIONARY errors.exact_item(TEMPLATE=charabia.template,Route=''und'',BuiltinStopWords=false,AddStopWords=%L,SettingsFingerprint=%L)','["'||repeat('x',2046)||'"]',repeat('0',64));
    IF ts_lexize('errors.exact_json','a') IS DISTINCT FROM ARRAY[]::text[] THEN RAISE EXCEPTION 'exact JSON budget rejected or truncated'; END IF;
    IF ts_lexize('errors.exact_item',repeat('x',2046)) IS DISTINCT FROM ARRAY[]::text[] THEN RAISE EXCEPTION 'exact custom item budget rejected or truncated'; END IF;
END
$exact$;
DO $$ BEGIN
    IF length((ts_lexize('charabia.norm_und',repeat('A',262144)))[1])<>262144 THEN RAISE EXCEPTION 'direct lexize bound was truncated'; END IF;
    IF to_tsvector('public.charabia','')<>''::tsvector THEN RAISE EXCEPTION 'empty vector'; END IF;
    IF to_tsvector('public.charabia',NULL) IS NOT NULL THEN RAISE EXCEPTION 'SQL NULL semantics'; END IF;
    -- Charabia's nonspacing-mark removal includes Latin, not Script::Other.
    -- Use the explicit English dictionary to exercise an empty final lexeme.
    IF ts_lexize('charabia.norm_eng',chr(769)) IS NOT NULL THEN RAISE EXCEPTION 'empty normalized Word must Ignore'; END IF;
    IF ts_lexize('charabia.norm_und',chr(769)) IS DISTINCT FROM ARRAY[chr(769)] THEN RAISE EXCEPTION 'Other-script mark was incorrectly folded'; END IF;
    IF ts_lexize('charabia.norm_und',chr(1)) IS NOT NULL THEN RAISE EXCEPTION 'empty prepared Word must Ignore'; END IF;
END $$;
DROP SCHEMA errors CASCADE;
