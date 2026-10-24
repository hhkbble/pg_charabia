#!/usr/bin/env python3
"""Native Text Search object ownership, ALTER/cache behavior and real restore."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from support import EVIDENCE, PSQL, Session, literal, run, save

schema = 'pgcb_config_' + str(os.getpid())
moved = schema + '_moved'
owner = schema + '_owner'
other = schema + '_other'
guardian = schema + '_guardian'
delegate = schema + '_delegate'
restored = schema + '_restore'
conflict = schema + '_install'
report = {
    'status': 'running', 'checks': [], 'errors': [], 'commands': [], 'dump_restore': {},
    'source': 'PG18 native object/ALTER/MVCC contracts; native configuration and shared Dictionary behavior; preserved pinned API observations',
}


def checkpoint():
    save('config-results.json', report)


def check(ok, label, evidence=None):
    report['checks'].append({'label': label, 'passed': bool(ok), 'evidence': evidence})
    checkpoint()
    if not ok:
        raise AssertionError((label, evidence))


def command(sql, database=None, allow_error=False):
    result = run('SET standard_conforming_strings=on;\n' + sql, database, check=False)
    report['commands'].append({'database': database, 'sql': sql, 'exit': result.returncode,
                               'stdout': result.stdout, 'stderr': result.stderr})
    checkpoint()
    if not allow_error and result.returncode:
        raise AssertionError(result.stderr)
    return result


def value(session, sql, allow_error=False):
    output = session.query(sql)
    report['commands'].append({'database': session.database, 'backend_pid': session.pid,
                               'sql': sql, 'output': output})
    checkpoint()
    if not allow_error and 'ERROR:' in output:
        raise AssertionError(output)
    return output


def scalar_json(session, sql):
    return json.loads(value(session, sql))


def direct(session, dictionary, raw):
    return scalar_json(session, 'SELECT to_jsonb(ts_lexize(' + literal(dictionary) + ',' + literal(raw) + '))')


def error(session, sql, label, state='22023'):
    output = value(session, sql, allow_error=True)
    report['errors'].append({'label': label, 'expected_state': state, 'output': output})
    check('ERROR:' in output and state in output, label, output)
    check(value(session, 'SELECT 1') == '1', 'backend recovers after ' + label)


def catalog(session, dictionary):
    return scalar_json(session, 'SELECT jsonb_build_object(\'oid\',oid,\'owner\',dictowner::regrole::text,'
                       "'options',dictinitoption) FROM pg_ts_dict WHERE oid=" + literal(dictionary) + '::regdictionary')


def dictionary(session, name, options="Language='eng'"):
    value(session, f'CREATE TEXT SEARCH DICTIONARY {name}(TEMPLATE=charabia.linguistic_template,{options})')


def mapping(session, name, dictionary_name):
    value(session, f'CREATE TEXT SEARCH CONFIGURATION {name}(PARSER=charabia.linguistic_parser)')
    value(session, f'ALTER TEXT SEARCH CONFIGURATION {name} ADD MAPPING FOR eng WITH {dictionary_name}')


def roundtrip_terms(session):
    return scalar_json(session, f"""SELECT jsonb_build_object(
      'linguistic',to_tsvector('{schema}.roundtrip_linguistic','running')::text,
      'structured',to_tsvector('{schema}.roundtrip_structured','running')::text,
      'independent',to_tsvector('{schema}.roundtrip_independent','running')::text,
      'number_linguistic',to_tsvector('{schema}.roundtrip_linguistic','100ms')::text,
      'number_structured',to_tsvector('{schema}.roundtrip_structured','100ms')::text)""")


def explicit_option_matrix(session):
    """Exercise INIT's finite SQL state space, not a language-quality oracle."""
    value(session, f"""CREATE TEMP TABLE route16_options(accepted int,rejected int);
    DO $matrix$ DECLARE language_code text; mask int; d bool; s bool; n bool; f bool; e bool;
      eligible bool; native_stem bool; valid bool; accepted int:=0; rejected int:=0;
    BEGIN
      FOREACH language_code IN ARRAY ARRAY['eng','deu','nld','rus','spa','fra','por','ita','ara','tur','cmn','jpn','kor','vie','tha','und'] LOOP
        eligible:=language_code=ANY(ARRAY['deu','nld','spa','fra','por','ita','tur','vie']);
        native_stem:=language_code=ANY(ARRAY['eng','deu','nld','rus','spa','fra','por','ita','ara','tur']);
        FOR mask IN 0..31 LOOP
          d:=(mask&1)<>0;s:=(mask&2)<>0;n:=(mask&4)<>0;f:=(mask&8)<>0;e:=(mask&16)<>0;
          valid:=(NOT e OR eligible) AND NOT(e AND f) AND NOT(s AND f)
            AND (NOT d OR language_code<>'und') AND (NOT s OR native_stem OR e);
          BEGIN
            EXECUTE format('CREATE TEXT SEARCH DICTIONARY {schema}.matrix_entry(TEMPLATE=charabia.linguistic_template,Language=%L,Discard=%s,Stem=%s,Normalize=%s,Filter=%s,English=%s)',language_code,d::text,s::text,n::text,f::text,e::text);
            IF NOT valid THEN RAISE EXCEPTION 'accepted forbidden Language=%, mask=%',language_code,mask;END IF;
            EXECUTE 'DROP TEXT SEARCH DICTIONARY {schema}.matrix_entry';accepted:=accepted+1;
          EXCEPTION WHEN invalid_parameter_value OR feature_not_supported THEN
            IF valid THEN RAISE EXCEPTION 'rejected valid Language=%, mask=%: %',language_code,mask,SQLERRM;END IF;
            rejected:=rejected+1;
          END;
        END LOOP;
      END LOOP;
      INSERT INTO route16_options VALUES(accepted,rejected);
    END $matrix$""")
    observed = scalar_json(session, "SELECT jsonb_build_object('accepted',accepted,'rejected',rejected) FROM route16_options")
    check(observed == {'accepted': 228, 'rejected': 284},
          'SQL INIT enforces the exact finite capability matrix', {
              'actual': observed,
              'basis': 'independent 16 route capability sets and five explicit Boolean predicates; not statistical language quality'})
    value(session, 'DROP TABLE route16_options')


def fresh_baseline():
    """Use the real staged 0.4.1 objects without an older-version SQL alias."""
    stage = Path(os.environ['PGCB_EXTENSION_STAGE']).resolve()
    check((stage / 'pg_charabia--0.4.1.sql').is_file()
          and not list(stage.glob('pg_charabia--0.3*.sql')),
          'fresh staging contains 0.4.1 installation SQL and no 0.3.x path')
    library = Path(os.environ['PGCB_LIBRARY_PATH'])
    check(hashlib.sha256(library.read_bytes()).hexdigest() == os.environ['PGCB_LIBRARY_SHA256'],
          'fresh baseline uses the identified current library')
    database = schema + '_fresh'
    report['fresh_baseline'] = {'database': database, 'library': str(library),
                                'library_sha256': os.environ['PGCB_LIBRARY_SHA256']}
    created = False
    try:
        command(f'CREATE DATABASE {database} TEMPLATE template0')
        created = True
        with Session(database) as before_install:
            host_default = value(before_install, 'SHOW default_text_search_config')
        command('CREATE EXTENSION pg_charabia', database)
        with Session(database) as s:
            check(value(s, 'SHOW default_text_search_config') == host_default,
                  'installation preserves the host default_text_search_config')
            check(value(s, "SELECT extversion FROM pg_extension WHERE extname='pg_charabia'") == '0.4.1',
                  'fresh CREATE EXTENSION installs 0.4.1')
            check(value(s, "SELECT count(*) FROM pg_extension_update_paths('pg_charabia') "
                           "WHERE source LIKE '0.3.%' OR target LIKE '0.3.%'") == '0',
                  'fresh baseline exposes no old upgrade path')
            check(scalar_json(s, """SELECT jsonb_build_object(
                'parser_equal',(SELECT cfgparser FROM pg_ts_config WHERE oid='public.charabia'::regconfig)=
                    (SELECT cfgparser FROM pg_ts_config WHERE oid='public.charabia_linguistic'::regconfig),
                'mappings_equal',(SELECT jsonb_agg(jsonb_build_array(maptokentype,mapseqno,mapdict) ORDER BY maptokentype,mapseqno)
                    FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig)=
                    (SELECT jsonb_agg(jsonb_build_array(maptokentype,mapseqno,mapdict) ORDER BY maptokentype,mapseqno)
                    FROM pg_ts_config_map WHERE mapcfg='public.charabia_linguistic'::regconfig),
                'three_configs',(SELECT count(*)=3 FROM pg_ts_config WHERE cfgnamespace='public'::regnamespace
                    AND cfgname IN('charabia','charabia_linguistic','charabia_structured')),
                'no_old_parser',NOT EXISTS(SELECT 1 FROM pg_ts_parser WHERE prsnamespace='charabia'::regnamespace AND prsname='parser'))""") == {
                    'parser_equal': True, 'mappings_equal': True, 'three_configs': True, 'no_old_parser': True},
                  'default and linguistic start equal with distinct configuration OIDs and no old Parser')
            value(s, 'BEGIN')
            value(s, 'ALTER TEXT SEARCH CONFIGURATION public.charabia ALTER MAPPING FOR '
                     'eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und WITH pg_catalog.simple')
            check(value(s, "SELECT mapdict='charabia.eng'::regdictionary FROM pg_ts_config_map "
                           "WHERE mapcfg='public.charabia_linguistic'::regconfig AND maptokentype=1") == 't',
                  'default mapping edits do not synchronize the linguistic configuration')
            check(value(s, "SELECT charabia.query_any('running')::text") == "'running'"
                  and value(s, "SELECT charabia.query_any('running','public.charabia')::text") == "'running'"
                  and value(s, "SELECT charabia.query_any('running','public.charabia_linguistic')::text") == "'running' | 'run'",
                  'omitted query config executes actual default mappings rather than the named linguistic configuration')
            check(scalar_json(s, "SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) FROM charabia.analyze('100ms')a")
                  == scalar_json(s, "SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) FROM charabia.analyze('100ms','public.charabia')a"),
                  'omitted analyze config equals explicit default Parser facts independently of mapping edits')
            check(value(s, 'SHOW default_text_search_config') == host_default,
                  'configuration mapping changes preserve the host default_text_search_config')
            value(s, 'ROLLBACK')
            value(s, "CREATE SCHEMA app; CREATE TEXT SEARCH CONFIGURATION app.ling(COPY=public.charabia_linguistic);"
                     "CREATE TEXT SEARCH CONFIGURATION app.structured(COPY=public.charabia_structured);")
            dictionary(s, 'app.shared', "Language='eng',Discard=false,Stem=true,Normalize=true,Filter=false,English=false")
            for config in ('app.ling', 'app.structured'):
                value(s, f"ALTER TEXT SEARCH CONFIGURATION {config} ALTER MAPPING FOR "
                         "eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und WITH app.shared")
            def run_matches():
                return scalar_json(s, "SELECT jsonb_agg(to_tsvector(config::regconfig,'running') @@ 'run'::tsquery ORDER BY config) "
                                      "FROM unnest(ARRAY['app.ling','app.structured'])config")
            check(run_matches() == [True, True], 'both mode mappings execute the same shared stemming Dictionary')
            value(s, 'ALTER TEXT SEARCH DICTIONARY app.shared(Stem=false)')
            check(run_matches() == [False, False], 'shared ALTER changes real terms in both modes')
            value(s, 'ALTER TEXT SEARCH DICTIONARY app.shared(Stem=true)')
            value(s, 'ALTER TEXT SEARCH CONFIGURATION app.ling RENAME TO renamed; CREATE SCHEMA moved;'
                     'ALTER TEXT SEARCH CONFIGURATION app.renamed SET SCHEMA moved')
            check(scalar_json(s, "SELECT jsonb_agg(raw ORDER BY byte_start) FROM charabia.analyze('100ms','moved.renamed')") == ['100ms'],
                  'COPY and configuration rename/schema move retain linguistic diagnostic mode')
            for mode in ('linguistic', 'structured'):
                config = 'moved.renamed' if mode == 'linguistic' else 'app.structured'
                before = scalar_json(s, f"SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) FROM charabia.analyze('100ms','{config}')a")
                value(s, 'BEGIN')
                value(s, f'ALTER TEXT SEARCH PARSER charabia.{mode}_parser RENAME TO renamed_parser;'
                         'ALTER TEXT SEARCH PARSER charabia.renamed_parser SET SCHEMA app;'
                         f'ALTER FUNCTION charabia.{mode}_parser_start(internal,integer) RENAME TO renamed_start;'
                         'ALTER FUNCTION charabia.renamed_start(internal,integer) SET SCHEMA app')
                after = scalar_json(s, f"SELECT jsonb_agg(to_jsonb(a) ORDER BY byte_start) FROM charabia.analyze('100ms','{config}')a")
                check(after == before, mode + ' diagnostics retain mode through Parser and START rename/schema move')
                value(s, 'ROLLBACK')
            value(s, "CREATE TEXT SEARCH PARSER app.same_callbacks("
                     "START=charabia.linguistic_parser_start,GETTOKEN=charabia.parser_gettoken,"
                     "END=charabia.parser_end,LEXTYPES=charabia.linguistic_parser_lextypes,HEADLINE=charabia.parser_headline);"
                     "CREATE TEXT SEARCH CONFIGURATION app.external(PARSER=app.same_callbacks);"
                     "ALTER TEXT SEARCH CONFIGURATION app.external ADD MAPPING FOR "
                     "eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und WITH pg_catalog.simple")
            check(value(s, "SELECT charabia.query_any('100ms','app.external')::text") == "'100ms'",
                  'external Parser can execute the exact same native callbacks')
            error(s, "SELECT * FROM charabia.analyze('100ms','app.external')",
                  'diagnostic rejects non-member Parser even when every callback is shared', '0A000')
            error(s, "SELECT * FROM charabia.analyze('alpha','pg_catalog.simple')",
                  'diagnostic rejects an external Parser', '0A000')
            check(value(s, "SELECT charabia.query_any('alpha beta','pg_catalog.simple')::text") == "'alpha' | 'beta'",
                  'query_any retains support for arbitrary native configurations')
    finally:
        if created:
            command(f'DROP DATABASE {database}')



try:
    command(f'CREATE ROLE {owner}; CREATE ROLE {other}; CREATE SCHEMA {schema} AUTHORIZATION {owner}; '
            f'CREATE SCHEMA {moved} AUTHORIZATION {owner}; GRANT USAGE ON SCHEMA {schema},{moved} TO {other}')
    fresh_baseline()
    # Failed installation must not overwrite a public object or adopt an unsafe namespace.
    command(f'CREATE DATABASE {conflict} TEMPLATE template0')
    with Session(conflict) as s:
        for config_name in ('charabia', 'charabia_linguistic', 'charabia_structured'):
            value(s, f'CREATE TEXT SEARCH CONFIGURATION public.{config_name}(COPY=pg_catalog.simple)')
            before = value(s, f"SELECT 'public.{config_name}'::regconfig::oid")
            # PG18 native catalog uniqueness raises 23505; installation must roll back.
            error(s, 'CREATE EXTENSION pg_charabia', config_name + ' conflict fails atomically', '23505')
            check(value(s, f"SELECT 'public.{config_name}'::regconfig::oid") == before,
                  config_name + ' failed installation preserves the user object')
            check(value(s, "SELECT count(*) FROM pg_extension WHERE extname='pg_charabia'") == '0',
                  config_name + ' failed installation leaves no extension membership')
            check(value(s, "SELECT count(*) FROM pg_ts_config WHERE cfgnamespace='public'::regnamespace "
                           "AND cfgname IN('charabia','charabia_linguistic','charabia_structured')") == '1',
                  config_name + ' failure leaves no partially installed configurations')
            value(s, f'DROP TEXT SEARCH CONFIGURATION public.{config_name}')
        value(s, 'CREATE SCHEMA IF NOT EXISTS charabia')
        value(s, 'GRANT CREATE ON SCHEMA charabia TO PUBLIC')
        error(s, 'CREATE EXTENSION pg_charabia', 'unsafe CREATE grant blocks schema adoption', '42501')
        value(s, 'REVOKE CREATE ON SCHEMA charabia FROM PUBLIC')
        value(s, f'CREATE ROLE {guardian} SUPERUSER NOLOGIN; '
                 f'CREATE ROLE {delegate} NOSUPERUSER NOLOGIN; '
                 f'GRANT {guardian} TO {delegate} WITH INHERIT TRUE, SET FALSE; '
                 f'GRANT CREATE ON SCHEMA charabia TO {guardian}')
        check(scalar_json(s, f"SELECT jsonb_build_object('superuser',rolsuper,"
                             f"'create',has_schema_privilege(oid,'charabia','CREATE'),"
                             f"'can_set',pg_has_role(oid,'{guardian}','SET')) "
                             f"FROM pg_roles WHERE rolname='{delegate}'") == {
                                 'superuser': False, 'create': True, 'can_set': False},
              'ordinary role inherits schema CREATE without inheriting SUPERUSER or SET ROLE')
        error(s, 'CREATE EXTENSION pg_charabia',
              'inherited CREATE through a superuser ACL blocks schema adoption', '42501')
        value(s, f'REVOKE CREATE ON SCHEMA charabia FROM {guardian}; '
                 f'ALTER SCHEMA charabia OWNER TO {guardian}; '
                 f'REVOKE ALL ON SCHEMA charabia FROM {guardian}; SET ROLE {guardian}')
        check(scalar_json(s, f"SELECT jsonb_build_object("
                             f"'create',has_schema_privilege('{delegate}','charabia','CREATE'),"
                             f"'grant',has_schema_privilege('{delegate}','charabia','CREATE WITH GRANT OPTION'))")
              == {'create': False, 'grant': True},
              'owner membership inherits implicit grant authority after ordinary CREATE is revoked')
        error(s, 'CREATE EXTENSION pg_charabia',
              'inherited owner grant authority blocks schema adoption', '42501')
        value(s, 'RESET ROLE; ALTER SCHEMA charabia OWNER TO CURRENT_USER; '
                 'GRANT USAGE,CREATE ON SCHEMA charabia TO CURRENT_USER')
        value(s, f'REVOKE {guardian} FROM {delegate}')
        value(s, f'ALTER SCHEMA charabia OWNER TO {other}')
        error(s, 'CREATE EXTENSION pg_charabia', 'foreign namespace owner blocks installation', '42501')
        value(s, 'ALTER SCHEMA charabia OWNER TO CURRENT_USER')
        value(s, 'CREATE EXTENSION pg_charabia')
        check(value(s, "SELECT to_tsvector('public.charabia','POSTGRESQL') @@ 'postgresql'::tsquery") == 't',
              'owned protected namespace allows installation after explicit repair')
    command(f'DROP DATABASE {conflict}')

    with Session() as s:
        value(s, 'SET ROLE ' + owner)
        a = schema + '.a'
        b = schema + '.b'
        english = schema + '.eng'
        dictionary(s, english)
        mapping(s, a, english)
        check(direct(s, english, 'running') == ['running', 'run'], 'English capability default enables stemming')
        check(direct(s, english, 'THE') == [], 'English capability default enables builtin discard')
        value(s, f'CREATE TEXT SEARCH CONFIGURATION {b}(COPY={a})')
        shared = scalar_json(s, "SELECT jsonb_agg(jsonb_build_array(mapcfg::regconfig::text,mapdict::regdictionary::text) ORDER BY mapcfg) "
                             f"FROM pg_ts_config_map WHERE mapcfg IN('{a}'::regconfig,'{b}'::regconfig)")
        check(len(shared) == 2 and all(row[1] == english for row in shared),
              'native COPY shallow-copies mappings and shares the same dictionary', shared)
        value(s, f'ALTER TEXT SEARCH DICTIONARY {english}(Stem=false)')
        check(direct(s, english, 'running') == ['running'] and direct(s, english, 'THE') == [],
              'ALTER merges omitted saved options and preserves default Discard')
        check(scalar_json(s, f"SELECT jsonb_agg(ts_lexize(mapdict::regdictionary,'running')) FROM pg_ts_config_map "
                         f"WHERE mapcfg IN('{a}'::regconfig,'{b}'::regconfig)") == [['running'], ['running']],
              'native shared dictionary mutation is visible through both COPY mappings')

        dictionary(s, schema + '.b_eng', "Language='eng',Discard=false,Stem=false,Normalize=false,Filter=false")
        value(s, f'ALTER TEXT SEARCH CONFIGURATION {b} ALTER MAPPING FOR eng WITH {schema}.b_eng')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {english}(Stem=true)')
        check(direct(s, english, 'running') == ['running', 'run'] and direct(s, schema + '.b_eng', 'running') == ['running'],
              'replacing one native mapping provides independent parameters without deep COPY')
        check(direct(s, schema + '.b_eng', 'CAFÉ') == ['café'], 'Normalize=false preserves prepared NFC surface')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.b_eng(Normalize=true)')
        check(direct(s, schema + '.b_eng', 'CAFÉ') == ['cafe'], 'Normalize=true executes the complete finalizer')
        check(direct(s, schema + '.b_eng', 'THE') == ['the'], 'Normalize ALTER does not enable builtin discard')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.b_eng(Discard=true)')
        check(direct(s, schema + '.b_eng', 'THE') == [] and direct(s, schema + '.b_eng', 'running') == ['running'],
              'Discard and Stem options remain independent')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.b_eng(Stem)')
        check(direct(s, schema + '.b_eng', 'running') == ['running', 'run'],
              'native ALTER option removal reuses the language capability default')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.b_eng(Stem=false,Stem=true)')
        check(direct(s, schema + '.b_eng', 'running') == ['running', 'run'],
              'core ALTER same-spelling duplicate merge uses the last value before INIT')
        error(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.b_eng("Stem"=false)',
              'quoted-case duplicate reaching INIT is rejected')

        # English is an independent branch, not a Parser switch or language fallback.
        dictionary(s, schema + '.vie', "Language='vie'")
        check(direct(s, schema + '.vie', 'running') == ['running','run'],
              'Vietnamese defaults enable the English stem without a native algorithm')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false)")
        check(direct(s, schema + '.vie', 'running') == ['running'],
              'omitted Stem recomputes false when English is disabled for Vietnamese')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=true,Stem=true)")
        error(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false)",
              'saved Stem cannot outlive its only enabled algorithm', '0A000')
        check(direct(s, schema + '.vie', 'running') == ['running','run'],
              'failed English ALTER rolls back the whole option change')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)")
        error(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(Filter=true,English=true)",
              'filter cannot transmit a parallel English branch', '22023')
        error(s, f"CREATE TEXT SEARCH DICTIONARY {schema}.bad_eng(TEMPLATE=charabia.linguistic_template,Language='eng',English=true)",
              'English option cannot duplicate the primary English route', '22023')
        error(s, f"CREATE TEXT SEARCH DICTIONARY {schema}.bad_lang(TEMPLATE=charabia.linguistic_template,Language='swe')",
              'outside Language is rejected rather than mapped to und', '22023')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(Filter=true)")
        check(direct(s, schema + '.vie', 'running') == ['running'],
              'saved false English/Stem permits one-replacement Vietnamese filter')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(Filter=false,English,Stem)")
        check(direct(s, schema + '.vie', 'running') == ['running','run'],
              'removing English and Stem options recomputes terminal capability defaults')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=true,Stem=true)")
        error(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie("English"=false)',
              'quoted-case English duplicate reaching INIT is rejected')
        error(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.vie(Language='eng')",
              'saved English true is rejected when primary Language becomes English')
        dictionary(s, schema + '.vie_filter', "Language='vie',Filter=true")
        check(direct(s, schema + '.vie_filter', 'running') == ['running'],
              'omitted English and Stem defaults are false in the no-native-stem filter')
        dictionary(s, schema + '.four', "Language='tur',Discard=false,Stem=true,Normalize=false,Filter=false,English=true")
        check(direct(s, schema + '.four', 'ABBREVIATE') == ['abbrevıate','abbrevıa','abbreviate','abbrevi'],
              'ordinary native dictionary stores four-variant parallel parameters')
        parallel_a = schema + '.parallel_a'
        parallel_b = schema + '.parallel_b'
        mapping(s, parallel_a, schema + '.vie')
        value(s, f'CREATE TEXT SEARCH CONFIGURATION {parallel_b}(COPY={parallel_a})')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)')
        check(scalar_json(s, f"SELECT jsonb_agg(ts_lexize(mapdict::regdictionary,'running')) FROM pg_ts_config_map "
                         f"WHERE mapcfg IN('{parallel_a}'::regconfig,'{parallel_b}'::regconfig)") == [['running'],['running']],
              'COPY shares native English-option mutation without a hidden profile')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=true,Stem=true)')
        explicit_option_matrix(s)

        # Language changes recompute only omitted defaults; explicit unsupported true is rejected.
        dictionary(s, schema + '.changing', "Language='eng'")
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='und')")
        check(direct(s, schema + '.changing', 'THE') == ['the'],
              'new language recomputes absent Discard/Stem defaults')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='eng',Stem=true)")
        error(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='und')",
              'saved unsupported capability prevents Language change', '0A000')
        check(direct(s, schema + '.changing', 'running') == ['running', 'run'],
              'failed ALTER leaves the prior dictionary intact')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='und',Stem=false)")
        error(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language)', 'required Language cannot be removed')
        value(s, 'BEGIN')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='eng',Stem=true)")
        check(direct(s, schema + '.changing', 'running') == ['running', 'run'], 'transaction sees its own changed parameters')
        value(s, 'ROLLBACK')
        check(direct(s, schema + '.changing', 'running') == ['running'], 'rollback restores catalog parameters and analysis')

        invalid = [
            ('Stem=false', '22023'),
            ("Language='zzz'", '22023'),
            ("Language='ENG'", '22023'),
            ("Language='eng',Unknown=true", '22023'),
            ("Language='eng',Stem=false,Stem=true", '22023'),
            ("Language='eng',Filter=true", '22023'),
            ("Language='eng',Stem=true,Filter=true", '22023'),
            ("Language='vie',English=false,Stem=true", '0A000'),
            ("Language='vie',English=true,Filter=true,Stem=false", '22023'),
            ("Language='rus',English=true", '22023'),
            ("Language='cmn',English=true", '22023'),
            ("Language='und',English=true", '22023'),
            ("Language='und',Discard=true", '0A000'),
            ("Language='und',Stem=true", '0A000'),
            ("Language='cmn',Stem=true", '0A000'),
            ("Language='und',Stem=true,Filter=true", '0A000'),
            ("Language='eng',Stem='yes'", '42601'),
            ("Language='eng',Stem='f'", '42601'),
        ]
        for ordinal, (options, state) in enumerate(invalid):
            bad = f'{schema}.bad{ordinal}'
            before = value(s, f"SELECT count(*) FROM pg_ts_dict WHERE dictnamespace='{schema}'::regnamespace")
            error(s, f'CREATE TEXT SEARCH DICTIONARY {bad}(TEMPLATE=charabia.linguistic_template,{options})',
                  'invalid INIT case ' + str(ordinal), state)
            check(value(s, f"SELECT count(*) FROM pg_ts_dict WHERE dictnamespace='{schema}'::regnamespace") == before,
                  'failed native CREATE leaves no dictionary ' + str(ordinal))
        dictionary(s, schema + '.numeric', "Language='eng',Discard=0,Stem=1,Normalize=1,Filter=0")
        check(direct(s, schema + '.numeric', 'running') == ['running', 'run'], 'native integer Boolean nodes survive catalog serialization')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.numeric(Stem=0)')
        check(direct(s, schema + '.numeric', 'running') == ['running'], 'native integer Boolean ALTER persists correctly')

        # A partial configuration and external Dictionary are ordinary valid PG objects.
        partial = schema + '.partial'
        value(s, f'CREATE TEXT SEARCH CONFIGURATION {partial}(PARSER=charabia.linguistic_parser)')
        value(s, f'ALTER TEXT SEARCH CONFIGURATION {partial} ADD MAPPING FOR eng WITH pg_catalog.simple')
        check(value(s, f"SELECT plainto_tsquery('{partial}','The children were running through the garden while their teachers were reading books.')::text") != '',
              'partial external mapping is accepted by native query analysis')
        check(value(s, f"SELECT charabia.query_any('The children were running through the garden while their teachers were reading books.','{partial}')::text") != '',
              'query_any accepts an ordinary partial external mapping')
        identity = scalar_json(s, 'SELECT charabia.analysis_info()')
        check(identity['language_count'] == 16 and 'configuration_oid' not in identity and 'settings' not in identity,
              'analysis_info remains static rather than enforcing complete private mappings')
        rows = scalar_json(s, "SELECT jsonb_agg(to_jsonb(a)) FROM charabia.analyze('alpha, beta') a")
        check(all(set(row) == {'raw', 'kind', 'script', 'charabia_language', 'language', 'language_source',
                               'byte_start', 'byte_end', 'char_start', 'char_end', 'token_type'} for row in rows),
              'analyze exposes only the eleven Parser-fact columns')

        # Quoted identifiers and native OID references survive rename/schema movement.
        unusual = f'{schema}."Mixed.""配置"'
        dictionary(s, unusual, "Language='fra',Discard=false,Stem=false,Normalize=false,Filter=false")
        check(direct(s, unusual, 'ÉTÉ') == ['été'], 'quoted dictionary identifiers retain Unicode parameter behavior')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {english} RENAME TO renamed')
        renamed = schema + '.renamed'
        value(s, f'ALTER TEXT SEARCH DICTIONARY {renamed} SET SCHEMA {moved}')
        renamed = moved + '.renamed'
        check(scalar_json(s, f"SELECT to_jsonb(mapdict='{renamed}'::regdictionary) FROM pg_ts_config_map WHERE mapcfg='{a}'::regconfig") is True,
              'native mapping follows dictionary OID after rename and SET SCHEMA')
        error(s, f'DROP TEXT SEARCH DICTIONARY {renamed}', 'native mapping dependency enforces RESTRICT', '2BP01')
        old_oid = int(value(s, f"SELECT '{a}'::regconfig::oid"))
        saved_dictionary = catalog(s, renamed)
        saved_parallel = catalog(s, schema + '.vie')
        dictionary(s, schema + '.roundtrip_shared',
                   "Language='eng',Discard=false,Stem=true,Normalize=true,Filter=false,English=false")
        dictionary(s, schema + '.roundtrip_separate',
                   "Language='eng',Discard=false,Stem=false,Normalize=true,Filter=false,English=false")
        for mode in ('linguistic', 'structured'):
            value(s, f'CREATE TEXT SEARCH CONFIGURATION {schema}.roundtrip_{mode}(COPY=public.charabia_{mode});'
                     f'ALTER TEXT SEARCH CONFIGURATION {schema}.roundtrip_{mode} ALTER MAPPING FOR '
                     'eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und '
                     f'WITH {schema}.roundtrip_shared')
        value(s, f'CREATE TEXT SEARCH CONFIGURATION {schema}.roundtrip_independent(COPY={schema}.roundtrip_linguistic);'
                 f'ALTER TEXT SEARCH CONFIGURATION {schema}.roundtrip_independent ALTER MAPPING FOR '
                 'eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und '
                 f'WITH {schema}.roundtrip_separate')
        # Controlled English mappings make these literal terms independent of detection.
        roundtrip_expected = {
            'linguistic': "'run':1 'running':1", 'structured': "'run':1 'running':1",
            'independent': "'running':1", 'number_linguistic': "'100ms':1",
            'number_structured': "'100':1 '100ms':1 'ms':1",
        }
        check(roundtrip_terms(s) == roundtrip_expected,
              'logical backup fixture has independent mode and shared/separate Dictionary expectations')
        saved_roundtrip = {name: catalog(s, schema + '.' + name)
                           for name in ('roundtrip_shared', 'roundtrip_separate')}
        value(s, 'RESET ROLE')
        value(s, 'SET ROLE ' + other)
        error(s, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)', 'foreign owner cannot alter dictionary', '42501')
        error(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)',
              'foreign owner cannot alter English parameters', '42501')
        error(s, f'CREATE TEXT SEARCH CONFIGURATION {schema}.forbidden(PARSER=charabia.linguistic_parser)',
              'target schema CREATE permission is enforced', '42501')
        error(s, 'CREATE FUNCTION charabia.untrusted()RETURNS int LANGUAGE sql AS $$SELECT 1$$',
              'internal schema denies untrusted CREATE', '42501')
        error(s, 'ALTER TEXT SEARCH DICTIONARY charabia.eng(Stem=false)', 'ordinary role cannot modify default extension members', '42501')
        value(s, 'RESET ROLE')

    # Warm one backend, change parameters in another, and keep rollback separate.
    with Session() as reader, Session() as writer:
        value(reader, 'SET ROLE ' + owner)
        value(writer, 'SET ROLE ' + owner)
        check(direct(reader, renamed, 'running') == ['running', 'run'], 'reader warms the real native dictionary cache')
        value(writer, 'BEGIN')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)')
        check(direct(writer, renamed, 'running') == ['running'], 'writer sees uncommitted ALTER')
        check(direct(reader, renamed, 'running') == ['running', 'run'], 'other session cannot see uncommitted ALTER')
        value(writer, 'ROLLBACK')
        check(direct(reader, renamed, 'running') == ['running', 'run'], 'rollback leaves the warmed reader unchanged')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)')
        check(direct(reader, renamed, 'running') == ['running'], 'committed ALTER invalidates the warm reader cache')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=true)')
        check(direct(reader, renamed, 'running') == ['running', 'run'], 'second commit rebuilds the same reader cache again')
        check(direct(reader, schema + '.vie', 'running') == ['running','run'],
              'reader warms a dictionary with the English-only algorithm')
        value(writer, 'BEGIN')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)')
        check(direct(writer, schema + '.vie', 'running') == ['running'] and
              direct(reader, schema + '.vie', 'running') == ['running','run'],
              'uncommitted English change stays in the writer session')
        value(writer, 'ROLLBACK')
        check(direct(reader, schema + '.vie', 'running') == ['running','run'],
              'English option rollback leaves the warm reader unchanged')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)')
        check(direct(reader, schema + '.vie', 'running') == ['running'],
              'committed English change invalidates the warm reader')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=true,Stem=true)')
        check(direct(reader, schema + '.vie', 'running') == ['running','run'],
              'second English commit rebuilds the reader using shared resources')
        routes = 'eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und'
        dictionary(writer, schema + '.cache_literal',
                   "Language='eng',Discard=false,Stem=false,Normalize=true,Filter=false,English=false")
        for mode in ('linguistic', 'structured'):
            cached_config = schema + '.cache_' + mode
            value(writer, f'CREATE TEXT SEARCH CONFIGURATION {cached_config}(COPY=public.charabia_{mode});'
                          f'ALTER TEXT SEARCH CONFIGURATION {cached_config} ALTER MAPPING FOR {routes} WITH {renamed}')
            value(reader, f"PREPARE cached_{mode}(text) AS SELECT to_tsvector('{cached_config}',$1) @@ 'run'::tsquery")
            value(reader, f"PREPARE mapped_{mode}(text) AS SELECT charabia.query_any($1,'{cached_config}')::text")
        def cached_matches(session, prepared=False):
            return [value(session, f"EXECUTE cached_{mode}('running')" if prepared else
                          f"SELECT to_tsvector('{schema}.cache_{mode}','running') @@ 'run'::tsquery")
                    for mode in ('linguistic', 'structured')]
        check(cached_matches(reader, True) == ['t', 't'],
              'parameterized plans warm both mode mappings and the shared Dictionary')
        value(writer, 'BEGIN')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)')
        check(cached_matches(writer) == ['f', 'f'] and cached_matches(reader, True) == ['t', 't'],
              'uncommitted shared parameters stay local across both mode plans')
        value(writer, 'ROLLBACK')
        check(cached_matches(reader, True) == ['t', 't'], 'rollback preserves both warm parameterized mode plans')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)')
        check(cached_matches(reader, True) == ['f', 'f'],
              'committed shared ALTER invalidates both parameterized mode plans')
        value(writer, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=true)')
        check(cached_matches(reader, True) == ['t', 't'], 'restored shared parameters rebuild both mode plans')
        value(writer, f'ALTER TEXT SEARCH CONFIGURATION {schema}.cache_linguistic ALTER MAPPING FOR {routes} WITH {schema}.cache_literal')
        check(value(reader, "EXECUTE mapped_linguistic('running')") == "'running'"
              and value(reader, "EXECUTE mapped_structured('running')") == "'running' | 'run'",
              'committed mapping invalidation changes one actual plan without synchronizing the other mode')
        for mode in ('linguistic', 'structured'):
            value(reader, f'DEALLOCATE cached_{mode}; DEALLOCATE mapped_{mode}')
        # These ordinary fixtures are covered by their own isolated assertions;
        # leave the retained baseline dump/restore fixture's object set unchanged.
        value(writer, f'DROP TEXT SEARCH CONFIGURATION {schema}.cache_linguistic;'
                      f'DROP TEXT SEARCH CONFIGURATION {schema}.cache_structured;'
                      f'DROP TEXT SEARCH DICTIONARY {schema}.cache_literal')
        report['cache_backends'] = {'reader': reader.pid, 'writer': writer.pid}
        checkpoint()

    # Restore ordinary catalogs into a new database: object names are not ownership metadata.
    archive = EVIDENCE / 'config-dump.custom'
    dump = [str(Path(PSQL).parent / 'pg_dump'), '-Fc', '-n', schema, '-n', moved, '-f', str(archive)]
    result = subprocess.run(dump, text=True, capture_output=True)
    report['dump_restore']['dump'] = {'argv': dump, 'exit': result.returncode, 'stderr': result.stderr}
    checkpoint()
    check(result.returncode == 0, 'pg_dump ordinary dictionaries/mappings succeeds', result.stderr)
    command(f'CREATE DATABASE {restored} TEMPLATE template0')
    command('CREATE EXTENSION pg_charabia', restored)
    restore = [str(Path(PSQL).parent / 'pg_restore'), '--exit-on-error', '-d', restored, str(archive)]
    result = subprocess.run(restore, text=True, capture_output=True)
    report['dump_restore']['restore'] = {'argv': restore, 'exit': result.returncode, 'stderr': result.stderr}
    checkpoint()
    check(result.returncode == 0, 'pg_restore ordinary catalogs succeeds', result.stderr)
    with Session(restored) as target:
        value(target, 'SET ROLE ' + owner)
        new_oid = int(value(target, f"SELECT '{a}'::regconfig::oid"))
        after = catalog(target, renamed)
        report['dump_restore'].update({'old_oid': old_oid, 'new_oid': new_oid, 'dictionary_before': saved_dictionary,
                                       'dictionary_after': after})
        check(new_oid != old_oid, 'restoration actually creates a new configuration OID', report['dump_restore'])
        check(after['owner'] == owner and after['options'] == saved_dictionary['options'],
              'native dump/restore preserves owner, serialized options and renamed references', after)
        check(direct(target, unusual, 'ÉTÉ') == ['été'], 'quoted Unicode dictionary restores correctly')
        restored_parallel = catalog(target, schema + '.vie')
        check(restored_parallel['oid'] != saved_parallel['oid'] and restored_parallel['owner'] == owner and
              restored_parallel['options'] == saved_parallel['options'],
              'dump/restore preserves English parameters and owner with a new OID', restored_parallel)
        check(direct(target, schema + '.four', 'ABBREVIATE') == ['abbrevıate','abbrevıa','abbreviate','abbrevi'],
              'restored Dictionary reconstructs four alternatives from immutable resources')
        check(roundtrip_terms(target) == roundtrip_expected,
              'logical restore preserves literal vectors and ordinary versus atomic positions in both modes')
        for name, previous in saved_roundtrip.items():
            current = catalog(target, schema + '.' + name)
            check(current['oid'] != previous['oid'] and current['owner'] == owner
                  and current['options'] == previous['options'],
                  'logical restore reconstructs ' + name + ' with a new OID and preserved options', current)
        restored_mappings = scalar_json(target, f"""SELECT jsonb_agg(jsonb_build_object(
          'name',c.cfgname,'owner',c.cfgowner::regrole::text,'parser',p.prsname,
          'dictionary',m.mapdict::regdictionary::text) ORDER BY c.cfgname)
          FROM pg_ts_config c JOIN pg_ts_parser p ON p.oid=c.cfgparser
          JOIN pg_ts_config_map m ON m.mapcfg=c.oid AND m.maptokentype=1 AND m.mapseqno=1
          WHERE c.cfgnamespace='{schema}'::regnamespace AND c.cfgname LIKE 'roundtrip_%'""")
        check(restored_mappings == [
            {'name': 'roundtrip_independent', 'owner': owner, 'parser': 'linguistic_parser',
             'dictionary': schema + '.roundtrip_separate'},
            {'name': 'roundtrip_linguistic', 'owner': owner, 'parser': 'linguistic_parser',
             'dictionary': schema + '.roundtrip_shared'},
            {'name': 'roundtrip_structured', 'owner': owner, 'parser': 'structured_parser',
             'dictionary': schema + '.roundtrip_shared'},
        ], 'logical restore retains actual Parsers, owners, and shared versus independent mappings', restored_mappings)
        for mode, kinds in (('linguistic', ['word']), ('structured', ['numword'])):
            config = schema + '.roundtrip_' + mode
            check(scalar_json(target, "SELECT jsonb_agg(kind ORDER BY byte_start) FROM "
                              f"charabia.analyze('100ms','{config}')") == kinds,
                  'restored ' + mode + ' configuration resolves its actual Parser mode')
            check(value(target, f"SELECT ts_headline('{config}','Running','run'::tsquery,'HighlightAll=true')")
                  == '<b>Running</b>', 'restored ' + mode + ' headline retains original case')
        check(scalar_json(target, f"SELECT jsonb_build_object("
                                  f"'usage',has_schema_privilege('{other}','{schema}','USAGE'),"
                                  f"'create',has_schema_privilege('{other}','{schema}','CREATE'))")
              == {'usage': True, 'create': False}, 'logical restore preserves application schema ACL')
        value(target, 'RESET ROLE; SET ROLE ' + other)
        check(roundtrip_terms(target) == roundtrip_expected,
              'restored schema USAGE permits ordinary callers to use both analysis modes')
        error(target, f'ALTER TEXT SEARCH DICTIONARY {schema}.roundtrip_shared(Stem=false)',
              'logical restore preserves Dictionary owner protection', '42501')
        value(target, 'RESET ROLE; SET ROLE ' + owner)
        value(target, f'ALTER TEXT SEARCH DICTIONARY {schema}.roundtrip_shared(Stem=false)')
        check(roundtrip_terms(target) == dict(roundtrip_expected, linguistic="'running':1", structured="'running':1"),
              'restored shared ALTER changes both modes without changing the independent Dictionary')
        value(target, f'ALTER TEXT SEARCH DICTIONARY {schema}.roundtrip_separate(Stem=true)')
        check(roundtrip_terms(target) == dict(roundtrip_expected, linguistic="'running':1",
                                              structured="'running':1", independent="'run':1 'running':1"),
              'restored independent ALTER changes only its own configuration')
        value(target, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)')
        check(direct(target, schema + '.vie', 'running') == ['running'],
              'restored English parameter can be changed using ordinary DDL')
        value(target, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=true,Stem)')
        check(direct(target, schema + '.vie', 'running') == ['running','run'],
              'restored option removal recomputes the English algorithm default')
        value(target, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)')
        check(direct(target, renamed, 'running') == ['running'], 'restored dictionary can be altered without old OID assumptions')
        value(target, f'DROP TEXT SEARCH CONFIGURATION {a}')
        value(target, f'DROP TEXT SEARCH DICTIONARY {renamed}')
        check(value(target, f"SELECT count(*) FROM pg_ts_dict d JOIN pg_namespace n ON n.oid=d.dictnamespace WHERE n.nspname='{moved}' AND d.dictname='renamed'") == '0',
              'restored ordinary configuration and dictionary can be dropped independently')

    with Session() as s:
        value(s, 'SET ROLE ' + owner)
        value(s, f'CREATE TABLE {schema}.dependent(body text)')
        value(s, f"CREATE INDEX dependent_gin ON {schema}.dependent USING gin(to_tsvector('{b}',body))")
        error(s, f'DROP TEXT SEARCH CONFIGURATION {b}', 'native expression index dependency enforces RESTRICT', '2BP01')
        value(s, f'DROP TABLE {schema}.dependent')
        value(s, f'DROP TEXT SEARCH CONFIGURATION {b}')
        check(direct(s, schema + '.b_eng', 'running') == ['running', 'run'],
              'dropping ordinary configuration does not implicitly delete its dictionaries')
    report['status'] = 'passed'
except BaseException as exc:
    report['status'] = 'failed'
    report['error'] = repr(exc)
    raise
finally:
    checkpoint()
    cleanup = []
    primary = sys.exception()
    for statement in [f'DROP DATABASE IF EXISTS {restored}', f'DROP DATABASE IF EXISTS {conflict}',
                      f'DROP SCHEMA IF EXISTS {schema} CASCADE', f'DROP SCHEMA IF EXISTS {moved} CASCADE',
                      f'DROP ROLE IF EXISTS {owner}', f'DROP ROLE IF EXISTS {other}',
                      f'DROP ROLE IF EXISTS {delegate}', f'DROP ROLE IF EXISTS {guardian}']:
        try:
            command(statement)
        except BaseException as exc:
            cleanup.append({'sql': statement, 'error': repr(exc)})
    report['cleanup_errors'] = cleanup
    checkpoint()
    if cleanup:
        if primary is None:
            raise AssertionError(cleanup)
        primary.add_note('configuration cleanup failed: ' + repr(cleanup))
