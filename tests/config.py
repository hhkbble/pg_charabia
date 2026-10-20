"""Persistent invoker-owned configurations: catalogs, boundaries and real restore."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from support import EVIDENCE, PSQL, Session, literal, run, save

schema = 'pgcb_config_' + str(os.getpid())
owner = schema + '_owner'
other = schema + '_other'
restored = schema + '_restore'
report = {'checks': [], 'errors': [], 'dump_restore': {}, 'source': 'PG18 SQL object ownership and transaction rules; configuration and cache lifecycle expectations; retained pinned-library observations'}

def checkpoint():
    save('config-results.json', report)

def check(ok, label, evidence=None):
    report['checks'].append({'label': label, 'passed': bool(ok), 'evidence': evidence})
    checkpoint()
    assert ok, (label, evidence)

def info(session, name):
    return json.loads(session.value('SELECT charabia.analysis_info(' + literal(name) + ')'))

def create(session, name, settings):
    return session.value('SELECT charabia.create_config(' + literal(name) + ',' + literal(json.dumps(settings,ensure_ascii=False)) + '::jsonb)::oid')

def alter(session, name, settings):
    return json.loads(session.value('SELECT charabia.alter_config(' + literal(name) + ',' + literal(json.dumps(settings,ensure_ascii=False)) + '::jsonb)'))

def direct(session, name, raw, language='eng'):
    return json.loads(session.value("SELECT to_json(ts_lexize(m.mapdict::regdictionary,"+literal(raw)+")) FROM pg_ts_config_map m JOIN ts_token_type('charabia.parser')t ON t.tokid=m.maptokentype WHERE m.mapcfg="+literal(name)+"::regconfig AND t.alias="+literal(language)))

def error(session, sql, label, state='22023'):
    output=session.query(sql)
    report['errors'].append({'label':label,'sql':sql,'expected_state':state,'output':output});checkpoint()
    check('ERROR:'in output and state in output,label,output)
    check(session.value('SELECT 1')=='1','backend recovers after '+label)

def concurrent_copy(failure_probe=False):
    # An uncommitted standard COPY already holds dictionary dependency locks,
    # but is invisible to the first shared-mapping check. After waiting, ALTER
    # must inspect current catalogs even under a repeatable-read snapshot.
    cases=[('READ COMMITTED','cleanup')] if failure_probe else [('READ COMMITTED','rc'),('REPEATABLE READ','rr')]
    for isolation, suffix in cases:
        original=schema+'.race_'+suffix
        copied=schema+'.race_copy_'+suffix
        with Session() as setup:
            setup.value('SET ROLE '+owner)
            create(setup,original,{'stemming':False,'builtin_stopwords':False})
        with Session() as writer, Session() as copier:
            writer.value('SET ROLE '+owner)
            copier.value('SET ROLE '+owner)
            writer.value('BEGIN ISOLATION LEVEL '+isolation)
            before=info(writer,original)
            copier.value('BEGIN;CREATE TEXT SEARCH CONFIGURATION '+copied+'(COPY='+original+')')
            outcome=[]
            def mutate():
                try:
                    outcome.append(writer.query('SELECT charabia.alter_config('+literal(original)+",'{}')",timeout=30))
                except BaseException as exc:
                    outcome.append(exc)
            worker=threading.Thread(target=mutate,name='pgcb-config-copy-'+suffix)
            worker.start()
            observation={'isolation':isolation,'writer_pid':writer.pid,'copier_pid':copier.pid,
                         'before':before['settings'],'blocked':False}
            report.setdefault('concurrent_copy',[]).append(observation);checkpoint()
            try:
                deadline=time.monotonic()+15
                while worker.is_alive() and time.monotonic()<deadline:
                    if run(f'SELECT {copier.pid}=ANY(pg_blocking_pids({writer.pid}))')=='t':
                        observation['blocked']=True;break
                    time.sleep(0.02)
                if failure_probe:
                    check(observation['blocked'],'cleanup probe reached a blocked writer',observation)
                    check(run(f'SELECT pg_terminate_backend({copier.pid})')=='t','terminate only the owned blocker')
                    raise AssertionError('injected config observer failure')
                copier.value('COMMIT')
                worker.join(timeout=20)
                observation['outcome']=[str(item) for item in outcome]
                if not worker.is_alive() and outcome and isinstance(outcome[0],str) and 'ERROR:'not in outcome[0]:
                    writer.value('COMMIT')
                    observation['committed_original']=info(writer,original)['settings']
                    observation['committed_copy']=info(writer,copied)['settings']
                checkpoint()
                check(observation['blocked'],'ALTER actually waits for standard COPY dictionary locks',observation)
                check(not worker.is_alive() and len(outcome)==1 and isinstance(outcome[0],str),'waiting ALTER exits',repr(outcome))
                observation['output']=outcome[0];checkpoint()
                check('ERROR:'in outcome[0] and '22023'in outcome[0] and 'shares dictionaries'in outcome[0],
                      'post-wait shared configuration is rejected',observation)
                writer.value('ROLLBACK')
                check(info(writer,original)['settings']==before['settings'] and
                      info(writer,copied)['settings']==before['settings'],
                      'rejected ALTER preserves both original and copied settings',observation)
                check(direct(writer,original,'running')==['running'] and direct(writer,copied,'running')==['running'],
                      'neither shared dictionary acquired new stemming settings')
            finally:
                # Release the blocker first, then cancel and join the exact
                # owned writer. Never leave a test thread using a closed Session.
                primary=sys.exception()
                cleanup_errors=[]
                try:
                    copier.query('ROLLBACK')
                except BaseException as exc:
                    cleanup_errors.append('blocker rollback: '+repr(exc))
                if worker.is_alive():
                    try:
                        writer.cancel()
                    except BaseException as exc:
                        cleanup_errors.append('writer cancel: '+repr(exc))
                worker.join(timeout=20)
                if worker.is_alive():
                    try:
                        run(f'SELECT pg_terminate_backend({writer.pid})',timeout=10)
                    except BaseException as exc:
                        cleanup_errors.append('writer terminate: '+repr(exc))
                    finally:
                        worker.join(timeout=10)
                if worker.is_alive():
                    try:
                        writer.process.kill()
                    except BaseException as exc:
                        cleanup_errors.append('writer process kill: '+repr(exc))
                    finally:
                        worker.join(timeout=5)
                if not worker.is_alive():
                    try:
                        writer.query('ROLLBACK')
                    except BaseException as exc:
                        cleanup_errors.append('writer rollback: '+repr(exc))
                observation['thread_exited']=not worker.is_alive()
                observation['cleanup_errors']=cleanup_errors;checkpoint()
                if worker.is_alive() or cleanup_errors:
                    message=f'configuration cleanup: alive={worker.is_alive()}, errors={cleanup_errors}'
                    if primary is None:
                        raise AssertionError(message)
                    primary.add_note(message)
        run('DROP TEXT SEARCH CONFIGURATION '+copied)
        run('SELECT charabia.drop_config('+literal(original)+')')

try:
    run(f'CREATE ROLE {owner};CREATE ROLE {other};CREATE SCHEMA {schema} AUTHORIZATION {owner}')
    concurrent_copy()
    try:
        concurrent_copy(failure_probe=True)
    except AssertionError as exc:
        check(str(exc)=='injected config observer failure','cleanup preserves the exact primary failure',repr(exc))
        probe=report['concurrent_copy'][-1]
        probe['primary_notes']=getattr(exc,'__notes__',[])
        check(probe['thread_exited'] and any(e.startswith('blocker rollback:')for e in probe['cleanup_errors']),
              'broken blocker connection cannot bypass writer join',probe)
    else:
        raise AssertionError('configuration cleanup failure probe did not execute')
    with Session() as s:
        # Default extension members cannot accumulate user settings lost by dump.
        error(s,"SELECT charabia.alter_config('public.charabia','{}')",'default is fixed')
        error(s,"SELECT charabia.drop_config('public.charabia')",'default cannot be dropped by helper')
        error(s,"SELECT charabia.create_config('unqualified','{}')",'qualified name required')
        s.value('SET ROLE '+owner)
        a=schema+'.a'; b=schema+'.b'
        create(s,a,{})
        b_oid=int(create(s,b,{'stemming':False,'builtin_stopwords':False}))
        check(info(s,a)['route_count']==71 and info(s,a)['token_type_count']==72,'complete finite mappings')
        check(direct(s,a,'running')==['running','run'] and direct(s,b,'running')==['running'],'independent stemming settings')
        check(direct(s,a,'THE')==[] and direct(s,b,'THE')==['the'],'independent builtin settings')
        for stem,builtin in [(True,True),(True,False),(False,True),(False,False)]:
            alter(s,a,{'stemming':stem,'builtin_stopwords':builtin})
            check(direct(s,a,'running')==(['running','run']if stem else ['running']),'stemming flag does not alter source key',{'stemming':stem,'builtin':builtin})
            check(direct(s,a,'THE')==([]if builtin else ['the']),'builtin flag does not alter stemming',{'stemming':stem,'builtin':builtin})
        alter(s,a,{'builtin_stopwords':False,'stopwords_add':{'eng':['RUNNING']}})
        check(direct(s,a,'running')==[],'custom add works when builtin disabled')
        alter(s,a,{'builtin_stopwords':False,'stopwords_add':{'eng':['RUNNING']},'stopwords_keep':{'eng':['running']}})
        check(direct(s,a,'RUNNING')==['running','run'],'canonical keep overrides add without disabling stem')
        alter(s,a,{'stemming':False,'stopwords_keep':{'eng':['the']}})
        check(direct(s,a,'THE')==['the'],'keep overrides builtin')
        replaced=alter(s,a,{'builtin_stopwords':False})
        check(replaced['settings']['stemming']is True and replaced['settings']['stopwords_keep']=={},'alter is complete replacement, not patch')
        check(replaced['maintenance_required']is True,'alter returns explicit maintenance requirement')
        before=info(s,a)
        s.value('BEGIN');alter(s,a,{'stemming':False});s.value('ROLLBACK')
        check(info(s,a)['settings']==before['settings'],'rollback restores all dictionary settings')
        create(s,schema+'.empty',{'stopwords_add':{'eng':[]}})
        check(info(s,schema+'.empty')['settings']['stopwords_add']=={},'empty language list has a canonical recoverable identity')
        s.value(f'CREATE TEXT SEARCH CONFIGURATION {schema}.copy(COPY={a})')
        check(info(s,schema+'.copy')['settings']==info(s,a)['settings'],'COPY may read shared mappings')
        error(s,"SELECT charabia.alter_config("+literal(schema+'.copy')+",'{}')",'COPY cannot mutate shared dictionaries')
        error(s,"SELECT charabia.drop_config("+literal(schema+'.copy')+")",'COPY cannot delete shared dictionaries')
        s.value(f'DROP TEXT SEARCH CONFIGURATION {schema}.copy')
        # Invalid initialization/settings must not leave partial catalogs.
        invalid=[{'missing':True},{'stemming':0},{'builtin_stopwords':'yes'},
                 {'stopwords_add':{'en':['word']}},{'stopwords_keep':{'display_only':['x']}},
                 {'stopwords_add':{'eng':['']}},{'stopwords_keep':{'eng':['two words']}},
                 {'stopwords_add':{'eng':['\x01']}},{'stopwords_add':{'eng':['x'*2047]}},
                 {'stopwords_add':{'eng':['x']*65537}},
                 {'stopwords_add':{'eng':['x'*2046]*600}}]
        for n,options in enumerate(invalid):
            before_count=s.value("SELECT count(*) FROM pg_ts_dict WHERE dictnamespace="+literal(schema)+"::regnamespace")
            output=s.query('SELECT charabia.create_config('+literal(schema+'.bad'+str(n))+','+literal(json.dumps(options))+'::jsonb)')
            check('ERROR:'in output,'invalid settings fail',{'options_case':n,'output':output})
            check(s.value("SELECT count(*) FROM pg_ts_dict WHERE dictnamespace="+literal(schema)+"::regnamespace")==before_count,'failed create leaves no dictionaries',n)
        s.value('BEGIN')
        s.value(f'ALTER TEXT SEARCH CONFIGURATION {a} ALTER MAPPING FOR eng WITH pg_catalog.simple')
        output=s.query('SELECT charabia.analysis_info('+literal(a)+')')
        check('ERROR:'in output and '22023'in output,'diagnostics reject foreign or incomplete maps',output)
        s.value('ROLLBACK')
        # Names go through PG parse_ident; quoted dots/quotes/Unicode are not split.
        unusual=f'{schema}."Mixed.""配置"'
        create(s,unusual,{'stemming':False})
        check(info(s,unusual)['settings']['stemming']is False,'quoted qualified identifiers roundtrip')
        settings={'stemming':False,'builtin_stopwords':False,'stopwords_add':{'eng':['quote"slash\\词']},'stopwords_keep':{'fra':['ÉTÉ']}}
        alter(s,a,settings)
        expected=info(s,a); old_oid=expected['configuration_oid']
        s.value('RESET ROLE')
        s.value('SET ROLE '+other)
        error(s,'SELECT charabia.alter_config('+literal(a)+",'{}')",'foreign owner cannot alter','42501')
        error(s,'SELECT charabia.create_config('+literal(schema+'.forbidden')+",'{}')",'schema CREATE permission enforced','42501')
        error(s,'CREATE FUNCTION charabia.untrusted()RETURNS int LANGUAGE sql AS $$SELECT 1$$','internal schema is protected','42501')
        s.value('RESET ROLE')
        # Real pg_dump/pg_restore, with existing cluster roles and a clean DB.
        archive=EVIDENCE/'config-dump.custom'
        dump=[str(Path(PSQL).parent/'pg_dump'),'-Fc','-n',schema,'-f',str(archive)]
        result=subprocess.run(dump,text=True,capture_output=True)
        report['dump_restore']['dump']={'argv':dump,'exit':result.returncode,'stderr':result.stderr};checkpoint()
        check(result.returncode==0,'pg_dump persistent configuration succeeds',result.stderr)
        s.value('CREATE DATABASE '+restored+' TEMPLATE template0')
        run('CREATE EXTENSION pg_charabia',restored)
        restore=[str(Path(PSQL).parent/'pg_restore'),'--exit-on-error','-d',restored,str(archive)]
        result=subprocess.run(restore,text=True,capture_output=True)
        report['dump_restore']['restore']={'argv':restore,'exit':result.returncode,'stderr':result.stderr};checkpoint()
        check(result.returncode==0,'pg_restore persistent configuration succeeds',result.stderr)
        with Session(restored) as target:
            target.value('SET ROLE '+owner)
            after=info(target,a)
            report['dump_restore'].update({'old_oid':old_oid,'new_oid':after['configuration_oid'],'settings':after['settings']})
            check(after['settings']==expected['settings'] and after['settings_fingerprint']==expected['settings_fingerprint'],'dump/restore preserves raw quoting, Unicode and settings identity',after)
            check(after['configuration_oid']!=old_oid,'restore actually uses a different configuration OID',report['dump_restore'])
            alter(target,a,{})
            check(direct(target,a,'running')==['running','run'],'restored instance can be changed without old OID/name assumptions')
            target.value('SELECT charabia.drop_config('+literal(a)+')')
            check(target.value("SELECT count(*) FROM pg_ts_config WHERE cfgnamespace="+literal(schema)+"::regnamespace AND cfgname='a'")=='0','restored instance can be dropped')
        s.value('DROP DATABASE '+restored)
        s.value('SET ROLE '+owner)
        s.value(f'CREATE TABLE {schema}.dependent(body text);CREATE INDEX dependent_gin ON {schema}.dependent USING gin(to_tsvector({literal(b)},body))')
        error(s,'SELECT charabia.drop_config('+literal(b)+')','native index dependency is RESTRICT','2BP01')
        check(info(s,b)['route_count']==71,'failed drop rolls back configuration deletion')
        s.value(f'DROP TABLE {schema}.dependent')
        s.value('SELECT charabia.drop_config('+literal(b)+')')
        check(s.value("SELECT count(*) FROM pg_ts_dict WHERE dictnamespace="+literal(schema)+"::regnamespace AND dictname LIKE "+literal('_pgcb_'+str(b_oid)+'_%'))=='0','drop removes every instance-owned dictionary')
        s.value('RESET ROLE')
    report['status']='passed'
except BaseException as exc:
    report['status']='failed';report['error']=repr(exc);raise
finally:
    checkpoint()
    cleanup=[]
    for statement in ['DROP DATABASE IF EXISTS '+restored,'DROP SCHEMA IF EXISTS '+schema+' CASCADE','DROP ROLE IF EXISTS '+owner,'DROP ROLE IF EXISTS '+other]:
        try: run(statement)
        except BaseException as exc: cleanup.append({'sql':statement,'error':repr(exc)})
    report['cleanup_errors']=cleanup;checkpoint()
    if cleanup and report.get('status')=='passed':raise AssertionError(cleanup)
