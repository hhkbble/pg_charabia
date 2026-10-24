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
    value(session, f'CREATE TEXT SEARCH CONFIGURATION {name}(PARSER=charabia.parser)')
    value(session, f'ALTER TEXT SEARCH CONFIGURATION {name} ADD MAPPING FOR eng WITH {dictionary_name}')


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


def upgrade_noop():
    """Exercise the native version transition with current SQL and the current library."""
    temporary = Path(os.environ['PGCB_TEMP_DIR']).resolve()
    stage = Path(os.environ['PGCB_EXTENSION_STAGE']).resolve()
    marker = temporary / '.pg-charabia-owner'
    check(marker.is_file() and not marker.is_symlink() and marker.read_text() == EVIDENCE.name
          and stage == temporary / 'stage/extension',
          'upgrade fixture writes only inside the current owned staging directory')
    source = stage / 'pg_charabia--0.3.1.sql'
    upgrade = stage / 'pg_charabia--0.3.0--0.3.1.sql'
    alias = stage / 'pg_charabia--0.3.0.sql'
    library = Path(os.environ['PGCB_LIBRARY_PATH'])
    contents = source.read_bytes()
    source_hash = hashlib.sha256(contents).hexdigest()
    check(upgrade.is_file() and hashlib.sha256(library.read_bytes()).hexdigest() == os.environ['PGCB_LIBRARY_SHA256'],
          'upgrade script and the identified current library are staged')
    database = schema + '_upgrade'
    evidence = report['upgrade'] = {
        'database': database, 'source_sql': str(source), 'source_sql_sha256': source_hash,
        'upgrade_sql': str(upgrade), 'upgrade_sql_sha256': hashlib.sha256(upgrade.read_bytes()).hexdigest(),
        'library': str(library), 'library_sha256': os.environ['PGCB_LIBRARY_SHA256'],
        'basis': 'The source installation DDL is byte-identical to v0.3.0. Current complete staged SQL is temporarily aliased as 0.3.0 to test the no-op catalog transition; this is not an old-library compatibility test.',
    }
    checkpoint()
    alias_identity = None
    created = False
    try:
        # Exclusive creation preserves any preexisting file. The source is the
        # just-built (or extracted-package) SQL, never an earlier package.
        with alias.open('xb') as handle:
            stat = os.fstat(handle.fileno())
            alias_identity = (stat.st_dev, stat.st_ino)
            handle.write(contents)
        evidence['alias_sql_sha256'] = hashlib.sha256(alias.read_bytes()).hexdigest()
        check(evidence['alias_sql_sha256'] == source_hash, 'old-version fixture uses the exact current installation SQL')
        command(f'CREATE DATABASE {database} TEMPLATE template0')
        created = True
        command("CREATE EXTENSION pg_charabia VERSION '0.3.0'", database)
        with Session(database) as session:
            check(value(session, "SELECT extversion FROM pg_extension WHERE extname='pg_charabia'") == '0.3.0',
                  'native CREATE installs the old catalog version for the upgrade fixture')
            check(scalar_json(session, 'SELECT charabia.analysis_info()')['extension'] == '0.3.1',
                  'old catalog fixture already runs the current 0.3.1 library')
            value(session, f'CREATE SCHEMA app AUTHORIZATION {owner}; SET ROLE {owner}')
            value(session, """CREATE TEXT SEARCH DICTIONARY app.words(
                TEMPLATE=charabia.linguistic_template,Language='eng',Discard=false,
                Stem=false,Normalize=false,Filter=false,English=false);
                CREATE TEXT SEARCH DICTIONARY app.units(
                TEMPLATE=charabia.structured_template,Kind='numword',Discard=false,Stem=false,Normalize=false);
                CREATE TEXT SEARCH CONFIGURATION app.search(COPY=public.charabia);
                ALTER TEXT SEARCH CONFIGURATION app.search ALTER MAPPING FOR
                eng,deu,nld,rus,spa,fra,por,ita,ara,tur,cmn,jpn,kor,vie,tha,und WITH app.words;
                ALTER TEXT SEARCH CONFIGURATION app.search ALTER MAPPING FOR numword WITH app.units;
                CREATE TABLE app.docs(id integer PRIMARY KEY,body text NOT NULL,vector tsvector NOT NULL);
                INSERT INTO app.docs VALUES(1,'running',$vector$'running':1$vector$::tsvector),
                    (2,'banana',$vector$'banana':1$vector$::tsvector);
                CREATE INDEX upgrade_gin ON app.docs USING gin(to_tsvector('app.search',body));
                RESET ROLE""")
            check(direct(session, 'app.words', 'CAFÉ') == ['café']
                  and direct(session, 'app.words', 'running') == ['running'],
                  'upgrade fixture has independent non-default dictionary options')
            snapshot = """SELECT jsonb_build_object(
                'extension',(SELECT to_jsonb(e)-'extversion' FROM pg_extension e WHERE extname='pg_charabia'),
                'members',(SELECT jsonb_agg(jsonb_build_array(classid::regclass::text,objid,objsubid) ORDER BY classid,objid,objsubid)
                    FROM pg_depend WHERE refclassid='pg_extension'::regclass
                    AND refobjid=(SELECT oid FROM pg_extension WHERE extname='pg_charabia') AND deptype='e'),
                'namespaces',(SELECT jsonb_agg(to_jsonb(n) ORDER BY oid) FROM pg_namespace n WHERE nspname IN('charabia','app')),
                'functions',(SELECT jsonb_agg(to_jsonb(p) ORDER BY oid) FROM pg_proc p WHERE pronamespace='charabia'::regnamespace),
                'parsers',(SELECT jsonb_agg(to_jsonb(p) ORDER BY oid) FROM pg_ts_parser p WHERE prsnamespace='charabia'::regnamespace),
                'templates',(SELECT jsonb_agg(to_jsonb(t) ORDER BY oid) FROM pg_ts_template t WHERE tmplnamespace='charabia'::regnamespace),
                'dictionaries',(SELECT jsonb_agg(to_jsonb(d) ORDER BY oid) FROM pg_ts_dict d WHERE dictnamespace IN('charabia'::regnamespace,'app'::regnamespace)),
                'configurations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY oid) FROM pg_ts_config c WHERE oid IN('public.charabia'::regconfig,'app.search'::regconfig)),
                'mappings',(SELECT jsonb_agg(to_jsonb(m) ORDER BY mapcfg,maptokentype,mapseqno) FROM pg_ts_config_map m WHERE mapcfg IN('public.charabia'::regconfig,'app.search'::regconfig)),
                'relations',(SELECT jsonb_agg(jsonb_build_object('oid',oid,'name',relname,'owner',relowner,'kind',relkind,'options',reloptions) ORDER BY oid)
                    FROM pg_class WHERE relnamespace='app'::regnamespace),
                'index_definition',pg_get_indexdef('app.upgrade_gin'::regclass),
                'rows',(SELECT jsonb_agg(to_jsonb(d) ORDER BY id) FROM app.docs d))"""

            def observe(label):
                mappings = (Path(f'/proc/{session.pid}/maps').read_text()
                            if os.environ['PGCB_PLATFORM'] == 'linux-amd64'
                            else subprocess.check_output(['lsof', '-p', str(session.pid)], text=True))
                (EVIDENCE / ('upgrade-' + label + '-backend-maps.txt')).write_text(mappings)
                check(any(path in mappings for path in (str(library), str(library.resolve())))
                      and hashlib.sha256(library.read_bytes()).hexdigest() == evidence['library_sha256'],
                      'upgrade ' + label + ' backend maps the exact current library')
                value(session, 'SET enable_seqscan=off')
                query = "SELECT id FROM app.docs WHERE to_tsvector('app.search',body)@@'running'::tsquery"
                plan = scalar_json(session, 'EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) ' + query)
                plan_nodes = [plan[0]['Plan']]
                for node in plan_nodes:
                    plan_nodes.extend(node.get('Plans', []))
                check(any(node.get('Index Name') == 'upgrade_gin' and node.get('Node Type') == 'Bitmap Index Scan'
                          and node.get('Actual Loops', 0) > 0 for node in plan_nodes),
                      'upgrade ' + label + ' executes the preserved GIN index', plan)
                check(scalar_json(session, "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM(" + query + ')q') == [1],
                      'upgrade ' + label + ' preserves the independently specified search result')
                value(session, 'RESET enable_seqscan')
                return scalar_json(session, snapshot)

            evidence['backend_pid'] = session.pid
            evidence['before'] = observe('before')
            checkpoint()
            value(session, "ALTER EXTENSION pg_charabia UPDATE TO '0.3.1'")
            evidence['after'] = observe('after')
            evidence['catalog_version'] = value(session, "SELECT extversion FROM pg_extension WHERE extname='pg_charabia'")
            check(evidence['catalog_version'] == '0.3.1', 'native UPDATE advances the extension catalog to 0.3.1')
            check(evidence['before'] == evidence['after'],
                  'no-op upgrade preserves default and user OIDs, ownership, options, mappings, data and indexes')
    finally:
        primary = sys.exception()
        cleanup = []
        if created:
            try:
                command(f'DROP DATABASE {database}')
                created = False
            except BaseException as exc:
                cleanup.append(repr(exc))
        if alias_identity is not None:
            try:
                stat = alias.lstat()
                if alias.is_symlink() or (stat.st_dev, stat.st_ino) != alias_identity or hashlib.sha256(alias.read_bytes()).hexdigest() != source_hash:
                    raise AssertionError('Upgrade alias ownership or contents changed; retained ' + str(alias))
                alias.unlink()
            except BaseException as exc:
                cleanup.append(repr(exc))
        evidence['cleanup'] = {'database_removed': not created, 'alias_removed': alias_identity is not None and not alias.exists(), 'errors': cleanup}
        checkpoint()
        if cleanup:
            if primary is None:
                raise AssertionError(cleanup)
            primary.add_note('upgrade fixture cleanup failed: ' + repr(cleanup))


try:
    command(f'CREATE ROLE {owner}; CREATE ROLE {other}; CREATE SCHEMA {schema} AUTHORIZATION {owner}; '
            f'CREATE SCHEMA {moved} AUTHORIZATION {owner}; GRANT USAGE ON SCHEMA {schema},{moved} TO {other}')
    upgrade_noop()
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
        value(s, f'CREATE TEXT SEARCH CONFIGURATION {partial}(PARSER=charabia.parser)')
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
        value(s, 'RESET ROLE')
        value(s, 'SET ROLE ' + other)
        error(s, f'ALTER TEXT SEARCH DICTIONARY {renamed}(Stem=false)', 'foreign owner cannot alter dictionary', '42501')
        error(s, f'ALTER TEXT SEARCH DICTIONARY {schema}.vie(English=false,Stem=false)',
              'foreign owner cannot alter English parameters', '42501')
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
