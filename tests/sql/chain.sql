\set ON_ERROR_STOP on
BEGIN;
CREATE SCHEMA pgcb_chain_sql;
CREATE FUNCTION pg_temp.assert_chain(ok boolean,label text) RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'chain assertion: %',label;END IF;END$fn$;
-- These explicit Language choices test dictionary semantics, not automatic detection.
CREATE TEXT SEARCH DICTIONARY pgcb_chain_sql.eng (
 TEMPLATE=charabia.template,Language='eng',Discard=false,Stem=true,Normalize=true,Filter=false,English=false);
CREATE TEXT SEARCH DICTIONARY pgcb_chain_sql.fra_plain (
 TEMPLATE=charabia.template,Language='fra',Discard=false,Stem=false,Normalize=false,Filter=false,English=false);
CREATE TEXT SEARCH DICTIONARY pgcb_chain_sql.fra_fold (
 TEMPLATE=charabia.template,Language='fra',Discard=false,Stem=false,Normalize=true,Filter=false,English=false);
CREATE TEXT SEARCH DICTIONARY pgcb_chain_sql.tur_plain (
 TEMPLATE=charabia.template,Language='tur',Discard=false,Stem=true,Normalize=false,Filter=false,English=false);
CREATE TEXT SEARCH DICTIONARY pgcb_chain_sql.tur_fold (
 TEMPLATE=charabia.template,Language='tur',Discard=false,Stem=true,Normalize=true,Filter=false,English=false);
CREATE TEXT SEARCH DICTIONARY pgcb_chain_sql.tur_parallel (
 TEMPLATE=charabia.template,Language='tur',Discard=false,Stem=true,Normalize=false,Filter=false,English=true);
SELECT pg_temp.assert_chain(ts_lexize('pgcb_chain_sql.eng','running')=ARRAY['running','run'],
 'terminal preserves surface and one different English stem');
SELECT pg_temp.assert_chain(ts_lexize('pgcb_chain_sql.fra_plain','CAFÉ')=ARRAY['café'],
 'Normalize=false retains NFC accent after required casing');
SELECT pg_temp.assert_chain(ts_lexize('pgcb_chain_sql.fra_fold','CAFÉ')=ARRAY['cafe'],
 'Normalize=true applies final accent folding');
SELECT pg_temp.assert_chain(ts_lexize('pgcb_chain_sql.tur_plain','IŞIKLAR')=ARRAY['ışıklar','ışık'],
 'Turkish casing and accent-preserving preparation precede stemming');
SELECT pg_temp.assert_chain(ts_lexize('pgcb_chain_sql.tur_fold','IŞIKLAR')=ARRAY['isiklar','isik'],
 'Turkish final folding applies separately to surface and stem');
-- Independent per-branch Snowball/casing golden; no automatic-language oracle.
SELECT pg_temp.assert_chain(ts_lexize('pgcb_chain_sql.tur_parallel','ABBREVIATE')=
 ARRAY['abbrevıate','abbrevıa','abbreviate','abbrevi'],
 'Turkish and English branches retain four ordered distinct alternatives');
CREATE TEXT SEARCH CONFIGURATION pgcb_chain_sql.simple_all(PARSER=charabia.parser);
DO $fn$ DECLARE r record;BEGIN
 FOR r IN SELECT alias FROM ts_token_type('charabia.parser') WHERE alias NOT IN('blank','display_only') LOOP
  EXECUTE format('ALTER TEXT SEARCH CONFIGURATION pgcb_chain_sql.simple_all ADD MAPPING FOR %I WITH pg_catalog.simple',r.alias);
 END LOOP;
END$fn$;
SELECT pg_temp.assert_chain(to_tsvector('pgcb_chain_sql.simple_all','alpha, beta')='''alpha'':1 ''beta'':2'::tsvector,
 'external simple cannot accidentally index language-tagged blank spans');
SELECT pg_temp.assert_chain((SELECT string_agg(token,'' ORDER BY ordinality)='alpha, beta' FROM ts_parse('charabia.parser','alpha, beta')WITH ORDINALITY),
 'Parser preserves the original blank bytes for headline');
SELECT pg_temp.assert_chain(NOT EXISTS(SELECT 1 FROM pg_ts_config_map m JOIN ts_token_type('charabia.parser')t ON t.tokid=m.maptokentype
 WHERE m.mapcfg='public.charabia'::regconfig AND t.alias IN('blank','display_only')),
 'default configuration leaves both nonlexical types unmapped');
ROLLBACK;
