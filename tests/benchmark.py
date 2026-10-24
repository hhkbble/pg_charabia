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

from support import ANALYSIS_REVISION, FORMAT, EVIDENCE, Session, literal, run, save, native_config_sql

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
results = {"format": FORMAT, "analysis_revision": ANALYSIS_REVISION, "modes": {"linguistic": "public.charabia_linguistic", "structured": "public.charabia_structured"}, "status": "running", "environment": {
    "target": "Linux AMD64 + PG18; Docker emulation when host is ARM64",
    "clock": "Python perf_counter_ns roundtrip wall clock; /proc backend CPU ticks",
    "no_sla": True,
}, "samples": {}, "plans": {}, "memory": [], "sources": {
    "corpus": "six fixed representative paragraphs, repeated 1..3 times and suffixed with unique IDs; ordinary text below every PG cap",
    "domain": "literal PostgreSQL is in every document; only the frozen English paragraph contains running, with upstream English run stem",
    "ranking": "actual target BM25 scores/ranks are observations, not float goldens or independent relevance oracles",
    "counterfactual": "terminal-only ts_debug lexemes (not filtering-chain simulation); last final lexeme per retained Word; logical TF/dl/candidates only, not a built stem-only index or measured stem-only CPU",
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
        # Keep PG18's actual row, including its path, rather than assuming the
        # parent column from an older server's memory-context view.
        "SELECT jsonb_agg(to_jsonb(m)ORDER BY level,name)"
        "FROM pg_backend_memory_contexts m", "memory-" + phase))
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
        state["fst_owned_bytes"] = state["test_hooks"]["dictionary_live_fst_bytes"]
        state["fst_referenced_bytes"] = state["test_hooks"]["dictionary_live_referenced_fst_bytes"]
        state["fst_static_bytes"] = state["test_hooks"]["embedded_stop_fst_bytes"] + state["test_hooks"]["embedded_english_fst_bytes"]
        check(state["fst_owned_bytes"] == 0, "dictionary state never owns static FST bytes", state)
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
    # Planner GUCs do not invalidate an existing generic prepared plan. Both
    # observations must replan in this exclusively owned benchmark session.
    default = json.loads(statement(session, defaults + "DISCARD PLANS;EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF) " + sql,
                                   name + "-default-plan"))
    # GIN's actual access path is bitmap-only. Merely disabling seqscan can
    # choose a primary-key scan that filters rows and proves no FTS index use.
    ordinary = "off" if index.endswith("_gin") else "on"
    force = f"SET enable_seqscan=off;SET enable_indexscan={ordinary};SET enable_indexonlyscan={ordinary};SET enable_bitmapscan=on;"
    forced = json.loads(statement(session, force + "DISCARD PLANS;EXPLAIN(ANALYZE,VERBOSE,FORMAT JSON,COSTS OFF,TIMING OFF) " + sql,
                                  name + "-index-plan"))
    target = [node for node in nodes(forced) if node.get("Index Name") == index and
              node.get("Node Type") in ("Index Scan", "Bitmap Index Scan") and node.get("Actual Loops", 0) > 0]
    check(bool(target), name + " did not execute the target index", forced)
    if "bm25" in index:
        check(all(node["Node Type"] == "Index Scan" for node in target), name + " used a non-BM25 scan", target)
    results["plans"][name] = {"sql": sql, "default": default, "forced": forced, "target_index": index,
                              "planner_reset":"DISCARD PLANS before each EXPLAIN; prepared SQL objects retained"}
    checkpoint()
    return forced


def structured_workloads():
    """Collect bounded structured observations before ordinary-domain gates run."""
    cases = [
        ("url", "https://example.com/running?q=100ms"),
        ("host", "standalone.example.net"),
        ("email", "user+tag@example.org"),
        ("file", "/src/running-tests.rs"),
        ("numword", "100ms"),
    ]
    record = results["structured_tokens"] = {
        "status": "running", "cases": cases, "modes": {},
        "basis": "Five fixed literal structures repeated across 100 rows; ordinary words explicitly use English without stop/stem. Query expectations come from declared literal/component rules, not observed extension output.",
        "control": "literal_only maps the five structured types to real pg_catalog.simple. It is a smaller alternative workload, not an equivalent analyzer or a model of structured CPU cost.",
        "cost_boundary": "Parser/analyze, full vectorization, index construction, indexed writes, query analysis, ranking, and whole-source headline are separate measured operations. No SLA or score/storage neutrality claim.",
    }
    checkpoint()
    run(f"CREATE TEXT SEARCH DICTIONARY {SCHEMA}.structured_words(TEMPLATE=charabia.linguistic_template,Language='eng',"
        "Discard=false,Stem=false,Normalize=true,Filter=false,English=false);"
        f"CREATE TEXT SEARCH CONFIGURATION {SCHEMA}.structured_default(COPY=public.charabia_structured);"
        "DO $structured_tokens$ DECLARE l jsonb; BEGIN "
        "FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP "
        f"EXECUTE format('ALTER TEXT SEARCH CONFIGURATION {SCHEMA}.structured_default ALTER MAPPING FOR %I WITH {SCHEMA}.structured_words',l->>'language');"
        "END LOOP;END $structured_tokens$;"
        f"CREATE TEXT SEARCH CONFIGURATION {SCHEMA}.structured_literal(COPY={SCHEMA}.structured_default);"
        f"ALTER TEXT SEARCH CONFIGURATION {SCHEMA}.structured_literal ALTER MAPPING FOR url,host,email,file,numword WITH pg_catalog.simple;")
    source = ",".join(f"({ordinal},{literal(kind)},{literal('left ' + raw + ' right')})"
        for ordinal, (kind, raw) in enumerate(cases))
    run(f"CREATE TABLE {SCHEMA}.structured_source(family_id integer PRIMARY KEY,family text NOT NULL,body text NOT NULL);"
        f"INSERT INTO {SCHEMA}.structured_source VALUES{source};")
    for mode, config in (("expanded", f"{SCHEMA}.structured_default"), ("literal_only", f"{SCHEMA}.structured_literal")):
        table = f"{SCHEMA}.structured_docs_{mode}"
        gin_index, bm25_index = f"structured_{mode}_gin", f"structured_{mode}_bm25"
        run(f"CREATE TABLE {table}(id integer PRIMARY KEY,family text NOT NULL,body text NOT NULL CHECK(octet_length(body)<=262144))")
        observations = record["modes"][mode] = {"mode":"structured", "configuration": config, "analysis_revision":ANALYSIS_REVISION, "table": table}
        with Session() as session:
            session.value("SET statement_timeout='180s';SET client_min_messages=warning")
            verify_library(session)
            measure(session, mode + "-structured-insert-bulk", f"INSERT INTO {table} SELECT g,s.family,s.body "
                f"FROM generate_series(1,100)g JOIN {SCHEMA}.structured_source s ON s.family_id=g%5")
            memory(session, mode + "-structured-cold", config)
            measure(session, mode + "-structured-first-vector", f"SELECT to_tsvector({literal(config)},body)::text FROM {table} WHERE id=1")
            measure(session, mode + "-structured-parser", f"SELECT sum((SELECT count(*)FROM charabia.analyze(d.body,{literal(config)})))FROM {table} d", 3)
            measure(session, mode + "-structured-vectorize", f"SELECT sum(length(to_tsvector({literal(config)},body)))FROM {table}", 3)
            observations["vectors_tf_dl"] = json.loads(statement(session,
                "SELECT jsonb_agg(jsonb_build_object('id',d.id,'family',d.family,'body',d.body,"
                f"'vector',to_tsvector({literal(config)},d.body)::text,"
                f"'tf',(SELECT jsonb_object_agg(lexeme,cardinality(positions))FROM unnest(to_tsvector({literal(config)},d.body))),"
                f"'dl',(SELECT sum(cardinality(positions))FROM unnest(to_tsvector({literal(config)},d.body))))ORDER BY d.id)"
                f"FROM {table} d WHERE id<=10", mode + "-structured-vector-tf-dl"))
            observations["tf_dl_meaning"] = "Actual tsvector position counts, before BM25 fieldnorm quantization; no invented BM25 formula."
            measure(session, mode + "-structured-build-gin", f"CREATE INDEX {gin_index} ON {table} USING gin(to_tsvector({literal(config)},body))")
            measure(session, mode + "-structured-build-bm25", f"CREATE INDEX {bm25_index} ON {table} USING bm25(body)WITH(text_config={literal(config)})")
            session.value(f"ANALYZE {table}")
            observations["queries"] = []
            for label, query in (("unit", "100ms"), ("full-url", cases[0][1])):
                expected = ([number for number in range(1, 101) if number % 5 in (0, 4)] if label == "unit" else list(range(1, 101))) if mode == "expanded" else (
                    [number for number in range(1, 101) if number % 5 == (4 if label == "unit" else 0)])
                ranked = (f"SELECT id,body <@> to_bm25query({literal(query)},'{SCHEMA}.{bm25_index}')AS score FROM {table} "
                    f"ORDER BY body <@> to_bm25query({literal(query)},'{SCHEMA}.{bm25_index}')LIMIT 200")
                capture_plan(session, mode + "-structured-" + label, ranked, bm25_index)
                rows = json.loads(measure(session, mode + "-structured-query-" + label,
                    "SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]')FROM(" + ranked + ")r", 3))
                observation = {"query": query, "rows": rows, "independent_candidate_ids": expected,
                    "meaning": "Candidate expansion and actual scores/ranks are separate observations; full-query bag matches do not establish structural equality or ranking quality."}
                observations["queries"].append(observation)
                checkpoint()
                check(sorted(row["id"] for row in rows) == expected, "structured benchmark literal/component candidates changed", observation)
                check(all(math.isfinite(row["score"]) for row in rows), "structured benchmark produced nonfinite scores", observation)
                measure(session, mode + "-structured-query-any-" + label,
                    f"SELECT charabia.query_any({literal(query)},{literal(config)})::text", 3)
            gin = f"SELECT id FROM {table} WHERE to_tsvector({literal(config)},body) @@ plainto_tsquery({literal(config)},'100ms')"
            capture_plan(session, mode + "-structured-gin", gin, gin_index)
            ids = json.loads(measure(session, mode + "-structured-query-gin", "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]')FROM(" + gin + ")r", 3))
            expected = [number for number in range(1, 101) if number % 5 in ((0, 4) if mode == "expanded" else (4,))]
            check(ids == expected, "structured benchmark native full/bundle IDs changed", {"expected": expected, "actual": ids})
            observations["native_unit_ids"] = ids
            session.value("SET enable_seqscan=on;SET enable_indexscan=on;SET enable_indexonlyscan=on;SET enable_bitmapscan=on")
            measure(session, mode + "-structured-insert", f"INSERT INTO {table} VALUES(101,'numword','left 100ms right')")
            measure(session, mode + "-structured-update", f"UPDATE {table} SET body='left replacement right'WHERE id=101")
            measure(session, mode + "-structured-delete", f"DELETE FROM {table} WHERE id=101")
            session.value(f"ANALYZE {table}")
            after_dml = f"SELECT id FROM {table} ORDER BY body <@> to_bm25query('100ms','{SCHEMA}.{bm25_index}')LIMIT 200"
            capture_plan(session, mode + "-structured-after-dml", after_dml, bm25_index)
            after_ids = json.loads(statement(session, "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]')FROM(" + after_dml + ")r", mode + "-structured-after-dml-ids"))
            check(after_ids == expected, "structured indexed write observations did not restore the source corpus", after_ids)
            topk = (f"WITH p AS MATERIALIZED(SELECT charabia.query_any('100ms',{literal(config)})AS q),"
                f"ranked AS MATERIALIZED(SELECT id,body,body <@> to_bm25query('100ms','{SCHEMA}.{bm25_index}')AS score "
                f"FROM {table} WHERE(SELECT numnode(q)>0 FROM p)ORDER BY body <@> to_bm25query('100ms','{SCHEMA}.{bm25_index}')LIMIT 10)"
                f"SELECT r.id,r.body,r.score,ts_headline({literal(config)},r.body,p.q,'HighlightAll=true')AS headline "
                "FROM ranked r CROSS JOIN p ORDER BY r.score")
            plan = capture_plan(session, mode + "-structured-topk-headline", topk, bm25_index)
            p_nodes = [node for node in nodes(plan) if node.get("Subplan Name") == "CTE p"]
            check(len(p_nodes) == 1 and p_nodes[0].get("Actual Loops") == 1 and p_nodes[0].get("Actual Rows") == 1,
                  "structured performance query_any was not evaluated once", p_nodes)
            headline_rows = json.loads(measure(session, mode + "-structured-topk-headline",
                "SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]')FROM(" + topk + ")r", 3))
            check(len(headline_rows) == 10 and all(row["id"] in expected for row in headline_rows),
                  "structured headline top-k includes a noncandidate", headline_rows)
            check(all(html.unescape(row["headline"].replace("<b>", "").replace("</b>", "")) == row["body"] and
                      "<b>" in row["headline"] for row in headline_rows), "structured headline performance path loses source", headline_rows)
            observations["topk_headlines"] = headline_rows
            observations["sizes"] = json.loads(statement(session,
                "SELECT jsonb_object_agg(relname,jsonb_build_object('main_bytes',pg_relation_size(oid),'total_bytes',pg_total_relation_size(oid)))"
                f"FROM pg_class WHERE relnamespace='{SCHEMA}'::regnamespace AND relname IN('structured_docs_{mode}','{gin_index}','{bm25_index}')",
                mode + "-structured-sizes"))
            memory(session, mode + "-structured-warm", config)
            observations["status"] = "measured"
            checkpoint()
    record["status"] = "measured"
    checkpoint()


def logical_ablation(session, table, config):
    rows = json.loads(statement(session, f"""
SELECT jsonb_agg(jsonb_build_object('id',d.id,'family',d.family,'body',d.body,
 'analysis',(SELECT jsonb_agg(jsonb_build_object('kind',CASE WHEN lexemes IS NULL THEN 'ignore' WHEN cardinality(lexemes)=0 THEN 'stop' ELSE 'word' END,'terms',coalesce(lexemes,ARRAY[]::text[]),'raw',token) ORDER BY ord)
   FROM ts_debug({literal(config)},d.body) WITH ORDINALITY x(alias,description,token,dictionaries,dictionary,lexemes,ord)),
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
                check(1 <= len(terms) <= 4 and len(set(terms)) == len(terms), "counterfactual Word terms invariant", token)
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
            f"SELECT coalesce(jsonb_agg(jsonb_build_object('terms',coalesce(lexemes,ARRAY[]::text[]))ORDER BY ord),'[]')FROM ts_debug({literal(config)},{literal(query)}) WITH ORDINALITY x(alias,description,token,dictionaries,dictionary,lexemes,ord)",
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
        structured_workloads()
        run(native_config_sql(f'{SCHEMA}.no_stem',stem=False,discard=True))
        # Same terminal dictionary, with zero/one/two real preparation filters.
        # Output equality is required before timing; these are not public profiles.
        run(f"""CREATE TEXT SEARCH CONFIGURATION {SCHEMA}.filter_one(COPY={SCHEMA}.no_stem);
CREATE TEXT SEARCH CONFIGURATION {SCHEMA}.filter_two(COPY={SCHEMA}.no_stem);
DO $bench$ DECLARE l jsonb; BEGIN
 FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP
  EXECUTE format('CREATE TEXT SEARCH DICTIONARY {SCHEMA}.%I(TEMPLATE=charabia.linguistic_template,Language=%L,Discard=false,Stem=false,Normalize=false,Filter=true)','pre_'||(l->>'language'),l->>'language');
  EXECUTE format('CREATE TEXT SEARCH DICTIONARY {SCHEMA}.%I(TEMPLATE=charabia.linguistic_template,Language=%L,Discard=false,Stem=false,Normalize=false,Filter=true)','pre2_'||(l->>'language'),l->>'language');
  EXECUTE format('ALTER TEXT SEARCH CONFIGURATION {SCHEMA}.filter_one ALTER MAPPING FOR %I WITH {SCHEMA}.%I,{SCHEMA}.%I',l->>'language','pre_'||(l->>'language'),'no_stem_'||(l->>'language'));
  EXECUTE format('ALTER TEXT SEARCH CONFIGURATION {SCHEMA}.filter_two ALTER MAPPING FOR %I WITH {SCHEMA}.%I,{SCHEMA}.%I,{SCHEMA}.%I',l->>'language','pre_'||(l->>'language'),'pre2_'||(l->>'language'),'no_stem_'||(l->>'language'));
 END LOOP;
END $bench$;""")
        filter_payload = PARAGRAPHS[0][1] + ' Café PostgreSQL '
        with Session() as filter_session:
            verify_library(filter_session)
            baseline = filter_session.value(f"SELECT to_tsvector('{SCHEMA}.no_stem',{literal(filter_payload)})::text")
            for mode in ('no_stem','filter_one','filter_two'):
                actual = filter_session.value(f"SELECT to_tsvector('{SCHEMA}.{mode}',{literal(filter_payload)})::text")
                check(actual == baseline, 'filter cost control changed analysis semantics', mode)
                measure(filter_session,'filter-chain-'+mode,
                    f"SELECT sum(length(to_tsvector('{SCHEMA}.{mode}',{literal(filter_payload)}||g::text)))FROM generate_series(1,200)g",5)
        run(f"CREATE TABLE {SCHEMA}.source(family_id integer PRIMARY KEY,family text,body text);INSERT INTO {SCHEMA}.source VALUES {values}")
        for label, config, stemming in (("default", "public.charabia", True), ("no_stem", f"{SCHEMA}.no_stem", False)):
            table = f"{SCHEMA}.docs_{label}"
            gin_index, bm25_index = f"{label}_gin", f"{label}_bm25"
            run(f"CREATE TABLE {table}(id integer PRIMARY KEY,family text NOT NULL,body text NOT NULL CHECK(octet_length(body)<=262144))")
            with Session() as session:
                session.value("SET statement_timeout='180s';SET client_min_messages=warning")
                verify_library(session)
                info = json.loads(statement(session, "SELECT charabia.analysis_info()", label + "-info"))
                check(info["extension"] == "0.4.0", "benchmark uses a different release", info)
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
                    results.setdefault("rank_observations", []).append({"mode":"linguistic","config":config,"query":query,"rows":rows,
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
                    results.setdefault("headline_workloads", []).append({"mode":"linguistic","config":config,"flavor":flavor,"limit":limit,
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
