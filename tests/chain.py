#!/usr/bin/env python3
"""Exercise PostgreSQL's real dictionary pipeline, not ts_debug's approximation."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

from support import Session, literal, save

REPORT = {
    "status": "running", "checks": [], "commands": [], "plans": [],
    "source": {
        "protocol": "PG18 ts_parse.c LexizeExec NULL/Stop/TSL_FILTER contract",
        "synonym": "PG18 distributed synonym_sample.syn: postgresql -> pgsql; indices -> index*",
        "algorithm": "fixed Snowball running -> run and preserved interface research goldens",
        "positions": "independent literals alpha/beta and one explicit Stop hole",
        "multi_position": "PG18 ts_parse.c TSL_ADDPOS; to_tsany.c pushval_morph/plain OP_AND, rewritten to OR by query_any",
        "integration": "real Parser/configuration/Dictionary/vector/index/headline calls",
    },
}
PROTOCOL = r'''
#include "postgres.h"
#include "fmgr.h"
#include "tsearch/ts_public.h"
#include "utils/builtins.h"
PG_MODULE_MAGIC;
/* These deterministic producers expose the core ABI. They are not extension hooks. */
PG_FUNCTION_INFO_V1(chain_dual);
Datum chain_dual(PG_FUNCTION_ARGS) {
    TSLexeme *r = palloc0(3 * sizeof(TSLexeme));
    r[0].lexeme = pstrdup("alpha"); r[0].flags = TSL_FILTER;
    r[1].lexeme = pstrdup("omega"); r[1].nvariant = 1;
    PG_RETURN_POINTER(r);
}
PG_FUNCTION_INFO_V1(chain_reject);
Datum chain_reject(PG_FUNCTION_ARGS) { PG_RETURN_POINTER(NULL); }
PG_FUNCTION_INFO_V1(chain_multi);
Datum chain_multi(PG_FUNCTION_ARGS) {
    TSLexeme *r;
    if (PG_GETARG_INT32(2) != 8 || memcmp(PG_GETARG_POINTER(1), "compound", 8))
        PG_RETURN_POINTER(NULL);
    r = palloc0(3 * sizeof(TSLexeme));
    r[0].lexeme = pstrdup("alpha");
    r[1].lexeme = pstrdup("beta"); r[1].flags = TSL_ADDPOS;
    PG_RETURN_POINTER(r);
}
PG_FUNCTION_INFO_V1(chain_stop);
Datum chain_stop(PG_FUNCTION_ARGS) {
    if (PG_GETARG_INT32(2) != 4 || memcmp(PG_GETARG_POINTER(1), "cafe", 4))
        PG_RETURN_POINTER(NULL);
    PG_RETURN_POINTER(palloc0(sizeof(TSLexeme)));
}
PG_FUNCTION_INFO_V1(chain_edges);
Datum chain_edges(PG_FUNCTION_ARGS) {
    char *word = pnstrdup(PG_GETARG_POINTER(1), PG_GETARG_INT32(2));
    TSLexeme *r;
    if (strcmp(word, "alpha") && strcmp(word, "beta")) {
        pfree(word); PG_RETURN_POINTER(NULL);
    }
    r = palloc0(2 * sizeof(TSLexeme)); r[0].lexeme = word;
    PG_RETURN_POINTER(r);
}
'''


def checkpoint():
    save("chain-results.json", REPORT)


def check(condition, label, evidence=None):
    REPORT["checks"].append({"label": label, "passed": bool(condition), "evidence": evidence})
    checkpoint()
    if not condition:
        raise AssertionError(label + ": " + repr(evidence))


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def main():
    schema = "pgcb_chain_" + str(os.getpid())
    owned_temp = Path(os.environ["PGCB_TEMP_DIR"]).resolve()
    protocol_dir = owned_temp / ("chain-protocol-" + str(os.getpid()))
    protocol_dir.mkdir(exist_ok=False)
    protocol_source = protocol_dir / "chain_protocol.c"
    protocol_library = protocol_dir / "chain_protocol.so"
    evidence_dir = Path(os.environ["PGCB_RUN_DIR"]) / "chain"
    evidence_dir.mkdir(exist_ok=False)
    sequence = 0
    schema_created = False

    def command(argv, label):
        result = subprocess.run([str(x) for x in argv], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
        (evidence_dir / (label + ".stdout")).write_text(result.stdout)
        (evidence_dir / (label + ".stderr")).write_text(result.stderr)
        REPORT["commands"].append({"argv": [str(x) for x in argv], "label": label,
                                   "exit_code": result.returncode})
        checkpoint()
        if result.returncode:
            raise AssertionError(label + ": " + result.stderr)
        return result.stdout.strip()

    def sql(session, statement, label, parse=False):
        nonlocal sequence
        sequence += 1
        stem = f"{sequence:03d}-{label}"
        (evidence_dir / (stem + ".sql")).write_text(statement + ";\n")
        output = session.query(statement)
        (evidence_dir / (stem + ".output")).write_text(output + "\n")
        checkpoint()
        if "ERROR:" in output:
            raise AssertionError(label + ": " + output)
        return json.loads(output) if parse else output

    def config(session, name, chain):
        sql(session, "CREATE TEXT SEARCH CONFIGURATION " + schema + "." + name +
            "(PARSER=charabia.parser);DO $fixture$ DECLARE r record;BEGIN "
            "FOR r IN SELECT alias FROM ts_token_type('charabia.parser') "
            "WHERE alias NOT IN('blank','display_only') LOOP "
            "EXECUTE format('ALTER TEXT SEARCH CONFIGURATION " + schema + "." + name +
            " ADD MAPPING FOR %I WITH " + chain + "',r.alias);END LOOP;END$fixture$", "config-" + name)

    try:
        protocol_source.write_text(PROTOCOL)
        (evidence_dir / "chain_protocol.c").write_text(PROTOCOL)
        include = command([os.environ["PG_CONFIG"], "--includedir-server"], "pg-server-include")
        pg_include = command([os.environ["PG_CONFIG"], "--includedir"], "pg-client-include")
        compiler = shlex.split(command([os.environ["PG_CONFIG"], "--cc"], "pg-compiler"))
        mode = ["-bundle", "-undefined", "dynamic_lookup"] if platform.system() == "Darwin" else ["-shared", "-fPIC"]
        cppflags = shlex.split(command([os.environ["PG_CONFIG"], "--cppflags"], "pg-cppflags"))
        command(compiler + mode + cppflags + ["-O2", "-I" + include, "-I" + pg_include, str(protocol_source),
                                   "-o", str(protocol_library)], "compile-test-protocol")
        REPORT["protocol_library"] = {
            "path": str(protocol_library),
            "sha256": hashlib.sha256(protocol_library.read_bytes()).hexdigest(),
            "file": command(["file", protocol_library], "test-protocol-architecture"),
            "production_library": False,
        }
        with Session() as session:
            sql(session, "CREATE SCHEMA " + schema, "schema")
            schema_created = True
            sql(session, "CREATE EXTENSION IF NOT EXISTS unaccent", "unaccent-extension")
            for name in ("dual", "reject", "multi", "stop", "edges"):
                sql(session, "CREATE FUNCTION " + schema + "." + name +
                    "(internal,internal,internal,internal)RETURNS internal AS " + literal(str(protocol_library)) +
                    "," + literal("chain_" + name) + " LANGUAGE c STRICT;"
                    "CREATE TEXT SEARCH TEMPLATE " + schema + "." + name + "_template(LEXIZE=" +
                    schema + "." + name + ");CREATE TEXT SEARCH DICTIONARY " + schema + "." + name +
                    "(TEMPLATE=" + schema + "." + name + "_template)", "protocol-" + name)
            for name, options in (
                ("eng", "Language='eng',Discard=false,Stem=true,Normalize=true,Filter=false"),
                ("fra_plain", "Language='fra',Discard=false,Stem=false,Normalize=false,Filter=false"),
                ("fra_fold", "Language='fra',Discard=false,Stem=false,Normalize=true,Filter=false"),
                ("filter_fold", "Language='fra',Discard=false,Stem=false,Normalize=true,Filter=true"),
                ("filter_raw", "Language='fra',Discard=false,Stem=false,Normalize=false,Filter=true"),
                ("eng_filter", "Language='eng',Discard=false,Stem=false,Normalize=true,Filter=true"),
                ("eng_stop_filter", "Language='eng',Discard=true,Stem=false,Normalize=true,Filter=true"),
            ):
                sql(session, "CREATE TEXT SEARCH DICTIONARY " + schema + "." + name +
                    "(TEMPLATE=charabia.template," + options + ")", "dictionary-" + name)
            sql(session, "CREATE TEXT SEARCH DICTIONARY " + schema + ".synonyms("
                "TEMPLATE=pg_catalog.synonym,SYNONYMS='synonym_sample');"
                "CREATE TEXT SEARCH DICTIONARY " + schema + ".simple_reject("
                "TEMPLATE=pg_catalog.simple,StopWords=english,Accept=false)", "native-dictionaries")
            check(sql(session, "SELECT to_json(ts_lexize(" + literal(schema + ".synonyms") +
                      ",'PostgreSQL'))", "synonym-upstream-fixture", True) == ["pgsql"],
                  "distributed synonym fixture has its fixed upstream meaning")
            chains = {
                "syn_first": schema + ".synonyms," + schema + ".eng",
                "terminal_first": schema + ".eng," + schema + ".synonyms",
                "filter_syn": schema + ".eng_filter," + schema + ".synonyms,pg_catalog.simple",
                "filter_simple": schema + ".filter_fold,pg_catalog.simple",
                "raw_filter_simple": schema + ".filter_raw,pg_catalog.simple",
                "snow_first": "pg_catalog.english_stem," + schema + ".eng",
                "filter_snow": schema + ".eng_filter,pg_catalog.english_stem",
                "sole_filter": schema + ".filter_fold",
                "last_filter": schema + ".reject," + schema + ".filter_fold",
                "filter_reject": schema + ".filter_fold," + schema + ".reject",
                "reject_simple": schema + ".filter_fold," + schema + ".reject,pg_catalog.simple",
                "filter_stop": schema + ".eng_filter," + schema + ".stop," + schema + ".edges",
                "reject_edges": schema + ".eng_filter," + schema + ".reject," + schema + ".edges",
                "native_stop": schema + ".eng_filter," + schema + ".simple_reject," + schema + ".edges",
                "upstream_stop": schema + ".eng_stop_filter,pg_catalog.simple",
                "unaccent": "public.unaccent," + schema + ".fra_plain",
                "dual_filter": schema + ".dual,pg_catalog.simple",
                "multi_position": schema + ".multi,pg_catalog.simple",
                "simple": "pg_catalog.simple",
            }
            for name, chain in chains.items():
                config(session, name, chain)
            # Literal vectors distinguish current replacement bytes from original input,
            # a Stop hole from a NULL rejection, and terminal from pipeline semantics.
            cases = [
                ("syn_first", "PostgreSQL", "'pgsql':1"),
                ("syn_first", "running", "'run':1 'running':1"),
                ("terminal_first", "PostgreSQL", "'postgresql':1"),
                ("terminal_first", "running", "'run':1 'running':1"),
                ("filter_syn", "PostgreSQL", "'pgsql':1"),
                ("filter_simple", "CAFÉ", "'cafe':1"),
                ("raw_filter_simple", "CAFÉ", "'café':1"),
                ("snow_first", "running", "'run':1"),
                ("filter_snow", "running", "'run':1"),
                ("filter_snow", "the", ""),
                ("sole_filter", "CAFÉ", ""),
                ("last_filter", "CAFÉ", ""),
                ("filter_reject", "CAFÉ", ""),
                ("reject_simple", "CAFÉ", "'cafe':1"),
                ("filter_stop", "alpha cafe beta", "'alpha':1 'beta':3"),
                ("reject_edges", "alpha cafe beta", "'alpha':1 'beta':2"),
                ("native_stop", "alpha cafe the beta", "'alpha':1 'beta':3"),
                ("upstream_stop", "alpha the beta", "'alpha':1 'beta':3"),
                ("unaccent", "Hôtel", "'hotel':1"),
                ("dual_filter", "word", "'alpha':1"),
                ("multi_position", "compound tail", "'alpha':1 'beta':2 'tail':3"),
                ("simple", "alpha, beta", "'alpha':1 'beta':2"),
            ]
            for name, raw, expected in cases:
                actual = sql(session, "SELECT to_tsvector(" + literal(schema + "." + name) + "," +
                             literal(raw) + ")::text", "vector-" + name)
                check(actual == expected, "real dictionary chain " + name,
                      {"input": raw, "expected": expected, "actual": actual})
            # Display source is never reconstructed from the external synonym lexeme.
            actual = sql(session, "SELECT ts_headline(" + literal(schema + ".filter_syn") +
                         ",'PostgreSQL & <tag>',plainto_tsquery(" + literal(schema + ".filter_syn") +
                         ",'PostgreSQL'),'HighlightAll=true')", "external-headline")
            check(actual == "<b>PostgreSQL</b> &amp; &lt;tag&gt;",
                  "real filtered synonym result matches original headline bytes", actual)
            check(sql(session, "SELECT charabia.query_any('PostgreSQL'," +
                      literal(schema + ".filter_syn") + ")::text", "external-query-any") == "'pgsql'",
                  "query_any accepts arbitrary valid native dictionary chains")
            multi = literal(schema + ".multi_position")
            # Plain analysis uses AND even for positions added by an external
            # dictionary. Phrase analysis is a separate native operator path.
            check(sql(session, "SELECT plainto_tsquery(" + multi + ",'compound')::text",
                      "multi-position-plain") == "'alpha' & 'beta'",
                  "external TSL_ADDPOS plain analysis keeps both terms")
            check(sql(session, "SELECT charabia.query_any('compound'," + multi + ")::text",
                      "multi-position-query-any") == "'alpha' | 'beta'",
                  "query_any rewrites the fresh multi-position plain AND")
            check(sql(session, "SELECT to_json(to_tsvector(" + multi + ",'compound') @@ phraseto_tsquery(" +
                      multi + ",'compound'))", "multi-position-phrase", True) is True,
                  "native phrase query matches the external dictionary positions")
            actual = sql(session, "SELECT ts_headline(" + multi + ",'compound tail',charabia.query_any('compound'," +
                         multi + "),'HighlightAll=true')", "multi-position-headline")
            check(actual == "<b>compound</b> tail",
                  "multi-position matching renders the original token exactly once", actual)
            # Both the parser's types and actual dictionaries take part in index build,
            # mutation and query analysis. Expected IDs are independent fixed literals.
            sql(session, "CREATE TABLE " + schema + ".docs(id integer PRIMARY KEY,body text NOT NULL);"
                "INSERT INTO " + schema + ".docs VALUES(1,'PostgreSQL'),(2,'unrelated banana');"
                "INSERT INTO " + schema + ".docs SELECT g,'cobaltxylophone'FROM generate_series(3,100)g;"
                "CREATE INDEX chain_gin ON " + schema + ".docs USING gin(to_tsvector(" +
                literal(schema + ".filter_syn") + ",body));ANALYZE " + schema + ".docs", "index-create")
            linux = os.environ["PGCB_PLATFORM"] == "linux-amd64"
            if linux:
                sql(session, "CREATE INDEX chain_bm25 ON " + schema + ".docs USING bm25(body)WITH(text_config=" +
                    literal(schema + ".filter_syn") + ")", "bm25-create")

            def index_assert(expected, state):
                query = ("SELECT id FROM " + schema + ".docs WHERE to_tsvector(" +
                         literal(schema + ".filter_syn") + ",body) @@ charabia.query_any('PostgreSQL'," +
                         literal(schema + ".filter_syn") + ")")
                default_plan = sql(session, "EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) " + query,
                                   state + "-gin-default-plan", True)
                sql(session, "SET enable_seqscan=off;SET enable_indexscan=off;SET enable_indexonlyscan=off;"
                    "SET enable_bitmapscan=on", state + "-gin-settings")
                plan = sql(session, "EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) " + query,
                           state + "-gin-index-plan", True)
                actual = sql(session, "SELECT coalesce(json_agg(id ORDER BY id),'[]'::json)FROM(" + query + ")d",
                             state + "-gin-results", True)
                check(actual == expected and any(n.get("Index Name") == "chain_gin" and
                      n.get("Node Type") == "Bitmap Index Scan" and n.get("Actual Loops", 0) > 0 for n in nodes(plan)),
                      "external chain GIN candidate/plan " + state, {"expected": expected, "actual": actual})
                REPORT["plans"].append({"state": state, "kind": "gin", "default": default_plan, "actual": plan})
                sql(session, "RESET enable_seqscan;RESET enable_indexscan;RESET enable_indexonlyscan;"
                    "RESET enable_bitmapscan", state + "-gin-reset")
                if linux:
                    query = ("SELECT id FROM " + schema + ".docs ORDER BY body <@> to_bm25query('PostgreSQL'," +
                             literal(schema + ".chain_bm25") + ")LIMIT 100")
                    plan = sql(session, "EXPLAIN(ANALYZE,FORMAT JSON,COSTS OFF,TIMING OFF) " + query,
                               state + "-bm25-plan", True)
                    actual = sql(session, "SELECT coalesce(json_agg(id ORDER BY id),'[]'::json)FROM(" + query + ")d",
                                 state + "-bm25-results", True)
                    check(actual == expected and any(n.get("Index Name") == "chain_bm25" and
                          n.get("Node Type") == "Index Scan" and n.get("Actual Loops", 0) > 0 for n in nodes(plan)),
                          "external chain BM25 candidate/plan " + state, {"expected": expected, "actual": actual})
                    REPORT["plans"].append({"state": state, "kind": "bm25", "actual": plan})
                    checkpoint()

            index_assert([1], "initial")
            sql(session, "INSERT INTO " + schema + ".docs VALUES(200,'PostgreSQL')", "insert")
            index_assert([1, 200], "inserted")
            sql(session, "UPDATE " + schema + ".docs SET body='banana'WHERE id=1", "update")
            index_assert([200], "updated")
            sql(session, "DELETE FROM " + schema + ".docs WHERE id=200", "delete")
            index_assert([], "deleted")
            if linux:
                maps = Path(f"/proc/{session.pid}/maps").read_text()
            else:
                maps = command(["lsof", "-p", str(session.pid)], "protocol-backend-lsof")
            (evidence_dir / "protocol-backend-maps.txt").write_text(maps)
            check(str(protocol_library) in maps,
                  "the backend really loaded the owned protocol module", REPORT["protocol_library"])
            sql(session, "DROP SCHEMA " + schema + " CASCADE", "schema-cleanup")
            schema_created = False
        REPORT["status"] = "passed"
    except BaseException as error:
        REPORT["status"] = "failed"
        REPORT["error"] = repr(error)
        raise
    finally:
        primary = sys.exception()
        try:
            # Session has already exited, so no backend still maps the test module.
            if schema_created:
                with Session() as cleanup:
                    cleanup.value("DROP SCHEMA " + schema + " CASCADE")
                schema_created = False
            shutil.rmtree(protocol_dir)
            REPORT["cleanup"] = {"schema_removed": not schema_created, "protocol_directory_removed": not protocol_dir.exists()}
        except BaseException as cleanup_error:
            REPORT["cleanup_error"] = repr(cleanup_error)
            REPORT["status"] = "failed"
            if primary is None:
                raise
            primary.add_note("chain fixture cleanup also failed: " + repr(cleanup_error))
        finally:
            checkpoint()


if __name__ == "__main__":
    main()
