"""Fixed corpus measurements; raw observations, no invented performance SLA."""
import json
import os
import statistics
import subprocess
from pathlib import Path
from support import EVIDENCE
import time
from support import Session, literal, run, save

assert os.environ["PGCB_PLATFORM"]=="linux-amd64"
direct_log=EVIDENCE/"benchmark-direct.log"
with direct_log.open("w") as output:
    subprocess.run(["cargo","test","--locked","--release","--lib","--no-default-features","--features","pg18","benchmark_direct_pipeline","--","--ignored","--nocapture"],stdout=output,stderr=subprocess.STDOUT,check=True)
direct=[json.loads(line.split("PGCB_BENCHMARK=",1)[1]) for line in direct_log.read_text().splitlines() if line.startswith("PGCB_BENCHMARK=")]
assert len(direct)==1,direct_log
results={"direct_charabia":direct[0],"environment":"Linux AMD64 under Docker emulation when host is ARM64", "samples":{}, "plans":{}}

def cpu_ticks(pid):
    fields=Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    return int(fields[11])+int(fields[12])

def measure(session,name,sql,repeats=1):
    samples=[]
    cpu=[]
    for _ in range(repeats):
        ticks=cpu_ticks(session.pid)
        start=time.perf_counter_ns()
        output=session.value(sql)
        samples.append((time.perf_counter_ns()-start)/1e6)
        cpu.append((cpu_ticks(session.pid)-ticks)*1000/os.sysconf("SC_CLK_TCK"))
    ordered=sorted(samples)
    results["samples"][name]={"roundtrip_ms":samples,"backend_cpu_ms":cpu,"median_ms":statistics.median(samples),"p95_ms":ordered[min(len(ordered)-1,int(len(ordered)*.95))],"p99_ms":ordered[min(len(ordered)-1,int(len(ordered)*.99))]}
    return output

def nodes(value):
    if isinstance(value,dict):
        yield value
        for child in value.values(): yield from nodes(child)
    elif isinstance(value,list):
        for child in value: yield from nodes(child)

run("CREATE SCHEMA benchmark; CREATE TABLE benchmark.docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144))")
try:
    with Session() as session:
        results["backend"]=session.pid
        from support import rss
        results["rss_cold_bytes"]=rss(session.pid)
        text="PostgreSQL 数据库 全文检索 café 日本語の検索 한국어 검색 /usr/local v18.6 https://example.com"
        measure(session,"insert_bulk_1000","INSERT INTO benchmark.docs SELECT g,repeat("+literal(text)+",1+(g%4)) FROM generate_series(1,1000) g")
        results["corpus"]=json.loads(session.value("SELECT json_build_object('rows',count(*),'bytes',sum(octet_length(body))) FROM benchmark.docs"))
        measure(session,"analyze_1000","SELECT sum((SELECT count(*) FROM charabia.analyze_v1(d.body))) FROM benchmark.docs d",3)
        measure(session,"vectorize_1000","SELECT sum(length(to_tsvector('charabia.default_v1',body))) FROM benchmark.docs",3)
        measure(session,"build_gin","CREATE INDEX benchmark_gin ON benchmark.docs USING gin(to_tsvector('charabia.default_v1',body))")
        measure(session,"build_bm25","CREATE INDEX benchmark_bm25 ON benchmark.docs USING bm25(body) WITH(text_config='charabia.default_v1')")
        measure(session,"insert_incremental_100","INSERT INTO benchmark.docs SELECT g,'PostgreSQL 中文 incremental' FROM generate_series(1001,1100) g")
        measure(session,"update_40","UPDATE benchmark.docs SET body='PostgreSQL 中文 updated' WHERE id BETWEEN 1001 AND 1040")
        measure(session,"delete_20","DELETE FROM benchmark.docs WHERE id BETWEEN 1081 AND 1100")
        session.value("ANALYZE benchmark.docs; SET enable_seqscan=off")
        queries={"gin":"SELECT id FROM benchmark.docs WHERE to_tsvector('charabia.default_v1',body) @@ plainto_tsquery('charabia.default_v1','PostgreSQL')", "bm25":"SELECT id FROM benchmark.docs ORDER BY body <@> to_bm25query('PostgreSQL','benchmark.benchmark_bm25') LIMIT 2000"}
        for name,query in queries.items():
            plan=json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) "+query))
            assert any(n.get("Index Name")=="benchmark_"+name and n.get("Actual Loops",0)>0 for n in nodes(plan)),plan
            results["plans"][name]=plan
            ids=measure(session,"query_"+name,query,10).splitlines()
            assert len(ids)==1080 and len(set(ids))==1080
        results["sizes"]=json.loads(session.value("SELECT json_object_agg(relname,pg_relation_size(oid)) FROM pg_class WHERE relnamespace='benchmark'::regnamespace"))
        results["rss_warm_bytes"]=rss(session.pid)
        results["process_status"]=Path(f"/proc/{session.pid}/status").read_text()
        results["process_stat"]=Path(f"/proc/{session.pid}/stat").read_text()
        results["clock_ticks_per_second"]=os.sysconf("SC_CLK_TCK")
        results["memory_contexts"]=json.loads(session.value("SELECT json_build_object('bytes',sum(total_bytes),'used',sum(used_bytes)) FROM pg_backend_memory_contexts"))
finally:
    save("benchmark-results.json",results)
    run("DROP SCHEMA benchmark CASCADE")
print(json.dumps({"status":"passed","corpus":results["corpus"],"measurements":len(results["samples"])}))
