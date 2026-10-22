DO $schema$
BEGIN
 IF EXISTS(SELECT FROM pg_catalog.pg_namespace n WHERE n.nspname='charabia'
   AND (n.nspowner<>current_user::regrole OR EXISTS(
     SELECT FROM pg_catalog.aclexplode(COALESCE(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) a
     WHERE a.privilege_type='CREATE' AND a.grantee<>n.nspowner
       AND (a.grantee=0 OR NOT EXISTS(SELECT FROM pg_catalog.pg_roles r WHERE r.oid=a.grantee AND r.rolsuper)))))
 THEN RAISE EXCEPTION 'charabia schema must be owned by the installer and deny untrusted CREATE' USING ERRCODE='42501'; END IF;
END
$schema$;
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

CREATE FUNCTION charabia.analysis_info() RETURNS jsonb
AS 'MODULE_PATHNAME','analysis_info_wrapper' LANGUAGE c STABLE SECURITY INVOKER PARALLEL RESTRICTED SET search_path=pg_catalog;

CREATE TEXT SEARCH CONFIGURATION public.charabia(PARSER=charabia.parser);
DO $install$
DECLARE l jsonb; d jsonb;
BEGIN
 FOR l IN SELECT value FROM pg_catalog.jsonb_array_elements(charabia.analysis_info()->'languages') LOOP
  d := l->'defaults';
  EXECUTE pg_catalog.format('CREATE TEXT SEARCH DICTIONARY charabia.%I (TEMPLATE=charabia.template,Language=%L,Discard=%s,Stem=%s,Normalize=true,Filter=false,English=%s)',l->>'language',l->>'language',d->>'discard',d->>'stem',d->>'english');
  EXECUTE pg_catalog.format('ALTER TEXT SEARCH CONFIGURATION public.charabia ADD MAPPING FOR %I WITH charabia.%I',l->>'language',l->>'language');
 END LOOP;
END
$install$;

CREATE FUNCTION charabia.analyze(text)
RETURNS TABLE(raw text,kind text,script text,charabia_language text,language text,language_source text,byte_start bigint,byte_end bigint,char_start bigint,char_end bigint,token_type text)
AS 'MODULE_PATHNAME','analyze_wrapper' LANGUAGE c STRICT STABLE SECURITY INVOKER PARALLEL RESTRICTED SET search_path=pg_catalog;
CREATE FUNCTION charabia.query_any(text,config regconfig DEFAULT 'public.charabia') RETURNS tsquery
AS 'MODULE_PATHNAME','query_any_wrapper' LANGUAGE c STRICT STABLE SECURITY INVOKER PARALLEL RESTRICTED SET search_path=pg_catalog;
