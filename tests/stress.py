"""Bounded real-backend concurrency, worker execution and memory observations."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import signal
from pathlib import Path
import statistics
import threading
import time

from support import Session, literal, run, save, rss


def interrupted(signum, _frame):
    raise InterruptedError(f"Received signal {signum}")


signal.signal(signal.SIGTERM, interrupted)

HAS_HOOKS = os.environ.get("PGCB_TEST_HOOKS") == "1"
LINUX = os.environ["PGCB_PLATFORM"] == "linux-amd64"
MIB = 1024 * 1024
results = {"memory": [], "cache_invalidation": {}, "concurrency": [], "parallel": {}}


def checkpoint():
    save("stress-results.json", results)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def hooks(session):
    return json.loads(session.value("SELECT charabia.test_stats()")) if HAS_HOOKS else None


def observe(session, phase, cycle):
    memory = json.loads(session.value(
        "SELECT json_build_object('bytes',sum(total_bytes),'used',sum(used_bytes),"
        "'contexts',count(*)) FROM pg_backend_memory_contexts"
    ))
    record = dict(memory, phase=phase, cycle=cycle, pid=session.pid, rss_bytes=rss(session.pid))
    if LINUX:
        status = Path(f"/proc/{session.pid}/status").read_text()
        record["linux_status_bytes"] = {
            line.split(":", 1)[0]: int(line.split()[1]) * 1024
            for line in status.splitlines()
            if line.split(":", 1)[0] in ("VmRSS", "RssAnon", "RssFile", "RssShmem")
        }
        components = record["linux_status_bytes"]
        assert set(components) == {"VmRSS", "RssAnon", "RssFile", "RssShmem"}, components
        assert all(value >= 0 for value in components.values()), components
        assert components["VmRSS"] == sum(components[key] for key in ("RssAnon", "RssFile", "RssShmem")), components
        record["rss_nonshared_bytes"] = components["VmRSS"] - components["RssShmem"]
        assert record["rss_nonshared_bytes"] == components["RssAnon"] + components["RssFile"], components
        rollup = Path(f"/proc/{session.pid}/smaps_rollup").read_text()
        record["linux_smaps_rollup_raw"] = rollup
        record["linux_smaps_rollup_bytes"] = {
            line.split(":", 1)[0]: int(line.split()[1]) * 1024
            for line in rollup.splitlines()
            if len(line.split()) == 3 and line.split()[2] == "kB"
        }
    if HAS_HOOKS:
        record["hooks"] = hooks(session)
    return record


def bounded_contexts(samples):
    assert max(item["bytes"] for item in samples) - min(item["bytes"] for item in samples) < MIB, samples


def parallel_execution():
    with Session() as session:
        session.value(
            "SET max_parallel_workers_per_gather=2; SET min_parallel_table_scan_size=0; "
            "SET parallel_setup_cost=0; SET parallel_tuple_cost=0; "
            "SET parallel_leader_participation=off; SET enable_indexscan=off; "
            "SET enable_bitmapscan=off; SET jit=off; SET statement_timeout='60s'"
        )
        query = (
            "SELECT count(*) FROM stress.docs WHERE "
            "to_tsvector('public.charabia',body) @@ "
            "plainto_tsquery('public.charabia','PostgreSQL')"
        )
        plan = json.loads(session.value(
            "EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF,SUMMARY OFF) " + query
        ))
        launched = sum(node.get("Workers Launched", 0) for node in nodes(plan))
        scans = [node for node in nodes(plan)
                 if node.get("Node Type") == "Seq Scan"
                 and node.get("Relation Name") == "docs"
                 and node.get("Schema") == "stress"
                 and node.get("Parallel Aware") is True
                 and "to_tsvector(" in node.get("Filter", "")]
        worker_rows = [dict(worker, scan_filter=scan["Filter"])
                       for scan in scans for worker in scan.get("Workers", [])]
        processed = sum(worker.get("Actual Rows", 0) * worker.get("Actual Loops", 0)
                        for worker in worker_rows)
        results["parallel"] = {
            "backend": session.pid, "workers_launched": launched,
            "leader_participation": session.value("SHOW parallel_leader_participation"),
            "worker_scan_rows": worker_rows, "worker_rows_total": processed, "plan": plan,
        }
        checkpoint()
        assert results["parallel"]["leader_participation"] == "off"
        assert launched > 0 and scans and worker_rows, results["parallel"]
        assert any(worker.get("Actual Rows", 0) > 0 and worker.get("Actual Loops", 0) > 0
                   for worker in worker_rows), results["parallel"]
        # With leader participation disabled, scan-node worker rows account for
        # every matching document; merely launching a worker cannot pass.
        assert processed == 20000, results["parallel"]
        assert session.value(query) == "20000"
        results["parallel"]["count"] = 20000


def repeated_requests():
    with Session() as session:
        session.value("SET statement_timeout='60s'; SET jit=off")
        # Warm all language resources before judging per-request reclamation.
        text = "PostgreSQL 数据库 全文检索 café 日本語の検索 한국어 검색 ภาษาไทย Ελληνικά עברית"
        statement = (
            "SELECT count(to_tsvector('public.charabia'," + literal(text)
            + " || ' ' || g::text)) FROM generate_series(1,250) g"
        )
        headline_statement = "SELECT sum(length(ts_headline('public.charabia'," + literal(text) + " || ' ' || g::text,'cafe'::tsquery,'HighlightAll=true'))) FROM generate_series(1,250) g"
        session.value(statement)
        session.value(headline_statement)
        for batch in range(8):
            before = hooks(session)
            assert session.value(statement) == "250"
            assert int(session.value(headline_statement)) > 0
            sample = observe(session, "requests", batch)
            results["memory"].append(sample)
            checkpoint()
            if before is not None:
                after = sample["hooks"]
                assert after["parser_created"] - before["parser_created"] == 500, (before, after)
                assert after["parser_created"] == after["parser_dropped"], after
                assert after["state_registered"] - before["state_registered"] == 500, (before, after)
                assert after["state_released"] - before["state_released"] == 500, (before, after)
        bounded_contexts(results["memory"][2:])
        if HAS_HOOKS:
            results["test_counters"] = hooks(session)


def cache_invalidation():
    state = results["cache_invalidation"]
    state.update({"builtin_language": "eng", "sentinel": "the", "warmup": [], "control": [], "measurement": [],
                  "basis": "Pinned NLTK English stoplist contains the; Discard=false references no stop FST; all FST bytes are static"})
    # This test measures one real dictionary lifecycle, independent of automatic
    # language quality. All lexical types deliberately map to the same English
    # dictionary; blank/display types remain unmapped.
    run("""CREATE TEXT SEARCH DICTIONARY stress.cache_words(
 TEMPLATE=charabia.template,Language='eng',Discard=false,Stem=false,Normalize=true,Filter=false);
CREATE TEXT SEARCH CONFIGURATION stress.cache_config(PARSER=charabia.parser);
DO $map$ DECLARE l jsonb; BEGIN
 FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP
  EXECUTE format('ALTER TEXT SEARCH CONFIGURATION stress.cache_config ADD MAPPING FOR %I WITH stress.cache_words',l->>'language');
 END LOOP;
END $map$;""")
    document = "The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running. alpha beta the"
    selected = json.loads(run("SELECT json_agg(DISTINCT language) FROM charabia.analyze(" + literal(document) + ") WHERE kind='word'"))
    state["probe_document"] = document
    state["selected_languages"] = selected
    with Session() as warm, Session() as updater:
        warm.value(
            "SET plan_cache_mode=force_generic_plan; SET jit=off; SET statement_timeout='60s'; "
            "PREPARE cache_probe(text) AS SELECT to_tsvector('stress.cache_config',$1)::text"
        )
        updater.value("SET statement_timeout='60s'")
        # Parameter binding prevents constant folding from hiding cache lookup.
        probe = "EXECUTE cache_probe(" + literal(document) + ")"
        first_value = warm.value(probe)
        assert "'the':" in first_value and "'alpha':" in first_value and "'beta':" in first_value, first_value
        initial = observe(warm, "initial", 0)
        state["initial"] = initial
        state["shared_buffers"] = warm.value("SHOW shared_buffers")
        state["shared_memory_buffer_blocks"] = json.loads(warm.value(
            "SELECT coalesce(json_agg(json_build_object('name',name,'off',off,"
            "'size',size,'allocated_size',allocated_size)),'[]'::json) "
            "FROM pg_shmem_allocations WHERE name='Buffer Blocks'"
        ))
        baseline = initial.get("hooks")
        if baseline is not None:
            state["initial_active_dictionary_count"] = baseline["dictionary_created"] - baseline["dictionary_dropped"]
            assert baseline["dictionary_last_referenced_fst_bytes"] == 0, baseline
            state["baseline_other_dictionary_live"] = (
                baseline["dictionary_created"] - baseline["dictionary_dropped"] - 1
            )
            state["baseline_other_fst_bytes"] = (
                baseline["dictionary_live_referenced_fst_bytes"] - baseline["dictionary_last_referenced_fst_bytes"]
            )
            state["baseline_callbacks_live"] = baseline["state_registered"] - baseline["state_released"]

        def check_state(sample, before, replaced):
            if not HAS_HOOKS:
                return
            current = sample["hooks"]
            assert current["dictionary_live_fst_bytes"] == 0 and current["dictionary_last_created_fst_bytes"] == 0, sample
            assert current["embedded_stop_fst_bytes"] == 30190, sample
            assert current["embedded_english_fst_bytes"] > 0, sample
            assert current["dictionary_last_created"] == current["dictionary_last_lexized"], sample
            assert current["dictionary_created"] - current["dictionary_dropped"] == (
                state["baseline_other_dictionary_live"] + 1
            ), sample
            assert current["dictionary_live_referenced_fst_bytes"] == (
                state["baseline_other_fst_bytes"] + current["dictionary_last_referenced_fst_bytes"]
            ), sample
            assert current["state_registered"] - current["state_released"] == state["baseline_callbacks_live"], sample
            assert current["parser_created"] == current["parser_dropped"], sample
            # Each probe owns one parser callback; an invalidation additionally
            # releases and replaces exactly one cached dictionary callback.
            assert current["state_registered"] - before["state_registered"] == 1 + int(replaced), (before, sample)
            assert current["state_released"] - before["state_released"] == 1 + int(replaced), (before, sample)
            assert current["dictionary_created"] - before["dictionary_created"] == int(replaced), (before, sample)
            assert current["dictionary_dropped"] - before["dictionary_dropped"] == int(replaced), (before, sample)

        def cycle(phase, number, selection, replaced):
            before = hooks(warm)
            if replaced:
                updater.value("ALTER TEXT SEARCH DICTIONARY stress.cache_words(Discard=" + ("true" if selection else "false") + ")")
            value = warm.value(probe)
            sample = dict(observe(warm, phase, number), discard=bool(selection), vector=value)
            state[phase].append(sample)
            checkpoint()
            assert (("'the':" in value) == (selection == 0)) and "'alpha':" in value and "'beta':" in value, sample
            if HAS_HOOKS:
                fst_bytes = sample["hooks"]["dictionary_last_referenced_fst_bytes"]
                assert (fst_bytes > 0) if selection else (fst_bytes == 0), sample
            check_state(sample, before, replaced)

        for number in range(8):
            cycle("warmup", number, number % 2, True)
        # The control uses the same already-warm backend, probe and observations
        # but no ALTER, so invalidation leakage cannot expand its RSS tolerance.
        for number in range(32):
            cycle("control", number, 1, False)
        control_rss = [sample["rss_bytes"] for sample in state["control"]]
        control_range = max(control_rss) - min(control_rss)
        state["rss_control_range_bytes"] = control_range
        state["rss_growth_tolerance_bytes"] = max(MIB, 4 * control_range)
        checkpoint()
        for number in range(32):
            cycle("measurement", number, number % 2, True)
        samples = state["measurement"]
        bounded_contexts(samples)
        first = statistics.median(sample["rss_bytes"] for sample in samples[:8])
        last = statistics.median(sample["rss_bytes"] for sample in samples[-8:])
        state["rss_first_eight_median_bytes"] = first
        state["rss_last_eight_median_bytes"] = last
        state["rss_median_growth_bytes"] = last - first
        state["raw_total_rss_gate_passed"] = last - first <= state["rss_growth_tolerance_bytes"]
        if LINUX:
            # Total backend RSS includes PG's shared-buffer residency. ALTER
            # touches catalog pages, which are not retained Rust heap.
            # Preserve that total and its original gate outcome; apply the same
            # predeclared tolerance only to unexplained non-shared residency.
            nonshared_first = statistics.median(sample["rss_nonshared_bytes"] for sample in samples[:8])
            nonshared_last = statistics.median(sample["rss_nonshared_bytes"] for sample in samples[-8:])
            shared_first = statistics.median(sample["linux_status_bytes"]["RssShmem"] for sample in samples[:8])
            shared_last = statistics.median(sample["linux_status_bytes"]["RssShmem"] for sample in samples[-8:])
            state["rss_nonshared_first_eight_median_bytes"] = nonshared_first
            state["rss_nonshared_last_eight_median_bytes"] = nonshared_last
            state["rss_nonshared_median_growth_bytes"] = nonshared_last - nonshared_first
            state["rss_shared_first_eight_median_bytes"] = shared_first
            state["rss_shared_last_eight_median_bytes"] = shared_last
            state["rss_shared_median_growth_bytes"] = shared_last - shared_first
            control_nonshared = [sample["rss_nonshared_bytes"] for sample in state["control"]]
            state["rss_nonshared_control_range_bytes"] = max(control_nonshared) - min(control_nonshared)
            state["nonshared_rss_gate_passed"] = nonshared_last - nonshared_first <= state["rss_growth_tolerance_bytes"]
            state["rss_gate_metric"] = "Linux status VmRSS - RssShmem (RssAnon + RssFile)"
            state["rss_accounting_classification"] = (
                "unexplained-nonshared-resident-growth" if not state["nonshared_rss_gate_passed"]
                else "total-growth-accounted-as-shared-residency" if not state["raw_total_rss_gate_passed"]
                else "raw-total-and-nonshared-residency-within-bound"
            )
            state["rss_component_first_to_last_growth_bytes"] = {
                key: samples[-1]["linux_status_bytes"][key] - samples[0]["linux_status_bytes"][key]
                for key in ("VmRSS", "RssAnon", "RssFile", "RssShmem")
            }
        state["prepared_plan_counts"] = json.loads(warm.value(
            "SELECT json_build_object('generic',generic_plans,'custom',custom_plans) "
            "FROM pg_prepared_statements WHERE name='cache_probe'"
        ))
        checkpoint()
        assert state["nonshared_rss_gate_passed"] if LINUX else state["raw_total_rss_gate_passed"], state
        assert state["prepared_plan_counts"]["generic"] == 73 and state["prepared_plan_counts"]["custom"] == 0, state
        final_value = warm.value(probe)
        assert "'the':" not in final_value and "'alpha':" in final_value and "'beta':" in final_value, final_value


def bm25_query(text):
    return (
        "SELECT coalesce(json_agg(id ORDER BY id),'[]'::json) FROM ("
        "SELECT id FROM stress.docs ORDER BY body <@> to_bm25query("
        + literal(text) + ",'stress.stress_bm25') LIMIT 25000) ranked"
    )


def assert_bm25_plan(plan):
    scans = [node for node in nodes(plan)
             if node.get("Node Type") == "Index Scan"
             and node.get("Index Name") == "stress_bm25"
             and node.get("Schema") == "stress"
             and node.get("Actual Loops", 0) > 0]
    assert scans, plan


def concurrency():
    if LINUX:
        # Every writer mutation below must maintain an already-existing index.
        run("CREATE INDEX stress_bm25 ON stress.docs USING bm25(body) WITH(text_config='public.charabia')")
    snapshots_ready = threading.Barrier(3)
    writes_committed = threading.Barrier(3)
    stop = threading.Event()
    active = {}
    active_lock = threading.Lock()
    records = []
    failures = []
    pids = []

    def client(role):
        record = {"role": role, "batches": [], "status": "running"}
        try:
            with Session() as session:
                record["backend"] = session.pid
                with active_lock:
                    active[role] = session.pid
                    pids.append(session.pid)
                session.value("SET application_name=" + literal("pgcb_stress_" + role) + "; SET statement_timeout='60s'; SET jit=off; SET enable_seqscan=off")
                for batch in range(8):
                    if stop.is_set():
                        break
                    started = time.monotonic()
                    if role == "writer":
                        snapshots_ready.wait(timeout=90)
                        first = 30000 + batch * 50
                        session.value(
                            f"INSERT INTO stress.docs SELECT g,'PostgreSQL 中文 writer' "
                            f"FROM generate_series({first},{first+49}) g"
                        )
                        session.value(
                            "UPDATE stress.docs SET body='updated PostgreSQL 中文' "
                            f"WHERE id BETWEEN {first} AND {first+19}"
                        )
                        session.value(f"DELETE FROM stress.docs WHERE id BETWEEN {first+40} AND {first+49}")
                        record["batches"].append({
                            "batch": batch, "inserted": 50, "updated": 20, "deleted": 10,
                            "started_monotonic": started, "committed_monotonic": time.monotonic(),
                        })
                        writes_committed.wait(timeout=90)
                    else:
                        text = ("PostgreSQL", "中文", "全文")[batch % 3]
                        session.value("BEGIN ISOLATION LEVEL REPEATABLE READ")
                        snapshot = session.value("SELECT pg_current_snapshot()::text")
                        snapshots_ready.wait(timeout=90)
                        lexemes = "tsvector_to_array(to_tsvector('public.charabia',"
                        oracle = json.loads(session.value(
                            "SELECT coalesce(json_agg(id ORDER BY id),'[]'::json) FROM stress.docs "
                            "WHERE " + lexemes + "body)) && " + lexemes + literal(text) + "))"
                        ))
                        assert len(oracle) < 25000, len(oracle)
                        observation = {
                            "batch": batch, "query": text, "snapshot": snapshot, "native_oracle": oracle,
                            "started_monotonic": started, "oracle_monotonic": time.monotonic(),
                        }
                        record["batches"].append(observation)
                        # The index now contains committed writes newer than
                        # this reader snapshot; its scan must honor that snapshot.
                        writes_committed.wait(timeout=90)
                        observation["indexed_read_monotonic"] = time.monotonic()
                        if LINUX:
                            query = bm25_query(text)
                            plan = json.loads(session.value(
                                "EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF,SUMMARY OFF) " + query
                            ))
                            observation["plan"] = plan
                            actual = json.loads(session.value(query))
                            observation["bm25_candidates"] = actual
                            assert_bm25_plan(plan)
                            assert actual == oracle, observation
                        else:
                            assert int(session.value("SELECT count(*) FROM stress.docs")) >= 20000
                        session.value("COMMIT")
                        error = session.query("SELECT * FROM charabia.analyze(repeat('x',262145))")
                        assert "ERROR:" in error and session.value("SELECT 1") == "1", error
                        observation["error_recovered"] = True
                assert len(record["batches"]) == 8, record
                record["status"] = "passed"
        except BaseException as error:
            record["status"] = "failed"
            record["error"] = repr(error)
            # The executor is always joined. Cancellation is sent to exact
            # suite-owned PIDs, never to an unrelated backend.
            stop.set()
            snapshots_ready.abort()
            writes_committed.abort()
            with active_lock:
                others = [(owner,pid) for owner, pid in active.items() if owner != role]
            for owner,pid in others:
                try:
                    run(f"SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid={pid} AND application_name=" + literal("pgcb_stress_" + owner))
                except BaseException as cancel_error:
                    record.setdefault("cancel_errors", []).append(repr(cancel_error))
            return record, error
        finally:
            with active_lock:
                active.pop(role, None)
        return record, None

    pool = ThreadPoolExecutor(max_workers=3)
    futures = []
    primary = None
    try:
        futures = [pool.submit(client,role) for role in ("writer","reader-a","reader-b")]
        # This wait is also interrupted by an observer/harness failure. The
        # finally block cancels exact owned sessions before joining the pool.
        for future in futures:
            future.result()
    except BaseException as error:
        primary = error
    finally:
        stop.set()
        snapshots_ready.abort()
        writes_committed.abort()
        with active_lock:
            remaining_clients = list(active.items())
        for owner,pid in remaining_clients:
            try:
                run(f"SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid={pid} AND application_name=" + literal("pgcb_stress_"+owner),check=False,timeout=10)
            except BaseException as error:
                results.setdefault("concurrency_cleanup_errors",[]).append(repr(error))
        pool.shutdown(wait=True,cancel_futures=True)
    # Collect every outcome after the pool joined, including failure records.
    for future in futures:
        try:
            record,failure = future.result()
            records.append(record)
            if failure is not None:
                failures.append(failure)
        except BaseException as error:
            failures.append(error)
    if primary is not None:
        failures.insert(0,primary)
    results["concurrency"] = records
    results["concurrency_joined"] = True
    results["concurrency_backend_pids"] = pids
    deadline = time.monotonic() + 5
    while True:
        remaining = int(run(
            "SELECT count(*) FROM pg_stat_activity WHERE pid IN (" + ",".join(map(str, pids)) + ")"
        )) if pids else 0
        results["concurrency_closed_backends"] = remaining
        if remaining == 0 or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    checkpoint()
    assert results["concurrency_closed_backends"] == 0, results
    if results.get("concurrency_cleanup_errors"):
        raise AssertionError(f"Concurrent client cleanup failed after join: {results['concurrency_cleanup_errors']}")
    if failures:
        raise AssertionError(f"Concurrent clients failed after join: {[repr(error) for error in failures]}") from failures[0]
    assert run("SELECT count(*) FROM stress.docs") == "20320"
    if LINUX:
        with Session() as session:
            session.value("SET enable_seqscan=off; SET jit=off; SET statement_timeout='60s'")
            query = bm25_query("PostgreSQL")
            plan = json.loads(session.value(
                "EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF,SUMMARY OFF) " + query
            ))
            actual = json.loads(session.value(query))
            results["bm25_after_concurrency"] = {"plan": plan, "candidates": actual}
            checkpoint()
            assert_bm25_plan(plan)
            assert len(actual) == 20320 and len(set(actual)) == 20320, results["bm25_after_concurrency"]


try:
    run("""
CREATE SCHEMA stress;
CREATE TABLE stress.docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO stress.docs SELECT g,'PostgreSQL 数据库 全文检索 café '||g FROM generate_series(1,20000) g;
CREATE INDEX stress_gin ON stress.docs USING gin(to_tsvector('public.charabia',body));
ANALYZE stress.docs;
""")
    parallel_execution()
    repeated_requests()
    cache_invalidation()
    concurrency()
    results["status"] = "passed"
except BaseException as error:
    results["status"] = "failed"
    results["error"] = repr(error)
    raise
finally:
    checkpoint()
    run("DROP SCHEMA IF EXISTS stress CASCADE")

print(json.dumps({
    "status": "passed", "parallel_workers": results["parallel"]["workers_launched"],
    "worker_rows": results["parallel"]["worker_rows_total"], "concurrent_clients": len(results["concurrency"]),
    "memory_batches": len(results["memory"]), "cache_warmup": 8, "cache_control": 32,
    "cache_measurements": 32, "rss_growth_bytes": results["cache_invalidation"]["rss_median_growth_bytes"],
    "raw_total_rss_gate_passed": results["cache_invalidation"]["raw_total_rss_gate_passed"],
    "rss_nonshared_growth_bytes": results["cache_invalidation"].get("rss_nonshared_median_growth_bytes"),
}))
