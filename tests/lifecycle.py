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
results = {"platform":os.environ["PGCB_PLATFORM"],"cache":[],"indexes":[],"prepared":[],"cached_init_faults":[],"drop_protection":{}}
has_hooks = os.environ.get("PGCB_TEST_HOOKS") == "1"
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

def plan_nodes(value):
    if isinstance(value, dict):
        if "Node Type" in value:
            yield value
        for child in value.values():
            yield from plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_nodes(child)


def target_plan(plan, index, bm25):
    kind = "Index Scan" if bm25 else "Bitmap Index Scan"
    nodes = [node for node in plan_nodes(plan)
             if node.get("Node Type") == kind and node.get("Index Name") == index]
    assert nodes and all(node.get("Actual Loops", 0) > 0 for node in nodes), plan
    assert any(node.get("Relation Name") == "docs" and node.get("Schema") == "lifecycle"
               for node in plan_nodes(plan)), plan


def candidates(session, bm25=False):
    session.value("SET enable_seqscan=off; SET enable_bitmapscan=on; SET enable_indexscan=" + ("on" if bm25 else "off"))
    if bm25:
        query = "SELECT id FROM lifecycle.docs ORDER BY body <@> to_bm25query('CAFÉ','lifecycle.lifecycle_bm25') LIMIT 10"
        index = "lifecycle_bm25"
    else:
        query = "SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','CAFÉ')"
        index = "lifecycle_gin"
    query = "SELECT coalesce(json_agg(id ORDER BY id),'[]'::json) FROM (" + query + ") q"
    plan = json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + query))
    target_plan(plan, index, bm25)
    ids = json.loads(session.value(query))
    results["indexes"].append({"index":index,"plan":plan,"ids":ids})
    save("lifecycle-results.json", results)
    return ids


def prepare_queries(session):
    for suffix in ("custom", "generic"):
        analysis_name = "lifecycle_analysis_" + suffix
        session.value(f"PREPARE {analysis_name}(text) AS SELECT jsonb_build_object("
                      "'vector',to_tsvector('lifecycle.cfg',$1)::text,"
                      "'query',plainto_tsquery('lifecycle.cfg',$1)::text,"
                      "'lexemes',tsvector_to_array(to_tsvector('lifecycle.cfg',$1)),"
                      "'default_vector',to_tsvector($1)::text,"
                      "'default_config',current_setting('default_text_search_config'))")
        for bm25 in (False, True) if linux else (False,):
            family = "bm25" if bm25 else "gin"
            name = "lifecycle_" + family + "_" + suffix
            ranked = ("SELECT id FROM lifecycle.docs ORDER BY body <@> "
                      "to_bm25query($1,'lifecycle.lifecycle_bm25') LIMIT 10") if bm25 else (
                      "SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) "
                      "@@ plainto_tsquery('lifecycle.cfg',$1)")
            session.value(f"PREPARE {name}(text) AS SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM ({ranked}) q")
    session.value("PREPARE lifecycle_literal AS SELECT plainto_tsquery('lifecycle.cfg','CAFÉ')::text")
    session.value("SET plan_cache_mode=force_generic_plan")
    assert session.value("EXECUTE lifecycle_literal") == "'cafe'"
    return json.loads(session.value("SELECT jsonb_object_agg(name,prepare_time) FROM pg_prepared_statements"))


def prepared_state(session, label, prepared_at, lossy, expected_ids, default_config):
    lemma = "cafe" if lossy else "CAFE\u0301"
    expected_vector = "'" + lemma + "':1"
    record = {"state":label,"pid":session.pid,"lossy":lossy,"expected_ids":expected_ids,
              "config_oid":int(session.value("SELECT 'lifecycle.cfg'::regconfig::oid")),
              "dictionary_options":session.value("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.latin'::regdictionary"),
              "queries":[]}
    results["prepared"].append(record)
    save("lifecycle-results.json", results)
    session.value("SET default_text_search_config=" + literal(default_config))
    # Built-in simple lowercasing follows LC_CTYPE; isolated C clusters retain
    # the accented uppercase character (cafÉ). This control is independent of
    # the Charabia golden and checks only the default-configuration dispatch.
    record["simple_control"] = json.loads(session.value(
        "SELECT jsonb_build_object('vector',to_tsvector('pg_catalog.simple','CAFÉ')::text,"
        "'lc_ctype',datctype,'lc_collate',datcollate) FROM pg_database WHERE datname=current_database()"))
    # Force a fresh heap evaluation of explicit native analysis. Stale index
    # candidates intentionally differ until REINDEX, although analysis agrees.
    session.value("SET enable_seqscan=on; SET enable_bitmapscan=off; SET enable_indexscan=off")
    oracle_sql = ("SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM lifecycle.docs "
                  "WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','CAFÉ')")
    record["native_oracle_plan"] = json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + oracle_sql))
    assert any(node.get("Node Type") == "Seq Scan" and node.get("Actual Loops", 0) > 0
               for node in plan_nodes(record["native_oracle_plan"])), record
    record["native_oracle_ids"] = json.loads(session.value(oracle_sql))
    assert record["native_oracle_ids"] == [1], record
    for suffix, mode in (("custom", "force_custom_plan"), ("generic", "force_generic_plan")):
        session.value("SET plan_cache_mode=" + mode)
        analysis = json.loads(session.value(f"EXECUTE lifecycle_analysis_{suffix}('CAFÉ')"))
        record.setdefault("analysis", {})[suffix] = analysis
        save("lifecycle-results.json", results)
        assert analysis["vector"] == expected_vector and analysis["lexemes"] == [lemma], analysis
        assert analysis["query"] == "'" + lemma + "'", analysis
        assert analysis["default_config"] == default_config, analysis
        expected_default = record["simple_control"]["vector"] if default_config == "pg_catalog.simple" else expected_vector
        assert record["simple_control"]["vector"] != expected_vector, record
        assert analysis["default_vector"] == expected_default, analysis
        for bm25 in (False, True) if linux else (False,):
            family = "bm25" if bm25 else "gin"
            name = "lifecycle_" + family + "_" + suffix
            session.value("SET enable_seqscan=off; SET enable_bitmapscan=on; SET enable_indexscan=" + ("on" if bm25 else "off"))
            execute = f"EXECUTE {name}('CAFÉ')"
            plan = json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + execute))
            index = "lifecycle_" + family
            target_plan(plan, index, bm25)
            ids = json.loads(session.value(execute))
            record["queries"].append({"mode":mode,"index":index,"execute":execute,"plan":plan,"ids":ids})
            save("lifecycle-results.json", results)
            assert ids == expected_ids, record["queries"][-1]
    stats = json.loads(session.value("SELECT jsonb_agg(jsonb_build_object('name',name,'prepare_time',prepare_time,"
                                    "'generic_plans',generic_plans,'custom_plans',custom_plans) ORDER BY name) FROM pg_prepared_statements"))
    record["prepared_statements"] = stats
    for stat in stats:
        assert stat["prepare_time"] == prepared_at[stat["name"]], stat
        if stat["name"].endswith("_custom"):
            assert stat["custom_plans"] > 0 and stat["generic_plans"] == 0, stat
        elif stat["name"].endswith("_generic"):
            assert stat["generic_plans"] > 0 and stat["custom_plans"] == 0, stat
    if linux:
        record["bm25_reloptions"] = json.loads(session.value("SELECT to_json(reloptions) FROM pg_class WHERE oid='lifecycle.lifecycle_bm25'::regclass"))
        assert "text_config=lifecycle.cfg" in record["bm25_reloptions"], record
    save("lifecycle-results.json", results)


def cached_init_faults():
    # Separate dictionary OIDs ensure first-load tests cannot reuse a successful
    # cache entry. DDL runs in backend B so it cannot consume A's armed hook.
    for warmed in (False, True):
        for point, modes in (("init", ("error", "panic")),
                             ("register_alloc", ("error", "panic", "allocation_error")),
                             ("register_link", ("error", "panic", "allocation_error"))):
            for mode in modes:
                name = f"lifecycle.failure_{int(warmed)}_{point}_{mode}"
                run(f"CREATE TEXT SEARCH DICTIONARY {name}(TEMPLATE=charabia.normalize_v1,Route='latin',StopWords='[\"canary\"]')")
                with Session() as session:
                    sql = "SELECT ts_lexize(" + literal(name) + ",'CAFÉ')"
                    if warmed:
                        assert session.value(sql) == "{cafe}"
                        run(f"ALTER TEXT SEARCH DICTIONARY {name}(Lossy=false)")
                    label = {"warmed":warmed,"point":point,"mode":mode,"pid":session.pid,"attempts":[]}
                    results["cached_init_faults"].append(label)
                    before = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                    initial_live = before["dictionary_created"] - before["dictionary_dropped"]
                    for attempt in range(2):
                        session.value("SELECT charabia.test_set_fault_v1(" + literal(point) + "," + literal(mode) + ")")
                        output = session.query(sql)
                        after = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                        label["attempts"].append({"attempt":attempt,"before":before,"after":after,"output":output})
                        save("lifecycle-results.json", results)
                        assert "ERROR:" in output, output
                        assert ("injected pg_charabia" in output) if mode != "allocation_error" else ("invalid memory alloc request size" in output), output
                        assert after["dictionary_created"] - before["dictionary_created"] == 1, (before, after)
                        failed_generation = after["dictionary_last_created"]
                        assert after["dictionary_last_lexized"] != failed_generation, after
                        assert after["dictionary_last_lexized"] == before["dictionary_last_lexized"], (before, after)
                        retained = point == "init"
                        previous_retained = (warmed if attempt == 0 else retained)
                        assert after["dictionary_dropped"] - before["dictionary_dropped"] == int(previous_retained) + int(not retained), (before, after)
                        assert after["state_registered"] - before["state_registered"] == int(retained), (before, after)
                        assert after["state_released"] - before["state_released"] == int(previous_retained), (before, after)
                        assert after["dictionary_created"] - after["dictionary_dropped"] == initial_live - int(warmed) + int(retained), after
                        assert after["dictionary_last_created_fst_bytes"] > 0, after
                        expected_bytes = (before["dictionary_live_fst_bytes"]
                                          - int(previous_retained) * before["dictionary_last_created_fst_bytes"]
                                          + int(retained) * after["dictionary_last_created_fst_bytes"])
                        assert after["dictionary_live_fst_bytes"] == expected_bytes, (before, after)
                        assert session.value("SELECT 1") == "1"
                        before = after
                    assert session.value(sql) == ("{CAFE\u0301}" if warmed else "{cafe}")
                    recovered = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                    assert recovered["dictionary_created"] - before["dictionary_created"] == 1, (before, recovered)
                    assert recovered["dictionary_dropped"] - before["dictionary_dropped"] == int(point == "init"), (before, recovered)
                    assert recovered["state_registered"] - before["state_registered"] == 1, (before, recovered)
                    assert recovered["state_released"] - before["state_released"] == int(point == "init"), (before, recovered)
                    assert recovered["dictionary_last_lexized"] == recovered["dictionary_last_created"], recovered
                    expected_bytes = (before["dictionary_live_fst_bytes"]
                                      - int(point == "init") * before["dictionary_last_created_fst_bytes"]
                                      + recovered["dictionary_last_created_fst_bytes"])
                    assert recovered["dictionary_live_fst_bytes"] == expected_bytes, (before, recovered)
                    assert recovered["dictionary_created"] - recovered["dictionary_dropped"] == initial_live + int(not warmed), recovered
                    label["recovered"] = recovered
                    # An external catalog change during A's transaction must
                    # recover through rollback-to-savepoint in the same backend.
                    run(f"ALTER TEXT SEARCH DICTIONARY {name}(Lossy={'true' if warmed else 'false'})")
                    session.value("BEGIN; SAVEPOINT cached_init")
                    before_savepoint = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                    session.value("SELECT charabia.test_set_fault_v1(" + literal(point) + "," + literal(mode) + ")")
                    failed = session.query(sql)
                    assert "ERROR:" in failed, failed
                    session.value("ROLLBACK TO SAVEPOINT cached_init")
                    after_savepoint = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                    assert after_savepoint["dictionary_last_lexized"] == before_savepoint["dictionary_last_lexized"], after_savepoint
                    retained = point == "init"
                    assert after_savepoint["dictionary_created"] - before_savepoint["dictionary_created"] == 1, (before_savepoint, after_savepoint)
                    assert after_savepoint["dictionary_dropped"] - before_savepoint["dictionary_dropped"] == 1 + int(not retained), (before_savepoint, after_savepoint)
                    assert after_savepoint["state_registered"] - before_savepoint["state_registered"] == int(retained), (before_savepoint, after_savepoint)
                    assert after_savepoint["state_released"] - before_savepoint["state_released"] == 1, (before_savepoint, after_savepoint)
                    assert session.value(sql) == ("{cafe}" if warmed else "{CAFE\u0301}")
                    final = json.loads(session.value("SELECT charabia.test_stats_v1()"))
                    assert final["dictionary_last_lexized"] == final["dictionary_last_created"], final
                    assert final["dictionary_created"] - after_savepoint["dictionary_created"] == 1, (after_savepoint, final)
                    assert final["dictionary_dropped"] - after_savepoint["dictionary_dropped"] == int(retained), (after_savepoint, final)
                    assert final["state_registered"] - after_savepoint["state_registered"] == 1, (after_savepoint, final)
                    assert final["state_released"] - after_savepoint["state_released"] == int(retained), (after_savepoint, final)
                    assert final["dictionary_live_fst_bytes"] == final["dictionary_last_created_fst_bytes"], final
                    assert final["dictionary_created"] - final["dictionary_dropped"] == initial_live + int(not warmed), final
                    session.value("COMMIT")
                    assert session.value("SELECT 1") == "1"
                    label["savepoint"] = {"before":before_savepoint,"error":failed,"after_rollback":after_savepoint,"recovered":final}
                    save("lifecycle-results.json", results)


restore_db="pgcb_restore_"+str(os.getpid())
drop_db="pgcb_drop_"+str(os.getpid())
try:
    with Session() as warm:
        assert warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")=="'alpha':1 'beta':2"
        before_cache=json.loads(warm.value("SELECT charabia.test_stats_v1()")) if has_hooks else None
        run("ALTER TEXT SEARCH DICTIONARY lifecycle.latin(StopWords='[\"alpha\"]')")
        current=warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")
        assert current=="'beta':2" and run("SELECT to_tsvector('lifecycle.cfg','alpha beta')")==current
        results["cache"].append({"warm_backend":warm.pid,"new_value":current})
        if before_cache is not None:
            after_cache=json.loads(warm.value("SELECT charabia.test_stats_v1()"))
            assert after_cache["dictionary_created"]-before_cache["dictionary_created"]==1,(before_cache,after_cache)
            assert after_cache["dictionary_dropped"]-before_cache["dictionary_dropped"]==1,(before_cache,after_cache)
            results["cache_counters"]={"before":before_cache,"after":after_cache}
        prepared_at = prepare_queries(warm)
        config_oid = int(warm.value("SELECT 'lifecycle.cfg'::regconfig::oid"))
        prepared_state(warm, "warmed", prepared_at, True, [1], "lifecycle.cfg")
        run("BEGIN; ALTER TEXT SEARCH DICTIONARY lifecycle.latin(StopWords='[\"beta\"]'); ROLLBACK;")
        assert warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")==current
        prepared_state(warm, "alter-rollback", prepared_at, True, [1], "lifecycle.cfg")
        bad=run("ALTER TEXT SEARCH DICTIONARY lifecycle.latin(StopWords='{}')",check=False)
        assert bad.returncode and warm.value("SELECT to_tsvector('lifecycle.cfg','alpha beta')")==current
        assert candidates(warm)==[1]
        if linux: assert candidates(warm,True)==[1]
        prepared_state(warm, "invalid-alter", prepared_at, True, [1], "lifecycle.cfg")
        prepared_state(warm, "default-simple", prepared_at, True, [1], "pg_catalog.simple")
        prepared_state(warm, "default-restored", prepared_at, True, [1], "lifecycle.cfg")
        run("ALTER TEXT SEARCH DICTIONARY lifecycle.latin(Lossy=false)")
        results["changed_analysis"]={"vector":warm.value("SELECT to_tsvector('lifecycle.cfg','CAFÉ')"),"query":warm.value("SELECT plainto_tsquery('lifecycle.cfg','CAFÉ')"),"options":run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.latin'::regdictionary")}
        save("lifecycle-results.json",results)
        warm.value("SET enable_seqscan=on; SET enable_bitmapscan=off; SET enable_indexscan=off")
        assert warm.value("SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','CAFÉ')")=="1"
        warm.value("SET enable_bitmapscan=on; SET enable_indexscan=on")
        assert candidates(warm)==[],"old GIN unexpectedly rebuilt itself"
        if linux: assert candidates(warm,True)==[],"old BM25 unexpectedly rebuilt itself"
        prepared_state(warm, "alter-stale-index", prepared_at, False, [], "lifecycle.cfg")
        prepared_state(warm, "alter-stale-default-simple", prepared_at, False, [], "pg_catalog.simple")
        warm.value("SET plan_cache_mode=force_generic_plan")
        stale_literal = warm.value("EXECUTE lifecycle_literal")
        assert stale_literal == "'cafe'", stale_literal
        results["constant_literal"] = {"before_reprepare":stale_literal,"fresh_parameterized":"'CAFE\u0301'",
                                      "classification":"PostgreSQL immutable constant folding; separate from dictionary cache"}
        run("REINDEX INDEX lifecycle.lifecycle_gin")
        if linux: run("REINDEX INDEX lifecycle.lifecycle_bm25")
        assert candidates(warm)==[1]
        if linux: assert candidates(warm,True)==[1]
        prepared_state(warm, "reindexed", prepared_at, False, [1], "lifecycle.cfg")
        run("BEGIN; ALTER TEXT SEARCH DICTIONARY lifecycle.latin(Lossy=true); ROLLBACK;")
        prepared_state(warm, "lossy-alter-rollback", prepared_at, False, [1], "pg_catalog.simple")
        assert int(warm.value("SELECT 'lifecycle.cfg'::regconfig::oid")) == config_oid
        assert all(item["config_oid"] == config_oid for item in results["prepared"])
        warm.value("DEALLOCATE lifecycle_literal; PREPARE lifecycle_literal AS SELECT plainto_tsquery('lifecycle.cfg','CAFÉ')::text")
        refreshed_literal = warm.value("EXECUTE lifecycle_literal")
        assert refreshed_literal == "'CAFE\u0301'", refreshed_literal
        results["constant_literal"]["after_reprepare"] = refreshed_literal

    if has_hooks:
        cached_init_faults()

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
print(json.dumps({"status":"passed","cache_sessions":len(results["cache"]),"index_checks":len(results["indexes"]),"prepared_states":len(results["prepared"]),"prepared_index_checks":sum(len(state["queries"]) for state in results["prepared"]),"cached_init_fault_cases":len(results["cached_init_faults"]),"dump_restore":True,"drop_protection":results["drop_protection"]},ensure_ascii=False))
