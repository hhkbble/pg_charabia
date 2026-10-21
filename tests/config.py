#!/usr/bin/env python3
"""Native Text Search object ownership, ALTER/cache behavior and real restore."""
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
restored = schema + '_restore'
conflict = schema + '_install'
report = {
    'status': 'running', 'checks': [], 'errors': [], 'commands': [], 'dump_restore': {},
    'source': 'PG18 SQL object ownership and transaction rules; configuration and cache lifecycle expectations; retained pinned-library observations',
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
    value(session, f'CREATE TEXT SEARCH DICTIONARY {name}(TEMPLATE=charabia.template,{options})')


def mapping(session, name, dictionary_name):
    value(session, f'CREATE TEXT SEARCH CONFIGURATION {name}(PARSER=charabia.parser)')
    value(session, f'ALTER TEXT SEARCH CONFIGURATION {name} ADD MAPPING FOR eng WITH {dictionary_name}')


try:
    command(f'CREATE ROLE {owner}; CREATE ROLE {other}; CREATE SCHEMA {schema} AUTHORIZATION {owner}; '
            f'CREATE SCHEMA {moved} AUTHORIZATION {owner}; GRANT USAGE ON SCHEMA {schema},{moved} TO {other}')
    # Failed installation must not overwrite a public object or adopt an unsafe namespace.
    command(f'CREATE DATABASE {conflict} TEMPLATE template0')
    command('CREATE TEXT SEARCH CONFIGURATION public.charabia(COPY=pg_catalog.simple)', conflict)
    with Session(conflict) as s:
        before = value(s, "SELECT 'public.charabia'::regconfig::oid")
        # PG18 DefineTSConfiguration reaches CatalogTupleInsert; its native
        # pg_ts_config_cfgname_index uniqueness check raises 23505 (preserved
        # first failure: artifacts/20261021T192358Z-88998/suite-config-python.log).
        error(s, 'CREATE EXTENSION pg_charabia', 'public default conflict fails atomically', '23505')
        check(value(s, "SELECT 'public.charabia'::regconfig::oid") == before,
              'failed installation preserves the preexisting public configuration')
        check(value(s, "SELECT count(*) FROM pg_extension WHERE extname='pg_charabia'") == '0',
              'failed conflict installation leaves no extension membership')
        value(s, 'DROP TEXT SEARCH CONFIGURATION public.charabia')
        value(s, 'CREATE SCHEMA IF NOT EXISTS charabia')
        value(s, 'GRANT CREATE ON SCHEMA charabia TO PUBLIC')
        error(s, 'CREATE EXTENSION pg_charabia', 'unsafe CREATE grant blocks schema adoption', '42501')
        value(s, 'REVOKE CREATE ON SCHEMA charabia FROM PUBLIC')
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

        # Language changes recompute only omitted defaults; explicit unsupported true is rejected.
        dictionary(s, schema + '.changing', "Language='eng'")
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='kat')")
        check(direct(s, schema + '.changing', 'THE') == ['the'],
              'new language recomputes absent Discard/Stem defaults')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='eng',Stem=true)")
        error(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='kat')",
              'saved unsupported capability prevents Language change', '0A000')
        check(direct(s, schema + '.changing', 'running') == ['running', 'run'],
              'failed ALTER leaves the prior dictionary intact')
        value(s, f"ALTER TEXT SEARCH DICTIONARY {schema}.changing(Language='kat',Stem=false)")
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
            ("Language='kat',Discard=true", '0A000'),
            ("Language='kat',Stem=true", '0A000'),
            ("Language='cmn',Stem=true", '0A000'),
            ("Language='und',Discard=true", '0A000'),
            ("Language='und',Stem=true,Filter=true", '0A000'),
            ("Language='eng',Stem='yes'", '42601'),
            ("Language='eng',Stem='f'", '42601'),
        ]
        for ordinal, (options, state) in enumerate(invalid):
            bad = f'{schema}.bad{ordinal}'
            before = value(s, f"SELECT count(*) FROM pg_ts_dict WHERE dictnamespace='{schema}'::regnamespace")
            error(s, f'CREATE TEXT SEARCH DICTIONARY {bad}(TEMPLATE=charabia.template,{options})',
                  'invalid INIT case ' + str(ordinal), state)
            check(value(s, f"SELECT count(*) FROM pg_ts_dict WHERE dictnamespace='{schema}'::regnamespace") == before,
                  'failed native CREATE leaves no dictionary ' + str(ordinal))
        dictionary(s, schema + '.numeric', "Language='eng',Discard=0,Stem=1,Normalize=1,Filter=0")
        check(direct(s, schema + '.numeric', 'running') == ['running', 'run'], 'native integer Boolean nodes survive catalog serialization')
        value(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.numeric(Stem=0)')
        check(direct(s, schema + '.numeric', 'running') == ['running'], 'native integer Boolean ALTER persists correctly')

        # A partial configuration and external Dictionary are ordinary valid PG objects.
        partial = schema + '.partial'
        value(s, f'CREATE TEXT SEARCH CONFIGURATION {partial}(PARSER=charabia.parser)')
        value(s, f'ALTER TEXT SEARCH CONFIGURATION {partial} ADD MAPPING FOR eng WITH pg_catalog.simple')
        check(value(s, f"SELECT plainto_tsquery('{partial}','The children were running through the garden while their teachers were reading books.')::text") != '',
              'partial external mapping is accepted by native query analysis')
        check(value(s, f"SELECT charabia.query_any('The children were running through the garden while their teachers were reading books.','{partial}')::text") != '',
              'query_any accepts an ordinary partial external mapping')
        identity = scalar_json(s, 'SELECT charabia.analysis_info()')
        check(identity['language_count'] == 71 and 'configuration_oid' not in identity and 'settings' not in identity,
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
        value(s, 'RESET ROLE')
        value(s, 'SET ROLE ' + other)
        error(s, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)', 'foreign owner cannot alter dictionary', '42501')
        error(s, f'CREATE TEXT SEARCH CONFIGURATION {schema}.forbidden(PARSER=charabia.parser)',
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
                      f'DROP ROLE IF EXISTS {owner}', f'DROP ROLE IF EXISTS {other}']:
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
