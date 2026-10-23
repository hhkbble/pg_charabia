#!/usr/bin/env python3
"""Real BM25 plans/candidates; independent domain quality remains separate."""
import html
import json
import os
from pathlib import Path
import subprocess
import time
from support import Session, literal, native_config_sql, record_quality_cases

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
        self.pending_quality = []

    def checkpoint(self):
        temporary = self.evidence / "bm25-results.json.tmp"
        temporary.write_text(json.dumps(self.result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.evidence / "bm25-results.json")

    def require(self, condition, message):
        self.checkpoint()
        if not condition:
            raise AssertionError(message)
        self.result["assertions"] += 1

    def queue_quality(self, case_id, **fields):
        # Preserve every domain assertion, but collect independent mechanism
        # evidence before the shared contract rejects any unreviewed result.
        if any(item["id"] == case_id for item in self.pending_quality):
            raise AssertionError("Duplicate pending domain assertion: " + case_id)
        self.pending_quality.append(dict(id=case_id, **fields))
        signature = fields.get("known_signature")
        frozen_match = signature is not None and all(fields[key] == signature[key] for key in ("input", "query", "config", "expected", "actual"))
        status = "known-domain-failure" if frozen_match else "domain-failure" if fields["actual"] != fields["expected"] else "unreviewed-improvement" if signature else "passed"
        self.result.setdefault("deferred_domain_assertions", []).append(dict(
            id=case_id, expected=fields["expected"], actual=fields["actual"], status=status))
        self.result["pending_quality"] = self.pending_quality
        self.checkpoint()

    def flush_quality(self):
        self.result["domain_quality"] = record_quality_cases(self.pending_quality)
        self.checkpoint()

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
        self.run(native_config_sql("bm25_test.no_stop", stem=True, discard=False) +
                 native_config_sql("bm25_test.no_stem", stem=False, discard=False) +
                 f"INSERT INTO bm25_test.docs VALUES {rows};\n"
                 "INSERT INTO bm25_test.docs SELECT g,'cobaltxylophone' FROM generate_series(100,180)g;\n"
                 "INSERT INTO bm25_test.no_stop_docs SELECT * FROM bm25_test.docs;\n"
                 "CREATE INDEX docs_bm25 ON bm25_test.docs USING bm25(body)WITH(text_config='public.charabia');\n"
                 "CREATE INDEX no_stop_bm25 ON bm25_test.no_stop_docs USING bm25(body)WITH(text_config='bm25_test.no_stop');\n"
                 "CREATE INDEX ranking_bm25 ON bm25_test.ranking_docs USING bm25(body)WITH(text_config='bm25_test.no_stem');\n"
                 "ANALYZE bm25_test.docs; ANALYZE bm25_test.no_stop_docs;", "bulk-build")

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
                        record["frozen_empty_analysis"] = []
                        record["frozen_empty_analysis_matches"] = oracle["query_lexemes"] == []
                        if phase == "bulk" and cache == "on" and mode == "force_custom_plan":
                            self.queue_quality("bm25-analysis-" + case["name"], input=case["text"], query=case["text"],
                                config=case["config"], expected=[], actual=oracle["query_lexemes"])
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
                        if "expected_order" in case:
                            self.require(actual == case["expected_order"], label + ": independent TF ranking differs")
                        record["path"] = "target-bm25-index-scan"
                    self.require(len(actual) == len(set(actual)), label + ": duplicate candidate IDs")
                    if "expected_ids" in case:
                        case_id = "bm25-domain-" + case["name"] if phase == "bulk" and cache == "on" and mode == "force_custom_plan" else (
                            "bm25-domain-" + phase + "-" + cache + "-" + mode + "-" + case["name"])
                        self.queue_quality(case_id,
                            input=FIXTURE["documents"] if case["table"] == "docs" else case["table"],
                            query=case["text"], config=case["config"], expected=case["expected_ids"], actual=sorted(actual))
                    if oracle["query_lexemes"]:
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
                f"'document_vector',to_tsvector('public.charabia',{body})::text,"
                f"'document_lexemes',tsvector_to_array(to_tsvector('public.charabia',{body})),"
                f"'query_lexemes',tsvector_to_array(to_tsvector('public.charabia',{q})),"
                f"'dictionary_decisions',(SELECT jsonb_agg(jsonb_build_object('raw',a.raw,'language',a.language,"
                f"'lexemes',ts_lexize(('charabia.'||a.language)::regdictionary,a.raw)))FROM charabia.analyze({q})a WHERE a.kind='word'),"
                f"'query',{query_expression}::text,"
                f"'actual',to_tsvector('public.charabia',{body}) @@ {query_expression});", "known-" + case_id)[0]
            self.result["known_quality"].append(dict(id=case_id, frozen=case, observation=observation,
                note="Original domain expectation and failure signature retained; current analysis is reviewed separately"))
            # A new candidate set must not pretend dan/swe/nob are reachable.
            # Actual selected languages and dictionary results above are evidence,
            # not expected values. Only the exact frozen signature is exempt.
            analysis = [a for a in observation["query_analysis"] or [] if a["kind"] == "word"]
            self.require(bool(analysis), case_id + ": query no longer has a real Word token")
            if observation["query_lexemes"]:
                observation["default_bag_ids"] = self.check_query(
                    "public.charabia", "docs", "docs_bm25", case["query"], None, "current-default-bag-" + case_id)
            else:
                guarded = ("SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM(SELECT id FROM bm25_test.docs "
                    f"WHERE numnode(charabia.query_any({q}))>0 ORDER BY body <@> to_bm25query({q},'bm25_test.docs_bm25') LIMIT 100)r")
                guarded_ids = self.run(guarded, "known-guarded-bag-" + case_id)[0]
                observation["explicit_empty_guard_bm25_ids"] = guarded_ids
                self.require(guarded_ids == [], case_id + ": empty-query guard did not suppress fallback rows")
            if phrase:
                same_context = self.run(f"SELECT to_jsonb(to_tsvector('public.charabia',{body}) @@ phraseto_tsquery('public.charabia',{body}));", "same-context-phrase")[0]
                self.require(same_context is True, "same-context positions/variants failed independently of short-query selection")
            self.queue_quality(case_id, **dict(case, actual=observation["actual"]), known_signature=dict(case))
            config, table, index = "bm25_test.no_stop", "no_stop_docs", "no_stop_bm25"
            cfg = literal(config)
            restored = self.run(f"SELECT to_jsonb(to_tsvector({cfg},{body}) @@ " +
                (f"phraseto_tsquery({cfg},{q})" if phrase else f"charabia.query_any({q},{cfg})") + ");",
                "explicit-recovery-" + case_id + "-" + table)[0]
            self.require(restored is True, f"{case_id}: explicit {config} did not restore domain match")
            self.queue_quality(case_id + "-" + table,
                input=case["input"],query=case["query"],config=config,expected=True,actual=restored)
            # This is an explicit different policy, with its original candidate
            # requirement retained even when new OR alternatives change recall.
            expected_ids = [1] if phrase else [5,11] if case_id == "english-den" else [12]
            self.check_query(config, table, index, case["query"], expected_ids,
                             "recovery-" + case_id + "-" + table, domain_case_id=case_id + "-no-stop-bag")
        retains_stemming = self.run("SELECT to_jsonb(tsvector_to_array(to_tsvector('bm25_test.no_stop'," +
            literal(frozen["english-short-phrase"]["input"]) + ")) @> ARRAY['run','running','were']::text[]);",
            "no-stop-retains-stemming")[0]
        self.require(retains_stemming is True, "Discard=false accidentally disabled stemming")

    def check_query(self, config, table, index, query, expected, label, domain_case_id=None):
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
        self.require(ids == oracle, label + f": actual {ids} disagree with native overlap {oracle}")
        if domain_case_id:
            self.queue_quality(domain_case_id, input=table, query=query, config=config, expected=expected, actual=ids)
        elif expected is not None:
            self.require(ids == expected, label + f": expected {expected}, actual {ids}")
        return ids

    def bag_and_headline(self):
        # BM25 bag-of-terms and native AND/phrase have different domain meanings.
        any_query, plain = self.run("SELECT to_jsonb(charabia.query_any('全文 banana')::text);"
            "SELECT to_jsonb(ARRAY(SELECT id FROM bm25_test.docs WHERE to_tsvector('public.charabia',body) "
            "@@ plainto_tsquery('public.charabia','全文 banana') ORDER BY id));", "bag-versus-and")
        self.require(" | " in any_query and " & " not in any_query and plain == [], "query_any does not represent plain OR")
        self.check_query("public.charabia","docs","docs_bm25","全文 banana",[2,10],"bag-independent-candidates",
                         domain_case_id="bm25-bag-independent-candidates")
        raw_query = "全文 running"
        # Background rows affect ranking even when they are not candidates.
        # Keep the quality input frozen, but retain the complete scoring corpus.
        corpus, options = self.run(
            "SELECT jsonb_agg(jsonb_build_object('id',id,'body',body,"
            "'vector',to_tsvector('public.charabia',body)::text,"
            "'stored_vector_positions',(SELECT coalesce(sum(cardinality(positions)),0)"
            "FROM unnest(to_tsvector('public.charabia',body))))ORDER BY id)FROM bm25_test.docs;"
            "SELECT to_jsonb(reloptions)FROM pg_class WHERE oid='bm25_test.docs_bm25'::regclass;",
            "topk-complete-corpus-context")
        self.require([[row["id"],row["body"]] for row in corpus] ==
                     FIXTURE["documents"] + [[number,"cobaltxylophone"] for number in range(100,181)],
                     "top-k corpus differs from the semantic fixture and background recipe")
        ranked = ("WITH p AS MATERIALIZED(SELECT charabia.query_any(" + literal(raw_query) + ",'public.charabia')AS q),"
            "ranked AS MATERIALIZED(SELECT id,body,body <@> to_bm25query(" + literal(raw_query) + ",'bm25_test.docs_bm25')AS score "
            "FROM bm25_test.docs WHERE(SELECT numnode(q)>0 FROM p)ORDER BY body <@> to_bm25query(" +
            literal(raw_query) + ",'bm25_test.docs_bm25')LIMIT 1)SELECT r.id,r.score,r.body,"
            "ts_headline('public.charabia',r.body,p.q,'HighlightAll=true')AS headline FROM ranked r CROSS JOIN p ORDER BY r.score")
        plan, rows = self.run("EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF) " + ranked +
            ";SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]'::jsonb)FROM("+ranked+")r;", "materialized-query-topk-headline")
        self.result["topk_headline"] = dict(query=raw_query,sql=ranked,plan=plan,rows=rows,
            corpus_context=dict(rows=corpus,index_options=options,
                basis="Actual complete table before top-k; background rows affect corpus statistics. Stored vector positions are diagnostics, not a universal BM25 length oracle."),
            query_analysis_cost="BM25 and query_any each analyze original query once through separate PG interfaces")
        target = index_nodes(plan,"docs_bm25")
        p_nodes = [n for n in nodes(plan) if n.get("Subplan Name") == "CTE p"]
        self.require(target and all(n.get("Actual Loops",0)>0 for n in target), "top-k headline did not use target BM25 index")
        self.require(len(p_nodes)==1 and p_nodes[0].get("Actual Loops")==1 and p_nodes[0].get("Actual Rows")==1,
                     "query_any CTE was not constructed exactly once")
        oracle = self.run(
            "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM bm25_test.docs "
            "WHERE tsvector_to_array(to_tsvector('public.charabia',body)) && "
            "tsvector_to_array(to_tsvector('public.charabia'," + literal(raw_query) + "));",
            "materialized-query-topk-native-overlap")[0]
        self.result["topk_headline"].update(native_overlap_ids=oracle,
            original_domain_expectation={"rows":1,"allowed_ids":[1,2]})
        self.require(len(rows) == min(1, len(oracle)) and all(row["id"] in oracle for row in rows),
                     "top-k row is not a real native-overlap candidate or violates LIMIT")
        self.queue_quality("bm25-topk-domain", input=FIXTURE["documents"], query=raw_query,
            config="public.charabia", expected=True, actual=len(rows)==1 and rows[0]["id"] in (1,2))
        for row in rows:
            restored = html.unescape(row["headline"].replace("<b>","").replace("</b>",""))
            self.require(restored == row["body"] and "<b>" in row["headline"], "top-k headline lost source or matching terms")
        guarded_empty = ranked.replace(literal(raw_query),"''")
        empty = self.run("SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]'::jsonb)FROM("+guarded_empty+")r;", "guard-empty-query")[0]
        self.require(empty == [], "explicit complete-query guard did not prevent empty BM25 fallback rows")

    def symbol_protocol(self):
        # These IDs follow literal structures and the explicit component rules.
        # The control dictionary removes automatic-language quality from this
        # mechanism corpus; no existing default-policy corpus is changed.
        documents = [
            (1, "alpha https://example.com/Running?q=100ms beta"),
            (2, "alpha https://example.com/Other?q=200ms beta"),
            (3, "alpha User+tag@example.com beta"),
            (4, "alpha /src/running-tests.rs beta"),
            (5, "alpha 100ms beta"), (6, "alpha 200ms beta"),
            (7, "alpha 0x5fff beta"), (8, "alpha 5fff beta"),
            (9, "alpha standalone.example.net beta"),
            (10, "alpha unrelated zygomorphic beta"),
            (11, "alpha 100 appendix ms beta"),
        ]
        config = "bm25_test.symbols"
        rows = ",".join(f"({number},{literal(body)})" for number, body in documents)
        record = self.result["symbols"] = {
            "status": "running", "documents": documents, "queries": [],
            "basis": "Fixed literal structures, English running -> run, same-position full OR component-AND policy; not extension-generated expectations",
            "quality_boundary": "The native overlap oracle checks index consistency only; fixed IDs check this controlled corpus. Scattered components and bag widening are reported separately from structural equality.",
        }
        self.checkpoint()
        self.run("CREATE TEXT SEARCH DICTIONARY bm25_test.symbol_words(TEMPLATE=charabia.template,"
            "Language='eng',Discard=false,Stem=false,Normalize=true,Filter=false,English=false);"
            "CREATE TEXT SEARCH CONFIGURATION bm25_test.symbols(COPY=public.charabia);"
            "DO $symbols$ DECLARE l jsonb; BEGIN "
            "FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP "
            "EXECUTE format('ALTER TEXT SEARCH CONFIGURATION bm25_test.symbols ALTER MAPPING FOR %I WITH bm25_test.symbol_words',l->>'language');"
            "END LOOP;END $symbols$;"
            "CREATE TABLE bm25_test.symbol_docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));"
            f"INSERT INTO bm25_test.symbol_docs VALUES{rows};"
            "CREATE INDEX symbol_bm25 ON bm25_test.symbol_docs USING bm25(body)WITH(text_config='bm25_test.symbols');"
            "ANALYZE bm25_test.symbol_docs;", "symbol-build")
        record["vectors"] = self.run(
            "SELECT jsonb_agg(jsonb_build_object('id',d.id,'body',d.body,"
            "'vector',to_tsvector('bm25_test.symbols',d.body)::text,"
            "'tf',(SELECT jsonb_object_agg(lexeme,cardinality(positions)) FROM unnest(to_tsvector('bm25_test.symbols',d.body))),"
            "'dl',(SELECT sum(cardinality(positions)) FROM unnest(to_tsvector('bm25_test.symbols',d.body))))ORDER BY d.id)"
            "FROM bm25_test.symbol_docs d;", "symbol-vector-tf-dl")[0]
        record["tf_dl_meaning"] = "Stored tsvector position counts are analyzer TF/dl inputs; they are not decoded BM25 fieldnorms or score goldens."
        cases = [
            ("unit", "100ms", [1, 2, 5, 6, 11], [1, 5, 11]),
            ("url", "https://example.com/Running?q=100ms", [1, 2, 3, 4, 5, 6, 9, 11], [1]),
            ("email", "User+tag@example.com", [1, 2, 3, 9], [3]),
            ("file", "/src/running-tests.rs", [1, 4], [4]),
            ("host", "standalone.example.net", [1, 2, 3, 9], [9]),
            ("hex", "5fff", [7, 8], [7, 8]),
        ]
        for cache in ("on", "off"):
            for mode in ("force_custom_plan", "force_generic_plan"):
                for name, query, bag_expected, native_expected in cases:
                    q = literal(query)
                    ranked = ("SELECT id,body <@> to_bm25query($1,'bm25_test.symbol_bm25')AS score "
                        "FROM bm25_test.symbol_docs ORDER BY body <@> to_bm25query($1,'bm25_test.symbol_bm25')LIMIT 100")
                    default, plan, scored, oracle, counts = self.run(
                        f"SET pg_textsearch.memtable_cache_enabled={cache};SET plan_cache_mode={mode};"
                        "SET enable_seqscan=on;EXPLAIN(FORMAT JSON,VERBOSE,COSTS OFF) " + ranked.replace("$1", q) + ";"
                        "SET enable_seqscan=off;PREPARE symbol_rank(text)AS SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]')FROM(" + ranked + ")r;"
                        f"EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF)EXECUTE symbol_rank({q});"
                        f"EXECUTE symbol_rank({q});"
                        "SELECT jsonb_build_object('overlap',coalesce((SELECT jsonb_agg(id ORDER BY id)FROM bm25_test.symbol_docs "
                        "WHERE tsvector_to_array(to_tsvector('bm25_test.symbols',body)) && "
                        f"tsvector_to_array(to_tsvector('bm25_test.symbols',{q}))),'[]'),"
                        "'native',coalesce((SELECT jsonb_agg(id ORDER BY id)FROM bm25_test.symbol_docs "
                        f"WHERE to_tsvector('bm25_test.symbols',body) @@ plainto_tsquery('bm25_test.symbols',{q})),'[]'),"
                        f"'query',plainto_tsquery('bm25_test.symbols',{q})::text,'terms',tsvector_to_array(to_tsvector('bm25_test.symbols',{q})));"
                        "SELECT jsonb_build_object('custom',custom_plans,'generic',generic_plans)FROM pg_prepared_statements WHERE name='symbol_rank';",
                        f"symbol-{cache}-{mode}-{name}")
                    ids = sorted(row["id"] for row in scored)
                    observation = dict(name=name, query=query, cache=cache, mode=mode, default_plan=default,
                        plan=plan, scored=scored, expected_bag_ids=bag_expected, expected_native_ids=native_expected,
                        oracle=oracle, prepared_counts=counts,
                        ranking="Observed order/scores; no arbitrary tie ordering, score neutrality, or structural-equality claim")
                    record["queries"].append(observation)
                    self.checkpoint()
                    target = index_nodes(plan, "symbol_bm25")
                    self.require(target and all(n.get("Actual Loops", 0) > 0 for n in target), name + ": symbol BM25 index did not execute")
                    self.require(ids == oracle["overlap"], name + ": symbol BM25/native-overlap inconsistency")
                    self.require(ids == bag_expected and oracle["native"] == native_expected,
                                 name + ": fixed symbol corpus candidates or native grouping changed")
                    expected_key, other_key = ("custom", "generic") if mode == "force_custom_plan" else ("generic", "custom")
                    self.require(counts[expected_key] > 0 and counts[other_key] == 0,
                                 name + ": symbol prepared plan mode was not exercised")
        positions, whole, grouping = self.run(
            "SELECT jsonb_object_agg(lexeme,positions)FROM unnest(to_tsvector('bm25_test.symbols','alpha 100ms beta'));"
            "SELECT to_jsonb(ts_headline('bm25_test.symbols','alpha https://example.com/Running?q=100ms beta',"
            "'ms'::tsquery,'HighlightAll=true'));"
            "SELECT jsonb_build_object('plain',plainto_tsquery('bm25_test.symbols','100ms')::text,"
            "'any',charabia.query_any('100ms','bm25_test.symbols')::text,"
            "'internal_phrase',to_tsvector('bm25_test.symbols','100ms') @@ '100 <-> ms'::tsquery,"
            "'outer_phrase',to_tsvector('bm25_test.symbols','alpha 100ms beta') @@ phraseto_tsquery('bm25_test.symbols','alpha 100ms beta'),"
            "'scattered',to_tsvector('bm25_test.symbols','100 appendix ms') @@ plainto_tsquery('bm25_test.symbols','100ms'));",
            "symbol-position-headline")
        record.update(positions=positions, whole_headline=whole, grouping=grouping)
        record["precision_cost"] = {"input": "100 appendix ms", "query": "100ms",
            "domain_expectation_for_structural_equality": False, "actual_native_plain": grouping["scattered"],
            "meaning": "Explicit native component-conjunction precision cost; a true match is not counted as a structural-quality success or a new known-failure exemption."}
        self.require(positions == {"alpha": [1], "100ms": [2], "100": [2], "ms": [2], "beta": [3]},
                     "symbol components did not retain one atomic position")
        self.require(grouping["outer_phrase"] and not grouping["internal_phrase"] and grouping["scattered"] and
                     " & " in grouping["plain"] and " & " not in grouping["any"],
                     "symbol group, query_any, or fixed phrase distance changed")
        self.require(whole == "alpha <b>https://example.com/Running?q=100ms</b> beta", "symbol component headline lost the complete original URL")
        topk = ("WITH p AS MATERIALIZED(SELECT charabia.query_any('100ms','bm25_test.symbols')AS q),"
            "ranked AS MATERIALIZED(SELECT id,body,body <@> to_bm25query('100ms','bm25_test.symbol_bm25')AS score "
            "FROM bm25_test.symbol_docs WHERE(SELECT numnode(q)>0 FROM p)"
            "ORDER BY body <@> to_bm25query('100ms','bm25_test.symbol_bm25')LIMIT 2)"
            "SELECT r.id,r.body,r.score,ts_headline('bm25_test.symbols',r.body,p.q,'HighlightAll=true')AS headline "
            "FROM ranked r CROSS JOIN p ORDER BY r.score")
        plan, rows = self.run("SET enable_seqscan=off;EXPLAIN(ANALYZE,FORMAT JSON,VERBOSE,COSTS OFF,TIMING OFF,SUMMARY OFF)" + topk +
            ";SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]')FROM(" + topk + ")r;", "symbol-topk-headline")
        record["topk"] = dict(plan=plan, rows=rows, query_analysis_cost="One query_any evaluation plus the necessary independent BM25 query analysis")
        target = index_nodes(plan, "symbol_bm25")
        p_nodes = [n for n in nodes(plan) if n.get("Subplan Name") == "CTE p"]
        self.require(target and all(n.get("Actual Loops", 0) > 0 for n in target), "symbol top-k did not execute BM25")
        self.require(len(p_nodes) == 1 and p_nodes[0].get("Actual Loops") == 1 and p_nodes[0].get("Actual Rows") == 1,
                     "symbol top-k constructed query_any more than once")
        self.require(len(rows) == 2 and all(row["id"] in [1, 2, 5, 6, 11] for row in rows), "symbol top-k includes a noncandidate")
        self.require(all(html.unescape(row["headline"].replace("<b>", "").replace("</b>", "")) == row["body"] and
                         "<b>" in row["headline"] for row in rows), "symbol top-k headline lost source or matching content")
        self.run("CREATE TEXT SEARCH DICTIONARY bm25_test.symbol_pre(TEMPLATE=charabia.template,Language='eng',"
            "Discard=false,Stem=false,Normalize=false,Filter=true,English=false);"
            "CREATE TEXT SEARCH CONFIGURATION bm25_test.symbol_chain(COPY=bm25_test.symbols);"
            "ALTER TEXT SEARCH CONFIGURATION bm25_test.symbol_chain ALTER MAPPING FOR numword WITH bm25_test.symbol_pre,charabia.numword;"
            "CREATE TABLE bm25_test.symbol_chain_docs(id integer PRIMARY KEY,body text NOT NULL CHECK(octet_length(body)<=262144));"
            "INSERT INTO bm25_test.symbol_chain_docs SELECT * FROM bm25_test.symbol_docs WHERE id IN(5,10);"
            "CREATE INDEX symbol_chain_bm25 ON bm25_test.symbol_chain_docs USING bm25(body)WITH(text_config='bm25_test.symbol_chain');"
            "ANALYZE bm25_test.symbol_chain_docs;", "symbol-prefilter-chain-build")
        self.check_query("bm25_test.symbol_chain", "symbol_chain_docs", "symbol_chain_bm25", "100ms", [5], "symbol-prefilter-chain-index")
        for label, sql, expected in (
            ("insert", "INSERT INTO bm25_test.symbol_docs VALUES(20,'alpha 100ms beta');", [1, 2, 5, 6, 11, 20]),
            ("update", "UPDATE bm25_test.symbol_docs SET body='replacementmarker'WHERE id=5;", [1, 2, 6, 11, 20]),
            ("delete", "DELETE FROM bm25_test.symbol_docs WHERE id=2;", [1, 6, 11, 20]),
        ):
            self.run(sql, "symbol-" + label)
            self.check_query(config, "symbol_docs", "symbol_bm25", "100ms", expected, "symbol-" + label + "-query")
        spill = self.run("SELECT to_jsonb(bm25_spill_index('bm25_test.symbol_bm25'));", "symbol-spill")[0]
        self.require(type(spill) is int and spill > 0, "symbol incremental writes did not spill")
        self.check_query(config, "symbol_docs", "symbol_bm25", "100ms", [1, 6, 11, 20], "symbol-after-spill")
        record.update(status="mechanisms-passed", final_bag_ids=[1, 6, 11, 20], spill_block=spill)
        self.checkpoint()

    def four_term_protocol(self):
        # The expected terms are retained direct Snowball/Charabia examples.
        # Raw ::tsquery predicates intentionally do not re-analyze final lexemes.
        expected = ["abbrevıate", "abbrevıa", "abbreviate", "abbrevi"]
        terms, query, positions, individual, headline = self.run(
            "SELECT to_jsonb(ts_lexize('bm25_test.four_terms_dict','ABBREVIATE'));"
            "SELECT to_jsonb(plainto_tsquery('bm25_test.four_terms','ABBREVIATE')::text);"
            "SELECT jsonb_agg(jsonb_build_object('lexeme',lexeme,'positions',positions)ORDER BY lexeme)"
            "FROM unnest(to_tsvector('bm25_test.four_terms','ABBREVIATE'));"
            "SELECT jsonb_object_agg(term,to_tsvector('bm25_test.four_terms','ABBREVIATE') @@ term::tsquery)"
            "FROM unnest(ARRAY['abbrevıate','abbrevıa','abbreviate','abbrevi'])term;"
            "SELECT to_jsonb(ts_headline('bm25_test.four_terms','left ABBREVIATE right',"
            "plainto_tsquery('bm25_test.four_terms','ABBREVIATE'),'HighlightAll=true'));",
            "four-terms-protocol")
        self.result["four_term_protocol"] = dict(terms=terms, query=query, positions=positions,
            individual_matches=individual, headline=headline,
            basis="Retained upstream Turkish/English stem samples; each variant is an independent same-position alternative")
        self.require(terms == expected, "forced Turkish/English four-term golden differs")
        self.require(query.count(" | ") == 3 and " & " not in query,
                     "four dictionary entries became AND or sequential positions")
        self.require(len(positions) == 4 and all(entry["positions"] == [1] for entry in positions),
                     "four dictionary entries do not share one PG position")
        self.require(individual == dict.fromkeys(expected, True), "a single raw variant cannot match")
        self.require(headline == "left <b>ABBREVIATE</b> right", "variant matches duplicated or lost original headline")
        self.check_query("bm25_test.four_terms", "four_docs", "four_bm25", "ABBREVIATE", [1,2,3,4,5], "four-terms-build")
        self.check_query("bm25_test.native_terms", "four_native_docs", "four_native_bm25", "ABBREVIATE", [1,2,3], "two-terms-control")
        comparison = self.run(
            "SELECT jsonb_build_object('four_bytes',pg_relation_size('bm25_test.four_bm25'),"
            "'native_bytes',pg_relation_size('bm25_test.four_native_bm25'),"
            "'four_vectors',(SELECT jsonb_agg(jsonb_build_object('id',id,'vector',to_tsvector('bm25_test.four_terms',body)::text)ORDER BY id)FROM bm25_test.four_docs),"
            "'native_vectors',(SELECT jsonb_agg(jsonb_build_object('id',id,'vector',to_tsvector('bm25_test.native_terms',body)::text)ORDER BY id)FROM bm25_test.four_native_docs));",
            "four-terms-observed-statistics")[0]
        comparison["meaning"] = "Observed vectors, position inputs and small relation sizes; scores are retained in focused_queries, with no score or storage neutrality claim"
        self.result["four_term_comparison"] = comparison
        self.run("INSERT INTO bm25_test.four_docs VALUES(20,'ABBREVIATE');", "four-terms-insert")
        self.check_query("bm25_test.four_terms", "four_docs", "four_bm25", "ABBREVIATE", [1,2,3,4,5,20], "four-terms-insert-query")
        self.run("UPDATE bm25_test.four_docs SET body='replacementcanary'WHERE id=20;", "four-terms-update")
        self.check_query("bm25_test.four_terms", "four_docs", "four_bm25", "ABBREVIATE", [1,2,3,4,5], "four-terms-update-query")
        self.run("DELETE FROM bm25_test.four_docs WHERE id=4;", "four-terms-delete")
        self.check_query("bm25_test.four_terms", "four_docs", "four_bm25", "ABBREVIATE", [1,2,3,5], "four-terms-delete-query")

    def round4_quality(self):
        fixture = json.loads((ROOT / "tests/fixtures/bm25/route16-quality.json").read_text())
        self.result["route16_quality"] = dict(basis=fixture["basis"], observations=[])
        # These ordinary native configurations are explicit controls. They do
        # not repair or excuse default-policy false positives.
        self.run("CREATE TEXT SEARCH DICTIONARY bm25_test.native_german_dict(TEMPLATE=charabia.template,"
            "Language='deu',Discard=true,Stem=true,Normalize=true,Filter=false,English=false);"
            "CREATE TEXT SEARCH DICTIONARY bm25_test.forced_italian_dict(TEMPLATE=charabia.template,"
            "Language='ita',Discard=true,Stem=true,Normalize=true,Filter=false,English=false);"
            "CREATE TEXT SEARCH CONFIGURATION bm25_test.native_german(PARSER=charabia.parser);"
            "CREATE TEXT SEARCH CONFIGURATION bm25_test.forced_italian(PARSER=charabia.parser);"
            "DO $fixture$ DECLARE l jsonb; BEGIN FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP "
            "EXECUTE format('ALTER TEXT SEARCH CONFIGURATION bm25_test.native_german ADD MAPPING FOR %I WITH bm25_test.native_german_dict',l->>'language');"
            "EXECUTE format('ALTER TEXT SEARCH CONFIGURATION bm25_test.forced_italian ADD MAPPING FOR %I WITH bm25_test.forced_italian_dict',l->>'language');"
            "END LOOP;END $fixture$;", "route16-quality-controls")

        def observe(label, body, query, config="public.charabia"):
            q, text, cfg = literal(query), literal(body), literal(config)
            record = self.run(
                f"SELECT jsonb_build_object('input',{text},'query',{q},'config',{cfg},"
                f"'query_analysis',(SELECT jsonb_agg(to_jsonb(a))FROM charabia.analyze({q})a),"
                f"'document_analysis',(SELECT jsonb_agg(to_jsonb(a))FROM charabia.analyze({text})a),"
                f"'document_vector',to_tsvector({cfg},{text})::text,'plain_query',plainto_tsquery({cfg},{q})::text,"
                f"'actual',to_tsvector({cfg},{text}) @@ plainto_tsquery({cfg},{q}),"
                f"'same_full_context',to_tsvector({cfg},{text}) @@ phraseto_tsquery({cfg},{text}),"
                f"'headline',ts_headline({cfg},{text},plainto_tsquery({cfg},{q}),'HighlightAll=true'));", label)[0]
            self.result["route16_quality"]["observations"].append(dict(label=label, **record))
            self.require(record["same_full_context"] is True, label + ": identical complete context does not match its own positions")
            restored = html.unescape(record["headline"].replace("<b>", "").replace("</b>", ""))
            self.require(restored == body, label + ": quality diagnostic headline lost original text")
            return record

        die = fixture["die"]
        observation = observe("route16-die-default", die["input"], die["query"])
        self.run("CREATE TABLE bm25_test.die_docs(id integer PRIMARY KEY,body text NOT NULL);"
            f"INSERT INTO bm25_test.die_docs VALUES(1,{literal(die['input'])}),"
            "(2,'The old trees may die when the dry summer continues without any rain.');"
            "CREATE INDEX die_bm25 ON bm25_test.die_docs USING bm25(body)WITH(text_config='public.charabia');"
            "ANALYZE bm25_test.die_docs;", "route16-die-index")
        die_ids = self.check_query("public.charabia", "die_docs", "die_bm25", die["query"], None, "route16-die-bm25")
        observation["bm25_candidate_ids"] = die_ids
        self.require((1 in die_ids) == observation["actual"], "die native query and actual BM25 candidate differ")
        self.queue_quality("german-die-precision", input=die["input"], query=die["query"],
            config="public.charabia", expected=die["expected"], actual=observation["actual"])
        control = observe("route16-die-english-disabled", die["input"], die["query"], "bm25_test.native_german")
        self.require(control["actual"] is False, "explicit native German stop control did not reject die")
        italian = fixture["italian"]
        observe("route16-italian-automatic", italian["input"], italian["query"])
        forced = observe("route16-italian-forced", italian["input"], italian["query"], "bm25_test.forced_italian")
        self.require(forced["actual"] is italian["forced_language_expected"],
                     "forced Italian native stemmer does not connect bambino/bambini")
        german = fixture["german"]
        for number, query in enumerate(german["queries"]):
            observe("route16-german-short-" + str(number), german["input"], query)
        self.result["route16_quality"]["automatic_selection_limit"] = (
            "Short Italian/German query observations are not universal language or inflection guarantees; "
            "the forced Italian algorithm and complete-context positions are independent required controls. "
            "The die precision difference remains a quality failure. Only its exact reviewed signature is a non-PASS release warning.")
        self.checkpoint()

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
        self.run(native_config_sql("bm25_test.control", stem=True, discard=False) +
            native_config_sql("bm25_test.control_new", stem=True, discard=False) +
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
            alter_control = """DO $fixture$ DECLARE l jsonb; BEGIN
 FOR l IN SELECT value FROM jsonb_array_elements(charabia.analysis_info()->'languages') LOOP
  EXECUTE format('ALTER TEXT SEARCH DICTIONARY %I.%I(Stem=false)',
    'bm25_test','control_'||(l->>'language'));
 END LOOP;
END$fixture$;"""
            rolled_back = self.run("BEGIN;" + alter_control +
                "SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb)FROM bm25_test.control_a_docs "
                "WHERE to_tsvector('bm25_test.control',body) @@ 'run'::tsquery;ROLLBACK;",
                "alter-rollback")
            self.require(rolled_back == [[9]], "native ALTER did not change analysis inside transaction")
            state("after-dictionary-rollback",[1,9],[1,9],"bm25_test.control")
            self.run(alter_control, "alter-same-oid")
            after_oid = self.session_value(session,"SELECT to_jsonb('bm25_test.control'::regconfig::oid)","config-oid-after")
            self.require(after_oid==original_oid,"native Dictionary ALTER replaced the configuration OID")
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
            self.run("ALTER TEXT SEARCH CONFIGURATION bm25_test.control_new RENAME TO control_renamed;",
                     "rename-config")
            state("renamed-config-metapage-still-valid",[1,9],[1,9],"bm25_test.control_renamed")
            negative_sql = "REINDEX INDEX bm25_test.control_a_bm25"
            output = session.query(negative_sql)
            self.sequence += 1
            stem = f"bm25-{self.sequence:03d}-rename-stale-reloption"
            (self.evidence / (stem + ".sql")).write_text(negative_sql + ";\n")
            (self.evidence / (stem + ".stdout")).write_text(output + "\n")
            self.result["configuration_rename"] = dict(sql=negative_sql,output=output,
                expected_sqlstate="42704",evidence=stem,
                source="pg_textsearch 1.5.1 build resolves text_config spelling; running queries use metapage OID")
            self.require("ERROR:" in output and "42704" in output and "control_new" in output,
                         "REINDEX did not reject the renamed configuration's stale reloption spelling")
            self.require(session.value("SELECT 1") == "1", "backend did not recover after expected REINDEX error")
            self.run("ALTER INDEX bm25_test.control_a_bm25 SET(text_config='bm25_test.control_renamed');"
                     "REINDEX INDEX bm25_test.control_a_bm25;", "rename-repair-reindex")
            state("renamed-config-spelling-repaired",[1,9],[1,9],"bm25_test.control_renamed")
            renamed_oid = self.session_value(session,"SELECT to_jsonb('bm25_test.control_renamed'::regconfig::oid)",
                                             "renamed-config-oid")
            self.require(renamed_oid == new_oid, "configuration rename changed its OID")
            self.result["configuration_oids"] = dict(original=original_oid,replacement=new_oid,renamed=renamed_oid,
                reloptions=self.session_value(session,"SELECT to_jsonb(reloptions)FROM pg_class WHERE oid='bm25_test.control_a_bm25'::regclass","new-oid-reloptions"))

    def spill(self):
        self.run("INSERT INTO bm25_test.docs VALUES(30,'spillcontrolcanary');","spill-live-canary")
        names = ("docs_bm25","no_stop_bm25","frequency_bm25","ranking_bm25")
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
        if self.result.get("symbols", {}).get("status") == "mechanisms-passed":
            self.check_query("bm25_test.symbols", "symbol_docs", "symbol_bm25", "100ms",
                             [1, 6, 11, 20], "symbol-after-restart")

    def execute(self):
        failure = None
        try:
            self.populate()
            self.symbol_protocol()
            self.matrix("bulk",full=True)
            self.four_term_protocol()
            self.known_quality()
            self.round4_quality()
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
            self.result["mechanism_status"]="passed"
            self.checkpoint()
            self.flush_quality()
            self.result["quality_status"]="passed"
            self.result["status"] = "passed"
        except BaseException as error:
            failure = error
            self.result["quality_status" if self.result.get("mechanism_status")=="passed" else "mechanism_status"]="failed"
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
