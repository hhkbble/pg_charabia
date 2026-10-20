"""Bounded real-backend concurrency, worker execution and memory observations."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
from support import Session, json_query, run, save, rss

results = {"memory": [], "concurrency": [], "parallel": {}}
run("""
CREATE SCHEMA stress;
CREATE TABLE stress.docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO stress.docs SELECT g,'PostgreSQL 数据库 全文检索 café '||g FROM generate_series(1,20000) g;
CREATE INDEX stress_gin ON stress.docs USING gin(to_tsvector('charabia.default_v1',body));
ANALYZE stress.docs;
""")

def nodes(value):
    if isinstance(value,dict):
        yield value
        for child in value.values(): yield from nodes(child)
    elif isinstance(value,list):
        for child in value: yield from nodes(child)


try:
    with Session() as session:
        session.value("SET max_parallel_workers_per_gather=2; SET min_parallel_table_scan_size=0; SET parallel_setup_cost=0; SET parallel_tuple_cost=0; SET enable_indexscan=off; SET enable_bitmapscan=off; SET jit=off")
        query="SELECT count(*) FROM stress.docs WHERE to_tsvector('charabia.default_v1',body) @@ plainto_tsquery('charabia.default_v1','PostgreSQL')"
        plan=json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) "+query))
        workers=sum(n.get("Workers Launched",0) for n in nodes(plan))
        assert workers>0,plan
        assert session.value(query)=="20000"
        results["parallel"]={"backend":session.pid,"workers_launched":workers,"plan":plan,"count":20000}
    with Session() as session:
        # Warm all language resources before judging per-request reclamation.
        text="PostgreSQL 数据库 全文检索 café 日本語の検索 한국어 검색 ภาษาไทย Ελληνικά עברית"
        from support import literal
        statement="SELECT count(to_tsvector('charabia.default_v1',"+literal(text)+" || ' ' || g::text)) FROM generate_series(1,250) g"
        session.value(statement)
        for batch in range(8):
            before=json.loads(session.value("SELECT charabia.test_stats_v1()")) if os.environ.get("PGCB_TEST_HOOKS")=="1" else None
            assert session.value(statement)=="250"
            if before is not None:
                after=json.loads(session.value("SELECT charabia.test_stats_v1()"))
                assert after["parser_created"]-before["parser_created"]==250,(before,after)
                assert after["parser_created"]==after["parser_dropped"],after
            memory=json.loads(session.value("SELECT json_build_object('bytes',sum(total_bytes),'used',sum(used_bytes),'contexts',count(*)) FROM pg_backend_memory_contexts"))
            results["memory"].append(dict(memory,batch=batch,pid=session.pid,rss_bytes=rss(session.pid)))
        tail=results["memory"][2:]
        # A query-context-sized allowance catches retained request allocations;
        # allocator/RSS observations are retained separately, not a speed target.
        assert max(x["bytes"] for x in tail)-min(x["bytes"] for x in tail)<1048576,tail
        if os.environ.get("PGCB_TEST_HOOKS")=="1":
            stats=json.loads(session.value("SELECT charabia.test_stats_v1()"))
            assert stats["parser_created"]==stats["parser_dropped"],stats
            results["test_counters"]=stats

    def client(role):
        with Session() as session:
            for batch in range(8):
                if role=="writer":
                    first=30000+batch*50
                    session.value(f"INSERT INTO stress.docs SELECT g,'PostgreSQL 中文 writer' FROM generate_series({first},{first+49}) g")
                    session.value(f"UPDATE stress.docs SET body='updated PostgreSQL 中文' WHERE id BETWEEN {first} AND {first+19}")
                    session.value(f"DELETE FROM stress.docs WHERE id BETWEEN {first+40} AND {first+49}")
                else:
                    assert int(session.value("SELECT count(*) FROM stress.docs WHERE to_tsvector('charabia.default_v1',body) @@ plainto_tsquery('charabia.default_v1','PostgreSQL')"))>=20000
                    error=session.query("SELECT * FROM charabia.analyze_v1(repeat('x',262145))")
                    assert "ERROR:" in error and session.value("SELECT 1")=="1",error
            return {"role":role,"backend":session.pid,"batches":8}
    with ThreadPoolExecutor(max_workers=3) as pool:
        results["concurrency"]=list(pool.map(client,["writer","reader-a","reader-b"]))
    assert run("SELECT count(*) FROM stress.docs")=="20320"
    if os.environ["PGCB_PLATFORM"]=="linux-amd64":
        run("CREATE INDEX stress_bm25 ON stress.docs USING bm25(body) WITH(text_config='charabia.default_v1')")
        with Session() as session:
            session.value("SET enable_seqscan=off")
            query="SELECT id FROM stress.docs ORDER BY body <@> to_bm25query('PostgreSQL','stress.stress_bm25') LIMIT 25000"
            plan=json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) "+query))
            assert any(n.get("Index Name")=="stress_bm25" and n.get("Actual Loops",0)>0 for n in nodes(plan)),plan
            assert session.value("SELECT count(*) FROM ("+query+") q")=="20320"
            results["bm25_after_concurrency"]={"plan":plan,"candidates":20320}
finally:
    save("stress-results.json",results)
    run("DROP SCHEMA stress CASCADE")
print(json.dumps({"status":"passed","parallel_workers":results["parallel"]["workers_launched"],"concurrent_clients":len(results["concurrency"]),"memory_batches":len(results["memory"])}))
