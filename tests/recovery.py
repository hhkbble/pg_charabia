#!/usr/bin/env python3
"""Recover the actual staged release through online backup and archive WAL."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from support import Session

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ['PGCB_RUN_DIR']) / 'recovery'
OUT.mkdir(exist_ok=True)
RUN = os.environ.get('PGCB_RUN_ID', OUT.parent.name)
TMP = Path(tempfile.mkdtemp(prefix='recovery-',dir=os.environ['PGCB_TEMP_DIR']))
(TMP/'.owner').write_text(RUN)
ENV = {k:v for k,v in os.environ.items() if not k.startswith('PG') or k == 'PG_CONFIG'}
BIN = Path(os.environ['PGCB_PG_BIN'])
ENV['PATH'] = str(BIN)+':'+ENV.get('PATH','')
REPORT = {'format':2,'mode':'release-physical-PITR','run':RUN,'temporary':str(TMP),'commands':[],'checks':[],'status':'running'}
STARTED = set()

def save():
    (OUT/'results.json').write_text(json.dumps(REPORT,ensure_ascii=False,indent=2)+'\n')

def command(argv,label,check=True,timeout=120):
    argv=list(map(str,argv)); start=time.monotonic()
    entry={'argv':argv,'label':label,'exit':'running'}
    REPORT['commands'].append(entry);save()
    try:
        result=subprocess.run(argv,env=ENV,text=True,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired as error:
        def text(value):return value.decode(errors='replace')if isinstance(value,bytes)else value or ''
        (OUT/(label+'.stdout')).write_text(text(error.stdout))
        (OUT/(label+'.stderr')).write_text(text(error.stderr))
        entry.update(exit='timeout',seconds=time.monotonic()-start,timeout_seconds=timeout);save()
        raise
    except BaseException as error:
        entry.update(exit='interrupted'if isinstance(error,InterruptedError)else'exception',error=repr(error),seconds=time.monotonic()-start);save()
        raise
    (OUT/(label+'.stdout')).write_text(result.stdout)
    (OUT/(label+'.stderr')).write_text(result.stderr)
    entry.update(exit=result.returncode,seconds=time.monotonic()-start);save()
    if check and result.returncode:raise RuntimeError(f'{label}: {result.returncode}: {result.stderr[-1600:]}')
    return result.stdout.strip()

def sql(text,label,database='postgres'):
    (OUT/(label+'.sql')).write_text(text+'\n')
    return command([BIN/'psql','-XAtq','-v','ON_ERROR_STOP=1','-d',database,'-c',text],label)

def check(ok,label,evidence=None):
    REPORT['checks'].append({'label':label,'ok':bool(ok),'evidence':evidence});save()
    if not ok:raise AssertionError((label,evidence))

def start(path,port):
    STARTED.add(path)
    command([BIN/'pg_ctl','-D',path,'-l',path.parent/(path.name+'.log'),'-w','-t','60','start'],path.name+'-start')
    ENV.update(PGHOST=str(TMP/'socket'),PGPORT=str(port),PGUSER='postgres',PGDATABASE='postgres')

def stop(path,mode='fast'):
    command([BIN/'pg_ctl','-D',path,'-m',mode,'-w','-t','60','stop'],path.name+'-stop-'+mode)
    STARTED.remove(path)

def nodes(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from nodes(child)
    elif isinstance(value,list):
        for child in value:yield from nodes(child)

def observe(label,expected):
    heap=json.loads(sql('SELECT jsonb_agg(id ORDER BY id) FROM lab.docs',label+'-heap'))
    check(heap==expected,label+' heap at target',heap)
    snapshot=json.loads(sql("SELECT jsonb_build_object('options',(SELECT dictinitoption FROM pg_ts_dict WHERE oid='lab.eng_stem'::regdictionary),'maps',(SELECT jsonb_agg(d.dictname ORDER BY m.mapseqno)FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid=m.mapdict JOIN ts_token_type('charabia.parser')t ON t.tokid=m.maptokentype WHERE m.mapcfg='lab.cfg'::regconfig AND t.alias='eng'),'vector',to_tsvector('lab.cfg',"+literal(ENGLISH)+")::text,'stored_consistent',NOT EXISTS(SELECT FROM lab.docs WHERE v IS DISTINCT FROM to_tsvector('lab.cfg',body)))",label+'-catalog'))
    check('stem = true' in snapshot['options'].lower().replace("'",""),label+' dictionary parameter restored',snapshot)
    check(snapshot['maps']==['eng_filter','eng_stem'],label+' mapping restored',snapshot)
    check("'run':" in snapshot['vector'] and snapshot['stored_consistent'],label+' analysis and stored vectors restored',snapshot)
    headline=sql("SELECT ts_headline('lab.cfg',body,'run'::tsquery,'HighlightAll=true')FROM lab.docs WHERE id=1",label+'-headline')
    check('<b>running</b>'in headline,label+' filter/stem headline retains original',headline)
    REPORT.setdefault('observations',{})[label]={'heap':heap,'catalog':snapshot,'queries':[]};save()
    summary = sql("SELECT bm25_summarize_index('lab.docs_bm25')",label+'-bm25-durable-state')
    durable = {key:int(re.search(r"^  "+key+r": (\d+)$",summary,re.M).group(1)) for key in ('total_docs','total_len','docs_persisted','len_persisted')}
    durable['memtable_documents'] = int(re.search(r"Memtable:.*?documents: (\d+)",summary,re.S).group(1))
    durable['segments'] = re.findall(r"^  L\d+ Segment .*",summary,re.M)
    REPORT.setdefault('durable',{})[label]=durable
    check(durable['memtable_documents']>0 and bool(durable['segments']),label+' retains both unspilled and segment paths',durable)
    if label=='restored':check(durable==REPORT['durable']['target'],'restored durable statistics and segments match target',durable)
    queries=[('gin',"SELECT id FROM lab.docs WHERE to_tsvector('lab.cfg',body) @@ 'run'::tsquery",'docs_gin',[1,3,5]),
             ('gin_stored',"SELECT id FROM lab.docs WHERE v @@ 'run'::tsquery",'docs_stored_gin',[1,3,5]),
             ('bm25',"SELECT id FROM lab.docs ORDER BY body <@> to_bm25query('run','lab.docs_bm25') LIMIT 20",'docs_bm25',[1,3,5]),
             ('cjk',"SELECT id FROM lab.docs ORDER BY body <@> to_bm25query('PostgreSQL','lab.docs_bm25') LIMIT 20",'docs_bm25',[2])]
    for kind,query,index,want in queries:
        settings='SET enable_seqscan=off;SET enable_indexscan='+('off'if kind.startswith('gin')else'on')+';SET enable_bitmapscan=on;'
        plan=json.loads(sql(settings+'EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) '+query,label+'-'+kind+'-plan'))
        check(any(n.get('Index Name')==index and n.get('Actual Loops',0)>0 for n in nodes(plan)),label+' executed '+index,plan)
        actual=json.loads(sql(settings+"SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM("+query+')q',label+'-'+kind+'-ids'))
        check(actual==want,label+' '+kind+' independent IDs',actual)
        REPORT['observations'][label]['queries'].append({'kind':kind,'plan':plan,'ids':actual});save()

def literal(s):return "'"+s.replace("'","''")+"'"

def interrupt(signum,frame):raise InterruptedError(str(signum))

signal.signal(signal.SIGINT,interrupt);signal.signal(signal.SIGTERM,interrupt)
ENGLISH='The children were running through the garden while the teachers were reading books and sharing their happy memories together.'
try:
    package=Path(os.environ['PGCB_EXTENSION_STAGE'])
    stage=TMP/'stage/extension';shutil.copytree(package,stage)
    lib=stage/'pg_charabia.so'
    REPORT['library']={'sha256':hashlib.sha256(lib.read_bytes()).hexdigest(),'path':str(lib)}
    check(REPORT['library']['sha256']==os.environ['PGCB_LIBRARY_SHA256'],'current release library identity')
    REPORT['loaded_library_sha256']=REPORT['library']['sha256']
    command(['file',lib],'library-file');command(['ldd',lib],'library-dependencies')
    REPORT['postgres']=command([BIN/'postgres','--version'],'postgres-version')
    REPORT['system']=command(['uname','-a'],'system')
    (TMP/'socket').mkdir();archive=TMP/'archive';archive.mkdir()
    source=TMP/'source';base=TMP/'base';recovered=TMP/'recovered'
    command([BIN/'initdb','-D',source,'--encoding=UTF8','--locale=C','--auth=trust','--no-instructions'],'initdb')
    settings=["listen_addresses=''",f"unix_socket_directories='{TMP}/socket'","port=55432",f"extension_control_path='{stage.parent}:$system'",f"dynamic_library_path='{stage}:$libdir'","shared_preload_libraries='pg_textsearch'","wal_level=replica","archive_mode=on",f"archive_command='test ! -f {archive}/%f && cp %p {archive}/%f'","max_wal_senders=4","fsync=on","full_page_writes=on","synchronous_commit=on","log_min_messages=notice"]
    with (source/'postgresql.conf').open('a')as h:h.write('\n'+'\n'.join(settings)+'\n')
    start(source,55432)
    probe=os.environ.get('PGCB_CLEANUP_PROBE')
    if probe in ('recovery-sql-error','recovery-sigterm'):
        REPORT['cleanup_probe']={'mode':probe,'point':'owned source started before setup'};save()
        if probe=='recovery-sql-error':sql('SELECT 1/0','controlled-source-sql-error')
        else:os.kill(os.getpid(),signal.SIGTERM)
    durability=json.loads(sql("SELECT jsonb_build_object('fsync',current_setting('fsync'),'full_page_writes',current_setting('full_page_writes'),'synchronous_commit',current_setting('synchronous_commit'),'archive_mode',current_setting('archive_mode'),'wal_level',current_setting('wal_level'))",'durability'))
    check(all(durability[k]=='on' for k in ('fsync','full_page_writes','synchronous_commit','archive_mode')) and durability['wal_level']=='replica','durable archive settings',durability)
    sql("CREATE EXTENSION pg_charabia;CREATE EXTENSION pg_textsearch;CREATE SCHEMA lab;CREATE TEXT SEARCH CONFIGURATION lab.cfg(COPY=public.charabia);CREATE TEXT SEARCH DICTIONARY lab.eng_raw(TEMPLATE=charabia.template,Language='eng',Stem=false,Discard=false);CREATE TEXT SEARCH DICTIONARY lab.eng_stem(TEMPLATE=charabia.template,Language='eng',Stem=false,Discard=false);ALTER TEXT SEARCH CONFIGURATION lab.cfg ALTER MAPPING FOR eng WITH lab.eng_raw;CREATE TEXT SEARCH DICTIONARY lab.eng_filter(TEMPLATE=charabia.template,Language='eng',Discard=false,Stem=false,Normalize=false,Filter=true);CREATE TABLE lab.docs(id int PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144),v tsvector);",'setup')
    sql("INSERT INTO lab.docs VALUES(1,"+literal(ENGLISH)+",NULL),(2,'中文全文检索 PostgreSQL 18 支持 BM25',NULL),(3,'unrelated',NULL),(4,"+literal(ENGLISH)+",NULL);UPDATE lab.docs SET v=to_tsvector('lab.cfg',body);CREATE INDEX docs_gin ON lab.docs USING gin(to_tsvector('lab.cfg',body));CREATE INDEX docs_stored_gin ON lab.docs USING gin(v);CREATE INDEX docs_bm25 ON lab.docs USING bm25(body)WITH(text_config='lab.cfg');CHECKPOINT",'base-data')
    # A real online basebackup; post-backup changes must come from archived WAL.
    command([BIN/'pg_basebackup','-D',base,'--format=plain','--wal-method=stream','--checkpoint=fast','--no-password'],'basebackup')
    command([BIN/'pg_verifybackup',base],'verify-basebackup')
    REPORT['backup_has_extension_so']=any(p.name=='pg_charabia.so'for p in base.rglob('*'))
    check(not REPORT['backup_has_extension_so'],'basebackup does not include staged extension files')
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.eng_stem(Stem=true);ALTER TEXT SEARCH CONFIGURATION lab.cfg ALTER MAPPING FOR eng WITH lab.eng_filter,lab.eng_stem;UPDATE lab.docs SET v=to_tsvector('lab.cfg',body);REINDEX INDEX lab.docs_gin;REINDEX INDEX lab.docs_stored_gin;REINDEX INDEX lab.docs_bm25;COMMIT",'target-config')
    sql("BEGIN;INSERT INTO lab.docs VALUES(5,"+literal(ENGLISH)+",to_tsvector('lab.cfg',"+literal(ENGLISH)+"));UPDATE lab.docs SET body="+literal(ENGLISH)+",v=to_tsvector('lab.cfg',"+literal(ENGLISH)+")WHERE id=3;DELETE FROM lab.docs WHERE id=4;COMMIT",'target-dml')
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.eng_stem(Stem=false);ROLLBACK",'rolled-back-ddl')
    observe('target',[1,2,3,5])
    target='pgcb_recovery_'+RUN.replace('-','_')
    lsn=sql('SELECT pg_create_restore_point('+literal(target)+')','restore-point')
    REPORT['target']={'name':target,'lsn':lsn,'timeline':int(sql("SELECT substring(pg_walfile_name(pg_current_wal_lsn()) for 8)",'source-timeline'),16),'kind':'named WAL restore point after target commits and rollback'};save()
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.eng_stem(Stem=false);ALTER TEXT SEARCH CONFIGURATION lab.cfg ALTER MAPPING FOR eng WITH lab.eng_raw;DELETE FROM lab.docs WHERE id=1;INSERT INTO lab.docs VALUES(99,'after target',to_tsvector('lab.cfg','after target'));COMMIT",'after-target')
    walfile=sql("SELECT pg_walfile_name(pg_switch_wal())",'wal-switch')
    deadline=time.monotonic()+30
    while not(archive/walfile).is_file()and time.monotonic()<deadline:time.sleep(.1)
    check((archive/walfile).is_file(),'target WAL segment actually archived',walfile)
    REPORT['archive']=[{'name':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}for p in sorted(archive.iterdir())];save()
    command([BIN/'pg_waldump','-p',archive,'-r','XLOG',walfile],'target-waldump')
    stop(source,'immediate')
    start(source,55432)
    # Crash recovery must retain committed later state and discard the rollback.
    check(sql('SELECT string_agg(id::text,\',\' ORDER BY id)FROM lab.docs','crash-heap')=='2,3,5,99','crash recovery retains committed post-target state')
    check('stem = false'in sql("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lab.eng_stem'::regdictionary",'crash-dictionary').lower().replace("'",""),'crash recovery dictionary parameter')
    stop(source)
    shutil.copytree(base,recovered)
    (recovered/'recovery.signal').touch()
    with (recovered/'postgresql.auto.conf').open('a')as h:
        h.write(f"\nport=55433\narchive_mode=off\nrestore_command='cp {archive}/%f %p'\nrecovery_target_name='{target}'\nrecovery_target_action='promote'\n")
    start(recovered,55433)
    deadline=time.monotonic()+30
    while sql('SELECT pg_is_in_recovery()','recovery-wait')!='f':
        if time.monotonic()>deadline:raise TimeoutError('named target did not promote')
        time.sleep(.1)
    held=lib.with_suffix('.held')
    lib.rename(held)
    try:
        missing=command([BIN/'psql','-XAtq','-v','ON_ERROR_STOP=1','-c',"SELECT to_tsvector('lab.cfg',"+literal(ENGLISH)+")"],'missing-external-library',check=False)
        failure=(OUT/'missing-external-library.stderr').read_text()
        check('could not access file' in failure and 'pg_charabia' in failure,'restored catalog does not replace a missing external library',failure)
    finally:
        held.rename(lib)
    observe('restored',[1,2,3,5])
    check(sql('SELECT pg_is_in_recovery()','restored-promoted')=='f','named recovery target promoted')
    REPORT['replay_lsn']=sql('SELECT pg_last_wal_replay_lsn()','restored-lsn')
    check(REPORT['replay_lsn']==lsn,'replay stopped at exact named target LSN',REPORT['replay_lsn'])
    REPORT['restored_timeline']=int(sql("SELECT substring(pg_walfile_name(pg_current_wal_lsn()) for 8)",'restored-timeline'),16)
    check(REPORT['restored_timeline']>REPORT['target']['timeline'],'promotion starts a new timeline',REPORT['restored_timeline'])
    sql("SELECT charabia.analysis_info();SELECT version();SHOW shared_preload_libraries",'restored-identity')
    # Persistent backend mapping proves which binary performs restored analysis.
    with Session(env=ENV) as backend:
        backend.value("LOAD 'pg_charabia'")
        maps=Path(f'/proc/{backend.pid}/maps').read_text()
        (OUT/'restored-backend-maps.txt').write_text(maps)
        check(str(lib)in maps,'restored backend maps exact staged release library',backend.pid)
    stop(recovered)
    REPORT['status']='passed'
except BaseException as e:
    REPORT.update(status='failed',error=repr(e));raise
finally:
    signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN)
    cleanup=[]
    for path in list(STARTED):
        try:stop(path)
        except BaseException as e:cleanup.append(repr(e))
    for path in TMP.glob('*.log'):shutil.copy2(path,OUT/path.name)
    can_remove=not STARTED and(TMP/'.owner').read_text()==RUN
    if can_remove:shutil.rmtree(TMP)
    REPORT['cleanup']={'marker_matches':can_remove,'clusters_stopped':not STARTED,'directory_removed':not TMP.exists(),'errors':cleanup};save()
    if cleanup:raise RuntimeError(cleanup)
