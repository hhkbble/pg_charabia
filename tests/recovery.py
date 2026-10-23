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
STRUCTURED_TOKENS = {
    'url': (1, 'https://example.com/running?q=100ms&item=x', 'run'),
    'host': (2, 'worker.example.net', 'worker'),
    'email': (3, 'User+tag@example.org', 'tag'),
    'file': (4, '/srv/WalkingTests.rs', 'walk'),
    'numword': (5, '250ms', '250'),
}
REPORT['structured_expectation_basis'] = {
    'literal': 'Fixed ASCII source spelling, raw scope, and the selected Kind; not extension-generated expected values',
    'components': 'Existing English Snowball running -> run / walking -> walk, literal numeric parts, and independently fixed document IDs',
    'recovery': 'Committed target parameters, ordered dictionary chains, maintained vectors/indexes, and the named WAL target',
}

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

def chinese_queries():
    return [('gin_chinese',"SELECT id FROM lab.docs WHERE to_tsvector('lab.cfg',body) @@ plainto_tsquery('lab.cfg','全文')",'docs_gin',[2]),
            ('gin_stored_chinese',"SELECT id FROM lab.docs WHERE v @@ plainto_tsquery('lab.cfg','全文')",'docs_stored_gin',[2]),
            ('bm25_chinese',"SELECT id FROM lab.docs ORDER BY body <@> to_bm25query('全文','lab.docs_bm25') LIMIT 20",'docs_bm25',[2])]

def observe_queries(label,queries):
    for kind,query,index,want in queries:
        settings='SET enable_seqscan=off;SET enable_indexscan='+('off'if kind.startswith('gin')else'on')+';SET enable_bitmapscan=on;'
        plan=json.loads(sql(settings+'EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) '+query,label+'-'+kind+'-plan'))
        node_type='Bitmap Index Scan'if kind.startswith('gin')else'Index Scan'
        check(any(n.get('Index Name')==index and n.get('Node Type')==node_type and n.get('Actual Loops',0)>0 for n in nodes(plan)),label+' executed '+index,plan)
        actual=json.loads(sql(settings+"SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM("+query+')q',label+'-'+kind+'-ids'))
        check(actual==want,label+' '+kind+' independent IDs',actual)
        REPORT['observations'][label]['queries'].append({'kind':kind,'plan':plan,'ids':actual});save()

def observe_chinese_headline(label):
    headline=sql("SELECT ts_headline('lab.cfg',body,plainto_tsquery('lab.cfg','全文'),'HighlightAll=true')FROM lab.docs WHERE id=2",label+'-chinese-headline')
    check('<b>全文</b>'in headline and headline.replace('<b>','').replace('</b>','')=='中文全文检索 PostgreSQL 18 支持 BM25',
          label+' Chinese term highlights the original source',headline)

def observe_round4(label):
    terms=['abbrevıate','abbrevıa','abbreviate','abbrevi']
    snapshot=json.loads(sql("SELECT jsonb_build_object('options',(SELECT dictinitoption FROM pg_ts_dict WHERE oid='lab.parallel'::regdictionary),'direct',ts_lexize('lab.parallel','ABBREVIATE'),'und',ts_lexize('lab.und_words','12345'),'und_dictionary',(SELECT mapdict::regdictionary::text FROM pg_ts_config_map WHERE mapcfg='lab.variant_cfg'::regconfig AND maptokentype=16),'positions',(SELECT jsonb_object_agg(lexeme,positions) FROM unnest(to_tsvector('lab.variant_cfg','ABBREVIATE'))),'types',(SELECT jsonb_agg(alias ORDER BY tokid)FROM ts_token_type('charabia.parser')))",label+'-round4-catalog'))
    normalized=snapshot['options'].lower().replace("'",'').replace('"','')
    check('english = true'in normalized and 'stem = true'in normalized and 'normalize = false'in normalized,
          label+' restores the committed parallel English parameters',snapshot)
    check(snapshot['und_dictionary']=='lab.und_words',label+' restores the configured und mapping',snapshot)
    check(snapshot['direct']==terms and snapshot['positions']=={term:[1]for term in terms},
          label+' rebuilds four same-position alternatives after cold INIT',snapshot)
    check(snapshot['und']==['12345'] and snapshot['types']==['eng','deu','nld','rus','spa','fra','por','ita','ara','tur','cmn','jpn','kor','vie','tha','und','blank','display_only','url','host','email','file','numword'],
          label+' restores the exact route catalog and immutable und behavior',snapshot)
    check(sql("SELECT bool_and(v IS NOT DISTINCT FROM to_tsvector('lab.variant_cfg',body))FROM lab.variant_docs",label+'-round4-stored')=='t',
          label+' restored variant stored vectors match target analysis')
    original='ABBREVIATE & <tag>'
    headline=sql("SELECT ts_headline('lab.variant_cfg',"+literal(original)+",'abbrevıate | abbrevıa | abbreviate | abbrevi'::tsquery,'HighlightAll=true')",label+'-round4-headline')
    check(headline=='<b>ABBREVIATE</b> &amp; &lt;tag&gt;',
          label+' restored alternatives highlight the original once with escaping',headline)
    merge_rows=json.loads(sql("SELECT jsonb_agg(to_jsonb(a))FROM charabia.analyze('PostgreSQL')a",label+'-round4-merge-spans'))
    check(len(merge_rows)==1 and merge_rows[0]['raw']=='PostgreSQL' and merge_rows[0]['byte_start']==0 and merge_rows[0]['byte_end']==10,
          label+' complete English word retains its exact original span',merge_rows)
    check(sql("SELECT ts_headline('lab.variant_cfg','left PostgreSQL right','postgresql:*'::tsquery,'HighlightAll=true')",label+'-round4-merge-headline')=='left <b>PostgreSQL</b> right',
          label+' merged whole-word prefix highlights original text')
    # Direct und Dictionary behavior above is unchanged; the Parser now owns numbers as structured_tokens.
    und_rows=json.loads(sql("SELECT jsonb_agg(to_jsonb(a))FROM charabia.analyze('12345')a",label+'-round4-und-span'))
    check(len(und_rows)==1 and und_rows[0]['raw']=='12345' and und_rows[0]['language']is None and
          und_rows[0]['kind']=='numword' and und_rows[0]['token_type']=='numword',
          label+' numeric Parser input has one literal structured token and no fabricated language',und_rows)
    REPORT['observations'][label]['round4']={'catalog':snapshot,'merge_spans':merge_rows,'und_spans':und_rows,'headline':headline};save()
    queries=[('gin_variant_'+str(i),"SELECT id FROM lab.variant_docs WHERE to_tsvector('lab.variant_cfg',body) @@ "+literal(term)+"::tsquery",'variant_gin',[1])for i,term in enumerate(terms)]
    queries += [('gin_stored_variant',"SELECT id FROM lab.variant_docs WHERE v @@ 'abbrevıa'::tsquery",'variant_stored_gin',[1]),
                ('bm25_variant',"SELECT id FROM lab.variant_docs ORDER BY body <@> to_bm25query('ABBREVIATE','lab.variant_bm25')LIMIT 20",'variant_bm25',[1]),
                ('gin_merged',"SELECT id FROM lab.variant_docs WHERE to_tsvector('lab.variant_cfg',body) @@ 'postgresql'::tsquery",'variant_gin',[2]),
                ('bm25_merged',"SELECT id FROM lab.variant_docs ORDER BY body <@> to_bm25query('PostgreSQL','lab.variant_bm25')LIMIT 20",'variant_bm25',[2]),
                ('gin_und',"SELECT id FROM lab.variant_docs WHERE to_tsvector('lab.variant_cfg',body) @@ '12345'::tsquery",'variant_gin',[3]),
                ('bm25_und',"SELECT id FROM lab.variant_docs ORDER BY body <@> to_bm25query('12345','lab.variant_bm25')LIMIT 20",'variant_bm25',[3])]
    observe_queries(label,queries)

def observe_structured_tokens(label,target_state=True):
    expected_ids=[1,2,3,4,5,6,7]if target_state else[2,3,4,5,6,7,99]
    snapshot=json.loads(sql("SELECT jsonb_build_object('heap',(SELECT jsonb_agg(id ORDER BY id)FROM lab.structured_docs),'options',(SELECT jsonb_object_agg(k.kind,d.dictinitoption)FROM unnest(ARRAY['url','host','email','file','numword'])k(kind)JOIN pg_ts_dict d ON d.oid=('lab.structured_'||k.kind)::regdictionary),'maps',(SELECT jsonb_object_agg(t.alias,(SELECT jsonb_agg(m.mapdict::regdictionary::text ORDER BY m.mapseqno)FROM pg_ts_config_map m WHERE m.mapcfg='lab.structured_cfg'::regconfig AND m.maptokentype=t.tokid))FROM ts_token_type('charabia.parser')t WHERE t.alias IN('url','host','email','file','numword')),'stored_consistent',NOT EXISTS(SELECT FROM lab.structured_docs WHERE v IS DISTINCT FROM to_tsvector('lab.structured_cfg',body)))",label+'-structured_tokens-catalog'))
    check(snapshot['heap']==expected_ids,label+' structured tokens contain the independently fixed committed rows',snapshot)
    check(snapshot['stored_consistent'],label+' structured stored vectors reflect the maintained target configuration',snapshot)
    for kind in STRUCTURED_TOKENS:
        options=snapshot['options'][kind].lower().replace("'",'').replace('"','')
        boolean='true'if target_state else'false'
        check(all(re.search(r'\b'+name+r'\s*=\s*'+boolean+r'\b',options)for name in('discard','stem','normalize')) and
              re.search(r'\bkind\s*=\s*'+kind+r'\b',options),label+' '+kind+' restores native structured parameters',options)
        want=['lab.eng_filter','lab.structured_url']if kind=='url'and target_state else(
             ['simple']if kind=='url'else['lab.structured_'+kind])
        check(snapshot['maps'][kind]==want,label+' '+kind+' restores the actual ordered dictionary chain',snapshot['maps'][kind])
    REPORT['observations'][label]['structured_tokens']={'catalog':snapshot};save()

    summary=sql("SELECT bm25_summarize_index('lab.structured_bm25')",label+'-structured_tokens-durable-state')
    durable={key:int(re.search(r'^  '+key+r': (\d+)$',summary,re.M).group(1))for key in('total_docs','total_len','docs_persisted','len_persisted')}
    durable['memtable_documents']=int(re.search(r'Memtable:.*?documents: (\d+)',summary,re.S).group(1))
    durable['segments']=re.findall(r'^  L\d+ Segment .*',summary,re.M)
    check(durable['memtable_documents']>0 and bool(durable['segments']),label+' structured BM25 retains disk and unspilled paths',durable)
    REPORT['observations'][label]['structured_tokens']['durable']=durable;save()
    if label=='restored':
        check(durable==REPORT['observations']['target']['structured_tokens']['durable'],
              'restored structured BM25 durable summary equals the actual target summary',durable)

    if target_state:
        queries=[]
        for kind,(identifier,raw,term)in STRUCTURED_TOKENS.items():
            direct=json.loads(sql('SELECT to_jsonb(ts_lexize('+literal('lab.structured_'+kind)+','+literal(raw)+'))',label+'-structured-direct-'+kind))
            full=raw if kind!='url'else'https://example.com/running?q=100ms&item=x'
            check(full in direct and term in direct,label+' '+kind+' cold INIT retains full and required component',direct)
            vector=json.loads(sql("SELECT jsonb_object_agg(lexeme,positions)FROM unnest(to_tsvector('lab.structured_cfg',"+literal(raw)+'))',label+'-structured-positions-'+kind))
            check(full in vector and term in vector and all(positions==[1]for positions in vector.values()),
                  label+' '+kind+' restored alternatives occupy exactly one position',vector)
            headline=json.loads(sql("SELECT to_jsonb(ts_headline('lab.structured_cfg',"+literal(raw)+','+literal(term)+"::tsquery,'HighlightAll=true'))",label+'-structured-headline-'+kind))
            escaped=raw.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;').replace("'",'&#39;')
            check(headline=='<b>'+escaped+'</b>',label+' '+kind+' highlights the whole original structured token once',headline)
            REPORT['observations'][label]['structured_tokens'][kind]={'direct':direct,'positions':vector,'headline':headline};save()
            queries += [
                ('gin_structured_'+kind,"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ "+literal(term)+"::tsquery",'structured_gin',[identifier]),
                ('gin_stored_structured_'+kind,"SELECT id FROM lab.structured_docs WHERE v @@ "+literal(term)+"::tsquery",'structured_stored_gin',[identifier]),
                ('bm25_structured_'+kind,"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query("+literal(term)+",'lab.structured_bm25')LIMIT 20",'structured_bm25',[identifier]),
                ('gin_structured_full_'+kind,"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ plainto_tsquery('lab.structured_cfg',"+literal(raw)+")",'structured_gin',[identifier]),
                ('gin_stored_structured_full_'+kind,"SELECT id FROM lab.structured_docs WHERE v @@ plainto_tsquery('lab.structured_cfg',"+literal(raw)+")",'structured_stored_gin',[identifier]),
            ]
        queries += [
            ('gin_structured_hex',"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ '5fff'::tsquery",'structured_gin',[7]),
            ('gin_stored_structured_hex',"SELECT id FROM lab.structured_docs WHERE v @@ '5fff'::tsquery",'structured_stored_gin',[7]),
            ('bm25_structured_hex',"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query('0x5fff','lab.structured_bm25')LIMIT 20",'structured_bm25',[7]),
            # The broad bag query deliberately reaches independent shared-host/unit rows.
            ('bm25_structured_url_bag',"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query("+literal(STRUCTURED_TOKENS['url'][1])+",'lab.structured_bm25')LIMIT 20",'structured_bm25',[1,2,3,5]),
        ]
    else:
        queries=[
            ('gin_structured_post_target',"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ plainto_tsquery('lab.structured_cfg','https://after.example.com/target')",'structured_gin',[99]),
            ('gin_stored_structured_post_target',"SELECT id FROM lab.structured_docs WHERE v @@ plainto_tsquery('lab.structured_cfg','https://after.example.com/target')",'structured_stored_gin',[99]),
            ('bm25_structured_post_target',"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query('https://after.example.com/target','lab.structured_bm25')LIMIT 20",'structured_bm25',[99]),
            ('gin_structured_stem_removed',"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ 'walk'::tsquery",'structured_gin',[]),
            ('gin_stored_structured_stem_removed',"SELECT id FROM lab.structured_docs WHERE v @@ 'walk'::tsquery",'structured_stored_gin',[]),
        ]
        for kind in('host','email','file','numword'):
            identifier,raw,term=STRUCTURED_TOKENS[kind]
            if kind=='file':term='walking'
            direct=json.loads(sql('SELECT to_jsonb(ts_lexize('+literal('lab.structured_'+kind)+','+literal(raw)+'))',label+'-structured-direct-'+kind))
            check(raw in direct and term in direct,label+' '+kind+' cold INIT uses the later committed nonstemming parameters',direct)
            if kind=='file':check('walk'not in direct,label+' later file parameters remove the old stem',direct)
            headline=json.loads(sql("SELECT to_jsonb(ts_headline('lab.structured_cfg',"+literal(raw)+','+literal(term)+"::tsquery,'HighlightAll=true'))",label+'-structured-headline-'+kind))
            check(headline=='<b>'+raw+'</b>',label+' '+kind+' whole-source headline remains usable after crash',headline)
            queries += [
                ('gin_structured_'+kind,"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ "+literal(term)+"::tsquery",'structured_gin',[identifier]),
                ('gin_stored_structured_'+kind,"SELECT id FROM lab.structured_docs WHERE v @@ "+literal(term)+"::tsquery",'structured_stored_gin',[identifier]),
                ('bm25_structured_'+kind,"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query("+literal(term)+",'lab.structured_bm25')LIMIT 20",'structured_bm25',[identifier]),
            ]
        headline=sql("SELECT ts_headline('lab.structured_cfg',body,plainto_tsquery('lab.structured_cfg','https://after.example.com/target'),'HighlightAll=true')FROM lab.structured_docs WHERE id=99",label+'-structured-post-target-headline')
        check(headline=='<b>https://after.example.com/target</b>',label+' later simple chain still highlights the full raw URL',headline)
    observe_queries(label,queries)

def observe(label,expected):
    heap=json.loads(sql('SELECT jsonb_agg(id ORDER BY id) FROM lab.docs',label+'-heap'))
    check(heap==expected,label+' heap at target',heap)
    snapshot=json.loads(sql("SELECT jsonb_build_object('options',(SELECT dictinitoption FROM pg_ts_dict WHERE oid='lab.eng_stem'::regdictionary),'maps',(SELECT jsonb_agg(d.dictname ORDER BY m.mapseqno)FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid=m.mapdict JOIN ts_token_type('charabia.parser')t ON t.tokid=m.maptokentype WHERE m.mapcfg='lab.cfg'::regconfig AND t.alias='eng'),'vector',to_tsvector('lab.cfg',"+literal(ENGLISH)+")::text,'stored_consistent',NOT EXISTS(SELECT FROM lab.docs WHERE v IS DISTINCT FROM to_tsvector('lab.cfg',body)))",label+'-catalog'))
    check('stem = true' in snapshot['options'].lower().replace("'",""),label+' dictionary parameter restored',snapshot)
    check(snapshot['maps']==['eng_filter','eng_stem'],label+' mapping restored',snapshot)
    check("'run':" in snapshot['vector'] and snapshot['stored_consistent'],label+' analysis and stored vectors restored',snapshot)
    headline=sql("SELECT ts_headline('lab.cfg',body,'run'::tsquery,'HighlightAll=true')FROM lab.docs WHERE id=1",label+'-headline')
    check('<b>running</b>'in headline,label+' filter/stem headline retains original',headline)
    observe_chinese_headline(label)
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
             ('mixed_latin',"SELECT id FROM lab.docs ORDER BY body <@> to_bm25query('PostgreSQL','lab.docs_bm25') LIMIT 20",'docs_bm25',[2])]
    observe_queries(label,queries+chinese_queries())
    observe_round4(label)
    observe_structured_tokens(label)

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
    sql("CREATE EXTENSION pg_charabia;CREATE EXTENSION pg_textsearch;CREATE SCHEMA lab;CREATE TEXT SEARCH CONFIGURATION lab.cfg(COPY=public.charabia);CREATE TEXT SEARCH DICTIONARY lab.eng_raw(TEMPLATE=charabia.linguistic_template,Language='eng',Stem=false,Discard=false,English=false);CREATE TEXT SEARCH DICTIONARY lab.eng_stem(TEMPLATE=charabia.linguistic_template,Language='eng',Stem=false,Discard=false,English=false);ALTER TEXT SEARCH CONFIGURATION lab.cfg ALTER MAPPING FOR eng WITH lab.eng_raw;CREATE TEXT SEARCH DICTIONARY lab.eng_filter(TEMPLATE=charabia.linguistic_template,Language='eng',Discard=false,Stem=false,Normalize=false,Filter=true,English=false);CREATE TABLE lab.docs(id int PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144),v tsvector);",'setup')
    sql("CREATE TEXT SEARCH DICTIONARY lab.parallel(TEMPLATE=charabia.linguistic_template,Language='tur',Discard=false,Stem=false,Normalize=false,Filter=false,English=false);CREATE TEXT SEARCH DICTIONARY lab.und_words(TEMPLATE=charabia.linguistic_template,Language='und',Discard=false,Stem=false,Normalize=false,Filter=false,English=false);CREATE TEXT SEARCH CONFIGURATION lab.variant_cfg(PARSER=charabia.parser);DO $m$ DECLARE r record;BEGIN FOR r IN SELECT alias FROM ts_token_type('charabia.parser')WHERE alias NOT IN('blank','display_only')LOOP EXECUTE format('ALTER TEXT SEARCH CONFIGURATION lab.variant_cfg ADD MAPPING FOR %I WITH lab.parallel',r.alias);END LOOP;END$m$;ALTER TEXT SEARCH CONFIGURATION lab.variant_cfg ALTER MAPPING FOR und WITH lab.und_words;CREATE TABLE lab.variant_docs(id int PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144),v tsvector);INSERT INTO lab.variant_docs VALUES(1,'ABBREVIATE',NULL),(2,'PostgreSQL',NULL),(3,'12345',NULL);UPDATE lab.variant_docs SET v=to_tsvector('lab.variant_cfg',body);CREATE INDEX variant_gin ON lab.variant_docs USING gin(to_tsvector('lab.variant_cfg',body));CREATE INDEX variant_stored_gin ON lab.variant_docs USING gin(v);CREATE INDEX variant_bm25 ON lab.variant_docs USING bm25(body)WITH(text_config='lab.variant_cfg')",'round4-base-data')
    structured_setup="CREATE TEXT SEARCH CONFIGURATION lab.structured_cfg(COPY=public.charabia);"
    for kind in STRUCTURED_TOKENS:
        structured_setup+=("CREATE TEXT SEARCH DICTIONARY lab.structured_"+kind+"(TEMPLATE=charabia.structured_template,Kind="+literal(kind)+",Discard=false,Stem=false,Normalize=false);"
                       "ALTER TEXT SEARCH CONFIGURATION lab.structured_cfg ALTER MAPPING FOR "+kind+" WITH lab.structured_"+kind+";")
    structured_setup+="CREATE TABLE lab.structured_docs(id int PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144),v tsvector);INSERT INTO lab.structured_docs VALUES"
    structured_setup+=','.join('('+str(identifier)+','+literal(raw)+',NULL)'for identifier,raw,_ in STRUCTURED_TOKENS.values())+",(6,'unrelated',NULL);"
    structured_setup+="UPDATE lab.structured_docs SET v=to_tsvector('lab.structured_cfg',body);CREATE INDEX structured_gin ON lab.structured_docs USING gin(to_tsvector('lab.structured_cfg',body));CREATE INDEX structured_stored_gin ON lab.structured_docs USING gin(v);CREATE INDEX structured_bm25 ON lab.structured_docs USING bm25(body)WITH(text_config='lab.structured_cfg');"
    sql(structured_setup,'structured-base-data')
    sql("INSERT INTO lab.docs VALUES(1,"+literal(ENGLISH)+",NULL),(2,'中文全文检索 PostgreSQL 18 支持 BM25',NULL),(3,'unrelated',NULL),(4,"+literal(ENGLISH)+",NULL);UPDATE lab.docs SET v=to_tsvector('lab.cfg',body);CREATE INDEX docs_gin ON lab.docs USING gin(to_tsvector('lab.cfg',body));CREATE INDEX docs_stored_gin ON lab.docs USING gin(v);CREATE INDEX docs_bm25 ON lab.docs USING bm25(body)WITH(text_config='lab.cfg');CHECKPOINT",'base-data')
    # A real online basebackup; post-backup changes must come from archived WAL.
    command([BIN/'pg_basebackup','-D',base,'--format=plain','--wal-method=stream','--checkpoint=fast','--no-password'],'basebackup')
    command([BIN/'pg_verifybackup',base],'verify-basebackup')
    REPORT['backup_has_extension_so']=any(p.name=='pg_charabia.so'for p in base.rglob('*'))
    check(not REPORT['backup_has_extension_so'],'basebackup does not include staged extension files')
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.eng_stem(Stem=true);ALTER TEXT SEARCH CONFIGURATION lab.cfg ALTER MAPPING FOR eng WITH lab.eng_filter,lab.eng_stem;UPDATE lab.docs SET v=to_tsvector('lab.cfg',body);REINDEX INDEX lab.docs_gin;REINDEX INDEX lab.docs_stored_gin;REINDEX INDEX lab.docs_bm25;ALTER TEXT SEARCH DICTIONARY lab.parallel(English=true,Stem=true);UPDATE lab.variant_docs SET v=to_tsvector('lab.variant_cfg',body);REINDEX INDEX lab.variant_gin;REINDEX INDEX lab.variant_stored_gin;REINDEX INDEX lab.variant_bm25;COMMIT",'target-config')
    structured_changes=''.join("ALTER TEXT SEARCH DICTIONARY lab.structured_"+kind+"(Discard=true,Stem=true,Normalize=true);"for kind in STRUCTURED_TOKENS)
    sql("BEGIN;"+structured_changes+"ALTER TEXT SEARCH CONFIGURATION lab.structured_cfg ALTER MAPPING FOR url WITH lab.eng_filter,lab.structured_url;UPDATE lab.structured_docs SET v=to_tsvector('lab.structured_cfg',body);REINDEX INDEX lab.structured_gin;REINDEX INDEX lab.structured_stored_gin;REINDEX INDEX lab.structured_bm25;COMMIT",'target-structured-config')
    sql("BEGIN;INSERT INTO lab.docs VALUES(5,"+literal(ENGLISH)+",to_tsvector('lab.cfg',"+literal(ENGLISH)+"));UPDATE lab.docs SET body="+literal(ENGLISH)+",v=to_tsvector('lab.cfg',"+literal(ENGLISH)+")WHERE id=3;DELETE FROM lab.docs WHERE id=4;COMMIT",'target-dml')
    sql("INSERT INTO lab.structured_docs VALUES(7,'0x5fff',to_tsvector('lab.structured_cfg','0x5fff'))",'target-structured-unspilled')
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.eng_stem(Stem=false);ALTER TEXT SEARCH DICTIONARY lab.parallel(English=false,Stem=false);ROLLBACK",'rolled-back-ddl')
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.structured_url(Discard=false,Stem=false,Normalize=false);ALTER TEXT SEARCH CONFIGURATION lab.structured_cfg ALTER MAPPING FOR url WITH pg_catalog.simple;ROLLBACK",'rolled-back-structured-ddl')
    observe('target',[1,2,3,5])
    target='pgcb_recovery_'+RUN.replace('-','_')
    lsn=sql('SELECT pg_create_restore_point('+literal(target)+')','restore-point')
    REPORT['target']={'name':target,'lsn':lsn,'timeline':int(sql("SELECT substring(pg_walfile_name(pg_current_wal_lsn()) for 8)",'source-timeline'),16),'kind':'named WAL restore point after target commits and rollback'};save()
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.eng_stem(Stem=false);ALTER TEXT SEARCH DICTIONARY lab.parallel(English=false,Stem=false);ALTER TEXT SEARCH CONFIGURATION lab.cfg ALTER MAPPING FOR eng WITH lab.eng_raw;DELETE FROM lab.docs WHERE id=1;INSERT INTO lab.docs VALUES(99,'after target',to_tsvector('lab.cfg','after target'));COMMIT",'after-target')
    structured_changes=''.join("ALTER TEXT SEARCH DICTIONARY lab.structured_"+kind+"(Discard=false,Stem=false,Normalize=false);"for kind in STRUCTURED_TOKENS)
    sql("BEGIN;"+structured_changes+"ALTER TEXT SEARCH CONFIGURATION lab.structured_cfg ALTER MAPPING FOR url WITH pg_catalog.simple;DELETE FROM lab.structured_docs WHERE id=1;UPDATE lab.structured_docs SET v=to_tsvector('lab.structured_cfg',body);REINDEX INDEX lab.structured_gin;REINDEX INDEX lab.structured_stored_gin;REINDEX INDEX lab.structured_bm25;INSERT INTO lab.structured_docs VALUES(99,'https://after.example.com/target',to_tsvector('lab.structured_cfg','https://after.example.com/target'));COMMIT",'after-target-structured_tokens')
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
    check('english = false'in sql("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lab.parallel'::regdictionary",'crash-english').lower().replace("'",''),
          'crash recovery retains the later committed English option')
    maps=json.loads(sql("SELECT jsonb_agg(d.dictname ORDER BY m.mapseqno)FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid=m.mapdict JOIN ts_token_type('charabia.parser')t ON t.tokid=m.maptokentype WHERE m.mapcfg='lab.cfg'::regconfig AND t.alias='eng'",'crash-mapping'))
    check(maps==['eng_raw'],'crash recovery retains the committed post-target mapping',maps)
    REPORT.setdefault('observations',{})['crash']={'heap':[2,3,5,99],'maps':maps,'queries':[]};save()
    # The later English ALTER deliberately left old postings in place. Chinese
    # analysis is unchanged, so its fixed IDs isolate crash index reconstruction.
    observe_queries('crash',chinese_queries())
    observe_chinese_headline('crash')
    observe_structured_tokens('crash',False)
    with Session(env=ENV) as backend:
        analysis_sql="SELECT to_tsvector('lab.cfg','中文全文检索')"
        vector=backend.value(analysis_sql)
        check("'全文':"in vector,'crash-restarted fresh backend rebuilds Chinese analysis',vector)
        mappings=Path(f'/proc/{backend.pid}/maps').read_text()
        (OUT/'crash-backend-maps.txt').write_text(mappings)
        check(str(lib)in mappings,'crash-restarted backend maps exact staged release library',backend.pid)
        REPORT['observations']['crash']['backend']={'pid':backend.pid,'sql':analysis_sql,'vector':vector,'library':str(lib)};save()
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
    # Recovery does not pin pre-backup OIDs or runtime state. Ordinary ALTER and
    # explicit maintenance must still work after target replay and promotion.
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.parallel(English=false,Stem=false);UPDATE lab.variant_docs SET v=to_tsvector('lab.variant_cfg',body);REINDEX INDEX lab.variant_gin;REINDEX INDEX lab.variant_stored_gin;REINDEX INDEX lab.variant_bm25;COMMIT",'restored-alter-maintenance')
    changed=json.loads(sql("SELECT to_jsonb(ts_lexize('lab.parallel','ABBREVIATE'))",'restored-alter-direct'))
    check(changed==['abbrevıate'],'restored dictionary can disable English and stem after promotion',changed)
    REPORT['observations']['restored-altered']={'queries':[]};save()
    observe_queries('restored-altered',[
        ('gin_changed',"SELECT id FROM lab.variant_docs WHERE to_tsvector('lab.variant_cfg',body) @@ 'abbrevıate'::tsquery",'variant_gin',[1]),
        ('gin_removed',"SELECT id FROM lab.variant_docs WHERE to_tsvector('lab.variant_cfg',body) @@ 'abbrevi'::tsquery",'variant_gin',[]),
        ('gin_stored_removed',"SELECT id FROM lab.variant_docs WHERE v @@ 'abbrevi'::tsquery",'variant_stored_gin',[]),
        ('bm25_changed',"SELECT id FROM lab.variant_docs ORDER BY body <@> to_bm25query('ABBREVIATE','lab.variant_bm25')LIMIT 20",'variant_bm25',[1]),
        ('bm25_removed',"SELECT id FROM lab.variant_docs ORDER BY body <@> to_bm25query('abbrevi','lab.variant_bm25')LIMIT 20",'variant_bm25',[])])
    sql("BEGIN;ALTER TEXT SEARCH DICTIONARY lab.structured_file(Discard=false,Stem=false,Normalize=false);ALTER TEXT SEARCH CONFIGURATION lab.structured_cfg ALTER MAPPING FOR url WITH lab.structured_url;UPDATE lab.structured_docs SET v=to_tsvector('lab.structured_cfg',body);REINDEX INDEX lab.structured_gin;REINDEX INDEX lab.structured_stored_gin;REINDEX INDEX lab.structured_bm25;COMMIT",'restored-structured-alter-maintenance')
    changed=json.loads(sql("SELECT jsonb_build_object('terms',ts_lexize('lab.structured_file','/srv/WalkingTests.rs'),'options',(SELECT dictinitoption FROM pg_ts_dict WHERE oid='lab.structured_file'::regdictionary),'url_chain',(SELECT jsonb_agg(mapdict::regdictionary::text ORDER BY mapseqno)FROM pg_ts_config_map WHERE mapcfg='lab.structured_cfg'::regconfig AND maptokentype=19),'stored_consistent',NOT EXISTS(SELECT FROM lab.structured_docs WHERE v IS DISTINCT FROM to_tsvector('lab.structured_cfg',body)))",'restored-structured-alter-catalog'))
    check('walking'in changed['terms']and'walk'not in changed['terms']and'/srv/WalkingTests.rs'in changed['terms'],
          'restored native structured dictionary can disable stemming without losing literal or surface',changed)
    check(changed['url_chain']==['lab.structured_url']and changed['stored_consistent'],
          'restored native structured mapping can change and all stored vectors can be maintained',changed)
    REPORT['observations']['restored-structured-altered']={'catalog':changed,'queries':[]};save()
    observe_queries('restored-structured-altered',[
        ('gin_structured_changed',"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ 'walking'::tsquery",'structured_gin',[4]),
        ('gin_structured_removed',"SELECT id FROM lab.structured_docs WHERE to_tsvector('lab.structured_cfg',body) @@ 'walk'::tsquery",'structured_gin',[]),
        ('gin_stored_structured_removed',"SELECT id FROM lab.structured_docs WHERE v @@ 'walk'::tsquery",'structured_stored_gin',[]),
        ('bm25_structured_changed',"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query('walking','lab.structured_bm25')LIMIT 20",'structured_bm25',[4]),
        ('bm25_structured_removed',"SELECT id FROM lab.structured_docs ORDER BY body <@> to_bm25query('walk','lab.structured_bm25')LIMIT 20",'structured_bm25',[])])
    check(sql("SELECT ts_headline('lab.structured_cfg','/srv/WalkingTests.rs','walking'::tsquery,'HighlightAll=true')",'restored-structured-alter-headline')=='<b>/srv/WalkingTests.rs</b>',
          'whole-source structured headline still uses the maintained post-recovery dictionary')
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
