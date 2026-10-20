#!/usr/bin/env python3
"""Real BM25 plans/candidates; independent domain quality remains separate."""
import html
import json
import os
from pathlib import Path
import subprocess
import time
from support import Session, literal, record_quality_case

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests/fixtures/bm25/corpus.json").read_text())
LIMIT = 100000


def json_values(output):
    decoder, values = json.JSONDecoder(), []
    remaining = output.lstrip()
    while remaining:
        value, end = decoder.raw_decode(remaining)
        values.append(value)
        remaining = remaining[end:].lstrip()
    return values


def nodes(value):
    if isinstance(value, dict):
        if "Node Type" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def index_nodes(plan, index):
    return [node for node in nodes(plan) if node.get("Node Type") == "Index Scan" and node.get("Index Name") == index]


class Suite:
    def __init__(self):
        self.evidence = Path(os.environ["PGCB_RUN_DIR"])
        self.temp = Path(os.environ["PGCB_TEMP_DIR"])
        self.bindir = Path(subprocess.check_output([os.environ["PG_CONFIG"], "--bindir"], text=True).strip())
        self.psql = [str(self.bindir / "psql"), "-X", "-qAt", "-v", "ON_ERROR_STOP=1"]
        self.result = {"status": "running", "fixture_basis": FIXTURE["basis"], "stages": [], "commands": [],
                       "assertions": 0, "domain_quality": [], "scores": [], "configuration_states": []}
        self.sequence = 0
        self.deleted = set()
        self.incremental = set()

    def checkpoint(self):
        temporary = self.evidence / "bm25-results.json.tmp"
        temporary.write_text(json.dumps(self.result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.evidence / "bm25-results.json")

    def require(self, condition, message):
        self.checkpoint()
        if not condition:
            raise AssertionError(message)
        self.result["assertions"] += 1

    def run(self, sql, label):
        self.sequence += 1
        stem = f"bm25-{self.sequence:03d}-{label}"
        (self.evidence / (stem + ".sql")).write_text(sql)
        started = time.monotonic()
        try:
            proc = subprocess.run(self.psql, input="SET standard_conforming_strings=on;\n" + sql,
                                  text=True, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired as error:
            def text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            (self.evidence / (stem + ".stdout")).write_text(text(error.stdout))
            (self.evidence / (stem + ".stderr")).write_text(text(error.stderr))
            self.result["commands"].append({"label":label,"exit_code":"timeout","evidence":stem})
            self.checkpoint()
            raise
        (self.evidence / (stem + ".stdout")).write_text(proc.stdout)
        (self.evidence / (stem + ".stderr")).write_text(proc.stderr)
        self.result["commands"].append({"label": label, "exit_code": proc.returncode,
            "seconds": round(time.monotonic() - started, 3), "evidence": stem})
        self.checkpoint()
        if proc.returncode:
            raise AssertionError(f"{label}: psql exit {proc.returncode}; see {stem}.stderr: {proc.stderr[-2000:]}")
        return json_values(proc.stdout)

    def session_value(self, session, sql, label):
        self.sequence += 1
        stem = f"bm25-{self.sequence:03d}-{label}"
        (self.evidence / (stem + ".sql")).write_text(sql)
        try:
            output = session.query(sql)
        except BaseException as error:
            (self.evidence / (stem + ".error")).write_text(repr(error))
            raise
        (self.evidence / (stem + ".stdout")).write_text(output + "\n")
        self.result["commands"].append({"label": label, "backend_pid": session.pid, "evidence": stem})
        self.checkpoint()
        if "ERROR:" in output:
            raise AssertionError(label + ": " + output)
        return json.loads(output)

    def populate(self):
        rows = ",".join(f"({number},{literal(body)})" for number, body in FIXTURE["documents"])
        self.run(f"INSERT INTO bm25_test.docs VALUES {rows};\n"
                 "INSERT INTO bm25_test.docs SELECT g,'cobaltxylophone' FROM generate_series(100,180)g;\n"
                 "INSERT INTO bm25_test.no_stop_docs SELECT * FROM bm25_test.docs;\n"
                 "INSERT INTO bm25_test.keep_docs SELECT * FROM bm25_test.docs;\n"
                 "CREATE INDEX docs_bm25 ON bm25_test.docs USING bm25(body)WITH(text_config='public.charabia');\n"
                 "CREATE INDEX no_stop_bm25 ON bm25_test.no_stop_docs USING bm25(body)WITH(text_config='bm25_test.no_stop');\n"
                 "CREATE INDEX keep_bm25 ON bm25_test.keep_docs USING bm25(body)WITH(text_config='bm25_test.keep');\n"
                 "ANALYZE bm25_test.docs; ANALYZE bm25_test.no_stop_docs; ANALYZE bm25_test.keep_docs;", "bulk-build")

    def queries(self, full):
        for item in FIXTURE["queries"]:
            if not full and item["name"] not in ("chinese", "english_case", "surface_running", "unknown"):
                continue
            case = dict(item, table="docs", index="docs_bm25", config="public.charabia")
            if "expected_ids" in item:
                expected = set(item["expected_ids"]) - self.deleted
                if item["name"] in ("chinese", "english_case", "surface_running"):
                    expected |= self.incremental
                case["expected_ids"] = sorted(expected)
            yield case
        if full:
            yield dict(name="expression-boundary", text="PostgreSQL", table="expression_docs", index="expression_bm25",
                config="public.charabia", expression="(title||' '||body)", expected_ids=[1])
            yield dict(name="array-boundary", text="PostgreSQL", table="array_docs", index="array_bm25",
                config="public.charabia", expression="parts", native_expression="array_to_string(parts,' ')", expected_ids=[1])
            yield dict(name="frequency-cap", text="echo", table="frequency_docs", index="frequency_bm25",
                config="public.charabia", expected_ids=[1])
            yield dict(name="position-cap", text="tailword", table="frequency_docs", index="frequency_bm25",
                config="public.charabia", expected_ids=[2])
            yield dict(name="frequency-ranking", text="rankcanary", table="ranking_docs", index="ranking_bm25",
                config="bm25_test.no_stem", expected_ids=[1,2], expected_order=[2,1])

    def matrix(self, phase, full=False):
        stage = {"phase": phase, "queries": [], "prepared_counts": []}
        self.result["stages"].append(stage)
        cases = list(self.queries(full))
        for cache in ("on", "off"):
            for mode in ("force_custom_plan", "force_generic_plan"):
                sql = [f"SET pg_textsearch.memtable_cache_enabled={cache}; SET plan_cache_mode={mode};"]
                for number, case in enumerate(cases):
                    expression = case.get("expression", "body")
                    native = case.get("native_expression", expression)
                    ranked = (f"SELECT id,{expression} <@> to_bm25query(bm25_test.checked_query($1),'bm25_test.{case['index']}') AS score "
                              f"FROM bm25_test.{case['table']} ORDER BY {expression} <@> "
                              f"to_bm25query(bm25_test.checked_query($1),'bm25_test.{case['index']}') LIMIT {LIMIT}")
                    q, config = literal(case["text"]), literal(case["config"])
                    sql += [f"PREPARE ranked_{number}(text) AS SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'score',score)),'[]'::jsonb) FROM ({ranked})r;",
                        "SET enable_seqscan=on;", f"EXPLAIN(FORMAT JSON,VERBOSE,COSTS OFF) {ranked.replace('$1',q)};",
                        "SET enable_seqscan=off;",
                        f"EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) EXECUTE ranked_{number}({q});",
                        f"EXECUTE ranked_{number}({q});",
                        f"SELECT jsonb_build_object('oracle',coalesce((SELECT jsonb_agg(id ORDER BY id) FROM bm25_test.{case['table']} "
                        f"WHERE tsvector_to_array(to_tsvector({config},{native})) && tsvector_to_array(to_tsvector({config},bm25_test.checked_query({q})))),'[]'::jsonb),"
                        f"'all_ids',(SELECT jsonb_agg(id ORDER BY id) FROM bm25_test.{case['table']}),'query_lexemes',tsvector_to_array(to_tsvector({config},{q})));",]
                sql += ["SELECT jsonb_agg(jsonb_build_object('name',name,'generic_plans',generic_plans,'custom_plans',custom_plans)ORDER BY name)FROM pg_prepared_statements;"]
                values = self.run("\n".join(sql), f"{phase}-{cache}-{mode}")
                self.require(len(values) == len(cases)*4+1, f"unexpected output count in {phase}/{cache}/{mode}")
                for number, case in enumerate(cases):
                    default, plan, scored, oracle = values[number*4:number*4+4]
                    actual = [value["id"] for value in scored]
                    label = f"{phase}/{cache}/{mode}/{case['name']}"
                    record = dict(case, cache=cache, mode=mode, default_plan=default, plan=plan, scored=scored, **oracle)
                    stage["queries"].append(record)
                    self.require(len(oracle["all_ids"]) < LIMIT, label + ": LIMIT truncates oracle")
                    target = index_nodes(plan, case["index"])
                    if case.get("empty_analysis"):
                        self.require(oracle["query_lexemes"] == [], label + ": not the frozen empty analysis")
                    else:
                        self.require(bool(oracle["query_lexemes"]), label + ": unexpected empty analysis cannot use the fallback allowance")
                    if not target and not oracle["query_lexemes"]:
                        # Fixed upstream fallback, explicitly separate from indexed retrieval.
                        self.require(any(n.get("Node Type") == "Seq Scan" for n in nodes(plan)) and
                                     any(n.get("Node Type") == "Sort" for n in nodes(plan)), label + ": unknown empty fallback")
                        self.require(sorted(actual) == oracle["all_ids"] and all(v["score"] == 0 for v in scored),
                                     label + ": standalone empty fallback is not all rows at score zero")
                        record["path"] = "empty-standalone-fallback"
                    else:
                        self.require(bool(target) and all(n.get("Schema") == "bm25_test" and n.get("Actual Loops",0)>0 for n in target),
                                     label + ": actual target BM25 Index Scan missing")
                        self.require(sorted(actual) == oracle["oracle"], label + ": candidates disagree with native lexeme overlap")
                        if "expected_ids" in case:
                            self.require(sorted(actual) == case["expected_ids"], label + f": independent domain IDs {case['expected_ids']} != {actual}")
                        if "expected_order" in case:
                            self.require(actual == case["expected_order"], label + ": independent TF ranking differs")
                        record["path"] = "target-bm25-index-scan"
                    self.require(len(actual) == len(set(actual)), label + ": duplicate candidate IDs")
                    if phase == "bulk" and cache == "on" and mode == "force_custom_plan" and "expected_ids" in case:
                        case_id = "bm25-domain-" + case["name"]
                        self.result["domain_quality"].append(record_quality_case(case_id,
                            input=FIXTURE["documents"] if case["table"] == "docs" else case["table"],
                            query=case["text"], config=case["config"], expected=case["expected_ids"], actual=sorted(actual)))
                    if not case.get("empty_analysis"):
                        self.result["scores"].append({"phase":phase,"case":case["name"],"cache":cache,"mode":mode,
                            "score_and_rank":scored,"note":"Observed scores/ranks; no floating-point golden or score-neutrality claim"})
                counts = values[-1]
                expected_key = "generic_plans" if mode == "force_generic_plan" else "custom_plans"
                other_key = "custom_plans" if mode == "force_generic_plan" else "generic_plans"
                self.require(all(v[expected_key]>0 and v[other_key]==0 for v in counts), "prepared mode was not actually exercised")
                stage["prepared_counts"].append(dict(cache=cache,mode=mode,counts=counts))
                self.checkpoint()
        print(f"BM25 {phase}: actual plans/candidates checked", flush=True)

    def known_quality(self):
        frozen = json.loads((ROOT / "tests/fixtures/quality-known.json").read_text())["cases"]
        self.result["known_quality"] = []
        for case_id, case in frozen.items():
            q, body = literal(case["query"]), literal(case["input"])
            phrase = case_id == "english-short-phrase"
            query_expression = f"phraseto_tsquery('public.charabia',{q})" if phrase else f"charabia.query_any({q})"
            observation = self.run(
                f"SELECT jsonb_build_object('query_analysis',(SELECT jsonb_agg(to_jsonb(a)) FROM charabia.analyze({q})a),"
                f"'document_analysis',(SELECT jsonb_agg(to_jsonb(a)) FROM charabia.analyze({body})a),"
                f"'document_vector',to_tsvector('public.charabia',{body})::text,'query',{query_expression}::text,"
                f"'actual',to_tsvector('public.charabia',{body}) @@ {query_expression});", "known-" + case_id)[0]
            self.result["known_quality"].append(dict(id=case_id, frozen=case, observation=observation,
                note="Unresolved domain failure, never counted as passing default retrieval"))
            analysis = [a for a in observation["query_analysis"] if a["kind"] in ("word", "stop")]
            language = "nob" if phrase else "dan" if case_id == "english-den" else "swe"
            self.require(analysis and all(a["language"] == language for a in analysis), case_id + ": failure changed selected language")
            self.require(observation["actual"] is False, case_id + ": unexpected improvement requires review")
            if not phrase:
                self.require(len(analysis) == 1 and analysis[0]["kind"] == "stop" and analysis[0]["terms"] == [],
                             case_id + ": not the frozen stop deletion mechanism")
                self.require(any(a["raw"] == case["query"] and a["kind"] == "word" for a in observation["document_analysis"]),
                             case_id + ": document no longer retains the domain word")
            if phrase:
                # The bag path still retrieves the original document; it does
                # not prove the separate domain phrase expectation true.
                observation["bag_ids"] = self.check_query("public.charabia","docs","docs_bm25",case["query"],[1],"known-phrase-bag")
                same_context = self.run(f"SELECT to_jsonb(to_tsvector('public.charabia',{body}) @@ phraseto_tsquery('public.charabia',{body}));", "same-context-phrase")[0]
                self.require(same_context is True,"same-context positions/variants failed independently of short-query selection")
            else:
                guarded = ("SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM(SELECT id FROM bm25_test.docs "
                    f"WHERE numnode(charabia.query_any({q}))>0 ORDER BY body <@> to_bm25query({q},'bm25_test.docs_bm25') LIMIT 100)r")
                guarded_ids = self.run(guarded,"known-guarded-bag-"+case_id)[0]
                observation["explicit_empty_guard_bm25_ids"] = guarded_ids
                self.require(guarded_ids==[],case_id+": explicit empty-query guard changed result shape")
            signature = dict(case)
            self.result["domain_quality"].append(record_quality_case(case_id, **dict(signature, actual=observation["actual"]), known_signature=signature))
            for config, table, index in (("bm25_test.no_stop","no_stop_docs","no_stop_bm25"),
                                          ("bm25_test.keep","keep_docs","keep_bm25")):
                cfg = literal(config)
                restored = self.run(f"SELECT to_jsonb(to_tsvector({cfg},{body}) @@ " +
                    (f"phraseto_tsquery({cfg},{q})" if phrase else f"charabia.query_any({q},{cfg})") + ");",
                    "explicit-recovery-" + case_id + "-" + table)[0]
                self.require(restored is True, f"{case_id}: explicit {config} did not restore domain match")
                self.result["domain_quality"].append(record_quality_case(case_id + "-" + table,
                    input=case["input"],query=case["query"],config=config,expected=True,actual=restored))
                # For the phrase case the bag candidate is still document 1;
                # phrase success above is the separate actual filtering check.
                expected_ids = [1] if phrase else [11] if case_id == "english-den" else [12]
                if case_id == "english-den" and table == "no_stop_docs":
                    # Turning stop off also retains the actual Swedish den in
                    # document 5; recovery is not a precision-neutral change.
                    expected_ids = [5,11]
                self.check_query(config, table, index, case["query"], expected_ids, "recovery-" + case_id + "-" + table)
        # Keep preserves a stop key, not the unstemmed surface as the sole term.
        keeps_stemming = self.run("SELECT to_jsonb(tsvector_to_array(to_tsvector('bm25_test.keep'," +
            literal(frozen["english-short-phrase"]["input"]) + ")) @> ARRAY['run','running','were']::text[]);", "keep-does-not-disable-stem")[0]
        self.require(keeps_stemming is True, "targeted keep accidentally disabled stemming")

    def check_query(self, config, table, index, query, expected, label):
        expression = (f"SELECT id,body <@> to_bm25query({literal(query)},'bm25_test.{index}') AS score "
            f"FROM bm25_test.{table} ORDER BY body <@> to_bm25query({literal(query)},'bm25_test.{index}') LIMIT {LIMIT}")
        plan, rows, oracle = self.run("SET enable_seqscan=off;\n" +
            "EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + expression + ";\n" +
            "SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]'::jsonb)FROM(" + expression + ")q;\n" +
            f"SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM bm25_test.{table} "
            f"WHERE tsvector_to_array(to_tsvector({literal(config)},body)) && "
            f"tsvector_to_array(to_tsvector({literal(config)},{literal(query)}));", label)
        ids = sorted(row["id"] for row in rows)
        record = dict(label=label,config=config,index=index,query=query,plan=plan,rows=rows,oracle=oracle,expected=expected)
        self.result.setdefault("focused_queries",[]).append(record)
        target = index_nodes(plan,index)
        self.require(target and all(n.get("Actual Loops",0)>0 for n in target), label + ": missing executed target index")
        self.require(ids == oracle == expected, label + f": expected {expected}, actual {ids}, native {oracle}")
        return ids

    def bag_and_headline(self):
        # BM25 bag-of-terms and native AND/phrase have different domain meanings.
        any_query, plain = self.run("SELECT to_jsonb(charabia.query_any('全文 banana')::text);"
            "SELECT to_jsonb(ARRAY(SELECT id FROM bm25_test.docs WHERE to_tsvector('public.charabia',body) "
            "@@ plainto_tsquery('public.charabia','全文 banana') ORDER BY id));", "bag-versus-and")
        self.require(" | " in any_query and " & " not in any_query and plain == [], "query_any does not represent plain OR")
        self.check_query("public.charabia","docs","docs_bm25","全文 banana",[2,10],"bag-independent-candidates")
        raw_query = "全文 running"
        ranked = ("WITH p AS MATERIALIZED(SELECT charabia.query_any(" + literal(raw_query) + ",'public.charabia')AS q),"
            "ranked AS MATERIALIZED(SELECT id,body,body <@> to_bm25query(" + literal(raw_query) + ",'bm25_test.docs_bm25')AS score "
            "FROM bm25_test.docs WHERE(SELECT numnode(q)>0 FROM p)ORDER BY body <@> to_bm25query(" +
            literal(raw_query) + ",'bm25_test.docs_bm25')LIMIT 1)SELECT r.id,r.score,r.body,"
            "ts_headline('public.charabia',r.body,p.q,'HighlightAll=true')AS headline FROM ranked r CROSS JOIN p ORDER BY r.score")
        plan, rows = self.run("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + ranked +
            ";SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]'::jsonb)FROM("+ranked+")r;", "materialized-query-topk-headline")
        self.result["topk_headline"] = dict(query=raw_query,sql=ranked,plan=plan,rows=rows,
            query_analysis_cost="BM25 and query_any each analyze original query once through separate PG interfaces")
        target = index_nodes(plan,"docs_bm25")
        p_nodes = [n for n in nodes(plan) if n.get("Subplan Name") == "CTE p"]
        self.require(target and all(n.get("Actual Loops",0)>0 for n in target), "top-k headline did not use target BM25 index")
        self.require(len(p_nodes)==1 and p_nodes[0].get("Actual Loops")==1 and p_nodes[0].get("Actual Rows")==1,
                     "query_any CTE was not constructed exactly once")
        self.require(len(rows)==1 and rows[0]["id"] in (1,2), "top-k changed raw-query bag candidates")
        for row in rows:
            restored = html.unescape(row["headline"].replace("<b>","").replace("</b>",""))
            self.require(restored == row["body"] and "<b>" in row["headline"], "top-k headline lost source or matching terms")
        guarded_empty = ranked.replace(literal(raw_query),"''")
        empty = self.run("SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]'::jsonb)FROM("+guarded_empty+")r;", "guard-empty-query")[0]
        self.require(empty == [], "explicit complete-query guard did not prevent empty BM25 fallback rows")

    def chunk_control(self):
        observations = []
        for config,table,index in (("pg_catalog.simple","chunk_docs","chunk_bm25"),
                                   ("public.charabia","bypass_docs","bypass_bm25")):
            for large in (False,True):
                query = "'chunkleft '||repeat(' ',262144)||' chunkright'" if large else "'chunkright'"
                ranked = f"SELECT id FROM bm25_test.{table} ORDER BY body <@> to_bm25query({query},'bm25_test.{index}')LIMIT 100"
                plan,ids = self.run("SET enable_seqscan=off;EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF)"+ranked+
                    ";SELECT coalesce(jsonb_agg(id),'[]'::jsonb)FROM("+ranked+")r;",f"upstream-bypass-{table}-{large}")
                observations.append(dict(config=config,large_query=large,plan=plan,ids=ids,
                    meaning="Out-of-contract bypass; chunked success is not a whole-document language/position guarantee"))
                self.result["upstream_chunk_control"] = observations
                target = index_nodes(plan,index)
                self.require(target and all(n.get("Actual Loops",0)>0 for n in target) and ids==[1], "upstream chunk control did not exercise target index")

    def ambiguous_index_guard(self):
        # Root literal queries can expose a foldable explicit-index requirement
        # before path selection. The earlier prepared/subquery failure is a
        # different planner shape, not a reason direct queries must fail.
        # Always reject a wrong scan; never require a particular planner choice.
        self.run("CREATE TABLE bm25_test.ambiguous_docs(LIKE bm25_test.control_a_docs INCLUDING ALL);"
            "INSERT INTO bm25_test.ambiguous_docs SELECT * FROM bm25_test.control_a_docs;"
            "ALTER TABLE bm25_test.ambiguous_docs ADD COLUMN title text;"
            "UPDATE bm25_test.ambiguous_docs SET title=body;"
            "CREATE INDEX ambiguous_a_bm25 ON bm25_test.ambiguous_docs USING bm25(body)WITH(text_config='bm25_test.control');"
            "CREATE INDEX ambiguous_b_bm25 ON bm25_test.ambiguous_docs USING bm25(body)WITH(text_config='bm25_test.no_stem');"
            "ANALYZE bm25_test.ambiguous_docs;", "ambiguous-same-column-build")
        observation = {"contract":"The requested index must actually execute, or a real mismatch must fail closed; direct literal plans may select both correctly",
                       "attempts":[],"first_failure_evidence":"artifacts/20261020T221128Z-83984-amd64/20261020T221128Z-83984-amd64-inner/bm25-038-two-independent-configs-a-custom-plan.stdout",
                       "source":"pg_textsearch fa142d049d08eac45a263f5abc250c8877ae076e src/planner/hooks.c:1497-1518,1670-1746,1807-1844; test/sql/explicit_index.sql wrong-column regression"}
        self.result["ambiguous_same_column_guard"] = observation
        with Session() as session:
            session.value("SET client_min_messages=warning;SET enable_seqscan=off")
            for family,other in (("a","b"),("b","a")):
                requested = f"ambiguous_{family}_bm25"
                sql = ("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF)"
                    f"SELECT id FROM bm25_test.ambiguous_docs ORDER BY body <@> to_bm25query('run','bm25_test.{requested}')LIMIT 100")
                output = session.query(sql)
                record = dict(requested_index=requested,sql=sql,output=output)
                observation["attempts"].append(record)
                self.checkpoint()
                if "ERROR:" in output:
                    expected = f'query specifies index "{requested}" but planner chose index "ambiguous_{other}_bm25"'
                    self.require("42809" in output and expected in output and
                                 "consistent tokenization" in output,
                                 "ambiguous index case failed for an unexpected reason")
                    record["path"] = "upstream-explicit-index-mismatch-rejected"
                else:
                    plan = json.loads(output)
                    target = index_nodes(plan,requested)
                    self.require(target and all(n.get("Actual Loops",0)>0 for n in target),
                                 "ambiguous query executed another index without failing closed")
                    record["path"] = "requested-index-executed"
                self.require(session.value("SELECT 1")=="1","backend did not recover from explicit-index mismatch")
            # Deterministic negative case from upstream's explicit_index.sql:
            # the named index is on body, while the scored Var is title. The
            # transform validates the column before any planner choice occurs.
            sql = ("EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF)SELECT id FROM bm25_test.ambiguous_docs "
                "ORDER BY title <@> to_bm25query('run','bm25_test.ambiguous_a_bm25')LIMIT 100")
            output = session.query(sql)
            observation["wrong_column"] = dict(sql=sql,output=output,expected_sqlstate="42809")
            self.checkpoint()
            self.require('ERROR:' in output and '42809' in output and
                         'index "ambiguous_a_bm25" is not on column "title"' in output and
                         'The explicitly specified index is not built on the column being searched.' in output,
                         "explicit index on a wrong column did not fail for the exact upstream reason")
            self.require(session.value("SELECT 1")=="1","backend did not recover from wrong-column error")
        observation["observed_mismatch_rejections"] = sum(v.get("path")=="upstream-explicit-index-mismatch-rejected" for v in observation["attempts"])
        self.run("DROP TABLE bm25_test.ambiguous_docs;","ambiguous-case-cleanup")

    def configuration_lifecycle(self):
        body = literal(FIXTURE["documents"][0][1])
        self.run("DO $$BEGIN PERFORM charabia.create_config('bm25_test.control','{\"builtin_stopwords\":false}'); "
            "PERFORM charabia.create_config('bm25_test.control_new','{\"builtin_stopwords\":false}');END$$;"
            "CREATE TABLE bm25_test.control_a_docs(id integer PRIMARY KEY,body text NOT NULL);"
            "CREATE TABLE bm25_test.control_b_docs(LIKE bm25_test.control_a_docs INCLUDING ALL);"
            f"INSERT INTO bm25_test.control_a_docs VALUES(1,{body}),(9,'run');"
            "INSERT INTO bm25_test.control_b_docs SELECT * FROM bm25_test.control_a_docs;"
            "CREATE INDEX control_a_bm25 ON bm25_test.control_a_docs USING bm25(body)WITH(text_config='bm25_test.control');"
            "CREATE INDEX control_b_bm25 ON bm25_test.control_b_docs USING bm25(body)WITH(text_config='bm25_test.no_stem');"
            "ANALYZE bm25_test.control_a_docs;ANALYZE bm25_test.control_b_docs;", "two-config-build")
        self.ambiguous_index_guard()
        with Session() as session:
            session.value("SET client_min_messages=warning")
            for family in ("a","b"):
                for mode in ("custom","generic"):
                    statement = (f"SELECT id FROM bm25_test.control_{family}_docs ORDER BY body <@> "
                        f"to_bm25query($1,'bm25_test.control_{family}_bm25') LIMIT 100")
                    session.value(f"PREPARE control_{family}_{mode}(text)AS SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM({statement})q")
            prepared_at = self.session_value(session,
                "SELECT jsonb_object_agg(name,prepare_time)FROM pg_prepared_statements", "prepared-identities")
            original_oid = self.session_value(session,"SELECT to_jsonb('bm25_test.control'::regconfig::oid)","config-oid-before")

            def state(label, expected_a, expected_native_a, config_a):
                record = {"label":label,"config_a":config_a,"queries":[]}
                self.result["configuration_states"].append(record)
                # Parameterized PREPARE remains alive across every ALTER/REINDEX.
                # Config-native oracle is fresh heap analysis, not stale postings.
                for family,config,expected,native_expected in (
                    ("a",config_a,expected_a,expected_native_a),
                    ("b","bm25_test.no_stem",[9],[9])):
                    native_sql = (f"SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM bm25_test.control_{family}_docs "
                        f"WHERE tsvector_to_array(to_tsvector({literal(config)},body)) && "
                        f"tsvector_to_array(to_tsvector({literal(config)},'run'))")
                    oracle = self.session_value(session,native_sql,label+"-"+family+"-native")
                    self.require(oracle==native_expected, label+": fresh config-native expected mismatch")
                    for suffix,mode in (("custom","force_custom_plan"),("generic","force_generic_plan")):
                        session.value("SET enable_seqscan=off;SET plan_cache_mode="+mode)
                        execute = f"EXECUTE control_{family}_{suffix}('run')"
                        plan = self.session_value(session,"EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF)"+execute,
                            label+"-"+family+"-"+suffix+"-plan")
                        ids = self.session_value(session,execute,label+"-"+family+"-"+suffix+"-ids")
                        entry = dict(family=family,mode=mode,plan=plan,ids=ids,native=oracle,expected=expected)
                        record["queries"].append(entry)
                        target = index_nodes(plan,f"control_{family}_bm25")
                        self.require(target and all(n.get("Actual Loops",0)>0 for n in target),label+": executed prepared target scan missing")
                        self.require(ids==expected,label+f": expected prepared IDs {expected}, got {ids}")
                stats = self.session_value(session,
                    "SELECT jsonb_agg(jsonb_build_object('name',name,'prepare_time',prepare_time,'generic_plans',generic_plans,'custom_plans',custom_plans)ORDER BY name)FROM pg_prepared_statements",
                    label+"-prepared-counts")
                record["prepared"] = stats
                self.require(all(v["prepare_time"]==prepared_at[v["name"]] for v in stats), "prepared statements were silently recreated")
                self.require(all(v["generic_plans"]>0 and v["custom_plans"]==0 if v["name"].endswith("generic") else
                                 v["custom_plans"]>0 and v["generic_plans"]==0 for v in stats),"wrong prepared-plan mode")
                self.checkpoint()

            state("two-independent-configs",[1,9],[1,9],"bm25_test.control")
            self.run("DO $$BEGIN PERFORM charabia.alter_config('bm25_test.control','{\"stemming\":false,\"builtin_stopwords\":false}');END$$;", "alter-same-oid")
            after_oid = self.session_value(session,"SELECT to_jsonb('bm25_test.control'::regconfig::oid)","config-oid-after")
            self.require(after_oid==original_oid,"alter_config replaced the configuration OID")
            state("same-oid-stale-postings",[1,9],[9],"bm25_test.control")
            self.run("REINDEX INDEX bm25_test.control_a_bm25;","same-oid-reindex")
            state("same-oid-rebuilt",[9],[9],"bm25_test.control")
            new_oid = self.session_value(session,"SELECT to_jsonb('bm25_test.control_new'::regconfig::oid)","new-config-oid")
            self.require(new_oid!=original_oid,"replacement instance did not have independent OID")
            # ALTER changes reloptions, but the pinned access method reads the
            # old metapage OID until rebuild. Do not call ALTER alone migration.
            self.run("ALTER INDEX bm25_test.control_a_bm25 SET(text_config='bm25_test.control_new');","set-new-config-reloption")
            state("reloption-only-old-metapage",[9],[9],"bm25_test.control")
            self.run("REINDEX INDEX bm25_test.control_a_bm25;","new-oid-reindex")
            state("new-oid-rebuilt",[1,9],[1,9],"bm25_test.control_new")
            self.result["configuration_oids"] = dict(original=original_oid,replacement=new_oid,
                reloptions=self.session_value(session,"SELECT to_jsonb(reloptions)FROM pg_class WHERE oid='bm25_test.control_a_bm25'::regclass","new-oid-reloptions"))

    def spill(self):
        self.run("INSERT INTO bm25_test.docs VALUES(30,'spillcontrolcanary');","spill-live-canary")
        names = ("docs_bm25","no_stop_bm25","keep_bm25","frequency_bm25","ranking_bm25")
        self.result["spill"] = self.run("\n".join(
            f"SELECT jsonb_build_object('index',{literal(index)},'segment_root',bm25_spill_index('bm25_test.{index}'));"
            for index in names),"spill-indices")
        values = {v["index"]:v["segment_root"] for v in self.result["spill"]}
        self.require(all(value is None or type(value)is int and value>0 for value in values.values()),"invalid nullable spill block number")
        self.require(values["docs_bm25"] is not None,"fresh memtable was not spilled")
        self.require(values["frequency_bm25"] is None and values["ranking_bm25"] is None,"unmodified bulk-only index was not a no-op spill")
        self.matrix("spill")

    def restart(self):
        marker = self.temp / ".pg-charabia-owner"
        self.require(marker.is_file() and marker.read_text()==self.evidence.name,"refusing restart: ownership marker mismatch")
        before = self.run("SELECT jsonb_build_object('data_directory',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'start',pg_postmaster_start_time());","restart-before")[0]
        self.require(Path(before["data_directory"]).resolve()==(self.temp/"data").resolve() and before["socket"]==str(self.temp/"socket"),"refusing restart of unknown cluster")
        command = [str(self.bindir/"pg_ctl"),"-D",str(self.temp/"data"),"-l",str(self.temp/"server.log"),"-m","fast","-w","-t","60","restart"]
        proc = subprocess.run(command,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
        (self.evidence/"bm25-restart.log").write_text(proc.stdout)
        self.result["restart"] = dict(argv=command,exit_code=proc.returncode,before=before)
        self.require(proc.returncode==0,"owned-cluster restart failed")
        with Session() as session:
            after = self.session_value(session,"SELECT jsonb_build_object('start',pg_postmaster_start_time(),'info',charabia.analysis_info())","restart-after")
            mappings = Path(f"/proc/{session.pid}/maps").read_text()
            (self.evidence/"bm25-restarted-backend-maps.txt").write_text(mappings)
            after["backend_pid"] = session.pid
            after["loaded_library_mappings"] = "\n".join(line for line in mappings.splitlines()if "pg_charabia" in line)
        self.result["restart"]["after"] = after
        self.require(before["start"]!=after["start"] and bool(after["loaded_library_mappings"]),"restarted backend did not load the staged library")
        self.matrix("restart")

    def execute(self):
        failure = None
        try:
            self.populate()
            self.matrix("bulk",full=True)
            self.known_quality()
            self.bag_and_headline()
            self.chunk_control()
            self.configuration_lifecycle()
            self.run("INSERT INTO bm25_test.docs VALUES(20,'中文全文检索 PostgreSQL running');","incremental-insert")
            self.incremental.add(20)
            self.matrix("insert")
            self.run("UPDATE bm25_test.docs SET body='replacementcanary'WHERE id=20;","incremental-update")
            self.incremental.remove(20)
            self.matrix("update")
            self.run("DELETE FROM bm25_test.docs WHERE id=1;","incremental-delete")
            self.deleted.add(1)
            self.matrix("delete")
            self.run("VACUUM bm25_test.docs;","vacuum")
            self.matrix("vacuum")
            self.spill()
            self.run("INSERT INTO bm25_test.docs VALUES(21,'中文全文检索 PostgreSQL running');","post-spill-insert")
            self.incremental.add(21)
            self.matrix("post-spill-insert")
            self.restart()
            self.run("REINDEX INDEX bm25_test.docs_bm25;","final-reindex")
            self.matrix("reindex")
            self.result["status"] = "passed"
        except BaseException as error:
            failure = error
            self.result["status"] = "failed"
            self.result["error"] = repr(error)
            self.checkpoint()
        finally:
            # The outer runner owns the cluster; this suite owns only its schema.
            # Preserve first failure before deleting suite-owned database objects.
            try:
                removed = self.run("DROP SCHEMA bm25_test CASCADE;SELECT to_jsonb(to_regnamespace('bm25_test')IS NULL);","schema-cleanup")
                self.result["schema_removed"] = removed==[True]
                self.require(self.result["schema_removed"],"suite schema survived cleanup")
            except BaseException as error:
                self.result["cleanup_error"] = repr(error)
                self.result["status"] = "failed"
                failure = failure or error
            self.checkpoint()
        if failure:
            raise failure
        print("BM25 mechanisms passed; known default language quality failures remain separately reported",flush=True)


if __name__ == "__main__":
    Suite().execute()
