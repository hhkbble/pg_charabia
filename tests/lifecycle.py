"""Actual catalog invalidation, stale indexes, rebuild and dump/restore."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from support import EVIDENCE, PSQL, Session, json_query, literal, run, save

linux = os.environ["PGCB_PLATFORM"] == "linux-amd64"
if linux:
    assert run("SELECT count(*) FROM pg_extension WHERE extname='pg_textsearch'")=="1"
results = {"platform":os.environ["PGCB_PLATFORM"],"cache":[],"indexes":[],"drop_protection":{}}
run("""
CREATE SCHEMA lifecycle;
CREATE TEXT SEARCH CONFIGURATION lifecycle.cfg(COPY=charabia.default_v1);
CREATE TEXT SEARCH DICTIONARY lifecycle.latin(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='[]');
ALTER TEXT SEARCH CONFIGURATION lifecycle.cfg ALTER MAPPING FOR latin WITH lifecycle.latin;
CREATE TABLE lifecycle.docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO lifecycle.docs VALUES(1,'CAFÉ'),(2,'unrelated');
CREATE INDEX lifecycle_gin ON lifecycle.docs USING gin(to_tsvector('lifecycle.cfg',body));
""")
if linux:
    run("CREATE INDEX lifecycle_bm25 ON lifecycle.docs USING bm25(body) WITH(text_config='lifecycle.cfg')")

def candidates(session,bm25=False):
    session.value("SET enable_seqscan=off; SET enable_bitmapscan=on; SET enable_indexscan="+("on" if bm25 else "off"))
    if bm25:
        query="SELECT id FROM lifecycle.docs ORDER BY body <@> to_bm25query('CAFÉ','lifecycle.lifecycle_bm25') LIMIT 10"
        index="lifecycle_bm25"
    else:
        query="SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','CAFÉ')"
        index="lifecycle_gin"
    query="SELECT coalesce(json_agg(id ORDER BY id),'[]'::json) FROM ("+query+") q"
    plan=json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF,SUMMARY OFF) "+query))
    assert index in json.dumps(plan),plan
    ids=session.value(query)
    results["indexes"].append({"index":index,"plan":plan,"ids":json.loads(ids)})
    save("lifecycle-results.json",results)
    return json.loads(ids)

restore_db="pgcb_restore_"+str(os.getpid())
drop_db="pgcb_drop_"+str(os.getpid())
try:
    with Session() as warm:
        assert warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")=="'alpha':1 'beta':2"
        run("ALTER TEXT SEARCH DICTIONARY lifecycle.latin(StopWords='[\"alpha\"]')")
        current=warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")
        assert current=="'beta':2" and run("SELECT to_tsvector('lifecycle.cfg','alpha beta')")==current
        results["cache"].append({"warm_backend":warm.pid,"new_value":current})
        run("BEGIN; ALTER TEXT SEARCH DICTIONARY lifecycle.latin(StopWords='[\"beta\"]'); ROLLBACK;")
        assert warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")==current
        bad=run("ALTER TEXT SEARCH DICTIONARY lifecycle.latin(StopWords='{}')",check=False)
        assert bad.returncode and warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")==current
        assert candidates(warm)==[1]
        if linux: assert candidates(warm,True)==[1]
        run("ALTER TEXT SEARCH DICTIONARY lifecycle.latin(Lossy=false)")
        results["changed_analysis"]={"vector":warm.value("SELECT to_tsvector('lifecycle.cfg','CAFÉ')"),"query":warm.value("SELECT plainto_tsquery('lifecycle.cfg','CAFÉ')"),"options":run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.latin'::regdictionary")}
        save("lifecycle-results.json",results)
        warm.value("SET enable_seqscan=on; SET enable_bitmapscan=off; SET enable_indexscan=off")
        assert warm.value("SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','CAFÉ')")=="1"
        warm.value("SET enable_bitmapscan=on; SET enable_indexscan=on")
        assert candidates(warm)==[],"old GIN unexpectedly rebuilt itself"
        if linux: assert candidates(warm,True)==[],"old BM25 unexpectedly rebuilt itself"
        run("REINDEX INDEX lifecycle.lifecycle_gin")
        if linux: run("REINDEX INDEX lifecycle.lifecycle_bm25")
        assert candidates(warm)==[1]
        if linux: assert candidates(warm,True)==[1]

    words=["the","O'Reilly","C:\\code","的","", "line\nbreak", "e\u0301"]
    encoded=json.dumps(words,ensure_ascii=False)
    run("CREATE TEXT SEARCH DICTIONARY lifecycle.roundtrip(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords="+literal(encoded)+")")
    near_limit='["a"]'+' '*(1048576-5)
    run("CREATE TEXT SEARCH DICTIONARY lifecycle.large_option(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords="+literal(near_limit)+")")
    options=run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.roundtrip'::regdictionary")
    run(f"CREATE DATABASE {restore_db} TEMPLATE template0")
    try:
        run("CREATE EXTENSION pg_charabia",restore_db)
        if linux: run("CREATE EXTENSION pg_textsearch",restore_db)
        dump=EVIDENCE/"lifecycle-schema.sql"
        with dump.open("w") as output:
            subprocess.run([str(Path(PSQL).with_name("pg_dump")),"--schema-only","--no-owner","--no-privileges","--schema=lifecycle"],stdout=output,check=True)
        restored=subprocess.run([PSQL,"-X","-v","ON_ERROR_STOP=1","-d",restore_db,"-f",str(dump)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        assert restored.returncode==0,restored.stderr
        restored_options=run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.roundtrip'::regdictionary",restore_db)
        assert options==restored_options
        assert run("SELECT ts_lexize('lifecycle.large_option','a')",restore_db)=="{}"
        for word in words:
            assert run("SELECT ts_lexize('lifecycle.roundtrip',"+literal(word)+")",restore_db)==run("SELECT ts_lexize('lifecycle.roundtrip',"+literal(word)+")"),repr(word)
        results["dump_restore"]={"json":encoded,"catalog_sha256":hashlib.sha256(options.encode()).hexdigest(),"schema_sha256":hashlib.sha256(dump.read_bytes()).hexdigest(),"payload_bytes":len(near_limit.encode()),"restored":True}
    finally:
        run(f"DROP DATABASE {restore_db}")

    if linux:
        run("CREATE TEXT SEARCH CONFIGURATION lifecycle.droppable(COPY=charabia.default_v1); CREATE TABLE lifecycle.dropdocs(body text); INSERT INTO lifecycle.dropdocs VALUES('hello'); CREATE INDEX droppable_bm25 ON lifecycle.dropdocs USING bm25(body) WITH(text_config='lifecycle.droppable')")
        dependency=run("SELECT count(*) FROM pg_depend WHERE classid='pg_class'::regclass AND objid='lifecycle.droppable_bm25'::regclass AND refobjid='lifecycle.droppable'::regconfig")
        with Session() as warm:
            assert warm.value("SELECT count(*) FROM (SELECT body FROM lifecycle.dropdocs ORDER BY body <@> to_bm25query('hello','lifecycle.droppable_bm25') LIMIT 10) q")=="1"
            dropped=run("DROP TEXT SEARCH CONFIGURATION lifecycle.droppable",check=False)
            if dropped.returncode==0:
                outcome=warm.query("SELECT body FROM lifecycle.dropdocs ORDER BY body <@> to_bm25query('hello','lifecycle.droppable_bm25') LIMIT 10")
                assert "ERROR:" in outcome,outcome
                assert warm.value("SELECT 1")=="1"
                results["drop_protection"]["config"]={"dependency_count":int(dependency),"protected":False,"error":outcome}
            else:
                assert "depend" in dropped.stderr.lower(),dropped.stderr
                results["drop_protection"]["config"]={"dependency_count":int(dependency),"protected":True,"error":dropped.stderr}
        run(f"CREATE DATABASE {drop_db} TEMPLATE template0")
        try:
            run("CREATE EXTENSION pg_charabia; CREATE EXTENSION pg_textsearch; CREATE TABLE docs(body text); INSERT INTO docs VALUES('hello'); CREATE INDEX docs_bm25 ON docs USING bm25(body) WITH(text_config='charabia.default_v1')",drop_db)
            with Session(drop_db) as warm:
                warm.value("SELECT body FROM docs ORDER BY body <@> to_bm25query('hello','public.docs_bm25') LIMIT 10")
                dropped=run("DROP EXTENSION pg_charabia",drop_db,check=False)
                if dropped.returncode==0:
                    outcome=warm.query("SELECT body FROM docs ORDER BY body <@> to_bm25query('hello','public.docs_bm25') LIMIT 10")
                    assert "ERROR:" in outcome and warm.value("SELECT 1")=="1",outcome
                    results["drop_protection"]["extension"]={"protected":False,"error":outcome}
                else:
                    assert "depend" in dropped.stderr.lower(),dropped.stderr
                    results["drop_protection"]["extension"]={"protected":True,"error":dropped.stderr}
        finally:
            run(f"DROP DATABASE {drop_db}")
finally:
    save("lifecycle-results.json",results)
    run("DROP SCHEMA lifecycle CASCADE")
save("lifecycle-results.json",results)
print(json.dumps({"status":"passed","cache_sessions":len(results["cache"]),"index_checks":len(results["indexes"]),"dump_restore":True,"drop_protection":results["drop_protection"]},ensure_ascii=False))
