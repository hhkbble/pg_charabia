CREATE SCHEMA charabia;

CREATE FUNCTION charabia.parser_start_v1(internal, integer) RETURNS internal
AS 'MODULE_PATHNAME', 'parser_start_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_gettoken_v1(internal, internal, internal) RETURNS internal
AS 'MODULE_PATHNAME', 'parser_gettoken_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_end_v1(internal) RETURNS void
AS 'MODULE_PATHNAME', 'parser_end_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_lextypes_v1(internal) RETURNS internal
AS 'MODULE_PATHNAME', 'parser_lextypes_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.dictionary_init_v1(internal) RETURNS internal
AS 'MODULE_PATHNAME', 'dictionary_init_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.dictionary_lexize_v1(internal, internal, internal, internal) RETURNS internal
AS 'MODULE_PATHNAME', 'dictionary_lexize_v1_wrapper' LANGUAGE c STRICT;

CREATE TEXT SEARCH PARSER charabia.parser_v1 (
    START = charabia.parser_start_v1, GETTOKEN = charabia.parser_gettoken_v1,
    END = charabia.parser_end_v1, LEXTYPES = charabia.parser_lextypes_v1
);
CREATE TEXT SEARCH TEMPLATE charabia.normalize_v1 (
    INIT = charabia.dictionary_init_v1, LEXIZE = charabia.dictionary_lexize_v1
);
CREATE TEXT SEARCH CONFIGURATION charabia.default_v1 (PARSER = charabia.parser_v1);

DO $install$
DECLARE alias text;
BEGIN
    FOREACH alias IN ARRAY ARRAY[
        'basic','case','latin','latin_swe','greek','marks','arabic','persian',
        'cj_chinese','cj_japanese','cj_unknown','cj_other','basic_swe','case_swe',
        'greek_swe','marks_swe','arabic_swe','cj_other_swe'
    ] LOOP
        EXECUTE format('CREATE TEXT SEARCH DICTIONARY charabia.%I (TEMPLATE=charabia.normalize_v1, Route=%L)', 'norm_' || alias || '_v1', alias);
        EXECUTE format('ALTER TEXT SEARCH CONFIGURATION charabia.default_v1 ADD MAPPING FOR %I WITH charabia.%I', alias, 'norm_' || alias || '_v1');
    END LOOP;
END
$install$;

CREATE FUNCTION charabia.analyze_v1(text)
RETURNS TABLE(raw text, lemma text, kind text, script text, language text,
    byte_start bigint, byte_end bigint, char_start bigint, char_end bigint, route text)
AS 'MODULE_PATHNAME', 'analyze_v1_wrapper' LANGUAGE c STRICT STABLE PARALLEL RESTRICTED;
CREATE FUNCTION charabia.analysis_info_v1() RETURNS jsonb
AS 'MODULE_PATHNAME', 'analysis_info_v1_wrapper' LANGUAGE c STABLE PARALLEL RESTRICTED;
