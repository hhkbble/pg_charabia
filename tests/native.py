#!/usr/bin/env python3
"""Assert real FTS results against preserved Charabia fixtures and real GIN scans."""
import json
import os
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parent.parent
ROUTES = [
    "basic", "case", "latin", "latin_swe", "greek", "marks", "arabic", "persian",
    "cj_chinese", "cj_japanese", "cj_unknown", "cj_other", "basic_swe", "case_swe",
    "greek_swe", "marks_swe", "arabic_swe", "cj_other_swe",
]


def literal(value):
    # standard_conforming_strings is explicitly enabled in every psql invocation.
    return "'" + value.replace("'", "''") + "'"


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def main():
    for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGCB_RUN_DIR"):
        if not os.environ.get(key):
            raise RuntimeError(f"{key} must identify the runner's private PostgreSQL cluster")
    evidence = Path(os.environ["PGCB_RUN_DIR"]) / "native"
    evidence.mkdir(exist_ok=False)
    default_config = "/opt/homebrew/opt/postgresql@18/bin/pg_config" if platform.system() == "Darwin" else "/usr/lib/postgresql/18/bin/pg_config"
    bindir = subprocess.check_output([os.environ.get("PG_CONFIG", default_config), "--bindir"], text=True).strip()
    psql = [str(Path(bindir) / "psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
    report = {"checks": [], "plans": [], "status": "running", "schema": f"pgcb_native_py_{os.getpid()}"}
    sequence = 0

    def checkpoint():
        (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def sql(query, label, parse=True, database=None):
        nonlocal sequence
        sequence += 1
        stem = f"{sequence:03d}-{label}"
        query = "SET standard_conforming_strings=on;\n" + query
        (evidence / f"{stem}.sql").write_text(query)
        result = subprocess.run(psql + (["-d", database] if database else []), input=query, text=True, capture_output=True, timeout=120, check=False)
        (evidence / f"{stem}.stdout").write_text(result.stdout)
        (evidence / f"{stem}.stderr").write_text(result.stderr)
        report["commands"] = sequence
        checkpoint()
        if result.returncode:
            raise RuntimeError(f"{label}: psql exit {result.returncode}; {result.stderr.strip()}")
        if parse:
            return json.loads(result.stdout)
        return result.stdout

    def check(ok, label):
        if not ok:
            raise AssertionError(label)
        report["checks"].append(label)
        checkpoint()

    schema = report["schema"]
    install_database = "pgcb_native_install_" + str(os.getpid())
    report["install_database"] = install_database
    try:
        # Exercise install/uninstall in a separately owned database; keep the runner database intact.
        sql(f"CREATE DATABASE {install_database} TEMPLATE template0;", "install-create-database", False)
        sql("CREATE EXTENSION pg_charabia;", "install-create-extension", False, install_database)
        installed = sql("SELECT to_jsonb(count(*)=18) FROM ts_token_type('charabia.parser_v1');", "install-catalog", database=install_database)
        check(installed is True, "CREATE EXTENSION installs every route in a clean database")
        sql("DROP EXTENSION pg_charabia;", "install-drop-extension", False, install_database)
        removed = sql("SELECT to_jsonb(NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='charabia') AND NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_charabia'));", "install-uninstall-clean", database=install_database)
        check(removed is True, "DROP EXTENSION removes its schema and SQL objects")
        sql("CREATE EXTENSION pg_charabia;", "install-recreate-extension", False, install_database)
        check(sql("SELECT to_jsonb(to_tsvector('charabia.default_v1','POSTGRESQL') = $v$'postgresql':1$v$::tsvector);", "install-recreate-call", database=install_database) is True,
              "CREATE EXTENSION works after uninstall")
        fixture_path = ROOT / "tests/fixtures/analysis/observed-auto-v1.json"
        fixture_file = json.loads(fixture_path.read_text())
        report["fixture_source"] = fixture_file["source"]
        fixtures = fixture_file["fixtures"]
        # All expectations below originate in the preserved direct Charabia experiment.
        values = ",".join(f"({index},{literal(item['input'])})" for index, item in enumerate(fixtures))
        actual = sql(f"""
WITH inputs(id,body) AS (VALUES {values})
SELECT jsonb_agg(jsonb_build_object(
    'id',id,
    'analysis',(SELECT coalesce(jsonb_agg(to_jsonb(a)-'ord' ORDER BY ord),'[]'::jsonb)
        FROM charabia.analyze_v1(body) WITH ORDINALITY a(raw,lemma,kind,script,language,byte_start,byte_end,char_start,char_end,route,ord)),
    'parse',(SELECT coalesce(jsonb_agg(jsonb_build_array(tokid,token) ORDER BY ord),'[]'::jsonb)
        FROM ts_parse('charabia.parser_v1',body) WITH ORDINALITY p(tokid,token,ord)),
    'debug',(SELECT coalesce(jsonb_agg(jsonb_build_object('alias',alias,'token',token,
        'dictionary',dictionary::text,'dictionaries',dictionaries::text[], 'lexemes',lexemes) ORDER BY ord),'[]'::jsonb)
        FROM ts_debug('charabia.default_v1',body) WITH ORDINALITY d(alias,description,token,dictionaries,dictionary,lexemes,ord)),
    'lexize',(SELECT coalesce(jsonb_agg(jsonb_build_object('raw',token,'lexemes',
        ts_lexize(('charabia.norm_'||alias||'_v1')::regdictionary,token)) ORDER BY ord),'[]'::jsonb)
        FROM ts_parse('charabia.parser_v1',body) WITH ORDINALITY p(tokid,token,ord)
        JOIN ts_token_type('charabia.parser_v1') t USING(tokid)),
    'vector',(SELECT coalesce(jsonb_object_agg(lexeme,positions),'{{}}'::jsonb)
        FROM unnest(to_tsvector('charabia.default_v1',body)))
) ORDER BY id) FROM inputs;
""", "recorded-fixtures")
        check(len(actual) == len(fixtures) == 9, "all nine independent fixtures are executed")
        token_count = 0
        for item, observed in zip(fixtures, actual):
            expected = item["tokens"]
            token_count += len(expected)
            name = f"fixture {observed['id']}"
            check(observed["analysis"] == expected, name + " diagnostic metadata and UTF-8 offsets")
            check(observed["parse"] == [[ROUTES.index(token["route"]) + 1, token["raw"]] for token in expected],
                  name + " original spans and route IDs")
            check("".join(token[1] for token in observed["parse"]) == item["input"], name + " exact raw reconstruction")
            expected_vector = {}
            expected_debug = []
            expected_lexize = []
            position = 0
            for token in expected:
                word = token["kind"] == "word" and token["lemma"] != ""
                lexemes = [token["lemma"]] if word else ([] if token["kind"] == "stopword" else None)
                dictionary = "charabia.norm_" + token["route"] + "_v1"
                # PG18 system_functions.sql selects the first mapped dictionary even
                # when all mapped dictionaries return NULL; lexemes records recognition.
                expected_debug.append({"alias": token["route"], "token": token["raw"],
                                       "dictionary": dictionary,
                                       "dictionaries": [dictionary], "lexemes": lexemes})
                expected_lexize.append({"raw": token["raw"], "lexemes": lexemes})
                if word or token["kind"] == "stopword":
                    position += 1
                if word:
                    expected_vector.setdefault(token["lemma"], []).append(position)
            check(observed["debug"] == expected_debug, name + " independent ts_debug dictionary classification")
            check(observed["lexize"] == expected_lexize, name + " direct ts_lexize without parser state")
            check(observed["vector"] == expected_vector, name + " lexemes and native positions")
        check(token_count == 148, "all 148 independently observed token rows are checked")

        sql(f"""
CREATE SCHEMA {schema};
CREATE TABLE {schema}.docs(id integer PRIMARY KEY,body text NOT NULL,
    manual tsvector NOT NULL,
    generated tsvector GENERATED ALWAYS AS(to_tsvector('charabia.default_v1',body)) STORED);
INSERT INTO {schema}.docs(id,body,manual)
SELECT id,body,to_tsvector('charabia.default_v1',body) FROM
    (SELECT id,CASE id%6
        WHEN 0 THEN '中文全文检索 PostgreSQL 18 支持 BM25'
        WHEN 1 THEN 'PostgreSQL English documentation'
        WHEN 2 THEN 'alpha beta' WHEN 3 THEN 'alpha. beta'
        WHEN 4 THEN 'gamma delta' ELSE 'wholly unrelated banana' END AS body
    FROM generate_series(1,2400) id) AS source;
CREATE INDEX docs_expression_gin ON {schema}.docs USING gin(to_tsvector('charabia.default_v1',body));
CREATE INDEX docs_manual_gin ON {schema}.docs USING gin(manual);
CREATE INDEX docs_generated_gin ON {schema}.docs USING gin(generated);
ANALYZE {schema}.docs;
""", "create-gin", False)
        capture = """
CREATE FUNCTION pg_temp.capture_native(query text) RETURNS jsonb LANGUAGE plpgsql AS $capture$
DECLARE default_plan jsonb; index_plan jsonb; seq_plan jsonb; indexed jsonb; sequential jsonb; result_query text;
BEGIN
    result_query:='SELECT coalesce(jsonb_agg(id ORDER BY id),''[]''::jsonb) FROM ('||query||') found';
    EXECUTE 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) '||result_query INTO default_plan;
    PERFORM set_config('enable_seqscan','off',false);
    PERFORM set_config('enable_indexscan','on',false);
    PERFORM set_config('enable_bitmapscan','on',false);
    EXECUTE 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) '||result_query INTO index_plan;
    EXECUTE result_query INTO indexed;
    PERFORM set_config('enable_indexscan','off',false);
    PERFORM set_config('enable_bitmapscan','off',false);
    PERFORM set_config('enable_seqscan','on',false);
    EXECUTE 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,COSTS OFF) '||result_query INTO seq_plan;
    EXECUTE result_query INTO sequential;
    RETURN jsonb_build_object('result_query',result_query,'default_plan',default_plan,'index_plan',index_plan,'seq_plan',seq_plan,
        'indexed',indexed,'sequential',sequential);
END
$capture$;
"""
        categories = {i: i % 6 for i in range(1, 2401)}

        def gin_check(stage, column, index, query, expected):
            predicate = f"{column} @@ {query}"
            select = f"SELECT id FROM {schema}.docs WHERE {predicate}"
            result = sql(capture + "SELECT pg_temp.capture_native(" + literal(select) + ");", f"gin-{stage}-{index}-{len(report['plans'])}")
            check(result["indexed"] == sorted(expected), f"{stage} {index} fixed retrieval expectation")
            check(result["indexed"] == result["sequential"], f"{stage} {index} matches sequential oracle")
            check(any(node.get("Index Name") == index and node.get("Node Type") in ("Index Scan", "Bitmap Index Scan")
                      for node in nodes(result["index_plan"])), f"{stage} {index} real GIN scan")
            check(any(node.get("Node Type") == "Seq Scan" for node in nodes(result["seq_plan"])),
                  f"{stage} {index} oracle really scans heap")
            report["plans"].append({"stage": stage, "index": index, "query": query, "matched_ids": result["indexed"],
                                    "result_query": result["result_query"], "default": result["default_plan"], "forced": result["index_plan"], "oracle": result["seq_plan"]})
            checkpoint()

        def verify_gin(stage):
            expression = "to_tsvector('charabia.default_v1',body)"
            for query, wanted in (("全文", {0}), ("POSTGRESQL", {0, 1}), ("alpha beta", {2, 3}), ("qzxnevermatchcanary", set())):
                gin_check(stage, expression, "docs_expression_gin", f"plainto_tsquery('charabia.default_v1',{literal(query)})",
                          [i for i, category in categories.items() if category in wanted])
            gin_check(stage, expression, "docs_expression_gin", "to_tsquery('charabia.default_v1','POSTGR:*')",
                      [i for i, category in categories.items() if category in {0, 1}])
            for column in ("manual", "generated"):
                gin_check(stage, column, f"docs_{column}_gin", "plainto_tsquery('charabia.default_v1','全文')",
                          [i for i, category in categories.items() if category == 0])
            check(sql(f"SELECT to_jsonb(count(*)=0) FROM {schema}.docs WHERE manual IS DISTINCT FROM generated OR generated IS DISTINCT FROM to_tsvector('charabia.default_v1',body);", f"{stage}-stored-values") is True,
                  stage + " manual and generated vectors reflect original text")

        verify_gin("bulk")
        sql(f"INSERT INTO {schema}.docs(id,body,manual) VALUES(2401,'中文全文检索 PostgreSQL',to_tsvector('charabia.default_v1','中文全文检索 PostgreSQL'));", "gin-insert", False)
        categories[2401] = 0
        verify_gin("insert")
        sql(f"UPDATE {schema}.docs SET body='gamma delta',manual=to_tsvector('charabia.default_v1','gamma delta') WHERE id=6;", "gin-update", False)
        categories[6] = 4
        verify_gin("update")
        sql(f"DELETE FROM {schema}.docs WHERE id IN(12,2401); VACUUM ANALYZE {schema}.docs;", "gin-delete-vacuum", False)
        del categories[12]
        del categories[2401]
        verify_gin("delete-vacuum")

        # Deliberately change a copied configuration, never the extension's default.
        routes = "\n".join(
            f"CREATE TEXT SEARCH DICTIONARY {schema}.stop_{alias}(TEMPLATE=charabia.normalize_v1,Route={literal(alias)});\n"
            f"ALTER TEXT SEARCH CONFIGURATION {schema}.changing ALTER MAPPING FOR \"{alias}\" WITH {schema}.stop_{alias};"
            for alias in ROUTES)
        sql(f"""
CREATE TEXT SEARCH CONFIGURATION {schema}.changing(COPY=charabia.default_v1);
{routes}
CREATE TABLE {schema}.migration(body text,
    manual tsvector,
    generated tsvector GENERATED ALWAYS AS(to_tsvector('{schema}.changing',body)) STORED);
INSERT INTO {schema}.migration(body,manual) VALUES('alpha beta',to_tsvector('{schema}.changing','alpha beta'));
CREATE INDEX migration_expression_gin ON {schema}.migration USING gin(to_tsvector('{schema}.changing',body));
CREATE INDEX migration_manual_gin ON {schema}.migration USING gin(manual);
CREATE INDEX migration_generated_gin ON {schema}.migration USING gin(generated);
""" + "\n".join(f"ALTER TEXT SEARCH DICTIONARY {schema}.stop_{alias}(StopWords='[\"alpha\"]');" for alias in ROUTES), "change-config", False)
        migration_state = f"""SELECT jsonb_build_object('fresh',to_tsvector('{schema}.changing',body)::text,
            'manual',manual::text,'generated',generated::text) FROM {schema}.migration;"""
        state = sql(migration_state, "migration-before-reindex")
        check(state == {"fresh": "'beta':2", "manual": "'alpha':1 'beta':2", "generated": "'alpha':1 'beta':2"},
              "configuration change leaves stored and generated vectors stale")
        sql(f"REINDEX TABLE {schema}.migration;", "migration-reindex-only", False)
        check(sql(migration_state, "migration-after-reindex") == state,
              "REINDEX alone does not recompute stored or generated vector columns")
        # REINDEX of an expression evaluates the current configuration against original text.
        expr_query = f"SELECT 1 AS id FROM {schema}.migration WHERE to_tsvector('{schema}.changing',body) @@ 'alpha'::tsquery"
        rebuilt = sql(capture + "SELECT pg_temp.capture_native(" + literal(expr_query) + ");", "migration-expression-rebuilt")
        check(rebuilt["indexed"] == rebuilt["sequential"] == [], "expression REINDEX removes stale alpha lexeme")
        check(any(node.get("Index Name") == "migration_expression_gin" for node in nodes(rebuilt["index_plan"])),
              "rebuilt expression verification really uses its GIN index")
        sql(f"UPDATE {schema}.migration SET body=body,manual=to_tsvector('{schema}.changing',body); REINDEX TABLE {schema}.migration;", "migration-recompute", False)
        check(sql(migration_state, "migration-complete") == {"fresh": "'beta':2", "manual": "'beta':2", "generated": "'beta':2"},
              "recompute original text then REINDEX repairs stored and generated vectors")
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            # CREATE SCHEMA may have succeeded before another setup statement failed.
            existing = sql(f"SELECT to_jsonb(EXISTS(SELECT 1 FROM pg_namespace WHERE nspname={literal(schema)}));", "cleanup-check")
            if existing:
                sql(f"DROP SCHEMA {schema} CASCADE;", "cleanup-owned-schema", False)
            database_exists = sql(f"SELECT to_jsonb(EXISTS(SELECT 1 FROM pg_database WHERE datname={literal(install_database)}));", "cleanup-database-check")
            if database_exists:
                sql(f"DROP DATABASE {install_database};", "cleanup-owned-database", False)
            report["cleanup"] = "passed"
        except BaseException as error:
            report["cleanup"] = "failed"
            report["cleanup_error"] = f"{type(error).__name__}: {error}"
            report["status"] = "failed"
            raise
        finally:
            checkpoint()
    print(f"native: {len(report['checks'])} assertions, {len(report['plans'])} actual GIN plans, 9 fixtures/148 tokens; cleanup passed")


if __name__ == "__main__":
    main()
