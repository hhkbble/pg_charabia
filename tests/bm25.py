#!/usr/bin/env python3
"""Real pg_textsearch candidate, plan, mutation, cache, and restart regression.

The runner creates both extensions and executes sql/bm25.sql first. This driver
uses only that suite's schema and the runner-marked disposable PostgreSQL cluster.
It never starts a container or changes an existing PostgreSQL installation.
"""
import json
import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests/fixtures/bm25/corpus.json").read_text())
LIMIT = 100000  # Larger than every corpus; compare candidates, not just top-k.


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def json_values(output):
    """psql -qAt emits successive JSON values, including multiline EXPLAIN."""
    decoder = json.JSONDecoder()
    values = []
    remaining = output.lstrip()
    while remaining:
        value, end = decoder.raw_decode(remaining)
        values.append(value)
        remaining = remaining[end:].lstrip()
    return values


def index_nodes(value, index):
    if isinstance(value, dict):
        own = value.get("Node Type") == "Index Scan" and value.get("Index Name") == index
        return ([value] if own else []) + [
            node for child in value.values() for node in index_nodes(child, index)
        ]
    if isinstance(value, list):
        return [node for child in value for node in index_nodes(child, index)]
    return []


def contains_node(value, kind):
    if isinstance(value, dict):
        return value.get("Node Type") == kind or any(contains_node(child, kind) for child in value.values())
    if isinstance(value, list):
        return any(contains_node(child, kind) for child in value)
    return False


class Suite:
    def __init__(self):
        self.evidence = Path(os.environ["PGCB_RUN_DIR"])
        self.temp = Path(os.environ["PGCB_TEMP_DIR"])
        self.bindir = Path(subprocess.check_output([os.environ["PG_CONFIG"], "--bindir"], text=True).strip())
        self.psql = [str(self.bindir / "psql"), "-X", "-qAt", "-v", "ON_ERROR_STOP=1"]
        self.result = {"status": "running", "fixture_basis": FIXTURE["basis"], "stages": [], "commands": [], "assertions": 0}
        self.sequence = 0

    def checkpoint(self):
        pending = self.evidence / "bm25-results.json.tmp"
        pending.write_text(json.dumps(self.result, ensure_ascii=False, indent=2) + "\n")
        pending.replace(self.evidence / "bm25-results.json")

    def run(self, sql, label):
        self.sequence += 1
        stem = f"bm25-{self.sequence:03d}-{label}"
        (self.evidence / f"{stem}.sql").write_text(sql)
        started = time.monotonic()
        proc = subprocess.run(self.psql, input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        (self.evidence / f"{stem}.stdout").write_text(proc.stdout)
        (self.evidence / f"{stem}.stderr").write_text(proc.stderr)
        self.result["commands"].append({"label": label, "exit_code": proc.returncode, "seconds": round(time.monotonic()-started, 3), "evidence": stem})
        self.checkpoint()
        if proc.returncode:
            raise AssertionError(f"{label}: psql exit {proc.returncode}; see {stem}.stderr: {proc.stderr[-3000:]}")
        return json_values(proc.stdout)

    def require(self, condition, message):
        if not condition:
            raise AssertionError(message)
        self.result["assertions"] += 1

    def populate(self):
        rows = ",".join(f"({number},{literal(body)})" for number, body in FIXTURE["documents"])
        self.run(f"INSERT INTO bm25_test.docs VALUES {rows};\nINSERT INTO bm25_test.stop_docs SELECT * FROM bm25_test.docs;\n"
                 "CREATE INDEX docs_bm25 ON bm25_test.docs USING bm25(body) WITH(text_config='charabia.default_v1');\n"
                 "CREATE INDEX stop_bm25 ON bm25_test.stop_docs USING bm25(body) WITH(text_config='bm25_test.stop_config');\n"
                 "ANALYZE bm25_test.docs; ANALYZE bm25_test.stop_docs;\n", "populate")

    def queries(self, phase):
        # Hand-reviewed smoke/domain expectations remain independent of the
        # native-overlap oracle. Mutations change only the following documents.
        extra = [12] if phase in ("insert", "update") else [13] if phase in ("post-spill-insert", "restart", "reindex") else []
        original = [] if phase in ("update", "delete", "vacuum", "spill", "post-spill-insert", "restart", "reindex") else [1]
        for group, cases, table, index, config in (
            ("default", FIXTURE["queries"], "docs", "docs_bm25", "charabia.default_v1"),
            ("stop", FIXTURE["stop_queries"], "stop_docs", "stop_bm25", "bm25_test.stop_config"),
        ):
            for item in cases:
                case = dict(item, group=group, table=table, index=index, config=config)
                if item["name"] == "chinese":
                    case["expected_ids"] = original + extra
                elif item["name"] in ("english_case", "stop_plus_content", "content_only"):
                    case["expected_ids"] = sorted(original + [2, 10] + extra)
                elif item["name"] == "mixed_or_terms":
                    case["expected_ids"] = sorted(original + [3] + extra)
                yield case
        yield {"name": "expression-boundary", "text": "PostgreSQL", "group": "expression", "table": "expression_docs", "index": "expression_bm25", "config": "charabia.default_v1", "expression": "(title||' '||body)", "expected_ids": [1]}
        yield {"name": "array-boundary", "text": "PostgreSQL", "group": "array", "table": "array_docs", "index": "array_bm25", "config": "charabia.default_v1", "expression": "parts", "native_expression": "array_to_string(parts,' ')", "expected_ids": [1]}
        yield {"name": "pg-frequency-cap", "text": "echo", "group": "frequency", "table": "frequency_docs", "index": "frequency_bm25", "config": "charabia.default_v1", "expected_ids": [1]}
        yield {"name": "pg-position-cap", "text": "tailword", "group": "frequency", "table": "frequency_docs", "index": "frequency_bm25", "config": "charabia.default_v1", "expected_ids": [2]}
        yield {"name": "frequency-ranking", "text": "rankcanary", "group": "ranking", "table": "ranking_docs", "index": "ranking_bm25", "config": "charabia.default_v1", "expected_ids": [1, 2], "expected_order": [2, 1]}

    def matrix(self, phase):
        stage = {"phase": phase, "queries": []}
        self.result["stages"].append(stage)
        cases = list(self.queries(phase))
        for cache in ("on", "off"):
            for mode in ("force_custom_plan", "force_generic_plan"):
                sql = [f"SET pg_textsearch.memtable_cache_enabled={cache}; SET plan_cache_mode={mode}; SET enable_seqscan=off;"]
                prepared = {}
                for case in cases:
                    group = case["group"]
                    if group not in prepared:
                        expression = case.get("expression", "body")
                        ranked = f"SELECT id FROM bm25_test.{case['table']} ORDER BY {expression} <@> to_bm25query(bm25_test.checked_query($1),'bm25_test.{case['index']}') LIMIT {LIMIT}"
                        prepared[group] = ranked
                        sql.append(f"PREPARE ranked_{group}(text) AS SELECT coalesce(jsonb_agg(id),'[]'::jsonb) FROM ({ranked}) ranked;")
                for case in cases:
                    q = literal(case["text"])
                    config = literal(case["config"])
                    group = case["group"]
                    body = case.get("native_expression", case.get("expression", "body"))
                    sql += [
                        f"SELECT jsonb_build_object('case',{literal(case['name'])},'cache',{literal(cache)},'mode',{literal(mode)});",
                        # Keep the default planner observation separate from the
                        # prepared plan, which must execute a real target scan.
                        "SET enable_seqscan=on;",
                        f"EXPLAIN(FORMAT JSON,VERBOSE,COSTS OFF) {prepared[group].replace('$1',q)};",
                        "SET enable_seqscan=off;",
                        f"EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) EXECUTE ranked_{group}({q});",
                        f"EXECUTE ranked_{group}({q});",
                        f"SELECT jsonb_build_object('oracle',coalesce((SELECT jsonb_agg(id ORDER BY id) FROM bm25_test.{case['table']} WHERE tsvector_to_array(to_tsvector({config},{body})) && tsvector_to_array(to_tsvector({config},bm25_test.checked_query({q})))),'[]'::jsonb),"
                        f"'all_ids',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM bm25_test.{case['table']}),'query_lexemes',tsvector_to_array(to_tsvector({config},bm25_test.checked_query({q}))));",
                    ]
                sql.append("SELECT jsonb_agg(jsonb_build_object('name',name,'generic_plans',generic_plans,'custom_plans',custom_plans) ORDER BY name) FROM pg_prepared_statements;")
                values = self.run("\n".join(sql) + "\n", f"{phase}-{cache}-{mode}")
                self.require(len(values) == len(cases)*5+1, f"{phase}: unexpected psql result count {len(values)}")
                for number, case in enumerate(cases):
                    marker, default_plan, plan, actual, oracle = values[number*5:number*5+5]
                    record = dict(marker, query=case["text"], index=case["index"], default_plan=default_plan, plan=plan, ranked_ids=actual, **oracle)
                    stage["queries"].append(record)
                    # The outer failure handler checkpoints this record before
                    # cleanup if an assertion fails. Raw SQL/stdout/stderr were
                    # already persisted by run(), including the first failure.
                    label = f"{phase}/{cache}/{mode}/{case['name']}"
                    target = index_nodes(plan, case["index"])
                    self.require(len(oracle["all_ids"]) < LIMIT, f"{label}: LIMIT could truncate the candidate oracle")
                    if case.get("empty_analysis"):
                        self.require(oracle["query_lexemes"] == [], f"{label}: fixture expected empty query analysis")
                    if not target and not oracle["query_lexemes"]:
                        # Stable upstream can choose standalone scoring for an
                        # empty literal: it returns all heap rows at zero score.
                        # Record and assert that path rather than counting it as
                        # successful indexed retrieval or silently skipping it.
                        self.require(contains_node(plan, "Seq Scan") and contains_node(plan, "Sort"), f"{label}: unrecognized empty-query plan")
                        self.require(sorted(actual) == oracle["all_ids"], f"{label}: empty fallback must return every row")
                        expression = case.get("expression", "body")
                        zero_scores = self.run(
                            "SELECT jsonb_build_object('zero_scores',coalesce(bool_and(("
                            f"{expression} <@> to_bm25query({literal(case['text'])},'bm25_test.{case['index']}'))=0),true)) "
                            f"FROM bm25_test.{case['table']};", f"{phase}-{cache}-{mode}-{case['name']}-fallback-scores"
                        )[0]
                        self.require(zero_scores["zero_scores"], f"{label}: standalone empty scores must be zero")
                        record["fallback_scores"] = zero_scores
                        record["path"] = "empty-standalone-fallback"
                    else:
                        self.require(bool(target), f"{label}: expected target BM25 Index Scan; got {plan}")
                        self.require(all(node.get("Schema") == "bm25_test" for node in target), f"{label}: wrong schema in Index Scan")
                        self.require(all(node.get("Actual Loops", 0) > 0 for node in target), f"{label}: target Index Scan was never executed")
                        self.require(sorted(actual) == oracle["oracle"], f"{label}: BM25 candidates {actual} != native overlap {oracle['oracle']}")
                        if "expected_ids" in case:
                            self.require(sorted(actual) == sorted(case["expected_ids"]), f"{label}: independent retrieval expectation {case['expected_ids']}, actual {actual}")
                        if "expected_order" in case:
                            self.require(actual == case["expected_order"], f"{label}: expected TF-based order {case['expected_order']}, actual {actual}")
                        record["path"] = "target-bm25-index-scan"
                    self.require(len(actual) == len(set(actual)), f"{label}: duplicate document IDs")
                stats = values[-1]
                self.require(all(item["generic_plans"] > 0 and item["custom_plans"] == 0 for item in stats) if mode == "force_generic_plan" else all(item["custom_plans"] > 0 and item["generic_plans"] == 0 for item in stats), f"{phase}/{mode}: did not actually exercise requested prepared-plan mode: {stats}")
                stage.setdefault("prepared_plan_counts", []).append({"cache": cache, "mode": mode, "counts": stats})
                self.checkpoint()
        self.checkpoint()
        print(f"BM25 {phase}: {len(stage['queries'])} real query/plan cases passed", flush=True)

    def change(self, label, statement):
        self.run(statement, label)
        self.matrix(label)

    def spill(self):
        # VACUUM/inline maintenance may already have drained earlier writes.
        # Add a live, unrelated canary so a non-NULL spill result proves this
        # run exercised the write-to-segment path rather than only a no-op.
        self.run("INSERT INTO bm25_test.docs VALUES(14,'spillcontrolcanary'); INSERT INTO bm25_test.stop_docs VALUES(14,'spillcontrolcanary');", "spill-live-canary")
        values = self.run("\n".join(
            f"SELECT jsonb_build_object('index',{literal(index)},'segment_root',bm25_spill_index({literal('bm25_test.'+index)}));"
            for index in ("docs_bm25", "stop_bm25", "expression_bm25", "array_bm25", "frequency_bm25", "ranking_bm25")
        ), "spill-indices")
        self.result["spill"] = values
        # Pinned pg_textsearch build.c:820-825,904-911: this API returns
        # https://github.com/timescale/pg_textsearch/blob/fa142d049d08eac45a263f5abc250c8877ae076e/src/access/build.c#L820-L825
        # a segment root BLOCK NUMBER, or SQL NULL for an empty memtable.
        # CREATE INDEX's bulk path has already published frequency/ranking
        # segments, while the two main indices have the fresh live canary.
        self.require(all(item["segment_root"] is None or (type(item["segment_root"]) is int and item["segment_root"] > 0) for item in values), "spill returned an invalid nullable segment root")
        roots = {item["index"]: item["segment_root"] for item in values}
        self.require(all(roots[index] is not None for index in ("docs_bm25", "stop_bm25")), "spill did not publish either live main memtable")
        self.require(all(roots[index] is None for index in ("frequency_bm25", "ranking_bm25")), "bulk-built unmodified indices should exercise the empty-memtable no-op")
        self.matrix("spill")

    def chunk_control(self):
        sql = """
SET enable_seqscan=off;
EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) SELECT id FROM bm25_test.chunk_docs
 ORDER BY body <@> to_bm25query('chunkright','bm25_test.chunk_bm25') LIMIT 100;
SELECT coalesce(jsonb_agg(id),'[]'::jsonb) FROM (SELECT id FROM bm25_test.chunk_docs
 ORDER BY body <@> to_bm25query('chunkright','bm25_test.chunk_bm25') LIMIT 100) ranked;
EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) SELECT id FROM bm25_test.chunk_docs
 ORDER BY body <@> to_bm25query('chunkleft '||repeat(' ',262144)||' chunkright','bm25_test.chunk_bm25') LIMIT 100;
SELECT coalesce(jsonb_agg(id),'[]'::jsonb) FROM (SELECT id FROM bm25_test.chunk_docs
 ORDER BY body <@> to_bm25query('chunkleft '||repeat(' ',262144)||' chunkright','bm25_test.chunk_bm25') LIMIT 100) ranked;
"""
        short_plan, short_ids, large_plan, large_ids = self.run(sql, "upstream-only-chunk-control")
        self.result["upstream_chunk_control"] = {"config": "pg_catalog.simple", "bypasses_application_guard": True, "short_plan": short_plan, "short_ids": short_ids, "oversized_query_plan": large_plan, "oversized_query_ids": large_ids}
        self.require(bool(index_nodes(short_plan, "chunk_bm25")) and bool(index_nodes(large_plan, "chunk_bm25")), "upstream chunk control must use its real index")
        self.require(short_ids == large_ids == [1], "upstream chunk control lost a term across the 256-KiB boundary")

    def restart(self):
        marker = self.temp / ".pg-charabia-owner"
        self.require(marker.is_file() and marker.read_text() == self.evidence.name, "refusing restart: cluster ownership marker mismatch")
        before = self.run("SELECT jsonb_build_object('data_directory',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'start',pg_postmaster_start_time());", "restart-identity-before")[0]
        self.require(Path(before["data_directory"]).resolve() == (self.temp / "data").resolve(), "refusing restart: wrong PGDATA")
        self.require(before["socket"] == str(self.temp / "socket"), "refusing restart: wrong socket")
        command = [str(self.bindir / "pg_ctl"), "-D", str(self.temp / "data"), "-l", str(self.temp / "server.log"), "-m", "fast", "-w", "-t", "60", "restart"]
        proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
        (self.evidence / "bm25-restart.log").write_text(proc.stdout)
        self.result["restart"] = {"argv": command, "exit_code": proc.returncode, "before": before}
        self.checkpoint()
        self.require(proc.returncode == 0, f"owned-cluster restart failed: {proc.stdout}")
        after = self.run("SELECT jsonb_build_object('start',pg_postmaster_start_time());", "restart-identity-after")[0]
        with (self.evidence / "bm25-restarted-backend.stderr").open("w") as error_log:
            backend = subprocess.Popen(self.psql, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log, text=True, bufsize=1)
            try:
                backend.stdin.write("SELECT charabia.analysis_info_v1(); SELECT pg_backend_pid();\n")
                backend.stdin.flush()
                after["analysis_info"] = json.loads(backend.stdout.readline())
                after["backend_pid"] = int(backend.stdout.readline().strip())
                # Read procfs from the harness while that backend stays alive;
                # PostgreSQL's whole-file helper can treat procfs st_size=0 as
                # an empty file, so it is not an identity probe here.
                mappings = Path(f"/proc/{after['backend_pid']}/maps").read_text()
                (self.evidence / "bm25-restarted-backend-maps.txt").write_text(mappings)
                after["loaded_library_mappings"] = "\n".join(line for line in mappings.splitlines() if "pg_charabia" in line)
                backend.stdin.write("\\q\n")
                backend.stdin.flush()
                backend.communicate(timeout=15)
                self.require(backend.returncode == 0, "restarted identity backend failed")
            finally:
                if backend.poll() is None:
                    backend.kill()
                    backend.wait()
        self.result["restart"]["after"] = after
        self.require(before["start"] != after["start"], "cluster restart did not change postmaster start time")
        self.require(bool(after["loaded_library_mappings"]), "restarted backend did not load the staged extension library")
        self.matrix("restart")

    def execute(self):
        failure = None
        try:
            self.populate()
            self.matrix("bulk")
            self.chunk_control()
            self.change("insert", "INSERT INTO bm25_test.docs VALUES(12,'全文 PostgreSQL incrementalcanary'); INSERT INTO bm25_test.stop_docs VALUES(12,'全文 PostgreSQL incrementalcanary');")
            self.change("update", "UPDATE bm25_test.docs SET body='replacementcanary' WHERE id=1; UPDATE bm25_test.stop_docs SET body='replacementcanary' WHERE id=1;")
            self.change("delete", "DELETE FROM bm25_test.docs WHERE id=12; DELETE FROM bm25_test.stop_docs WHERE id=12;")
            self.change("vacuum", "VACUUM bm25_test.docs; VACUUM bm25_test.stop_docs;")
            self.spill()
            self.change("post-spill-insert", "INSERT INTO bm25_test.docs VALUES(13,'全文 PostgreSQL afterspillcanary'); INSERT INTO bm25_test.stop_docs VALUES(13,'全文 PostgreSQL afterspillcanary');")
            self.restart()
            self.change("reindex", "REINDEX INDEX bm25_test.docs_bm25; REINDEX INDEX bm25_test.stop_bm25;")
            self.result["status"] = "passed"
        except BaseException as exc:
            failure = exc
            self.result["status"] = "failed"
            self.result["error"] = str(exc)
            self.checkpoint()
        finally:
            # Preserve all observed plans/results before deleting this suite's
            # objects. The outer runner remains sole owner of cluster cleanup.
            try:
                self.run("DROP SCHEMA bm25_test CASCADE; SELECT to_jsonb(to_regnamespace('bm25_test') IS NULL);", "schema-cleanup")
                self.result["schema_removed"] = True
            except BaseException as exc:
                self.result["cleanup_error"] = str(exc)
                self.result["status"] = "failed"
                failure = failure or exc
            self.checkpoint()
        if failure:
            raise failure
        print(f"BM25 suite passed: {self.result['assertions']} assertions; evidence {self.evidence / 'bm25-results.json'}", flush=True)


if __name__ == "__main__":
    Suite().execute()
