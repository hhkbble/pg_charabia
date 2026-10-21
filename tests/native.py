#!/usr/bin/env python3
"""Real native FTS/GIN contracts; algorithm, adapter and index oracles stay distinct."""
import json
import os
from pathlib import Path

from support import literal, native_config_sql, run

ROOT = Path(__file__).resolve().parent.parent


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def main():
    evidence = Path(os.environ["PGCB_RUN_DIR"]) / "native"
    evidence.mkdir(exist_ok=False)
    schema = f"pgcb_native_py_{os.getpid()}"
    install_database = f"pgcb_native_install_{os.getpid()}"
    report = {"status": "running", "checks": [], "plans": [], "schema": schema,
              "install_database": install_database, "source": {
                  "algorithm": "fixtures/analysis/general/direct-lexize.json upstream/preserved goldens",
                  "adapter": "raw byte/scalar ranges, diagnostic/Parser/Dictionary/PG representation agreement",
                  "index": "fixed corpus IDs and real indexed versus heap scan; not an algorithm oracle",
                  "maintenance": "PG18 expression REINDEX evaluates raw text; stored vector REINDEX rebuilds existing values"}}
    sequence = 0

    def checkpoint():
        (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def sql(query, label, parse=True, database=None):
        nonlocal sequence
        sequence += 1
        stem = f"{sequence:03d}-{label}"
        query = "SET standard_conforming_strings=on;\n" + query
        (evidence / f"{stem}.sql").write_text(query)
        result = run(query, database=database, check=False)
        (evidence / f"{stem}.stdout").write_text(result.stdout)
        (evidence / f"{stem}.stderr").write_text(result.stderr)
        report["commands"] = sequence
        checkpoint()
        if result.returncode:
            raise AssertionError(f"{label}: psql exit {result.returncode}; {result.stderr.strip()}")
        return json.loads(result.stdout) if parse else result.stdout

    def check(condition, label):
        report["checks"].append({"label": label, "passed": bool(condition)})
        checkpoint()
        if not condition:
            raise AssertionError(label)

    # Plans include exact input and both actual execution paths, rather than a
    # planner setting alone being accepted as proof of index use.
    capture = """
CREATE FUNCTION pg_temp.capture_native(query text) RETURNS jsonb LANGUAGE plpgsql AS $capture$
DECLARE default_plan jsonb; index_plan jsonb; seq_plan jsonb; indexed jsonb; sequential jsonb; result_query text;
BEGIN
 result_query:='SELECT coalesce(jsonb_agg(id ORDER BY id),''[]''::jsonb) FROM ('||query||') found';
 EXECUTE 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) '||result_query INTO default_plan;
 PERFORM set_config('enable_seqscan','off',false);
 -- GIN uses a bitmap path. Disable ordinary B-tree scans so the ordered
 -- JSON aggregate cannot satisfy ordering through the primary key and filter.
 PERFORM set_config('enable_indexscan','off',false);
 PERFORM set_config('enable_indexonlyscan','off',false);
 PERFORM set_config('enable_bitmapscan','on',false);
 EXECUTE 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) '||result_query INTO index_plan;
 EXECUTE result_query INTO indexed;
 PERFORM set_config('enable_indexscan','off',false);
 PERFORM set_config('enable_bitmapscan','off',false);
 PERFORM set_config('enable_seqscan','on',false);
 EXECUTE 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) '||result_query INTO seq_plan;
 EXECUTE result_query INTO sequential;
 RETURN jsonb_build_object('result_query',result_query,'default_plan',default_plan,'index_plan',index_plan,
  'seq_plan',seq_plan,'indexed',indexed,'sequential',sequential);
END
$capture$;
"""
    try:
        sql(f"CREATE DATABASE {install_database} TEMPLATE template0;", "install-create-database", False)
        sql("CREATE EXTENSION pg_charabia;", "install-create-extension", False, install_database)
        check(sql("SELECT to_jsonb((SELECT count(*) FROM ts_token_type('charabia.parser'))=73 AND "
                  "(SELECT count(*) FROM pg_ts_config_map WHERE mapcfg='public.charabia'::regconfig)=71);",
                  "install-catalog", database=install_database) is True,
              "clean CREATE EXTENSION installs 73 types and 71 public default mappings")
        sql("DROP EXTENSION pg_charabia;", "install-drop-extension", False, install_database)
        uninstalled = sql("SELECT jsonb_build_object('extension_gone',NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_charabia'),"
            "'default_gone',NOT EXISTS(SELECT 1 FROM pg_ts_config c JOIN pg_namespace n ON n.oid=c.cfgnamespace WHERE n.nspname='public' AND c.cfgname='charabia'),"
            "'namespace_retained',EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='charabia'),"
            "'namespace_empty',NOT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='charabia'::regnamespace)"
            " AND NOT EXISTS(SELECT 1 FROM pg_ts_config WHERE cfgnamespace='charabia'::regnamespace)"
            " AND NOT EXISTS(SELECT 1 FROM pg_ts_dict WHERE dictnamespace='charabia'::regnamespace)"
            " AND NOT EXISTS(SELECT 1 FROM pg_ts_parser WHERE prsnamespace='charabia'::regnamespace)"
            " AND NOT EXISTS(SELECT 1 FROM pg_ts_template WHERE tmplnamespace='charabia'::regnamespace),"
            "'public_create_denied',NOT EXISTS(SELECT 1 FROM pg_namespace n,LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner)))a WHERE n.nspname='charabia'AND a.grantee=0 AND a.privilege_type='CREATE'));",
            "install-uninstall-catalog",database=install_database)
        report["uninstall_catalog"] = uninstalled
        check(all(uninstalled.values()), "DROP EXTENSION removes every member/default and retains only the protected control-created namespace")
        sql("CREATE EXTENSION pg_charabia;", "install-recreate-extension", False, install_database)
        check(sql("SELECT to_jsonb(to_tsvector('public.charabia','POSTGRESQL') @@ 'postgresql'::tsquery);",
                  "install-recreate-call", database=install_database) is True,
              "reinstallation loads the actual extension successfully")

        fixture = json.loads((ROOT / "tests/fixtures/analysis/general/direct-lexize.json").read_text())
        report["algorithm_sources"] = fixture["sources"]
        for ordinal, case in enumerate(fixture["cases"]):
            actual = sql(f"SELECT coalesce(to_jsonb(ts_lexize('charabia.{case['route']}',{literal(case['raw'])})),'null'::jsonb);",
                         f"independent-lexize-{ordinal}")
            if "contains" in case:
                check(isinstance(actual, list) and case["contains"] in actual,
                      f"route {case['route']} retains independent source word {case['contains']}")
            else:
                check(actual == case["terms"], f"independent route {case['route']} golden {ordinal}")

        # These are integration invariants, not expected language/segmentation
        # results generated by the extension and then called algorithm goldens.
        texts = ["中文全文检索 PostgreSQL 18 支持 BM25", "日本語の検索 한국어 검색",
                 "Русский язык и английский текст", "café cafe\u0301 CAFÉ İSTANBUL",
                 "The children were running through the garden while their teachers were reading books.",
                 "\t\n😀 é\u0301\u0001 1234"]
        values = ",".join(f"({i},{literal(text)})" for i, text in enumerate(texts))
        actual = sql(f"""
WITH inputs(id,body) AS(VALUES {values}) SELECT jsonb_agg(jsonb_build_object(
 'id',id,'analysis',(SELECT coalesce(jsonb_agg(to_jsonb(a)-'ord' ORDER BY ord),'[]')
   FROM charabia.analyze(body) WITH ORDINALITY a(raw,kind,script,charabia_language,language,language_source,
       byte_start,byte_end,char_start,char_end,token_type,ord)),
 'parse',(SELECT coalesce(jsonb_agg(jsonb_build_array(tokid,token) ORDER BY ord),'[]')
   FROM ts_parse('charabia.parser',body) WITH ORDINALITY p(tokid,token,ord)),
 'debug',(SELECT coalesce(jsonb_agg(jsonb_build_object('alias',alias,'token',token,'dictionary',dictionary::text,
       'dictionaries',dictionaries::text[],'lexemes',lexemes) ORDER BY ord),'[]')
   FROM ts_debug('public.charabia',body) WITH ORDINALITY d(alias,description,token,dictionaries,dictionary,lexemes,ord)),
 'vector',(SELECT coalesce(jsonb_object_agg(lexeme,positions),'{{}}') FROM unnest(to_tsvector('public.charabia',body)))
) ORDER BY id) FROM inputs;
""", "adapter-invariants")
        aliases = sql("SELECT jsonb_object_agg(tokid,alias) FROM ts_token_type('charabia.parser');", "route-identities")
        for text, item in zip(texts, actual):
            byte_text = text.encode()
            check("".join(token for _, token in item["parse"]) == text,
                  f"input {item['id']} Parser reconstructs original Unicode text")
            check(len(item["analysis"]) == len(item["parse"]) == len(item["debug"]),
                  f"input {item['id']} all diagnostic paths visit the same original spans")
            positions, previous_byte, previous_char, expected_vector = 0, 0, 0, {}
            for token, parsed, debug in zip(item["analysis"], item["parse"], item["debug"]):
                raw = token["raw"]
                check(token["byte_start"] == previous_byte and token["char_start"] == previous_char,
                      f"input {item['id']} continuous byte/scalar starts")
                check(byte_text[token["byte_start"]:token["byte_end"]].decode() == raw and
                      token["byte_end"] == previous_byte + len(raw.encode()) and
                      token["char_end"] == previous_char + len(raw),
                      f"input {item['id']} offsets reference raw bytes and Unicode scalars")
                check(parsed[1] == raw == debug["token"] and aliases[str(parsed[0])] == token["token_type"] == debug["alias"],
                      f"input {item['id']} explicit route and original span reach PG Dictionary")
                if token["kind"] == "word":
                    check(debug["dictionaries"] == ["charabia." + token["token_type"]],
                          f"input {item['id']} language mapping selects its default dictionary")
                    check(token["language"] == token["token_type"],
                          f"input {item['id']} selected Parser language matches lexical type")
                else:
                    check(debug["dictionaries"] == [] and debug["lexemes"] is None and token["language"] is None,
                          f"input {item['id']} blank/display remains unmapped and has no selected Dictionary language")
                # This checks adapter position accounting only. These dictionary
                # results are not accepted as independent algorithm goldens.
                if debug["lexemes"] is not None:
                    positions += 1
                    for term in debug["lexemes"]:
                        expected_vector.setdefault(term, []).append(positions)
                previous_byte, previous_char = token["byte_end"], token["char_end"]
            check(previous_byte == len(byte_text) and previous_char == len(text),
                  f"input {item['id']} complete raw range coverage")
            check(item["vector"] == expected_vector,
                  f"input {item['id']} PG assigns variant positions and Stop/Ignore distances correctly")

        stable = f"{schema}.stable"
        sql(f"""
CREATE SCHEMA {schema};
{native_config_sql(stable)}
CREATE TABLE {schema}.docs(id integer PRIMARY KEY,body text NOT NULL,manual tsvector NOT NULL,
 generated tsvector GENERATED ALWAYS AS(to_tsvector('{stable}',body)) STORED);
INSERT INTO {schema}.docs(id,body,manual) SELECT id,body,to_tsvector('{stable}',body) FROM
 (SELECT id,CASE id%6 WHEN 0 THEN '中文全文检索 PostgreSQL 18 支持 BM25'
 WHEN 1 THEN 'PostgreSQL English documentation' WHEN 2 THEN 'alpha beta' WHEN 3 THEN 'alpha. beta'
 WHEN 4 THEN 'gamma delta' ELSE 'wholly unrelated banana' END AS body FROM generate_series(1,600) id) source;
CREATE INDEX docs_expression_gin ON {schema}.docs USING gin(to_tsvector('{stable}',body));
CREATE INDEX docs_manual_gin ON {schema}.docs USING gin(manual);
CREATE INDEX docs_generated_gin ON {schema}.docs USING gin(generated);
ANALYZE {schema}.docs;
""", "create-gin", False)
        categories = {i: i % 6 for i in range(1, 601)}

        def gin_check(stage, column, index, query, expected, table="docs"):
            select = f"SELECT id FROM {schema}.{table} WHERE {column} @@ {query}"
            result = sql(capture + "SELECT pg_temp.capture_native(" + literal(select) + ");",
                         f"gin-{stage}-{index}-{len(report['plans'])}")
            check(result["indexed"] == sorted(expected), f"{stage} {index} independent corpus ID expectation")
            check(result["indexed"] == result["sequential"], f"{stage} {index} heap/index consistency")
            check(any(node.get("Index Name") == index and node.get("Node Type") in ("Index Scan", "Bitmap Index Scan")
                      for node in nodes(result["index_plan"])), f"{stage} {index} actual GIN scan")
            check(any(node.get("Node Type") == "Seq Scan" for node in nodes(result["seq_plan"])),
                  f"{stage} {index} independent execution path scans heap")
            report["plans"].append({"stage": stage, "index": index, "query": query, "matched_ids": result["indexed"],
                                    "default": result["default_plan"], "forced": result["index_plan"], "oracle": result["seq_plan"]})
            checkpoint()

        def verify_gin(stage):
            expression = f"to_tsvector('{stable}',body)"
            for query, wanted in (("全文", {0}), ("POSTGRESQL", {0, 1}), ("alpha beta", {2, 3}), ("qzxnevermatchcanary", set())):
                gin_check(stage, expression, "docs_expression_gin", f"plainto_tsquery('{stable}',{literal(query)})",
                          [i for i, category in categories.items() if category in wanted])
            gin_check(stage, expression, "docs_expression_gin", f"to_tsquery('{stable}','POSTGR:*')",
                      [i for i, category in categories.items() if category in {0, 1}])
            for column in ("manual", "generated"):
                gin_check(stage, column, f"docs_{column}_gin", f"plainto_tsquery('{stable}','全文')",
                          [i for i, category in categories.items() if category == 0])
            check(sql(f"SELECT to_jsonb(count(*)=0) FROM {schema}.docs WHERE manual IS DISTINCT FROM generated "
                      f"OR generated IS DISTINCT FROM to_tsvector('{stable}',body);", f"{stage}-stored-values") is True,
                  stage + " manual and generated vectors correspond to raw text")

        english = ("The children were running through the garden while the teachers were reading "
                   "books and sharing their happy memories together. These cats were running.")
        unrelated = "The teachers were reading books and sharing their happy stories together in the garden."
        sql(f"""
CREATE TABLE {schema}.default_docs(id integer PRIMARY KEY,body text NOT NULL);
INSERT INTO {schema}.default_docs VALUES(1,{literal(english)}),(2,{literal(unrelated)}),(3,'run');
CREATE INDEX default_expression_gin ON {schema}.default_docs USING gin(to_tsvector('public.charabia',body));
ANALYZE {schema}.default_docs;
""", "default-dual-gin", False)
        default_expression = "to_tsvector('public.charabia',body)"
        gin_check("default-surface", default_expression, "default_expression_gin", "'running'::tsquery", [1], "default_docs")
        gin_check("default-stem", default_expression, "default_expression_gin", "'run'::tsquery", [1, 3], "default_docs")
        sql(f"INSERT INTO {schema}.default_docs VALUES(4,{literal(english)});", "default-dual-insert", False)
        gin_check("default-insert", default_expression, "default_expression_gin", "'running'::tsquery", [1, 4], "default_docs")
        sql(f"UPDATE {schema}.default_docs SET body={literal(unrelated)} WHERE id=4;", "default-dual-update", False)
        gin_check("default-update", default_expression, "default_expression_gin", "'running'::tsquery", [1], "default_docs")
        sql(f"DELETE FROM {schema}.default_docs WHERE id=3;", "default-dual-delete", False)
        gin_check("default-delete", default_expression, "default_expression_gin", "'run'::tsquery", [1], "default_docs")

        verify_gin("bulk")
        sql(f"INSERT INTO {schema}.docs VALUES(601,'中文全文检索 PostgreSQL',to_tsvector('{stable}','中文全文检索 PostgreSQL'),DEFAULT);",
            "gin-insert", False)
        categories[601] = 0
        verify_gin("insert")
        sql(f"UPDATE {schema}.docs SET body='gamma delta',manual=to_tsvector('{stable}','gamma delta') WHERE id=6;", "gin-update", False)
        categories[6] = 4
        verify_gin("update")
        sql(f"DELETE FROM {schema}.docs WHERE id IN(12,601); VACUUM ANALYZE {schema}.docs;", "gin-delete-vacuum", False)
        del categories[12]
        del categories[601]
        verify_gin("delete-vacuum")

        changing = f"{schema}.changing"
        sql(f"""
CREATE TEXT SEARCH DICTIONARY {schema}.changing_eng(TEMPLATE=charabia.template,
 Language='eng',Discard=false,Stem=false,Normalize=true,Filter=false);
CREATE TEXT SEARCH CONFIGURATION {changing}(PARSER=charabia.parser);
DO $map$
DECLARE types text;
BEGIN
 SELECT string_agg(quote_ident(alias),',') INTO types FROM ts_token_type('charabia.parser') WHERE tokid<=71;
 EXECUTE 'ALTER TEXT SEARCH CONFIGURATION {changing} ADD MAPPING FOR '||types||' WITH {schema}.changing_eng';
END $map$;
CREATE TABLE {schema}.migration(id integer PRIMARY KEY,body text,manual tsvector,
 generated tsvector GENERATED ALWAYS AS(to_tsvector('{changing}',body)) STORED);
INSERT INTO {schema}.migration(id,body,manual) VALUES(1,'alpha the beta',to_tsvector('{changing}','alpha the beta'));
CREATE INDEX migration_expression_gin ON {schema}.migration USING gin(to_tsvector('{changing}',body));
CREATE INDEX migration_manual_gin ON {schema}.migration USING gin(manual);
CREATE INDEX migration_generated_gin ON {schema}.migration USING gin(generated);
ALTER TEXT SEARCH DICTIONARY {schema}.changing_eng(Discard=true);
""", "change-config", False)
        state_query = f"SELECT jsonb_build_object('fresh',to_tsvector('{changing}',body)::text,'manual',manual::text,'generated',generated::text) FROM {schema}.migration;"
        state = sql(state_query, "migration-before-reindex")
        check(state == {"fresh": "'alpha':1 'beta':3", "manual": "'alpha':1 'beta':3 'the':2", "generated": "'alpha':1 'beta':3 'the':2"},
              "native ALTER changes fresh analysis while stored vectors retain old terms")
        sql(f"REINDEX TABLE {schema}.migration;", "migration-reindex-only", False)
        check(sql(state_query, "migration-after-reindex") == state,
              "REINDEX rebuilds stored indexes without reanalyzing stored vectors")
        gin_check("expression-rebuilt", f"to_tsvector('{changing}',body)", "migration_expression_gin", "'the'::tsquery", [], "migration")
        gin_check("stored-stale", "manual", "migration_manual_gin", "'the'::tsquery", [1], "migration")
        gin_check("generated-stale", "generated", "migration_generated_gin", "'the'::tsquery", [1], "migration")
        sql(f"UPDATE {schema}.migration SET body=body,manual=to_tsvector('{changing}',body); REINDEX TABLE {schema}.migration;",
            "migration-recompute", False)
        check(sql(state_query, "migration-complete") == {"fresh": "'alpha':1 'beta':3", "manual": "'alpha':1 'beta':3", "generated": "'alpha':1 'beta':3"},
              "raw-text recomputation before REINDEX repairs both stored paths")
        for column in ("manual", "generated"):
            gin_check("stored-recomputed", column, f"migration_{column}_gin", "'the'::tsquery", [], "migration")
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            if sql(f"SELECT to_jsonb(EXISTS(SELECT 1 FROM pg_namespace WHERE nspname={literal(schema)}));", "cleanup-check"):
                sql(f"DROP SCHEMA {schema} CASCADE;", "cleanup-owned-schema", False)
            if sql(f"SELECT to_jsonb(EXISTS(SELECT 1 FROM pg_database WHERE datname={literal(install_database)}));", "cleanup-database-check"):
                sql(f"DROP DATABASE {install_database};", "cleanup-owned-database", False)
            report["cleanup"] = "passed"
        except BaseException as error:
            report["cleanup"] = "failed"
            report["cleanup_error"] = f"{type(error).__name__}: {error}"
            report["status"] = "failed"
            raise
        finally:
            checkpoint()
    print(f"native: {len(report['checks'])} assertions, {len(report['plans'])} actual GIN plans; cleanup passed")


if __name__ == "__main__":
    main()
