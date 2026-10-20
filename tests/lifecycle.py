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
DOCUMENT = "The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running. CAFÉ."
CACHE_DOCUMENT = DOCUMENT + " alpha beta"
BASE = {"builtin_stopwords": False, "stemming": True}


def options_sql(value):
    return literal(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + "::jsonb"


def alter(name, value, session=None):
    sql = "SELECT charabia.alter_config(" + literal(name) + "," + options_sql(value) + ")"
    return session.value(sql) if session else run(sql)


def dictionary(config, language="eng"):
    return run("SELECT m.mapdict::regdictionary::text FROM pg_ts_config_map m "
               "JOIN ts_token_type('charabia.parser') t ON t.tokid=m.maptokentype "
               "WHERE m.mapcfg=" + literal(config) + "::regconfig AND t.alias=" + literal(language))


run("CREATE SCHEMA lifecycle")
run("SELECT charabia.create_config('lifecycle.cfg'," + options_sql(BASE) + ")")
run("SELECT charabia.create_config('lifecycle.cache_cfg'," + options_sql({"builtin_stopwords":False,"stemming":False}) + ")")
run("CREATE TABLE lifecycle.docs(id integer PRIMARY KEY,body text NOT NULL); INSERT INTO lifecycle.docs VALUES(1," + literal(DOCUMENT) + "),(2,'unrelated'); CREATE INDEX lifecycle_gin ON lifecycle.docs USING gin(to_tsvector('lifecycle.cfg',body))")
if linux:
    run("CREATE INDEX lifecycle_bm25 ON lifecycle.docs USING bm25(body) WITH(text_config='lifecycle.cfg')")
selected_languages = json_query("SELECT json_agg(DISTINCT language) FROM charabia.analyze(" + literal(DOCUMENT) + ",'lifecycle.cfg') WHERE kind='word'")
results["language_control"] = {"document":DOCUMENT,"languages":selected_languages,"basis":"Preserved English corpus plus upstream English running->run golden"}
save("lifecycle-results.json",results)
assert selected_languages == ["eng"], selected_languages

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
        query = "SELECT id FROM lifecycle.docs ORDER BY body <@> to_bm25query('run','lifecycle.lifecycle_bm25') LIMIT 10"
        index = "lifecycle_bm25"
    else:
        query = "SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','run')"
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
        session.value(f"PREPARE lifecycle_analysis_{suffix}(text) AS SELECT jsonb_build_object("
                      "'vector',to_tsvector('lifecycle.cfg',$1)::text,"
                      "'query',plainto_tsquery('lifecycle.cfg',$1)::text,"
                      "'lexemes',tsvector_to_array(to_tsvector('lifecycle.cfg',$1)),"
                      "'default_vector',to_tsvector($1)::text,"
                      "'default_config',current_setting('default_text_search_config'))")
        for bm25 in (False, True) if linux else (False,):
            family = "bm25" if bm25 else "gin"
            ranked = ("SELECT id FROM lifecycle.docs ORDER BY body <@> to_bm25query($1,'lifecycle.lifecycle_bm25') LIMIT 10") if bm25 else ("SELECT id FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg',$1)")
            session.value(f"PREPARE lifecycle_{family}_{suffix}(text) AS SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM ({ranked}) q")
    session.value("PREPARE lifecycle_literal AS SELECT plainto_tsquery('lifecycle.cfg'," + literal(DOCUMENT) + ")::text")
    session.value("SET plan_cache_mode=force_generic_plan")
    assert "'run'" in session.value("EXECUTE lifecycle_literal")
    return json.loads(session.value("SELECT jsonb_object_agg(name,prepare_time) FROM pg_prepared_statements"))


def prepared_state(session, label, prepared_at, stemming, expected_ids, default_config):
    record = {"state":label,"pid":session.pid,"stemming":stemming,"expected_index_ids":expected_ids,
              "config_oid":int(session.value("SELECT 'lifecycle.cfg'::regconfig::oid")),
              "settings":json.loads(session.value("SELECT charabia.analysis_info('lifecycle.cfg')->'settings'")),"queries":[]}
    results["prepared"].append(record)
    save("lifecycle-results.json", results)
    session.value("SET default_text_search_config=" + literal(default_config))
    simple = session.value("SELECT to_tsvector('pg_catalog.simple'," + literal(DOCUMENT) + ")::text")
    session.value("SET enable_seqscan=on; SET enable_bitmapscan=off; SET enable_indexscan=off")
    oracle_sql = "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM lifecycle.docs WHERE to_tsvector('lifecycle.cfg',body) @@ plainto_tsquery('lifecycle.cfg','run')"
    record["native_oracle_plan"] = json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + oracle_sql))
    assert any(node.get("Node Type") == "Seq Scan" and node.get("Actual Loops", 0) > 0 for node in plan_nodes(record["native_oracle_plan"])), record
    record["native_oracle_ids"] = json.loads(session.value(oracle_sql))
    assert record["native_oracle_ids"] == ([1] if stemming else []), record
    for suffix, mode in (("custom", "force_custom_plan"), ("generic", "force_generic_plan")):
        session.value("SET plan_cache_mode=" + mode)
        analysis = json.loads(session.value(f"EXECUTE lifecycle_analysis_{suffix}(" + literal(DOCUMENT) + ")"))
        record.setdefault("analysis", {})[suffix] = analysis
        assert "running" in analysis["lexemes"] and (("run" in analysis["lexemes"]) == stemming), analysis
        assert (("'run'" in analysis["query"]) == stemming), analysis
        assert analysis["default_config"] == default_config, analysis
        assert analysis["vector"] != simple, (analysis, simple)
        assert analysis["default_vector"] == (simple if default_config == "pg_catalog.simple" else analysis["vector"]), analysis
        for bm25 in (False, True) if linux else (False,):
            family = "bm25" if bm25 else "gin"
            session.value("SET enable_seqscan=off; SET enable_bitmapscan=on; SET enable_indexscan=" + ("on" if bm25 else "off"))
            execute = f"EXECUTE lifecycle_{family}_{suffix}('run')"
            plan = json.loads(session.value("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + execute))
            target_plan(plan, "lifecycle_" + family, bm25)
            ids = json.loads(session.value(execute))
            record["queries"].append({"mode":mode,"index":"lifecycle_"+family,"execute":execute,"plan":plan,"ids":ids})
            save("lifecycle-results.json", results)
            assert ids == expected_ids, record["queries"][-1]
    stats = json.loads(session.value("SELECT jsonb_agg(jsonb_build_object('name',name,'prepare_time',prepare_time,'generic_plans',generic_plans,'custom_plans',custom_plans) ORDER BY name) FROM pg_prepared_statements"))
    record["prepared_statements"] = stats
    for stat in stats:
        assert stat["prepare_time"] == prepared_at[stat["name"]], stat
        if stat["name"].endswith("_custom"):
            assert stat["custom_plans"] > 0 and stat["generic_plans"] == 0, stat
        elif stat["name"].endswith("_generic"):
            assert stat["generic_plans"] > 0 and stat["custom_plans"] == 0, stat
    if linux:
        record["bm25_reloptions"] = json.loads(session.value("SELECT to_json(reloptions) FROM pg_class WHERE oid='lifecycle.lifecycle_bm25'::regclass"))
        assert "text_config=lifecycle.cfg" in record["bm25_reloptions"]
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
                config_name = name
                run("SELECT charabia.create_config(" + literal(config_name) + "," + options_sql({**BASE,"stopwords_add":{"eng":["canary"]}}) + ")")
                name = dictionary(config_name)
                with Session() as session:
                    sql = "SELECT ts_lexize(" + literal(name) + ",'running')"
                    if warmed:
                        assert session.value(sql) == "{running,run}"
                        alter(config_name, {**BASE,"stemming":False,"stopwords_add":{"eng":["canary"]}})
                    label = {"warmed":warmed,"point":point,"mode":mode,"pid":session.pid,"attempts":[]}
                    results["cached_init_faults"].append(label)
                    before = json.loads(session.value("SELECT charabia.test_stats()"))
                    initial_live = before["dictionary_created"] - before["dictionary_dropped"]
                    for attempt in range(2):
                        session.value("SELECT charabia.test_set_fault(" + literal(point) + "," + literal(mode) + ")")
                        output = session.query(sql)
                        after = json.loads(session.value("SELECT charabia.test_stats()"))
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
                    assert session.value(sql) == ("{running}" if warmed else "{running,run}")
                    recovered = json.loads(session.value("SELECT charabia.test_stats()"))
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
                    alter(config_name, {**BASE,"stemming":warmed,"stopwords_add":{"eng":["canary"]}})
                    session.value("BEGIN; SAVEPOINT cached_init")
                    before_savepoint = json.loads(session.value("SELECT charabia.test_stats()"))
                    session.value("SELECT charabia.test_set_fault(" + literal(point) + "," + literal(mode) + ")")
                    failed = session.query(sql)
                    assert "ERROR:" in failed, failed
                    session.value("ROLLBACK TO SAVEPOINT cached_init")
                    after_savepoint = json.loads(session.value("SELECT charabia.test_stats()"))
                    assert after_savepoint["dictionary_last_lexized"] == before_savepoint["dictionary_last_lexized"], after_savepoint
                    retained = point == "init"
                    assert after_savepoint["dictionary_created"] - before_savepoint["dictionary_created"] == 1, (before_savepoint, after_savepoint)
                    assert after_savepoint["dictionary_dropped"] - before_savepoint["dictionary_dropped"] == 1 + int(not retained), (before_savepoint, after_savepoint)
                    assert after_savepoint["state_registered"] - before_savepoint["state_registered"] == int(retained), (before_savepoint, after_savepoint)
                    assert after_savepoint["state_released"] - before_savepoint["state_released"] == 1, (before_savepoint, after_savepoint)
                    assert session.value(sql) == ("{running,run}" if warmed else "{running}")
                    final = json.loads(session.value("SELECT charabia.test_stats()"))
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
                run("SELECT charabia.drop_config(" + literal(config_name) + ")")


restore_db="pgcb_restore_"+str(os.getpid())
drop_db="pgcb_drop_"+str(os.getpid())
try:
    with Session() as warm:
        cache_sql = "SELECT tsvector_to_array(to_tsvector('lifecycle.cache_cfg'," + literal(CACHE_DOCUMENT) + "))::text"
        before_terms = warm.value(cache_sql)
        assert "alpha" in before_terms and "beta" in before_terms
        before_cache = json.loads(warm.value("SELECT charabia.test_stats()")) if has_hooks else None
        cache_options = {"builtin_stopwords":False,"stemming":False,"stopwords_add":{"eng":["alpha"]}}
        alter("lifecycle.cache_cfg", cache_options)
        current = warm.value(cache_sql)
        assert "alpha" not in current and "beta" in current and run(cache_sql) == current
        results["cache"].append({"warm_backend":warm.pid,"new_value":current})
        if before_cache is not None:
            after_cache = json.loads(warm.value("SELECT charabia.test_stats()"))
            assert after_cache["dictionary_created"]-before_cache["dictionary_created"] == 1, (before_cache,after_cache)
            assert after_cache["dictionary_dropped"]-before_cache["dictionary_dropped"] == 1, (before_cache,after_cache)
            results["cache_counters"] = {"before":before_cache,"after":after_cache}
        prepared_at = prepare_queries(warm)
        config_oid = int(warm.value("SELECT 'lifecycle.cfg'::regconfig::oid"))
        prepared_state(warm,"warmed",prepared_at,True,[1],"lifecycle.cfg")
        run("BEGIN; SELECT charabia.alter_config('lifecycle.cfg'," + options_sql({**BASE,"stemming":False}) + "); ROLLBACK")
        prepared_state(warm,"alter-rollback",prepared_at,True,[1],"lifecycle.cfg")
        bad = run("SELECT charabia.alter_config('lifecycle.cfg','{\"stemming\":0}')",check=False)
        assert bad.returncode
        prepared_state(warm,"invalid-alter",prepared_at,True,[1],"lifecycle.cfg")
        prepared_state(warm,"default-simple",prepared_at,True,[1],"pg_catalog.simple")
        prepared_state(warm,"default-restored",prepared_at,True,[1],"lifecycle.cfg")
        alter("lifecycle.cfg",{**BASE,"stemming":False})
        # The old postings retain run from English stemming. Heap analysis no
        # longer has run; both index families remain stale until REINDEX.
        prepared_state(warm,"alter-stale-index",prepared_at,False,[1],"lifecycle.cfg")
        prepared_state(warm,"alter-stale-default-simple",prepared_at,False,[1],"pg_catalog.simple")
        warm.value("SET plan_cache_mode=force_generic_plan")
        stale_literal = warm.value("EXECUTE lifecycle_literal")
        assert "'run'" in stale_literal, stale_literal
        results["constant_literal"] = {"before_reprepare":stale_literal,"classification":"PostgreSQL immutable constant folding; separate from dictionary cache"}
        run("REINDEX INDEX lifecycle.lifecycle_gin")
        if linux:
            run("REINDEX INDEX lifecycle.lifecycle_bm25")
        prepared_state(warm,"reindexed",prepared_at,False,[],"lifecycle.cfg")
        run("BEGIN; SELECT charabia.alter_config('lifecycle.cfg'," + options_sql(BASE) + "); ROLLBACK")
        prepared_state(warm,"stem-alter-rollback",prepared_at,False,[],"pg_catalog.simple")
        assert all(item["config_oid"] == config_oid for item in results["prepared"])
        warm.value("DEALLOCATE lifecycle_literal; PREPARE lifecycle_literal AS SELECT plainto_tsquery('lifecycle.cfg'," + literal(DOCUMENT) + ")::text")
        refreshed = warm.value("EXECUTE lifecycle_literal")
        assert "'run'" not in refreshed, refreshed
        results["constant_literal"]["after_reprepare"] = refreshed
    if has_hooks:
        cached_init_faults()

    roundtrip_options = {"stemming":False,"builtin_stopwords":False,"stopwords_add":{"eng":["the","O'Reilly","C:\\code","e\u0301"],"cmn":["的"]},"stopwords_keep":{"eng":["will"]}}
    run("SELECT charabia.create_config('lifecycle.roundtrip'," + options_sql(roundtrip_options) + ")")
    original_info = json_query("SELECT charabia.analysis_info('lifecycle.roundtrip')")
    run(f"CREATE DATABASE {restore_db} TEMPLATE template0")
    try:
        run("CREATE EXTENSION pg_charabia",restore_db)
        if linux:
            run("CREATE EXTENSION pg_textsearch",restore_db)
        dump = EVIDENCE / "lifecycle-schema.sql"
        with dump.open("w") as output:
            subprocess.run([str(Path(PSQL).with_name("pg_dump")),"--schema-only","--no-owner","--no-privileges","--schema=lifecycle"],stdout=output,check=True)
        restored = subprocess.run([PSQL,"-X","-v","ON_ERROR_STOP=1","-d",restore_db,"-f",str(dump)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        assert restored.returncode == 0, restored.stderr
        restored_info = json.loads(run("SELECT charabia.analysis_info('lifecycle.roundtrip')",restore_db))
        assert original_info["configuration_oid"] != restored_info["configuration_oid"], (original_info,restored_info)
        for field in ("settings","settings_fingerprint","analysis_fingerprint"):
            assert original_info[field] == restored_info[field], (field,original_info,restored_info)
        for word in ("the","O'Reilly","C:\\code","e\u0301","的","will"):
            sql = "SELECT to_tsvector('lifecycle.roundtrip'," + literal(word) + ")"
            assert run(sql,restore_db) == run(sql), repr(word)
        run("SELECT charabia.alter_config('lifecycle.roundtrip','{\"stemming\":false,\"builtin_stopwords\":false}'); SELECT charabia.drop_config('lifecycle.roundtrip')",restore_db)
        assert run("SELECT count(*) FROM pg_ts_config WHERE cfgnamespace='lifecycle'::regnamespace AND cfgname='roundtrip'",restore_db) == "0"
        results["dump_restore"] = {"options":roundtrip_options,"fingerprint":original_info["settings_fingerprint"],"schema_sha256":hashlib.sha256(dump.read_bytes()).hexdigest(),"restored_alter_drop":True}
    finally:
        run(f"DROP DATABASE {restore_db}")

    if linux:
        run("SELECT charabia.create_config('lifecycle.droppable'); CREATE TABLE lifecycle.dropdocs(body text); INSERT INTO lifecycle.dropdocs VALUES('hello'); CREATE INDEX droppable_bm25 ON lifecycle.dropdocs USING bm25(body) WITH(text_config='lifecycle.droppable')")
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
            run("CREATE EXTENSION pg_charabia; CREATE EXTENSION pg_textsearch; CREATE TABLE docs(body text); INSERT INTO docs VALUES('hello'); CREATE INDEX docs_bm25 ON docs USING bm25(body) WITH(text_config='public.charabia')",drop_db)
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
