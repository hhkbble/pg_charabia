#!/usr/bin/env python3
"""Bounded real-stage observations, independent labels, and raw samples; no SLA."""
from collections import Counter
import hashlib
import html
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time

from support import EVIDENCE, Session, literal, run, save

if os.environ["PGCB_PLATFORM"] != "linux-amd64":
    raise AssertionError("The delivery benchmark requires Linux AMD64")

SCHEMA = f"pgcb_benchmark_{os.getpid()}"
PARAGRAPHS = [
    ("english", "The children were running through the garden while the teachers were reading books and sharing their happy memories together. These cats were running."),
    ("chinese", "中文数据库支持全文检索，技术文档和知识库包含查询与索引。"),
    ("french", "Les enfants ont écrit des lettres aimées et les chevaux traversaient la campagne pendant les vacances."),
    ("turkish", "Türkçe belgeler ve kitapları hakkında konuşuyoruz. IŞIKLAR altında çocuklar bahçede oynuyorlar."),
    ("swedish", "Flickornas röster hördes över den stora gården och skogarna medan hästarna vandrade längs vägen."),
    ("japanese-korean", "日本語の検索と技術文書 한국어 검색 데이터베이스 /usr/local v18.6 <doc> & café"),
]
results = {"status": "running", "environment": {
    "target": "Linux AMD64 + PG18; Docker emulation when host is ARM64",
    "clock": "Python perf_counter_ns roundtrip wall clock; /proc backend CPU ticks",
    "no_sla": True,
}, "samples": {}, "plans": {}, "memory": [], "sources": {
    "corpus": "six fixed representative paragraphs, repeated 1..3 times and suffixed with unique IDs; ordinary text below every PG cap",
    "domain": "literal PostgreSQL is in every document; only the frozen English paragraph contains running, with upstream English run stem",
    "ranking": "actual target BM25 scores/ranks are observations, not float goldens or independent relevance oracles",
    "counterfactual": "last final lexeme per retained Word; logical TF/dl/candidates only, not a built stem-only index or measured stem-only CPU",
}}
sequence = 0


def checkpoint():
    save("benchmark-results.json", results)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cpu_ticks(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    return int(fields[11]) + int(fields[12])


def nodes(value):
    if isinstance(value, dict):
        if "Node Type" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def check(condition, message, evidence=None):
    if not condition:
        results["assertion_failure"] = {"message": message, "evidence": evidence}
        checkpoint()
        raise AssertionError(message)


def statement(session, sql, label):
    global sequence
    sequence += 1
    stem = f"benchmark-{sequence:03d}-{label}"
    (EVIDENCE / f"{stem}.sql").write_text(sql + ";\n")
    output = session.query(sql, timeout=240)
    (EVIDENCE / f"{stem}.stdout").write_text(output + "\n")
    check("ERROR:" not in output, label + " raised PostgreSQL ERROR", output)
    return output


def measure(session, name, sql, repeats=1):
    raw = []
    output = None
    for ordinal in range(repeats):
        ticks = cpu_ticks(session.pid)
        start = time.perf_counter_ns()
        output = statement(session, sql, f"{name}-{ordinal}")
        elapsed = time.perf_counter_ns() - start
        raw.append({"roundtrip_ns": elapsed,
                    "backend_cpu_ticks": cpu_ticks(session.pid) - ticks,
                    "output_sha256": hashlib.sha256(output.encode()).hexdigest()})
        results["samples"][name] = {"sql": sql, "backend_pid": session.pid, "raw": raw,
                                   "measurement": "PSQL roundtrip includes result serialization and client evidence write"}
        checkpoint()
    elapsed_ms = sorted(sample["roundtrip_ns"] / 1e6 for sample in raw)
    results["samples"][name].update({
        "count": repeats, "p50_ms": statistics.median(elapsed_ms),
        "p95_ms": elapsed_ms[math.ceil(repeats * 0.95) - 1],
        "backend_cpu_ms": [sample["backend_cpu_ticks"] * 1000 / os.sysconf("SC_CLK_TCK") for sample in raw],
    })
    checkpoint()
    return output


def memory(session, phase, config):
    contexts = json.loads(statement(session,
        "SELECT jsonb_agg(jsonb_build_object('name',name,'parent',parent,'level',level,"
        "'total_bytes',total_bytes,'used_bytes',used_bytes,'free_bytes',free_bytes)ORDER BY level,name)"
        "FROM pg_backend_memory_contexts", "memory-" + phase))
    status = Path(f"/proc/{session.pid}/status").read_text()
    rollup = Path(f"/proc/{session.pid}/smaps_rollup").read_text()
    components = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in status.splitlines()
                  if line.split(":", 1)[0] in ("VmRSS", "RssAnon", "RssFile", "RssShmem")}
    smaps = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in rollup.splitlines()
             if len(line.split()) == 3 and line.split()[2] == "kB"}
    check(set(components) == {"VmRSS", "RssAnon", "RssFile", "RssShmem"}, "missing Linux RSS components", components)
    state = {"phase": phase, "config": config, "pid": session.pid, "contexts": contexts,
             "linux_status_raw": status, "linux_smaps_rollup_raw": rollup,
             "linux_status_bytes": components,
             "nonshared_rss_bytes": components["RssAnon"] + components["RssFile"],
             "private_resident_bytes": smaps["Private_Clean"] + smaps["Private_Dirty"],
             "fst_live_counter": None,
             "fst_counter_note": "release SQL has no live FST counter; exact local per-route FST sizes are in direct Rust results"}
    if os.environ.get("PGCB_TEST_HOOKS") == "1":
        state["test_hooks"] = json.loads(statement(session, "SELECT charabia.test_stats()", "memory-hooks-" + phase))
        state["fst_live_counter"] = state["test_hooks"]["dictionary_live_fst_bytes"]
        state["fst_counter_note"] = "test-hooks backend counters; does not replace release-library evidence"
    results["memory"].append(state)
    checkpoint()


def verify_library(session):
    session.value("LOAD 'pg_charabia'")
    stage = Path(os.environ["PGCB_TEMP_DIR"]) / "stage/extension"
    libraries = list(stage.glob("pg_charabia*.so"))
    check(len(libraries) == 1, "expected one staged target shared library", [str(p) for p in libraries])
    library = libraries[0]
    maps = Path(f"/proc/{session.pid}/maps").read_text()
    check(str(library.resolve()) in maps or str(library) in maps, "benchmark backend did not map the staged library")
    info = json.loads(session.value("SELECT charabia.analysis_info()"))
    architecture = subprocess.check_output(["file", str(library)], text=True).strip()
    dependencies = subprocess.check_output(["ldd", str(library)], text=True)
    check("ELF 64-bit" in architecture and "x86-64" in architecture, "wrong benchmark library architecture", architecture)
    check("not found" not in dependencies, "unresolved benchmark dependency", dependencies)
    package = EVIDENCE / "package/extension" / library.name
    check(package.is_file() and digest(library) == digest(package), "actual loaded staged/package library identity mismatch")
    database = json.loads(session.value("SELECT jsonb_build_object('version',version(),'extensions',"
        "(SELECT jsonb_object_agg(extname,extversion)FROM pg_extension),'block_size',current_setting('block_size'))"))
    identity = {"pid": session.pid, "loaded_library_path": str(library), "sha256": digest(library),
                "database": database,
                "architecture": architecture, "dynamic_dependencies": dependencies, "analysis_info": info,
                "loaded_mappings": [line for line in maps.splitlines() if "pg_charabia" in line or "pg_textsearch" in line]}
    results.setdefault("loaded_backends", []).append(identity)
    checkpoint()


def capture_plan(session, name, sql, index):
    defaults = "SET enable_seqscan=on;SET enable_indexscan=on;SET enable_indexonlyscan=on;SET enable_bitmapscan=on;"
    default = json.loads(statement(session, defaults + "EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF) " + sql,
                                   name + "-default-plan"))
    # GIN's actual access path is bitmap-only. Merely disabling seqscan can
    # choose a primary-key scan that filters rows and proves no FTS index use.
    ordinary = "off" if index.endswith("_gin") else "on"
    force = f"SET enable_seqscan=off;SET enable_indexscan={ordinary};SET enable_indexonlyscan={ordinary};SET enable_bitmapscan=on;"
    forced = json.loads(statement(session, force + "EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF) " + sql,
                                  name + "-index-plan"))
    target = [node for node in nodes(forced) if node.get("Index Name") == index and
              node.get("Node Type") in ("Index Scan", "Bitmap Index Scan") and node.get("Actual Loops", 0) > 0]
    check(bool(target), name + " did not execute the target index", forced)
    if "bm25" in index:
        check(all(node["Node Type"] == "Index Scan" for node in target), name + " used a non-BM25 scan", target)
    results["plans"][name] = {"sql": sql, "default": default, "forced": forced, "target_index": index}
    checkpoint()
    return forced


def logical_ablation(session, table, config):
    rows = json.loads(statement(session, f"""
SELECT jsonb_agg(jsonb_build_object('id',d.id,'family',d.family,'body',d.body,
 'analysis',(SELECT jsonb_agg(jsonb_build_object('kind',kind,'terms',terms,'raw',raw,'language',language)ORDER BY byte_start)
   FROM charabia.analyze(d.body,{literal(config)})),
 'native_tf',(SELECT jsonb_object_agg(lexeme,cardinality(positions))FROM unnest(to_tsvector({literal(config)},d.body))))ORDER BY d.id)
FROM {table} d WHERE d.id<=60
""", "logical-ablation-raw"))
    records = []
    for row in rows:
        body = row["body"]
        check(len(body.encode()) < 2047, "counterfactual corpus includes a long source document", row["id"])
        dual, single = Counter(), Counter()
        words = 0
        for token in row["analysis"]:
            check(token["kind"] != "display_only", "counterfactual corpus includes display-only")
            terms = token["terms"]
            if token["kind"] == "word":
                check(1 <= len(terms) <= 2 and len(set(terms)) == len(terms), "counterfactual Word terms invariant", token)
                check(all(0 < len(term.encode()) <= 2047 for term in terms), "counterfactual normalized cap assumption", token)
                words += 1
                dual.update(terms)
                single.update([terms[-1]])
            else:
                check(not terms, "counterfactual non-Word unexpectedly has terms", token)
        check(words < 16383 and max(dual.values(), default=0) < 255, "counterfactual position cap assumption", row["id"])
        check(dict(dual) == row["native_tf"], "counterfactual adapter TF does not agree with native stored positions", row["id"])
        records.append({"id": row["id"], "family": row["family"], "dual_tf": dict(dual), "dual_dl": sum(dual.values()),
                        "stem_only_counterfactual_tf": dict(single), "stem_only_counterfactual_dl": sum(single.values())})
    queries = {}
    for query in ("run", "running", "PostgreSQL"):
        tokens = json.loads(statement(session,
            f"SELECT coalesce(jsonb_agg(jsonb_build_object('kind',kind,'terms',terms)ORDER BY byte_start),'[]')FROM charabia.analyze({literal(query)},{literal(config)})",
            "logical-query-" + query.lower()))
        dual_query = {term for token in tokens for term in token["terms"]}
        single_query = {token["terms"][-1] for token in tokens if token["terms"]}
        queries[query] = {"dual_terms": sorted(dual_query), "counterfactual_terms": sorted(single_query),
            "dual_candidates": [row["id"] for row in records if dual_query.intersection(row["dual_tf"])],
            "counterfactual_candidates": [row["id"] for row in records if single_query.intersection(row["stem_only_counterfactual_tf"])]}
    results["logical_stem_only_counterfactual"] = {"configuration": config, "rows": records, "queries": queries,
        "not_measured": ["stem-only index size", "stem-only build/write/query CPU", "stem-only pg_textsearch scores/ranks"],
        "rank_link": "actual default/no-stem ranked rows are separately recorded; no simplified formula pretends to reproduce fieldnorm quantization"}
    checkpoint()


def main():
    direct_log = EVIDENCE / "benchmark-direct.log"
    failure = None
    created_schema = False
    try:
        check(os.uname().machine == "x86_64", "benchmark target is not Linux AMD64", os.uname().machine)
        results["environment"].update({"uname": list(os.uname()),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
            "cpuinfo_raw": Path("/proc/cpuinfo").read_text(),
            "cgroup_limits": {name: Path("/sys/fs/cgroup", name).read_text() for name in
                ("cpu.max", "memory.max") if Path("/sys/fs/cgroup", name).is_file()}})
        checkpoint()
        # This opt-in Rust benchmark measures the real modules. Its ordinary
        # algorithm/invariant tests have already run in the unit suite.
        command = ["cargo", "test", "--locked", "--release", "--lib", "--no-default-features", "--features",
                   "pg18" + (",test-hooks" if os.environ.get("PGCB_TEST_HOOKS") == "1" else ""),
                   "benchmark_direct_pipeline", "--", "--ignored", "--nocapture"]
        with direct_log.open("w") as output:
            process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        results["direct_command"] = {"argv": command, "exit_code": process.returncode}
        check(process.returncode == 0, "direct Rust stage benchmark failed", direct_log.name)
        direct = [json.loads(line.split("PGCB_BENCHMARK=", 1)[1]) for line in direct_log.read_text().splitlines()
                  if line.startswith("PGCB_BENCHMARK=")]
        check(len(direct) == 1, "missing direct Rust benchmark result")
        results["direct_rust"] = direct[0]
        values = ",".join(f"({ordinal},{literal(family)},{literal('PostgreSQL ' + paragraph)})"
                          for ordinal, (family, paragraph) in enumerate(PARAGRAPHS))
        setup = run(f"CREATE SCHEMA {SCHEMA}", check=False)
        check(setup.returncode == 0, "cannot create exclusively owned benchmark schema", setup.stderr)
        created_schema = True
        run(f"SELECT charabia.create_config('{SCHEMA}.no_stem','{{\"stemming\":false}}')")
        run(f"CREATE TABLE {SCHEMA}.source(family_id integer PRIMARY KEY,family text,body text);INSERT INTO {SCHEMA}.source VALUES {values}")
        for label, config, stemming in (("default", "public.charabia", True), ("no_stem", f"{SCHEMA}.no_stem", False)):
            table = f"{SCHEMA}.docs_{label}"
            gin_index, bm25_index = f"{label}_gin", f"{label}_bm25"
            run(f"CREATE TABLE {table}(id integer PRIMARY KEY,family text NOT NULL,body text NOT NULL CHECK(octet_length(body)<=262144))")
            with Session() as session:
                session.value("SET statement_timeout='180s';SET client_min_messages=warning")
                verify_library(session)
                info = json.loads(statement(session, f"SELECT charabia.analysis_info({literal(config)})", label + "-info"))
                check(info["settings"] == {"stemming": stemming, "builtin_stopwords": True, "stopwords_add": {}, "stopwords_keep": {}},
                      "true comparison differs in more than approved stemming flag", info["settings"])
                memory(session, label + "-loaded-cold", config)
                measure(session, label + "-insert-bulk", f"INSERT INTO {table} SELECT g,s.family,repeat(s.body,1+g%3)||' '||g "
                        f"FROM generate_series(1,1000)g JOIN {SCHEMA}.source s ON s.family_id=g%6")
                results.setdefault("corpora", {})[label] = json.loads(statement(session,
                    f"SELECT jsonb_build_object('rows',count(*),'bytes',sum(octet_length(body)),'max_bytes',max(octet_length(body)))FROM {table}", label + "-corpus"))
                measure(session, label + "-first-vector", f"SELECT length(to_tsvector({literal(config)},body))FROM {table} WHERE id=1")
                memory(session, label + "-first-document", config)
                measure(session, label + "-analyze-corpus", f"SELECT sum((SELECT count(*)FROM charabia.analyze(d.body,{literal(config)})))FROM {table} d", 3)
                measure(session, label + "-vectorize-corpus", f"SELECT sum(length(to_tsvector({literal(config)},body)))FROM {table}", 3)
                if label == "default":
                    logical_ablation(session, table, config)
                measure(session, label + "-build-gin", f"CREATE INDEX {gin_index} ON {table} USING gin(to_tsvector({literal(config)},body))")
                measure(session, label + "-build-bm25", f"CREATE INDEX {bm25_index} ON {table} USING bm25(body)WITH(text_config={literal(config)})")
                session.value(f"ANALYZE {table}")
                for query in ("PostgreSQL", "run"):
                    ranked = (f"SELECT id,body <@> to_bm25query({literal(query)},'{SCHEMA}.{bm25_index}')AS score FROM {table} "
                              f"ORDER BY body <@> to_bm25query({literal(query)},'{SCHEMA}.{bm25_index}')LIMIT 2000")
                    plan = capture_plan(session, label + "-bm25-" + query.lower(), ranked, bm25_index)
                    rows = json.loads(measure(session, label + "-query-bm25-" + query.lower(),
                        "SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]')FROM(" + ranked + ")r", 10))
                    ids = sorted(row["id"] for row in rows)
                    expected = list(range(1,1001)) if query == "PostgreSQL" else ([i for i in range(1,1001) if i%6 == 0] if stemming else [])
                    check(ids == expected, "benchmark corpus domain IDs changed", {"label": label,"query":query,"expected":expected,"actual":ids})
                    check(all(math.isfinite(row["score"]) for row in rows), "nonfinite actual BM25 score", rows)
                    results.setdefault("rank_observations", []).append({"config":config,"query":query,"rows":rows,
                        "independent_domain_ids":expected,"relevance_labels":"literal marker all rows; run relates only to fixed English family",
                        "interpretation":"scores/ties/ranks are observations; no float score or arbitrary tie-order assertion"})
                    checkpoint()
                gin = f"SELECT id FROM {table} WHERE to_tsvector({literal(config)},body) @@ plainto_tsquery({literal(config)},'PostgreSQL')"
                capture_plan(session, label + "-gin", gin, gin_index)
                ids = json.loads(measure(session, label + "-query-gin", "SELECT jsonb_agg(id ORDER BY id)FROM(" + gin + ")r", 10))
                check(ids == list(range(1,1001)), "actual GIN domain marker IDs changed", ids)
                sizes = f"SELECT jsonb_object_agg(relname,jsonb_build_object('main_bytes',pg_relation_size(oid),'total_bytes',pg_total_relation_size(oid)))FROM pg_class WHERE relnamespace='{SCHEMA}'::regnamespace AND relname IN('docs_{label}','{gin_index}','{bm25_index}')"
                results.setdefault("sizes", {})[label + "-built"] = json.loads(statement(session, sizes, label + "-built-sizes"))
                session.value("SET enable_seqscan=on;SET enable_indexscan=on;SET enable_indexonlyscan=on;SET enable_bitmapscan=on")
                measure(session, label + "-insert-incremental", f"INSERT INTO {table} SELECT g,'english','PostgreSQL 中文 incremental running '||g FROM generate_series(1001,1100)g")
                measure(session, label + "-update", f"UPDATE {table} SET body='PostgreSQL 中文 updated '||id WHERE id BETWEEN 1001 AND 1040")
                measure(session, label + "-delete", f"DELETE FROM {table} WHERE id BETWEEN 1081 AND 1100")
                session.value(f"ANALYZE {table}")
                dml_ranked = f"SELECT id FROM {table} ORDER BY body <@> to_bm25query('PostgreSQL','{SCHEMA}.{bm25_index}')LIMIT 2000"
                capture_plan(session, label + "-bm25-after-dml", dml_ranked, bm25_index)
                ids = json.loads(statement(session, "SELECT jsonb_agg(id ORDER BY id)FROM(" + dml_ranked + ")r", label + "-after-dml-ids"))
                check(ids == list(range(1,1081)), "incremental write/delete candidate mismatch", ids)
                results["sizes"][label + "-after-dml"] = json.loads(statement(session, sizes, label + "-after-dml-sizes"))
                statement(session, f"PREPARE query_only(text)AS SELECT charabia.query_any($1,{literal(config)})::text", label + "-prepare-query-only")
                query_output = measure(session, label + "-query-any-once", "EXECUTE query_only('PostgreSQL')", 10)
                check(bool(query_output), "query_any standalone benchmark unexpectedly returned empty analysis")
                # Bound parameters remain raw text for ranking. The materialized
                # TSQuery is constructed once, never fed back into BM25 parsing.
                for limit, flavor in ((10, "topk"), (2000, "all_candidates")):
                    query = (f"WITH p AS MATERIALIZED(SELECT charabia.query_any($1,{literal(config)})AS q),"
                        f"ranked AS MATERIALIZED(SELECT id,body,body <@> to_bm25query($1,'{SCHEMA}.{bm25_index}')AS score "
                        f"FROM {table} WHERE (SELECT numnode(q)FROM p)>0 ORDER BY body <@> to_bm25query($1,'{SCHEMA}.{bm25_index}')LIMIT {limit}) "
                        f"SELECT r.id,r.score,r.body,ts_headline({literal(config)},r.body,p.q,'HighlightAll=true')AS headline "
                        "FROM ranked r CROSS JOIN p ORDER BY r.score")
                    name = f"headline_{flavor}"
                    statement(session, f"PREPARE {name}(text)AS SELECT coalesce(jsonb_agg(to_jsonb(h)),'[]')FROM({query})h", label + "-prepare-" + flavor)
                    session.value("SET plan_cache_mode=force_generic_plan")
                    execute = f"EXECUTE {name}('PostgreSQL')"
                    plan = capture_plan(session, label + "-headline-" + flavor, execute, bm25_index)
                    p_nodes = [node for node in nodes(plan) if node.get("Subplan Name") == "CTE p"]
                    check(len(p_nodes) == 1 and p_nodes[0].get("Actual Loops") == 1 and p_nodes[0].get("Actual Rows") == 1,
                          "materialized query_any was not evaluated once", p_nodes)
                    rows = json.loads(measure(session, label + "-headline-" + flavor, execute, 5))
                    check(len(rows) == min(limit,1080), "headline workload candidate count changed")
                    check(all(html.unescape(row["headline"].replace("<b>","").replace("</b>","")) == row["body"] for row in rows),
                          "headline performance path loses original text")
                    results.setdefault("headline_workloads", []).append({"config":config,"flavor":flavor,"limit":limit,
                        "actual_rows":len(rows),"sql":query,"query_any_evaluations":1,
                        "cost_boundary":"BM25 raw-query analysis plus once-only query_any analysis plus per-result original document analysis/rendering"})
                session.value("SET enable_seqscan=on;SET enable_indexscan=on;SET enable_indexonlyscan=on;SET enable_bitmapscan=on")
                for options, suffix in (("HighlightAll=true,Escape=false", "all_unescaped"),
                                        ("HighlightAll=true", "all_escaped"),
                                        ("MinWords=5,MaxWords=15,MaxFragments=1", "fragment_escaped")):
                    measure(session, label + "-headline-render-" + suffix,
                        f"SELECT sum(octet_length(ts_headline({literal(config)},body,'postgresql'::tsquery,{literal(options)})))FROM {table}", 3)
                coverage = json.loads(statement(session,
                    f"SELECT jsonb_agg(DISTINCT a.language ORDER BY a.language)FROM {table} d CROSS JOIN LATERAL charabia.analyze(d.body,{literal(config)})a",
                    label + "-touched-routes"))
                results.setdefault("touched_routes", {})[label] = {"routes":coverage,
                    "meaning":"observed analysis routes; not an exact release PG live-state counter"}
                memory(session, label + "-warm", config)
        results["status"] = "passed"
    except BaseException as error:
        failure = error
        results["status"] = "failed"
        results["error"] = repr(error)
        checkpoint()
    finally:
        if created_schema:
            try:
                removed = run(f"DROP SCHEMA {SCHEMA} CASCADE", check=False)
                results["cleanup"] = {"exit_code":removed.returncode,"stdout":removed.stdout,"stderr":removed.stderr}
                if removed.returncode:
                    raise AssertionError("benchmark owned schema cleanup failed")
                absent = run(f"SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname={literal(SCHEMA)})")
                check(absent == "t", "benchmark owned schema survived cleanup")
                results["cleanup"]["schema_absent"] = True
            except BaseException as error:
                results["status"] = "failed"
                results["cleanup_error"] = repr(error)
                failure = failure or error
        checkpoint()
    if failure:
        raise failure
    print(json.dumps({"status":"passed","measurements":len(results["samples"]),"corpora":results["corpora"],
                      "meaning":"observed baselines; no performance SLA or simulated stem-only benchmark"}))


if __name__ == "__main__":
    main()
