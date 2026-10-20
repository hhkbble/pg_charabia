GRANT USAGE ON SCHEMA charabia TO PUBLIC;
REVOKE CREATE ON SCHEMA charabia FROM PUBLIC;

CREATE FUNCTION charabia.parser_start(internal,integer) RETURNS internal
AS 'MODULE_PATHNAME','parser_start_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_gettoken(internal,internal,internal) RETURNS internal
AS 'MODULE_PATHNAME','parser_gettoken_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_end(internal) RETURNS void
AS 'MODULE_PATHNAME','parser_end_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_lextypes(internal) RETURNS internal
AS 'MODULE_PATHNAME','parser_lextypes_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.parser_headline(internal,internal,tsquery) RETURNS internal
AS 'MODULE_PATHNAME','parser_headline_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.dictionary_init(internal) RETURNS internal
AS 'MODULE_PATHNAME','dictionary_init_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.dictionary_lexize(internal,internal,internal,internal) RETURNS internal
AS 'MODULE_PATHNAME','dictionary_lexize_wrapper' LANGUAGE c STRICT;
REVOKE ALL ON FUNCTION charabia.parser_start(internal,integer),charabia.parser_gettoken(internal,internal,internal),charabia.parser_end(internal),charabia.parser_lextypes(internal),charabia.parser_headline(internal,internal,tsquery),charabia.dictionary_init(internal),charabia.dictionary_lexize(internal,internal,internal,internal) FROM PUBLIC;

CREATE TEXT SEARCH PARSER charabia.parser(START=charabia.parser_start,GETTOKEN=charabia.parser_gettoken,END=charabia.parser_end,LEXTYPES=charabia.parser_lextypes,HEADLINE=charabia.parser_headline);
CREATE TEXT SEARCH TEMPLATE charabia.template(INIT=charabia.dictionary_init,LEXIZE=charabia.dictionary_lexize);

CREATE TEXT SEARCH CONFIGURATION public.charabia(PARSER=charabia.parser);
DO $install$
DECLARE t record;
        fp text := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to('{"builtin_stopwords":true,"stemming":true,"stopwords_add":{},"stopwords_keep":{}}','UTF8')),'hex');
BEGIN
    FOR t IN SELECT alias FROM pg_catalog.ts_token_type('charabia.parser') WHERE alias<>'display_only' LOOP
        EXECUTE pg_catalog.format('CREATE TEXT SEARCH DICTIONARY charabia.%I (TEMPLATE=charabia.template,Route=%L,SettingsFingerprint=%L)','norm_'||t.alias,t.alias,fp);
        EXECUTE pg_catalog.format('ALTER TEXT SEARCH CONFIGURATION public.charabia ADD MAPPING FOR %I WITH charabia.%I',t.alias,'norm_'||t.alias);
    END LOOP;
END
$install$;

CREATE FUNCTION charabia.create_config(name text,options jsonb DEFAULT '{}') RETURNS regconfig
AS 'MODULE_PATHNAME','create_config_wrapper' LANGUAGE c STRICT VOLATILE SECURITY INVOKER PARALLEL UNSAFE SET search_path=pg_catalog;
CREATE FUNCTION charabia.alter_config(config regconfig,options jsonb) RETURNS jsonb
AS 'MODULE_PATHNAME','alter_config_wrapper' LANGUAGE c STRICT VOLATILE SECURITY INVOKER PARALLEL UNSAFE SET search_path=pg_catalog;
CREATE FUNCTION charabia.drop_config(config regconfig) RETURNS void
AS 'MODULE_PATHNAME','drop_config_wrapper' LANGUAGE c STRICT VOLATILE SECURITY INVOKER PARALLEL UNSAFE SET search_path=pg_catalog;
CREATE FUNCTION charabia.analysis_info(config regconfig DEFAULT 'public.charabia') RETURNS jsonb
AS 'MODULE_PATHNAME','analysis_info_wrapper' LANGUAGE c STRICT STABLE PARALLEL RESTRICTED SET search_path=pg_catalog;
CREATE FUNCTION charabia.analyze(text,config regconfig DEFAULT 'public.charabia')
RETURNS TABLE(raw text,prepared text,terms text[],kind text,script text,charabia_language text,language text,language_source text,byte_start bigint,byte_end bigint,char_start bigint,char_end bigint,route text,stemmer text)
AS 'MODULE_PATHNAME','analyze_wrapper' LANGUAGE c STRICT STABLE PARALLEL RESTRICTED SET search_path=pg_catalog;
CREATE FUNCTION charabia.query_any(text,config regconfig DEFAULT 'public.charabia') RETURNS tsquery
AS 'MODULE_PATHNAME','query_any_wrapper' LANGUAGE c STRICT STABLE PARALLEL RESTRICTED SET search_path=pg_catalog;
