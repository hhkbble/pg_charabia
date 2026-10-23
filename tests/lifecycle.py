"""Actual catalog invalidation, stale indexes, rebuild and dump/restore."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from support import EVIDENCE, PSQL, Session, json_query, literal, native_config_sql, run, save

linux = os.environ["PGCB_PLATFORM"] == "linux-amd64"
if linux:
    assert run("SELECT count(*) FROM pg_extension WHERE extname='pg_textsearch'")=="1"
results = {"platform":os.environ["PGCB_PLATFORM"],"cache":[],"indexes":[],"prepared":[],"cached_init_faults":[],"drop_protection":{}}
has_hooks = os.environ.get("PGCB_TEST_HOOKS") == "1"
DOCUMENT = "The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running. CAFÉ."
CACHE_DOCUMENT = DOCUMENT + " alpha beta the"


def alter_dictionary(name, value, session=None):
    options = []
    for key, flag in value.items():
        if key not in ("language", "discard", "stem", "normalize", "filter"):
            raise ValueError("Unexpected native dictionary option: " + key)
        options.append(key + "=" + (literal(flag) if key == "language" else "true" if flag else "false"))
    sql = "ALTER TEXT SEARCH DICTIONARY " + name + " (" + ",".join(options) + ")"
    return session.value(sql) if session else run(sql)


def alter(name, value, session=None):
    return alter_dictionary(dictionary(name), value, session)


def dictionary(config, language="eng"):
    return run("SELECT m.mapdict::regdictionary::text FROM pg_ts_config_map m "
               "JOIN ts_token_type('charabia.parser') t ON t.tokid=m.maptokentype "
               "WHERE m.mapcfg=" + literal(config) + "::regconfig AND t.alias=" + literal(language))


run("CREATE SCHEMA lifecycle")
run(native_config_sql("lifecycle.cfg", stem=True, discard=False))
run(native_config_sql("lifecycle.cache_cfg", stem=False, discard=False))
run("CREATE TABLE lifecycle.docs(id integer PRIMARY KEY,body text NOT NULL); INSERT INTO lifecycle.docs VALUES(1," + literal(DOCUMENT) + "),(2,'unrelated'); CREATE INDEX lifecycle_gin ON lifecycle.docs USING gin(to_tsvector('lifecycle.cfg',body))")
if linux:
    run("CREATE INDEX lifecycle_bm25 ON lifecycle.docs USING bm25(body) WITH(text_config='lifecycle.cfg')")
selected_languages = json_query("SELECT json_agg(DISTINCT language) FROM charabia.analyze(" + literal(DOCUMENT) + ") WHERE kind='word'")
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
              "dictionary_options":session.value("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.cfg_eng'::regdictionary"),"queries":[]}
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
                run("CREATE TEXT SEARCH DICTIONARY " + name + " (TEMPLATE=charabia.template,Language='eng',Discard=true,Stem=true,Normalize=true,Filter=false)")
                with Session() as session:
                    sql = "SELECT ts_lexize(" + literal(name) + ",'running')"
                    if warmed:
                        assert session.value(sql) == "{running,run}"
                        alter_dictionary(name, {"stem":False})
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
                        assert after["dictionary_live_fst_bytes"] == 0, after
                        retained = point == "init"
                        previous_retained = (warmed if attempt == 0 else retained)
                        assert after["dictionary_dropped"] - before["dictionary_dropped"] == int(previous_retained) + int(not retained), (before, after)
                        assert after["state_registered"] - before["state_registered"] == int(retained), (before, after)
                        assert after["state_released"] - before["state_released"] == int(previous_retained), (before, after)
                        assert after["dictionary_created"] - after["dictionary_dropped"] == initial_live - int(warmed) + int(retained), after
                        assert after["dictionary_last_referenced_fst_bytes"] > 0, after
                        expected_bytes = (before["dictionary_live_referenced_fst_bytes"]
                                          - int(previous_retained) * before["dictionary_last_referenced_fst_bytes"]
                                          + int(retained) * after["dictionary_last_referenced_fst_bytes"])
                        assert after["dictionary_live_referenced_fst_bytes"] == expected_bytes, (before, after)
                        assert session.value("SELECT 1") == "1"
                        before = after
                    assert session.value(sql) == ("{running}" if warmed else "{running,run}")
                    recovered = json.loads(session.value("SELECT charabia.test_stats()"))
                    assert recovered["dictionary_created"] - before["dictionary_created"] == 1, (before, recovered)
                    assert recovered["dictionary_dropped"] - before["dictionary_dropped"] == int(point == "init"), (before, recovered)
                    assert recovered["state_registered"] - before["state_registered"] == 1, (before, recovered)
                    assert recovered["state_released"] - before["state_released"] == int(point == "init"), (before, recovered)
                    assert recovered["dictionary_last_lexized"] == recovered["dictionary_last_created"], recovered
                    expected_bytes = (before["dictionary_live_referenced_fst_bytes"]
                                      - int(point == "init") * before["dictionary_last_referenced_fst_bytes"]
                                      + recovered["dictionary_last_referenced_fst_bytes"])
                    assert recovered["dictionary_live_referenced_fst_bytes"] == expected_bytes, (before, recovered)
                    assert recovered["dictionary_created"] - recovered["dictionary_dropped"] == initial_live + int(not warmed), recovered
                    label["recovered"] = recovered
                    # An external catalog change during A's transaction must
                    # recover through rollback-to-savepoint in the same backend.
                    alter_dictionary(name, {"stem":warmed})
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
                    assert final["dictionary_live_referenced_fst_bytes"] == final["dictionary_last_referenced_fst_bytes"], final
                    assert final["dictionary_created"] - final["dictionary_dropped"] == initial_live + int(not warmed), final
                    session.value("COMMIT")
                    assert session.value("SELECT 1") == "1"
                    label["savepoint"] = {"before":before_savepoint,"error":failed,"after_rollback":after_savepoint,"recovered":final}
                    save("lifecycle-results.json", results)
                run("DROP TEXT SEARCH DICTIONARY " + name)


def parallel_cached_init_faults():
    """Complement the exhaustive native faults with the new two-branch state."""
    manifest = json.loads((Path(__file__).resolve().parent.parent / "resources/manifest.json").read_text())
    native_bytes = manifest["compiled_stop_sets"]["tur"]["fst_bytes"]
    both_bytes = native_bytes + manifest["compiled_stop_sets"]["eng"]["fst_bytes"]
    four = ["abbrevıate", "abbrevıa", "abbreviate", "abbrevi"]
    folded = ["abbreviate", "abbrevia", "abbrevi"]
    results["parallel_cached_init_faults"] = []
    for warmed, point, mode in ((False,"register_alloc","allocation_error"),
                                (False,"init","panic"),
                                (True,"register_link","error"),
                                (True,"init","error")):
        name = "lifecycle.parallel_failure_" + str(int(warmed)) + "_" + point
        run("CREATE TEXT SEARCH DICTIONARY " + name + "(TEMPLATE=charabia.template,"
            "Language='tur',Discard=true,Stem=true,Normalize=false,Filter=false,English=true)")
        try:
            with Session() as session:
                sql = "SELECT to_json(ts_lexize(" + literal(name) + ",'ABBREVIATE'))"
                if warmed:
                    assert json.loads(session.value(sql)) == four
                    # Preserve both stemmers and both stop references while
                    # invalidating a warmed cache entry from another backend.
                    run("ALTER TEXT SEARCH DICTIONARY " + name + "(Normalize=true)")
                before = json.loads(session.value("SELECT charabia.test_stats()"))
                session.value("SELECT charabia.test_set_fault(" + literal(point) + "," + literal(mode) + ")")
                output = session.query(sql)
                after = json.loads(session.value("SELECT charabia.test_stats()"))
                record = {"warmed":warmed,"point":point,"mode":mode,"pid":session.pid,
                          "before":before,"error":output,"after":after,"expected_referenced_bytes":both_bytes}
                results["parallel_cached_init_faults"].append(record)
                save("lifecycle-results.json", results)
                retained = point == "init"
                assert "ERROR:" in output and "XX000" in output, record
                assert ("invalid memory alloc request size" if mode == "allocation_error" else "injected pg_charabia") in output, record
                assert after["dictionary_created"] - before["dictionary_created"] == 1, record
                assert after["dictionary_dropped"] - before["dictionary_dropped"] == int(warmed) + int(not retained), record
                assert after["state_registered"] - before["state_registered"] == int(retained), record
                assert after["state_released"] - before["state_released"] == int(warmed), record
                assert after["dictionary_last_lexized"] == before["dictionary_last_lexized"], record
                assert after["dictionary_last_created"] != after["dictionary_last_lexized"], record
                assert after["dictionary_live_fst_bytes"] == 0 and after["dictionary_last_created_fst_bytes"] == 0, record
                assert after["dictionary_last_referenced_fst_bytes"] == both_bytes, record
                assert after["dictionary_live_referenced_fst_bytes"] == (
                    before["dictionary_live_referenced_fst_bytes"] - int(warmed) * both_bytes + int(retained) * both_bytes), record
                assert session.value("SELECT 1") == "1"
                actual = json.loads(session.value(sql))
                expected = folded if warmed else four
                recovered = json.loads(session.value("SELECT charabia.test_stats()"))
                record.update(recovered=recovered,recovered_terms=actual)
                save("lifecycle-results.json", results)
                assert actual == expected, record
                assert recovered["dictionary_created"] - after["dictionary_created"] == 1, record
                assert recovered["dictionary_dropped"] - after["dictionary_dropped"] == int(retained), record
                assert recovered["state_registered"] - after["state_registered"] == 1, record
                assert recovered["state_released"] - after["state_released"] == int(retained), record
                assert recovered["dictionary_last_lexized"] == recovered["dictionary_last_created"], record
                assert recovered["dictionary_created"] - recovered["dictionary_dropped"] == 1, record
                assert recovered["dictionary_live_fst_bytes"] == 0 and recovered["dictionary_live_referenced_fst_bytes"] == both_bytes, record
                record["alter_states"] = []
                for enabled in (False, True):
                    run("ALTER TEXT SEARCH DICTIONARY " + name + "(English=" + ("true" if enabled else "false") + ")")
                    current = json.loads(session.value(sql))
                    counters = json.loads(session.value("SELECT charabia.test_stats()"))
                    state = {"english":enabled,"terms":current,"counters":counters}
                    record["alter_states"].append(state)
                    save("lifecycle-results.json", results)
                    native_terms = ["abbreviate","abbrevia"] if warmed else ["abbrevıate","abbrevıa"]
                    assert current == (expected if enabled else native_terms), state
                    assert counters["dictionary_created"] - recovered["dictionary_created"] == 1, state
                    assert counters["dictionary_dropped"] - recovered["dictionary_dropped"] == 1, state
                    assert counters["state_registered"] - recovered["state_registered"] == 1, state
                    assert counters["state_released"] - recovered["state_released"] == 1, state
                    assert counters["dictionary_last_lexized"] == counters["dictionary_last_created"], state
                    assert counters["dictionary_live_fst_bytes"] == 0, state
                    assert counters["dictionary_live_referenced_fst_bytes"] == (both_bytes if enabled else native_bytes), state
                    assert counters["dictionary_created"] - counters["dictionary_dropped"] == 1, state
                    recovered = counters
        finally:
            run("DROP TEXT SEARCH DICTIONARY " + name)


def symbol_prepared_lifecycle():
    """Real symbol INIT invalidation without repreparing parameterized plans."""
    config = "lifecycle.symbol_config"
    dictionary_name = "lifecycle.symbol_words"
    run("CREATE TEXT SEARCH DICTIONARY " + dictionary_name + "("
        "TEMPLATE=charabia.symbol_template,Kind='numword',Discard=true,Stem=true,Normalize=true);"
        "CREATE TEXT SEARCH CONFIGURATION " + config + "(COPY=public.charabia);"
        "ALTER TEXT SEARCH CONFIGURATION " + config + " ALTER MAPPING FOR numword WITH " + dictionary_name + ";"
        "CREATE TABLE lifecycle.symbol_docs(id int PRIMARY KEY,body text NOT NULL);"
        "INSERT INTO lifecycle.symbol_docs VALUES(1,'Running100'),(2,'Other200');"
        "CREATE INDEX symbol_lifecycle_gin ON lifecycle.symbol_docs USING gin(to_tsvector('" + config + "',body))")
    results["symbol_prepared"] = []
    with Session() as warm, Session() as updater:
        for suffix in ("custom", "generic"):
            warm.value("PREPARE symbol_analysis_" + suffix + "(text) AS SELECT jsonb_build_object("
                "'terms',tsvector_to_array(to_tsvector('" + config + "',$1)),"
                "'query',plainto_tsquery('" + config + "',$1)::text)")
            warm.value("PREPARE symbol_index_" + suffix + "(text) AS SELECT coalesce(jsonb_agg(id ORDER BY id),'[]') "
                "FROM lifecycle.symbol_docs WHERE to_tsvector('" + config + "',body) @@ $1::tsquery")
        prepared = json.loads(warm.value("SELECT jsonb_object_agg(name,prepare_time) FROM pg_prepared_statements"))

        def state(label, stem, expected_ids):
            record = {"state": label, "stem": stem, "expected_ids": expected_ids,
                      "config_oid": warm.value("SELECT '" + config + "'::regconfig::oid"), "plans": []}
            results["symbol_prepared"].append(record)
            save("lifecycle-results.json", results)
            for suffix, mode in (("custom", "force_custom_plan"), ("generic", "force_generic_plan")):
                warm.value("SET plan_cache_mode=" + mode)
                execute = "EXECUTE symbol_analysis_" + suffix + "('Running100')"
                analysis = json.loads(warm.value(execute))
                item = {"mode": mode, "execute": execute, "analysis": analysis}
                record["plans"].append(item)
                save("lifecycle-results.json", results)
                assert "Running100" in analysis["terms"] and "running100" in analysis["terms"], item
                assert ("run" in analysis["terms"]) == stem and (("'run'" in analysis["query"]) == stem), item
                warm.value("SET enable_seqscan=off;SET enable_indexscan=off;SET enable_indexonlyscan=off;SET enable_bitmapscan=on")
                index_sql = "EXECUTE symbol_index_" + suffix + "('run')"
                plan = json.loads(warm.value("EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF,SUMMARY OFF) " + index_sql))
                actual = json.loads(warm.value(index_sql))
                item.update(index_sql=index_sql, plan=plan, ids=actual)
                save("lifecycle-results.json", results)
                assert any(node.get("Index Name") == "symbol_lifecycle_gin" and node.get("Actual Loops", 0) > 0
                           for node in plan_nodes(plan)), item
                assert actual == expected_ids, item
            current = json.loads(warm.value("SELECT jsonb_agg(jsonb_build_object('name',name,'prepare_time',prepare_time,"
                "'custom',custom_plans,'generic',generic_plans)) FROM pg_prepared_statements"))
            record["prepared_statements"] = current
            save("lifecycle-results.json", results)
            for item in current:
                assert item["prepare_time"] == prepared[item["name"]], item
                assert item["custom" if item["name"].endswith("_custom") else "generic"] > 0, item

        state("warm", True, [1])
        updater.value("BEGIN;ALTER TEXT SEARCH DICTIONARY " + dictionary_name + "(Stem=false);ROLLBACK")
        state("rolled-back-alter", True, [1])
        updater.value("ALTER TEXT SEARCH DICTIONARY " + dictionary_name + "(Stem=false)")
        state("committed-alter-stale-postings", False, [1])
        updater.value("REINDEX INDEX lifecycle.symbol_lifecycle_gin")
        state("reindexed", False, [])
        updater.value("ALTER TEXT SEARCH DICTIONARY " + dictionary_name + "(Stem)")
        updater.value("REINDEX INDEX lifecycle.symbol_lifecycle_gin")
        state("removed-option-default-restored", True, [1])
        assert len({item["config_oid"] for item in results["symbol_prepared"]}) == 1


def symbol_cached_init_faults():
    """Generic dictionary counters also own the new exact symbol state type."""
    results["symbol_cached_init_faults"] = []
    manifest = json.loads((Path(__file__).resolve().parent.parent / "resources/manifest.json").read_text())
    fst_bytes = manifest["compiled_stop_sets"]["eng"]["fst_bytes"]
    for warmed, point, mode in ((False, "register_alloc", "allocation_error"),
                                (False, "init", "panic"),
                                (True, "register_link", "error"),
                                (True, "init", "error")):
        name = "lifecycle.symbol_failure_" + str(int(warmed)) + "_" + point
        run("CREATE TEXT SEARCH DICTIONARY " + name + "(TEMPLATE=charabia.symbol_template,"
            "Kind='numword',Discard=true,Stem=true,Normalize=true)")
        try:
            with Session() as session:
                statement = "SELECT to_json(ts_lexize(" + literal(name) + ",'Running100'))"
                if warmed:
                    assert "run" in json.loads(session.value(statement))
                    run("ALTER TEXT SEARCH DICTIONARY " + name + "(Stem=false)")
                record = {"warmed": warmed, "point": point, "mode": mode, "sql": statement,
                          "pid": session.pid, "attempts": [], "fst_bytes": fst_bytes}
                results["symbol_cached_init_faults"].append(record)
                before = json.loads(session.value("SELECT charabia.test_stats()"))
                retained = point == "init"
                for attempt in range(2):
                    session.value("SELECT charabia.test_set_fault(" + literal(point) + "," + literal(mode) + ")")
                    output = session.query(statement)
                    after = json.loads(session.value("SELECT charabia.test_stats()"))
                    item = {"attempt": attempt, "before": before, "after": after, "error": output}
                    record["attempts"].append(item)
                    save("lifecycle-results.json", results)
                    previous = warmed if attempt == 0 else retained
                    assert "ERROR:" in output and "XX000" in output, item
                    assert ("invalid memory alloc request size" if mode == "allocation_error" else "injected pg_charabia") in output, item
                    assert after["dictionary_created"] - before["dictionary_created"] == 1, item
                    assert after["dictionary_dropped"] - before["dictionary_dropped"] == int(previous) + int(not retained), item
                    assert after["state_registered"] - before["state_registered"] == int(retained), item
                    assert after["state_released"] - before["state_released"] == int(previous), item
                    assert after["dictionary_last_lexized"] == before["dictionary_last_lexized"], item
                    assert after["dictionary_last_created"] != after["dictionary_last_lexized"], item
                    assert after["dictionary_live_fst_bytes"] == 0 and after["dictionary_last_referenced_fst_bytes"] == fst_bytes, item
                    assert after["dictionary_live_referenced_fst_bytes"] == int(retained) * fst_bytes, item
                    assert after["dictionary_created"] - after["dictionary_dropped"] == int(retained), item
                    assert session.value("SELECT 1") == "1"
                    before = after
                actual = json.loads(session.value(statement))
                recovered = json.loads(session.value("SELECT charabia.test_stats()"))
                record.update(recovered=recovered, terms=actual)
                save("lifecycle-results.json", results)
                assert "Running100" in actual and (("run" in actual) == (not warmed)), record
                assert recovered["dictionary_created"] - before["dictionary_created"] == 1, record
                assert recovered["dictionary_dropped"] - before["dictionary_dropped"] == int(retained), record
                assert recovered["state_registered"] - before["state_registered"] == 1, record
                assert recovered["state_released"] - before["state_released"] == int(retained), record
                assert recovered["dictionary_last_created"] == recovered["dictionary_last_lexized"], record
                assert recovered["dictionary_live_referenced_fst_bytes"] == fst_bytes, record
                run("ALTER TEXT SEARCH DICTIONARY " + name + "(Stem=" + ("true" if warmed else "false") + ")")
                session.value("BEGIN;SAVEPOINT symbol_init")
                before = json.loads(session.value("SELECT charabia.test_stats()"))
                session.value("SELECT charabia.test_set_fault(" + literal(point) + "," + literal(mode) + ")")
                output = session.query(statement)
                session.value("ROLLBACK TO SAVEPOINT symbol_init")
                after = json.loads(session.value("SELECT charabia.test_stats()"))
                record["savepoint"] = {"before": before, "error": output, "after_rollback": after}
                save("lifecycle-results.json", results)
                assert "ERROR:" in output and after["dictionary_last_lexized"] == before["dictionary_last_lexized"], record
                assert after["dictionary_created"] - before["dictionary_created"] == 1, record
                assert after["dictionary_dropped"] - before["dictionary_dropped"] == 1 + int(not retained), record
                assert after["state_registered"] - before["state_registered"] == int(retained), record
                assert after["state_released"] - before["state_released"] == 1, record
                actual = json.loads(session.value(statement))
                final = json.loads(session.value("SELECT charabia.test_stats()"))
                record["savepoint"].update(recovered=final, terms=actual)
                save("lifecycle-results.json", results)
                assert (("run" in actual) == warmed) and "Running100" in actual, record
                assert final["dictionary_created"] - after["dictionary_created"] == 1, record
                assert final["dictionary_dropped"] - after["dictionary_dropped"] == int(retained), record
                assert final["state_registered"] - after["state_registered"] == 1, record
                assert final["state_released"] - after["state_released"] == int(retained), record
                assert final["dictionary_created"] - final["dictionary_dropped"] == 1, record
                assert final["dictionary_live_fst_bytes"] == 0 and final["dictionary_live_referenced_fst_bytes"] == fst_bytes, record
                session.value("COMMIT")
        finally:
            run("DROP TEXT SEARCH DICTIONARY " + name)


restore_db="pgcb_restore_"+str(os.getpid())
drop_db="pgcb_drop_"+str(os.getpid())
try:
    with Session() as warm:
        cache_sql = "SELECT to_json(tsvector_to_array(to_tsvector('lifecycle.cache_cfg'," + literal(CACHE_DOCUMENT) + ")))"
        before_terms = json.loads(warm.value(cache_sql))
        assert "alpha" in before_terms and "beta" in before_terms and "the" in before_terms
        before_cache = json.loads(warm.value("SELECT charabia.test_stats()")) if has_hooks else None
        cache_options = {"discard":True}
        alter("lifecycle.cache_cfg", cache_options)
        current = json.loads(warm.value(cache_sql))
        assert "the" not in current and "alpha" in current and "beta" in current and json.loads(run(cache_sql)) == current
        results["cache"].append({"warm_backend":warm.pid,"new_value":current})
        if before_cache is not None:
            after_cache = json.loads(warm.value("SELECT charabia.test_stats()"))
            assert after_cache["dictionary_created"]-before_cache["dictionary_created"] == 1, (before_cache,after_cache)
            assert after_cache["dictionary_dropped"]-before_cache["dictionary_dropped"] == 1, (before_cache,after_cache)
            results["cache_counters"] = {"before":before_cache,"after":after_cache}
        prepared_at = prepare_queries(warm)
        config_oid = int(warm.value("SELECT 'lifecycle.cfg'::regconfig::oid"))
        prepared_state(warm,"warmed",prepared_at,True,[1],"lifecycle.cfg")
        run("BEGIN; ALTER TEXT SEARCH DICTIONARY lifecycle.cfg_eng (Stem=false); ROLLBACK")
        prepared_state(warm,"alter-rollback",prepared_at,True,[1],"lifecycle.cfg")
        bad = run("\\set VERBOSITY verbose\nALTER TEXT SEARCH DICTIONARY lifecycle.cfg_eng (Stem='invalid')",check=False)
        results["invalid_alter"] = {"exit_code":bad.returncode,"error":bad.stderr}
        save("lifecycle-results.json",results)
        assert bad.returncode and "42601" in bad.stderr, bad.stderr
        prepared_state(warm,"invalid-alter",prepared_at,True,[1],"lifecycle.cfg")
        prepared_state(warm,"default-simple",prepared_at,True,[1],"pg_catalog.simple")
        prepared_state(warm,"default-restored",prepared_at,True,[1],"lifecycle.cfg")
        alter("lifecycle.cfg",{"stem":False})
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
        run("BEGIN; ALTER TEXT SEARCH DICTIONARY lifecycle.cfg_eng (Stem=true); ROLLBACK")
        prepared_state(warm,"stem-alter-rollback",prepared_at,False,[],"pg_catalog.simple")
        assert all(item["config_oid"] == config_oid for item in results["prepared"])
        warm.value("DEALLOCATE lifecycle_literal; PREPARE lifecycle_literal AS SELECT plainto_tsquery('lifecycle.cfg'," + literal(DOCUMENT) + ")::text")
        refreshed = warm.value("EXECUTE lifecycle_literal")
        assert "'run'" not in refreshed, refreshed
        results["constant_literal"]["after_reprepare"] = refreshed
    symbol_prepared_lifecycle()
    if has_hooks:
        cached_init_faults()
        parallel_cached_init_faults()
        symbol_cached_init_faults()

    run(native_config_sql("lifecycle.roundtrip", stem=False, discard=False, normalize=False))
    original_oid = int(run("SELECT 'lifecycle.roundtrip'::regconfig::oid"))
    original_options = run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.roundtrip_eng'::regdictionary")
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
        restored_oid = int(run("SELECT 'lifecycle.roundtrip'::regconfig::oid",restore_db))
        restored_options = run("SELECT dictinitoption FROM pg_ts_dict WHERE oid='lifecycle.roundtrip_eng'::regdictionary",restore_db)
        assert original_oid != restored_oid, (original_oid,restored_oid)
        assert original_options == restored_options, (original_options,restored_options)
        for word in ("the","O'Reilly",r"C:\code","e\u0301","的","will"):
            sql = "SELECT to_tsvector('lifecycle.roundtrip'," + literal(word) + ")"
            assert run(sql,restore_db) == run(sql), repr(word)
        run("ALTER TEXT SEARCH DICTIONARY lifecycle.roundtrip_eng(Stem=true,Normalize=true); DROP TEXT SEARCH CONFIGURATION lifecycle.roundtrip; DROP TEXT SEARCH DICTIONARY lifecycle.roundtrip_eng",restore_db)
        assert run("SELECT count(*) FROM pg_ts_config WHERE cfgnamespace='lifecycle'::regnamespace AND cfgname='roundtrip'",restore_db) == "0"
        assert run("SELECT count(*) FROM pg_ts_dict WHERE dictnamespace='lifecycle'::regnamespace AND dictname='roundtrip_eng'",restore_db) == "0"
        results["dump_restore"] = {"dictionary_options":original_options,"original_oid":original_oid,"restored_oid":restored_oid,"schema_sha256":hashlib.sha256(dump.read_bytes()).hexdigest(),"restored_alter_drop":True}
    finally:
        run(f"DROP DATABASE {restore_db}")

    if linux:
        run("CREATE TEXT SEARCH CONFIGURATION lifecycle.droppable(COPY=public.charabia); CREATE TABLE lifecycle.dropdocs(body text); INSERT INTO lifecycle.dropdocs VALUES('hello'); CREATE INDEX droppable_bm25 ON lifecycle.dropdocs USING bm25(body) WITH(text_config='lifecycle.droppable')")
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
